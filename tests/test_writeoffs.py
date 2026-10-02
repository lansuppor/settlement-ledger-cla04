import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

T = "wo"


def _new_order(oid: str, amount: int, paid: int = 0, tenant: str = T) -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"})
    if paid:
        client.post(f"/orders/{oid}/payments", json={"amount_cents": paid}, headers={"X-Tenant": tenant})


def _refund(oid: str, rid: str, amount: int, tenant: str = T):
    return client.post(
        f"/orders/{oid}/refunds", json={"refund_id": rid, "amount_cents": amount}, headers={"X-Tenant": tenant}
    )


def _write_off(oid: str, wid: str, debt_no: int, amount: int, tenant: str = T):
    return client.post(
        f"/orders/{oid}/write-offs",
        json={"write_off_id": wid, "debt_no": debt_no, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


def _items(oid: str, tenant: str = T) -> list[dict]:
    res = client.get(f"/orders/{oid}/debt-items", headers={"X-Tenant": tenant})
    assert res.status_code == 200, res.text
    return res.json()["items"]


# ---------- 欠款条目生成 ----------

def test_order_acceptance_creates_first_debt_item() -> None:
    _new_order("wo-o1", 1000)
    items = _items("wo-o1")
    assert items == [
        {"debt_no": 1, "amount_cents": 1000, "written_off_cents": 0, "outstanding_cents": 1000, "status": "open"}
    ]


def test_refund_appends_debt_item_with_refund_amount() -> None:
    _new_order("wo-o2", 1000, 800)
    assert _refund("wo-o2", "wo-rf1", 300).status_code == 200
    assert _refund("wo-o2", "wo-rf2", 100).status_code == 200
    items = _items("wo-o2")
    assert [(i["debt_no"], i["amount_cents"], i["status"]) for i in items] == [
        (1, 1000, "open"), (2, 300, "open"), (3, 100, "open")
    ]
    # 闭合：Σ欠款金额 = 订单金额 + 已退金额；Σ未核销余额 = 订单未收金额（收款尚未核销时差额即未核销到账）。
    order = client.get("/orders/wo-o2", headers={"X-Tenant": T}).json()
    assert sum(i["amount_cents"] for i in items) == order["amount_cents"] + order["refunded_cents"]


def test_imported_order_also_gets_first_debt_item() -> None:
    res = client.post("/orders/import", json={
        "tenant": T, "batch_id": "wo-b1",
        "rows": [{"order_id": "wo-imp1", "amount_cents": 500, "currency": "CNY"}],
    })
    assert res.status_code == 202
    for _ in range(50):
        batch = client.get("/orders/import/wo-b1", headers={"X-Tenant": T}).json()
        if batch["status"] == "completed":
            break
    assert batch["success_count"] == 1
    items = _items("wo-imp1")
    assert len(items) == 1 and items[0]["debt_no"] == 1 and items[0]["amount_cents"] == 500


# ---------- 核销登记 ----------

def test_write_off_partial_then_full_closes_item() -> None:
    _new_order("wo-o3", 1000, 1000)
    first = _write_off("wo-o3", "wo-w1", 1, 600)
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["write_off_id"] == "wo-w1" and body["debt_no"] == 1 and body["amount_cents"] == 600
    assert body["item_outstanding_cents"] == 400 and body["outstanding_cents"] == 400
    item = _items("wo-o3")[0]
    assert item["written_off_cents"] == 600 and item["outstanding_cents"] == 400 and item["status"] == "open"
    second = _write_off("wo-o3", "wo-w2", 1, 400)
    assert second.status_code == 201
    assert second.json()["outstanding_cents"] == 0
    item = _items("wo-o3")[0]
    assert item["written_off_cents"] == 1000 and item["outstanding_cents"] == 0 and item["status"] == "closed"
    # 核销不改变订单的已收、已退、未收金额与订单状态。
    order = client.get("/orders/wo-o3", headers={"X-Tenant": T}).json()
    assert order["paid_cents"] == 1000 and order["refunded_cents"] == 0
    assert order["outstanding_cents"] == 0 and order["status"] == "settled"


def test_write_off_appends_ledger_entry_with_order_unwritten_total() -> None:
    _new_order("wo-o4", 1000, 500)
    _refund("wo-o4", "wo-rf4", 200)
    client.post("/orders/wo-o4/payments", json={"amount_cents": 200}, headers={"X-Tenant": T})
    # 欠款条目：#1 = 1000、#2 = 200；核销 #2 的 200 后订单仍未核销合计 = 1000。
    res = _write_off("wo-o4", "wo-w4", 2, 200)
    assert res.status_code == 201
    assert res.json()["outstanding_cents"] == 1000
    entries = client.get("/orders/wo-o4/ledger", headers={"X-Tenant": T}).json()["entries"]
    last = entries[-1]
    assert last["type"] == "write_off" and last["biz_ref"] == "wo-w4"
    assert last["amount_cents"] == 200 and last["outstanding_cents"] == 1000


def test_write_off_replay_returns_first_result_without_double_effect() -> None:
    _new_order("wo-o5", 800, 800)
    payload = {"write_off_id": "wo-w5", "debt_no": 1, "amount_cents": 300}
    first = client.post("/orders/wo-o5/write-offs", json=payload, headers={"X-Tenant": T})
    second = client.post("/orders/wo-o5/write-offs", json=payload, headers={"X-Tenant": T})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert _items("wo-o5")[0]["written_off_cents"] == 300
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM write_offs WHERE tenant=? AND write_off_id='wo-w5'", (T,)
        ).fetchone()["n"]
        led = conn.execute(
            "SELECT COUNT(*) AS n FROM ledger_entries WHERE tenant=? AND order_id='wo-o5' AND entry_type='write_off'",
            (T,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1 and led == 1


def test_write_off_id_reused_with_different_target_is_conflicted() -> None:
    _new_order("wo-o6a", 500, 500)
    _new_order("wo-o6b", 500, 500)
    assert _write_off("wo-o6a", "wo-w6", 1, 200).status_code == 201
    assert _write_off("wo-o6b", "wo-w6", 1, 200).status_code == 409   # 换订单
    assert _write_off("wo-o6a", "wo-w6", 1, 300).status_code == 409   # 换金额
    _refund("wo-o6a", "wo-rf6", 100)
    assert _write_off("wo-o6a", "wo-w6", 2, 100).status_code == 409   # 换欠款编号
    # 冲突提交不产生任何核销效果。
    assert _items("wo-o6a")[0]["written_off_cents"] == 200


def test_write_off_exceeding_balance_is_rejected_without_trace() -> None:
    _new_order("wo-o7", 500, 500)
    assert _write_off("wo-o7", "wo-w7a", 1, 501).status_code == 409
    assert _write_off("wo-o7", "wo-w7b", 1, 200).status_code == 201
    assert _write_off("wo-o7", "wo-w7c", 1, 301).status_code == 409
    # 整笔拒绝：条目余额、核销登记与账务历史均无半截记录。
    assert _items("wo-o7")[0]["written_off_cents"] == 200
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM write_offs WHERE tenant=? AND write_off_id IN ('wo-w7a','wo-w7c')", (T,)
        ).fetchone()["n"]
        led = conn.execute(
            "SELECT COUNT(*) AS n FROM ledger_entries WHERE tenant=? AND order_id='wo-o7' AND entry_type='write_off'",
            (T,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 0 and led == 1


def test_write_off_unknown_or_cross_tenant_is_not_found() -> None:
    _new_order("wo-o8", 100, 100)
    assert _write_off("wo-missing", "wo-w8a", 1, 50).status_code == 404
    assert _write_off("wo-o8", "wo-w8b", 9, 50).status_code == 404          # 欠款编号不存在
    assert _write_off("wo-o8", "wo-w8c", 1, 50, tenant="other").status_code == 404
    assert client.get("/orders/wo-o8/debt-items", headers={"X-Tenant": "other"}).status_code == 404
    assert client.get("/orders/wo-nope/debt-items", headers={"X-Tenant": T}).status_code == 404
    assert client.get("/orders/wo-o8/debt-items").status_code == 400
    # 跨租户失败不占用该核销标识在本租户的使用。
    assert _write_off("wo-o8", "wo-w8c", 1, 50).status_code == 201


def test_write_off_rejected_while_settlement_effective() -> None:
    _new_order("wo-o9", 400, 400)
    client.post("/orders/wo-o9/settlements", json={"settlement_id": "wo-s9", "amount_cents": 400}, headers={"X-Tenant": T})
    assert _write_off("wo-o9", "wo-w9", 1, 400).status_code == 409
    assert _items("wo-o9")[0]["written_off_cents"] == 0
    # 冲正结算后可正常核销。
    client.post(
        "/orders/wo-o9/settlements/wo-s9/reversals",
        json={"reversal_id": "wo-rv9", "reason": "reopen"}, headers={"X-Tenant": T},
    )
    assert _write_off("wo-o9", "wo-w9", 1, 400).status_code == 201


def test_concurrent_write_offs_on_same_item_only_one_succeeds() -> None:
    _new_order("wo-o10", 1000, 1000)

    def attempt(wid: str) -> int:
        return _write_off("wo-o10", wid, 1, 700).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(attempt, ["wo-w10a", "wo-w10b"]))
    assert sorted(codes) == [201, 409]
    item = _items("wo-o10")[0]
    # 任何情况下同一条目的已核销金额之和不超过其欠款金额。
    assert item["written_off_cents"] == 700 and item["outstanding_cents"] == 300


# ---------- 查询与闭合 ----------

def test_closure_invariants_across_payment_writeoff_flow() -> None:
    _new_order("wo-o11", 1200)
    client.post("/orders/wo-o11/payments", json={"amount_cents": 700}, headers={"X-Tenant": T})
    _refund("wo-o11", "wo-rf11", 200)
    client.post("/orders/wo-o11/payments", json={"amount_cents": 500}, headers={"X-Tenant": T})
    # 已收 1200、已退 200、未收 200；欠款条目 #1 = 1200、#2 = 200。
    # 到账 1200 逐笔核销：#1 核销 1000、#2 核销 200。
    assert _write_off("wo-o11", "wo-w11a", 1, 1000).status_code == 201
    assert _write_off("wo-o11", "wo-w11b", 2, 200).status_code == 201
    order = client.get("/orders/wo-o11", headers={"X-Tenant": T}).json()
    items = _items("wo-o11")
    # 欠款编号 1 的金额 = 受理时订单金额；Σ欠款金额 = 订单金额 + 已退；Σ未核销余额 = 订单未收。
    assert items[0]["amount_cents"] == 1200
    assert sum(i["amount_cents"] for i in items) == order["amount_cents"] + order["refunded_cents"]
    assert sum(i["outstanding_cents"] for i in items) == order["outstanding_cents"] == 200
    assert [i["status"] for i in items] == ["open", "closed"]


def test_write_off_does_not_affect_order_search_reconciliation_or_tickets() -> None:
    _new_order("wo-o12", 600, 600)
    _write_off("wo-o12", "wo-w12", 1, 600)
    # 条件检索结果不受核销影响。
    found = client.get("/orders?order_id_prefix=wo-o12", headers={"X-Tenant": T}).json()["orders"]
    assert len(found) == 1 and found[0]["paid_cents"] == 600 and found[0]["outstanding_cents"] == 0
    # 对账汇总不受核销影响。
    recon = client.post("/reconciliations", json={"tenant": "wo-rc", "reconciliation_id": "wo-rec1"})
    assert recon.status_code == 201 and recon.json()["order_count"] == 0
    # 工单登记与处理不受核销影响。
    tk = client.post("/tickets", json={
        "tenant": T, "ticket_id": "wo-tk1", "order_id": "wo-o12",
        "ticket_type": "payment", "description": "核销后工单链路正常",
    })
    assert tk.status_code == 201
    assert client.post(
        "/tickets/wo-tk1/process", json={"status": "resolved", "note": "ok"}, headers={"X-Tenant": T}
    ).status_code == 200


def test_write_off_result_persisted_and_replayable_after_reconnect() -> None:
    # 结果落库：换连接直查仍在，重复提交不产生新的核销效果（重启语义等价于新连接读取）。
    _new_order("wo-o13", 300, 300)
    first = _write_off("wo-o13", "wo-w13", 1, 300)
    assert first.status_code == 201
    conn = connect()
    try:
        row = conn.execute(
            "SELECT order_id, debt_no, amount_cents FROM write_offs WHERE tenant=? AND write_off_id='wo-w13'", (T,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row["order_id"] == "wo-o13" and row["debt_no"] == 1 and row["amount_cents"] == 300
    again = _write_off("wo-o13", "wo-w13", 1, 300)
    assert again.status_code == 201 and again.json() == first.json()
    assert _items("wo-o13")[0]["written_off_cents"] == 300
