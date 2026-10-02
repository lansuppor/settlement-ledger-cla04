import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import payments
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

T = "pr"
H = {"X-Tenant": T}


def _new_order(oid: str, amount: int, tenant: str = T):
    return client.post(
        "/orders",
        json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"},
    )


def _pay(oid: str, amount: int, tenant: str = T):
    return client.post(f"/orders/{oid}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})


def _refund(oid: str, rid: str, amount: int, tenant: str = T):
    return client.post(
        f"/orders/{oid}/refunds",
        json={"refund_id": rid, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


def _reverse(oid: str, rid: str, pref: str, amount: int, tenant: str = T):
    return client.post(
        f"/orders/{oid}/payments/reversals",
        json={"reversal_id": rid, "payment_ref": pref, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


def _writeoff(oid: str, wid: str, debt_no: int, amount: int):
    return client.post(
        f"/orders/{oid}/writeoffs",
        json={"writeoff_id": wid, "debt_no": debt_no, "amount_cents": amount},
        headers=H,
    )


def _order(oid: str, tenant: str = T):
    return client.get(f"/orders/{oid}", headers={"X-Tenant": tenant}).json()


def _debts(oid: str, tenant: str = T):
    return client.get(f"/orders/{oid}/debts", headers={"X-Tenant": tenant}).json()["debts"]


def _assert_invariants(oid: str) -> None:
    order = _order(oid)
    debts = _debts(oid)
    assert sum(d["amount_cents"] for d in debts) == order["amount_cents"] + order["refunded_cents"]
    assert sum(d["settled_cents"] for d in debts) == order["paid_cents"]
    assert sum(d["remaining_cents"] for d in debts) == order["outstanding_cents"]
    assert order["amount_cents"] == order["paid_cents"] - order["refunded_cents"] + order["outstanding_cents"]
    for d in debts:
        assert 0 <= d["settled_cents"] <= d["amount_cents"]
        assert d["status"] == ("settled" if d["remaining_cents"] == 0 else "unsettled")


# ---------- 基本冲回 ----------

def test_full_reversal_reduces_paid_and_reopens_order() -> None:
    _new_order("pr-o1", 1000)
    _pay("pr-o1", 1000)
    res = _reverse("pr-o1", "pr-rv1", "pay-1", 1000)
    assert res.status_code == 200
    assert res.json() == {
        "reversal_id": "pr-rv1",
        "payment_ref": "pay-1",
        "amount_cents": 1000,
        "payment_net_cents": 0,
        "paid_cents": 0,
        "refunded_cents": 0,
        "outstanding_cents": 1000,
        "status": "accepted",
        "created_at": res.json()["created_at"],
    }
    order = _order("pr-o1")
    assert order["paid_cents"] == 0 and order["refunded_cents"] == 0
    assert order["outstanding_cents"] == 1000 and order["status"] == "accepted"
    _assert_invariants("pr-o1")


def test_partial_reversal_returns_net_and_split_amounts() -> None:
    _new_order("pr-o2", 1000)
    _pay("pr-o2", 1000)
    res = _reverse("pr-o2", "pr-rv2", "pay-1", 300)
    assert res.status_code == 200
    body = res.json()
    assert body["amount_cents"] == 300 and body["payment_net_cents"] == 700
    assert body["paid_cents"] == 700 and body["refunded_cents"] == 0
    assert body["outstanding_cents"] == 300 and body["status"] == "accepted"
    # 已退金额不受撤销影响。
    _assert_invariants("pr-o2")


def test_multiple_partial_reversals_accumulate_until_net_zero() -> None:
    _new_order("pr-o3", 1000)
    _pay("pr-o3", 600)
    _pay("pr-o3", 400)
    r1 = _reverse("pr-o3", "pr-rv3a", "pay-1", 200)
    r2 = _reverse("pr-o3", "pr-rv3b", "pay-1", 300)
    assert r1.json()["payment_net_cents"] == 400
    assert r2.json()["payment_net_cents"] == 100
    r3 = _reverse("pr-o3", "pr-rv3c", "pay-1", 100)
    assert r3.status_code == 200 and r3.json()["payment_net_cents"] == 0
    # pay-1 已全部撤销；再撤销同一笔（哪怕 1 分）整笔拒绝。
    rejected = _reverse("pr-o3", "pr-rv3d", "pay-1", 1)
    assert rejected.status_code == 409
    _assert_invariants("pr-o3")


def test_reversal_over_net_is_rejected_without_any_record() -> None:
    _new_order("pr-o4", 1000)
    _pay("pr-o4", 500)
    assert _reverse("pr-o4", "pr-rv4", "pay-1", 600).status_code == 409
    order = _order("pr-o4")
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 500
    conn = connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM payment_reversals WHERE tenant=? AND reversal_id='pr-rv4'", (T,)
        ).fetchone()["n"] == 0
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM ledger_entries"
            " WHERE tenant=? AND order_id='pr-o4' AND entry_type='payment_reversal'", (T,)
        ).fetchone()["n"] == 0
        net = conn.execute(
            "SELECT amount_cents - reversed_cents AS net FROM payments"
            " WHERE tenant=? AND order_id='pr-o4' AND payment_ref='pay-1'", (T,)
        ).fetchone()["net"]
    finally:
        conn.close()
    assert net == 500
    _assert_invariants("pr-o4")


# ---------- 幂等 ----------

def test_reversal_replay_returns_first_result() -> None:
    _new_order("pr-o5", 1000)
    _pay("pr-o5", 1000)
    payload = {"reversal_id": "pr-rv5", "payment_ref": "pay-1", "amount_cents": 200}
    first = client.post("/orders/pr-o5/payments/reversals", json=payload, headers=H)
    second = client.post("/orders/pr-o5/payments/reversals", json=payload, headers=H)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM payment_reversals WHERE tenant=? AND reversal_id='pr-rv5'", (T,)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert count == 1
    assert _order("pr-o5")["paid_cents"] == 800


def test_same_reversal_id_with_other_amount_or_payment_is_conflicted() -> None:
    _new_order("pr-o6", 1000)
    _pay("pr-o6", 600)
    _pay("pr-o6", 400)
    assert _reverse("pr-o6", "pr-rv6", "pay-1", 100).status_code == 200
    assert _reverse("pr-o6", "pr-rv6", "pay-1", 200).status_code == 409
    assert _reverse("pr-o6", "pr-rv6", "pay-2", 100).status_code == 409
    # 被拒绝的两次提交不产生任何冲回。
    assert _order("pr-o6")["paid_cents"] == 900


def test_same_reversal_id_on_another_order_is_conflicted() -> None:
    _new_order("pr-o7a", 1000)
    _new_order("pr-o7b", 1000)
    _pay("pr-o7a", 1000)
    _pay("pr-o7b", 1000)
    assert _reverse("pr-o7a", "pr-rv7", "pay-1", 100).status_code == 200
    assert _reverse("pr-o7b", "pr-rv7", "pay-1", 100).status_code == 409
    assert _order("pr-o7b")["paid_cents"] == 1000


# ---------- 欠款占用尾部释放 ----------

def test_tail_release_runs_backwards_across_debt_entries() -> None:
    # 订单 1000：收 600（pay-1 占第 1 条 600）；退 300（第 2 条）；
    # 补收 500（pay-2 跨第 1 条 400 + 第 2 条 100）。
    _new_order("pr-o8", 1000)
    _pay("pr-o8", 600)
    _refund("pr-o8", "pr-rf8", 300)
    _pay("pr-o8", 500)
    debts = _debts("pr-o8")
    assert [d["settled_cents"] for d in debts] == [1000, 100]
    # 撤销 pay-2 的 300：先释放第 2 条的 100，再从第 1 条尾部释放 200。
    res = _reverse("pr-o8", "pr-rv8", "pay-2", 300)
    assert res.status_code == 200
    debts = _debts("pr-o8")
    assert debts[0]["settled_cents"] == 800 and debts[0]["status"] == "unsettled"
    assert debts[1]["settled_cents"] == 0 and debts[1]["status"] == "unsettled"
    order = _order("pr-o8")
    assert order["paid_cents"] == 800 and order["refunded_cents"] == 300
    assert order["outstanding_cents"] == 500
    _assert_invariants("pr-o8")


def test_settled_debt_keeps_status_while_balance_remains_zero() -> None:
    # pay-2 只占用第 2 条；撤销后第 1 条保持已核销，第 2 条回到未核销。
    _new_order("pr-o9", 1000)
    _pay("pr-o9", 1000)
    _refund("pr-o9", "pr-rf9", 600)
    _pay("pr-o9", 300)
    debts = _debts("pr-o9")
    assert [d["settled_cents"] for d in debts] == [1000, 300]
    assert _reverse("pr-o9", "pr-rv9", "pay-2", 300).status_code == 200
    debts = _debts("pr-o9")
    assert debts[0]["status"] == "settled" and debts[0]["settled_cents"] == 1000
    assert debts[1]["status"] == "unsettled" and debts[1]["settled_cents"] == 0
    _assert_invariants("pr-o9")


def test_reversal_after_writeoff_follows_repinned_allocation() -> None:
    # 收 800（pay-1 占第 1 条 800）；退 600（第 2 条）；补收 300（pay-2 占第 2 条）；
    # 再把 300 核销到第 2 条：实际从第 1 条把 pay-1 的 300 改配到第 2 条。
    _new_order("pr-o10", 800)
    _pay("pr-o10", 800)
    _refund("pr-o10", "pr-rf10", 600)
    _pay("pr-o10", 300)
    assert _writeoff("pr-o10", "pr-wo10", 2, 300).status_code == 201
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT payment_ref, debt_no, amount_cents FROM payment_allocations"
            " WHERE tenant=? AND order_id='pr-o10' ORDER BY payment_ref, debt_no", (T,)
        ).fetchall()
        alloc = {(r["payment_ref"], r["debt_no"]): r["amount_cents"] for r in rows}
    finally:
        conn.close()
    # pay-1 的占用随核销迁移：第 1 条 500、第 2 条 300；pay-2 仍在第 2 条。
    assert alloc == {("pay-1", 1): 500, ("pay-1", 2): 300, ("pay-2", 2): 300}
    # 撤销 pay-1 的 300：按尾部释放先解其改配到第 2 条的占用，第 1 条不动。
    assert _reverse("pr-o10", "pr-rv10", "pay-1", 300).status_code == 200
    debts = _debts("pr-o10")
    assert debts[0]["settled_cents"] == 500
    assert debts[1]["settled_cents"] == 300  # 剩 pay-2 的占用
    _assert_invariants("pr-o10")


# ---------- 与结算、退款、再收款相容 ----------

def test_reversal_blocked_while_settlement_effective_and_allowed_after_reversal() -> None:
    _new_order("pr-o11", 1000)
    _pay("pr-o11", 1000)
    settle = client.post(
        "/orders/pr-o11/settlements",
        json={"settlement_id": "pr-s11", "amount_cents": 1000},
        headers=H,
    )
    assert settle.status_code == 201
    res = _reverse("pr-o11", "pr-rv11", "pay-1", 100)
    assert res.status_code == 409
    assert _order("pr-o11")["paid_cents"] == 1000
    client.post(
        "/orders/pr-o11/settlements/pr-s11/reversals",
        json={"reversal_id": "pr-srv11", "reason": "录单错误"},
        headers=H,
    )
    assert _reverse("pr-o11", "pr-rv11", "pay-1", 100).status_code == 200
    _assert_invariants("pr-o11")


def test_refund_after_reversal_is_capped_by_net_paid() -> None:
    _new_order("pr-o12", 1000)
    _pay("pr-o12", 1000)
    _reverse("pr-o12", "pr-rv12", "pay-1", 400)
    # 撤销后已收净额 600：退款 700 拒绝，退款 600 成功。
    assert _refund("pr-o12", "pr-rf12a", 700).status_code == 409
    assert _refund("pr-o12", "pr-rf12b", 600).status_code == 200
    order = _order("pr-o12")
    assert order["paid_cents"] == 600 and order["refunded_cents"] == 600
    assert order["outstanding_cents"] == 1000
    _assert_invariants("pr-o12")


def test_repay_after_reversal_collects_again_and_settles() -> None:
    _new_order("pr-o13", 1000)
    _pay("pr-o13", 1000)
    _reverse("pr-o13", "pr-rv13", "pay-1", 300)
    # 撤销腾出的未收可重新收款；超过未收仍被拒。
    assert _pay("pr-o13", 400).status_code == 409
    assert _pay("pr-o13", 300).status_code == 200
    order = _order("pr-o13")
    assert order["paid_cents"] == 1000 and order["outstanding_cents"] == 0
    assert order["status"] == "settled"
    _assert_invariants("pr-o13")


def test_reversal_then_refund_then_repay_keeps_all_invariants() -> None:
    _new_order("pr-o14", 1000)
    _pay("pr-o14", 1000)
    _reverse("pr-o14", "pr-rv14a", "pay-1", 500)
    _refund("pr-o14", "pr-rf14", 300)
    _pay("pr-o14", 200)
    _reverse("pr-o14", "pr-rv14b", "pay-2", 200)
    _assert_invariants("pr-o14")
    # 原收款流水仍在，净额分别为 pay-1 500、pay-2 0。
    conn = connect()
    try:
        nets = {
            r["payment_ref"]: r["amount_cents"] - r["reversed_cents"]
            for r in conn.execute(
                "SELECT payment_ref, amount_cents, reversed_cents FROM payments"
                " WHERE tenant=? AND order_id='pr-o14'", (T,)
            )
        }
    finally:
        conn.close()
    assert nets == {"pay-1": 500, "pay-2": 0}


# ---------- 账务历史 ----------

def test_reversal_appends_distinguishable_ledger_entry_and_replays() -> None:
    _new_order("pr-o15", 1000)
    _pay("pr-o15", 800)
    _reverse("pr-o15", "pr-rv15", "pay-1", 300)
    entries = client.get("/orders/pr-o15/ledger", headers=H).json()["entries"]
    assert [e["type"] for e in entries] == ["payment", "payment_reversal"]
    rev = entries[-1]
    assert rev["biz_ref"] == "pr-rv15" and rev["amount_cents"] == 300
    assert rev["outstanding_cents"] == _order("pr-o15")["outstanding_cents"] == 500
    # 原收款流水不删除：按序列逐笔推进未收，终值与订单读取一致。
    outstanding = 1000
    for entry in entries:
        if entry["type"] == "payment":
            outstanding -= entry["amount_cents"]
        elif entry["type"] == "payment_reversal":
            outstanding += entry["amount_cents"]
        assert outstanding == entry["outstanding_cents"]


# ---------- 404 / 400 ----------

def test_unknown_payment_order_or_cross_tenant_is_not_found() -> None:
    _new_order("pr-o16", 1000)
    _pay("pr-o16", 1000)
    assert _reverse("pr-o16", "pr-rv16a", "pay-9", 1).status_code == 404
    assert _reverse("pr-nope", "pr-rv16b", "pay-1", 1).status_code == 404
    assert _reverse("pr-o16", "pr-rv16c", "pay-1", 1, tenant="other").status_code == 404


def test_invalid_reversal_body_is_400() -> None:
    _new_order("pr-o17", 1000)
    _pay("pr-o17", 1000)
    base = "/orders/pr-o17/payments/reversals"
    assert client.post(base, json={"reversal_id": "x", "payment_ref": "pay-1", "amount_cents": 0}, headers=H).status_code == 400
    assert client.post(base, json={"reversal_id": "x", "payment_ref": "pay-1", "amount_cents": -1}, headers=H).status_code == 400
    assert client.post(base, json={"reversal_id": "", "payment_ref": "pay-1", "amount_cents": 1}, headers=H).status_code == 400
    assert client.post(base, json={"reversal_id": "x", "payment_ref": "", "amount_cents": 1}, headers=H).status_code == 400
    assert client.post(base, json={"reversal_id": "x", "amount_cents": 1}, headers=H).status_code == 400
    assert client.post(base, json={"reversal_id": "x", "payment_ref": "pay-1", "amount_cents": 1}).status_code == 400


# ---------- 并发 ----------

def test_concurrent_reversals_never_exceed_payment_net() -> None:
    _new_order("pr-o18", 1000)
    _pay("pr-o18", 1000)

    def attempt(rid: str) -> int:
        res = _reverse("pr-o18", rid, "pay-1", 800)
        return res.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(attempt, ["pr-rv18a", "pr-rv18b"]))
    assert sorted(statuses) == [200, 409]
    order = _order("pr-o18")
    assert order["paid_cents"] == 200 and order["outstanding_cents"] == 800
    _assert_invariants("pr-o18")


def test_concurrent_same_reversal_id_both_see_first_result() -> None:
    _new_order("pr-o19", 1000)
    _pay("pr-o19", 1000)

    def attempt(_: int) -> dict | None:
        result = payments.reverse_payment(T, "pr-o19", "pr-rv19", "pay-1", 200)
        return result

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [0, 1]))
    assert all(r is not None for r in results)
    assert results[0] == results[1]
    assert _order("pr-o19")["paid_cents"] == 800


