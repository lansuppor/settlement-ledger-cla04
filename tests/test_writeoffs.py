import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store.db import MIGRATIONS_DIR, connect, migrate

migrate()
client = TestClient(app)

T = "wo"


def _new_order(oid: str, amount: int, paid: int = 0, tenant: str = T) -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"})
    if paid:
        client.post(f"/orders/{oid}/payments", json={"amount_cents": paid}, headers={"X-Tenant": tenant})


def _writeoff(oid: str, wid: str, debt_no: int, amount: int, tenant: str = T):
    return client.post(
        f"/orders/{oid}/writeoffs",
        json={"writeoff_id": wid, "debt_no": debt_no, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


def _debts(oid: str, tenant: str = T):
    return client.get(f"/orders/{oid}/debts", headers={"X-Tenant": tenant})


def _refund(oid: str, rid: str, amount: int, tenant: str = T):
    return client.post(
        f"/orders/{oid}/refunds",
        json={"refund_id": rid, "amount_cents": amount},
        headers={"X-Tenant": tenant},
    )


# ---------- 欠款条目生成 ----------

def test_accept_creates_first_debt_equal_to_order_amount() -> None:
    _new_order("wo-o1", 1000)
    res = _debts("wo-o1")
    assert res.status_code == 200
    debts = res.json()["debts"]
    assert debts == [
        {
            "debt_no": 1,
            "amount_cents": 1000,
            "settled_cents": 0,
            "remaining_cents": 1000,
            "status": "unsettled",
        }
    ]


def test_refund_appends_new_debt_and_keeps_prior_entry_untouched() -> None:
    _new_order("wo-o2", 1000, paid=1000)
    assert _refund("wo-o2", "wo-rf1", 300).status_code == 200
    debts = _debts("wo-o2").json()["debts"]
    # 第 1 条金额仍为订单受理金额；新增第 2 条金额等于退回金额，未核销。
    assert [d["debt_no"] for d in debts] == [1, 2]
    assert debts[0]["amount_cents"] == 1000 and debts[0]["settled_cents"] == 1000
    assert debts[1] == {
        "debt_no": 2,
        "amount_cents": 300,
        "settled_cents": 0,
        "remaining_cents": 300,
        "status": "unsettled",
    }


def test_payment_auto_applies_in_debt_no_order() -> None:
    _new_order("wo-o3", 1000, paid=1000)
    _refund("wo-o3", "wo-rf2", 300)
    # 再收款 200：按编号顺序先占第 1 条（已满），实际全部落到第 2 条的一部分？
    # 第 1 条已被早先 1000 收款占满，故 200 占用第 2 条；再收 100 占满第 2 条。
    assert client.post("/orders/wo-o3/payments", json={"amount_cents": 200}, headers={"X-Tenant": T}).status_code == 200
    assert client.post("/orders/wo-o3/payments", json={"amount_cents": 100}, headers={"X-Tenant": T}).status_code == 200
    debts = _debts("wo-o3").json()["debts"]
    assert debts[0]["settled_cents"] == 1000 and debts[0]["status"] == "settled"
    assert debts[1]["settled_cents"] == 300 and debts[1]["status"] == "settled"
    order = client.get("/orders/wo-o3", headers={"X-Tenant": T}).json()
    assert sum(d["remaining_cents"] for d in debts) == order["outstanding_cents"]


def test_debt_totals_always_equal_order_amounts() -> None:
    # 任意交错下：Σ欠款金额 = 订单金额 + 已退；Σ未核销余额 = 订单未收。
    _new_order("wo-o4", 800)
    client.post("/orders/wo-o4/payments", json={"amount_cents": 500}, headers={"X-Tenant": T})
    _refund("wo-o4", "wo-rf3", 200)
    client.post("/orders/wo-o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": T})
    debts = _debts("wo-o4").json()["debts"]
    order = client.get("/orders/wo-o4", headers={"X-Tenant": T}).json()
    assert sum(d["amount_cents"] for d in debts) == order["amount_cents"] + order["refunded_cents"] == 1000
    assert sum(d["remaining_cents"] for d in debts) == order["outstanding_cents"] == 400


# ---------- 核销登记 ----------

def test_writeoff_moves_arrived_amount_onto_target_debt() -> None:
    # 收 800 自动落在第 1 条；退 600 生成第 2 条；补收 300 占用第 2 条的一半。
    _new_order("wo-o5", 800, paid=800)
    _refund("wo-o5", "wo-rf4", 600)
    client.post("/orders/wo-o5/payments", json={"amount_cents": 300}, headers={"X-Tenant": T})
    debts = _debts("wo-o5").json()["debts"]
    assert [d["settled_cents"] for d in debts] == [800, 300]
    # 把第 2 条剩余 300 核销：到账金额从第 1 条自动占用的尾部空间改配过来。
    res = _writeoff("wo-o5", "wo-w1", debt_no=2, amount=300)
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["writeoff_id"] == "wo-w1" and body["debt_no"] == 2 and body["amount_cents"] == 300
    assert body["settled_cents"] == 600 and body["remaining_cents"] == 0 and body["status"] == "settled"
    debts = _debts("wo-o5").json()["debts"]
    assert [d["settled_cents"] for d in debts] == [500, 600]
    assert [d["remaining_cents"] for d in debts] == [300, 0]
    # 已核销之和恒等于已收；未核销之和恒等于订单未收（核销不改变订单任何金额与状态）。
    order = client.get("/orders/wo-o5", headers={"X-Tenant": T}).json()
    assert order["paid_cents"] == 1100 and order["refunded_cents"] == 600
    assert order["outstanding_cents"] == 300 and order["status"] == "accepted"
    assert sum(d["settled_cents"] for d in debts) == order["paid_cents"]
    assert sum(d["remaining_cents"] for d in debts) == order["outstanding_cents"]
    assert body["remaining_total_cents"] == 300


def test_partial_writeoff_keeps_entry_unsettled() -> None:
    # 全额收款后退款 300 生成第 2 条；补收 100 后第 2 条已核销 100、余额 200。
    _new_order("wo-o6", 500, paid=500)
    _refund("wo-o6", "wo-rf5", 300)
    client.post("/orders/wo-o6/payments", json={"amount_cents": 100}, headers={"X-Tenant": T})
    res = _writeoff("wo-o6", "wo-w2b", debt_no=2, amount=100)
    assert res.status_code == 201
    body = res.json()
    assert body["settled_cents"] == 200 and body["remaining_cents"] == 100 and body["status"] == "unsettled"
    debts = _debts("wo-o6").json()["debts"]
    assert debts[1]["status"] == "unsettled"


def test_writeoff_exceeding_balance_is_rejected_without_records() -> None:
    _new_order("wo-o7", 1000, paid=1000)
    _refund("wo-o7", "wo-rf6", 400)
    client.post("/orders/wo-o7/payments", json={"amount_cents": 200}, headers={"X-Tenant": T})
    # 第 2 条已核销 200、余额 200（第 1 条有钱可改配），核销 401 仍须按超出条目余额整笔拒绝。
    assert _writeoff("wo-o7", "wo-w3", 2, 401).status_code == 409
    conn = connect()
    try:
        writeoffs = conn.execute(
            "SELECT COUNT(*) AS n FROM writeoffs WHERE tenant=? AND writeoff_id='wo-w3'", (T,)
        ).fetchone()["n"]
        ledger_rows = conn.execute(
            "SELECT COUNT(*) AS n FROM ledger_entries WHERE tenant=? AND order_id='wo-o7' AND entry_type='writeoff'",
            (T,),
        ).fetchone()["n"]
        settled_total = conn.execute(
            "SELECT COALESCE(SUM(settled_cents),0) AS n FROM debt_entries WHERE tenant=? AND order_id='wo-o7'",
            (T,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert writeoffs == 0 and ledger_rows == 0
    assert settled_total == 1200  # 与已收一致，未发生任何改配


def test_writeoff_replay_returns_first_result() -> None:
    _new_order("wo-o8", 600, paid=600)
    _refund("wo-o8", "wo-rf7", 300)
    client.post("/orders/wo-o8/payments", json={"amount_cents": 200}, headers={"X-Tenant": T})
    payload = {"writeoff_id": "wo-w4", "debt_no": 2, "amount_cents": 100}
    first = client.post("/orders/wo-o8/writeoffs", json=payload, headers={"X-Tenant": T})
    second = client.post("/orders/wo-o8/writeoffs", json=payload, headers={"X-Tenant": T})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM writeoffs WHERE tenant=? AND writeoff_id='wo-w4'", (T,)
        ).fetchone()["n"]
        settled = conn.execute(
            "SELECT settled_cents FROM debt_entries WHERE tenant=? AND order_id='wo-o8' AND debt_no=2",
            (T,),
        ).fetchone()["settled_cents"]
    finally:
        conn.close()
    assert n == 1 and settled == 300  # 重复提交不重复核销


def test_same_writeoff_id_with_different_params_is_conflicted() -> None:
    _new_order("wo-o9a", 500, paid=500)
    _refund("wo-o9a", "wo-rf8", 200)
    client.post("/orders/wo-o9a/payments", json={"amount_cents": 100}, headers={"X-Tenant": T})
    _new_order("wo-o9b", 500, paid=500)
    assert _writeoff("wo-o9a", "wo-w5", 2, 100).status_code == 201
    assert _writeoff("wo-o9a", "wo-w5", 2, 99).status_code == 409   # 换金额
    assert _writeoff("wo-o9a", "wo-w5", 1, 100).status_code == 409  # 换欠款编号
    assert _writeoff("wo-o9b", "wo-w5", 1, 100).status_code == 409  # 换订单


def test_writeoff_unknown_order_or_debt_or_cross_tenant_is_not_found() -> None:
    assert _writeoff("wo-missing", "wo-wx", 1, 1).status_code == 404
    _new_order("wo-o10", 100)
    assert _writeoff("wo-o10", "wo-w6", 2, 1).status_code == 404  # 欠款编号不存在
    assert _writeoff("wo-o10", "wo-w7", 1, 1, tenant="other").status_code == 404  # 跨租户
    # 跨租户失败不占用核销标识。
    assert _writeoff("wo-o10", "wo-w7", 1, 1).status_code == 409  # 本租户无到账金额：冲突而非 404
    assert _debts("wo-o10", tenant="other").status_code == 404
    assert _debts("wo-missing").status_code == 404
    assert client.get("/orders/wo-o10/debts").status_code == 400


def test_writeoff_rejected_while_settlement_effective_allowed_after_reversal() -> None:
    _new_order("wo-o11", 300, paid=300)
    settle = client.post(
        "/orders/wo-o11/settlements",
        json={"settlement_id": "wo-s1", "amount_cents": 300},
        headers={"X-Tenant": T},
    )
    assert settle.status_code == 201
    assert _writeoff("wo-o11", "wo-w8", 1, 1).status_code == 409
    client.post(
        "/orders/wo-o11/settlements/wo-s1/reversals",
        json={"reversal_id": "wo-rv1", "reason": "reopen"},
        headers={"X-Tenant": T},
    )
    # 冲正后退款再补收，可对新条目核销：证明“须先冲正结算”这道门随冲正打开。
    _refund("wo-o11", "wo-rf-s1", 200)
    client.post("/orders/wo-o11/payments", json={"amount_cents": 100}, headers={"X-Tenant": T})
    assert _writeoff("wo-o11", "wo-w9", 2, 100).status_code == 201


def test_concurrent_writeoffs_on_one_debt_only_one_wins() -> None:
    _new_order("wo-o12", 500, paid=500)
    _refund("wo-o12", "wo-rf9", 500)
    # 第 2 条未收 500、第 1 条上有 500 到账金额可改配；两笔各 500 并发，至多一笔成功。
    with ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(lambda wid: _writeoff("wo-o12", wid, 2, 500).status_code, ["wo-w9a", "wo-w9b"]))
    assert sorted(codes) == [201, 409]
    conn = connect()
    try:
        row = conn.execute(
            "SELECT amount_cents, settled_cents FROM debt_entries WHERE tenant=? AND order_id='wo-o12' AND debt_no=2",
            (T,),
        ).fetchone()
        total_settled = conn.execute(
            "SELECT SUM(settled_cents) AS n FROM debt_entries WHERE tenant=? AND order_id='wo-o12'",
            (T,),
        ).fetchone()["n"]
        n_writes = conn.execute(
            "SELECT COUNT(*) AS n FROM writeoffs WHERE tenant=? AND order_id='wo-o12'", (T,)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert row["settled_cents"] == row["amount_cents"] == 500  # 绝不超过欠款金额
    assert total_settled == 500 and n_writes == 1  # 已核销之和恒等于已收


def test_concurrent_partial_writeoffs_never_exceed_amount() -> None:
    _new_order("wo-o13", 600, paid=600)
    _refund("wo-o13", "wo-rf10", 600)
    # 第 2 条余额 600、可从第 1 条释放 600；四笔各 300 并发，恰好两笔成功、两笔按超额拒绝。
    with ThreadPoolExecutor(max_workers=4) as pool:
        codes = list(
            pool.map(lambda i: _writeoff("wo-o13", f"wo-w10-{i}", 2, 300).status_code, range(4))
        )
    assert sorted(codes) == [201, 201, 409, 409]
    conn = connect()
    try:
        row = conn.execute(
            "SELECT amount_cents, settled_cents FROM debt_entries WHERE tenant=? AND order_id='wo-o13' AND debt_no=2",
            (T,),
        ).fetchone()
        total_settled = conn.execute(
            "SELECT SUM(settled_cents) AS n FROM debt_entries WHERE tenant=? AND order_id='wo-o13'",
            (T,),
        ).fetchone()["n"]
    finally:
        conn.close()
    assert row["settled_cents"] == row["amount_cents"] == 600
    assert total_settled == 600  # = 已收金额


# ---------- 账务留痕 ----------

def test_writeoff_appends_ledger_entry() -> None:
    _new_order("wo-o14", 700, paid=700)
    _refund("wo-o14", "wo-rf11", 400)
    client.post("/orders/wo-o14/payments", json={"amount_cents": 150}, headers={"X-Tenant": T})
    assert _writeoff("wo-o14", "wo-w11", 2, 250).status_code == 201
    ledger_res = client.get("/orders/wo-o14/ledger", headers={"X-Tenant": T}).json()
    entries = [e for e in ledger_res["entries"] if e["type"] == "writeoff"]
    assert len(entries) == 1
    entry = entries[0]
    order = client.get("/orders/wo-o14", headers={"X-Tenant": T}).json()
    assert entry["biz_ref"] == "wo-w11" and entry["amount_cents"] == 250
    # 留痕含核销后仍未核销的欠款合计，恒等于订单未收金额。
    assert entry["outstanding_cents"] == order["outstanding_cents"] == 250


def test_writeoff_does_not_affect_reconciliation() -> None:
    rt = "wo-rc"
    _new_order("wo-o15", 1000, paid=1000, tenant=rt)
    _refund("wo-o15", "wo-rf12", 400, tenant=rt)
    client.post("/orders/wo-o15/payments", json={"amount_cents": 200}, headers={"X-Tenant": rt})
    before = client.post(
        "/reconciliations", json={"tenant": rt, "reconciliation_id": "wo-rec1"}
    ).json()
    assert _writeoff("wo-o15", "wo-w12", 2, 200, tenant=rt).status_code == 201
    after = client.post(
        "/reconciliations", json={"tenant": rt, "reconciliation_id": "wo-rec2"}
    ).json()
    for key in (
        "total_receivable_cents",
        "total_paid_cents",
        "total_refunded_cents",
        "total_outstanding_cents",
    ):
        assert before[key] == after[key]


def test_persistence_after_restart_still_queries() -> None:
    # 结果落本地 SQLite：重新取连接（等价重启后读取）仍可查询，重复提交不产生新效果。
    _new_order("wo-o16", 400, paid=400)
    _refund("wo-o16", "wo-rf13", 100)
    client.post("/orders/wo-o16/payments", json={"amount_cents": 50}, headers={"X-Tenant": T})
    assert _writeoff("wo-o16", "wo-w13", 2, 50).status_code == 201
    conn = connect()
    try:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM debt_entries WHERE tenant=? AND order_id='wo-o16'", (T,)
        ).fetchone()["n"]
    finally:
        conn.close()
    assert n == 2
    again = _writeoff("wo-o16", "wo-w13", 2, 50)
    assert again.status_code == 201 and again.json()["remaining_total_cents"] == 50


def test_imported_order_gets_first_debt() -> None:
    import time

    rows = [{"order_id": "wo-imp1", "amount_cents": 900, "currency": "CNY"}]
    res = client.post("/orders/import", json={"tenant": T, "batch_id": "wo-batch1", "rows": rows})
    assert res.status_code == 202
    deadline = time.time() + 10
    while time.time() < deadline:
        batch = client.get("/orders/import/wo-batch1", headers={"X-Tenant": T}).json()
        if batch["status"] == "completed":
            break
        time.sleep(0.02)
    assert batch["status"] == "completed" and batch["success_count"] == 1
    debts = _debts("wo-imp1").json()["debts"]
    assert len(debts) == 1 and debts[0]["debt_no"] == 1 and debts[0]["amount_cents"] == 900


def test_invalid_writeoff_body_is_400() -> None:
    _new_order("wo-o17", 100)
    bad = [
        {"writeoff_id": "wo-bad1", "debt_no": 0, "amount_cents": 1},
        {"writeoff_id": "wo-bad2", "debt_no": 1, "amount_cents": 0},
        {"writeoff_id": "", "debt_no": 1, "amount_cents": 1},
    ]
    for payload in bad:
        res = client.post("/orders/wo-o17/writeoffs", json=payload, headers={"X-Tenant": T})
        assert res.status_code == 400, payload
    assert client.post("/orders/wo-o17/writeoffs", json=bad[0]).status_code == 400


# ---------- 老库升级回填 ----------

def test_migration_backfills_debts_for_legacy_orders() -> None:
    import sqlite3

    legacy = Path(tempfile.mkdtemp()) / "legacy.sqlite"
    conn = sqlite3.connect(legacy)
    try:
        conn.executescript((MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8"))
        conn.executescript((MIGRATIONS_DIR / "002_refunds.sql").read_text(encoding="utf-8"))
        # 旧库三笔订单：未收款 / 全额收讫 / 退款后补收。
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, refunded_cents, currency, status)"
            " VALUES('lg','a',1000,0,0,'CNY','accepted')"
        )
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, refunded_cents, currency, status)"
            " VALUES('lg','b',1000,1000,0,'CNY','accepted')"
        )
        conn.execute(
            "INSERT INTO orders(tenant, order_id, amount_cents, paid_cents, refunded_cents, currency, status)"
            " VALUES('lg','c',1000,1200,200,'CNY','accepted')"
        )
        conn.execute(
            "INSERT INTO refunds(tenant, refund_id, order_id, amount_cents, currency, paid_after,"
            " refunded_after, outstanding_after) VALUES('lg','r1','c',200,'CNY',1000,200,200)"
        )
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(legacy)
    try:
        sql = (MIGRATIONS_DIR / "006_debts.sql").read_text(encoding="utf-8")
        for stmt in (s.strip() for s in sql.split(";") if s.strip()):
            conn.execute(stmt)
        conn.commit()
        rows = conn.execute(
            "SELECT order_id, debt_no, amount_cents, settled_cents, status FROM debt_entries ORDER BY order_id, debt_no"
        ).fetchall()
    finally:
        conn.close()
    assert rows == [
        ("a", 1, 1000, 0, "unsettled"),
        ("b", 1, 1000, 1000, "settled"),
        ("c", 1, 1000, 1000, "settled"),
        ("c", 2, 200, 200, "settled"),
    ]
