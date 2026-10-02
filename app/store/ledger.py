import sqlite3

from app.store.db import connect


def record_entry(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    entry_id: str,
    entry_type: str,
    amount_cents: int,
    outstanding_after: int,
) -> None:
    # 在调用方的事务内落一条账务流水；流水与状态/金额变更同生共死，失败整体回滚。
    conn.execute(
        "INSERT INTO ledger_entries(tenant, order_id, entry_id, entry_type, amount_cents, outstanding_after, created_at) "
        "VALUES(?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%f','now'))",
        (tenant, order_id, entry_id, entry_type, amount_cents, outstanding_after),
    )

def next_payment_id(conn: sqlite3.Connection) -> str:
    # 收款没有调用方提供的标识，用全局单调序号生成业务标识（pm-<序号>）。
    # 调用方持有 BEGIN IMMEDIATE 写锁，序号与即将插入的流水行一一对应。
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM ledger_entries").fetchone()["n"]
    return f"pm-{seq}"

def history(tenant: str, order_id: str) -> dict | None:
    # 按（时间, 业务标识）升序返回某订单的全部账务条目；
    # 重放该序列可得到与当前一致的最终未收金额与状态。
    conn = connect()
    try:
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            return None
        rows = conn.execute(
            "SELECT entry_id, entry_type, amount_cents, outstanding_after, created_at "
            "FROM ledger_entries WHERE tenant=? AND order_id=? ORDER BY created_at ASC, entry_id ASC",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return {
        "tenant": tenant,
        "order_id": order_id,
        "entries": [
            {
                "entry_id": row["entry_id"],
                "type": row["entry_type"],
                "amount_cents": row["amount_cents"],
                "outstanding_after": row["outstanding_after"],
                "created_at": row["created_at"],
            }
            for row in rows
        ],
    }
