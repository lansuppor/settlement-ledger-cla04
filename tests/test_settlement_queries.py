import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_settlement_queries.sqlite"))

from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)


def _make_order(oid: str, amount: int = 500, tenant: str = "t1") -> None:
    body = {"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201


def _pay(oid: str, amount: int, tenant: str = "t1"):
    resp = client.post(f"/orders/{oid}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _settle(oid: str, settlement_id: str, tenant: str = "t1"):
    return client.post(
        f"/orders/{oid}/settlements",
        json={"settlement_id": settlement_id},
        headers={"X-Tenant": tenant},
    )


def _revoke(doc_id: str, revocation_id: str, tenant: str = "t1"):
    return client.post(
        f"/settlements/{doc_id}/revocations",
        json={"revocation_id": revocation_id},
        headers={"X-Tenant": tenant},
    )


def _list(oid: str, tenant: str = "t1", **params):
    return client.get(f"/orders/{oid}/settlements", params=params, headers={"X-Tenant": tenant})


def _reconcile(oid: str, tenant: str = "t1"):
    return client.get(f"/orders/{oid}/reconciliation", headers={"X-Tenant": tenant})


def test_list_empty_for_order_without_settlements() -> None:
    _make_order("q1", 500)
    resp = _list("q1")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["tenant"] == "t1" and data["order_id"] == "q1"
    assert data["status_filter"] == "all"
    assert data["settlements"] == []


def test_list_preserves_settle_revoke_resettle_timeline() -> None:
    _make_order("q2", 500)
    _pay("q2", 200)
    _pay("q2", 300)
    first = _settle("q2", "set-q2a").json()
    assert _revoke(first["settlement_doc_id"], "revoke-q2").status_code == 200
    second = _settle("q2", "set-q2b").json()

    resp = _list("q2")
    assert resp.status_code == 200, resp.text
    items = resp.json()["settlements"]
    # 首次核销与撤销后重新核销按生成先后排列，旧单保留
    assert len(items) == 2
    assert items[0]["settlement_doc_id"] == first["settlement_doc_id"]
    assert items[0]["settlement_id"] == "set-q2a"
    assert items[0]["status"] == "revoked"
    assert items[0]["revoked_at"] is not None
    assert items[0]["revocation_id"] == "revoke-q2"
    assert items[1]["settlement_doc_id"] == second["settlement_doc_id"]
    assert items[1]["settlement_id"] == "set-q2b"
    assert items[1]["status"] == "active"
    assert items[1]["revoked_at"] is None
    assert "revocation_id" not in items[1]
    # 金额快照保留核销时值，不因后续撤销/再结算改变
    assert items[0]["amount_cents"] == 500
    assert items[1]["amount_cents"] == 500
    # 生成时间从早到晚稳定排序
    assert items[0]["created_at"] <= items[1]["created_at"]


def test_list_status_filter() -> None:
    _make_order("q3", 100)
    _pay("q3", 100)
    doc = _settle("q3", "set-q3a").json()["settlement_doc_id"]
    assert _revoke(doc, "revoke-q3").status_code == 200
    _settle("q3", "set-q3b")

    active = _list("q3", status="active").json()
    assert [s["settlement_id"] for s in active["settlements"]] == ["set-q3b"]
    revoked = _list("q3", status="revoked").json()
    assert [s["settlement_id"] for s in revoked["settlements"]] == ["set-q3a"]
    all_items = _list("q3").json()
    assert [s["settlement_id"] for s in all_items["settlements"]] == ["set-q3a", "set-q3b"]
    # 过滤只影响返回范围，不改变单据状态
    again = _list("q3").json()
    assert again == all_items


def test_list_invalid_status_filter_is_400() -> None:
    _make_order("q4", 100)
    resp = _list("q4", status="bogus")
    assert resp.status_code == 400


def test_list_unknown_order_and_cross_tenant_are_404() -> None:
    _make_order("q5", 100, tenant="t1")
    _pay("q5", 100, tenant="t1")
    assert _settle("q5", "set-q5", tenant="t1").status_code == 201
    # 不存在的订单与其他租户点名返回相同结论，不泄漏对象是否存在
    missing = _list("no-such-order")
    foreign = _list("q5", tenant="t2")
    assert missing.status_code == 404 and foreign.status_code == 404
    assert missing.json() == foreign.json()


def test_reconcile_fully_paid_and_settled_is_closed() -> None:
    _make_order("q6", 500)
    _pay("q6", 200)
    _pay("q6", 300)
    assert _settle("q6", "set-q6").status_code == 201
    resp = _reconcile("q6")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["amount_cents"] == 500
    assert data["paid_cents"] == 500
    assert data["live_paid_cents"] == 500          # 已收恒等于未被冲正的收款合计
    assert data["outstanding_cents"] == 0
    assert data["paid_cents"] + data["outstanding_cents"] == data["amount_cents"]
    assert data["closed"] is True
    assert data["discrepancies"] == []


def test_reconcile_partially_paid_reflects_live_totals() -> None:
    _make_order("q7", 500)
    pid = _pay("q7", 200)["payment_id"]
    _pay("q7", 300)
    # 冲正一笔后：账面已收 = 未被冲正收款合计 = 300，未收 200
    resp = client.post(
        "/orders/q7/reversals",
        json={"reversal_id": "rev-q7", "payment_id": pid},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 200
    data = _reconcile("q7").json()
    assert data["paid_cents"] == 300
    assert data["live_paid_cents"] == 300
    assert data["outstanding_cents"] == 200
    assert data["closed"] is True


def test_reconcile_unclosed_reports_difference() -> None:
    _make_order("q8", 500)
    _pay("q8", 500)
    # 直接改动账面制造不闭合（模拟账面与明细脱节），查询须标明未闭合并说明差异
    conn = connect()
    try:
        conn.execute("UPDATE orders SET paid_cents = paid_cents - 100 WHERE order_id='q8'")
    finally:
        conn.close()
    data = _reconcile("q8").json()
    assert data["closed"] is False
    assert data["paid_cents"] == 400
    assert data["live_paid_cents"] == 500
    assert len(data["discrepancies"]) == 1
    diff = data["discrepancies"][0]
    assert diff["check"] == "paid_equals_live_payments"
    assert diff["difference_cents"] == -100


def test_reconcile_unknown_order_and_cross_tenant_are_404() -> None:
    _make_order("q9", 100, tenant="t1")
    missing = _reconcile("no-such-order")
    foreign = _reconcile("q9", tenant="t2")
    assert missing.status_code == 404 and foreign.status_code == 404
    assert missing.json() == foreign.json()


def test_queries_require_tenant_header() -> None:
    assert client.get("/orders/q1/settlements").status_code == 400
    assert client.get("/orders/q1/reconciliation").status_code == 400


def test_queries_are_read_only_and_do_not_leak_across_tenants() -> None:
    _make_order("q10", 100, tenant="t1")
    _pay("q10", 100, tenant="t1")
    _make_order("q10", 100, tenant="tB")
    _pay("q10", 100, tenant="tB")
    _settle("q10", "set-q10-t1", tenant="t1")
    _settle("q10", "set-q10-tB", tenant="tB")

    before_t1 = _list("q10", tenant="t1").json()
    before_tB = _list("q10", tenant="tB").json()
    # 各自只看到自己的结算单
    assert [s["settlement_id"] for s in before_t1["settlements"]] == ["set-q10-t1"]
    assert [s["settlement_id"] for s in before_tB["settlements"]] == ["set-q10-tB"]
    # 重复查询结果一致
    assert _list("q10", tenant="t1").json() == before_t1
    assert _reconcile("q10", tenant="t1").json() == _reconcile("q10", tenant="t1").json()
    # 查询后再次撤销/重新结算行为不变，既有单据不受影响
    doc = before_t1["settlements"][0]["settlement_doc_id"]
    assert _revoke(doc, "revoke-q10", tenant="t1").status_code == 200
    assert _settle("q10", "set-q10-t1b", tenant="t1").status_code == 201
    after = _list("q10", tenant="t1").json()["settlements"]
    assert [s["settlement_id"] for s in after] == ["set-q10-t1", "set-q10-t1b"]
    assert after[0]["status"] == "revoked" and after[0]["amount_cents"] == 100
    # tB 的单据不受 t1 操作影响
    assert _list("q10", tenant="tB").json() == before_tB


def test_queries_consistent_across_restart() -> None:
    _make_order("q11", 400)
    _pay("q11", 400)
    doc = _settle("q11", "set-q11a").json()["settlement_doc_id"]
    assert _revoke(doc, "revoke-q11").status_code == 200
    _settle("q11", "set-q11b")
    before_list = _list("q11").json()
    before_recon = _reconcile("q11").json()
    migrate()  # 等价于服务重启后重连同一数据库
    assert _list("q11").json() == before_list
    assert _reconcile("q11").json() == before_recon
    # 多次核销、撤销与再核销的顺序关系仍可还原
    items = before_list["settlements"]
    assert [s["status"] for s in items] == ["revoked", "active"]
