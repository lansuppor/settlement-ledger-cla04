"""收款批量导入的存储层。

一个批次（调用方指定的 batch_id）携带多条待登记收款，按提交顺序逐行受理：

- 每一行在独立的 ``BEGIN IMMEDIATE`` 事务内完成“登记收款 + 落批次结果”，
  行内原子：不可能出现收款已落而结果行未落（或反之）的半行状态；
- 业务拒绝（订单不存在/跨租户、金额非法、超过未收金额、批内重复）在该行
  事务内落一条 rejected 结果并提交，不影响批内其他行；
- 非预期的内部错误只回滚当前行，此前已提交的行保留；调用方用同一批次号
  重新提交即可安全续跑，最终结果与一次连续跑完一致；
- (tenant, batch_id, line_no) 是本次导入的行标识：已落库的行重放时返回
  首次结果（幂等，不重复登记收款），同一标识带不同负载重放记为冲突，
  既有状态保持不变。
"""
import sqlite3
import uuid

from app.store.db import connect

# 结构化拒绝原因：业务拒绝（随批次行落库、出现在逐行结果中）与 HTTP 500
# 内部错误严格区分，绝不混为一谈。
REASON_ORDER_NOT_FOUND = "order_not_found"          # 订单不存在或跨租户（不泄漏对象是否存在）
REASON_INVALID_AMOUNT = "invalid_amount"            # 金额非法（非正整数）
REASON_EXCEEDS_OUTSTANDING = "exceeds_outstanding"  # 收款后超过订单未收金额
REASON_DUPLICATE_ORDER = "duplicate_order"          # 同批次内后一条记录复用了前一条的订单标识
REASON_LINE_TAKEN = "line_no_taken"                  # 同一提交清单内行内序号重复（无法再落库）
REASON_CONFLICT = "identifier_conflict"             # 已落库的行标识被携带不同金额/订单重放

REASON_MESSAGES = {
    REASON_ORDER_NOT_FOUND: "order not found",
    REASON_INVALID_AMOUNT: "amount must be a positive integer",
    REASON_EXCEEDS_OUTSTANDING: "payment exceeds outstanding amount",
    REASON_DUPLICATE_ORDER: "duplicate order id within batch",
    REASON_LINE_TAKEN: "duplicate line_no within this submission",
    REASON_CONFLICT: "batch line already recorded with a different payload",
}


