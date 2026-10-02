from app.store.db import connect


def _shape(batch, rows) -> dict:
    return {
        "tenant": batch["tenant"],
        "reconciliation_id": batch["reconciliation_id"],
        "order_count": batch["order_count"],
        "amount_cents": batch["amount_cents"],
        "paid_cents": batch["paid_cents"],
        "refunded_cents": batch["refunded_cents"],
        "outstanding_cents": batch["outstanding_cents"],
        "created_at": batch["created_at"],
        "orders": [
            {
                "order_id": row["order_id"],
                "amount_cents": row["amount_cents"],
                "paid_cents": row["paid_cents"],
                "refunded_cents": row["refunded_cents"],
                "outstanding_cents": row["outstanding_cents"],
                "has_active_settlement": bool(row["has_active_settlement"]),
            }
            for row in rows
        ],
    }

def get(tenant: str, reconciliation_id: str) -> dict | None:
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT * FROM reconciliations WHERE tenant=? AND reconciliation_id=?",
            (tenant, reconciliation_id),
        ).fetchone()
        if batch is None:
            return None
        rows = conn.execute(
            "SELECT * FROM reconciliation_orders WHERE tenant=? AND reconciliation_id=? ORDER BY order_id ASC",
            (tenant, reconciliation_id),
        ).fetchall()
    finally:
        conn.close()
    return _shape(batch, rows)

def reconcile(tenant: str, reconciliation_id: str) -> dict:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 幂等：同一对账标识重复发起返回已落库的结果，不重复计算；
        # 对账期间新发生的账务不改变已生成结果，重新发起（新标识）才反映。
        existing = conn.execute(
            "SELECT 1 FROM reconciliations WHERE tenant=? AND reconciliation_id=?",
            (tenant, reconciliation_id),
        ).fetchone()
        if existing is None:
            orders = conn.execute(
                "SELECT order_id, amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? ORDER BY order_id ASC",
                (tenant,),
            ).fetchall()
            settled = {
                row["order_id"]
                for row in conn.execute(
                    "SELECT order_id FROM settlements WHERE tenant=? AND status='active'",
                    (tenant,),
                ).fetchall()
            }
            totals = {"amount": 0, "paid": 0, "refunded": 0, "outstanding": 0}
            for order in orders:
                outstanding = order["amount_cents"] - order["paid_cents"] + order["refunded_cents"]
                totals["amount"] += order["amount_cents"]
                totals["paid"] += order["paid_cents"]
                totals["refunded"] += order["refunded_cents"]
                totals["outstanding"] += outstanding
                conn.execute(
                    "INSERT INTO reconciliation_orders(tenant, reconciliation_id, order_id, amount_cents, paid_cents, "
                    "refunded_cents, outstanding_cents, has_active_settlement) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        tenant, reconciliation_id, order["order_id"], order["amount_cents"], order["paid_cents"],
                        order["refunded_cents"], outstanding, 1 if order["order_id"] in settled else 0,
                    ),
                )
            # 守恒：应收合计 = 已收合计 − 已退合计 + 未收合计（未收沿用现有定义，含退款回冲部分）。
            conn.execute(
                "INSERT INTO reconciliations(tenant, reconciliation_id, order_count, amount_cents, paid_cents, "
                "refunded_cents, outstanding_cents) VALUES(?,?,?,?,?,?,?)",
                (
                    tenant, reconciliation_id, len(orders),
                    totals["amount"], totals["paid"], totals["refunded"], totals["outstanding"],
                ),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, reconciliation_id)
