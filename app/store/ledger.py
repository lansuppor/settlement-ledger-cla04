import sqlite3

from app.store.db import connect

# 订单账务历史流水。收款/退款/结算/冲正/核销共用一张流水表：每条含业务标识、类型、金额与
# 操作后未收金额，按 (created_at, seq_no) 升序即得到可重放的账务序列。
#
# 收款没有调用方提供的标识，流水以“pay-<订单内收款序号>”作为业务标识；其它四类使用
# 调用方提供的业务标识。同一 (订单, 业务标识) 不重复（收款由订单内计数天然唯一）。
# 核销条目的 outstanding_after 记录“该订单核销后仍未核销的欠款合计”（核销不改变订单未收金额）。

ENTRY_PAYMENT = "payment"
ENTRY_REFUND = "refund"
ENTRY_SETTLEMENT = "settlement"
ENTRY_REVERSAL = "reversal"
ENTRY_WRITE_OFF = "write_off"


def next_seq(conn: sqlite3.Connection, tenant: str, order_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq_no), 0) + 1 AS next FROM ledger_entries WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    return row["next"]


def append(
    conn: sqlite3.Connection,
    tenant: str,
    order_id: str,
    biz_ref: str,
    entry_type: str,
    amount_cents: int,
    outstanding_after: int,
) -> None:
    seq_no = next_seq(conn, tenant, order_id)
    conn.execute(
        "INSERT INTO ledger_entries(tenant, order_id, seq_no, biz_ref, entry_type, amount_cents, outstanding_after)"
        " VALUES(?,?,?,?,?,?,?)",
        (tenant, order_id, seq_no, biz_ref, entry_type, amount_cents, outstanding_after),
    )


def list_entries(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?", (tenant, order_id)
        ).fetchone()
        if exists is None:
            return None
        rows = conn.execute(
            "SELECT biz_ref, entry_type, amount_cents, outstanding_after, created_at, seq_no"
            " FROM ledger_entries WHERE tenant=? AND order_id=? ORDER BY created_at ASC, seq_no ASC",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "biz_ref": row["biz_ref"],
            "type": row["entry_type"],
            "amount_cents": row["amount_cents"],
            "outstanding_cents": row["outstanding_after"],
            "at": row["created_at"],
        }
        for row in rows
    ]
