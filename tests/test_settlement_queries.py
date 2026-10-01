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


def _reverse(oid: str, reversal_id: str, payment_id: str, tenant: str = "t1"):
    return client.post(
        f"/orders/{oid}/reversals",
        json={"reversal_id": reversal_id, "payment_id": payment_id},
        headers={"X-Tenant": tenant},
    )


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


def _query(oid: str, tenant: str = "t1", status: str | None = None):
    params = {"status": status} if status is not None else {}
    return client.get(f"/orders/{oid}/settlements", headers={"X-Tenant": tenant}, params=params)


# ---------- 订单存在性与租户隔离 ----------

def test_query_unknown_order_is_404() -> None:
    resp = _query("q-missing")
    assert resp.status_code == 404 and resp.json()["detail"] == "order not found"


def test_query_without_tenant_header_is_400() -> None:
    resp = client.get("/orders/q1/settlements")
    assert resp.status_code == 400 and resp.json()["detail"] == "tenant header is required"


def test_cross_tenant_query_is_404_and_does_not_leak_existence() -> None:
    _make_order("q2", 100, tenant="t1")
    _pay("q2", 100, tenant="t1")
    _settle("q2", "set-q2", tenant="t1")
    # 其他租户请求头查询本租户订单：与订单不存在相同结论
    resp = _query("q2", tenant="t2")
    assert resp.status_code == 404 and resp.json()["detail"] == "order not found"


def test_query_result_contains_only_own_tenant_records() -> None:
    _make_order("q3a", 100, tenant="t1")
    # 另一租户存在同业务往来的订单，但不生成结算单；本租户查询不得混入其任何记录
    _make_order("q3b-other", 100, tenant="t2")
    _pay("q3b-other", 100, tenant="t2")
    _pay("q3a", 100, tenant="t1")
    doc_a = _settle("q3a", "set-q3a", tenant="t1").json()["settlement_doc_id"]
    data = _query("q3a", tenant="t1").json()
    assert data["tenant"] == "t1"
    assert [s["settlement_doc_id"] for s in data["settlements"]] == [doc_a]
    for s in data["settlements"]:
        assert s["order_id"] == "q3a"
    rec = data["reconciliation"]
    assert rec["order_amount_cents"] == 100 and rec["live_payments_total_cents"] == 100


# ---------- 结算单时间线 ----------

def test_query_empty_order_returns_empty_timeline_and_closed_books() -> None:
    _make_order("q4", 500)
    _pay("q4", 200)
    data = _query("q4").json()
    assert data["status_filter"] == "all"
    assert data["settlements"] == []
    rec = data["reconciliation"]
    # 部分收款也闭合：账面已收 == 未被冲正收款合计；已收 + 未收 == 订单金额
    assert rec["closed"] is True and rec["conclusion"] == "closed"
    assert rec["order_amount_cents"] == 500
    assert rec["booked_paid_cents"] == 200
    assert rec["live_payments_total_cents"] == 200
    assert rec["outstanding_cents"] == 300
    assert rec["discrepancies"] == []


def test_query_active_settlement_fields() -> None:
    _make_order("q5", 500)
    _pay("q5", 200)
    _pay("q5", 300)
    created = _settle("q5", "set-q5").json()
    data = _query("q5").json()
    assert len(data["settlements"]) == 1
    item = data["settlements"][0]
    assert item["settlement_id"] == "set-q5"
    assert item["settlement_doc_id"] == created["settlement_doc_id"]
    assert item["amount_cents"] == 500
    assert item["status"] == "active"
    assert item["created_at"] == created["created_at"]
    assert item["revoked_at"] is None
    assert "revocation_id" not in item
    rec = data["reconciliation"]
    assert rec["closed"] is True
    assert rec["booked_paid_cents"] == rec["live_payments_total_cents"] == 500
    assert rec["outstanding_cents"] == 0


def test_timeline_keeps_revoke_and_resettle_in_order_with_immutable_snapshot() -> None:
    _make_order("q6", 500)
    p1 = _pay("q6", 200)["payment_id"]
    _pay("q6", 300)
    first = _settle("q6", "set-q6a").json()
    assert _revoke(first["settlement_doc_id"], "cancel-q6").status_code == 200
    assert _reverse("q6", "rev-q6", p1).status_code == 200
    _pay("q6", 200)
    second = _settle("q6", "set-q6b").json()

    data = _query("q6").json()
    docs = data["settlements"]
    assert [d["status"] for d in docs] == ["revoked", "active"]
    assert [d["settlement_id"] for d in docs] == ["set-q6a", "set-q6b"]
    assert [d["settlement_doc_id"] for d in docs] == [
        first["settlement_doc_id"],
        second["settlement_doc_id"],
    ]
    # 生成时间从早到晚稳定排序（同毫秒时由存储层 rowid 兜底先后）
    assert docs[0]["created_at"] <= docs[1]["created_at"]

    old, new = docs
    # 旧结算单保留撤销信息，金额快照不因撤销/再结算而改变
    assert old["revoked_at"] is not None
    assert old["revocation_id"] == "cancel-q6"
    assert old["amount_cents"] == 500
    assert new["revoked_at"] is None
    assert new["amount_cents"] == 500

    rec = data["reconciliation"]
    assert rec["closed"] is True
    assert rec["live_payments_total_cents"] == 500


