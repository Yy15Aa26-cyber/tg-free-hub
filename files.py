"""File library: index files that already live on Telegram, search them, send them.

Telegram is the storage. We only keep (file_id, chat_id, message_id, name, size) so the server
needs no disk and no bandwidth, and files of any size (up to 2GB / 4GB) can be re-sent because
sending an existing file_id or copying the original message never re-uploads anything.
"""
import re
import time

from .db import DB
from .telegram import TG, TGError


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def extract_media(msg: dict) -> dict | None:
    """Pull a document/video/audio out of a Telegram message dict."""
    caption = (msg.get("caption") or "").strip()
    for kind in ("document", "video", "audio", "animation"):
        m = msg.get(kind)
        if not m:
            continue
        uid = m.get("file_unique_id")
        name = m.get("file_name") or m.get("title")
        if not name:
            first = caption.splitlines()[0][:80] if caption else ""
            ext = (m.get("mime_type") or "").split("/")[-1] or "bin"
            name = f"{first or kind + '_' + (uid or '')[:8]}.{ext}" if first or uid else f"{kind}.{ext}"
        return {
            "file_unique_id": uid,
            "file_id": m["file_id"],
            "name": name,
            "caption": caption,
            "size": int(m.get("file_size") or 0),
            "mime": m.get("mime_type") or "",
            "kind": "video" if kind in ("video", "animation") else kind,
            "chat_id": msg["chat"]["id"],
            "message_id": msg["message_id"],
        }
    return None


def index_message(db: DB, msg: dict) -> dict | None:
    media = extract_media(msg)
    if not media:
        return None
    existing = db.one("SELECT id FROM files WHERE file_unique_id=?", (media["file_unique_id"],))
    if existing:
        return None
    media["id"] = db.x(
        "INSERT INTO files(file_unique_id,file_id,name,caption,size,mime,kind,chat_id,message_id,added) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (media["file_unique_id"], media["file_id"], media["name"], media["caption"], media["size"],
         media["mime"], media["kind"], media["chat_id"], media["message_id"], time.time()),
    )
    return media


def _row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "caption": r["caption"], "size": r["size"],
            "size_h": human_size(r["size"]), "kind": r["kind"], "mime": r["mime"]}


def search(db: DB, query: str, limit: int = 20) -> list[dict]:
    tokens = re.findall(r"\w+", query or "", re.UNICODE)
    if not tokens:
        rows = db.q("SELECT * FROM files ORDER BY added DESC LIMIT ?", (limit,))
        return [_row(r) for r in rows]
    match = " ".join(f'"{t}"*' for t in tokens[:8])
    rows = db.q(
        "SELECT f.* FROM files_fts JOIN files f ON f.id = files_fts.rowid "
        "WHERE files_fts MATCH ? ORDER BY bm25(files_fts) LIMIT ?",
        (match, limit),
    )
    return [_row(r) for r in rows]


async def send_file(tg: TG, db: DB, file_id_db: int, to_chat: int, mode: str = "copy") -> str:
    """Sends the file to a chat. Returns a short status string. Raises LookupError / TGError."""
    r = db.one("SELECT * FROM files WHERE id=?", (file_id_db,))
    if not r:
        raise LookupError("file not found")

    async def by_copy():
        await tg.call("copyMessage", chat_id=to_chat, from_chat_id=r["chat_id"], message_id=r["message_id"])

    async def by_file_id():
        method = {"video": "sendVideo", "audio": "sendAudio"}.get(r["kind"], "sendDocument")
        field = {"video": "video", "audio": "audio"}.get(r["kind"], "document")
        if mode == "document":
            method, field = "sendDocument", "document"
        await tg.call(method, chat_id=to_chat, **{field: r["file_id"]}, caption=r["name"][:200])

    first, second = (by_copy, by_file_id) if mode != "document" else (by_file_id, by_copy)
    try:
        await first()
    except TGError:
        await second()
    return r["name"]
