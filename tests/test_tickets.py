import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

T = "tk"


def _new_order(oid: str, amount: int, paid: int = 0, tenant: str = T) -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"})
    if paid:
        client.post(f"/orders/{oid}/payments", json={"amount_cents": paid}, headers={"X-Tenant": tenant})


def _register(tid: str, oid: str, ttype: str = "payment", desc: str = "到账金额不符", tenant: str = T):
    return client.post(
        "/tickets",
        json={"tenant": tenant, "ticket_id": tid, "order_id": oid, "ticket_type": ttype, "description": desc},
    )


def _process(tid: str, status: str, note=None, tenant: str = T):
    return client.post(
        f"/tickets/{tid}/process",
        json={"status": status, "note": note},
        headers={"X-Tenant": tenant},
    )


# ---------- 登记 ----------

def test_register_ticket_returns_pending_view() -> None:
    _new_order("tk-o1", 1000)
    res = _register("tk-t1", "tk-o1")
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["ticket_id"] == "tk-t1" and body["order_id"] == "tk-o1"
    assert body["ticket_type"] == "payment" and body["status"] == "pending"
    assert body["description"] == "到账金额不符"
    assert body["note"] is None and body["processed_at"] is None
    assert body["created_at"]


def test_register_all_five_types_accepted() -> None:
    _new_order("tk-o2", 100)
    for i, ttype in enumerate(("accept", "payment", "refund", "settlement", "reversal")):
        res = _register(f"tk-t2-{i}", "tk-o2", ttype=ttype)
        assert res.status_code == 201, res.text
        assert res.json()["ticket_type"] == ttype


def test_register_invalid_params_are_bad_request() -> None:
    _new_order("tk-o3", 100)
    # 工单类型不在五类内
    assert _register("tk-t3a", "tk-o3", ttype="other").status_code == 400
    # 问题描述为空
    assert _register("tk-t3b", "tk-o3", desc="").status_code == 400
    # 缺字段
    assert client.post("/tickets", json={"tenant": T, "ticket_id": "tk-t3c"}).status_code == 400
    # 非法请求不占用工单标识
    assert _register("tk-t3a", "tk-o3").status_code == 201


def test_register_unknown_or_cross_tenant_order_is_not_found() -> None:
    assert _register("tk-t4a", "tk-missing").status_code == 404
    _new_order("tk-o4", 100)
    assert _register("tk-t4b", "tk-o4", tenant="other").status_code == 404
    # 跨租户失败不得占用该工单标识在本租户的使用。
    assert _register("tk-t4b", "tk-o4").status_code == 201


def test_register_replay_returns_first_result() -> None:
    _new_order("tk-o5", 100)
    first = _register("tk-t5", "tk-o5")
    second = _register("tk-t5", "tk-o5")
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM tickets WHERE tenant=? AND ticket_id='tk-t5'", (T,)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_register_id_reused_with_other_order_or_type_is_conflicted() -> None:
    _new_order("tk-o6a", 100)
    _new_order("tk-o6b", 100)
    assert _register("tk-t6", "tk-o6a").status_code == 201
    assert _register("tk-t6", "tk-o6b").status_code == 409
    assert _register("tk-t6", "tk-o6a", ttype="refund").status_code == 409


def test_ticket_id_is_scoped_per_tenant() -> None:
    _new_order("tk-o7", 100)
    _new_order("tk-o7", 100, tenant="other-tk")
    assert _register("tk-t7", "tk-o7").status_code == 201
    assert _register("tk-t7", "tk-o7", tenant="other-tk").status_code == 201


# ---------- 处理 ----------

