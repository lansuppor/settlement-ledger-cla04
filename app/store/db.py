import sqlite3
from pathlib import Path
from app.config import db_path

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def migrate() -> None:
    conn = connect()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY)")
        applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
        # 老库在引入迁移记录前已应用 001_init.sql，按 orders 表存在补齐记录
        if "001_init.sql" not in applied:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='orders'"
            ).fetchone()
            if row is not None:
                conn.execute("INSERT INTO schema_migrations(name) VALUES('001_init.sql')")
                applied.add("001_init.sql")
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in applied:
                continue
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO schema_migrations(name) VALUES(?)", (path.name,))
    finally:
        conn.close()
