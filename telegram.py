import httpx


class TGError(Exception):
    def __init__(self, method: str, description: str, code: int = 0):
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.code = code


class TG:
    def __init__(self, token: str, client: httpx.AsyncClient):
        self.token = token
        self.client = client
        self.base = f"https://api.telegram.org/bot{token}/"

    async def call(self, method: str, timeout: float = 30, **params):
        try:
            r = await self.client.post(self.base + method, json=params, timeout=timeout)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TGError(method, f"network error: {type(e).__name__}") from None
        if not data.get("ok"):
            raise TGError(method, data.get("description", "unknown error"), data.get("error_code", 0))
        return data["result"]

    async def send_message(self, chat_id: int, text: str, **kw):
        # Telegram caps messages at 4096 chars
        chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [""]
        last = None
        for i, c in enumerate(chunks):
            extra = kw if i == len(chunks) - 1 else {}
            last = await self.call("sendMessage", chat_id=chat_id, text=c, **extra)
        return last

    async def send_document_bytes(self, chat_id: int, filename: str, data: bytes, caption: str = ""):
        files = {"document": (filename, data)}
        r = await self.client.post(
            self.base + "sendDocument", data={"chat_id": str(chat_id), "caption": caption},
            files=files, timeout=60,
        )
        j = r.json()
        if not j.get("ok"):
            raise TGError("sendDocument", j.get("description", "unknown"), j.get("error_code", 0))
        return j["result"]

    async def download(self, file_id: str) -> bytes:
        info = await self.call("getFile", file_id=file_id)
        r = await self.client.get(
            f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}", timeout=60
        )
        r.raise_for_status()
        return r.content
