import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import payment_reversals
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

T = "pr"


def _new_order(oid: str, amount: int, paid: int = 0, tenant: str = T):
    client.post("/orders", json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"})
    if paid:
        return client.post(f"/orders/{oid}/payments", json={"amount_cents": paid}, headers={"X-Tenant": tenant})
    return None


def _pay(oid: str, amount: int, tenant: str = T):
    return client.post(f"/orders/{oid}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})


def _reverse(oid: str, rid: str, payment_ref: str, amount: int, tenant: str = T):
    return client.post(
        f"/orders/{oid}/payments/reversals",
        json={"reversal_id": rid, "payment_ref": payment_ref, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


def _refund(oid: str, rid: str, amount: int, tenant: str = T):
    return client.post(
        f"/orders/{oid}/refunds",
        json={"refund_id": rid, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


def _debts(oid: str, tenant: str = T):
    return client.get(f"/orders/{oid}/debts", headers={"X-Tenant": tenant})


def _order(oid: str, tenant: str = T):
    return client.get(f"/orders/{oid}", headers={"X-Tenant": tenant}).json()


# ---------- 基本冲回 ----------

def test_full_reversal_returns_snapshot_and_reverts_accounts() -> None:
    _new_order("pr-o1", 1000, paid=600)
    res = _reverse("pr-o1", "pr-r1", "pay-1", 600)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reversal_id"] == "pr-r1"
    assert body["payment_ref"] == "pay-1"
    assert body["amount_cents"] == 600
    assert body["payment_net_cents"] == 0
    assert body["paid_cents"] == 0
    assert body["refunded_cents"] == 0
    assert body["outstanding_cents"] == 1000
    assert body["status"] == "accepted"
    order = _order("pr-o1")
    assert order["paid_cents"] == 0 and order["refunded_cents"] == 0
    assert order["outstanding_cents"] == 1000 and order["status"] == "accepted"


def test_partial_reversal_keeps_net_and_outstanding() -> None:
    _new_order("pr-o2", 1000, paid=600)
    res = _reverse("pr-o2", "pr-r2", "pay-1", 200)
    assert res.status_code == 200
    body = res.json()
    assert body["payment_net_cents"] == 400
    assert body["paid_cents"] == 400 and body["outstanding_cents"] == 600
    assert body["status"] == "accepted"
    order = _order("pr-o2")
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 600


def test_reversal_does_not_change_refunded_amount() -> None:
    _new_order("pr-o3", 1000, paid=1000)
    _refund("pr-o3", "pr-rf3", 300)
    # 当前已收 1000、已退 300、未收 300；撤销其中一笔收款 200，已退保持 300。
    res = _reverse("pr-o3", "pr-r3", "pay-1", 200)
    assert res.status_code == 200
    body = res.json()
    assert body["paid_cents"] == 800 and body["refunded_cents"] == 300
    assert body["outstanding_cents"] == 500
    assert body["status"] == "accepted"
    # 守恒：订单金额 = 已收 − 已退 + 未收
    assert 1000 == 800 - 300 + 500


def test_fully_paid_order_reversal_reopens_to_accepted() -> None:
    # 无生效结算记录、仅因全额收款进入 settled 的历史订单，撤销令未收 > 0 即回到 accepted。
    _new_order("pr-o4", 1000, paid=1000)
    assert _order("pr-o4")["status"] == "settled"
    res = _reverse("pr-o4", "pr-r4", "pay-1", 1)
    assert res.status_code == 200
    assert res.json()["outstanding_cents"] == 1 and res.json()["status"] == "accepted"


# ---------- 同一收款多次部分撤销 ----------

def test_multiple_partial_reversals_accumulate_and_net_to_zero() -> None:
    _new_order("pr-o5", 1000, paid=1000)
    r1 = _reverse("pr-o5", "pr-r5a", "pay-1", 300)
    assert r1.status_code == 200 and r1.json()["payment_net_cents"] == 700
    r2 = _reverse("pr-o5", "pr-r5b", "pay-1", 500)
    assert r2.status_code == 200 and r2.json()["payment_net_cents"] == 200
    r3 = _reverse("pr-o5", "pr-r5c", "pay-1", 200)
    assert r3.status_code == 200 and r3.json()["payment_net_cents"] == 0
    order = _order("pr-o5")
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 1000


def test_reversal_cumulative_over_net_is_rejected_without_record() -> None:
    _new_order("pr-o6", 1000, paid=600)
    assert _reverse("pr-o6", "pr-r6a", "pay-1", 400).status_code == 200
    # 该笔收款净额仅剩 200，撤销 300 整笔拒绝（409），不产生任何记录。
    res = _reverse("pr-o6", "pr-r6b", "pay-1", 300)
    assert res.status_code == 409
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM payment_reversals WHERE tenant=? AND reversal_id='pr-r6b'",
            (T,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 0
    order = _order("pr-o6")
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 800


def test_full_then_any_further_reversal_is_conflict() -> None:
    _new_order("pr-o7", 1000, paid=600)
    assert _reverse("pr-o7", "pr-r7a", "pay-1", 600).status_code == 200
    assert _reverse("pr-o7", "pr-r7b", "pay-1", 1).status_code == 409


# ---------- 幂等 ----------

def test_reversal_replay_returns_first_result() -> None:
    _new_order("pr-o8", 1000, paid=600)
    payload = {"reversal_id": "pr-r8", "payment_ref": "pay-1", "amount_cents": 200}
    first = client.post("/orders/pr-o8/payments/reversals", json=payload, headers={"X-Tenant": T})
    # 首次后再收一笔款、再做一笔撤销，订单当前金额已不同于首次快照。
    _pay("pr-o8", 100)
    _reverse("pr-o8", "pr-r8-later", "pay-2", 100)
    second = client.post("/orders/pr-o8/payments/reversals", json=payload, headers={"X-Tenant": T})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM payment_reversals WHERE tenant=? AND reversal_id='pr-r8'",
            (T,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 1


def test_reversal_replay_with_other_payment_ref_is_conflict() -> None:
    _new_order("pr-o9", 1000)
    _pay("pr-o9", 300)
    _pay("pr-o9", 300)
    assert _reverse("pr-o9", "pr-r9", "pay-1", 100).status_code == 200
    assert _reverse("pr-o9", "pr-r9", "pay-2", 100).status_code == 409


def test_reversal_replay_with_other_amount_is_conflict() -> None:
    _new_order("pr-o10", 1000, paid=600)
    assert _reverse("pr-o10", "pr-r10", "pay-1", 100).status_code == 200
    assert _reverse("pr-o10", "pr-r10", "pay-1", 200).status_code == 409


def test_reversal_id_reused_on_another_order_is_conflict() -> None:
    _new_order("pr-o11a", 1000, paid=500)
    _new_order("pr-o11b", 1000, paid=500)
    assert _reverse("pr-o11a", "pr-r11", "pay-1", 100).status_code == 200
    assert _reverse("pr-o11b", "pr-r11", "pay-1", 100).status_code == 409
    order = _order("pr-o11b")
    assert order["paid_cents"] == 500


# ---------- 404 / 400 ----------

def test_reverse_unknown_payment_is_not_found() -> None:
    _new_order("pr-o12", 1000, paid=100)
    assert _reverse("pr-o12", "pr-r12", "pay-9", 10).status_code == 404


def test_reverse_unknown_order_is_not_found() -> None:
    assert _reverse("pr-nope", "pr-r13", "pay-1", 10).status_code == 404


def test_reverse_cross_tenant_is_not_found() -> None:
    _new_order("pr-o14", 1000, paid=100)
    assert _reverse("pr-o14", "pr-r14", "pay-1", 10, tenant="pr-other").status_code == 404


def test_reverse_without_tenant_header_is_bad_request() -> None:
    _new_order("pr-o15", 1000, paid=100)
    res = client.post(
        "/orders/pr-o15/payments/reversals",
        json={"reversal_id": "pr-r15", "payment_ref": "pay-1", "amount_cents": 10},
    )
    assert res.status_code == 400


def test_reverse_invalid_params_are_bad_request() -> None:
    _new_order("pr-o16", 1000, paid=100)
    base = "/orders/pr-o16/payments/reversals"
    h = {"X-Tenant": T}
    assert client.post(base, json={"reversal_id": "", "payment_ref": "pay-1", "amount_cents": 10}, headers=h).status_code == 400
    assert client.post(base, json={"reversal_id": "x", "payment_ref": "", "amount_cents": 10}, headers=h).status_code == 400
    assert client.post(base, json={"reversal_id": "x", "payment_ref": "pay-1", "amount_cents": 0}, headers=h).status_code == 400
    assert client.post(base, json={"reversal_id": "x", "payment_ref": "pay-1", "amount_cents": -1}, headers=h).status_code == 400


# ---------- 欠款核销收窄与守恒 ----------

def test_reversal_narrows_debt_settlements_in_reverse_no_order() -> None:
    # 订单 1000；收款 600 占第 1 条 600；再收 400 占满第 1 条。
    _new_order("pr-o17", 1000)
    _pay("pr-o17", 600)
    _pay("pr-o17", 400)
    # 撤销第一笔 600：按编号逆序从尾部释放 600（全部来自第 1 条，唯一欠款）。
    assert _reverse("pr-o17", "pr-r17", "pay-1", 600).status_code == 200
    debts = _debts("pr-o17").json()["debts"]
    assert [d["settled_cents"] for d in debts] == [400]
    assert debts[0]["remaining_cents"] == 600 and debts[0]["status"] == "unsettled"
    order = _order("pr-o17")
    assert sum(d["settled_cents"] for d in debts) == order["paid_cents"] == 400
    assert sum(d["remaining_cents"] for d in debts) == order["outstanding_cents"] == 600


def test_reversal_release_tail_across_multiple_debts() -> None:
    # 收 800 占第 1 条；退 600 追加第 2 条；补收 600 占第 2 条 600。
    _new_order("pr-o18", 800, paid=800)
    _refund("pr-o18", "pr-rf18", 600)
    _pay("pr-o18", 600)
    debts = _debts("pr-o18").json()["debts"]
    assert [d["settled_cents"] for d in debts] == [800, 600]
    # 撤销最新一笔收款（pay-2, 600）：逆序先释放第 2 条 600。
    assert _reverse("pr-o18", "pr-r18a", "pay-2", 600).status_code == 200
    debts = _debts("pr-o18").json()["debts"]
    assert [d["settled_cents"] for d in debts] == [800, 0]
    assert debts[1]["status"] == "unsettled" and debts[0]["status"] == "settled"
    # 再撤销第一笔收款中的 500：逆序释放——第 2 条已无核销，全部从第 1 条尾部释放 500。
    assert _reverse("pr-o18", "pr-r18b", "pay-1", 500).status_code == 200
    debts = _debts("pr-o18").json()["debts"]
    assert [d["settled_cents"] for d in debts] == [300, 0]
    assert debts[0]["status"] == "unsettled"
    order = _order("pr-o18")
    # 已收 = 800+600-600-500 = 300；已退 600；未收 = 800-300+600 = 1100
    assert order["paid_cents"] == 300 and order["refunded_cents"] == 600
    assert order["outstanding_cents"] == 1100
    assert sum(d["remaining_cents"] for d in debts) == 1100
    assert sum(d["settled_cents"] for d in debts) == 300


def test_reversal_after_writeoff_still_conserves() -> None:
    # 收 800、退 600、补收 300（第 2 条占 300），再把第 2 条核销改配到满：
    # 第 1 条 800 -> 500，第 2 条 300 -> 600。
    _new_order("pr-o19", 800, paid=800)
    _refund("pr-o19", "pr-rf19", 600)
    _pay("pr-o19", 300)
    wo = client.post(
        "/orders/pr-o19/writeoffs",
        json={"writeoff_id": "pr-wo19", "debt_no": 2, "amount_cents": 300},
        headers={"X-Tenant": T},
    )
    assert wo.status_code == 201
    assert [d["settled_cents"] for d in _debts("pr-o19").json()["debts"]] == [500, 600]
    # 撤销补收那笔（pay-2, 300）：逆序从第 2 条尾部释放 300（该条含被改配来的核销）。
    assert _reverse("pr-o19", "pr-r19", "pay-2", 300).status_code == 200
    debts = _debts("pr-o19").json()["debts"]
    assert [d["settled_cents"] for d in debts] == [500, 300]
    order = _order("pr-o19")
    assert sum(d["settled_cents"] for d in debts) == order["paid_cents"] == 800
    assert sum(d["remaining_cents"] for d in debts) == order["outstanding_cents"]
    assert order["refunded_cents"] == 600 and order["outstanding_cents"] == 600


# ---------- 生效结算拦截 ----------

def test_reverse_with_effective_settlement_is_conflict() -> None:
    _new_order("pr-o20", 1000, paid=1000)
    st = client.post(
        "/orders/pr-o20/settlements",
        json={"settlement_id": "pr-s20", "amount_cents": 1000},
        headers={"X-Tenant": T},
    )
    assert st.status_code == 201
    res = _reverse("pr-o20", "pr-r20", "pay-1", 100)
    assert res.status_code == 409
    order = _order("pr-o20")
    assert order["paid_cents"] == 1000 and order["status"] == "settled"


def test_reverse_after_settlement_reversed_succeeds() -> None:
    _new_order("pr-o21", 1000, paid=1000)
    client.post(
        "/orders/pr-o21/settlements",
        json={"settlement_id": "pr-s21", "amount_cents": 1000},
        headers={"X-Tenant": T},
    )
    rv = client.post(
        "/orders/pr-o21/settlements/pr-s21/reversals",
        json={"reversal_id": "pr-rv21", "reason": "fix"},
        headers={"X-Tenant": T},
    )
    assert rv.status_code == 201
    res = _reverse("pr-o21", "pr-r21", "pay-1", 400)
    assert res.status_code == 200
    assert res.json()["paid_cents"] == 600 and res.json()["outstanding_cents"] == 400
    assert res.json()["status"] == "accepted"


# ---------- 撤销后再收款、再退款 ----------

def test_repay_after_reversal_fills_outstanding_and_settles() -> None:
    _new_order("pr-o22", 1000, paid=1000)
    assert _reverse("pr-o22", "pr-r22", "pay-1", 300).status_code == 200
    # 未收 300：只能补收 300，超收仍按既有规则 409。
    assert _pay("pr-o22", 400).status_code == 409
    assert _pay("pr-o22", 300).status_code == 200
    order = _order("pr-o22")
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0
    assert order["status"] == "settled"


def test_refund_after_reversal_is_capped_at_net_paid() -> None:
    _new_order("pr-o23", 1000, paid=1000)
    assert _reverse("pr-o23", "pr-r23", "pay-1", 600).status_code == 200
    # 已收净额仅剩 400，退款 500 被拒；退 400 成功，退回部分重新计入未收。
    assert _refund("pr-o23", "pr-rf23a", 500).status_code == 409
    res = _refund("pr-o23", "pr-rf23b", 400)
    assert res.status_code == 200
    assert res.json() == {"paid_cents": 400, "refunded_cents": 400, "outstanding_cents": 1000}


# ---------- 账务历史 ----------

def test_ledger_appends_reversal_entry_and_replays_to_order() -> None:
    _new_order("pr-o24", 1000)
    _pay("pr-o24", 800)
    assert _reverse("pr-o24", "pr-r24", "pay-1", 300).status_code == 200
    ledger = client.get("/orders/pr-o24/ledger", headers={"X-Tenant": T}).json()["entries"]
    types = [e["type"] for e in ledger]
    assert types == ["payment", "payment_reversal"]
    rev = ledger[-1]
    assert rev["biz_ref"] == "pr-r24" and rev["amount_cents"] == 300
    assert rev["outstanding_cents"] == 500
    order = _order("pr-o24")
    assert rev["outstanding_cents"] == order["outstanding_cents"]
    # 原收款流水仍在，未被删除。
    assert ledger[0]["biz_ref"] == "pay-1" and ledger[0]["amount_cents"] == 800


def test_original_payment_flow_is_not_deleted() -> None:
    _new_order("pr-o25", 1000, paid=600)
    _reverse("pr-o25", "pr-r25", "pay-1", 600)
    conn = connect()
    try:
        pay = conn.execute(
            "SELECT amount_cents, reversed_cents FROM payments WHERE tenant=? AND order_id='pr-o25' AND biz_ref='pay-1'",
            (T,),
        ).fetchone()
        le = conn.execute(
            "SELECT COUNT(*) AS n FROM ledger_entries WHERE tenant=? AND order_id='pr-o25' AND entry_type='payment'",
            (T,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert pay["amount_cents"] == 600 and pay["reversed_cents"] == 600
    assert le == 1


# ---------- 并发 ----------

def test_concurrent_reversals_compete_for_one_payment_net() -> None:
    _new_order("pr-o26", 1000, paid=1000)

    def attempt(rid: str) -> str:
        try:
            result = payment_reversals.reverse(T, "pr-o26", rid, "pay-1", 600)
        except payment_reversals.Conflict:
            return "rejected"
        return "accepted" if result is not None else "not_found"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["pr-r26a", "pr-r26b"]))
    assert outcomes.count("accepted") == 1 and outcomes.count("rejected") == 1
    order = _order("pr-o26")
    assert order["paid_cents"] == 400 and order["outstanding_cents"] == 600


def test_concurrent_same_reversal_id_single_effect() -> None:
    _new_order("pr-o27", 1000, paid=1000)

    def attempt(_: int) -> int:
        res = _reverse("pr-o27", "pr-r27", "pay-1", 200)
        return res.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(attempt, [0, 1]))
    assert codes == [200, 200]
    order = _order("pr-o27")
    assert order["paid_cents"] == 800
