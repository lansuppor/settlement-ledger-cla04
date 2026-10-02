from app.store import ledger
from app.store.db import connect


def _settlement_result(tenant: str, order_id: str, settlement_id: str, amount_cents: int,
                       paid_cents: int, refunded_cents: int) -> dict:
    # 结算不改变账面金额：未收在结算前已为 0，结算后仍为 0。
    return {
        "tenant": tenant,
        "order_id": order_id,
        "settlement_id": settlement_id,
        "amount_cents": amount_cents,
        "paid_cents": paid_cents,
        "refunded_cents": refunded_cents,
        "outstanding_cents": 0,
        "status": "settled",
    }

def settle(tenant: str, order_id: str, settlement_id: str, amount_cents: int) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 幂等：结算标识在同一租户内唯一。同订单同金额重复提交返回与首次一致的结果；
        #    同标识用于不同订单或不同金额按冲突拒绝。
        prev = conn.execute(
            "SELECT order_id, amount_cents, paid_cents, refunded_cents FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            if prev["order_id"] != order_id or prev["amount_cents"] != amount_cents:
                raise ValueError("settlement_id already used")
            return _settlement_result(
                tenant, order_id, settlement_id,
                prev["amount_cents"], prev["paid_cents"], prev["refunded_cents"],
            )
        # 2) 订单存在性（跨租户按不存在处理）与未收金额：未收大于 0 时整笔拒绝，不留任何变化。
        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        outstanding = row["amount_cents"] - row["paid_cents"] + row["refunded_cents"]
        if outstanding > 0:
            conn.execute("ROLLBACK")
            raise ValueError("order has outstanding amount")
        if amount_cents != row["amount_cents"]:
            conn.execute("ROLLBACK")
            raise ValueError("settlement amount does not match order amount")
        # 3) 同一订单任一时刻至多一条生效结算；历史（已冲正）记录不阻拦重新结算。
        active = conn.execute(
            "SELECT 1 FROM settlements WHERE tenant=? AND order_id=? AND status='active'",
            (tenant, order_id),
        ).fetchone()
        if active is not None:
            conn.execute("ROLLBACK")
            raise ValueError("order already has an active settlement")
        # 4) 结算记录、订单状态、账务流水同一事务落库。
        conn.execute(
            "INSERT INTO settlements(tenant, settlement_id, order_id, amount_cents, paid_cents, refunded_cents, status) "
            "VALUES(?,?,?,?,?,?,'active')",
            (tenant, settlement_id, order_id, amount_cents, row["paid_cents"], row["refunded_cents"]),
        )
        conn.execute(
            "UPDATE orders SET status='settled' WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        )
        ledger.record_entry(conn, tenant, order_id, settlement_id, "settlement", amount_cents, 0)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return _settlement_result(tenant, order_id, settlement_id, amount_cents, row["paid_cents"], row["refunded_cents"])

def _reversal_result(tenant: str, order_id: str, reversal_id: str, settlement_id: str,
                     reason: str, amount_cents: int, outstanding_after: int) -> dict:
    return {
        "tenant": tenant,
        "order_id": order_id,
        "reversal_id": reversal_id,
        "settlement_id": settlement_id,
        "reason": reason,
        "amount_cents": amount_cents,
        "outstanding_cents": outstanding_after,
        "status": "accepted",
    }

def reverse(tenant: str, order_id: str, reversal_id: str, reason: str) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 幂等：冲正标识在同一租户内唯一（与结算标识互不影响）。重复提交返回与首次一致的结果；
        #    同标识用于不同订单按冲突拒绝。
        prev = conn.execute(
            "SELECT order_id, settlement_id, reason, amount_cents, outstanding_after FROM reversals WHERE tenant=? AND reversal_id=?",
            (tenant, reversal_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            if prev["order_id"] != order_id:
                raise ValueError("reversal_id already used")
            return _reversal_result(
                tenant, order_id, reversal_id,
                prev["settlement_id"], prev["reason"], prev["amount_cents"], prev["outstanding_after"],
            )
        # 2) 订单存在性（跨租户按不存在处理）。
        row = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        # 3) 只能冲正当前生效的结算；无生效结算（含已冲正）时整笔拒绝。
        active = conn.execute(
            "SELECT settlement_id, amount_cents FROM settlements WHERE tenant=? AND order_id=? AND status='active'",
            (tenant, order_id),
        ).fetchone()
        if active is None:
            conn.execute("ROLLBACK")
            raise ValueError("no active settlement to reverse")
        # 4) 作废结算、订单退回未结算状态、留存冲正记录与流水，同一事务完成。
        #    冲正只改变结算状态，不改动收退金额，未收金额保持冲正前的值。
        outstanding = row["amount_cents"] - row["paid_cents"] + row["refunded_cents"]
        conn.execute(
            "UPDATE settlements SET status='reversed' WHERE tenant=? AND settlement_id=?",
            (tenant, active["settlement_id"]),
        )
        conn.execute(
            "INSERT INTO reversals(tenant, reversal_id, settlement_id, order_id, reason, amount_cents, outstanding_after) "
            "VALUES(?,?,?,?,?,?,?)",
            (tenant, reversal_id, active["settlement_id"], order_id, reason, active["amount_cents"], outstanding),
        )
        conn.execute(
            "UPDATE orders SET status='accepted' WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        )
        ledger.record_entry(conn, tenant, order_id, reversal_id, "reversal", active["amount_cents"], outstanding)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return _reversal_result(
        tenant, order_id, reversal_id, active["settlement_id"], reason, active["amount_cents"], outstanding
    )
