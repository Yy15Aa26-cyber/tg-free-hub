import logging
import os
import tempfile
import time

from . import files as filelib
from .ai.presets import PRESETS
from .ai.router import AllProvidersFailed
from .hub import Hub, RateLimited
from .telegram import TGError

log = logging.getLogger("bot")

HELP = (
    "👋 ברוך הבא!\n\n"
    "• כתוב לי כל שאלה ואענה (עוזר AI)\n"
    "• /search מילות חיפוש - חיפוש קבצים וסרטונים\n"
    "• /reset - ניקוי ההקשר של השיחה\n"
    "• או פתח את האפליקציה בכפתור למטה"
)
ADMIN_HELP = (
    "\n\n🔧 פקודות מנהל:\n"
    "/providers - רשימת ספקים ומפתחות\n"
    "/addkey ספק מפתח [שם] - הוספת מפתח (ספק = gemini, groq, openrouter... או שם ספק קיים)\n"
    "/addprovider שם סוג כתובת מודל - ספק חדש (סוג: openai / gemini / anthropic)\n"
    "/setmodel ספק מודל - החלפת מודל\n"
    "/testai [ספק] - בדיקת חיבור\n"
    "/backup - שליחת גיבוי למסד\n"
    "כדי לשחזר: שלח את קובץ הגיבוי עם הכיתוב /restore\n"
    "קבצים שתשלח לכאן או לערוץ שהבוט מנהל נוספים אוטומטית לאינדקס."
)


def _kb_results(found: list[dict]) -> dict:
    rows = []
    for f in found[:10]:
        icon = {"video": "🎬", "audio": "🎵"}.get(f["kind"], "📄")
        rows.append([{"text": f"{icon} {f['name'][:45]} · {f['size_h']}", "callback_data": f"send:{f['id']}"}])
    return {"inline_keyboard": rows}