# ---------- 对账一致 ----------

def test_reconciliation_after_reversal_still_balances() -> None:
    rt = "pr-rec"
    rh = {"X-Tenant": rt}
    client.post(
        "/orders",
        json={"tenant": rt, "order_id": "only", "amount_cents": 1000, "currency": "CNY"},
    )
    client.post("/orders/only/payments", json={"amount_cents": 1000}, headers=rh)
    res = client.post(
        "/orders/only/payments/reversals",
        json={"reversal_id": "pr-rv20", "payment_ref": "pay-1", "amount_cents": 400},
        headers=rh,
    )
    assert res.status_code == 200
    res = client.post(
        "/reconciliations", json={"tenant": rt, "reconciliation_id": "pr-rec20"}
    )
    assert res.status_code == 201
    body = res.json()
    assert body["order_count"] == 1
    assert body["total_paid_cents"] == 600 and body["total_outstanding_cents"] == 400
    assert body["total_receivable_cents"] == body["total_paid_cents"] + body["total_outstanding_cents"]


# ---------- 老库升级回填 ----------

def test_legacy_database_backfills_payments_and_allocations() -> None:
    from app.store import db

    legacy = os.path.join(tempfile.mkdtemp(), "legacy-reversal.sqlite")
    previous = os.environ["APP_DB"]
    os.environ["APP_DB"] = legacy
    try:
        # 仅应用到 006 的老库，手工构造一段与现网历史一致的账务：
        # 收 600、退 200、补收 400（已收 1000、已退 200、未收 200）。
        old_migrations = db.MIGRATIONS
        db.MIGRATIONS = tuple(name for name in old_migrations if not name.startswith("007"))
        try:
            db.migrate()
        finally:
            db.MIGRATIONS = old_migrations
        conn = connect()
        try:
            conn.execute(
                "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, refunded_cents, currency, status)"
                " VALUES('t1','leg',1000,1000,200,'CNY','accepted')"
            )
            conn.execute(
                "INSERT INTO debt_entries(tenant, order_id, debt_no, amount_cents, settled_cents, currency, status)"
                " VALUES('t1','leg',1,1000,1000,'CNY','settled')"
            )
            conn.execute(
                "INSERT INTO debt_entries(tenant, order_id, debt_no, amount_cents, settled_cents, currency, status)"
                " VALUES('t1','leg',2,200,0,'CNY','unsettled')"
            )
            conn.execute(
                "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, currency, paid_after,"
                " refunded_after, outstanding_after) VALUES('t1','leg-rf','leg',200,'CNY',600,200,600)"
            )
            for seq, (ref, typ, amount, outstanding) in enumerate(
                [("pay-1", "payment", 600, 400), ("leg-rf", "refund", 200, 600), ("pay-2", "payment", 400, 200)],
                start=1,
            ):
                conn.execute(
                    "INSERT INTO ledger_entries(tenant, order_id, seq_no, biz_ref, entry_type, amount_cents, outstanding_after)"
                    " VALUES('t1','leg',?,?,?,?,?)",
                    (seq, ref, typ, amount, outstanding),
                )
            conn.commit()
        finally:
            conn.close()
        # 应用 007：回填收款与占用，随后撤销 pay-2 的 400，占用尾部从第 1 条释放。
        db.migrate()
        conn = connect()
        try:
            pays = {
                r["payment_ref"]: r["amount_cents"]
                for r in conn.execute("SELECT payment_ref, amount_cents FROM payments WHERE tenant='t1'")
            }
            alloc = conn.execute(
                "SELECT payment_ref, debt_no, amount_cents FROM payment_allocations WHERE tenant='t1'"
            ).fetchall()
        finally:
            conn.close()
        assert pays == {"pay-1": 600, "pay-2": 400}
        assert {(r["payment_ref"], r["debt_no"]): r["amount_cents"] for r in alloc} == {
            ("pay-1", 1): 600,
            ("pay-2", 1): 400,
        }
        res = client.post(
            "/orders/leg/payments/reversals",
            json={"reversal_id": "leg-rv", "payment_ref": "pay-2", "amount_cents": 400},
            headers={"X-Tenant": "t1"},
        )
        assert res.status_code == 200 and res.json()["payment_net_cents"] == 0
        debts = client.get("/orders/leg/debts", headers={"X-Tenant": "t1"}).json()["debts"]
        assert [d["settled_cents"] for d in debts] == [600, 0]
    finally:
        os.environ["APP_DB"] = previous


