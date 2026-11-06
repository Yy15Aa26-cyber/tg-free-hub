"""initData validation, signed session tokens, OTP codes, key encryption."""
import base64
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl

from cryptography.fernet import Fernet, InvalidToken


def _fernet(secret: str) -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(("enc:" + secret).encode()).digest())
    return Fernet(key)


def encrypt(secret: str, plain: str) -> str:
    return _fernet(secret).encrypt(plain.encode()).decode()


def decrypt(secret: str, token: str) -> str:
    try:
        return _fernet(secret).decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ValueError("cannot decrypt (wrong SECRET_KEY?)") from e


def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict | None:
    """Returns the Telegram user dict if initData is authentic, else None."""
    if not init_data:
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return json.loads(pairs.get("user", "{}")) or None
    except (ValueError, json.JSONDecodeError):
        return None


def make_token(secret: str, user_id: int, ttl: int = 30 * 86400) -> str:
    exp = int(time.time()) + ttl
    body = f"{user_id}.{exp}"
    sig = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def read_token(secret: str, token: str) -> int | None:
    try:
        uid, exp, sig = token.split(".")
        body = f"{uid}.{exp}"
        good = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(good, sig) or int(exp) < time.time():
            return None
        return int(uid)
    except (ValueError, AttributeError):
        return None


def new_code() -> str:
    return f"{secrets.randbelow(10**6):06d}"
