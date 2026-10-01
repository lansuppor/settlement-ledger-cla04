import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
import threading

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

def test_accept_and_read_order() -> None:
    body = {"tenant": "t1", "order_id": "o1", "amount_cents": 500, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    got = client.get("/orders/o1", headers={"X-Tenant": "t1"})
    assert got.status_code == 200 and got.json()["outstanding_cents"] == 500

def test_duplicate_is_refused() -> None:
    body = {"tenant": "t1", "order_id": "o2", "amount_cents": 100, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201
    assert client.post("/orders", json=body).status_code == 409

def test_cross_tenant_read_is_not_found() -> None:
    body = {"tenant": "t1", "order_id": "o3", "amount_cents": 100, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.get("/orders/o3", headers={"X-Tenant": "t2"}).status_code == 404

def test_payment_cannot_exceed_outstanding() -> None:
    _make_order("o4", 300)
    assert client.post("/orders/o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/o4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 409

def test_payment_response_returns_payment_id() -> None:
    _make_order("r1", 500)
    body = _pay("r1", 200)
    assert body["payment_id"]
    assert body["paid_cents"] == 200 and body["outstanding_cents"] == 300

def test_reversal_opens_outstanding_and_closes_again() -> None:
    _make_order("r2", 500)
    p1 = _pay("r2", 200)["payment_id"]
    p2 = _pay("r2", 300)["payment_id"]
    full = client.get("/orders/r2", headers={"X-Tenant": "t1"}).json()
    assert full["status"] == "settled" and full["paid_cents"] == 500 and full["outstanding_cents"] == 0

    resp = _reverse("r2", "rev-1", p1)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["reversal_id"] == "rev-1"
    assert data["payment_id"] == p1
    assert data["reversed_amount_cents"] == 200
    assert data["paid_cents"] == 300 and data["outstanding_cents"] == 200 and data["status"] == "accepted"

    # 冲正后可继续登记收款；收满后状态回到 settled
    again = _pay("r2", 200)
    assert again["paid_cents"] == 500 and again["outstanding_cents"] == 0 and again["status"] == "settled"
    assert again["payment_id"] != p1 and again["payment_id"] != p2

def test_reversal_is_idempotent() -> None:
    _make_order("r3", 500)
    pid = _pay("r3", 100)["payment_id"]
    first = _reverse("r3", "rev-x", pid)
    second = _reverse("r3", "rev-x", pid)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    state = client.get("/orders/r3", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 0 and state["outstanding_cents"] == 500

def test_reverse_unknown_payment_is_404() -> None:
    _make_order("r4", 500)
    resp = _reverse("r4", "rev-a", "no-such-payment")
    assert resp.status_code == 404 and resp.json()["detail"] == "payment not found"
    state = client.get("/orders/r4", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 0

def test_reverse_already_reversed_payment_is_409() -> None:
    _make_order("r5", 500)
    pid = _pay("r5", 100)["payment_id"]
    assert _reverse("r5", "rev-1", pid).status_code == 200
    resp = _reverse("r5", "rev-2", pid)
    assert resp.status_code == 409 and resp.json()["detail"] == "payment already reversed"
    state = client.get("/orders/r5", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 0

def test_reversal_id_conflict_is_409() -> None:
    _make_order("r6", 500)
    p1 = _pay("r6", 100)["payment_id"]
    p2 = _pay("r6", 100)["payment_id"]
    assert _reverse("r6", "dup-rev", p1).status_code == 200
    resp = _reverse("r6", "dup-rev", p2)
    assert resp.status_code == 409 and "another payment" in resp.json()["detail"]
    # 冲突不改变状态：只有 p1 被冲正
    state = client.get("/orders/r6", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 100

def test_reversal_on_unknown_order_is_404() -> None:
    resp = _reverse("missing", "rev-1", "p-1")
    assert resp.status_code == 404 and resp.json()["detail"] == "order not found"

def test_cross_tenant_reversal_is_not_found() -> None:
    _make_order("r7", 500)
    pid = _pay("r7", 100, tenant="t1")["payment_id"]
    resp = _reverse("r7", "rev-1", pid, tenant="t2")
    assert resp.status_code == 404
    # 原租户收款未受影响
    state = client.get("/orders/r7", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 100

def test_reversal_without_tenant_header_is_400() -> None:
    resp = client.post("/orders/r8/reversals", json={"reversal_id": "r", "payment_id": "p"})
    assert resp.status_code == 400

def test_reversal_record_persists_after_restart() -> None:
    _make_order("r9", 500)
    pid = _pay("r9", 250)["payment_id"]
    assert _reverse("r9", "rev-persist", pid).status_code == 200
    # 重新迁移/重连同一数据库文件，等价于服务重启后的重放查询
    migrate()
    state = client.get("/orders/r9", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 0 and state["outstanding_cents"] == 500
    conn = connect()
    try:
        row = conn.execute(
            "SELECT reversal_id, payment_id, amount_cents FROM reversals WHERE order_id='r9'"
        ).fetchone()
    finally:
        conn.close()
    assert row["reversal_id"] == "rev-persist" and row["payment_id"] == pid and row["amount_cents"] == 250

def test_concurrent_payments_and_reversal_never_break_invariant() -> None:
    _make_order("rc", 1000)
    # 先登记两笔各 500 收满
    pid_a = _pay("rc", 500)["payment_id"]
    _pay("rc", 500)
    errors: list[Exception] = []

    def pay_more() -> None:
        try:
            resp = client.post("/orders/rc/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"})
            # 订单已满时收款应被 409 拒绝；冲正腾出额度后可能成功
            assert resp.status_code in (200, 409)
        except Exception as exc:  # noqa: BLE001 - 记录线程内断言失败
            errors.append(exc)

    def reverse_a() -> None:
        try:
            resp = _reverse("rc", "rev-conc", pid_a)
            assert resp.status_code in (200,)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reverse_a)]
    threads += [threading.Thread(target=pay_more) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    state = client.get("/orders/rc", headers={"X-Tenant": "t1"}).json()
    assert 0 <= state["paid_cents"] <= 1000
    assert state["paid_cents"] + state["outstanding_cents"] == 1000

    # 明细层闭合：未冲正收款合计 == paid_cents；冲正只影响被点名的收款
    conn = connect()
    try:
        live_total = conn.execute(
            "SELECT COALESCE(SUM(p.amount_cents),0) AS s FROM payments p "
            "WHERE p.order_id='rc' AND NOT EXISTS ("
            "SELECT 1 FROM reversals v WHERE v.tenant=p.tenant AND v.order_id=p.order_id AND v.payment_id=p.payment_id)"
        ).fetchone()["s"]
        reversal_count = conn.execute("SELECT COUNT(*) AS c FROM reversals WHERE order_id='rc'").fetchone()["c"]
    finally:
        conn.close()
    assert live_total == state["paid_cents"]
    assert reversal_count == 1
