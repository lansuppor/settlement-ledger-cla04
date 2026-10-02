import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders
from app.store.db import connect, migrate

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

def test_refund_returns_ledger_and_keeps_identity() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r1", "amount_cents": 1000, "currency": "CNY"})
    client.post("/orders/r1/payments", json={"amount_cents": 600}, headers={"X-Tenant": "t1"})
    res = client.post("/orders/r1/refunds", json={"refund_id": "rf-1", "amount_cents": 200}, headers={"X-Tenant": "t1"})
    assert res.status_code == 200
    assert res.json() == {"paid_cents": 600, "refunded_cents": 200, "outstanding_cents": 600}
    order = client.get("/orders/r1", headers={"X-Tenant": "t1"}).json()
    assert order["amount_cents"] == order["paid_cents"] - order["refunded_cents"] + order["outstanding_cents"]

def test_refund_cannot_exceed_refundable_net() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r2", "amount_cents": 500, "currency": "CNY"})
    client.post("/orders/r2/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"})
    res = client.post("/orders/r2/refunds", json={"refund_id": "rf-2", "amount_cents": 200}, headers={"X-Tenant": "t1"})
    assert res.status_code == 409
    order = client.get("/orders/r2", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 100 and order["refunded_cents"] == 0 and order["outstanding_cents"] == 400

def test_refund_replay_returns_first_result() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r3", "amount_cents": 1000, "currency": "CNY"})
    client.post("/orders/r3/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    payload = {"refund_id": "rf-3", "amount_cents": 200}
    first = client.post("/orders/r3/refunds", json=payload, headers={"X-Tenant": "t1"})
    second = client.post("/orders/r3/refunds", json=payload, headers={"X-Tenant": "t1"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"paid_cents": 500, "refunded_cents": 200, "outstanding_cents": 700}
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM refunds WHERE tenant='t1' AND refund_id='rf-3'").fetchone()["n"]
    finally:
        conn.close()
    assert count == 1

def test_refund_replay_with_other_amount_is_rejected() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r3b", "amount_cents": 1000, "currency": "CNY"})
    client.post("/orders/r3b/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r3b/refunds", json={"refund_id": "rf-3b", "amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/r3b/refunds", json={"refund_id": "rf-3b", "amount_cents": 200}, headers={"X-Tenant": "t1"}).status_code == 409

def test_refund_id_reused_on_another_order_is_rejected() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r4a", "amount_cents": 1000, "currency": "CNY"})
    client.post("/orders", json={"tenant": "t1", "order_id": "r4b", "amount_cents": 1000, "currency": "CNY"})
    client.post("/orders/r4a/payments", json={"amount_cents": 1000}, headers={"X-Tenant": "t1"})
    client.post("/orders/r4b/payments", json={"amount_cents": 1000}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r4a/refunds", json={"refund_id": "rf-4", "amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/r4b/refunds", json={"refund_id": "rf-4", "amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 409
    order = client.get("/orders/r4b", headers={"X-Tenant": "t1"}).json()
    assert order["refunded_cents"] == 0

def test_cross_tenant_refund_is_not_found() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r5", "amount_cents": 1000, "currency": "CNY"})
    client.post("/orders/r5/payments", json={"amount_cents": 1000}, headers={"X-Tenant": "t1"})
    res = client.post("/orders/r5/refunds", json={"refund_id": "rf-5", "amount_cents": 100}, headers={"X-Tenant": "t2"})
    assert res.status_code == 404

def test_refund_then_repay_settles_again() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r6", "amount_cents": 1000, "currency": "CNY"})
    client.post("/orders/r6/payments", json={"amount_cents": 1000}, headers={"X-Tenant": "t1"})
    assert client.post("/orders/r6/refunds", json={"refund_id": "rf-6", "amount_cents": 300}, headers={"X-Tenant": "t1"}).json()["outstanding_cents"] == 300
    assert client.post("/orders/r6/payments", json={"amount_cents": 400}, headers={"X-Tenant": "t1"}).status_code == 409
    assert client.post("/orders/r6/payments", json={"amount_cents": 300}, headers={"X-Tenant": "t1"}).status_code == 200
    order = client.get("/orders/r6", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 1300 and order["refunded_cents"] == 300 and order["outstanding_cents"] == 0
    assert order["status"] == "settled"

def test_concurrent_refunds_compete_for_one_refundable_space() -> None:
    client.post("/orders", json={"tenant": "t1", "order_id": "r7", "amount_cents": 1000, "currency": "CNY"})
    client.post("/orders/r7/payments", json={"amount_cents": 1000}, headers={"X-Tenant": "t1"})

    def attempt(rid: str) -> str:
        try:
            result = orders.add_refund("t1", "r7", rid, 600)
        except ValueError:
            return "rejected"
        return "accepted" if result is not None else "not_found"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["rf-7a", "rf-7b"]))
    assert outcomes.count("accepted") == 1 and outcomes.count("rejected") == 1
    order = client.get("/orders/r7", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 1000 and order["refunded_cents"] == 600 and order["outstanding_cents"] == 600
    assert order["amount_cents"] == order["paid_cents"] - order["refunded_cents"] + order["outstanding_cents"]

def test_legacy_database_upgrade_keeps_amount_identity() -> None:
    legacy = os.path.join(tempfile.mkdtemp(), "legacy.sqlite")
    previous = os.environ["APP_DB"]
    os.environ["APP_DB"] = legacy
    try:
        import sqlite3
        schema_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations", "001_init.sql")
        conn = sqlite3.connect(legacy)
        with open(schema_path, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute("INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status) VALUES('t1','old',700,300,'CNY','accepted')")
        conn.commit()
        conn.close()
        migrate()
        order = orders.get("t1", "old")
        assert order is not None
        assert order["refunded_cents"] == 0 and order["outstanding_cents"] == 400
        assert order["amount_cents"] == order["paid_cents"] - order["refunded_cents"] + order["outstanding_cents"]
    finally:
        os.environ["APP_DB"] = previous
