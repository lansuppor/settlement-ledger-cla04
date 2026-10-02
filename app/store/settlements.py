import sqlite3

from app.store import ledger
from app.store.db import connect


class Conflict(ValueError):
    """业务冲突（409）。"""


def _settlement_view(row: sqlite3.Row) -> dict:
    # 结算提交的幂等响应始终呈现“首次生效”视图：同一标识重复提交即便发生在冲正之后，
    # 也返回与首次一致的结果（历史记录的当前状态可在账务历史中查到）。
    return {
        "tenant": row["tenant"],
        "settlement_id": row["settlement_id"],
        "order_id": row["order_id"],
        "seq": row["seq"],
        "amount_cents": row["amount_cents"],
        "paid_cents": row["paid_at_settlement"],
        "refunded_cents": row["refunded_at_settlement"],
        "outstanding_cents": row["outstanding_after"],
        "currency": row["currency"],
        "status": "effective",
        "created_at": row["created_at"],
    }


def _reversal_view(row: sqlite3.Row) -> dict:
    return {
        "tenant": row["tenant"],
        "reversal_id": row["reversal_id"],
        "settlement_id": row["settlement_id"],
        "order_id": row["order_id"],
        "reason": row["reason"],
        "amount_cents": row["amount_cents"],
        "outstanding_cents": row["outstanding_after"],
        "status": "reversed",
        "created_at": row["created_at"],
    }


def settle(tenant: str, order_id: str, settlement_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 幂等：结算标识以（租户, 标识）唯一。同标识对同订单、同金额重复提交直接返回
        #    首次结果；同标识用于不同订单或不同金额拒绝。
        prev = conn.execute(
            "SELECT * FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            if prev["order_id"] != order_id or prev["amount_cents"] != amount_cents:
                raise Conflict("settlement_id already used")
            return _settlement_view(prev)
        # 2) 订单存在性与跨租户：与读取一致按不存在处理（404）。
        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, currency, status FROM orders"
            " WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None
        outstanding = order["amount_cents"] - order["paid_cents"] + order["refunded_cents"]
        # 3) 存在未收金额一律拒绝：不产生结算记录、流水或状态变化。
        #    结算金额为调用方申报金额，仅要求为正（由入参校验），原样入账并参与幂等比对。
        if outstanding > 0:
            conn.execute("ROLLBACK")
            raise Conflict("order has outstanding amount")
        # 4) 同一订单任一时刻至多一条生效结算；冲正后旧记录 voided，方可重新结算。
        active = conn.execute(
            "SELECT 1 FROM settlements WHERE tenant=? AND order_id=? AND status='effective'",
            (tenant, order_id),
        ).fetchone()
        if active is not None:
            conn.execute("ROLLBACK")
            raise Conflict("order already settled")
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next FROM settlements WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        # 5) 结算记录、订单状态、账务历史同事务落库；并发的第二笔结算在此撞唯一索引整笔回滚。
        try:
            conn.execute(
                "INSERT INTO settlements(tenant, settlement_id, order_id, seq, amount_cents,"
                " paid_at_settlement, refunded_at_settlement, outstanding_after, currency, status)"
                " VALUES(?,?,?,?,?,?,?,?,?,'effective')",
                (
                    tenant,
                    settlement_id,
                    order_id,
                    seq_row["next"],
                    amount_cents,
                    order["paid_cents"],
                    order["refunded_cents"],
                    0,
                    order["currency"],
                ),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise Conflict("order already settled")
        conn.execute(
            "UPDATE orders SET status='settled' WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        )
        ledger.append(
            conn, tenant, order_id, settlement_id, ledger.ENTRY_SETTLEMENT, amount_cents, 0
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get_settlement(tenant, settlement_id)


def get_settlement(tenant: str, settlement_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
    finally:
        conn.close()
    return _settlement_view(row) if row is not None else None


def reverse(
    tenant: str, order_id: str, settlement_id: str, reversal_id: str, reason: str
) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 冲正标识与结算标识是两个独立请求身份：各自按（租户, 标识）幂等，互不复用或去重。
        prev = conn.execute(
            "SELECT * FROM reversals WHERE tenant=? AND reversal_id=?",
            (tenant, reversal_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            if prev["settlement_id"] != settlement_id or prev["order_id"] != order_id:
                raise Conflict("reversal_id already used")
            return _reversal_view(prev)
        # 2) 结算记录须存在且属于该订单；不存在、跨租户或不属于路径订单统一按 404，不泄漏存在性。
        sett = conn.execute(
            "SELECT * FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if sett is None or sett["order_id"] != order_id:
            conn.execute("ROLLBACK")
            return None
        # 3) 仅生效结算可冲正；对同一结算重复冲正（含并发）拒绝。
        if sett["status"] != "effective":
            conn.execute("ROLLBACK")
            raise Conflict("settlement already reversed")
        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 结算存在则订单必存在；防御性处理，按整体失败回滚。
            conn.execute("ROLLBACK")
            return None
        outstanding = order["amount_cents"] - order["paid_cents"] + order["refunded_cents"]
        # 4) 作废旧结算、订单退回未结算状态、留冲正原因与流水，同事务整体生效。
        #    reversals 上 (tenant, settlement_id) 唯一兜底并发重复冲正。
        try:
            conn.execute(
                "INSERT INTO reversals(tenant, reversal_id, settlement_id, order_id, reason,"
                " amount_cents, outstanding_after) VALUES(?,?,?,?,?,?,?)",
                (
                    tenant,
                    reversal_id,
                    settlement_id,
                    order_id,
                    reason,
                    sett["amount_cents"],
                    outstanding,
                ),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise Conflict("settlement already reversed")
        conn.execute(
            "UPDATE settlements SET status='voided' WHERE tenant=? AND settlement_id=? AND status='effective'",
            (tenant, settlement_id),
        )
        conn.execute(
            "UPDATE orders SET status='accepted' WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        )
        ledger.append(
            conn,
            tenant,
            order_id,
            reversal_id,
            ledger.ENTRY_REVERSAL,
            sett["amount_cents"],
            outstanding,
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM reversals WHERE tenant=? AND reversal_id=?",
            (tenant, reversal_id),
        ).fetchone()
    finally:
        conn.close()
    return _reversal_view(row) if row is not None else None
