"""Minimal stand-in for httpx, used ONLY when the real package is not installed (offline sandbox).
With `pip install -r requirements.txt` the real httpx is used instead."""
import sys
import types


class HTTPError(Exception):
    pass


class TimeoutException(HTTPError):
    pass


class Response:
    def __init__(self, status_code=200, json_data=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.text = text or (str(json_data) if json_data is not None else "")

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class AsyncClient:
    pass


def install():
    try:
        import httpx  # noqa: F401
        return False
    except ImportError:
        m = types.ModuleType("httpx")
        m.HTTPError, m.TimeoutException, m.Response, m.AsyncClient = HTTPError, TimeoutException, Response, AsyncClient
        sys.modules["httpx"] = m
        return True