class Bot:
    def __init__(self, hub: Hub):
        self.hub = hub
        self.tg = hub.tg
        self.s = hub.s

    async def handle(self, update: dict) -> None:
        try:
            if "callback_query" in update:
                await self.on_callback(update["callback_query"])
            elif "channel_post" in update:
                filelib.index_message(self.hub.db, update["channel_post"])
            elif "message" in update:
                await self.on_message(update["message"])
        except TGError as e:
            log.warning("telegram error: %s", e)
        except Exception:
            log.exception("handler crashed")

    # ------------------------------------------------------------------
    async def on_callback(self, cb: dict) -> None:
        uid = cb["from"]["id"]
        data = cb.get("data", "")
        if data.startswith("send:") and self.hub.is_allowed(uid):
            try:
                name = await filelib.send_file(self.tg, self.hub.db, int(data[5:]), uid, self.s.send_mode)
                await self.tg.call("answerCallbackQuery", callback_query_id=cb["id"], text=f"נשלח: {name[:100]}")
            except (LookupError, TGError):
                await self.tg.call("answerCallbackQuery", callback_query_id=cb["id"],
                                   text="לא הצלחתי לשלוח. ייתכן שהקובץ הוסר.", show_alert=True)
        else:
            await self.tg.call("answerCallbackQuery", callback_query_id=cb["id"])

    async def on_message(self, m: dict) -> None:
        if m["chat"]["type"] != "private":
            return
        uid = m["from"]["id"]
        chat = m["chat"]["id"]
        text = (m.get("text") or "").strip()
        caption = (m.get("caption") or "").strip()

        if not self.hub.is_allowed(uid):
            await self.tg.send_message(chat, "⛔ אין לך הרשאה להשתמש בבוט הזה.")
            return

        # admin uploads: restore or index
        if self.hub.is_admin(uid) and filelib.extract_media(m):
            if caption.startswith("/restore"):
                return await self.restore(chat, m)
            added = filelib.index_message(self.hub.db, m)
            await self.tg.send_message(chat, f"✅ נוסף לאינדקס: {added['name']} ({filelib.human_size(added['size'])})"
                                       if added else "ℹ️ הקובץ כבר קיים באינדקס.")
            return

        if text.startswith("/"):
            await self.command(uid, chat, text)
            if text.lower().startswith("/addkey"):  # never leave API keys sitting in the chat
                try:
                    await self.tg.call("deleteMessage", chat_id=chat, message_id=m["message_id"])
                except TGError:
                    pass
            return
        if text:
            return await self.ai(uid, chat, text)

    # ------------------------------------------------------------------
    async def command(self, uid: int, chat: int, text: str) -> None:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        rest = rest.strip()
        admin = self.hub.is_admin(uid)

        if cmd in ("/start", "/help"):
            kb = None
            if self.s.public_url:
                kb = {"inline_keyboard": [[{"text": "📂 פתח את האפליקציה", "web_app": {"url": self.s.public_url}}]]}
            return await self._send(chat, HELP + (ADMIN_HELP if admin else ""), reply_markup=kb)
        if cmd == "/reset":
            self.hub.reset_history(uid)
            return await self._send(chat, "🧹 ההקשר נוקה.")
        if cmd == "/search":
            found = filelib.search(self.hub.db, rest, 10)
            if not found:
                return await self._send(chat, "לא נמצאו קבצים. נסה מילות חיפוש אחרות.")
            return await self._send(chat, f"נמצאו {len(found)} תוצאות. לחץ כדי לקבל את הקובץ:",
                                    reply_markup=_kb_results(found))
        if cmd == "/ai":
            return await self.ai(uid, chat, rest) if rest else await self._send(chat, "כתוב: /ai השאלה שלך")

        if not admin:
            return await self._send(chat, "פקודה לא מוכרת. /help")

        if cmd == "/providers":
            return await self._send(chat, self.render_providers())
        if cmd == "/addkey":
            return await self.addkey(chat, m_id=None, rest=rest)
        if cmd == "/addprovider":
            parts = rest.split()
            if len(parts) < 4:
                return await self._send(chat, "שימוש: /addprovider שם סוג כתובת מודל\nלדוגמה:\n"
                                        "/addprovider mylocal openai http://host:11434/v1 llama3")
            try:
                self.hub.router.add_provider(parts[0], parts[1], parts[2], parts[3])
                return await self._send(chat, f"✅ הספק {parts[0]} נוצר. עכשיו: /addkey {parts[0]} המפתח")
            except ValueError as e:
                return await self._send(chat, f"❌ {e}")
        if cmd == "/setmodel":
            parts = rest.split()
            row = self.hub.db.one("SELECT id FROM providers WHERE name=?", (parts[0],)) if len(parts) == 2 else None
            if not row:
                return await self._send(chat, "שימוש: /setmodel שם-ספק מודל")
            self.hub.router.update_provider(row["id"], model=parts[1])
            return await self._send(chat, "✅ המודל עודכן.")
        if cmd == "/testai":
            return await self.testai(chat, rest)
        if cmd == "/backup":
            return await self.backup(chat)
        return await self._send(chat, "פקודה לא מוכרת. /help")

    async def _send(self, chat: int, text: str, **kw):
        await self.tg.send_message(chat, text, **kw)

    # ------------------------------------------------------------------
    async def ai(self, uid: int, chat: int, text: str) -> None:
        try:
            await self.tg.call("sendChatAction", chat_id=chat, action="typing")
        except TGError:
            pass
        try:
            out = await self.hub.chat(uid, text)
        except RateLimited:
            return await self._send(chat, "⏳ יותר מדי הודעות בדקה האחרונה. נסה שוב עוד רגע.")
        except AllProvidersFailed:
            msg = "😕 כל מנועי ה-AI לא זמינים כרגע."
            if self.hub.is_admin(uid):
                msg += "\nבדוק /providers - ייתכן שחסרים מפתחות או שהמכסה נגמרה."
            return await self._send(chat, msg)
        kb = _kb_results(out["files"]) if out["files"] else None
        await self._send(chat, out["reply"], **({"reply_markup": kb} if kb else {}))

    def render_providers(self) -> str:
        provs = self.hub.router.list_providers()
        if not provs:
            return "אין ספקים עדיין. הוסף עם:\n/addkey gemini המפתח-שלך\n\nספקים מוכנים: " + ", ".join(
                k for k in PRESETS if k != "custom")
        lines = []
        for p in provs:
            lines.append(f"{'🟢' if p['enabled'] else '⚪'} {p['name']} · {p['type']} · {p['model']} (עדיפות {p['priority']})")
            for k in p["keys"]:
                state = "✅" if k["enabled"] and not k["cooldown"] else ("⏸" if k["cooldown"] else "⚪")
                extra = f" · מנוחה {k['cooldown']}ש׳" if k["cooldown"] else ""
                lines.append(f"   {state} {k['hint']} {k['label']}{extra} · שימושים {k['uses']}")
        return "\n".join(lines)

    async def addkey(self, chat: int, m_id, rest: str) -> None:
        parts = rest.split()
        if len(parts) < 2:
            return await self._send(chat, "שימוש: /addkey ספק מפתח [שם]\nלדוגמה: /addkey groq gsk_xxx")
        name, key, label = parts[0].lower(), parts[1], " ".join(parts[2:])
        router = self.hub.router
        try:
            row = self.hub.db.one("SELECT id FROM providers WHERE name=?", (name,))
            if row:
                router.add_key(row["id"], key, label)
            elif name in PRESETS and name != "custom":
                router.add_from_preset(name, key, label)
            else:
                return await self._send(chat, "ספק לא מוכר. ספקים מוכנים: " + ", ".join(
                    k for k in PRESETS if k != "custom") + "\nאו צור ספק חדש עם /addprovider")
        except ValueError as e:
            return await self._send(chat, f"❌ {e}")
        await self._send(chat, f"✅ המפתח נוסף ל-{name} (ההודעה עם המפתח נמחקת מהצ'אט).")

    async def testai(self, chat: int, name: str) -> None:
        provs = self.hub.router.list_providers()
        if name:
            provs = [p for p in provs if p["name"] == name]
        if not provs:
            return await self._send(chat, "לא נמצאו ספקים לבדיקה.")
        lines = []
        for p in provs:
            r = await self.hub.router.test_provider(p["id"])
            lines.append(f"{'✅' if r['ok'] else '❌'} {p['name']}: " + (f"{r['ms']}ms" if r["ok"] else r["error"]))
        await self._send(chat, "\n".join(lines))

    async def backup(self, chat: int) -> None:
        await self.send_backup(chat)

    async def send_backup(self, chat: int) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "hub-backup.db")
            self.hub.db.snapshot(path)
            with open(path, "rb") as f:
                data = f.read()
        name = time.strftime("hub-backup-%Y%m%d-%H%M.db")
        await self.tg.send_document_bytes(
            chat, name, data, "גיבוי. מפתחות ה-API מוצפנים ב-SECRET_KEY שלך. לשחזור: שלח אותי חזרה עם הכיתוב /restore")
        self.hub.db.dirty = False

    async def restore(self, chat: int, m: dict) -> None:
        media = filelib.extract_media(m)
        if not media or media["size"] > 15 * 1024 * 1024:
            return await self._send(chat, "❌ קובץ גיבוי לא תקין או גדול מדי.")
        data = await self.tg.download(media["file_id"])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "restore.db")
            with open(path, "wb") as f:
                f.write(data)
            try:
                stats = self.hub.db.restore_from(path)
            except Exception as e:
                return await self._send(chat, f"❌ השחזור נכשל: {e}")
        await self._send(chat, f"✅ שוחזר: {stats['providers']} ספקים, {stats['keys']} מפתחות, {stats['files']} קבצים.")