def test_process_full_lifecycle_and_terminal_keeps_last_note() -> None:
    _new_order("tp-o1", 100)
    _register("tp-t1", "tp-o1")
    res = _process("tp-t1", "processing", "已联系渠道核实")
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "processing" and res.json()["note"] == "已联系渠道核实"
    res = _process("tp-t1", "resolved", "已补记收款")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "resolved"
    assert body["note"] == "已补记收款" and body["processed_at"]
    # 终态不再变化：任何进一步处理都拒绝且状态、备注不变。
    assert _process("tp-t1", "processing", "x").status_code == 409
    assert _process("tp-t1", "rejected", "y").status_code == 409
    got = client.get("/tickets", params={"order_id": "tp-o1"}, headers={"X-Tenant": T}).json()
    assert got["tickets"][0]["status"] == "resolved"
    assert got["tickets"][0]["note"] == "已补记收款"


def test_process_pending_can_jump_to_terminal_directly() -> None:
    _new_order("tp-o2", 100)
    _register("tp-t2a", "tp-o2")
    assert _process("tp-t2a", "resolved", "直接办结").status_code == 200
    _register("tp-t2b", "tp-o2")
    assert _process("tp-t2b", "rejected", "非本系统问题").status_code == 200
    assert _process("tp-t2b", "rejected", "非本系统问题").json()["status"] == "rejected"


def test_process_illegal_transition_is_conflicted_and_changes_nothing() -> None:
    _new_order("tp-o3", 100)
    _register("tp-t3", "tp-o3")
    _process("tp-t3", "processing", "跟进中")
    # 处理中不可回退待处理
    assert _process("tp-t3", "pending").status_code == 409
    got = client.get("/tickets", params={"order_id": "tp-o3"}, headers={"X-Tenant": T}).json()
    assert got["tickets"][0]["status"] == "processing"
    assert got["tickets"][0]["note"] == "跟进中"


def test_process_replay_same_target_returns_first_result() -> None:
    _new_order("tp-o4", 100)
    _register("tp-t4", "tp-o4")
    first = _process("tp-t4", "processing", "备注A")
    second = _process("tp-t4", "processing", "备注A")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_process_same_target_with_different_note_is_conflicted() -> None:
    _new_order("tp-o5", 100)
    _register("tp-t5", "tp-o5")
    assert _process("tp-t5", "processing", "备注A").status_code == 200
    assert _process("tp-t5", "processing", "备注B").status_code == 409
    got = client.get("/tickets", params={"order_id": "tp-o5"}, headers={"X-Tenant": T}).json()
    assert got["tickets"][0]["note"] == "备注A"


def test_process_unknown_or_cross_tenant_is_not_found() -> None:
    _new_order("tp-o6", 100)
    _register("tp-t6", "tp-o6")
    assert _process("tp-missing", "processing").status_code == 404
    assert _process("tp-t6", "processing", tenant="other").status_code == 404
    assert client.post("/tickets/tp-t6/process", json={"status": "processing"}).status_code == 400
    # 跨租户与缺租户头的失败不影响本租户正常处理。
    assert _process("tp-t6", "processing").status_code == 200


def test_process_invalid_status_is_bad_request() -> None:
    _new_order("tp-o7", 100)
    _register("tp-t7", "tp-o7")
    assert _process("tp-t7", "done").status_code == 400


def test_process_does_not_touch_order_or_ledger() -> None:
    _new_order("tp-o8", 1000, 600)
    before = client.get("/orders/tp-o8", headers={"X-Tenant": T}).json()
    ledger_before = client.get("/orders/tp-o8/ledger", headers={"X-Tenant": T}).json()
    _register("tp-t8", "tp-o8")
    _process("tp-t8", "processing", "核实中")
    _process("tp-t8", "resolved", "已解决")
    after = client.get("/orders/tp-o8", headers={"X-Tenant": T}).json()
    ledger_after = client.get("/orders/tp-o8/ledger", headers={"X-Tenant": T}).json()
    assert after == before
    assert ledger_after == ledger_before


# ---------- 检索 ----------

