import sqlite3

from app.store.db import connect

MAX_PAGE_SIZE = 200

# 工单类型限定在受理、收款、退款、结算、冲正五类。
TICKET_TYPES = ("acceptance", "payment", "refund", "settlement", "reversal")

STATUSES = ("pending", "processing", "resolved", "rejected")

# 状态只沿 待处理→处理中→已解决/已驳回 的方向推进；已解决与已驳回是终态，不再变化。
_NEXT = {
    "pending": ("processing", "resolved", "rejected"),
    "processing": ("resolved", "rejected"),
    "resolved": (),
    "rejected": (),
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
        "created_at": row["created_at"],
        "processed_at": row["processed_at"],
    }


def register(tenant: str, order_id: str, ticket_id: str, ticket_type: str, description: str) -> dict | None:
    # 幂等登记：同一（租户, 工单标识）只受理一次。同标识对同一订单、同一工单类型重复
    # 登记返回首次结果；同标识换订单或换工单类型拒绝。订单不存在或跨租户按不存在处理。
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        prev = conn.execute(
            "SELECT * FROM tickets WHERE tenant=? AND ticket_id=?",
            (tenant, ticket_id),
        ).fetchone()
        if prev is not None:
            conn.execute("ROLLBACK")
            if prev["order_id"] != order_id or prev["ticket_type"] != ticket_type:
                raise Conflict("ticket_id already used")
            return _view(prev)
        order = conn.execute(
            "SELECT 1 FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            conn.execute("ROLLBACK")
            return None
        conn.execute(
            "INSERT INTO tickets(tenant, ticket_id, order_id, ticket_type, description, status)"
            " VALUES(?,?,?,?,?,'pending')",
            (tenant, ticket_id, order_id, ticket_type, description),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, ticket_id)


def process(tenant: str, ticket_id: str, target_status: str, note: str | None) -> dict | None:
    # 处理只推进工单自身状态，不触碰订单的金额、状态与任何账务。
    # 重复提交相同目标状态且备注一致返回首次结果（不重复处理，处理时间不变）；
    # 目标状态与当前状态相同但备注不同、或跨状态跳跃违反推进顺序，均拒绝且状态与备注不变。
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM tickets WHERE tenant=? AND ticket_id=?",
            (tenant, ticket_id),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            return None
        if row["status"] == target_status:
            conn.execute("ROLLBACK")
            if row["note"] != note:
                raise Conflict("same target status with different note")
            return _view(row)
        if target_status not in _NEXT[row["status"]]:
            conn.execute("ROLLBACK")
            raise Conflict("illegal status transition")
        conn.execute(
            "UPDATE tickets SET status=?, note=?, processed_at=datetime('now') WHERE tenant=? AND ticket_id=?",
            (target_status, note, tenant, ticket_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return get(tenant, ticket_id)


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


def search(
    tenant: str,
    order_id: str | None = None,
    status: str | None = None,
    page_size: int = 50,
    after: tuple[str, str] | None = None,
) -> tuple[list[dict], bool]:
    # 租户内按订单标识、处理状态任意组合过滤，按（订单标识, 工单标识）升序做键集分页，
    # 相同条件下分页稳定、不重不漏；页大小超过上限按上限截断。
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
    sql = f"SELECT * FROM tickets WHERE {' AND '.join(clauses)} ORDER BY order_id ASC, ticket_id ASC LIMIT ?"
    params.append(size + 1)
    conn = connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    has_more = len(rows) > size
    return [_view(row) for row in rows[:size]], has_more
