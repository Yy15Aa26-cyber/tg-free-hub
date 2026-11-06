import re
import time
from collections import defaultdict, deque

from . import files as filelib
from .ai.router import AIRouter, AllProvidersFailed
from .config import Settings
from .db import DB
from .telegram import TG

SYSTEM_PROMPT = (
    "אתה עוזר אישי חכם בתוך בוט טלגרם. ענה בשפה שבה פונה אליך המשתמש (ברירת מחדל: עברית), "
    "בקצרה ובבהירות. אם המשתמש מבקש למצוא קובץ, סרט או מסמך מהספרייה שלו, ענה אך ורק בשורה אחת "
    "בפורמט הזה, בלי שום טקסט נוסף: [[search: מילות חיפוש]]"
)
SEARCH_CMD = re.compile(r"^\s*\[\[search:\s*(.+?)\s*\]\]\s*$", re.S | re.I)


class RateLimited(Exception):
    pass


class Hub:
    def __init__(self, settings: Settings, db: DB, tg: TG, router: AIRouter):
        self.s = settings
        self.db = db
        self.tg = tg
        self.router = router
        self._hits: dict[int, deque] = defaultdict(deque)

    # ------------------------------------------------------------ access
    def is_admin(self, uid: int) -> bool:
        return uid in self.s.admin_ids

    def is_allowed(self, uid: int) -> bool:
        return self.s.allow_public or uid in self.s.allowed_ids or self.is_admin(uid)

    def rate_limit(self, uid: int, per_minute: int = 20) -> None:
        now = time.time()
        q = self._hits[uid]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= per_minute:
            raise RateLimited()
        q.append(now)

    # ------------------------------------------------------------ chat
    def history(self, uid: int) -> list[dict]:
        n = max(1, self.s.context_messages)
        rows = self.db.q(
            "SELECT role, content FROM (SELECT * FROM chat_history WHERE user_id=? ORDER BY id DESC LIMIT ?) "
            "ORDER BY id", (uid, n))
        return [{"role": r["role"], "content": r["content"]} for r in rows]

    def reset_history(self, uid: int) -> None:
        self.db.x("DELETE FROM chat_history WHERE user_id=?", (uid,), mark_dirty=False)

    def _remember(self, uid: int, role: str, content: str) -> None:
        self.db.x("INSERT INTO chat_history(user_id,role,content,ts) VALUES(?,?,?,?)",
                  (uid, role, content, time.time()), mark_dirty=False)
        # keep only the most recent messages -> saves tokens and disk
        self.db.x(
            "DELETE FROM chat_history WHERE user_id=? AND id NOT IN "
            "(SELECT id FROM chat_history WHERE user_id=? ORDER BY id DESC LIMIT ?)",
            (uid, uid, self.s.context_messages * 2), mark_dirty=False)

    async def chat(self, uid: int, text: str) -> dict:
        """Returns {reply, provider, model, files}. Raises AllProvidersFailed / RateLimited."""
        self.rate_limit(uid)
        msgs = self.history(uid)
        # collapse to a valid alternating list ending with the new user message
        msgs.append({"role": "user", "content": text})
        res = await self.router.chat(msgs, system=SYSTEM_PROMPT)
        m = SEARCH_CMD.match(res.text or "")
        if m:
            found = filelib.search(self.db, m.group(1), 10)
            reply = (f"🔎 חיפשתי \"{m.group(1)}\" ומצאתי {len(found)} תוצאות:" if found
                     else f"🔎 לא מצאתי קבצים עבור \"{m.group(1)}\".")
            out = {"reply": reply, "files": found}
        else:
            out = {"reply": (res.text or "").strip() or "…", "files": []}
        self._remember(uid, "user", text)
        self._remember(uid, "assistant", out["reply"])
        out.update(provider=res.provider, model=res.model)
        return out
