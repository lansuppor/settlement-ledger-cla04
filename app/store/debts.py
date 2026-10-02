import sqlite3

from app.store import ledger
from app.store.db import connect

# 欠款条目与收款核销。
#
# 不变量（对任意订单、任意时刻）：
#   欠款金额之和 = 订单金额 + 已退金额
#   已核销金额之和 = 订单已收金额
#   未核销余额之和 = 订单未收金额 = 订单金额 − 已收 + 已退
#
# 欠款条目不随订单金额字段改写：受理生成第 1 条，每笔退款成功追加一条，金额固定。
# 为使“已核销之和 = 已收金额”恒成立，收款登记按 debt_no 升序自动占用各条目余额；
# 收款核销登记则把一笔已到账金额从其它条目自动占用的尾部空间改配（re-pin）到指定条目，
# 订单的已收/已退/未收金额与状态均不变化。

ENTRY_DEBT_SETTLED = "settled"
ENTRY_DEBT_UNSETTLED = "unsettled"


class Conflict(ValueError):
    """业务冲突（409）。"""


def open_first(
    conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int, currency: str
) -> None:
    # 订单受理：生成第 1 条欠款，金额等于订单金额。
    conn.execute(
        "INSERT INTO debt_entries(tenant, order_id, debt_no, amount_cents, settled_cents, currency, status)"
        " VALUES(?,?,1,?,0,?,'unsettled')",
        (tenant, order_id, amount_cents, currency),
    )


