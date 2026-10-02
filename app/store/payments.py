import sqlite3

from app.store import debts, ledger
from app.store.db import connect

# 收款撤销（账务冲回，非退款）。
#
# 撤销按一笔已登记收款的业务标识（pay-<订单内收款序号>）冲回全部或部分金额：
#   已收金额随撤销等额减少，未收金额等额增加，已退金额不变；撤销不删除原收款流水，
#   只累加该笔收款的 reversed_cents，并在账务历史追加 payment_reversal 条目。
# 撤销以（租户, 撤销标识）幂等：同标识对同收款、同金额重复提交返回首次快照，
# 换收款标识或换金额拒绝；累计撤销不得超过该笔收款净额。
# 收款在欠款条目上的核销占用随撤销按欠款编号逆序从该笔收款的占用尾部释放。


class Conflict(ValueError):
    """业务冲突（409）。"""


def _view(row: sqlite3.Row) -> dict:
    # 幂等响应始终呈现首次生效时的快照（含撤销后收款净额与订单各金额、状态）。
    return {
        "reversal_id": row["reversal_id"],
        "payment_ref": row["payment_ref"],
        "amount_cents": row["amount_cents"],
        "payment_net_cents": row["payment_net_after"],
        "paid_cents": row["paid_after"],
        "refunded_cents": row["refunded_after"],
        "outstanding_cents": row["outstanding_after"],
        "status": row["status_after"],
        "created_at": row["created_at"],
    }


def reverse_payment(
    tenant: str,
    order_id: str,
    reversal_id: str,
    payment_ref: str,
    amount_cents: int,
) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 幂等：撤销标识以（租户, 标识）唯一。同标识对同收款、同金额重复提交
        #    直接返回首次结果；换收款业务标识或换金额拒绝（409）。
        prev = conn.execute(
            "SELECT * FROM payment_reversals WHERE tenant=? AND reversal_id=?",
            (tenant, reversal_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            # 收款业务标识在订单内命名：换订单（即便标识字符串相同）、换收款标识或换金额均拒绝。
            if (
                prev["order_id"] != order_id
                or prev["payment_ref"] != payment_ref
                or prev["amount_cents"] != amount_cents
            ):
                raise Conflict("payment reversal id already used")
            return _view(prev)
        # 2) 参数合法性（正整数）在入口模型校验，这里防御性兜底。
        if amount_cents <= 0:
            conn.execute("ROLLBACK")
            raise Conflict("reversal amount must be positive")
        # 3) 订单存在性（跨租户与读取一致按不存在处理，404，不泄漏对象是否存在）。
        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents, currency FROM orders"
            " WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None
        # 4) 被撤销收款须存在且属于该订单；收款业务标识（pay-<订单内收款序号>）在订单内
        #    唯一，不存在或跨租户统一按 404，不泄漏对象是否存在。
        payment = conn.execute(
            "SELECT amount_cents, reversed_cents, amount_cents - reversed_cents AS net_cents"
            " FROM payments WHERE tenant=? AND order_id=? AND payment_ref=?",
            (tenant, order_id, payment_ref),
        ).fetchone()
        if payment is None:
            conn.execute("ROLLBACK")
            return None
        # 5) 累计撤销不得超过该笔收款净额（原收款金额 − 累计已撤销），超出整笔拒绝，
        #    不产生任何记录。
        if amount_cents > payment["net_cents"]:
            conn.execute("ROLLBACK")
            raise Conflict("reversal exceeds payment net amount")
        # 6) 存在生效结算时账务已闭合：须先冲正结算才能撤销收款，
        #    否则会出现“生效结算仍在、订单却有未收”的不一致。
        locked = conn.execute(
            "SELECT 1 FROM settlements WHERE tenant=? AND order_id=? AND status='effective'",
            (tenant, order_id),
        ).fetchone()
        if locked is not None:
            conn.execute("ROLLBACK")
            raise Conflict("order is settled; reverse the settlement before reversing payment")
        # 7) 同事务冲回：订单已收等额减少、未收等额增加；未收大于 0 时订单回到待收。
        paid_after = order["paid_cents"] - amount_cents
        refunded_after = order["refunded_cents"]
        outstanding_after = order["amount_cents"] - paid_after + refunded_after
        status_after = "accepted" if outstanding_after > 0 else "settled"
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents - ?,"
            " status = CASE WHEN amount_cents - (paid_cents - ?) + refunded_cents > 0 THEN 'accepted' ELSE status END"
            " WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        # 8) 收窄该笔收款在欠款条目上的占用：按欠款编号逆序从其占用尾部释放，
        #    “已核销金额之和恒等于订单已收金额”，条目状态随余额回到未核销或保持已核销，
        #    未核销余额之和继续等于订单未收金额。
        debts.release_payment(conn, tenant, order_id, payment_ref, amount_cents)
        payment_net_after = payment["net_cents"] - amount_cents
        conn.execute(
            "UPDATE payments SET reversed_cents = reversed_cents + ?"
            " WHERE tenant=? AND order_id=? AND payment_ref=?",
            (amount_cents, tenant, order_id, payment_ref),
        )
        # 9) 撤销登记落库（并发同标识在此撞主键整笔回滚）。
        try:
            conn.execute(
                "INSERT INTO payment_reversals(tenant, reversal_id, payment_ref, order_id, amount_cents,"
                " payment_net_after, paid_after, refunded_after, outstanding_after, status_after)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    reversal_id,
                    payment_ref,
                    order_id,
                    amount_cents,
                    payment_net_after,
                    paid_after,
                    refunded_after,
                    outstanding_after,
                    status_after,
                ),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise Conflict("payment reversal id already used")
        # 10) 账务历史追加可辨识的撤销条目：含撤销标识、撤销金额与操作后未收金额；
        #     不删除原收款流水，按历史序列逐笔重放仍得到与订单读取一致的未收与状态。
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
