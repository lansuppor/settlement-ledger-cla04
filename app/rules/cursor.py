import base64
import hashlib
import hmac
import json

from app.config import cursor_secret

# 游标为“负载.签名”两段式文本：负载是 base64url(JSON)，签名是 HMAC-SHA256。
# 负载内携带租户、过滤条件、锚点与签发时间，跨租户/换条件/过期/篡改都在解码后可区分。

class CursorError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason

def _sign(body: str) -> str:
    return hmac.new(cursor_secret().encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()

def encode(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii")
    return f"{body}.{_sign(body)}"

def decode(token: str) -> dict:
    try:
        body, signature = token.rsplit(".", 1)
    except ValueError:
        raise CursorError("cursor_malformed") from None
    if not hmac.compare_digest(_sign(body), signature):
        raise CursorError("cursor_malformed")
    try:
        payload = json.loads(base64.urlsafe_b64decode(body.encode("ascii")))
    except (ValueError, TypeError):
        raise CursorError("cursor_malformed") from None
    if not isinstance(payload, dict) or not {"tenant", "filters", "last", "iat"} <= payload.keys():
        raise CursorError("cursor_malformed")
    return payload
