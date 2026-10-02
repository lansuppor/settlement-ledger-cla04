import json
import os
import tempfile
import time

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.entry import app
from app.store import imports, orders
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

def _wait_batch(tenant: str, batch_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        res = client.get(f"/orders/import/{batch_id}", headers={"X-Tenant": tenant})
        assert res.status_code == 200
        body = res.json()
        if body["status"] == "completed":
            return body
        time.sleep(0.05)
    raise AssertionError(f"batch {batch_id} did not complete in time")

def test_import_partial_success_and_counts_close() -> None:
    client.post("/orders", json={"tenant": "imp", "order_id": "imp-dup", "amount_cents": 100, "currency": "CNY"})
    rows = [
        {"order_id": "imp-a", "amount_cents": 100, "currency": "CNY"},
        {"order_id": "imp-b", "amount_cents": 0, "currency": "CNY"},
        {"order_id": "imp-c", "amount_cents": 50, "currency": "GBP"},
        {"order_id": "imp-dup", "amount_cents": 100, "currency": "CNY"},
        {"order_id": "imp-e", "amount_cents": 200, "currency": "USD"},
    ]
    res = client.post("/orders/import", json={"tenant": "imp", "batch_id": "b1", "rows": rows})
    assert res.status_code == 202
    result = _wait_batch("imp", "b1")
    assert result["total_rows"] == 5
    assert result["success_count"] == 2 and result["failure_count"] == 3
    assert result["success_count"] + result["failure_count"] == result["total_rows"]
    errors = {f["row_no"]: f["error"] for f in result["failures"]}
    assert set(errors) == {2, 3, 4}
    assert errors[2] == "amount_cents must be a positive integer"
    assert errors[3] == "unsupported currency"
    assert errors[4] == "order already accepted"
    # 被重复拒绝的既有订单不被修改；成功行已生效。
    dup = client.get("/orders/imp-dup", headers={"X-Tenant": "imp"}).json()
    assert dup["amount_cents"] == 100 and dup["paid_cents"] == 0
    assert client.get("/orders/imp-a", headers={"X-Tenant": "imp"}).status_code == 200
    assert client.get("/orders/imp-e", headers={"X-Tenant": "imp"}).status_code == 200

def test_import_replay_same_batch_returns_same_receipt() -> None:
    rows = [{"order_id": "imp-r1", "amount_cents": 10, "currency": "EUR"}]
    first = client.post("/orders/import", json={"tenant": "imp", "batch_id": "b2", "rows": rows})
    assert first.status_code == 202
    _wait_batch("imp", "b2")
    second = client.post("/orders/import", json={"tenant": "imp", "batch_id": "b2", "rows": rows})
    assert second.status_code == 202
    assert first.json() == second.json()
    result = _wait_batch("imp", "b2")
    assert result["success_count"] == 1 and result["failure_count"] == 0
    order = client.get("/orders/imp-r1", headers={"X-Tenant": "imp"}).json()
    assert order["amount_cents"] == 10

def test_import_same_batch_id_with_different_rows_is_rejected() -> None:
    rows = [{"order_id": "imp-x1", "amount_cents": 10, "currency": "CNY"}]
    assert client.post("/orders/import", json={"tenant": "imp", "batch_id": "b3", "rows": rows}).status_code == 202
    other = [{"order_id": "imp-x2", "amount_cents": 20, "currency": "CNY"}]
    assert client.post("/orders/import", json={"tenant": "imp", "batch_id": "b3", "rows": other}).status_code == 409

def test_import_status_cross_tenant_is_not_found() -> None:
    assert client.get("/orders/import/b1", headers={"X-Tenant": "imp-other"}).status_code == 404
    assert client.get("/orders/import/b1").status_code == 400

def test_import_concurrent_submit_same_batch_processes_once() -> None:
    rows = [{"order_id": f"imp-cc-{i}", "amount_cents": 5, "currency": "CNY"} for i in range(4)]
    payload = {"tenant": "imp", "batch_id": "b4", "rows": rows}
    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(lambda _: client.post("/orders/import", json=payload).json(), range(3)))
    assert all(receipt == receipts[0] for receipt in receipts)
    result = _wait_batch("imp", "b4")
    assert result["success_count"] == 4 and result["failure_count"] == 0
    assert result["success_count"] + result["failure_count"] == result["total_rows"]

def test_import_resume_after_interruption_keeps_counts_closed() -> None:
    # 模拟服务中断：批次仍为 processing，第 1 行已生效落库，其余行未处理。
    payload = [
        {"order_id": "imp-c1", "amount_cents": 100, "currency": "CNY"},
        {"order_id": "imp-c2", "amount_cents": 200, "currency": "CNY"},
        {"order_id": "imp-c3", "amount_cents": 300, "currency": "CNY"},
    ]
    orders.insert("imp", "imp-c1", 100, "CNY")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO import_batches(tenant, batch_id, total_rows, status, request_hash, payload) VALUES(?,?,?,'processing',?,?)",
            ("imp", "b-crash", 3, imports.hash_rows(payload), json.dumps(payload)),
        )
        conn.execute(
            "INSERT INTO import_rows(tenant, batch_id, row_no, order_id, outcome, error) VALUES('imp','b-crash',1,'imp-c1','success',NULL)"
        )
    finally:
        conn.close()
    imports.resume_incomplete()
    result = _wait_batch("imp", "b-crash")
    assert result["status"] == "completed"
    assert result["success_count"] == 3 and result["failure_count"] == 0
    assert result["success_count"] + result["failure_count"] == result["total_rows"]
    # 已生效的行未重复受理、未被改写。
    order = client.get("/orders/imp-c1", headers={"X-Tenant": "imp"}).json()
    assert order["amount_cents"] == 100 and order["paid_cents"] == 0
    # 重启后续跑同一批次标识：重复提交也只返回同一回执，不重复受理。
    again = client.post("/orders/import", json={"tenant": "imp", "batch_id": "b-crash", "rows": payload})
    assert again.status_code == 202
    assert _wait_batch("imp", "b-crash")["success_count"] == 3
