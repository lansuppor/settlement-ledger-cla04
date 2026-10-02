import os
from pathlib import Path


def db_path() -> Path:
    return Path(os.environ.get("APP_DB", "var/app.sqlite"))

def tenant_header() -> str:
    return os.environ.get("APP_TENANT_HEADER", "X-Tenant")

def cursor_secret() -> str:
    return os.environ.get("APP_CURSOR_SECRET", "settlement-ledger-dev-secret")

def cursor_ttl_seconds() -> int:
    return int(os.environ.get("APP_CURSOR_TTL", "3600"))
