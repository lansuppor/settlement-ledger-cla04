import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from app.entry import app
from app.store import settlements
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

def make_order(order_id: str, amount: int, tenant: str = "t1") -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": order_id, "amount_cents": amount, "currency": "CNY"})

def pay(order_id: str, amount: int, tenant: str = "t1"):
    return client.post(f"/orders/{order_id}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})

def settle(order_id: str, settlement_id: str, amount: int, tenant: str = "t1"):
    return client.post(
        f"/orders/{order_id}/settlements",
        json={"settlement_id": settlement_id, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )

def reverse(order_id: str, reversal_id: str, reason: str = "纠错", tenant: str = "t1"):
    return client.post(
        f"/orders/{order_id}/reversals",
        json={"reversal_id": reversal_id, "reason": reason},
        headers={"X-Tenant": tenant},
    )

def test_settle_success_marks_order_settled() -> None:
    make_order("s1", 1000)
    pay("s1", 1000)
    res = settle("s1", "st-1", 1000)
    assert res.status_code == 200
    body = res.json()
    assert body["settlement_id"] == "st-1" and body["status"] == "settled"
    assert body["amount_cents"] == 1000 and body["paid_cents"] == 1000
    assert body["refunded_cents"] == 0 and body["outstanding_cents"] == 0
    assert client.get("/orders/s1", headers={"X-Tenant": "t1"}).json()["status"] == "settled"

def test_settle_with_outstanding_is_rejected_without_side_effects() -> None:
    make_order("s2", 1000)
    pay("s2", 400)
    res = settle("s2", "st-2", 1000)
    assert res.status_code == 409
    order = client.get("/orders/s2", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 400 and order["status"] == "accepted"
    # 未产生任何结算记录：收齐后用同一标识可正常结算。
    pay("s2", 600)
    assert settle("s2", "st-2", 1000).status_code == 200

def test_settle_replay_returns_first_result() -> None:
    make_order("s3", 800)
    pay("s3", 800)
    first = settle("s3", "st-3", 800)
    second = settle("s3", "st-3", 800)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM settlements WHERE tenant='t1' AND settlement_id='st-3'").fetchone()["n"]
    finally:
        conn.close()
    assert count == 1

def test_settle_id_reused_with_other_order_or_amount_is_rejected() -> None:
    make_order("s4a", 500)
    make_order("s4b", 500)
    pay("s4a", 500)
    pay("s4b", 500)
    assert settle("s4a", "st-4", 500).status_code == 200
    assert settle("s4b", "st-4", 500).status_code == 409
    assert settle("s4a", "st-4", 400).status_code == 409

def test_settle_missing_or_cross_tenant_is_not_found() -> None:
    make_order("s5", 500)
    pay("s5", 500)
    assert settle("s5", "st-5", 500, tenant="t2").status_code == 404
    assert settle("no-such-order", "st-5b", 500).status_code == 404

def test_second_active_settlement_is_rejected() -> None:
    make_order("s6", 500)
    pay("s6", 500)
    assert settle("s6", "st-6a", 500).status_code == 200
    assert settle("s6", "st-6b", 500).status_code == 409

def test_reverse_voids_settlement_and_restores_unsettled() -> None:
    make_order("v1", 900)
    pay("v1", 900)
    settle("v1", "st-v1", 900)
    res = reverse("v1", "rv-1", "结算金额有误")
    assert res.status_code == 200
    body = res.json()
    assert body["settlement_id"] == "st-v1" and body["reason"] == "结算金额有误"
    assert body["status"] == "accepted" and body["outstanding_cents"] == 0
    assert client.get("/orders/v1", headers={"X-Tenant": "t1"}).json()["status"] == "accepted"

def test_reverse_replay_returns_first_result_and_double_reverse_is_rejected() -> None:
    make_order("v2", 700)
    pay("v2", 700)
    settle("v2", "st-v2", 700)
    first = reverse("v2", "rv-2", "重复冲正测试")
    second = reverse("v2", "rv-2", "重复冲正测试")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    # 对同一结算换标识再次冲正：结算已作废，拒绝。
    assert reverse("v2", "rv-2b", "再次冲正").status_code == 409
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS n FROM reversals WHERE tenant='t1' AND reversal_id='rv-2'").fetchone()["n"]
    finally:
        conn.close()
    assert count == 1

def test_reverse_without_settlement_is_rejected() -> None:
    make_order("v3", 700)
    pay("v3", 700)
    assert reverse("v3", "rv-3", "无结算可冲").status_code == 409
    assert reverse("no-such-order", "rv-3b", "不存在").status_code == 404
    assert reverse("v3", "rv-3c", "跨租户", tenant="t2").status_code == 404

def test_reversal_and_settlement_ids_are_independent_namespaces() -> None:
    make_order("v4", 600)
    pay("v4", 600)
    settle("v4", "shared-id", 600)
    # 冲正标识与结算标识相同也互不影响。
    assert reverse("v4", "shared-id", "同标识不冲突").status_code == 200

def test_resettle_after_reverse_keeps_single_active_settlement() -> None:
    make_order("v5", 600)
    pay("v5", 600)
    settle("v5", "st-v5a", 600)
    reverse("v5", "rv-5", "冲正后重新结算")
    assert settle("v5", "st-v5b", 600).status_code == 200
    assert client.get("/orders/v5", headers={"X-Tenant": "t1"}).json()["status"] == "settled"
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT settlement_id, status FROM settlements WHERE tenant='t1' AND order_id='v5' ORDER BY created_at, settlement_id"
        ).fetchall()
    finally:
        conn.close()
    # 历史记录保留可查，任一时刻至多一条生效。
    assert [(row["settlement_id"], row["status"]) for row in rows] == [("st-v5a", "reversed"), ("st-v5b", "active")]

def test_ledger_history_explains_order_state() -> None:
    make_order("h1", 1000)
    pay("h1", 600)
    client.post("/orders/h1/refunds", json={"refund_id": "rf-h1", "amount_cents": 200}, headers={"X-Tenant": "t1"})
    pay("h1", 600)
    settle("h1", "st-h1", 1000)
    reverse("h1", "rv-h1", "历史回放")
    settle("h1", "st-h1b", 1000)
    res = client.get("/orders/h1/ledger", headers={"X-Tenant": "t1"})
    assert res.status_code == 200
    entries = res.json()["entries"]
    assert [e["type"] for e in entries] == ["payment", "refund", "payment", "settlement", "reversal", "settlement"]
    assert [e["outstanding_after"] for e in entries] == [400, 600, 0, 0, 0, 0]
    assert entries[1]["entry_id"] == "rf-h1" and entries[3]["entry_id"] == "st-h1"
    # 重放同一序列：从订单金额出发，收款减未收、退款加回未收，最终与当前订单一致。
    outstanding = 1000
    for entry in entries:
        if entry["type"] == "payment":
            outstanding -= entry["amount_cents"]
        elif entry["type"] == "refund":
            outstanding += entry["amount_cents"]
        assert outstanding == entry["outstanding_after"]
    order = client.get("/orders/h1", headers={"X-Tenant": "t1"}).json()
    assert outstanding == order["outstanding_cents"]
    # 状态由最后一对结算/冲正决定：最后是结算，当前为已结算。
    assert order["status"] == "settled"

def test_ledger_history_cross_tenant_is_not_found() -> None:
    make_order("h2", 100)
    assert client.get("/orders/h2/ledger", headers={"X-Tenant": "t2"}).status_code == 404
    assert client.get("/orders/no-such/ledger", headers={"X-Tenant": "t1"}).status_code == 404

def test_reconciliation_snapshot_and_conservation() -> None:
    make_order("rc1", 1000, tenant="tr")
    make_order("rc2", 500, tenant="tr")
    pay("rc1", 1000, tenant="tr")
    settle("rc1", "st-rc1", 1000, tenant="tr")
    pay("rc2", 200, tenant="tr")
    client.post(
        "/orders/rc2/refunds", json={"refund_id": "rf-rc2", "amount_cents": 50}, headers={"X-Tenant": "tr"}
    )
    res = client.post("/reconciliations", json={"tenant": "tr", "reconciliation_id": "rc-1"})
    assert res.status_code == 201
    body = res.json()
    assert body["order_count"] == 2
    assert body["amount_cents"] == 1500 and body["paid_cents"] == 1200
    assert body["refunded_cents"] == 50 and body["outstanding_cents"] == 350
    # 守恒：应收 = 已收 − 已退 + 未收。
    assert body["amount_cents"] == body["paid_cents"] - body["refunded_cents"] + body["outstanding_cents"]
    by_id = {row["order_id"]: row for row in body["orders"]}
    assert by_id["rc1"]["has_active_settlement"] is True
    assert by_id["rc2"]["has_active_settlement"] is False
    # 逐张订单金额与订单读取接口一致。
    for order_id in ("rc1", "rc2"):
        order = client.get(f"/orders/{order_id}", headers={"X-Tenant": "tr"}).json()
        row = by_id[order_id]
        assert row["amount_cents"] == order["amount_cents"] and row["paid_cents"] == order["paid_cents"]
        assert row["refunded_cents"] == order["refunded_cents"]
        assert row["outstanding_cents"] == order["outstanding_cents"]

def test_reconciliation_replay_and_snapshot_isolation() -> None:
    make_order("rc3", 400, tenant="tr2")
    pay("rc3", 100, tenant="tr2")
    first = client.post("/reconciliations", json={"tenant": "tr2", "reconciliation_id": "rc-2"})
    assert first.status_code == 201
    # 对账之后新发生的账务不改变已生成结果。
    pay("rc3", 300, tenant="tr2")
    replay = client.post("/reconciliations", json={"tenant": "tr2", "reconciliation_id": "rc-2"})
    assert replay.json() == first.json()
    got = client.get("/reconciliations/rc-2", headers={"X-Tenant": "tr2"})
    assert got.status_code == 200 and got.json() == first.json()
    assert got.json()["paid_cents"] == 100
    # 重新发起（新标识）才反映新账务。
    fresh = client.post("/reconciliations", json={"tenant": "tr2", "reconciliation_id": "rc-3"})
    assert fresh.json()["paid_cents"] == 400

def test_reconciliation_cross_tenant_is_not_found() -> None:
    client.post("/reconciliations", json={"tenant": "tr3", "reconciliation_id": "rc-9"})
    assert client.get("/reconciliations/rc-9", headers={"X-Tenant": "other"}).status_code == 404
    assert client.get("/reconciliations/no-such", headers={"X-Tenant": "tr3"}).status_code == 404

def test_concurrent_settlements_leave_single_active_record() -> None:
    make_order("c1", 500)
    pay("c1", 500)

    def attempt(sid: str) -> str:
        try:
            settlements.settle("t1", "c1", sid, 500)
        except ValueError:
            return "rejected"
        return "settled"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["st-c1a", "st-c1b"]))
    assert outcomes.count("settled") == 1 and outcomes.count("rejected") == 1
    conn = connect()
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM settlements WHERE tenant='t1' AND order_id='c1' AND status='active'"
        ).fetchone()["n"]
    finally:
        conn.close()
    assert active == 1

def test_concurrent_payment_and_settlement_never_corrupt_amounts() -> None:
    make_order("c2", 1000)
    pay("c2", 400)

    def do_payment() -> str:
        res = pay("c2", 600)
        return "ok" if res.status_code == 200 else "rejected"

    def do_settlement() -> str:
        try:
            settlements.settle("t1", "c2", "st-c2", 1000)
        except ValueError:
            return "rejected"
        return "settled"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda fn: fn(), [do_payment, do_settlement]))
    order = client.get("/orders/c2", headers={"X-Tenant": "t1"}).json()
    # 无论交错顺序如何，金额守恒且状态与生效结算记录一致。
    assert order["amount_cents"] == order["paid_cents"] - order["refunded_cents"] + order["outstanding_cents"]
    conn = connect()
    try:
        active = conn.execute(
            "SELECT COUNT(*) AS n FROM settlements WHERE tenant='t1' AND order_id='c2' AND status='active'"
        ).fetchone()["n"]
    finally:
        conn.close()
    if "settled" in outcomes:
        assert active == 1 and order["status"] == "settled" and order["outstanding_cents"] == 0
    else:
        assert active == 0