def test_search_filters_by_order_and_status() -> None:
    tenant = "tk-search"
    _new_order("ts-o1", 100, tenant=tenant)
    _new_order("ts-o2", 100, tenant=tenant)
    _register("ts-t1", "ts-o1", tenant=tenant)
    _register("ts-t2", "ts-o1", ttype="refund", tenant=tenant)
    _register("ts-t3", "ts-o2", tenant=tenant)
    _process("ts-t2", "resolved", "ok", tenant=tenant)
    # 按订单过滤
    res = client.get("/tickets", params={"order_id": "ts-o1"}, headers={"X-Tenant": tenant})
    assert [t["ticket_id"] for t in res.json()["tickets"]] == ["ts-t1", "ts-t2"]
    # 按状态过滤
    res = client.get("/tickets", params={"status": "resolved"}, headers={"X-Tenant": tenant})
    assert [t["ticket_id"] for t in res.json()["tickets"]] == ["ts-t2"]
    # 组合过滤
    res = client.get(
        "/tickets", params={"order_id": "ts-o1", "status": "pending"}, headers={"X-Tenant": tenant}
    )
    assert [t["ticket_id"] for t in res.json()["tickets"]] == ["ts-t1"]
    # 非法状态过滤
    assert client.get("/tickets", params={"status": "done"}, headers={"X-Tenant": tenant}).status_code == 400
    # 缺租户头
    assert client.get("/tickets").status_code == 400


def test_search_paginates_by_order_id_ascending() -> None:
    tenant = "tk-page"
    for i in range(5):
        _new_order(f"tp-o{i}", 100, tenant=tenant)
        _register(f"tp-t{i}", f"tp-o{i}", tenant=tenant)
    seen = []
    cursor = None
    while True:
        params = {"page_size": 2}
        if cursor:
            params["cursor"] = cursor
        res = client.get("/tickets", params=params, headers={"X-Tenant": tenant})
        assert res.status_code == 200, res.text
        body = res.json()
        seen.extend(t["ticket_id"] for t in body["tickets"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == [f"tp-t{i}" for i in range(5)]


def test_search_page_size_is_capped() -> None:
    tenant = "tk-cap"
    _new_order("tc-o1", 100, tenant=tenant)
    _register("tc-t1", "tc-o1", tenant=tenant)
    res = client.get("/tickets", params={"page_size": 100000}, headers={"X-Tenant": tenant})
    assert res.status_code == 200
    assert len(res.json()["tickets"]) == 1


def test_search_is_tenant_scoped() -> None:
    tenant = "tk-iso"
    _new_order("ti-o1", 100, tenant=tenant)
    _register("ti-t1", "ti-o1", tenant=tenant)
    # 他租户检索不到本租户工单；跨租户按不存在处理。
    res = client.get("/tickets", params={"order_id": "ti-o1"}, headers={"X-Tenant": "other"})
    assert res.status_code == 200 and res.json()["tickets"] == []


def test_search_cursor_errors_are_distinguishable() -> None:
    tenant = "tk-cur"
    _new_order("tu-o1", 100, tenant=tenant)
    _register("tu-t1", "tu-o1", tenant=tenant)
    # 非法游标
    res = client.get("/tickets", params={"cursor": "bad"}, headers={"X-Tenant": tenant})
    assert res.status_code == 400 and res.json()["detail"] == "cursor_malformed"
    # 跨租户游标
    other = "tk-cur2"
    _new_order("tu-o9", 100, tenant=other)
    for i in range(3):
        _register(f"tu-t9-{i}", "tu-o9", tenant=other)
    page1 = client.get("/tickets", params={"page_size": 1}, headers={"X-Tenant": other}).json()
    cur = page1["next_cursor"]
    assert cur is not None
    res = client.get("/tickets", params={"cursor": cur}, headers={"X-Tenant": tenant})
    assert res.status_code == 400 and res.json()["detail"] == "cursor_tenant_mismatch"
    # 换过滤条件
    res = client.get(
        "/tickets", params={"cursor": cur, "status": "pending"}, headers={"X-Tenant": other}
    )
    assert res.status_code == 400 and res.json()["detail"] == "cursor_filter_mismatch"
