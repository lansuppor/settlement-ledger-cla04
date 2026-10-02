import json
import sqlite3

from app.rules.order_rules import ALLOWED_CURRENCIES
from app.store.db import connect


def create_batch(tenant: str, batch_id: str, rows: list[dict], payload_hash: str) -> bool:
    """原子落入批次头与全部待处理行；批次标识已存在时返回 False，不改变既有批次。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO import_batches(tenant, batch_id, status, total_rows, payload_hash) VALUES(?,?,'processing',?,?)",
                (tenant, batch_id, len(rows), payload_hash),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            return False
        for line_no, row in enumerate(rows, start=1):
            conn.execute(
                "INSERT INTO import_batch_rows(tenant, batch_id, line_no, payload) VALUES(?,?,?,?)",
                (tenant, batch_id, line_no, json.dumps(row, ensure_ascii=False)),
            )
        conn.execute("COMMIT")
        return True
    finally:
        conn.close()


def get_batch(tenant: str, batch_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, batch_id, status, total_rows, succeeded_rows, failed_rows, payload_hash FROM import_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return dict(row)


def list_processing() -> list[tuple[str, str]]:
    """服务重启后用于续跑的未完成批次清单。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT tenant, batch_id FROM import_batches WHERE status='processing' ORDER BY created_at"
        ).fetchall()
    finally:
        conn.close()
    return [(row["tenant"], row["batch_id"]) for row in rows]


def get_batch_view(tenant: str, batch_id: str) -> dict | None:
    """批次进度与结果视图：计数 + 每一失败行的行号与原因（跨租户一律按不存在处理）。"""
    conn = connect()
    try:
        head = conn.execute(
            "SELECT tenant, batch_id, status, total_rows, succeeded_rows, failed_rows FROM import_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if head is None:
            return None
        failed = conn.execute(
            "SELECT line_no, payload, error FROM import_batch_rows WHERE tenant=? AND batch_id=? AND state='failed' ORDER BY line_no",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()
    failures = []
    for row in failed:
        order_id = None
        try:
            value = json.loads(row["payload"]).get("order_id")
            order_id = value if isinstance(value, str) else None
        except (ValueError, AttributeError):
            pass
        failures.append({"line_no": row["line_no"], "order_id": order_id, "error": row["error"]})
    return {
        "tenant": head["tenant"],
        "batch_id": head["batch_id"],
        "status": head["status"],
        "total_rows": head["total_rows"],
        "succeeded_rows": head["succeeded_rows"],
        "failed_rows": head["failed_rows"],
        "failures": failures,
    }


def _apply_row(conn: sqlite3.Connection, tenant: str, payload: str) -> tuple[str, str | None]:
    """单行校验并受理；返回 (状态, 失败原因)。重复订单只拒绝该行，不修改既有订单。"""
    try:
        data = json.loads(payload)
    except ValueError:
        return "failed", "row payload unreadable"
    order_id = data.get("order_id")
    amount_cents = data.get("amount_cents")
    currency = data.get("currency")
    if not isinstance(order_id, str) or not order_id:
        return "failed", "order_id must be a non-empty string"
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
        return "failed", "amount_cents must be a positive integer"
    if not isinstance(currency, str) or currency not in ALLOWED_CURRENCIES:
        return "failed", "unsupported currency"
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    except sqlite3.IntegrityError:
        return "failed", "duplicate order"
    return "imported", None


def process_pending(tenant: str, batch_id: str, max_rows: int | None = None) -> None:
    """逐行处理 pending 行：每行结果与批次计数同事务提交；处理完毕将批次置为 completed。

    中断后重入安全：已落库的行状态不会回退，续跑只取剩余 pending 行，计数始终闭合。
    max_rows 仅供测试模拟中断（处理若干行后返回）。
    """
    done = 0
    while True:
        if max_rows is not None and done >= max_rows:
            return
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT line_no, payload FROM import_batch_rows WHERE tenant=? AND batch_id=? AND state='pending' ORDER BY line_no LIMIT 1",
                (tenant, batch_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "UPDATE import_batches SET status='completed' WHERE tenant=? AND batch_id=?",
                    (tenant, batch_id),
                )
                conn.execute("COMMIT")
                return
            state, error = _apply_row(conn, tenant, row["payload"])
            conn.execute(
                "UPDATE import_batch_rows SET state=?, error=? WHERE tenant=? AND batch_id=? AND line_no=?",
                (state, error, tenant, batch_id, row["line_no"]),
            )
            conn.execute(
                "UPDATE import_batches SET succeeded_rows = succeeded_rows + ?, failed_rows = failed_rows + ? WHERE tenant=? AND batch_id=?",
                (1 if state == "imported" else 0, 1 if state == "failed" else 0, tenant, batch_id),
            )
            conn.execute("COMMIT")
            done += 1
        finally:
            conn.close()
