import hashlib
import json
import threading

from app.store import batches

# 单进程内的在途批次工作器登记表，避免同一批次被并发重复处理。
_active: set[tuple[str, str]] = set()
_lock = threading.Lock()


def fingerprint(rows: list[dict]) -> str:
    """提交内容指纹：同一批次标识重复提交时据此判定是否为同一批数据。"""
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def submit(tenant: str, batch_id: str, rows: list[dict]) -> dict:
    """异步受理批次：落库后立即返回受理回执；重复提交同一批次标识返回与首次完全一致的结果。"""
    digest = fingerprint(rows)
    created = batches.create_batch(tenant, batch_id, rows, digest)
    if not created:
        existing = batches.get_batch(tenant, batch_id)
        if existing is None or existing["payload_hash"] != digest:
            raise ValueError("batch_id already used with a different payload")
    ensure_worker(tenant, batch_id)
    return {"tenant": tenant, "batch_id": batch_id, "status": "accepted", "total_rows": len(rows)}


def ensure_worker(tenant: str, batch_id: str) -> None:
    """为批次拉起后台工作器；已在途则直接返回（提交、查询、重启恢复均经由此处自愈续跑）。"""
    key = (tenant, batch_id)
    with _lock:
        if key in _active:
            return
        _active.add(key)
    threading.Thread(target=_run, args=key, daemon=True).start()


def _run(tenant: str, batch_id: str) -> None:
    try:
        batches.process_pending(tenant, batch_id)
    finally:
        with _lock:
            _active.discard((tenant, batch_id))


def resume_interrupted() -> None:
    """服务启动时恢复所有未完成批次：已生效行不重复受理，未完成的行继续处理。"""
    for tenant, batch_id in batches.list_processing():
        ensure_worker(tenant, batch_id)
