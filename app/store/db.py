import sqlite3
from pathlib import Path

from app.config import db_path

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
MIGRATIONS = ("001_init.sql", "002_refunds.sql", "003_imports.sql", "004_ledger.sql", "005_tickets.sql", "006_debts.sql")

def connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发写事务在 IMMEDIATE 锁上等待后重试，使竞争方读到最新状态并按业务规则拒绝，
    # 而不是直接抛 SQLITE_BUSY。
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn

def migrate() -> None:
    conn = connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        applied = {row["name"] for row in conn.execute("SELECT name FROM schema_migrations")}
        for name in MIGRATIONS:
            if name in applied:
                continue
            sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
            conn.execute("BEGIN")
            try:
                # 002 含 ALTER TABLE ADD COLUMN，老库重复执行会报错；
                # 按语句逐条执行并容忍“列已存在/表已存在”，保证升级可重入。
                for stmt in (s.strip() for s in sql.split(";") if s.strip()):
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError as error:
                        message = str(error)
                        if "duplicate column name" not in message and "already exists" not in message:
                            raise
                conn.execute("INSERT OR IGNORE INTO schema_migrations(name) VALUES(?)", (name,))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()
