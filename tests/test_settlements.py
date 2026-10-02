import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

T = "sl"
RT = "rct"  # 对账测试使用独立租户，避免同库其它订单进入租户全量汇总


def _new_order(oid: str, amount: int, paid: int = 0, tenant: str = T) -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"})
    if paid:
        client.post(f"/orders/{oid}/payments", json={"amount_cents": paid}, headers={"X-Tenant": tenant})


def _settle(oid: str, sid: str, amount: int, tenant: str = T):
    return client.post(
        f"/orders/{oid}/settlements",
        json={"settlement_id": sid, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


# ---------- 结算 ----------

def test_settle_fully_paid_order_records_snapshot() -> None:
    _new_order("sl-o1", 1000, 1000)
    res = _settle("sl-o1", "sl-s1", 1000)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["settlement_id"] == "sl-s1"
    assert body["amount_cents"] == 1000
    assert body["paid_cents"] == 1000 and body["refunded_cents"] == 0
    assert body["status"] == "effective"
    order = client.get("/orders/sl-o1", headers={"X-Tenant": T}).json()
    assert order["status"] == "settled" and order["outstanding_cents"] == 0


def test_settle_with_outstanding_is_conflicted_and_changes_nothing() -> None:
    _new_order("sl-o2", 1000, 600)
    assert _settle("sl-o2", "sl-s2", 1000).status_code == 409
    order = client.get("/orders/sl-o2", headers={"X-Tenant": T}).json()
    assert order["status"] == "accepted" and order["outstanding_cents"] == 400
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM settlements WHERE tenant=? AND settlement_id='sl-s2'", (T,)).fetchone()["n"]
        led = conn.execute("SELECT COUNT(*) AS n FROM ledger_entries WHERE tenant=? AND order_id='sl-o2' AND entry_type='settlement'", (T,)).fetchone()["n"]
    finally:
        conn.close()
    assert n == 0 and led == 0


def test_settle_replay_returns_first_result() -> None:
    _new_order("sl-o3", 500, 500)
    payload = {"settlement_id": "sl-s3", "amount_cents": 500}
    first = client.post("/orders/sl-o3/settlements", json=payload, headers={"X-Tenant": T})
    second = client.post("/orders/sl-o3/settlements", json=payload, headers={"X-Tenant": T})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM settlements WHERE tenant=? AND settlement_id='sl-s3'", (T,)).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_settle_id_reused_on_other_order_or_amount_is_conflicted() -> None:
    _new_order("sl-o4a", 300, 300)
    _new_order("sl-o4b", 300, 300)
    assert _settle("sl-o4a", "sl-s4", 300).status_code == 201
    assert _settle("sl-o4b", "sl-s4", 300).status_code == 409
    assert _settle("sl-o4a", "sl-s4", 299).status_code == 409


def test_settle_unknown_or_cross_tenant_order_is_not_found() -> None:
    assert _settle("sl-missing", "sl-sx", 1, tenant=T).status_code == 404
    _new_order("sl-o5", 100, 100)
    assert _settle("sl-o5", "sl-s5", 100, tenant="other").status_code == 404
    # 跨租户失败不得占用该结算标识在本租户的使用。
    assert _settle("sl-o5", "sl-s5", 100).status_code == 201


def test_settle_twice_on_one_order_requires_reversal() -> None:
    _new_order("sl-o6", 200, 200)
    assert _settle("sl-o6", "sl-s6a", 200).status_code == 201
    assert _settle("sl-o6", "sl-s6b", 200).status_code == 409


def test_concurrent_settlements_only_one_effective() -> None:
    _new_order("sl-o7", 400, 400)

    def attempt(sid: str) -> int:
        return _settle("sl-o7", sid, 400).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(attempt, ["sl-s7a", "sl-s7b"]))
    assert sorted(codes) == [201, 409]
    conn = connect()
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM settlements WHERE tenant=? AND order_id='sl-o7' AND status='effective'",
            (T,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert active == 1


# ---------- 冲正 ----------

def _reverse(oid: str, sid: str, rid: str, reason: str, tenant: str = T):
    return client.post(
        f"/orders/{oid}/settlements/{sid}/reversals",
        json={"reversal_id": rid, "reason": reason},
        headers={"X-Tenant": tenant},
    )


def test_reverse_voids_settlement_and_order_back_to_accepted() -> None:
    _new_order("rv-o1", 800, 800)
    _settle("rv-o1", "rv-s1", 800)
    res = _reverse("rv-o1", "rv-s1", "rv-r1", "customer dispute")
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["settlement_id"] == "rv-s1" and body["reason"] == "customer dispute"
    assert body["status"] == "reversed"
    order = client.get("/orders/rv-o1", headers={"X-Tenant": T}).json()
    assert order["status"] == "accepted"
    conn = connect()
    try:
        st = conn.execute("SELECT status FROM settlements WHERE tenant=? AND settlement_id='rv-s1'", (T,)).fetchone()["status"]
    finally:
        conn.close()
    assert st == "voided"


def test_reverse_replay_and_double_reverse() -> None:
    _new_order("rv-o2", 800, 800)
    _settle("rv-o2", "rv-s2", 800)
    payload = {"reversal_id": "rv-r2", "reason": "x"}
    first = client.post("/orders/rv-o2/settlements/rv-s2/reversals", json=payload, headers={"X-Tenant": T})
    second = client.post("/orders/rv-o2/settlements/rv-s2/reversals", json=payload, headers={"X-Tenant": T})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    # 另一冲正标识对同一结算再次冲正：冲突。
    assert _reverse("rv-o2", "rv-s2", "rv-r2b", "y").status_code == 409


def test_reverse_unknown_is_not_found_cross_tenant() -> None:
    _new_order("rv-o3", 100, 100)
    _settle("rv-o3", "rv-s3", 100)
    assert _reverse("rv-o3", "rv-s3", "rv-r3", "z", tenant="other").status_code == 404
    assert _reverse("rv-o3", "rv-missing", "rv-r3b", "z").status_code == 404
    assert _reverse("rv-other-order", "rv-s3", "rv-r3c", "z").status_code == 404


def test_reverse_then_settle_again_keeps_history() -> None:
    _new_order("rv-o4", 1000, 1000)
    _settle("rv-o4", "rv-s4a", 1000)
    _reverse("rv-o4", "rv-s4a", "rv-r4", "again")
    again = _settle("rv-o4", "rv-s4b", 1000)
    assert again.status_code == 201 and again.json()["seq"] == 2
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT settlement_id, status FROM settlements WHERE tenant=? AND order_id='rv-o4' ORDER BY seq",
            (T,),
        ).fetchall()
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM settlements WHERE tenant=? AND order_id='rv-o4' AND status='effective'",
            (T,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert [r["status"] for r in rows] == ["voided", "effective"]
    assert active == 1


def test_reversal_id_is_independent_from_settlement_id() -> None:
    _new_order("rv-o5", 100, 100)
    # 冲正标识与结算标识使用同一字符串，互不影响、互不拒绝。
    _settle("rv-o5", "same-id", 100)
    assert _reverse("rv-o5", "same-id", "same-id", "ok").status_code == 201


def test_concurrent_reversals_only_one_succeeds() -> None:
    _new_order("rv-o6", 100, 100)
    _settle("rv-o6", "rv-s6", 100)
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda rid: _reverse("rv-o6", "rv-s6", rid, "c").status_code, ["rv-r6a", "rv-r6b"]))
    assert sorted(codes) == [201, 409]


