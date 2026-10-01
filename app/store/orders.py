import sqlite3
import uuid

from app.store.db import connect


class PaymentExceedsOutstanding(ValueError):
    """收款金额超过订单未收金额。"""

class PaymentNotFound(Exception):
    """订单内不存在该收款标识（跨租户访问同样按此处理）。"""

class PaymentAlreadyReversed(Exception):
    """该收款此前已被冲正。"""

class ReversalConflict(Exception):
    """冲正标识已被用于冲正另一笔收款。"""

class ReversalBlockedBySettlement(Exception):
    """订单存在未撤销的结算单，核销闭合期间不允许冲正收款。"""

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
    return _order_view(row)

def _order_view(row: sqlite3.Row) -> dict:
    return {**dict(row), "outstanding_cents": row["amount_cents"] - row["paid_cents"]}

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
            raise PaymentExceedsOutstanding("payment exceeds outstanding amount")
        payment_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO payments(tenant, order_id, payment_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, order_id, payment_id, amount_cents),
        )
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    view = get(tenant, order_id)
    view["payment_id"] = payment_id
    return view

def reverse_payment(tenant: str, order_id: str, reversal_id: str, payment_id: str) -> dict | None:
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

        blocked = conn.execute(
            "SELECT 1 FROM settlements WHERE tenant=? AND order_id=? AND status='active'",
            (tenant, order_id),
        ).fetchone()
        if blocked is not None:
            # 核销闭合期间（存在未撤销结算单）不允许冲正收款，否则结算金额快照与未冲正收款合计会脱节；
            # 需先撤销结算再冲正。状态保持不变
            conn.execute("ROLLBACK")
            raise ReversalBlockedBySettlement("payment reversal blocked by active settlement")

        existing = conn.execute(
            "SELECT reversal_id, payment_id, amount_cents FROM reversals WHERE tenant=? AND order_id=? AND reversal_id=?",
            (tenant, order_id, reversal_id),
        ).fetchone()
        if existing is not None:
            # 幂等重放：同一冲正标识只能指向同一笔收款，重复请求返回首次结果
            if existing["payment_id"] != payment_id:
                conn.execute("ROLLBACK")
                raise ReversalConflict("reversal id already used for another payment")
            conn.execute("COMMIT")
            return _reversal_view(tenant, order_id, existing)

        payment = conn.execute(
            "SELECT amount_cents FROM payments WHERE tenant=? AND order_id=? AND payment_id=?",
            (tenant, order_id, payment_id),
        ).fetchone()
        if payment is None:
            conn.execute("ROLLBACK")
            raise PaymentNotFound("payment not found")

        reversed_row = conn.execute(
            "SELECT 1 FROM reversals WHERE tenant=? AND order_id=? AND payment_id=?",
            (tenant, order_id, payment_id),
        ).fetchone()
        if reversed_row is not None:
            conn.execute("ROLLBACK")
            raise PaymentAlreadyReversed("payment already reversed")

        amount_cents = payment["amount_cents"]
        try:
            conn.execute(
                "INSERT INTO reversals(tenant, order_id, reversal_id, payment_id, amount_cents) VALUES(?,?,?,?,?)",
                (tenant, order_id, reversal_id, payment_id, amount_cents),
            )
            conn.execute(
                "UPDATE orders SET paid_cents = paid_cents - ?, status = 'accepted' WHERE tenant=? AND order_id=?",
                (amount_cents, tenant, order_id),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            # 并发下唯一索引兜底（收款已被冲正 / 冲正标识已占用），整笔回滚
            conn.execute("ROLLBACK")
            clash = connect()
            try:
                same_id = clash.execute(
                    "SELECT payment_id FROM reversals WHERE tenant=? AND order_id=? AND reversal_id=?",
                    (tenant, order_id, reversal_id),
                ).fetchone()
            finally:
                clash.close()
            if same_id is not None and same_id["payment_id"] != payment_id:
                raise ReversalConflict("reversal id already used for another payment")
            raise PaymentAlreadyReversed("payment already reversed")
    finally:
        conn.close()

    conn2 = connect()
    try:
        saved = conn2.execute(
            "SELECT reversal_id, payment_id, amount_cents FROM reversals WHERE tenant=? AND order_id=? AND reversal_id=?",
            (tenant, order_id, reversal_id),
        ).fetchone()
        return _reversal_view(tenant, order_id, saved)
    finally:
        conn2.close()

def _reversal_view(tenant: str, order_id: str, reversal_row: sqlite3.Row) -> dict:
    order = get(tenant, order_id)
    return {
        **order,
        "reversal_id": reversal_row["reversal_id"],
        "payment_id": reversal_row["payment_id"],
        "reversed_amount_cents": reversal_row["amount_cents"],
    }
