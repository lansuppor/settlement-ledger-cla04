import sqlite3

from app.store.db import connect

# 工单只记录问题与处理过程：所有写操作仅触及 tickets 表，不回写订单金额、状态与账务，
# 因此收款、退款、结算、冲正、导入、检索与对账的任何结果都不受工单影响。

MAX_PAGE_SIZE = 200

# 工单类型限定在五类业务环节内。
TICKET_TYPES = ("accept", "payment", "refund", "settlement", "reversal")

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_RESOLVED = "resolved"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PENDING, STATUS_PROCESSING, STATUS_RESOLVED, STATUS_REJECTED)

# 允许的状态推进：待处理可转处理中或直接转已解决/已驳回；处理中可转已解决/已驳回；
# 已解决与已驳回是终态，不再变化。
_TRANSITIONS = {
    STATUS_PENDING: (STATUS_PROCESSING, STATUS_RESOLVED, STATUS_REJECTED),
    STATUS_PROCESSING: (STATUS_RESOLVED, STATUS_REJECTED),
    STATUS_RESOLVED: (),
    STATUS_REJECTED: (),
}


class Conflict(ValueError):
    """业务冲突（409）。"""


def _view(row: sqlite3.Row) -> dict:
    return {
        "tenant": row["tenant"],
        "ticket_id": row["ticket_id"],
        "order_id": row["order_id"],
        "ticket_type": row["ticket_type"],
        "description": row["description"],
        "status": row["status"],
        "note": row["note"],
        "processed_at": row["processed_at"],
        "created_at": row["created_at"],
    }


def get(tenant: str, ticket_id: str) -> dict | None:
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM tickets WHERE tenant=? AND ticket_id=?",
            (tenant, ticket_id),
        ).fetchone()
    finally:
        conn.close()
    return _view(row) if row is not None else None


def register(
    tenant: str, ticket_id: str, order_id: str, ticket_type: str, description: str
) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # 1) 幂等：工单标识以（租户, 标识）唯一。同标识对同一订单、同一工单类型重复登记
        #    直接返回首次结果（不重复受理）；同标识换订单或换工单类型拒绝。
        prev = conn.execute(
            "SELECT * FROM tickets WHERE tenant=? AND ticket_id=?",
            (tenant, ticket_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            if prev["order_id"] != order_id or prev["ticket_type"] != ticket_type:
                raise Conflict("ticket_id already used")
            return _view(prev)
        # 2) 订单存在性与跨租户：与订单读取一致按不存在处理（404），不泄漏对象是否存在。
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None
        # 3) 登记为待处理；并发下同标识在此撞主键，整笔回滚。
        try:
            conn.execute(
                "INSERT INTO tickets(tenant, ticket_id, order_id, ticket_type, description, status)"
                " VALUES(?,?,?,?,?,'pending')",
                (tenant, ticket_id, order_id, ticket_type, description),
            )
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK")
            raise Conflict("ticket_id already used")
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, ticket_id)


def process(tenant: str, ticket_id: str, status: str, note: str | None) -> dict | None:
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tickets WHERE tenant=? AND ticket_id=?",
            (tenant, ticket_id),
        ).fetchone()
        if row is None:
            # 不存在或跨租户统一按不存在处理，不泄漏对象是否存在。
            conn.execute("ROLLBACK")
            return None
        current = row["status"]
        if status == current:
            conn.execute("ROLLBACK")
            # 重复提交相同目标状态：处理备注一致返回与首次一致的结果（不重复处理）；
            # 备注不同拒绝，工单状态与备注不变。
            if note != row["note"]:
                raise Conflict("note differs for the same target status")
            return _view(row)
        if status not in _TRANSITIONS[current]:
            # 跨状态跳跃或终态再变更：拒绝，工单状态与备注不变。
            conn.execute("ROLLBACK")
            raise Conflict("illegal status transition")
        # 合法推进：更新状态并留存最后一次处理备注与处理时间。
        conn.execute(
            "UPDATE tickets SET status=?, note=?, processed_at=datetime('now')"
            " WHERE tenant=? AND ticket_id=?",
            (status, note, tenant, ticket_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, ticket_id)


def search(
    tenant: str,
    order_id: str | None = None,
    status: str | None = None,
    page_size: int = 50,
    after: tuple[str, str] | None = None,
) -> tuple[list[dict], bool]:
    # 租户内组合过滤，按 (订单标识, 工单标识) 升序做键集分页：同一订单可有多张工单，
    # 以工单标识兜底保证分页稳定、不重不漏；页大小超过上限按上限截断。
    clauses = ["tenant=?"]
    params: list = [tenant]
    if order_id:
        clauses.append("order_id=?")
        params.append(order_id)
    if status:
        clauses.append("status=?")
        params.append(status)
    if after is not None:
        clauses.append("(order_id>? OR (order_id=? AND ticket_id>?))")
        params.extend([after[0], after[0], after[1]])
    size = max(1, min(page_size, MAX_PAGE_SIZE))
    sql = (
        "SELECT * FROM tickets"
        f" WHERE {' AND '.join(clauses)} ORDER BY order_id ASC, ticket_id ASC LIMIT ?"
    )
    params.append(size + 1)
    conn = connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    has_more = len(rows) > size
    return [_view(row) for row in rows[:size]], has_more
