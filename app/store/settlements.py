"""结算单的发起（对账核销）与撤销。

发起结算在单事务内完成「结算标识幂等判定 + 订单存在性 + 未收满判定 +
未冲正收款合计闭合核对 + 结算单落库」，任一条不满足整笔回滚，不留下半笔生效的核销。
撤销在单事务内解除该结算单的核销状态，不触碰订单收款与已收金额。
所有写事务均以 BEGIN IMMEDIATE 串行化，与登记收款/冲正互斥，闭合关系始终成立。
"""
import sqlite3
from uuid import uuid4

from app.store.db import connect

ACTIVE = "active"
REVOKED = "revoked"


class OrderNotFullyPaid(Exception):
    """订单未收满（账面已收金额不等于订单金额），不能发起结算。"""


class SettlementNotBalanced(Exception):
    """对账不闭合：未冲正收款合计、账面已收金额与订单金额三者不一致。"""


class SettlementAlreadyActive(Exception):
    """同一订单已存在未撤销的结算单。"""


class SettlementKeyConflict(Exception):
    """同一结算标识已被用于另一订单。"""


class SettlementNotFound(Exception):
    """结算单不存在（含跨租户点名，统一按不存在处理）。"""


class SettlementAlreadyRevoked(Exception):
    """结算单此前已被撤销。"""


class RevocationConflict(Exception):
    """撤销标识已被用于另一张结算单。"""