def test_status_filter_only_changes_returned_scope() -> None:
    _make_order("q7", 100)
    _pay("q7", 100)
    doc = _settle("q7", "set-q7").json()["settlement_doc_id"]
    assert _revoke(doc, "cancel-q7").status_code == 200

    revoked_only = _query("q7", status="revoked").json()
    assert [s["status"] for s in revoked_only["settlements"]] == ["revoked"]
    assert revoked_only["status_filter"] == "revoked"

    active_only = _query("q7", status="active").json()
    assert active_only["settlements"] == []
    assert active_only["status_filter"] == "active"

    all_docs = _query("q7", status="all").json()
    assert len(all_docs["settlements"]) == 1
    # 默认（不带条件）等价于 all
    default = _query("q7").json()
    assert default["settlements"] == all_docs["settlements"]
    # 过滤不影响对账核对结果
    assert revoked_only["reconciliation"] == active_only["reconciliation"] == all_docs["reconciliation"]

    # 过滤不改变任何单据状态
    conn = connect()
    try:
        status = conn.execute(
            "SELECT status FROM settlements WHERE settlement_id=?", (doc,)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert status == "revoked"


def test_invalid_status_filter_is_400() -> None:
    _make_order("q8", 100)
    resp = _query("q8", status="archived")
    assert resp.status_code == 400
    assert "status filter" in resp.json()["detail"]


# ---------- 对账逐笔数据 ----------

def test_live_payments_total_excludes_reversed_payments() -> None:
    _make_order("q9", 500)
    p1 = _pay("q9", 200)["payment_id"]
    _pay("q9", 300)
    assert _reverse("q9", "rev-q9", p1).status_code == 200
    rec = _query("q9").json()["reconciliation"]
    # 被冲正的 200 不计入未被冲正收款合计；账面已收随之减少，仍与逐笔合计闭合
    assert rec["live_payments_total_cents"] == 300
    assert rec["booked_paid_cents"] == 300
    assert rec["outstanding_cents"] == 200
    assert rec["closed"] is True


def test_not_closed_is_explicit_when_booked_amount_drifts_from_payments() -> None:
    # 正常写路径保证恒等闭合；这里通过底层直接篡改账面已收模拟账实不符，
    # 查询必须显式标明未闭合并指出差异，而不是静默给出成功结论
    _make_order("q10", 500)
    _pay("q10", 500)
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE orders SET paid_cents=400 WHERE tenant='t1' AND order_id='q10'")
        conn.execute("COMMIT")
    finally:
        conn.close()

    rec = _query("q10").json()["reconciliation"]
    assert rec["closed"] is False
    assert rec["conclusion"] == "not_closed"
    assert rec["order_amount_cents"] == 500
    assert rec["booked_paid_cents"] == 400
    assert rec["live_payments_total_cents"] == 500
    assert rec["outstanding_cents"] == 100
    checks = {d["check"]: d for d in rec["discrepancies"]}
    assert "booked_paid_equals_live_payments" in checks
    drift = checks["booked_paid_equals_live_payments"]
    assert drift["expected"] == 500 and drift["actual"] == 400
    assert drift["difference_cents"] == -100
    # 已收 400 + 未收 100 仍等于订单金额，该恒等项不应报差异
    assert "paid_plus_outstanding_equals_order_amount" not in checks


# ---------- 只读、幂等与持久化 ----------

def test_query_is_read_only_and_repeatable() -> None:
    _make_order("q11", 100)
    _pay("q11", 100)
    created = _settle("q11", "set-q11").json()

    first = _query("q11")
    second = _query("q11")
    third = _query("q11", status="active")
    assert first.status_code == second.status_code == third.status_code == 200
    assert first.json() == second.json()

    # 重复查询不改变既有结算单状态；查询后撤销仍正常生效
    assert _revoke(created["settlement_doc_id"], "cancel-q11").status_code == 200
    after = _query("q11").json()
    assert after["settlements"][0]["status"] == "revoked"
    # 查询后仍可用新标识重新核销
    again = _settle("q11", "set-q11b")
    assert again.status_code == 201 and again.json()["status"] == "active"
    docs = _query("q11").json()["settlements"]
    assert len(docs) == 2 and docs[0]["amount_cents"] == docs[1]["amount_cents"] == 100


def test_query_survives_restart_with_same_timeline() -> None:
    _make_order("q12", 400)
    _pay("q12", 400)
    doc = _settle("q12", "set-q12a").json()["settlement_doc_id"]
    assert _revoke(doc, "cancel-q12").status_code == 200
    assert _settle("q12", "set-q12b").status_code == 201

    before = _query("q12").json()
    migrate()  # 等价于服务重启后重连同一数据库
    after = _query("q12").json()

    assert after == before
    assert [s["settlement_id"] for s in after["settlements"]] == ["set-q12a", "set-q12b"]
    assert [s["status"] for s in after["settlements"]] == ["revoked", "active"]
    assert after["reconciliation"]["closed"] is True
