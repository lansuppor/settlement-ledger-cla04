import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import migrate

migrate()
client = TestClient(app)

HEADERS = {"X-Tenant": "t1"}


def _order(order_id: str, tenant: str = "t1", amount: int = 1000) -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"})


def _register(ticket_id: str, order_id: str, tenant: str = "t1", ticket_type: str = "payment", description: str = "收款未到账"):
    return client.post("/tickets", json={
        "tenant": tenant, "ticket_id": ticket_id, "order_id": order_id,
        "ticket_type": ticket_type, "description": description,
    })


def test_register_and_read_ticket() -> None:
    _order("tk-o1")
    res = _register("tk-1", "tk-o1")
    assert res.status_code == 201
    body = res.json()
    assert body["ticket_id"] == "tk-1" and body["order_id"] == "tk-o1"
    assert body["ticket_type"] == "payment" and body["status"] == "pending"
    assert body["created_at"] and body["note"] is None and body["processed_at"] is None
    got = client.get("/tickets/tk-1", headers=HEADERS)
    assert got.status_code == 200 and got.json() == body


def test_register_rejects_invalid_params() -> None:
    _order("tk-o2")
    assert _register("tk-2a", "tk-o2", ticket_type="other").status_code == 400
    assert _register("tk-2b", "tk-o2", description="").status_code == 400
    for ticket_type in ("acceptance", "payment", "refund", "settlement", "reversal"):
        assert _register(f"tk-2-{ticket_type}", "tk-o2", ticket_type=ticket_type).status_code == 201


def test_register_missing_or_cross_tenant_order_is_not_found() -> None:
    _order("tk-o3")
    assert _register("tk-3a", "tk-o3-missing").status_code == 404
    assert _register("tk-3b", "tk-o3", tenant="t2").status_code == 404


def test_register_replay_returns_first_result() -> None:
    _order("tk-o4")
    first = _register("tk-4", "tk-o4")
    second = _register("tk-4", "tk-o4")
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    page = client.get("/tickets", headers=HEADERS, params={"order_id": "tk-o4"}).json()
    assert len(page["tickets"]) == 1


def test_register_id_reused_with_other_order_or_type_is_rejected() -> None:
    _order("tk-o5a")
    _order("tk-o5b")
    assert _register("tk-5", "tk-o5a").status_code == 201
    assert _register("tk-5", "tk-o5b").status_code == 409
    assert _register("tk-5", "tk-o5a", ticket_type="refund").status_code == 409


