import base64
import hashlib
import hmac
import json
import os
import time

# 游标为“仅同租户、同过滤条件可续读”的凭据：HMAC 签名防伪造，内嵌过期时间。
TTL_SECONDS = int(os.environ.get("APP_CURSOR_TTL_SECONDS", "1800"))


class CursorError(Exception):
    """reason 取值：invalid（无法识别/被篡改）、expired（已过期）。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _secret() -> bytes:
    return os.environ.get("APP_CURSOR_SECRET", "settlement-ledger-dev-secret").encode()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(body: str) -> str:
    return _b64encode(hmac.new(_secret(), body.encode(), hashlib.sha256).digest())


def encode(payload: dict) -> str:
    body = _b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
    return f"{body}.{_sign(body)}"


def decode(token: str) -> dict:
    try:
        body, sig = token.split(".")
        if not hmac.compare_digest(sig, _sign(body)):
            raise CursorError("invalid")
        payload = json.loads(_b64decode(body))
    except CursorError:
        raise
    except Exception as error:
        raise CursorError("invalid") from error
    if not isinstance(payload, dict) or payload.get("exp", 0) < time.time():
        raise CursorError("expired")
    return payload


def issue(tenant: str, filters: dict, last_order_id: str) -> str:
    return encode(
        {
            "v": 1,
            "tenant": tenant,
            "filters": filters,
            "last": last_order_id,
            "exp": int(time.time()) + TTL_SECONDS,
        }
    )
