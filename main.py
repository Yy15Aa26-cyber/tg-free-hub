import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import files as filelib
from .ai.presets import PRESETS
from .ai.router import AIRouter, AllProvidersFailed, ProviderError
from .bot import Bot
from .config import Settings, load_settings
from .db import DB
from .hub import Hub, RateLimited
from .security import make_token, new_code, read_token, validate_init_data
from .telegram import TG, TGError

log = logging.getLogger("hub")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"


# ---------------------------------------------------------------- request models
class SendReq(BaseModel):
    id: int


class ChatReq(BaseModel):
    message: str


class ProviderReq(BaseModel):
    preset: str | None = None
    name: str | None = None
    type: str | None = None
    base_url: str | None = None
    model: str | None = None
    priority: int | None = None
    key: str | None = None
    label: str = ""


class ProviderPatch(BaseModel):
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    priority: int | None = None
    enabled: bool | None = None


class KeyReq(BaseModel):
    key: str
    label: str = ""


class KeyPatch(BaseModel):
    enabled: bool | None = None
    label: str | None = None
    reset: bool = False


class OtpReq(BaseModel):
    user_id: int


class OtpVerify(BaseModel):
    user_id: int
    code: str


# ---------------------------------------------------------------- app factory
def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None) -> FastAPI:
    s = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        http = client or httpx.AsyncClient()
        db = DB(s.db_path)
        tg = TG(s.bot_token, http)
        router = AIRouter(db, s.secret_key, http, s.ai_timeout)
        hub = Hub(s, db, tg, router)
        app.state.hub = hub
        app.state.bot = Bot(hub)
        app.state.http = http
        app.state.tasks = set()

        if s.ai_keys_seed and not db.meta_get("seeded"):
            n = router.seed(s.ai_keys_seed)
            db.meta_set("seeded", "1")
            log.info("seeded %d AI keys", n)

        bg = []
        if s.bot_token and s.public_url:
            bg.append(asyncio.create_task(_setup_telegram(s, tg)))
        if s.bot_token and s.admin_ids:
            bg.append(asyncio.create_task(_backup_loop(app)))
        yield
        for t in bg:
            t.cancel()
        if client is None:
            await http.aclose()

    app = FastAPI(title="TG Free Hub", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=s.cors_origins, allow_methods=["*"],
                       allow_headers=["*"], allow_credentials=False)
    hub_of = lambda request: request.app.state.hub  # noqa: E731

    # ------------------------------------------------------------ auth
    async def current_user(request: Request,
                           x_init_data: str | None = Header(default=None),
                           authorization: str | None = Header(default=None)) -> dict:
        hub: Hub = hub_of(request)
        uid = None
        if x_init_data:
            u = validate_init_data(x_init_data, s.bot_token)
            uid = u.get("id") if u else None
        elif authorization and authorization.lower().startswith("bearer "):
            uid = read_token(s.secret_key, authorization[7:].strip())
        if not uid:
            raise HTTPException(401, "לא מחובר")
        if not hub.is_allowed(uid):
            raise HTTPException(403, "אין לך הרשאה")
        return {"id": uid, "is_admin": hub.is_admin(uid)}

    async def admin_user(user: dict = Depends(current_user)) -> dict:
        if not user["is_admin"]:
            raise HTTPException(403, "למנהלים בלבד")
        return user

    # ------------------------------------------------------------ public
    @app.get("/health")
    async def health():
        return {"ok": True, "t": int(time.time())}

    @app.post(s.webhook_path)
    async def webhook(request: Request, x_telegram_bot_api_secret_token: str | None = Header(default=None)):
        if x_telegram_bot_api_secret_token != s.webhook_secret:
            raise HTTPException(403)
        update = await request.json()
        task = asyncio.create_task(request.app.state.bot.handle(update))
        request.app.state.tasks.add(task)
        task.add_done_callback(request.app.state.tasks.discard)
        return {"ok": True}

    @app.post("/api/auth/request")
    async def auth_request(body: OtpReq, request: Request):
        hub: Hub = hub_of(request)
        uid = body.user_id
        generic = {"ok": True}
        if not hub.is_allowed(uid):
            return generic  # do not reveal who is allowed
        row = hub.db.one("SELECT * FROM otp WHERE user_id=?", (uid,))
        if row and row["expires"] - 600 + 30 > time.time():
            raise HTTPException(429, "המתן 30 שניות לפני בקשה חדשה")
        code = new_code()
        hub.db.x("INSERT INTO otp(user_id,code,expires,tries) VALUES(?,?,?,0) "
                 "ON CONFLICT(user_id) DO UPDATE SET code=excluded.code, expires=excluded.expires, tries=0",
                 (uid, code, time.time() + 600), mark_dirty=False)
        try:
            await hub.tg.send_message(uid, f"🔐 קוד הכניסה שלך: {code}\nתקף ל-10 דקות. אל תשתף אותו.")
        except TGError:
            raise HTTPException(400, "לא הצלחתי לשלוח הודעה. שלח /start לבוט ונסה שוב.")
        return generic

    @app.post("/api/auth/verify")
    async def auth_verify(body: OtpVerify, request: Request):
        hub: Hub = hub_of(request)
        row = hub.db.one("SELECT * FROM otp WHERE user_id=?", (body.user_id,))
        if not row or row["expires"] < time.time() or row["tries"] >= 5:
            raise HTTPException(401, "הקוד לא תקף. בקש קוד חדש.")
        if body.code.strip() != row["code"]:
            hub.db.x("UPDATE otp SET tries=tries+1 WHERE user_id=?", (body.user_id,), mark_dirty=False)
            raise HTTPException(401, "קוד שגוי")
        hub.db.x("DELETE FROM otp WHERE user_id=?", (body.user_id,), mark_dirty=False)
        return {"token": make_token(s.secret_key, body.user_id), "is_admin": hub.is_admin(body.user_id)}

    # ------------------------------------------------------------ user API
    @app.get("/api/me")
    async def me(request: Request, user: dict = Depends(current_user)):
        hub: Hub = hub_of(request)
        return {**user, "ai_ready": hub.router.has_any_key(),
                "files": hub.db.one("SELECT COUNT(*) c FROM files")["c"]}

    @app.get("/api/search")
    async def api_search(request: Request, q: str = "", limit: int = 30, user: dict = Depends(current_user)):
        return {"results": filelib.search(hub_of(request).db, q, min(max(limit, 1), 50))}

    @app.post("/api/send")
    async def api_send(body: SendReq, request: Request, user: dict = Depends(current_user)):
        hub: Hub = hub_of(request)
        try:
            name = await filelib.send_file(hub.tg, hub.db, body.id, user["id"], s.send_mode)
        except LookupError:
            raise HTTPException(404, "הקובץ לא נמצא")
        except TGError:
            raise HTTPException(502, "השליחה נכשלה. ודא שהתחלת שיחה עם הבוט (/start) ושהקובץ עדיין קיים.")
        return {"ok": True, "name": name}

    @app.post("/api/chat")
    async def api_chat(body: ChatReq, request: Request, user: dict = Depends(current_user)):
        hub: Hub = hub_of(request)
        text = body.message.strip()[:4000]
        if not text:
            raise HTTPException(400, "הודעה ריקה")
        try:
            return await hub.chat(user["id"], text)
        except RateLimited:
            raise HTTPException(429, "יותר מדי הודעות. נסה שוב בעוד דקה.")
        except AllProvidersFailed as e:
            detail = ("כל מנועי ה-AI לא זמינים כרגע."
                      + (" בדוק את מסך הניהול: חסרים מפתחות או שהמכסה נגמרה." if user["is_admin"] else ""))
            return JSONResponse({"detail": detail, "attempts": e.attempts if user["is_admin"] else []}, 503)

    @app.post("/api/chat/reset")
    async def api_reset(request: Request, user: dict = Depends(current_user)):
        hub_of(request).reset_history(user["id"])
        return {"ok": True}

    # ------------------------------------------------------------ admin API
    @app.get("/api/admin/presets")
    async def presets(user: dict = Depends(admin_user)):
        return {k: {kk: vv for kk, vv in v.items()} for k, v in PRESETS.items()}

    @app.get("/api/admin/providers")
    async def providers(request: Request, user: dict = Depends(admin_user)):
        return {"providers": hub_of(request).router.list_providers()}

    @app.post("/api/admin/providers")
    async def add_provider(body: ProviderReq, request: Request, user: dict = Depends(admin_user)):
        r = hub_of(request).router
        try:
            if body.preset and body.preset != "custom":
                pid = r.add_from_preset(body.preset, body.key, body.label)
                if body.model or body.priority is not None:
                    r.update_provider(pid, model=body.model, priority=body.priority)
            else:
                pid = r.add_provider(body.name or "", body.type or "openai", body.base_url or "",
                                     body.model or "", body.priority if body.priority is not None else 100)
                if body.key:
                    r.add_key(pid, body.key, body.label)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"id": pid}

    @app.patch("/api/admin/providers/{pid}")
    async def patch_provider(pid: int, body: ProviderPatch, request: Request, user: dict = Depends(admin_user)):
        r = hub_of(request).router
        r.update_provider(pid, name=body.name, base_url=body.base_url, model=body.model, priority=body.priority,
                          enabled=None if body.enabled is None else int(body.enabled))
        return {"ok": True}

    @app.delete("/api/admin/providers/{pid}")
    async def del_provider(pid: int, request: Request, user: dict = Depends(admin_user)):
        hub_of(request).router.delete_provider(pid)
        return {"ok": True}

    @app.post("/api/admin/providers/{pid}/keys")
    async def add_key(pid: int, body: KeyReq, request: Request, user: dict = Depends(admin_user)):
        try:
            return {"id": hub_of(request).router.add_key(pid, body.key, body.label)}
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.patch("/api/admin/keys/{kid}")
    async def patch_key(kid: int, body: KeyPatch, request: Request, user: dict = Depends(admin_user)):
        hub_of(request).router.update_key(kid, body.enabled, body.label, body.reset)
        return {"ok": True}

    @app.delete("/api/admin/keys/{kid}")
    async def del_key(kid: int, request: Request, user: dict = Depends(admin_user)):
        hub_of(request).router.delete_key(kid)
        return {"ok": True}

    @app.post("/api/admin/providers/{pid}/test")
    async def test_provider(pid: int, request: Request, user: dict = Depends(admin_user)):
        return await hub_of(request).router.test_provider(pid)

    @app.get("/api/admin/providers/{pid}/models")
    async def models(pid: int, request: Request, user: dict = Depends(admin_user)):
        try:
            return {"models": await hub_of(request).router.list_models(pid)}
        except ValueError as e:
            raise HTTPException(400, str(e))
        except ProviderError as e:
            raise HTTPException(502, f"{e.status}: {e.message}")

    @app.get("/api/admin/stats")
    async def stats(request: Request, user: dict = Depends(admin_user)):
        db = hub_of(request).db
        return {"files": db.one("SELECT COUNT(*) c FROM files")["c"],
                "bytes": db.one("SELECT COALESCE(SUM(size),0) c FROM files")["c"]}

    @app.delete("/api/admin/files/{fid}")
    async def del_file(fid: int, request: Request, user: dict = Depends(admin_user)):
        hub_of(request).db.x("DELETE FROM files WHERE id=?", (fid,))
        return {"ok": True}

    @app.post("/api/admin/backup")
    async def backup_now(request: Request, user: dict = Depends(admin_user)):
        await request.app.state.bot.send_backup(user["id"])
        return {"ok": True}

    if WEB_DIR.exists():
        app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