def submit(
    tenant: str,
    batch_id: str,
    lines: list[dict],
    *,
    internal_failure: dict | None = None,
) -> dict:
    """逐行受理一个批次，返回整体汇总与逐条结果。

    lines: 每项含 line_no、order_id、amount_cents，按提交顺序受理。
    internal_failure: 测试钩子，形如 {"line_no": n}，处理到该新行时注入一次
    内部错误（回滚该行后抛出），用于验证中断续跑；生产路径不传。
    """
    conn = connect()
    try:
        # 预载本批已落库行出现过的订单标识（含成功与拒绝行）：续跑时批内“重复订单”
        # 是按提交位置的规则——任何更靠前的行已用过该订单，后续行即重复，
        # 与该行最终成功与否无关——从而保证中断续跑与一次连续跑完结果一致。
        stored = conn.execute(
            "SELECT DISTINCT order_id FROM payment_imports WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchall()
        seen_orders: set[str] = {row["order_id"] for row in stored}

        # 仅统计“本次提交清单”内已经出现过的行号：同一清单里序号重复的后一条
        # 无法占用同一主键落库，只作为本次响应中的拒绝结果。
        request_lines: set[int] = set()

        results: list[dict] = []
        accepted = rejected = 0

        for raw in lines:
            line_no = raw["line_no"]
            order_id = raw["order_id"]
            amount_cents = raw["amount_cents"]

            if line_no in request_lines:
                results.append(_rejected_view(batch_id, line_no, order_id, REASON_LINE_TAKEN))
                rejected += 1
                continue
            request_lines.add(line_no)

            view = _process_line(
                conn, tenant, batch_id, line_no, order_id, amount_cents, seen_orders, internal_failure
            )
            results.append(view)
            if view["status"] == "accepted":
                accepted += 1
                seen_orders.add(order_id)
            elif view.get("reject_reason") == REASON_CONFLICT:
                # 冲突回放：该行首受理时的订单已在预载集合中，本次携带的不同订单不得污染判定
                rejected += 1
            else:
                rejected += 1
                # 位置规则：无论首行最终受理还是被拒，后续同订单行都按重复处理
                seen_orders.add(order_id)

        return {
            "tenant": tenant,
            "batch_id": batch_id,
            "total": len(results),
            "accepted_count": accepted,
            "rejected_count": rejected,
            "lines": results,
        }
    finally:
        conn.close()


def _process_line(
    conn: sqlite3.Connection,
    tenant: str,
    batch_id: str,
    line_no: int,
    order_id: str,
    amount_cents: int,
    seen_orders: set[str],
    internal_failure: dict | None,
) -> dict:
    """在独立事务内受理一行；业务拒绝返回 rejected 视图并提交，内部错误向上抛。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT line_no, order_id, amount_cents, status, payment_id, reject_reason "
            "FROM payment_imports WHERE tenant=? AND batch_id=? AND line_no=?",
            (tenant, batch_id, line_no),
        ).fetchone()
        if existing is not None:
            # 已落库行：同负载幂等回放；不同负载（金额/订单）记为冲突，既有状态不动
            view = _existing_view(conn, tenant, batch_id, existing, order_id, amount_cents)
            conn.execute("COMMIT")
            return view

        # 新行：先校验金额（逐行拒绝，不拖垮整批），再做批内重复判定
        valid_amount = isinstance(amount_cents, int) and not isinstance(amount_cents, bool) and amount_cents > 0
        if not valid_amount:
            return _commit_reject(conn, tenant, batch_id, line_no, order_id, None, REASON_INVALID_AMOUNT)
        if order_id in seen_orders:
            # 同批次两条记录使用相同订单标识：后一条不得覆盖前一条，记为重复被拒
            return _commit_reject(conn, tenant, batch_id, line_no, order_id, amount_cents, REASON_DUPLICATE_ORDER)

        if internal_failure is not None and line_no == internal_failure.get("line_no"):
            # 模拟服务中断：本行什么都没提交，回滚后交由调用方凭同标识续跑
            conn.execute("ROLLBACK")
            raise RuntimeError("simulated internal failure mid-batch")

        order = conn.execute(
            "SELECT amount_cents, paid_cents, currency, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        if order is None:
            # 查询带 tenant 条件：跨租户提交同样落到这里，不泄漏对象是否存在
            return _commit_reject(conn, tenant, batch_id, line_no, order_id, amount_cents, REASON_ORDER_NOT_FOUND)
        if order["paid_cents"] + amount_cents > order["amount_cents"]:
            return _commit_reject(conn, tenant, batch_id, line_no, order_id, amount_cents, REASON_EXCEEDS_OUTSTANDING)

        payment_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO payments(tenant, order_id, payment_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, order_id, payment_id, amount_cents),
        )
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, "
            "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (amount_cents, amount_cents, tenant, order_id),
        )
        conn.execute(
            "INSERT INTO payment_imports(tenant, batch_id, line_no, order_id, amount_cents, "
            "status, payment_id) VALUES(?,?,?,?,?, 'accepted', ?)",
            (tenant, batch_id, line_no, order_id, amount_cents, payment_id),
        )
        latest = conn.execute(
            "SELECT amount_cents, paid_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, order_id),
        ).fetchone()
        view = _accepted_view(batch_id, line_no, order_id, payment_id, latest)
        conn.execute("COMMIT")
        return view
    except sqlite3.IntegrityError:
        # 并发兜底：同批次行被另一个已提交的请求抢先落库。回滚后重读，按回放/冲突处理。
        conn.execute("ROLLBACK")
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT line_no, order_id, amount_cents, status, payment_id, reject_reason "
            "FROM payment_imports WHERE tenant=? AND batch_id=? AND line_no=?",
            (tenant, batch_id, line_no),
        ).fetchone()
        if existing is None:
            conn.execute("ROLLBACK")
            raise
        view = _existing_view(conn, tenant, batch_id, existing, order_id, amount_cents)
        conn.execute("COMMIT")
        return view


def _commit_reject(
    conn: sqlite3.Connection,
    tenant: str,
    batch_id: str,
    line_no: int,
    order_id: str,
    amount_cents: int,
    reason: str,
) -> dict:
    """落一条 rejected 批次行并提交；拒绝不产生收款，不影响其他行。"""
    conn.execute(
        "INSERT INTO payment_imports(tenant, batch_id, line_no, order_id, amount_cents, "
        "status, reject_reason) VALUES(?,?,?,?,?, 'rejected', ?)",
        (tenant, batch_id, line_no, order_id, amount_cents, reason),
    )
    conn.execute("COMMIT")
    return _rejected_view(batch_id, line_no, order_id, reason)


def _same_amount(stored_amount: int | None, submitted_amount: object) -> bool:
    """比较已落库金额与本次提交金额。

    非法金额拒绝行落库为 NULL：本次仍提交非法金额视为同负载幂等回放；
    本次改传正整数则属于“同一标识带不同金额”，按冲突处理。
    """
    if stored_amount is None:
        return not (isinstance(submitted_amount, int) and not isinstance(submitted_amount, bool)
                    and submitted_amount > 0)
    return stored_amount == submitted_amount


def _existing_view(
    conn: sqlite3.Connection,
    tenant: str,
    batch_id: str,
    existing: sqlite3.Row,
    order_id: str,
    amount_cents: int,
) -> dict:
    """已落库行的本次响应：同负载幂等回放，不同负载记为标识冲突（仅响应、不落库）。"""
    line_no = existing["line_no"]
    if existing["order_id"] != order_id or not _same_amount(existing["amount_cents"], amount_cents):
        return _rejected_view(batch_id, line_no, order_id, REASON_CONFLICT)

    if existing["status"] == "accepted":
        latest = conn.execute(
            "SELECT amount_cents, paid_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, existing["order_id"]),
        ).fetchone()
        # 收款标识稳定回放；订单状态给当下最新值（可能已被后续收款/冲正改变）
        return _accepted_view(batch_id, line_no, existing["order_id"], existing["payment_id"], latest)
    return _rejected_view(batch_id, line_no, existing["order_id"], existing["reject_reason"])


def get_batch(tenant: str, batch_id: str) -> dict | None:
    """按批次号读取逐行受理结果（用于重复提交核对与后续查询）；批次不存在返回 None。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT line_no, order_id, amount_cents, status, payment_id, reject_reason "
            "FROM payment_imports WHERE tenant=? AND batch_id=? ORDER BY line_no",
            (tenant, batch_id),
        ).fetchall()
        if not rows:
            return None
        lines = []
        for row in rows:
            if row["status"] == "accepted":
                latest = conn.execute(
                    "SELECT amount_cents, paid_cents, status FROM orders WHERE tenant=? AND order_id=?",
                    (tenant, row["order_id"]),
                ).fetchone()
                lines.append(_accepted_view(batch_id, row["line_no"], row["order_id"], row["payment_id"], latest))
            else:
                lines.append(_rejected_view(batch_id, row["line_no"], row["order_id"], row["reject_reason"]))
    finally:
        conn.close()

    accepted = sum(1 for view in lines if view["status"] == "accepted")
    return {
        "tenant": tenant,
        "batch_id": batch_id,
        "total": len(lines),
        "accepted_count": accepted,
        "rejected_count": len(lines) - accepted,
        "lines": lines,
    }


def _accepted_view(
    batch_id: str, line_no: int, order_id: str, payment_id: str, order: sqlite3.Row | None
) -> dict:
    if order is None:
        # 理论上不会发生（无订单删除路径）；保持结构稳定而非抛出
        return {
            "batch_id": batch_id,
            "line_no": line_no,
            "order_id": order_id,
            "status": "accepted",
            "payment_id": payment_id,
            "order_status": None,
            "paid_cents": None,
            "outstanding_cents": None,
        }
    return {
        "batch_id": batch_id,
        "line_no": line_no,
        "order_id": order_id,
        "status": "accepted",
        "payment_id": payment_id,
        "order_status": order["status"],
        "paid_cents": order["paid_cents"],
        "outstanding_cents": order["amount_cents"] - order["paid_cents"],
    }


def _rejected_view(batch_id: str, line_no: int, order_id: str, reason: str) -> dict:
    return {
        "batch_id": batch_id,
        "line_no": line_no,
        "order_id": order_id,
        "status": "rejected",
        "reject_reason": reason,
        "reject_message": REASON_MESSAGES.get(reason, reason),
    }
