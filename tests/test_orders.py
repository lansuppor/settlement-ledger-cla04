import os, tempfile
os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from concurrent.futures import ThreadPoolExecutor
from fastapi.testclient import TestClient
from app.entry import app
from app.store import orders
from app.store.db import migrate

migrate()
client = TestClient(app)

def test_accept_and_read_order() -> None:
    body = {"tenant": "t1", "order_id": "o1", "amount_cents": 500, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    got = client.get("/orders/o1", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["outstanding_cents"] == 500

def test_duplicate_is_refused() -> None:
    body = {"tenant": "t1", "order_id": "o2", "amount_cents": 100, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    assert client.post("/orders", json=body).status_code == 409

def test_cross_tenant_read_is_not_found() -> None:
    body = {"tenant": "t1", "order_id": "o3", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.get("/orders/o3", headers={"X-Tenant": "t2"}).status_code == 404

def test_payment_cannot_exceed_outstanding() -> None:
    body = {"tenant": "t1", "order_id": "o4", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.post("/orders/o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/o4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 409

def test_refund_returns_amounts_to_outstanding() -> None:
    body = {"tenant": "t1", "order_id": "o5", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o5/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"})
    got = client.post("/orders/o5/refunds", json={"refund_id": "r1", "amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert got.status_code == 200
    assert got.json() == {"order_id": "o5", "paid_cents": 300, "refunded_cents": 100, "outstanding_cents": 100}
    order = client.get("/orders/o5", headers={"X-Tenant": "t1"}).json()
    assert order["outstanding_cents"] == 100 and order["refunded_cents"] == 100
    # 退款后可以再次收款
    assert client.post("/orders/o5/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    order = client.get("/orders/o5", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 400 and order["refunded_cents"] == 100 and order["outstanding_cents"] == 0

def test_refund_replay_returns_first_result() -> None:
    body = {"tenant": "t1", "order_id": "o6", "amount_cents": 200, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o6/payments", json={"amount_cents": 200}, headers={"X-Tenant": "t1"})
    first = client.post("/orders/o6/refunds", json={"refund_id": "r2", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    assert first.status_code == 200
    client.post("/orders/o6/payments", json={"amount_cents": 50}, headers={"X-Tenant": "t1"})
    again = client.post("/orders/o6/refunds", json={"refund_id": "r2", "amount_cents": 50}, headers={"X-Tenant": "t1"})
    assert again.status_code == 200 and again.json() == first.json()
    order = client.get("/orders/o6", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 50  # 未重复扣减

def test_refund_id_reused_across_orders_is_refused() -> None:
    for oid in ("o7", "o8"):
        client.post("/orders", json={"tenant": "t1", "order_id": oid, "amount_cents": 100, "currency": "CNY"})
        client.post(f"/orders/{oid}/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/o7/refunds", json={"refund_id": "r3", "amount_cents": 10}, headers={"X-Tenant": "t1"}).status_code == 200
    got = client.post("/orders/o8/refunds", json={"refund_id": "r3", "amount_cents": 10}, headers={"X-Tenant": "t1"})
    assert got.status_code == 409 and got.json()["detail"] == "refund id already used"

def test_refund_cannot_exceed_refundable() -> None:
    body = {"tenant": "t1", "order_id": "o9", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o9/payments", json={"amount_cents": 60}, headers={"X-Tenant": "t1"})
    got = client.post("/orders/o9/refunds", json={"refund_id": "r4", "amount_cents": 61}, headers={"X-Tenant": "t1"})
    assert got.status_code == 409 and got.json()["detail"] == "refund exceeds refundable amount"
    order = client.get("/orders/o9", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 60 and order["refunded_cents"] == 0 and order["outstanding_cents"] == 40

def test_refund_cross_tenant_is_not_found() -> None:
    body = {"tenant": "t1", "order_id": "o10", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    client.post("/orders/o10/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    got = client.post("/orders/o10/refunds", json={"refund_id": "r5", "amount_cents": 10}, headers={"X-Tenant": "t2"})
    assert got.status_code == 404

def test_concurrent_refunds_only_one_wins() -> None:
    orders.insert("t1", "o11", 100, "CNY")
    orders.add_payment("t1", "o11", 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda rid: _try_refund("t1", "o11", rid, 60), ["ra", "rb"]))
    assert sorted(r[0] for r in results) == ["conflict", "ok"]
    order = orders.get("t1", "o11")
    assert order["refunded_cents"] == 60
    assert order["amount_cents"] == order["paid_cents"] - order["refunded_cents"] + order["outstanding_cents"]

def _try_refund(tenant: str, order_id: str, refund_id: str, amount_cents: int) -> tuple:
    try:
        orders.add_refund(tenant, order_id, refund_id, amount_cents)
        return ("ok",)
    except ValueError:
        return ("conflict",)
