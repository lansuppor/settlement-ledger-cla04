import sqlite3
from app.store.db import connect

def _shape(row: sqlite3.Row) -> dict:
    # 未收金额 = 订单金额 − 已收金额 + 已退金额（退款部分重新回到待收状态）
    outstanding = row["amount_cents"] - row["paid_cents"] + row["refunded_cents"]
    return {
        "tenant": row["tenant"],
        "order_id": row["order_id"],
        "amount_cents": row["amount_cents"],
        "paid_cents": row["paid_cents"],
        "refunded_cents": row["refunded_cents"],
        "currency": row["currency"],
        "status": row["status"],
        "outstanding_cents": outstanding,
    }

def insert(tenant: str, order_id: str, amount_cents: int, currency: str) -> None:
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
            (tenant, order_id, amount_cents, currency),
        )
    finally:
        conn.close()

def get(tenant: str, order_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, order_id, amount_cents, paid_cents, refunded_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return _shape(row)

def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        # 未收 = 订单金额 − 已收 + 已退；无退款时与“订单金额 − 已收”完全一致。
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"] + row["refunded_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("payment exceeds outstanding amount")
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, status = CASE WHEN paid_cents + ? >= amount_cents + refunded_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)

def _refund_result(paid_cents: int, refunded_cents: int, outstanding_cents: int) -> dict:
    return {
        "paid_cents": paid_cents,
        "refunded_cents": refunded_cents,
        "outstanding_cents": outstanding_cents,
    }

def add_refund(tenant: str, order_id: str, refund_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 幂等：退款标识在同一租户内唯一，重复提交返回首次登记时的同样结果。
        prev = conn.execute(
            "SELECT order_id, amount_cents, paid_after, refunded_after, outstanding_after FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            if prev["order_id"] != order_id or prev["amount_cents"] != amount_cents:
                raise ValueError("refund_id already used")
            return _refund_result(prev["paid_after"], prev["refunded_after"], prev["outstanding_after"])
        # 2) 订单存在性（跨租户与读取一致按不存在处理）与当前可退净额（已收 − 已退）。
        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, currency FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        refundable = row["paid_cents"] - row["refunded_cents"]
        if amount_cents <= 0 or amount_cents > refundable:
            conn.execute("ROLLBACK")
            raise ValueError("refund exceeds refundable amount")
        paid_after = row["paid_cents"]
        refunded_after = row["refunded_cents"] + amount_cents
        outstanding_after = row["amount_cents"] - paid_after + refunded_after
        # 3) 退款回写订单：退回款项重新计入未收，订单由结清回到待收。
        conn.execute(
            "UPDATE orders SET refunded_cents = refunded_cents + ?, status = CASE WHEN amount_cents - paid_cents + refunded_cents + ? > 0 THEN 'accepted' ELSE status END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        # 4) 落退款流水：并发下不同订单复用同一（租户, 退款标识）在此撞主键，整笔回滚。
        try:
            conn.execute(
                "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, currency, paid_after, refunded_after, outstanding_after) VALUES(?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    refund_id,
                    order_id,
                    amount_cents,
                    row["currency"],
                    paid_after,
                    refunded_after,
                    outstanding_after,
                ),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise ValueError("refund_id already used")
        conn.execute("COMMIT")
    finally:
        conn.close()
    return _refund_result(paid_after, refunded_after, outstanding_after)