def test_process_follows_status_order() -> None:
    _order("tk-o6")
    _register("tk-6", "tk-o6")
    res = client.post("/tickets/tk-6/process", json={"status": "processing", "note": "排查中"}, headers=HEADERS)
    assert res.status_code == 200
    assert res.json()["status"] == "processing" and res.json()["note"] == "排查中"
    assert res.json()["processed_at"]
    res = client.post("/tickets/tk-6/process", json={"status": "resolved", "note": "已补账"}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["status"] == "resolved"
    # 终态不再变化
    assert client.post("/tickets/tk-6/process", json={"status": "processing"}, headers=HEADERS).status_code == 409
    final = client.get("/tickets/tk-6", headers=HEADERS).json()
    assert final["status"] == "resolved" and final["note"] == "已补账" and final["processed_at"]


def test_process_pending_can_jump_to_terminal() -> None:
    _order("tk-o7")
    _register("tk-7", "tk-o7")
    res = client.post("/tickets/tk-7/process", json={"status": "rejected", "note": "非问题"}, headers=HEADERS)
    assert res.status_code == 200 and res.json()["status"] == "rejected"


def test_process_illegal_transition_keeps_state() -> None:
    _order("tk-o8")
    _register("tk-8", "tk-o8")
    client.post("/tickets/tk-8/process", json={"status": "processing", "note": "排查中"}, headers=HEADERS)
    # 处理中不可回退待处理
    assert client.post("/tickets/tk-8/process", json={"status": "pending"}, headers=HEADERS).status_code == 409
    ticket = client.get("/tickets/tk-8", headers=HEADERS).json()
    assert ticket["status"] == "processing" and ticket["note"] == "排查中"


def test_process_replay_same_status_returns_first_result() -> None:
    _order("tk-o9")
    _register("tk-9", "tk-o9")
    first = client.post("/tickets/tk-9/process", json={"status": "processing", "note": "排查中"}, headers=HEADERS)
    second = client.post("/tickets/tk-9/process", json={"status": "processing", "note": "排查中"}, headers=HEADERS)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    # 同状态不同备注拒绝，状态与备注不变
    assert client.post("/tickets/tk-9/process", json={"status": "processing", "note": "换个备注"}, headers=HEADERS).status_code == 409
    assert client.get("/tickets/tk-9", headers=HEADERS).json()["note"] == "排查中"


def test_process_unknown_status_and_missing_ticket() -> None:
    _order("tk-o10")
    _register("tk-10", "tk-o10")
    assert client.post("/tickets/tk-10/process", json={"status": "closed"}, headers=HEADERS).status_code == 400
    assert client.post("/tickets/tk-10-missing/process", json={"status": "processing"}, headers=HEADERS).status_code == 404
    assert client.post("/tickets/tk-10/process", json={"status": "processing"}, headers={"X-Tenant": "t2"}).status_code == 404
    assert client.post("/tickets/tk-10/process", json={"status": "processing"}).status_code == 400


def test_process_does_not_touch_order_or_ledger() -> None:
    _order("tk-o11", amount=800)
    client.post("/orders/tk-o11/payments", json={"amount_cents": 300}, headers=HEADERS)
    before = client.get("/orders/tk-o11", headers=HEADERS).json()
    ledger_before = client.get("/orders/tk-o11/ledger", headers=HEADERS).json()
    _register("tk-11", "tk-o11")
    client.post("/tickets/tk-11/process", json={"status": "processing"}, headers=HEADERS)
    client.post("/tickets/tk-11/process", json={"status": "resolved", "note": "已核实"}, headers=HEADERS)
    assert client.get("/orders/tk-o11", headers=HEADERS).json() == before
    assert client.get("/orders/tk-o11/ledger", headers=HEADERS).json() == ledger_before


def test_cross_tenant_read_is_not_found() -> None:
    _order("tk-o12")
    _register("tk-12", "tk-o12")
    assert client.get("/tickets/tk-12", headers={"X-Tenant": "t2"}).status_code == 404
    page = client.get("/tickets", headers={"X-Tenant": "t2"}).json()
    assert all(t["tenant"] == "t2" for t in page["tickets"])


def test_search_filters_and_pagination() -> None:
    for idx in range(3):
        _order(f"tk-p{idx}")
    _register("tk-p1", "tk-p0", ticket_type="acceptance", description="受理异常")
    _register("tk-p2", "tk-p1", ticket_type="refund", description="退款未到账")
    _register("tk-p3", "tk-p2", ticket_type="settlement", description="结算金额不符")
    client.post("/tickets/tk-p2/process", json={"status": "resolved"}, headers=HEADERS)

    page = client.get("/tickets", headers=HEADERS, params={"order_id": "tk-p1"}).json()
    assert [t["ticket_id"] for t in page["tickets"]] == ["tk-p2"]
    page = client.get("/tickets", headers=HEADERS, params={"status": "resolved", "order_id": "tk-p1"}).json()
    assert [t["ticket_id"] for t in page["tickets"]] == ["tk-p2"]
    page = client.get("/tickets", headers=HEADERS, params={"status": "pending", "order_id": "tk-p1"}).json()
    assert page["tickets"] == []
    assert client.get("/tickets", headers=HEADERS, params={"status": "closed"}).status_code == 400

    # 按订单标识升序分页，游标翻页不重不漏
    first = client.get("/tickets", headers=HEADERS, params={"page_size": 2}).json()
    assert first["next_cursor"] is not None
    seen = [t["ticket_id"] for t in first["tickets"]]
    cursor = first["next_cursor"]
    while cursor:
        page = client.get("/tickets", headers=HEADERS, params={"page_size": 2, "cursor": cursor}).json()
        seen += [t["ticket_id"] for t in page["tickets"]]
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen))
    assert "tk-p1" in seen and "tk-p3" in seen


def test_search_cursor_rejects_filter_change_and_cross_tenant() -> None:
    _order("tk-c0")
    _order("tk-c1")
    _register("tk-c1", "tk-c0", description="问题一")
    _register("tk-c2", "tk-c1", description="问题二")
    first = client.get("/tickets", headers=HEADERS, params={"status": "pending", "page_size": 1}).json()
    cursor = first["next_cursor"]
    assert cursor is not None
    changed = client.get("/tickets", headers=HEADERS, params={"status": "resolved", "cursor": cursor})
    assert changed.status_code == 400 and changed.json()["detail"] == "cursor_filter_mismatch"
    other = client.get("/tickets", headers={"X-Tenant": "t2"}, params={"cursor": cursor})
    assert other.status_code == 400 and other.json()["detail"] == "cursor_tenant_mismatch"
    bad = client.get("/tickets", headers=HEADERS, params={"cursor": "not-a-cursor"})
    assert bad.status_code == 400 and bad.json()["detail"] == "cursor_malformed"
