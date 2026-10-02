import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test-batches.sqlite"))
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.entry import app
from app.store import batches
from app.store.db import connect, migrate
from app.usecase import batch_import

migrate()
client = TestClient(app)


def wait_batch(tenant: str, batch_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get(f"/orders/batches/{batch_id}", headers={"X-Tenant": tenant}).json()
        if view["status"] == "completed":
            return view
        time.sleep(0.02)
    raise AssertionError(f"batch {batch_id} did not complete in time")


def test_batch_import_partial_success_and_closed_counts() -> None:
    payload = {
        "tenant": "bt1",
        "batch_id": "b-1",
        "rows": [
            {"order_id": "bo-1", "amount_cents": 100, "currency": "CNY"},
            {"order_id": "bo-2", "amount_cents": -5, "currency": "CNY"},      # 金额非正整数
            {"order_id": "bo-3", "amount_cents": 300, "currency": "GBP"},     # 币种不允许
            {"order_id": "bo-1", "amount_cents": 100, "currency": "CNY"},     # 批内重复
            {"order_id": "bo-4", "amount_cents": 400, "currency": "USD"},
        ],
    }
    res = client.post("/orders/batches", json=payload)
    assert res.status_code == 202
    assert res.json() == {"tenant": "bt1", "batch_id": "b-1", "status": "accepted", "total_rows": 5}
    view = wait_batch("bt1", "b-1")
    assert view["succeeded_rows"] == 2 and view["failed_rows"] == 3
    assert view["succeeded_rows"] + view["failed_rows"] == view["total_rows"] == 5
    failures = {f["line_no"]: f["error"] for f in view["failures"]}
    assert failures == {2: "amount_cents must be a positive integer", 3: "unsupported currency", 4: "duplicate order"}
    assert client.get("/orders/bo-1", headers={"X-Tenant": "bt1"}).json()["amount_cents"] == 100
    assert client.get("/orders/bo-4", headers={"X-Tenant": "bt1"}).json()["amount_cents"] == 400


def test_batch_row_does_not_modify_existing_order() -> None:
    client.post("/orders", json={"tenant": "bt2", "order_id": "keep-1", "amount_cents": 700, "currency": "CNY"})
    payload = {
        "tenant": "bt2",
        "batch_id": "b-2",
        "rows": [{"order_id": "keep-1", "amount_cents": 1, "currency": "USD"}],
    }
    assert client.post("/orders/batches", json=payload).status_code == 202
    view = wait_batch("bt2", "b-2")
    assert view["failed_rows"] == 1 and view["failures"][0]["error"] == "duplicate order"
    order = client.get("/orders/keep-1", headers={"X-Tenant": "bt2"}).json()
    assert order["amount_cents"] == 700 and order["currency"] == "CNY"


def test_batch_resubmit_returns_identical_result_without_reprocessing() -> None:
    payload = {
        "tenant": "bt3",
        "batch_id": "b-3",
        "rows": [{"order_id": "bo-9", "amount_cents": 100, "currency": "CNY"}],
    }
    first = client.post("/orders/batches", json=payload)
    wait_batch("bt3", "b-3")
    second = client.post("/orders/batches", json=payload)
    assert second.status_code == 202 and second.json() == first.json()
    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM import_batch_rows WHERE tenant='bt3' AND batch_id='b-3' AND state='imported'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert count == 1
    changed = dict(payload)
    changed["rows"] = [{"order_id": "bo-9", "amount_cents": 200, "currency": "CNY"}]
    assert client.post("/orders/batches", json=changed).status_code == 409


def test_batch_resume_after_interruption_keeps_counts_closed() -> None:
    rows = [
        {"order_id": f"rs-{i}", "amount_cents": 100 + i, "currency": "CNY"}
        for i in range(5)
    ]
    digest = batch_import.fingerprint(rows)
    assert batches.create_batch("bt4", "b-4", rows, digest)
    # 模拟服务中断：只处理前两行后“进程退出”（不启动工作器）。
    batches.process_pending("bt4", "b-4", max_rows=2)
    mid = batches.get_batch("bt4", "b-4")
    assert mid["status"] == "processing" and mid["succeeded_rows"] == 2
    # 重启后续跑同一批次标识：受理回执与首次一致，剩余行继续处理。
    res = client.post("/orders/batches", json={"tenant": "bt4", "batch_id": "b-4", "rows": rows})
    assert res.status_code == 202
    assert res.json() == {"tenant": "bt4", "batch_id": "b-4", "status": "accepted", "total_rows": 5}
    view = wait_batch("bt4", "b-4")
    assert view["succeeded_rows"] == 5 and view["failed_rows"] == 0
    assert view["succeeded_rows"] + view["failed_rows"] == view["total_rows"]
    for i in range(5):
        order = client.get(f"/orders/rs-{i}", headers={"X-Tenant": "bt4"})
        assert order.status_code == 200 and order.json()["amount_cents"] == 100 + i


def test_concurrent_submit_of_same_batch_is_processed_once() -> None:
    payload = {
        "tenant": "bt5",
        "batch_id": "b-5",
        "rows": [{"order_id": "cc-1", "amount_cents": 100, "currency": "CNY"}],
    }
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: client.post("/orders/batches", json=payload), range(2)))
    assert {r.status_code for r in results} == {202}
    assert results[0].json() == results[1].json()
    view = wait_batch("bt5", "b-5")
    assert view["succeeded_rows"] == 1 and view["failed_rows"] == 0


def test_batch_read_is_tenant_scoped() -> None:
    payload = {"tenant": "bt6", "batch_id": "b-6", "rows": []}
    assert client.post("/orders/batches", json=payload).status_code == 202
    wait_batch("bt6", "b-6")
    assert client.get("/orders/batches/b-6", headers={"X-Tenant": "other"}).status_code == 404
    assert client.get("/orders/batches/b-6").status_code == 400
