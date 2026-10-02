import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test-search.sqlite"))
import time

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate
from app.usecase import cursor as cursor_mod

migrate()
client = TestClient(app)


def seed() -> None:
    orders = [
        ("st1", "a-001", 500, "CNY"),
        ("st1", "a-002", 700, "CNY"),
        ("st1", "a-003", 900, "CNY"),
        ("st1", "b-001", 100, "CNY"),
        ("st2", "a-001", 500, "CNY"),  # 同标识不同租户，检索不得越界
    ]
    for tenant, order_id, amount, currency in orders:
        client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": currency})
    # st1/a-001 收 500 结清；st1/a-002 收 300 仍待收；st1/a-003 未收
    client.post("/orders/a-001/payments", json={"amount_cents": 500}, headers={"X-Tenant": "st1"})
    client.post("/orders/a-002/payments", json={"amount_cents": 300}, headers={"X-Tenant": "st1"})


seed()


def test_search_filters_combine_and_stay_in_tenant() -> None:
    res = client.get("/orders", params={"prefix": "a-"}, headers={"X-Tenant": "st1"})
    assert res.status_code == 200
    ids = [item["order_id"] for item in res.json()["items"]]
    assert ids == ["a-001", "a-002", "a-003"]  # 升序，且不含 st2 的 a-001
    res = client.get(
        "/orders",
        params={"prefix": "a-", "status": "accepted", "min_paid_cents": 300},
        headers={"X-Tenant": "st1"},
    )
    ids = [item["order_id"] for item in res.json()["items"]]
    assert ids == ["a-002"]


def test_search_pagination_is_stable_and_complete() -> None:
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params = {"page_size": 2}
        if cursor:
            params["cursor"] = cursor
        res = client.get("/orders", params=params, headers={"X-Tenant": "st1"})
        assert res.status_code == 200
        body = res.json()
        assert body["page_size"] == 2
        seen += [item["order_id"] for item in body["items"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert seen == ["a-001", "a-002", "a-003", "b-001"]  # 不重不漏


def test_search_page_size_is_capped() -> None:
    res = client.get("/orders", params={"page_size": 100000}, headers={"X-Tenant": "st1"})
    assert res.status_code == 200 and res.json()["page_size"] == 200
    assert client.get("/orders", params={"page_size": 0}, headers={"X-Tenant": "st1"}).status_code == 400


def test_search_requires_tenant_and_valid_filters() -> None:
    assert client.get("/orders").status_code == 400
    assert client.get("/orders", params={"status": "bogus"}, headers={"X-Tenant": "st1"}).status_code == 400
    assert client.get("/orders", params={"min_paid_cents": -1}, headers={"X-Tenant": "st1"}).status_code == 400


def first_page_cursor(tenant: str, **params) -> str:
    params["page_size"] = 1
    body = client.get("/orders", params=params, headers={"X-Tenant": tenant}).json()
    assert body["next_cursor"]
    return body["next_cursor"]


def test_cursor_rejected_across_tenant() -> None:
    cursor = first_page_cursor("st1")
    res = client.get("/orders", params={"page_size": 1, "cursor": cursor}, headers={"X-Tenant": "st2"})
    assert res.status_code == 400 and res.json()["detail"] == "cursor tenant mismatch"


def test_cursor_rejected_when_filters_change() -> None:
    cursor = first_page_cursor("st1")
    res = client.get(
        "/orders",
        params={"page_size": 1, "prefix": "a-", "cursor": cursor},
        headers={"X-Tenant": "st1"},
    )
    assert res.status_code == 400 and res.json()["detail"] == "cursor filters mismatch"


def test_cursor_tampered_or_malformed_is_invalid() -> None:
    cursor = first_page_cursor("st1")
    tampered = cursor[:-2] + ("AA" if not cursor.endswith("AA") else "BB")
    res = client.get("/orders", params={"page_size": 1, "cursor": tampered}, headers={"X-Tenant": "st1"})
    assert res.status_code == 400 and res.json()["detail"] == "invalid cursor"
    res = client.get("/orders", params={"page_size": 1, "cursor": "not-a-cursor"}, headers={"X-Tenant": "st1"})
    assert res.status_code == 400 and res.json()["detail"] == "invalid cursor"


def test_cursor_expired_is_distinguishable() -> None:
    filters = {"prefix": "", "status": "", "min_paid_cents": 0, "page_size": 1}
    expired = cursor_mod.encode(
        {"v": 1, "tenant": "st1", "filters": filters, "last": "a-001", "exp": int(time.time()) - 1}
    )
    res = client.get("/orders", params={"page_size": 1, "cursor": expired}, headers={"X-Tenant": "st1"})
    assert res.status_code == 410 and res.json()["detail"] == "cursor expired"