def open_next(
    conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int, currency: str
) -> None:
    # 退款成功：追加一条金额等于退回金额的未核销欠款，订单内编号顺序递增。
    row = conn.execute(
        "SELECT COALESCE(MAX(debt_no), 0) + 1 AS next FROM debt_entries WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    ).fetchone()
    conn.execute(
        "INSERT INTO debt_entries(tenant, order_id, debt_no, amount_cents, settled_cents, currency, status)"
        " VALUES(?,?,?,?,0,?,'unsettled')",
        (tenant, order_id, row["next"], amount_cents, currency),
    )


def apply_payment(conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int) -> None:
    # 收款登记：按欠款编号升序逐笔占用未核销余额（先入账的钱先核销最早的欠款）。
    # 调用方已保证 amount_cents <= 订单未收 = 各条目余额之和，故循环结束恰好分完。
    remaining = amount_cents
    rows = conn.execute(
        "SELECT debt_no, amount_cents - settled_cents AS room FROM debt_entries"
        " WHERE tenant=? AND order_id=? AND amount_cents - settled_cents > 0 ORDER BY debt_no ASC",
        (tenant, order_id),
    ).fetchall()
    for row in rows:
        take = min(row["room"], remaining)
        conn.execute(
            "UPDATE debt_entries SET settled_cents = settled_cents + ?,"
            " status = CASE WHEN settled_cents + ? >= amount_cents THEN 'settled' ELSE status END"
            " WHERE tenant=? AND order_id=? AND debt_no=?",
            (take, take, tenant, order_id, row["debt_no"]),
        )
        remaining -= take
        if remaining == 0:
            break


def release_payment(conn: sqlite3.Connection, tenant: str, order_id: str, amount_cents: int) -> None:
    # 收款撤销：按欠款编号逆序从该笔收款占用的核销尾部释放（与收款升序占用互为镜像），
    # 较早的欠款优先保留核销；释放后“已核销金额之和”随订单已收等额下降。
    # 调用方已保证 amount_cents <= 被撤销收款净额，且 Σ已核销 = 订单已收 >= 该净额，
    # 故各条目已核销金额之和足以覆盖本次释放，循环结束恰好释放完。
    remaining = amount_cents
    rows = conn.execute(
        "SELECT debt_no, settled_cents FROM debt_entries"
        " WHERE tenant=? AND order_id=? AND settled_cents > 0 ORDER BY debt_no DESC",
        (tenant, order_id),
    ).fetchall()
    for row in rows:
        take = min(row["settled_cents"], remaining)
        conn.execute(
            "UPDATE debt_entries SET settled_cents = settled_cents - ?"
            " WHERE tenant=? AND order_id=? AND debt_no=?",
            (take, tenant, order_id, row["debt_no"]),
        )
        remaining -= take
        if remaining == 0:
            break
    # 释放后各条目状态随未核销余额正确回到未核销或保持已核销。
    conn.execute(
        "UPDATE debt_entries SET status = CASE WHEN settled_cents >= amount_cents THEN 'settled' ELSE 'unsettled' END"
        " WHERE tenant=? AND order_id=?",
        (tenant, order_id),
    )


def _writeoff_view(row: sqlite3.Row) -> dict:
    # 幂等响应始终呈现首次生效时的快照：目标条目核销后的已核销/未核销金额、
    # 该订单核销后仍未核销的欠款合计都从登记快照常量化返回。
    return {
        "writeoff_id": row["writeoff_id"],
        "order_id": row["order_id"],
        "debt_no": row["debt_no"],
        "amount_cents": row["amount_cents"],
        "settled_cents": row["settled_after"],
        "remaining_cents": row["remaining_after"],
        "remaining_total_cents": row["remaining_total_after"],
        "status": ENTRY_DEBT_SETTLED if row["remaining_after"] == 0 else ENTRY_DEBT_UNSETTLED,
        "created_at": row["created_at"],
    }


def register(
    tenant: str, order_id: str, writeoff_id: str, debt_no: int, amount_cents: int
) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 幂等：核销标识以（租户, 标识）唯一。同标识对同订单、同欠款编号、同金额重复提交
        #    直接返回首次结果；换订单、换欠款编号或换金额拒绝（409）。
        prev = conn.execute(
            "SELECT * FROM writeoffs WHERE tenant=? AND writeoff_id=?",
            (tenant, writeoff_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            if (
                prev["order_id"] != order_id
                or prev["debt_no"] != debt_no
                or prev["amount_cents"] != amount_cents
            ):
                raise Conflict("writeoff_id already used")
            return _writeoff_view(prev)
        # 2) 订单存在性（跨租户与读取一致按不存在处理）。
        order = conn.execute(
            "SELECT amount_cents, paid_cents, refunded_cents FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None
        # 3) 欠款条目须存在且属于该订单；不存在或跨租户按不存在处理（404）。
        debt = conn.execute(
            "SELECT amount_cents, settled_cents FROM debt_entries"
            " WHERE tenant=? AND order_id=? AND debt_no=?",
            (tenant, order_id, debt_no),
        ).fetchone()
        if debt is None:
            conn.execute("ROLLBACK")
            return None
        # 4) 存在生效结算时账务已闭合：须先冲正结算才能核销。
        locked = conn.execute(
            "SELECT 1 FROM settlements WHERE tenant=? AND order_id=? AND status='effective'",
            (tenant, order_id),
        ).fetchone()
        if locked is not None:
            conn.execute("ROLLBACK")
            raise Conflict("order is settled; reverse the settlement before writing off")
        remaining_target = debt["amount_cents"] - debt["settled_cents"]
        # 5) 核销金额不得超过该条目当前未核销余额，超出整笔拒绝，不产生任何记录。
        if amount_cents <= 0 or amount_cents > remaining_target:
            conn.execute("ROLLBACK")
            raise Conflict("writeoff exceeds unsettled balance of the debt entry")
        # 6) 已核销之和恒等于已收金额：核销只改配已到账款项。可从其它条目释放的金额
        #    = 已收 − 目标条目当前已核销；不足时同样整笔拒绝（无对应到账金额可核销）。
        #    按编号逆序从自动占用的尾部释放，较早的欠款优先保留核销。
        freable_rows = conn.execute(
            "SELECT debt_no, settled_cents FROM debt_entries"
            " WHERE tenant=? AND order_id=? AND debt_no != ? AND settled_cents > 0"
            " ORDER BY debt_no DESC",
            (tenant, order_id, debt_no),
        ).fetchall()
        to_free = amount_cents
        plan: list[tuple[int, int]] = []
        for row in freable_rows:
            take = min(row["settled_cents"], to_free)
            plan.append((row["debt_no"], take))
            to_free -= take
            if to_free == 0:
                break
        if to_free > 0:
            conn.execute("ROLLBACK")
            raise Conflict("writeoff exceeds arrived amount available for the debt entry")
        # 7) 同事务生效：先从其它条目释放，再加到目标条目（CHECK 约束兜底不超额）。
        for other_no, take in plan:
            conn.execute(
                "UPDATE debt_entries SET settled_cents = settled_cents - ? WHERE tenant=? AND order_id=? AND debt_no=?",
                (take, tenant, order_id, other_no),
            )
        conn.execute(
            "UPDATE debt_entries SET settled_cents = settled_cents + ?,"
            " status = CASE WHEN settled_cents + ? >= amount_cents THEN 'settled' ELSE status END"
            " WHERE tenant=? AND order_id=? AND debt_no=?",
            (amount_cents, amount_cents, tenant, order_id, debt_no),
        )
        conn.execute(
            "UPDATE debt_entries SET status = CASE WHEN settled_cents >= amount_cents THEN 'settled' ELSE 'unsettled' END"
            " WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        )
        settled_after = debt["settled_cents"] + amount_cents
        remaining_after = debt["amount_cents"] - settled_after
        remaining_total = conn.execute(
            "SELECT COALESCE(SUM(amount_cents - settled_cents), 0) AS n FROM debt_entries"
            " WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()["n"]
        # 8) 核销登记落库（并发同标识在此撞主键整笔回滚），并向账务历史追加核销条目：
        #    含核销标识、核销金额与核销后仍未核销的欠款合计（等于订单未收金额）。
        try:
            conn.execute(
                "INSERT INTO writeoffs(tenant, writeoff_id, order_id, debt_no, amount_cents,"
                " settled_after, remaining_after, remaining_total_after)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    writeoff_id,
                    order_id,
                    debt_no,
                    amount_cents,
                    settled_after,
                    remaining_after,
                    remaining_total,
                ),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise Conflict("writeoff_id already used")
        ledger.append(
            conn, tenant, order_id, writeoff_id, ledger.ENTRY_WRITEOFF, amount_cents, remaining_total
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM writeoffs WHERE tenant=? AND writeoff_id=?",
            (tenant, writeoff_id),
        ).fetchone()
    finally:
        conn.close()
    return _writeoff_view(row) if row is not None else None


def list_debts(tenant: str, order_id: str) -> list[dict] | None:
    conn = connect()
    try:
        exists = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?", (tenant, order_id)
        ).fetchone()
        if exists is None:
            return None
        rows = conn.execute(
            "SELECT debt_no, amount_cents, settled_cents, amount_cents - settled_cents AS remaining_cents, status"
            " FROM debt_entries WHERE tenant=? AND order_id=? ORDER BY debt_no ASC",
            (tenant, order_id),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "debt_no": row["debt_no"],
            "amount_cents": row["amount_cents"],
            "settled_cents": row["settled_cents"],
            "remaining_cents": row["remaining_cents"],
            "status": row["status"],
        }
        for row in rows
    ]
