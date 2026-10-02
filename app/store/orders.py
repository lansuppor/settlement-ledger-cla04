import sqlite3
from app.store.db import connect

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
    outstanding = row["amount_cents"] - row["paid_cents"] + row["refunded_cents"]
    return {**dict(row), "outstanding_cents": outstanding}

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
        outstanding = row["amount_cents"] - row["paid_cents"] + row["refunded_cents"]
        if amount_cents <= 0 or amount_cents > outstanding:
            conn.execute("ROLLBACK")
            raise ValueError("payment exceeds outstanding amount")
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, status = CASE WHEN paid_cents + ? - refunded_cents >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, order_id)

def add_refund(tenant: str, order_id: str, refund_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT order_id, paid_cents, refunded_cents, outstanding_cents FROM refunds WHERE tenant=? AND refund_id=?",
            (tenant, refund_id),
        ).fetchone()
        if existing is not None:
            if existing["order_id"] != order_id:
                conn.execute("ROLLBACK")
                raise ValueError("refund id already used")
            conn.execute("COMMIT")
            return {
                "order_id": order_id,
                "paid_cents": existing["paid_cents"],
                "refunded_cents": existing["refunded_cents"],
                "outstanding_cents": existing["outstanding_cents"],
            }
        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        refundable = row["paid_cents"] - row["refunded_cents"]
        if amount_cents <= 0 or amount_cents > refundable:
            conn.execute("ROLLBACK")
            raise ValueError("refund exceeds refundable amount")
        refunded = row["refunded_cents"] + amount_cents
        outstanding = row["amount_cents"] - row["paid_cents"] + refunded
        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, paid_cents, refunded_cents, outstanding_cents) VALUES(?,?,?,?,?,?,?)",
            (tenant, refund_id, order_id, amount_cents, row["paid_cents"], refunded, outstanding),
        )
        conn.execute(
            "UPDATE orders SET refunded_cents = refunded_cents + ?, status = CASE WHEN paid_cents - refunded_cents - ? >= amount_cents THEN 'settled' ELSE 'accepted' END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute("COMMIT")
        return {
            "order_id": order_id,
            "paid_cents": row["paid_cents"],
            "refunded_cents": refunded,
            "outstanding_cents": outstanding,
        }
    finally:
        conn.close()