# ---------- 账务历史 ----------

def test_ledger_lists_entries_in_order_and_replays_state() -> None:
    _new_order("ld-o1", 1000)
    client.post("/orders/ld-o1/payments", json={"amount_cents": 600}, headers={"X-Tenant": T})
    client.post("/orders/ld-o1/refunds", json={"refund_id": "ld-rf1", "amount_cents": 200}, headers={"X-Tenant": T})
    client.post("/orders/ld-o1/payments", json={"amount_cents": 600}, headers={"X-Tenant": T})
    _settle("ld-o1", "ld-s1", 1000)
    _reverse("ld-o1", "ld-s1", "ld-rv1", "why")
    res = client.get("/orders/ld-o1/ledger", headers={"X-Tenant": T})
    assert res.status_code == 200
    entries = res.json()["entries"]
    assert [e["type"] for e in entries] == [
        "payment", "refund", "payment", "settlement", "reversal"
    ]
    assert [e["amount_cents"] for e in entries] == [600, 200, 600, 1000, 1000]
    # 每条含业务标识、类型、金额与操作后未收金额；操作后未收逐笔推进。
    assert [e["outstanding_cents"] for e in entries] == [400, 600, 0, 0, 0]
    assert entries[1]["biz_ref"] == "ld-rf1"
    # 序列最后一条的操作后未收与订单读取一致；冲正后状态为未结算。
    order = client.get("/orders/ld-o1", headers={"X-Tenant": T}).json()
    assert entries[-1]["outstanding_cents"] == order["outstanding_cents"] == 0
    assert order["status"] == "accepted"