def test_pre_ledger_legacy_order_backfills_synthetic_pay_zero() -> None:
    from app.store import db

    legacy = os.path.join(tempfile.mkdtemp(), "legacy-pre-ledger.sqlite")
    previous = os.environ["APP_DB"]
    os.environ["APP_DB"] = legacy
    try:
        import sqlite3

        schema_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations", "001_init.sql")
        conn = sqlite3.connect(legacy)
        with open(schema_path, encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, currency, status)"
            " VALUES('t1','ancient',700,300,'CNY','accepted')"
        )
        conn.commit()
        conn.close()
        db.migrate()
        conn = connect()
        try:
            row = conn.execute(
                "SELECT payment_ref, amount_cents, reversed_cents FROM payments"
                " WHERE tenant='t1' AND order_id='ancient'"
            ).fetchone()
            alloc = conn.execute(
                "SELECT debt_no, amount_cents FROM payment_allocations WHERE tenant='t1' AND order_id='ancient'"
            ).fetchall()
        finally:
            conn.close()
        assert row["payment_ref"] == "pay-0" and row["amount_cents"] == 300 and row["reversed_cents"] == 0
        assert [(r["debt_no"], r["amount_cents"]) for r in alloc] == [(1, 300)]
        res = client.post(
            "/orders/ancient/payments/reversals",
            json={"reversal_id": "anc-rv", "payment_ref": "pay-0", "amount_cents": 100},
            headers={"X-Tenant": "t1"},
        )
        assert res.status_code == 200 and res.json()["payment_net_cents"] == 200
    finally:
        os.environ["APP_DB"] = previous