# ---------------------------------------------------------------- background jobs
async def _setup_telegram(s: Settings, tg: TG) -> None:
    try:
        await tg.call("setWebhook", url=s.public_url + s.webhook_path, secret_token=s.webhook_secret,
                      allowed_updates=["message", "callback_query", "channel_post"], drop_pending_updates=True)
        await tg.call("setChatMenuButton", menu_button={
            "type": "web_app", "text": "📂 פתח", "web_app": {"url": s.public_url}})
        await tg.call("setMyCommands", commands=[
            {"command": "search", "description": "חיפוש קבצים וסרטונים"},
            {"command": "ai", "description": "שאלה לעוזר ה-AI"},
            {"command": "reset", "description": "ניקוי הקשר השיחה"},
            {"command": "help", "description": "עזרה"},
        ])
        log.info("webhook set to %s", s.public_url + s.webhook_path)
    except TGError as e:
        log.error("telegram setup failed: %s", e)


async def _backup_loop(app: FastAPI) -> None:
    """Free hosts wipe the disk on restart. Every 30 min, if something changed, DM a backup to the first admin."""
    hub: Hub = app.state.hub
    admin = sorted(hub.s.admin_ids)[0]
    while True:
        await asyncio.sleep(1800)
        try:
            if hub.db.dirty:
                await app.state.bot.send_backup(admin)
        except Exception as e:  # noqa: BLE001
            log.warning("auto-backup failed: %s", e)


app = create_app()
