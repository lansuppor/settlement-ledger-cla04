import hashlib
import json
import sqlite3
import threading

from app.rules.order_rules import ALLOWED_CURRENCIES
from app.store.db import connect

# 每个批次最多一个后台工作线程；重复提交或重启续跑都通过 _ensure_worker 汇入同一线程。
_workers: dict[tuple[str, str], threading.Thread] = {}
_workers_lock = threading.Lock()

def hash_rows(rows: list) -> str:
    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

def _validate_row(raw) -> tuple[str | None, int | None, str | None, str | None]:
    # 逐行校验，规则与单笔受理一致：标识非空、金额为正整数、币种在允许集合内。
    if not isinstance(raw, dict):
        return None, None, None, "row must be an object"
    order_id = raw.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        return None, None, None, "order_id is required"
    amount = raw.get("amount_cents")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return None, None, None, "amount_cents must be a positive integer"
    currency = raw.get("currency")
    if not isinstance(currency, str) or currency not in ALLOWED_CURRENCIES:
        return None, None, None, "unsupported currency"
    return order_id, amount, currency, None

def submit(tenant: str, batch_id: str, rows: list) -> dict:
    # 幂等受理：同（租户, 批次标识）同内容直接返回与首次一致的回执；
    # 同标识不同内容按冲突拒绝。批次与行原文落库后才启动处理，提交不丢。
    digest = hash_rows(rows)
    payload = json.dumps(rows, ensure_ascii=False)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT request_hash FROM import_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO import_batches(tenant, batch_id, total_rows, status, request_hash, payload) VALUES(?,?,?,'processing',?,?)",
                (tenant, batch_id, len(rows), digest, payload),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()
    if existing is not None and existing["request_hash"] != digest:
        raise ValueError("batch_id already used with different rows")
    _ensure_worker(tenant, batch_id)
    return {"tenant": tenant, "batch_id": batch_id, "status": "accepted", "total_rows": len(rows)}

def get_batch(tenant: str, batch_id: str) -> dict | None:
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT tenant, batch_id, total_rows, status FROM import_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
        if batch is None:
            return None
        # 计数由逐行结果实时汇总，天然闭合：成功 + 失败 = 已处理行数。
        counts = {
            row["outcome"]: row["n"]
            for row in conn.execute(
                "SELECT outcome, COUNT(*) AS n FROM import_rows WHERE tenant=? AND batch_id=? GROUP BY outcome",
                (tenant, batch_id),
            )
        }
        failures = conn.execute(
            "SELECT row_no, order_id, error FROM import_rows WHERE tenant=? AND batch_id=? AND outcome='failed' ORDER BY row_no",
            (tenant, batch_id),
        ).fetchall()
    finally:
        conn.close()
    success = counts.get("success", 0)
    failure = counts.get("failed", 0)
    return {
        "tenant": batch["tenant"],
        "batch_id": batch["batch_id"],
        "status": batch["status"],
        "total_rows": batch["total_rows"],
        "processed_rows": success + failure,
        "success_count": success,
        "failure_count": failure,
        "failures": [
            {"row_no": row["row_no"], "order_id": row["order_id"], "error": row["error"]}
            for row in failures
        ],
    }

def resume_incomplete() -> None:
    # 服务重启后扫出未完成的批次继续处理；已落库的行会被跳过，不重复受理。
    conn = connect()
    try:
        pending = conn.execute(
            "SELECT tenant, batch_id FROM import_batches WHERE status='processing'"
        ).fetchall()
    finally:
        conn.close()
    for row in pending:
        _ensure_worker(row["tenant"], row["batch_id"])

def _ensure_worker(tenant: str, batch_id: str) -> None:
    key = (tenant, batch_id)
    with _workers_lock:
        thread = _workers.get(key)
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(target=_run, args=(tenant, batch_id), daemon=True)
        _workers[key] = thread
        thread.start()

def _run(tenant: str, batch_id: str) -> None:
    conn = connect()
    try:
        batch = conn.execute(
            "SELECT payload FROM import_batches WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        ).fetchone()
    finally:
        conn.close()
    if batch is None:
        return
    rows = json.loads(batch["payload"])
    for index, raw in enumerate(rows):
        _process_row(tenant, batch_id, index + 1, raw)
    conn = connect()
    try:
        conn.execute(
            "UPDATE import_batches SET status='completed' WHERE tenant=? AND batch_id=?",
            (tenant, batch_id),
        )
    finally:
        conn.close()

def _process_row(tenant: str, batch_id: str, row_no: int, raw) -> None:
    order_id, amount, currency, error = _validate_row(raw)
    # 校验失败的行也尽量保留原始订单标识，便于逐行定位。
    if order_id is None and isinstance(raw, dict) and isinstance(raw.get("order_id"), str):
        order_id = raw["order_id"]
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        done = conn.execute(
            "SELECT 1 FROM import_rows WHERE tenant=? AND batch_id=? AND row_no=?",
            (tenant, batch_id, row_no),
        ).fetchone()
        if done is not None:
            # 中断续跑：该行此前已原子落库，直接跳过。
            conn.execute("ROLLBACK")
            return
        if error is None:
            try:
                conn.execute(
                    "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES(?,?,?,0,?,'accepted')",
                    (tenant, order_id, amount, currency),
                )
                outcome, reason = "success", None
            except sqlite3.IntegrityError:
                # 与库内已有订单重复：只拒绝该行，不改动既有订单。
                outcome, reason = "failed", "order already accepted"
        else:
            outcome, reason = "failed", error
        conn.execute(
            "INSERT INTO import_rows(tenant, batch_id, row_no, order_id, outcome, error) VALUES(?,?,?,?,?,?)",
            (tenant, batch_id, row_no, order_id, outcome, reason),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