def settle(tenant: str, order_id: str, settlement_key: str) -> tuple[dict, bool]:
    """按订单发起结算，返回（结算单视图, 是否首次生成）。失败抛出业务异常，状态不变。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        keyed = conn.execute(
            "SELECT settlement_id, order_id, status FROM settlements WHERE tenant=? AND settlement_key=?",
            (tenant, settlement_key),
        ).fetchone()
        if keyed is not None:
            # 同一结算标识：指向同一订单为幂等重放，返回首次生成的同一张结算单（含其当前状态）；
            # 指向另一订单为冲突，既有状态保持不变
            conn.execute("ROLLBACK")
            if keyed["order_id"] != order_id:
                raise SettlementKeyConflict("settlement id already used for another order")
            return _settlement_view(tenant, keyed["settlement_id"]), False

        order = conn.execute(
            "SELECT amount_cents, paid_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 订单不存在或跨租户点名统一按不存在处理，不泄漏对象是否存在
            conn.execute("ROLLBACK")
            raise SettlementNotFound("order not found")

        active = conn.execute(
            "SELECT settlement_id FROM settlements WHERE tenant=? AND order_id=? AND status='active'",
            (tenant, order_id),
        ).fetchone()
        if active is not None:
            conn.execute("ROLLBACK")
            raise SettlementAlreadyActive("active settlement already exists for order")

        # 对账核销只认真实的未被冲正收款合计，不信任订单状态字段
        live_total = conn.execute(
            "SELECT COALESCE(SUM(p.amount_cents),0) AS s FROM payments p "
            "WHERE p.tenant=? AND p.order_id=? AND NOT EXISTS ("
            "SELECT 1 FROM reversals v WHERE v.tenant=p.tenant AND v.order_id=p.order_id "
            "AND v.payment_id=p.payment_id)",
            (tenant, order_id),
        ).fetchone()["s"]

        if order["paid_cents"] != order["amount_cents"] or live_total != order["amount_cents"]:
            # 未收满与对账不闭合分别以可区分的原因拒绝：前者是钱没收齐，后者是账面/明细对不上
            conn.execute("ROLLBACK")
            if live_total != order["paid_cents"]:
                raise SettlementNotBalanced("reconciled payments do not match booked amount")
            raise OrderNotFullyPaid("order not fully paid")

        settlement_id = uuid4().hex
        try:
            conn.execute(
                "INSERT INTO settlements(tenant, settlement_id, settlement_key, order_id, amount_cents, status) "
                "VALUES(?,?,?,?,?,'active')",
                (tenant, settlement_id, settlement_key, order_id, live_total),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            # 并发兜底：结算标识被占用或该订单已有未撤销结算单，整笔回滚后按可区分原因拒绝
            conn.execute("ROLLBACK")
            clash = connect()
            try:
                same_key = clash.execute(
                    "SELECT settlement_id, order_id FROM settlements WHERE tenant=? AND settlement_key=?",
                    (tenant, settlement_key),
                ).fetchone()
            finally:
                clash.close()
            if same_key is not None:
                if same_key["order_id"] == order_id:
                    # 并发下同标识同订单已落库：按幂等重放返回同一张结算单
                    return _settlement_view(tenant, same_key["settlement_id"]), False
                raise SettlementKeyConflict("settlement id already used for another order")
            raise SettlementAlreadyActive("active settlement already exists for order")
    finally:
        conn.close()
    return _settlement_view(tenant, settlement_id), True


def revoke(tenant: str, settlement_id: str, revocation_id: str) -> dict:
    """撤销结算单：只解除核销状态，不改变订单收款与已收金额。失败抛出业务异常。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        keyed = conn.execute(
            "SELECT settlement_id FROM settlement_revocations WHERE tenant=? AND revocation_id=?",
            (tenant, revocation_id),
        ).fetchone()
        if keyed is not None:
            # 同一撤销标识：指向同一结算单为幂等重放；指向另一结算单为冲突
            conn.execute("ROLLBACK")
            if keyed["settlement_id"] != settlement_id:
                raise RevocationConflict("revocation id already used for another settlement")
            return _revocation_view(tenant, settlement_id, revocation_id)

        settlement = conn.execute(
            "SELECT status FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
        if settlement is None:
            # 结算单不存在或跨租户点名统一按不存在处理
            conn.execute("ROLLBACK")
            raise SettlementNotFound("settlement not found")
        if settlement["status"] == REVOKED:
            conn.execute("ROLLBACK")
            raise SettlementAlreadyRevoked("settlement already revoked")

        try:
            conn.execute(
                "INSERT INTO settlement_revocations(tenant, revocation_id, settlement_id) VALUES(?,?,?)",
                (tenant, revocation_id, settlement_id),
            )
            conn.execute(
                "UPDATE settlements SET status='revoked', "
                "revoked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE tenant=? AND settlement_id=?",
                (tenant, settlement_id),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError:
            # 并发兜底：撤销标识被占用或结算单已被撤销，整笔回滚后按可区分原因拒绝
            conn.execute("ROLLBACK")
            clash = connect()
            try:
                same_id = clash.execute(
                    "SELECT settlement_id FROM settlement_revocations WHERE tenant=? AND revocation_id=?",
                    (tenant, revocation_id),
                ).fetchone()
            finally:
                clash.close()
            if same_id is not None:
                if same_id["settlement_id"] == settlement_id:
                    # 并发下同标识同结算单已落库：按幂等重放返回首次结果
                    return _revocation_view(tenant, settlement_id, revocation_id)
                raise RevocationConflict("revocation id already used for another settlement")
            raise SettlementAlreadyRevoked("settlement already revoked")
    finally:
        conn.close()
    return _revocation_view(tenant, settlement_id, revocation_id)


def get(tenant: str, settlement_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, settlement_id, settlement_key, order_id, amount_cents, status, created_at, revoked_at "
            "FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return dict(row)


def _settlement_view(tenant: str, settlement_id: str) -> dict:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT tenant, settlement_id, settlement_key, order_id, amount_cents, status, created_at, revoked_at "
            "FROM settlements WHERE tenant=? AND settlement_id=?",
            (tenant, settlement_id),
        ).fetchone()
    finally:
        conn.close()
    return dict(row)


def _revocation_view(tenant: str, settlement_id: str, revocation_id: str) -> dict:
    return {
        **_settlement_view(tenant, settlement_id),
        "revocation_id": revocation_id,
    }
