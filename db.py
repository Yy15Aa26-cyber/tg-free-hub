import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT UNIQUE NOT NULL,
  type TEXT NOT NULL,                       -- openai | gemini | anthropic
  base_url TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 100,    -- נמוך = קודם
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS api_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  provider_id INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
  label TEXT NOT NULL DEFAULT '',
  key_enc TEXT NOT NULL,
  hint TEXT NOT NULL DEFAULT '',
  enabled INTEGER NOT NULL DEFAULT 1,
  cooldown_until REAL NOT NULL DEFAULT 0,
  uses INTEGER NOT NULL DEFAULT 0,
  fails INTEGER NOT NULL DEFAULT 0,
  last_used REAL NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  file_unique_id TEXT UNIQUE,
  file_id TEXT NOT NULL,
  name TEXT NOT NULL,
  caption TEXT NOT NULL DEFAULT '',
  size INTEGER NOT NULL DEFAULT 0,
  mime TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,                       -- document | video | audio
  chat_id INTEGER NOT NULL,
  message_id INTEGER NOT NULL,
  added REAL NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
  name, caption, content='files', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS files_ai AFTER INSERT ON files BEGIN
  INSERT INTO files_fts(rowid, name, caption) VALUES (new.id, new.name, new.caption);
END;
CREATE TRIGGER IF NOT EXISTS files_ad AFTER DELETE ON files BEGIN
  INSERT INTO files_fts(files_fts, rowid, name, caption)
  VALUES('delete', old.id, old.name, old.caption);
END;
CREATE TABLE IF NOT EXISTS chat_history(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hist ON chat_history(user_id, id);
CREATE TABLE IF NOT EXISTS otp(
  user_id INTEGER PRIMARY KEY,
  code TEXT NOT NULL,
  expires REAL NOT NULL,
  tries INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


class DB:
    """Tiny thread-safe sqlite wrapper (single connection, WAL)."""

    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.dirty = False

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def x(self, sql: str, args: tuple = (), mark_dirty: bool = True) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            if mark_dirty:
                self.dirty = True
            return cur.lastrowid

    def meta_get(self, k: str, default: str = "") -> str:
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return r["v"] if r else default

    def meta_set(self, k: str, v: str):
        self.x(
            "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
            mark_dirty=False,
        )

    def snapshot(self, dest: str):
        with self._lock:
            out = sqlite3.connect(dest)
            self.conn.backup(out)
            out.close()

    def restore_from(self, backup_path: str) -> dict:
        """Merge providers/keys/files from a backup file into the live DB (replaces them)."""
        import sqlite3 as _s
        probe = _s.connect(backup_path)
        try:
            names = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            probe.close()
        if not {"providers", "api_keys", "files"} <= names:
            raise ValueError("not a valid backup file")
        with self._lock:
            self.conn.execute("ATTACH DATABASE ? AS bk", (backup_path,))
            try:
                self.conn.execute("DELETE FROM api_keys")
                self.conn.execute("DELETE FROM providers")
                self.conn.execute("DELETE FROM files")
                self.conn.execute("INSERT INTO providers SELECT * FROM bk.providers")
                self.conn.execute("INSERT INTO api_keys SELECT * FROM bk.api_keys")
                self.conn.execute("INSERT INTO files SELECT * FROM bk.files")
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise
            finally:
                self.conn.execute("DETACH DATABASE bk")
        self.dirty = False
        return {
            "providers": self.one("SELECT COUNT(*) c FROM providers")["c"],
            "keys": self.one("SELECT COUNT(*) c FROM api_keys")["c"],
            "files": self.one("SELECT COUNT(*) c FROM files")["c"],
        }

    @staticmethod
    def now() -> float:
        return time.time()
