from app.store.db import connect


class LedgerError(Exception):
    """业务规则拒绝；reason 为可区分的稳定原因码，detail 面向调用方说明。"""

    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


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
            "SELECT tenant, order_id, amount_cents, paid_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    outstanding = row["amount_cents"] - row["paid_cents"]
    return {**dict(row), "outstanding_cents": outstanding}


def add_payment(tenant: str, order_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if amount_cents <= 0 or row["paid_cents"] + amount_cents > row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise LedgerError("payment_exceeds_outstanding", "payment exceeds outstanding amount")
        cur = conn.execute(
            "INSERT INTO payments(tenant, order_id, amount_cents) VALUES(?,?,?)",
            (tenant, order_id, amount_cents),
        )
        payment_id = cur.lastrowid
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return {"payment_id": payment_id, **get(tenant, order_id)}


def reverse_payment(tenant: str, order_id: str, reversal_id: str, payment_id: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute(
            "SELECT amount_cents, paid_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None

        existing = conn.execute(
            "SELECT payment_id, amount_cents FROM reversals WHERE tenant=? AND order_id=? AND reversal_id=?",
            (tenant, order_id, reversal_id),
        ).fetchone()
        if existing is not None:
            # 同一冲正标识重放：不重复扣减，结果与首次一致；指向其他收款则为标识冲突。
            if existing["payment_id"] != payment_id:
                conn.execute("ROLLBACK")
                raise LedgerError("reversal_id_conflict", "reversal_id already used for another payment")
            conn.execute("ROLLBACK")
            return _reversal_view(tenant, order_id, reversal_id, payment_id, existing["amount_cents"])

        payment = conn.execute(
            "SELECT amount_cents, reversed FROM payments WHERE id=? AND tenant=? AND order_id=?",
            (payment_id, tenant, order_id),
        ).fetchone()
        if payment is None:
            conn.execute("ROLLBACK")
            raise LedgerError("reversal_payment_not_found", "payment not found")
        if payment["reversed"]:
            conn.execute("ROLLBACK")
            raise LedgerError("reversal_payment_already_reversed", "payment already reversed")

        conn.execute("UPDATE payments SET reversed=1 WHERE id=?", (payment_id,))
        conn.execute(
            "INSERT INTO reversals(tenant, order_id, reversal_id, payment_id, amount_cents) VALUES(?,?,?,?,?)",
            (tenant, order_id, reversal_id, payment_id, payment["amount_cents"]),
        )
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents - ?, status = CASE WHEN paid_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (payment["amount_cents"], payment["amount_cents"], tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return _reversal_view(tenant, order_id, reversal_id, payment_id, payment["amount_cents"])


def _reversal_view(tenant: str, order_id: str, reversal_id: str, payment_id: int, amount_cents: int) -> dict:
    return {
        "reversal_id": reversal_id,
        "payment_id": payment_id,
        "reversed_amount_cents": amount_cents,
        **get(tenant, order_id),
    }
