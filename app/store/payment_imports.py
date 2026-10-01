"""批量导入收款的逐行受理。

每一行在一个独立的写事务内完成「校验 + 收款落明细 + 订单余额更新 + 日记账写入」：
行要么整体生效要么整体回滚，不存在半行生效。已落库的行即中断点，重复提交或中断
续跑时按（租户, 批次号, 行内序号）命中日记账并原样返回，不再重复登记或扣减。
"""
import json
import sqlite3
from uuid import uuid4

from app.store.db import connect

# 拒绝原因码：业务拒绝与内部错误严格区分，内部错误直接抛出而非落到行结果
ORDER_NOT_FOUND = "order_not_found"
INVALID_AMOUNT = "invalid_amount"
EXCEEDS_OUTSTANDING = "exceeds_outstanding"
DUPLICATE_ORDER_IN_BATCH = "duplicate_order_in_batch"
DUPLICATE_LINE_SEQ = "duplicate_line_seq"
IDENTIFIER_CONFLICT = "identifier_conflict"

ACCEPTED = "accepted"
REJECTED = "rejected"


class ImportLine:
    def __init__(self, line_no: int, line_seq: int, order_id: str, amount_cents: object) -> None:
        self.line_no = line_no
        self.line_seq = line_seq
        self.order_id = order_id
        self.amount_cents = amount_cents


def _valid_amount(value: object) -> bool:
    # 金额必须是正整数（最小货币单位）；bool 是 int 的子类型，显式排除
    return type(value) is int and value > 0


def _amount_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def submit(tenant: str, batch_id: str, lines: list[ImportLine]) -> dict:
    """按提交顺序逐行受理，返回整批可解释结果。任一行内部错误时该行整体回滚并向上抛出。"""
    conn = connect()
    results: list[dict] = []
    seen_seqs: set[int] = set()
    try:
        for line in lines:
            if line.line_seq in seen_seqs:
                # 同一次提交内行内序号重复：无法按主键落日记账，按提交顺序确定性拒绝
                results.append(_reject_view(line, DUPLICATE_LINE_SEQ))
                continue
            seen_seqs.add(line.line_seq)
            results.append(_process_line(conn, tenant, batch_id, line))
    finally:
        conn.close()
    return _batch_view(tenant, batch_id, results)


def get_batch(tenant: str, batch_id: str) -> dict | None:
    """按批次号读取已落库的逐行结果（中断续跑时可见已受理的部分）；不存在返回 None。"""
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM payment_import_lines WHERE tenant=? AND batch_id=? ORDER BY rowid",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        return None
    return _batch_view(tenant, batch_id, [_journal_view(row) for row in rows])


