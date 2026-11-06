"""Dynamic multi-provider AI router.

* Providers and API keys live in the DB (keys encrypted) and can be added/removed at runtime.
* Three wire protocols cover practically every model on the market:
    openai    - any OpenAI-compatible endpoint (Groq, Cerebras, OpenRouter, HF, Mistral, DeepSeek, Ollama...)
    gemini    - Google Gemini native API
    anthropic - Anthropic Messages API
* Providers are tried by priority; inside a provider keys are rotated (least recently used first).
* 429 / quota / 5xx / timeouts put the key on a temporary cooldown and the router moves on.
"""
import re
import time
from dataclasses import dataclass, field

import httpx

from ..db import DB
from ..security import decrypt, encrypt
from .presets import PRESETS

TYPES = ("openai", "gemini", "anthropic")


class ProviderError(Exception):
    def __init__(self, status: int, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after = retry_after


class AllProvidersFailed(Exception):
    def __init__(self, attempts: list[dict]):
        super().__init__("all AI providers failed")
        self.attempts = attempts


@dataclass
class ChatResult:
    text: str
    provider: str
    model: str
    attempts: list[dict] = field(default_factory=list)


def _clip(s: str, n: int = 300) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[:n]


def _retry_after(resp: httpx.Response) -> float | None:
    v = resp.headers.get("retry-after")
    if v and v.replace(".", "", 1).isdigit():
        return float(v)
    return None


class AIRouter:
    def __init__(self, db: DB, secret: str, client: httpx.AsyncClient, timeout: float = 45.0):
        self.db = db
        self.secret = secret
        self.client = client
        self.timeout = timeout

    # ---------------------------------------------------------------- management
    def add_provider(self, name: str, type_: str, base_url: str, model: str, priority: int = 100) -> int:
        if type_ not in TYPES:
            raise ValueError(f"type must be one of {TYPES}")
        if not name.strip() or not model.strip():
            raise ValueError("name and model are required")
        if type_ == "openai" and not base_url.strip():
            raise ValueError("base_url is required for OpenAI-compatible providers")
        if self.db.one("SELECT 1 FROM providers WHERE name=?", (name,)):
            raise ValueError("provider name already exists")
        return self.db.x(
            "INSERT INTO providers(name,type,base_url,model,priority) VALUES(?,?,?,?,?)",
            (name.strip(), type_, base_url.strip().rstrip("/"), model.strip(), priority),
        )

    def add_from_preset(self, preset: str, key: str | None = None, label: str = "") -> int:
        p = PRESETS.get(preset)
        if not p or preset == "custom":
            raise ValueError("unknown preset")
        row = self.db.one("SELECT id FROM providers WHERE name=?", (preset,))
        pid = row["id"] if row else self.add_provider(preset, p["type"], p["base_url"], p["model"], p["priority"])
        if key:
            self.add_key(pid, key, label)
        return pid

    def update_provider(self, pid: int, **fields) -> None:
        allowed = {"model", "base_url", "priority", "enabled", "name"}
        sets, args = [], []
        for k, v in fields.items():
            if k in allowed and v is not None:
                sets.append(f"{k}=?")
                args.append(v.strip().rstrip("/") if k == "base_url" else v)
        if sets:
            args.append(pid)
            self.db.x(f"UPDATE providers SET {', '.join(sets)} WHERE id=?", tuple(args))

    def delete_provider(self, pid: int) -> None:
        self.db.x("DELETE FROM providers WHERE id=?", (pid,))

    def add_key(self, pid: int, key: str, label: str = "") -> int:
        key = key.strip()
        if len(key) < 8 or re.search(r"\s", key):
            raise ValueError("that does not look like an API key")
        if not self.db.one("SELECT 1 FROM providers WHERE id=?", (pid,)):
            raise ValueError("provider not found")
        for r in self.db.q("SELECT key_enc FROM api_keys WHERE provider_id=?", (pid,)):
            try:
                if decrypt(self.secret, r["key_enc"]) == key:
                    raise ValueError("this key already exists for the provider")
            except ValueError as e:
                if "already exists" in str(e):
                    raise
        hint = f"{key[:4]}…{key[-4:]}"
        return self.db.x(
            "INSERT INTO api_keys(provider_id,label,key_enc,hint) VALUES(?,?,?,?)",
            (pid, label.strip(), encrypt(self.secret, key), hint),
        )

    def update_key(self, kid: int, enabled: bool | None = None, label: str | None = None, reset: bool = False):
        if enabled is not None:
            self.db.x("UPDATE api_keys SET enabled=? WHERE id=?", (1 if enabled else 0, kid))
        if label is not None:
            self.db.x("UPDATE api_keys SET label=? WHERE id=?", (label, kid))
        if reset:
            self.db.x("UPDATE api_keys SET cooldown_until=0, fails=0, last_error='' WHERE id=?", (kid,))

    def delete_key(self, kid: int) -> None:
        self.db.x("DELETE FROM api_keys WHERE id=?", (kid,))

    def list_providers(self) -> list[dict]:
        now = time.time()
        out = []
        for p in self.db.q("SELECT * FROM providers ORDER BY priority, id"):
            keys = []
            for k in self.db.q("SELECT * FROM api_keys WHERE provider_id=? ORDER BY id", (p["id"],)):
                cd = max(0, int(k["cooldown_until"] - now))
                keys.append({
                    "id": k["id"], "label": k["label"], "hint": k["hint"], "enabled": bool(k["enabled"]),
                    "cooldown": cd, "uses": k["uses"], "fails": k["fails"], "last_error": k["last_error"],
                })
            out.append({
                "id": p["id"], "name": p["name"], "type": p["type"], "base_url": p["base_url"],
                "model": p["model"], "priority": p["priority"], "enabled": bool(p["enabled"]), "keys": keys,
            })
        return out

    def seed(self, spec: str) -> int:
        """spec = 'gemini:KEY,groq:KEY' - used once on first boot."""
        n = 0
        for part in (spec or "").split(","):
            if ":" not in part:
                continue
            preset, key = part.split(":", 1)
            try:
                self.add_from_preset(preset.strip(), key.strip(), "seed")
                n += 1
            except ValueError:
                pass
        return n

    def has_any_key(self) -> bool:
        return bool(self.db.one("SELECT 1 FROM api_keys WHERE enabled=1"))

    # ---------------------------------------------------------------- calling
    async def chat(self, messages: list[dict], system: str = "", only_provider: int | None = None,
                   max_tokens: int = 1024) -> ChatResult:
        attempts: list[dict] = []
        where = "enabled=1" + (" AND id=?" if only_provider else "")
        args = (only_provider,) if only_provider else ()
        providers = self.db.q(f"SELECT * FROM providers WHERE {where} ORDER BY priority, id", args)
        for p in providers:
            now = time.time()
            keys = self.db.q(
                "SELECT * FROM api_keys WHERE provider_id=? AND enabled=1 AND cooldown_until<=? "
                "ORDER BY last_used ASC, id ASC",
                (p["id"], now),
            )
            if not keys:
                attempts.append({"provider": p["name"], "error": "no usable keys (missing or cooling down)"})
                continue
            tried = 0
            for k in keys:
                if tried >= 3:
                    break
                tried += 1
                try:
                    api_key = decrypt(self.secret, k["key_enc"])
                    text = await self._call(p, api_key, messages, system, max_tokens)
                except ProviderError as e:
                    self._penalize(k, e)
                    attempts.append({"provider": p["name"], "key": k["hint"], "status": e.status, "error": e.message})
                    if e.status in (400, 404):  # bad model / request: other keys won't help
                        break
                    continue
                except ValueError as e:  # decrypt failure
                    attempts.append({"provider": p["name"], "key": k["hint"], "error": str(e)})
                    continue
                self.db.x(
                    "UPDATE api_keys SET uses=uses+1, last_used=?, fails=0, last_error='' WHERE id=?",
                    (time.time(), k["id"]), mark_dirty=False,
                )
                return ChatResult(text=text, provider=p["name"], model=p["model"], attempts=attempts)
        raise AllProvidersFailed(attempts)

    def _penalize(self, key_row, err: ProviderError) -> None:
        fails = key_row["fails"] + 1
        if err.status == 429:
            cd = err.retry_after or min(60 * (2 ** min(fails - 1, 5)), 3600)
        elif err.status in (401, 403):
            cd = 24 * 3600
        elif err.status in (400, 404):
            cd = 0
        else:
            cd = 30
        self.db.x(
            "UPDATE api_keys SET fails=?, cooldown_until=?, last_used=?, last_error=? WHERE id=?",
            (fails, time.time() + cd if cd else 0, time.time(), f"{err.status}: {err.message}"[:300], key_row["id"]),
            mark_dirty=False,
        )

    async def _call(self, p, api_key: str, messages: list[dict], system: str, max_tokens: int) -> str:
        t = p["type"]
        try:
            if t == "openai":
                return await self._openai(p, api_key, messages, system, max_tokens)
            if t == "gemini":
                return await self._gemini(p, api_key, messages, system, max_tokens)
            if t == "anthropic":
                return await self._anthropic(p, api_key, messages, system, max_tokens)
        except httpx.TimeoutException:
            raise ProviderError(504, "timeout")
        except httpx.HTTPError as e:
            raise ProviderError(502, _clip(f"network error: {type(e).__name__}"))
        raise ProviderError(400, f"unknown provider type {t}")

    @staticmethod
    def _fail(resp: httpx.Response):
        msg = _clip(resp.text)
        try:
            j = resp.json()
            err = j.get("error", j) if isinstance(j, dict) else j
            if isinstance(err, dict):
                msg = _clip(str(err.get("message") or err))
        except ValueError:
            pass
        status = resp.status_code
        # Some providers signal quota exhaustion with 403/400; treat as rate limit.
        if status in (400, 403) and re.search(r"quota|rate.?limit|resource.?exhausted", msg, re.I):
            status = 429
        raise ProviderError(status, msg, _retry_after(resp))

    async def _openai(self, p, key, messages, system, max_tokens):
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        r = await self.client.post(
            p["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": p["model"], "messages": msgs, "max_tokens": max_tokens, "temperature": 0.7},
            timeout=self.timeout,
        )
        if r.status_code != 200:
            self._fail(r)
        try:
            return r.json()["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, ValueError):
            raise ProviderError(502, "unexpected response format")

    async def _gemini(self, p, key, messages, system, max_tokens):
        body = {
            "contents": [
                {"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["content"]}]}
                for m in messages
            ],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0.7},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        base = p["base_url"] or "https://generativelanguage.googleapis.com"
        r = await self.client.post(
            f"{base.rstrip('/')}/v1beta/models/{p['model']}:generateContent",
            headers={"x-goog-api-key": key}, json=body, timeout=self.timeout,
        )
        if r.status_code != 200:
            self._fail(r)
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(x.get("text", "") for x in parts)
        except (KeyError, IndexError, ValueError):
            raise ProviderError(502, "empty or blocked response")

    async def _anthropic(self, p, key, messages, system, max_tokens):
        msgs = list(messages)
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        body = {"model": p["model"], "max_tokens": max_tokens, "messages": msgs}
        if system:
            body["system"] = system
        base = p["base_url"] or "https://api.anthropic.com"
        r = await self.client.post(
            f"{base.rstrip('/')}/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, json=body, timeout=self.timeout,
        )
        if r.status_code != 200:
            self._fail(r)
        try:
            return "".join(b.get("text", "") for b in r.json()["content"] if b.get("type") == "text")
        except (KeyError, ValueError):
            raise ProviderError(502, "unexpected response format")

    # ---------------------------------------------------------------- tools for the admin UI
    async def test_provider(self, pid: int) -> dict:
        t0 = time.time()
        try:
            res = await self.chat([{"role": "user", "content": "Reply with the single word: pong"}],
                                  only_provider=pid, max_tokens=20)
            return {"ok": True, "ms": int((time.time() - t0) * 1000), "reply": _clip(res.text, 80)}
        except AllProvidersFailed as e:
            last = e.attempts[-1] if e.attempts else {"error": "provider not found or disabled"}
            return {"ok": False, "error": last.get("error", "failed")}

    async def list_models(self, pid: int) -> list[str]:
        p = self.db.one("SELECT * FROM providers WHERE id=?", (pid,))
        k = self.db.one("SELECT * FROM api_keys WHERE provider_id=? AND enabled=1 ORDER BY id LIMIT 1", (pid,))
        if not p or not k:
            raise ValueError("provider needs at least one key first")
        key = decrypt(self.secret, k["key_enc"])
        try:
            if p["type"] == "gemini":
                base = p["base_url"] or "https://generativelanguage.googleapis.com"
                r = await self.client.get(f"{base.rstrip('/')}/v1beta/models?pageSize=200",
                                          headers={"x-goog-api-key": key}, timeout=20)
                if r.status_code != 200:
                    self._fail(r)
                return sorted(
                    m["name"].removeprefix("models/") for m in r.json().get("models", [])
                    if "generateContent" in m.get("supportedGenerationMethods", [])
                )
            if p["type"] == "anthropic":
                base = p["base_url"] or "https://api.anthropic.com"
                r = await self.client.get(f"{base.rstrip('/')}/v1/models?limit=100",
                                          headers={"x-api-key": key, "anthropic-version": "2023-06-01"}, timeout=20)
            else:
                r = await self.client.get(p["base_url"].rstrip("/") + "/models",
                                          headers={"Authorization": f"Bearer {key}"}, timeout=20)
            if r.status_code != 200:
                self._fail(r)
            return sorted(m["id"] for m in r.json().get("data", []))
        except httpx.HTTPError as e:
            raise ProviderError(502, f"network error: {type(e).__name__}")