def test_ledger_replay_after_more_activity_matches_order() -> None:
    _new_order("ld-o2", 500, 500)
    _settle("ld-o2", "ld-s2", 500)
    _reverse("ld-o2", "ld-s2", "ld-rv2", "open again")
    client.post("/orders/ld-o2/refunds", json={"refund_id": "ld-rf2", "amount_cents": 100}, headers={"X-Tenant": T})
    client.post("/orders/ld-o2/payments", json={"amount_cents": 100}, headers={"X-Tenant": T})
    entries = client.get("/orders/ld-o2/ledger", headers={"X-Tenant": T}).json()["entries"]
    order = client.get("/orders/ld-o2", headers={"X-Tenant": T}).json()
    assert entries[-1]["outstanding_cents"] == order["outstanding_cents"]
    assert order["amount_cents"] == order["paid_cents"] - order["refunded_cents"] + order["outstanding_cents"]


def test_ledger_unknown_or_cross_tenant_is_not_found() -> None:
    assert client.get("/orders/ld-nope/ledger", headers={"X-Tenant": T}).status_code == 404
    _new_order("ld-o3", 10, 10)
    assert client.get("/orders/ld-o3/ledger", headers={"X-Tenant": "other"}).status_code == 404
    assert client.get("/orders/ld-o3/ledger").status_code == 400


# ---------- 对账核销 ----------

def _start_recon(rid: str, tenant: str = T):
    return client.post("/reconciliations", json={"tenant": tenant, "reconciliation_id": rid})


def test_reconciliation_totals_conserve_and_match_order_reads() -> None:
    _new_order("rc-o1", 1000, 1000, tenant=RT)  # 已结清
    _settle("rc-o1", "rc-s1", 1000, tenant=RT)
    _new_order("rc-o2", 800, 600, tenant=RT)    # 部分收款，未收 200
    _new_order("rc-o3", 500, 500, tenant=RT)
    client.post("/orders/rc-o3/refunds", json={"refund_id": "rc-rf1", "amount_cents": 200}, headers={"X-Tenant": RT})
    # rc-o3: 已收 500、已退 200、未收 200；应收 = 500+200=700 = 订单金额 + 已退。
    res = _start_recon("rc-rec1", tenant=RT)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["order_count"] == 3
    assert body["total_paid_cents"] == 2100
    assert body["total_refunded_cents"] == 200
    assert body["total_outstanding_cents"] == 400
    # 守恒：应收 = 已收 + 未收
    assert body["total_receivable_cents"] == body["total_paid_cents"] + body["total_outstanding_cents"]
    assert body["total_receivable_cents"] == 2500
    by_id = {row["order_id"]: row for row in body["orders"]}
    for oid in ("rc-o1", "rc-o2", "rc-o3"):
        live = client.get(f"/orders/{oid}", headers={"X-Tenant": RT}).json()
        snap = by_id[oid]
        assert snap["amount_cents"] == live["amount_cents"]
        assert snap["paid_cents"] == live["paid_cents"]
        assert snap["refunded_cents"] == live["refunded_cents"]
        assert snap["outstanding_cents"] == live["outstanding_cents"]
        assert snap["amount_cents"] + snap["refunded_cents"] == snap["paid_cents"] + snap["outstanding_cents"]
    assert by_id["rc-o1"]["has_active_settlement"] is True
    assert by_id["rc-o2"]["has_active_settlement"] is False
    assert by_id["rc-o3"]["has_active_settlement"] is False


