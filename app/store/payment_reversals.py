import sqlite3

from app.store import debts, ledger
from app.store.db import connect

# 收款撤销（账务冲回，非退款）。
#
# 撤销按一笔已登记收款的业务标识（pay-<订单内收款序号>）冲回其全部或部分金额：
# 订单已收随撤销等额减少、未收等额增加，已退金额不变；未收大于 0 时订单回到待收。
# 撤销不删除原收款流水，而是在订单账务历史追加一条 payment_reversal 条目。
#
# 幂等：撤销标识以（租户, 撤销标识）唯一。同标识对同一订单、同一收款、同一金额
# 重复提交返回首次快照；换订单、换收款业务标识或换金额拒绝（409）。
# 同一笔收款可被多笔不同标识分次冲回，累计撤销不得超过其净额，超出整笔拒绝。


class Conflict(ValueError):
    """业务冲突（409）。"""


def _view(row: sqlite3.Row) -> dict:
    # 幂等响应始终呈现首次生效时的快照：即便撤销后订单又发生收款/退款，
    # 同一撤销标识重放仍返回与首次一致的结果。
    return {
        "reversal_id": row["reversal_id"],
        "order_id": row["order_id"],
        "payment_ref": row["payment_ref"],
        "amount_cents": row["amount_cents"],
        "payment_net_cents": row["payment_net_after"],
        "paid_cents": row["paid_after"],
        "refunded_cents": row["refunded_cents"],
        "outstanding_cents": row["outstanding_after"],
        "status": row["status_after"],
        "created_at": row["created_at"],
    }


def reverse(
    tenant: str, order_id: str, reversal_id: str, payment_ref: str, amount_cents: int
) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 幂等：撤销标识以（租户, 标识）唯一。同标识对同订单、同收款、同金额重复提交
        #    直接返回首次结果；换订单、换收款或换金额拒绝（409）。
        prev = conn.execute(
            "SELECT * FROM payment_reversals WHERE tenant=? AND reversal_id=?",
            (tenant, reversal_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            if (
                prev["order_id"] != order_id
                or prev["payment_ref"] != payment_ref
                or prev["amount_cents"] != amount_cents
            ):
                raise Conflict("reversal_id already used")
            return _view(prev)
        # 2) 订单存在性（跨租户与读取一致按不存在处理，不泄漏对象是否存在）。
        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, currency, status FROM orders"
            " WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None
        # 3) 被撤销收款须存在且属于该订单；不存在或跨租户统一按 404。
        payment = conn.execute(
            "SELECT pay_no, amount_cents, reversed_cents FROM payments"
            " WHERE tenant=? AND order_id=? AND biz_ref=?",
            (tenant, order_id, payment_ref),
        ).fetchone()
        if payment is None:
            conn.execute("ROLLBACK")
            return None
        # 4) 存在生效结算时账务已闭合：撤销（会回冲未收并退回未结算状态）必须先冲正结算。
        locked = conn.execute(
            "SELECT 1 FROM settlements WHERE tenant=? AND order_id=? AND status='effective'",
            (tenant, order_id),
        ).fetchone()
        if locked is not None:
            conn.execute("ROLLBACK")
            raise Conflict("order is settled; reverse the settlement before reversing payment")
        payment_net = payment["amount_cents"] - payment["reversed_cents"]
        # 5) 撤销金额为正整数，且累计撤销不得超过该笔收款净额；超出整笔拒绝，不产生任何记录。
        if amount_cents <= 0 or amount_cents > payment_net:
            conn.execute("ROLLBACK")
            raise Conflict("reversal exceeds net amount of the payment")
        # 6) 计算冲回后快照：已收等额减少、未收等额增加，已退不变；未收大于 0 回到待收。
        paid_after = order["paid_cents"] - amount_cents
        outstanding_after = order["amount_cents"] - paid_after + order["refunded_cents"]
        status_after = "accepted" if outstanding_after > 0 else order["status"]
        payment_net_after = payment_net - amount_cents
        # 7) 同事务生效：累计撤销额、订单已收/状态、欠款占用核销、账务历史一致改写，
        #    失败整体回滚不留半截记录。
        conn.execute(
            "UPDATE payments SET reversed_cents = reversed_cents + ?"
            " WHERE tenant=? AND order_id=? AND pay_no=?",
            (amount_cents, tenant, order_id, payment["pay_no"]),
        )
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents - ?,"
            " status = CASE WHEN amount_cents - (paid_cents - ?) + refunded_cents > 0"
            " THEN 'accepted' ELSE status END WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        # 7.1) 按欠款编号逆序从该笔收款占用的核销尾部释放，保持
        #      Σ已核销 = 订单已收、Σ未核销余额 = 订单未收，条目状态随余额回落。
        debts.release_payment(conn, tenant, order_id, amount_cents)
        # 7.2) 撤销登记落库（并发同标识在此撞主键整笔回滚）；CHECK 兜底累计撤销不超额。
        try:
            conn.execute(
                "INSERT INTO payment_reversals(tenant, reversal_id, order_id, payment_ref, amount_cents,"
                " payment_net_after, paid_after, refunded_cents, outstanding_after, status_after)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    reversal_id,
                    order_id,
                    payment_ref,
                    amount_cents,
                    payment_net_after,
                    paid_after,
                    order["refunded_cents"],
                    outstanding_after,
                    status_after,
                ),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise Conflict("reversal_id already used")
        # 8) 账务历史追加可辨识的撤销条目：含撤销标识、撤销金额与操作后未收金额，
        #    按历史序列逐笔重放仍得到与订单读取一致的最终未收金额与状态。
        ledger.append(
            conn,
            tenant,
            order_id,
            reversal_id,
            ledger.ENTRY_PAYMENT_REVERSAL,
            amount_cents,
            outstanding_after,
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM payment_reversals WHERE tenant=? AND reversal_id=?",
            (tenant, reversal_id),
        ).fetchone()
    finally:
        conn.close()
    return _view(row) if row is not None else None
