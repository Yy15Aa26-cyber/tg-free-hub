import hashlib
import os
from dataclasses import dataclass, field


def _ids(raw: str) -> set[int]:
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


@dataclass
class Settings:
    bot_token: str = ""
    admin_ids: set[int] = field(default_factory=set)
    allowed_ids: set[int] = field(default_factory=set)
    allow_public: bool = False
    secret_key: str = "dev-secret-change-me"
    public_url: str = ""
    db_path: str = "data/hub.db"
    send_mode: str = "copy"
    context_messages: int = 10
    ai_timeout: float = 45.0
    cors_origins: list[str] = field(default_factory=lambda: ["*"])
    ai_keys_seed: str = ""

    @property
    def webhook_path(self) -> str:
        h = hashlib.sha256(("wh:" + self.bot_token + self.secret_key).encode()).hexdigest()[:24]
        return f"/webhook/{h}"

    @property
    def webhook_secret(self) -> str:
        return hashlib.sha256(("sec:" + self.bot_token + self.secret_key).encode()).hexdigest()[:32]


def load_settings() -> Settings:
    e = os.environ.get
    return Settings(
        bot_token=e("BOT_TOKEN", ""),
        admin_ids=_ids(e("ADMIN_IDS", "")),
        allowed_ids=_ids(e("ALLOWED_USER_IDS", "")),
        allow_public=e("ALLOW_PUBLIC", "false").lower() == "true",
        secret_key=e("SECRET_KEY", "dev-secret-change-me"),
        public_url=e("PUBLIC_URL", "").rstrip("/"),
        db_path=e("DATABASE_PATH", "data/hub.db"),
        send_mode=e("SEND_MODE", "copy"),
        context_messages=int(e("CHAT_CONTEXT_MESSAGES", "10")),
        ai_timeout=float(e("AI_TIMEOUT_SECONDS", "45")),
        cors_origins=[o.strip() for o in e("CORS_ORIGINS", "*").split(",") if o.strip()],
        ai_keys_seed=e("AI_KEYS_SEED", ""),
    )
