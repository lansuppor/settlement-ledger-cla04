from app.store.db import connect


def _header_view(row) -> dict:
    return {
        "tenant": row["tenant"],
        "reconciliation_id": row["reconciliation_id"],
        "order_count": row["order_count"],
        "total_receivable_cents": row["total_receivable_cents"],
        "total_paid_cents": row["total_paid_cents"],
        "total_refunded_cents": row["total_refunded_cents"],
        "total_outstanding_cents": row["total_outstanding_cents"],
        "created_at": row["created_at"],
    }


def _load_summary(conn, tenant: str, reconciliation_id: str) -> dict | None:
    header = conn.execute(
        "SELECT * FROM reconciliations WHERE tenant=? AND reconciliation_id=?",
        (tenant, reconciliation_id),
    ).fetchone()
    if header is None:
        return None
    rows = conn.execute(
        "SELECT order_id, amount_cents, paid_cents, refunded_cents, outstanding_cents, currency, status,"
        " has_active_settlement FROM reconciliation_orders WHERE tenant=? AND reconciliation_id=?"
        " ORDER BY order_id ASC",
        (tenant, reconciliation_id),
    ).fetchall()
    summary = _header_view(header)
    summary["orders"] = [
        {
            "order_id": row["order_id"],
            "amount_cents": row["amount_cents"],
            "paid_cents": row["paid_cents"],
            "refunded_cents": row["refunded_cents"],
            "outstanding_cents": row["outstanding_cents"],
            "currency": row["currency"],
            "status": row["status"],
            "has_active_settlement": bool(row["has_active_settlement"]),
        }
        for row in rows
    ]
    return summary


def start(tenant: str, reconciliation_id: str) -> dict:
    # 幂等发起：同一（租户, 对账标识）只计算一次，重复发起直接返回首次快照；
    # 汇总在单个 IMMEDIATE 事务内读取并落库，相当于取发起时刻的一致性快照，
    # 对账期间新发生的账务不改变已生成结果，需以新标识重新发起才反映。
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = _load_summary(conn, tenant, reconciliation_id)
        if existing is not None:
            conn.execute("ROLLBACK")
            return existing
        orders = conn.execute(
            "SELECT order_id, amount_cents, paid_cents, refunded_cents, currency, status FROM orders"
            " WHERE tenant=? ORDER BY order_id ASC",
            (tenant,),
        ).fetchall()
        active = {
            row["order_id"]
            for row in conn.execute(
                "SELECT order_id FROM settlements WHERE tenant=? AND status='effective'",
                (tenant,),
            )
        }
        total_receivable = total_paid = total_refunded = total_outstanding = 0
        snapshot_rows = []
        for order in orders:
            # 未收沿用订单口径：订单金额 − 已收 + 已退（含退款回冲部分）；
            # 应收 = 已收 + 未收 = 订单金额 + 已退（退款回冲部分仍需收讫）。
            outstanding = order["amount_cents"] - order["paid_cents"] + order["refunded_cents"]
            total_receivable += order["amount_cents"] + order["refunded_cents"]
            total_paid += order["paid_cents"]
            total_refunded += order["refunded_cents"]
            total_outstanding += outstanding
            snapshot_rows.append(
                (
                    tenant,
                    reconciliation_id,
                    order["order_id"],
                    order["amount_cents"],
                    order["paid_cents"],
                    order["refunded_cents"],
                    outstanding,
                    order["currency"],
                    order["status"],
                    1 if order["order_id"] in active else 0,
                )
            )
        conn.execute(
            "INSERT INTO reconciliations(tenant, reconciliation_id, order_count, total_receivable_cents,"
            " total_paid_cents, total_refunded_cents, total_outstanding_cents) VALUES(?,?,?,?,?,?,?)",
            (
                tenant,
                reconciliation_id,
                len(orders),
                total_receivable,
                total_paid,
                total_refunded,
                total_outstanding,
            ),
        )
        conn.executemany(
            "INSERT INTO reconciliation_orders(tenant, reconciliation_id, order_id, amount_cents,"
            " paid_cents, refunded_cents, outstanding_cents, currency, status, has_active_settlement)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            snapshot_rows,
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, reconciliation_id)  # type: ignore[return-value]


def get(tenant: str, reconciliation_id: str) -> dict | None:
    conn = connect()
    try:
        return _load_summary(conn, tenant, reconciliation_id)
    finally:
        conn.close()