def _process_line(conn: sqlite3.Connection, tenant: str, batch_id: str, line: ImportLine) -> dict:
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM payment_import_lines WHERE tenant=? AND batch_id=? AND line_seq=?",
            (tenant, batch_id, line.line_seq),
        ).fetchone()
        if existing is not None:
            # 同一标识：订单与金额一致则原样重放（成功行不重复登记、拒绝行不重复处理）；
            # 订单或金额不同则冲突拒绝，既有日记账与订单状态保持不变
            conn.execute("ROLLBACK")
            same_order = existing["order_id"] == line.order_id
            same_amount = existing["amount_json"] == _amount_json(line.amount_cents)
            if same_order and same_amount:
                return _journal_view(existing)
            return _reject_view(line, IDENTIFIER_CONFLICT)

        # 批内同一订单的后一条记录记为重复被拒。日记账只含更早顺序处理且已提交的行：
        # 同一次提交内前一行必然已提交；中断续跑时日记账只含此前已落库的前缀。
        # 当前行自身若已在日记账中，会在上方的重放分支直接返回，不会走到这里
        dup = conn.execute(
            "SELECT 1 FROM payment_import_lines WHERE tenant=? AND batch_id=? AND order_id=?",
            (tenant, batch_id, line.order_id),
        ).fetchone()
        if dup is not None:
            return _journal_reject(conn, tenant, batch_id, line, DUPLICATE_ORDER_IN_BATCH)

        if not _valid_amount(line.amount_cents):
            return _journal_reject(conn, tenant, batch_id, line, INVALID_AMOUNT)

        order = conn.execute(
            "SELECT amount_cents, paid_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, line.order_id),
        ).fetchone()
        # 订单不存在或跨租户点名统一按不存在处理，不泄漏对象是否存在
        if order is None:
            return _journal_reject(conn, tenant, batch_id, line, ORDER_NOT_FOUND)

        if order["paid_cents"] + line.amount_cents > order["amount_cents"]:
            return _journal_reject(conn, tenant, batch_id, line, EXCEEDS_OUTSTANDING)

        payment_id = uuid4().hex
        conn.execute(
            "INSERT INTO payments(tenant, order_id, payment_id, amount_cents) VALUES(?,?,?,?)",
            (tenant, line.order_id, payment_id, line.amount_cents),
        )
        conn.execute(
            "UPDATE orders SET paid_cents = paid_cents + ?, "
            "status = CASE WHEN paid_cents + ? >= amount_cents THEN 'settled' ELSE 'accepted' END "
            "WHERE tenant=? AND order_id=?",
            (line.amount_cents, line.amount_cents, tenant, line.order_id),
        )
        latest = conn.execute(
            "SELECT paid_cents, status FROM orders WHERE tenant=? AND order_id=?",
            (tenant, line.order_id),
        ).fetchone()
        conn.execute(
            "INSERT INTO payment_import_lines(tenant, batch_id, line_seq, order_id, amount_json, line_no, "
            "status, reject_reason, payment_id, order_status, paid_cents, outstanding_cents) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                tenant,
                batch_id,
                line.line_seq,
                line.order_id,
                _amount_json(line.amount_cents),
                line.line_no,
                ACCEPTED,
                None,
                payment_id,
                latest["status"],
                latest["paid_cents"],
                order["amount_cents"] - latest["paid_cents"],
            ),
        )
        conn.execute("COMMIT")
    except BaseException:
        # 任何内部错误：本行整笔回滚，不允许半行生效；错误向上抛出由调用方返回 5xx
        conn.execute("ROLLBACK")
        raise

    row = conn.execute(
        "SELECT * FROM payment_import_lines WHERE tenant=? AND batch_id=? AND line_seq=?",
        (tenant, batch_id, line.line_seq),
    ).fetchone()
    return _journal_view(row)


def _journal_reject(
    conn: sqlite3.Connection, tenant: str, batch_id: str, line: ImportLine, reason: str
) -> dict:
    conn.execute(
        "INSERT INTO payment_import_lines(tenant, batch_id, line_seq, order_id, amount_json, line_no, "
        "status, reject_reason, payment_id, order_status, paid_cents, outstanding_cents) "
        "VALUES(?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL)",
        (
            tenant,
            batch_id,
            line.line_seq,
            line.order_id,
            _amount_json(line.amount_cents),
            line.line_no,
            REJECTED,
            reason,
        ),
    )
    conn.execute("COMMIT")
    return _reject_view(line, reason)


def _reject_view(line: ImportLine, reason: str) -> dict:
    return {
        "line_no": line.line_no,
        "line_seq": line.line_seq,
        "order_id": line.order_id,
        "amount_cents": line.amount_cents,
        "result": REJECTED,
        "reject_reason": reason,
    }


def _journal_view(row: sqlite3.Row) -> dict:
    view = {
        "line_no": row["line_no"],
        "line_seq": row["line_seq"],
        "order_id": row["order_id"],
        "amount_cents": json.loads(row["amount_json"]),
        "result": row["status"],
    }
    if row["status"] == ACCEPTED:
        view.update(
            payment_id=row["payment_id"],
            order_status=row["order_status"],
            paid_cents=row["paid_cents"],
            outstanding_cents=row["outstanding_cents"],
        )
    else:
        view["reject_reason"] = row["reject_reason"]
    return view


def _batch_view(tenant: str, batch_id: str, results: list[dict]) -> dict:
    accepted = sum(1 for r in results if r["result"] == ACCEPTED)
    return {
        "tenant": tenant,
        "batch_id": batch_id,
        "total": len(results),
        "accepted": accepted,
        "rejected": len(results) - accepted,
        "results": results,
    }
