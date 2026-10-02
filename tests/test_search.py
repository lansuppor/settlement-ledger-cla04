import os
import tempfile
import time

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.rules import cursor as cursors
from app.store.db import migrate

migrate()
client = TestClient(app)

def _mk(tenant: str, order_id: str, amount: int, paid: int = 0) -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"})
    if paid:
        client.post(f"/orders/{order_id}/payments", json={"amount_cents": paid}, headers={"X-Tenant": tenant})

def _seed() -> None:
    _mk("s1", "s-a1", 1000, paid=1000)   # settled
    _mk("s1", "s-a2", 500, paid=100)     # accepted, paid 100
    _mk("s1", "s-b1", 300)               # accepted, unpaid
    _mk("s2", "s-a9", 700, paid=700)     # 同前缀但属于另一租户

_seed()

def test_search_filters_combine_within_tenant() -> None:
    res = client.get(
        "/orders",
        params={"order_id_prefix": "s-a", "status": "accepted", "min_paid_cents": 50},
        headers={"X-Tenant": "s1"},
    )
    assert res.status_code == 200
    ids = [o["order_id"] for o in res.json()["orders"]]
    assert ids == ["s-a2"]
    assert res.json()["next_cursor"] is None

def test_search_never_leaks_across_tenants() -> None:
    res = client.get("/orders", params={"order_id_prefix": "s-a"}, headers={"X-Tenant": "s2"})
    assert [o["order_id"] for o in res.json()["orders"]] == ["s-a9"]
    res = client.get("/orders", headers={"X-Tenant": "s2"})
    assert all(o["tenant"] == "s2" for o in res.json()["orders"])

def test_search_orders_ascending_by_order_id() -> None:
    res = client.get("/orders", headers={"X-Tenant": "s1"})
    ids = [o["order_id"] for o in res.json()["orders"]]
    assert ids == sorted(ids)

def test_search_pagination_is_stable_without_dup_or_gap() -> None:
    for i in range(5):
        _mk("s3", f"sp-{i}", 100)
    seen: list[str] = []
    cursor = None
    while True:
        params = {"order_id_prefix": "sp-", "page_size": 2}
        if cursor:
            params["cursor"] = cursor
        res = client.get("/orders", params=params, headers={"X-Tenant": "s3"})
        assert res.status_code == 200
        body = res.json()
        seen.extend(o["order_id"] for o in body["orders"])
        assert len(body["orders"]) <= 2
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"sp-{i}" for i in range(5)]

def test_search_page_size_over_limit_is_clamped() -> None:
    res = client.get("/orders", params={"order_id_prefix": "sp-", "page_size": 100000}, headers={"X-Tenant": "s3"})
    assert res.status_code == 200
    assert len(res.json()["orders"]) == 5
    assert res.json()["next_cursor"] is None

def _first_page_cursor(tenant: str, prefix: str) -> str:
    res = client.get("/orders", params={"order_id_prefix": prefix, "page_size": 1}, headers={"X-Tenant": tenant})
    assert res.status_code == 200
    cursor = res.json()["next_cursor"]
    assert cursor is not None
    return cursor

def test_cursor_cross_tenant_is_rejected_with_distinct_reason() -> None:
    cursor = _first_page_cursor("s3", "sp-")
    res = client.get("/orders", params={"order_id_prefix": "sp-", "cursor": cursor}, headers={"X-Tenant": "s4"})
    assert res.status_code == 400 and res.json()["detail"] == "cursor_tenant_mismatch"

def test_cursor_with_changed_filters_is_rejected() -> None:
    cursor = _first_page_cursor("s3", "sp-")
    res = client.get(
        "/orders",
        params={"order_id_prefix": "sp-", "min_paid_cents": 1, "cursor": cursor},
        headers={"X-Tenant": "s3"},
    )
    assert res.status_code == 400 and res.json()["detail"] == "cursor_filter_mismatch"

def test_cursor_malformed_is_rejected() -> None:
    res = client.get("/orders", params={"cursor": "not-a-cursor"}, headers={"X-Tenant": "s3"})
    assert res.status_code == 400 and res.json()["detail"] == "cursor_malformed"

def test_cursor_expired_is_rejected_with_distinct_reason() -> None:
    stale = cursors.encode(
        {
            "v": 1,
            "tenant": "s3",
            "filters": {"order_id_prefix": "sp-", "status": None, "min_paid_cents": None},
            "last": "sp-0",
            "iat": time.time() - 7200,
        }
    )
    res = client.get("/orders", params={"order_id_prefix": "sp-", "cursor": stale}, headers={"X-Tenant": "s3"})
    assert res.status_code == 400 and res.json()["detail"] == "cursor_expired"

def test_search_requires_tenant_header() -> None:
    assert client.get("/orders").status_code == 400
