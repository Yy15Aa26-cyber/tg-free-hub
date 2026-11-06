import asyncio
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
import unittest
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests import _httpx_stub  # noqa: E402

STUBBED = _httpx_stub.install()
import httpx  # noqa: E402

from app import files as filelib  # noqa: E402
from app.ai.router import AIRouter, AllProvidersFailed  # noqa: E402
from app.bot import Bot  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import DB  # noqa: E402
from app.hub import Hub  # noqa: E402
from app.security import decrypt, encrypt, make_token, read_token, validate_init_data  # noqa: E402

R = httpx.Response


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """Scripted HTTP client: routes is a list of (url_substring, [responses...]) consumed in order."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def post(self, url, headers=None, json=None, timeout=None, **kw):
        self.calls.append({"url": url, "headers": headers or {}, "json": json})
        for sub, queue in self.routes:
            if sub in url:
                item = queue.pop(0) if len(queue) > 1 else queue[0]
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError("unexpected url " + url)

    async def get(self, url, headers=None, timeout=None, **kw):
        return await self.post(url, headers=headers)


def ok_openai(text="hi"):
    return R(200, {"choices": [{"message": {"content": text}}]})


class SecurityTests(unittest.TestCase):
    def test_init_data(self):
        token = "123:ABC"
        fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": 42, "first_name": "א"})}
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        self.assertEqual(validate_init_data(urlencode(fields), token)["id"], 42)
        fields["hash"] = "0" * 64
        self.assertIsNone(validate_init_data(urlencode(fields), token))
        self.assertIsNone(validate_init_data("", token))

    def test_stale_init_data_rejected(self):
        token = "123:ABC"
        fields = {"auth_date": str(int(time.time()) - 999999), "user": json.dumps({"id": 1})}
        check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        self.assertIsNone(validate_init_data(urlencode(fields), token))

    def test_token_and_crypto(self):
        t = make_token("s", 7)
        self.assertEqual(read_token("s", t), 7)
        self.assertIsNone(read_token("other", t))
        self.assertIsNone(read_token("s", t[:-2] + "zz"))
        self.assertIsNone(read_token("s", make_token("s", 7, ttl=-5)))
        self.assertEqual(decrypt("k", encrypt("k", "secret-api-key")), "secret-api-key")
        with self.assertRaises(ValueError):
            decrypt("wrong", encrypt("k", "x"))


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.db = DB(":memory:")

    def msg(self, name, size, uid, mid=1, kind="document", caption=""):
        return {"message_id": mid, "chat": {"id": -100},
                kind: {"file_id": "F" + uid, "file_unique_id": uid, "file_name": name, "file_size": size,
                       "mime_type": "video/mp4" if kind == "video" else "application/pdf"},
                "caption": caption}

    def test_index_search_hebrew_and_size(self):
        filelib.index_message(self.db, self.msg("חשבונית מרץ.pdf", 2_500_000, "a"))
        filelib.index_message(self.db, self.msg("movie.2024.mkv", 1_610_612_736, "b", 2, "video"))
        self.assertIsNone(filelib.index_message(self.db, self.msg("חשבונית מרץ.pdf", 2_500_000, "a")))  # dedupe
        r = filelib.search(self.db, "חשבונ")
        self.assertEqual([x["name"] for x in r], ["חשבונית מרץ.pdf"])
        self.assertEqual(r[0]["size_h"], "2.4 MB")
        r = filelib.search(self.db, "movie 2024")
        self.assertEqual(r[0]["size_h"], "1.50 GB")
        self.assertEqual(len(filelib.search(self.db, "")), 2)
        self.assertEqual(filelib.search(self.db, '"; DROP TABLE files; --'), [])
        self.assertEqual(self.db.one("SELECT COUNT(*) c FROM files")["c"], 2)

    def test_delete_keeps_fts_consistent(self):
        filelib.index_message(self.db, self.msg("alpha.pdf", 10, "x"))
        self.db.x("DELETE FROM files WHERE file_unique_id='x'")
        self.assertEqual(filelib.search(self.db, "alpha"), [])


class RouterTests(unittest.TestCase):
    def make(self, routes):
        self.db = DB(":memory:")
        self.client = FakeClient(routes)
        self.r = AIRouter(self.db, "secret", self.client, 5)
        return self.r

    def test_rotation_and_cooldown_on_429(self):
        r = self.make([("groq", [R(429, {"error": {"message": "rate limit"}}, {"retry-after": "30"}), ok_openai("hello")])])
        pid = r.add_from_preset("groq", "gsk_key_number_one")
        r.add_key(pid, "gsk_key_number_two")
        res = run(r.chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(res.text, "hello")
        self.assertEqual(len(res.attempts), 1)
        keys = r.list_providers()[0]["keys"]
        self.assertTrue(any(30 >= k["cooldown"] > 25 for k in keys))
        self.assertNotIn("gsk_key_number", json.dumps(r.list_providers()))  # never leaks keys

    def test_fallback_across_providers_and_formats(self):
        r = self.make([
            ("generativelanguage", [R(503, {"error": {"message": "overloaded"}})]),
            ("groq", [R(500, {"error": {"message": "boom"}})]),
            ("anthropic.com", [R(200, {"content": [{"type": "text", "text": "from claude"}]})]),
        ])
        r.add_from_preset("gemini", "AIza_gemini_key_1")
        r.add_from_preset("groq", "gsk_groq_key_1")
        r.add_from_preset("anthropic", "sk-ant-key-1")
        res = run(r.chat([{"role": "user", "content": "yo"}], system="be nice"))
        self.assertEqual((res.provider, res.text), ("anthropic", "from claude"))
        gem = self.client.calls[0]
        self.assertEqual(gem["headers"]["x-goog-api-key"], "AIza_gemini_key_1")
        self.assertEqual(gem["json"]["systemInstruction"]["parts"][0]["text"], "be nice")
        ant = self.client.calls[-1]
        self.assertEqual(ant["headers"]["x-api-key"], "sk-ant-key-1")
        self.assertEqual(ant["json"]["system"], "be nice")

    def test_gemini_success_parsing_and_roles(self):
        r = self.make([("generativelanguage", [R(200, {"candidates": [{"content": {"parts": [{"text": "שלום"}]}}]})])])
        r.add_from_preset("gemini", "AIza_gemini_key_1")
        res = run(r.chat([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
                          {"role": "user", "content": "c"}]))
        self.assertEqual(res.text, "שלום")
        self.assertEqual([c["role"] for c in self.client.calls[0]["json"]["contents"]], ["user", "model", "user"])

    def test_bad_key_gets_long_cooldown_and_all_failed(self):
        r = self.make([("groq", [R(401, {"error": {"message": "invalid api key"}})])])
        r.add_from_preset("groq", "gsk_bad_key_000")
        with self.assertRaises(AllProvidersFailed) as cm:
            run(r.chat([{"role": "user", "content": "x"}]))
        self.assertEqual(cm.exception.attempts[0]["status"], 401)
        self.assertGreater(r.list_providers()[0]["keys"][0]["cooldown"], 80000)
        with self.assertRaises(AllProvidersFailed):  # now no usable key at all
            run(r.chat([{"role": "user", "content": "x"}]))

    def test_quota_wording_on_403_is_treated_as_rate_limit(self):
        r = self.make([("groq", [R(403, {"error": {"message": "Quota exceeded for today"}}), ok_openai("ok")])])
        pid = r.add_from_preset("groq", "gsk_key_aaaa1")
        r.add_key(pid, "gsk_key_bbbb2")
        res = run(r.chat([{"role": "user", "content": "x"}]))
        self.assertEqual(res.text, "ok")
        self.assertLess(max(k["cooldown"] for k in r.list_providers()[0]["keys"]), 3601)

    def test_timeout_moves_on(self):
        r = self.make([("groq", [httpx.TimeoutException("t"), ok_openai("late")])])
        pid = r.add_from_preset("groq", "gsk_key_aaaa1")
        r.add_key(pid, "gsk_key_bbbb2")
        self.assertEqual(run(r.chat([{"role": "user", "content": "x"}])).text, "late")

    def test_custom_provider_validation_and_dupes(self):
        r = self.make([])
        with self.assertRaises(ValueError):
            r.add_provider("x", "openai", "", "m")
        with self.assertRaises(ValueError):
            r.add_provider("x", "weird", "http://h", "m")
        pid = r.add_provider("local", "openai", "http://h:11434/v1/", "llama3")
        self.assertEqual(r.list_providers()[0]["base_url"], "http://h:11434/v1")
        r.add_key(pid, "some-long-key-1")
        with self.assertRaises(ValueError):
            r.add_key(pid, "some-long-key-1")
        with self.assertRaises(ValueError):
            r.add_key(pid, "short")

    def test_priority_order(self):
        r = self.make([("groq", [ok_openai("groq")]), ("openrouter", [ok_openai("or")])])
        a = r.add_from_preset("groq", "gsk_key_aaaa1")
        b = r.add_from_preset("openrouter", "sk-or-key-bbb1")
        r.update_provider(b, priority=1)
        self.assertEqual(run(r.chat([{"role": "user", "content": "x"}])).provider, "openrouter")
        r.update_provider(b, enabled=0)
        self.assertEqual(run(r.chat([{"role": "user", "content": "x"}])).provider, "groq")

    def test_seed(self):
        r = self.make([])
        self.assertEqual(r.seed("gemini:AIza_seed_key_1, groq:gsk_seed_key_1,bogus:xxxxxxxxxx"), 2)
        self.assertTrue(r.has_any_key())


class FakeTG:
    def __init__(self):
        self.sent, self.calls = [], []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((chat_id, text, kw))

    async def call(self, method, **p):
        self.calls.append((method, p))
        return {}

    async def send_document_bytes(self, chat_id, filename, data, caption=""):
        self.calls.append(("sendDocumentBytes", {"chat_id": chat_id, "name": filename, "len": len(data)}))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.s = Settings(bot_token="1:T", admin_ids={1}, allowed_ids={2}, secret_key="k", context_messages=4)
        self.db = DB(":memory:")
        self.tg = FakeTG()
        self.client = FakeClient([("groq", [ok_openai("תשובה")])])
        self.router = AIRouter(self.db, "k", self.client, 5)
        self.hub = Hub(self.s, self.db, self.tg, self.router)
        self.bot = Bot(self.hub)

    def msg(self, uid, text, mid=10):
        return {"update_id": 1, "message": {"message_id": mid, "chat": {"id": uid, "type": "private"},
                                            "from": {"id": uid}, "text": text}}

    def test_addkey_by_admin_deletes_message_and_ai_answers(self):
        run(self.bot.handle(self.msg(1, "/addkey groq gsk_super_secret_key")))
        self.assertTrue(self.router.has_any_key())
        self.assertIn(("deleteMessage", {"chat_id": 1, "message_id": 10}), self.tg.calls)
        run(self.bot.handle(self.msg(1, "מה המצב?")))
        self.assertEqual(self.tg.sent[-1][1], "תשובה")
        self.assertNotIn("gsk_super_secret_key", json.dumps(self.tg.sent, ensure_ascii=False))

    def test_non_admin_cannot_manage_and_stranger_blocked(self):
        run(self.bot.handle(self.msg(2, "/addkey groq gsk_hacker_key_1")))
        self.assertFalse(self.router.has_any_key())
        run(self.bot.handle(self.msg(99, "hello")))
        self.assertIn("אין לך הרשאה", self.tg.sent[-1][1])
        self.assertEqual(self.client.calls, [])

    def test_history_is_trimmed_to_context_window(self):
        self.router.add_from_preset("groq", "gsk_key_aaaa1")
        for i in range(6):
            run(self.hub.chat(1, f"msg {i}"))
        self.assertLessEqual(len(self.hub.history(1)), 4)
        sent = self.client.calls[-1]["json"]["messages"]
        self.assertLessEqual(len(sent), 1 + 4 + 1)  # system + context + new

    def test_ai_search_command_returns_files(self):
        self.router.add_from_preset("groq", "gsk_key_aaaa1")
        filelib.index_message(self.db, {"message_id": 5, "chat": {"id": -1}, "document": {
            "file_id": "F", "file_unique_id": "u", "file_name": "tax report.pdf", "file_size": 1024 * 1024}})
        self.client.routes = [("groq", [ok_openai("[[search: tax]]")])]
        out = run(self.hub.chat(1, "תמצא לי את הדוח של המס"))
        self.assertEqual(out["files"][0]["name"], "tax report.pdf")

    def test_callback_send_copies_original_message(self):
        filelib.index_message(self.db, {"message_id": 5, "chat": {"id": -1}, "document": {
            "file_id": "F", "file_unique_id": "u", "file_name": "a.zip", "file_size": 3 * 1024 ** 3}})
        run(self.bot.handle({"callback_query": {"id": "c1", "from": {"id": 2}, "data": "send:1"}}))
        self.assertEqual(self.tg.calls[0][0], "copyMessage")
        self.assertEqual(self.tg.calls[0][1]["from_chat_id"], -1)

    def test_channel_post_is_indexed(self):
        run(self.bot.handle({"channel_post": {"message_id": 9, "chat": {"id": -100, "type": "channel"}, "video": {
            "file_id": "V", "file_unique_id": "vv", "file_name": "clip.mp4", "file_size": 5}}}))
        self.assertEqual(filelib.search(self.db, "clip")[0]["kind"], "video")

    def test_backup_restore_roundtrip(self):
        self.router.add_from_preset("groq", "gsk_key_aaaa1")
        filelib.index_message(self.db, {"message_id": 5, "chat": {"id": -1}, "document": {
            "file_id": "F", "file_unique_id": "u", "file_name": "keep.pdf", "file_size": 1}})
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "b.db")
            self.db.snapshot(p)
            fresh = DB(":memory:")
            stats = fresh.restore_from(p)
        self.assertEqual((stats["providers"], stats["keys"], stats["files"]), (1, 1, 1))
        self.assertEqual(filelib.search(fresh, "keep")[0]["name"], "keep.pdf")
        # key still decryptable with the same SECRET_KEY after restore
        self.assertEqual(AIRouter(fresh, "k", None).has_any_key(), True)


if __name__ == "__main__":
    print("using stub httpx:", STUBBED)
    unittest.main(verbosity=2)