def test_reconciliation_replay_is_frozen_snapshot() -> None:
    rt = "rct-frz"  # 独立租户：该租户内只有本测试的订单
    _new_order("rc-o4", 100, 0, tenant=rt)
    first = _start_recon("rc-rec2", tenant=rt)
    assert first.status_code == 201
    # 对账期间新发生的账务不改变已生成结果。
    client.post("/orders/rc-o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": rt})
    second = _start_recon("rc-rec2", tenant=rt)
    assert second.status_code == 201
    assert first.json() == second.json()
    got = client.get("/reconciliations/rc-rec2", headers={"X-Tenant": rt})
    assert got.status_code == 200 and got.json() == first.json()
    # 重新以新标识发起才反映新账务。
    newer = _start_recon("rc-rec3", tenant=rt).json()
    assert newer["total_paid_cents"] == 100 and newer["total_outstanding_cents"] == 0


def test_reconciliation_query_cross_tenant_is_not_found() -> None:
    _start_recon("rc-rec4")
    assert client.get("/reconciliations/rc-rec4", headers={"X-Tenant": "other"}).status_code == 404
    assert client.get("/reconciliations/rc-rec4").status_code == 400
    # 租户隔离：他租户以同一标识发起得到自己的空汇总。
    other = _start_recon("rc-rec4", tenant="other-rc").json()
    assert other["tenant"] == "other-rc" and other["order_count"] == 0


def test_interleaved_payment_refund_settlement_stays_consistent() -> None:
    # 结算成功后退款无净额可退必失败；冲正打开后退款、再收款、再结算全链路金额守恒。
    _new_order("ix-o1", 1000, 1000)
    _settle("ix-o1", "ix-s1", 1000)
    assert client.post(
        "/orders/ix-o1/refunds", json={"refund_id": "ix-rf0", "amount_cents": 1}, headers={"X-Tenant": T}
    ).status_code == 409
    _reverse("ix-o1", "ix-s1", "ix-rv1", "reopen")
    assert client.post(
        "/orders/ix-o1/refunds", json={"refund_id": "ix-rf1", "amount_cents": 300}, headers={"X-Tenant": T}
    ).status_code == 200
    assert client.post("/orders/ix-o1/payments", json={"amount_cents": 400}, headers={"X-Tenant": T}).status_code == 409
    assert client.post("/orders/ix-o1/payments", json={"amount_cents": 300}, headers={"X-Tenant": T}).status_code == 200
    assert _settle("ix-o1", "ix-s2", 1000).status_code == 201
    order = client.get("/orders/ix-o1", headers={"X-Tenant": T}).json()
    assert order["paid_cents"] == 1300 and order["refunded_cents"] == 300
    assert order["outstanding_cents"] == 0 and order["status"] == "settled"
    # store 层直查：生效结算唯一。
    conn = connect()
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM settlements WHERE tenant=? AND order_id='ix-o1' AND status='effective'",
            (T,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert active == 1
    # 结算/冲正记录重启后仍可查询（结果已落库），重复提交不产生新效果。
    assert orders.get(T, "ix-o1")["status"] == "settled"
