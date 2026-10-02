import sqlite3

from app.store import ledger
from app.store.db import connect

# 收款核销：把一笔到账金额逐笔核销到指定订单的指定欠款条目上。
#
# 欠款条目（debt_items）：订单受理时生成欠款编号 1（金额 = 受理时订单金额）；每笔退款
# 成功后追加一条（金额 = 该笔退回金额）。条目欠款金额生成后不再变化，已核销金额随核销
# 单调增加，未核销余额 = 欠款金额 − 已核销金额；余额为 0 时状态为已核销（closed），
# 否则为未核销（open，含部分核销）。核销只作用于欠款条目与账务留痕，不回写订单的
# 已收、已退、未收金额与订单状态，因此收款、退款、结算、冲正、导入、检索与对账的
# 任何结果都不受核销影响。
#
# 闭合关系：Σ欠款金额 = 订单金额 + 已退金额；每笔到账（收款）经核销后，核销金额同时
# 减少条目未核销余额，恒有 Σ未核销余额 = 订单未收金额（未核销的到账在核销前体现为
# 条目余额高于订单未收，核销后恢复相等）。

ITEM_OPEN = "open"      # 未核销（含部分核销）
ITEM_CLOSED = "closed"  # 已核销（余额为 0）


class Conflict(ValueError):
    """业务冲突（409）。"""


def _item_view(row: sqlite3.Row) -> dict:
    return {
        "debt_no": row["debt_no"],
        "amount_cents": row["amount_cents"],
        "written_off_cents": row["written_off_cents"],
        "outstanding_cents": row["amount_cents"] - row["written_off_cents"],
        "status": row["status"],
    }


def _write_off_view(row: sqlite3.Row) -> dict:
    return {
        "tenant": row["tenant"],
        "write_off_id": row["write_off_id"],
        "order_id": row["order_id"],
        "debt_no": row["debt_no"],
        "amount_cents": row["amount_cents"],
        "item_outstanding_cents": row["item_balance_after"],
        "outstanding_cents": row["order_unwritten_after"],
        "created_at": row["created_at"],
    }


def append_item(conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int) -> None:
    # 在调用方事务内追加一条欠款条目：欠款编号在订单内从 1 开始按序编号，状态未核销。
    row = conn.execute(
        "SELECT COALESCE(MAX(debt_no), 0) + 1 AS next FROM debt_items WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    conn.execute(
        "INSERT INTO debt_items(tenant, order_id, debt_no, amount_cents, status) VALUES(?,?,?,?,'open')",
        (tenant, order_id, row["next"], amount_cents),
    )


def list_items(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?", (tenant, order_id)
        ).fetchone()
        if exists is None:
            return None
        rows = conn.execute(
            "SELECT debt_no, amount_cents, written_off_cents, status FROM debt_items"
            " WHERE tenant=? AND order_id=? ORDER BY debt_no ASC",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [_item_view(row) for row in rows]


def write_off(
    tenant: str, order_id: str, write_off_id: str, debt_no: int, amount_cents: int
) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 幂等：核销标识以（租户, 标识）唯一。同标识对同一订单、同一欠款编号、同一金额
        #    重复提交直接返回首次结果（不重复核销）；同标识换订单、换欠款编号或换金额拒绝。
        prev = conn.execute(
            "SELECT * FROM write_offs WHERE tenant=? AND write_off_id=?",
            (tenant, write_off_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            if (
                prev["order_id"] != order_id
                or prev["debt_no"] != debt_no
                or prev["amount_cents"] != amount_cents
            ):
                raise Conflict("write_off_id already used")
            return _write_off_view(prev)
        # 2) 订单存在性与跨租户：与订单读取一致按不存在处理（404），不泄漏对象是否存在。
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None
        # 3) 欠款条目须存在且属于该订单；条目主键含订单标识，跨订单/跨租户天然按不存在处理。
        item = conn.execute(
            "SELECT amount_cents, written_off_cents FROM debt_items"
            " WHERE tenant=? AND order_id=? AND debt_no=?",
            (tenant, order_id, debt_no),
        ).fetchone()
        if item is None:
            conn.execute("ROLLBACK")
            return None
        # 4) 存在生效结算时订单账务已闭合：核销须先冲正结算（与退款同一约束）。
        locked = conn.execute(
            "SELECT 1 FROM settlements WHERE tenant=? AND order_id=? AND status='effective'",
            (tenant, order_id),
        ).fetchone()
        if locked is not None:
            conn.execute("ROLLBACK")
            raise Conflict("order is settled; reverse the settlement before writing off")
        # 5) 核销金额不得超过该条目当前未核销余额，超出时整笔拒绝且不产生任何记录。
        balance = item["amount_cents"] - item["written_off_cents"]
        if amount_cents <= 0 or amount_cents > balance:
            conn.execute("ROLLBACK")
            raise Conflict("write-off exceeds unwritten balance")
        # 6) 条目余额更新：条件更新兜底并发——同一欠款条目并发核销时至多一笔成功，
        #    其余因余额不足在此落空，按超出未核销余额拒绝；恒有 已核销 <= 欠款金额。
        cursor = conn.execute(
            "UPDATE debt_items"
            " SET written_off_cents = written_off_cents + ?,"
            "     status = CASE WHEN written_off_cents + ? >= amount_cents THEN 'closed' ELSE 'open' END"
            " WHERE tenant=? AND order_id=? AND debt_no=? AND amount_cents - written_off_cents >= ?",
            (amount_cents, amount_cents, tenant, order_id, debt_no, amount_cents),
        )
        if cursor.rowcount != 1:
            conn.execute("ROLLBACK")
            raise Conflict("write-off exceeds unwritten balance")
        item_balance_after = balance - amount_cents
        order_unwritten_after = conn.execute(
            "SELECT COALESCE(SUM(amount_cents - written_off_cents), 0) AS rest"
            " FROM debt_items WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()["rest"]
        # 7) 核销登记落库：并发下同标识在此撞主键，整笔回滚。
        try:
            conn.execute(
                "INSERT INTO write_offs(tenant, write_off_id, order_id, debt_no, amount_cents,"
                " item_balance_after, order_unwritten_after) VALUES(?,?,?,?,?,?,?)",
                (
                    tenant,
                    write_off_id,
                    order_id,
                    debt_no,
                    amount_cents,
                    item_balance_after,
                    order_unwritten_after,
                ),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise Conflict("write_off_id already used")
        # 8) 账务留痕：核销登记、条目余额更新、账务历史在单个事务内一次生效，失败整体回滚。
        #    核销条目的操作后金额记录该订单核销后仍未核销的欠款合计。
        ledger.append(
            conn, tenant, order_id, write_off_id, ledger.ENTRY_WRITE_OFF, amount_cents, order_unwritten_after
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM write_offs WHERE tenant=? AND write_off_id=?",
            (tenant, write_off_id),
        ).fetchone()
    finally:
        conn.close()
    return _write_off_view(row) if row is not None else None
