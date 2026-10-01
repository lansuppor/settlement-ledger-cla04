import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))
from fastapi.testclient import TestClient

from app.entry import app
from app.store import orders
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

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
    body = {"tenant": "t1", "order_id": "o4", "amount_cents": 300, "currency": "CNY"}
    client.post("/orders", json=body)
    assert client.post("/orders/o4/payments", json={"amount_cents": 100}, headers={"X-Tenant": "t1"}).status_code == 200
    assert client.post("/orders/o4/payments", json={"amount_cents": 500}, headers={"X-Tenant": "t1"}).status_code == 409

def _make_order(oid: str, amount: int, tenant: str = "rv") -> None:
    client.post("/orders", json={"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"})

def _pay(oid: str, amount: int, tenant: str = "rv"):
    resp = client.post(f"/orders/{oid}/payments", json={"amount_cents": amount}, headers={"X-Tenant": tenant})
    assert resp.status_code == 200, resp.text
    return resp.json()

def test_payment_returns_payment_id_and_order_totals() -> None:
    _make_order("r1", 500)
    body = _pay("r1", 200)
    assert isinstance(body["payment_id"], int)
    assert body["paid_cents"] == 200 and body["outstanding_cents"] == 300

def test_reversal_happy_path_reopens_and_settles_again() -> None:
    _make_order("r2", 300)
    p1 = _pay("r2", 100)["payment_id"]
    p2 = _pay("r2", 200)["payment_id"]
    assert client.get("/orders/r2", headers={"X-Tenant": "rv"}).json()["status"] == "settled"

    resp = client.post("/orders/r2/reversals", json={"reversal_id": "rev-1", "payment_id": p1},
                       headers={"X-Tenant": "rv"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 只影响被点名的那一笔；冲正记录保留原收款金额与双方标识
    assert body["reversal_id"] == "rev-1" and body["payment_id"] == p1 and body["reversed_amount_cents"] == 100
    assert body["paid_cents"] == 200 and body["outstanding_cents"] == 100 and body["status"] == "accepted"

    # 重新可以登记收款，再次收满后回到已结算
    assert _pay("r2", 100)["status"] == "settled"
    order = client.get("/orders/r2", headers={"X-Tenant": "rv"}).json()
    assert order["paid_cents"] + order["outstanding_cents"] == 300
    # 另一笔收款未受影响
    assert client.post("/orders/r2/reversals", json={"reversal_id": "rev-2", "payment_id": p2},
                       headers={"X-Tenant": "rv"}).json()["paid_cents"] == 100

def test_reversal_idempotent_replay() -> None:
    _make_order("r3", 200)
    pid = _pay("r3", 120)["payment_id"]
    payload = {"reversal_id": "dup", "payment_id": pid}
    first = client.post("/orders/r3/reversals", json=payload, headers={"X-Tenant": "rv"})
    second = client.post("/orders/r3/reversals", json=payload, headers={"X-Tenant": "rv"})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    order = client.get("/orders/r3", headers={"X-Tenant": "rv"}).json()
    assert order["paid_cents"] == 0 and order["outstanding_cents"] == 200

def test_reversal_rejections_are_distinguishable() -> None:
    _make_order("r4", 300)
    pid = _pay("r4", 100)["payment_id"]

    missing = client.post("/orders/r4/reversals", json={"reversal_id": "m", "payment_id": 999999},
                          headers={"X-Tenant": "rv"})
    assert missing.status_code == 409
    assert missing.json()["detail"]["reason"] == "reversal_payment_not_found"

    ok = client.post("/orders/r4/reversals", json={"reversal_id": "a", "payment_id": pid},
                     headers={"X-Tenant": "rv"})
    assert ok.status_code == 200

    already = client.post("/orders/r4/reversals", json={"reversal_id": "b", "payment_id": pid},
                          headers={"X-Tenant": "rv"})
    assert already.status_code == 409
    assert already.json()["detail"]["reason"] == "reversal_payment_already_reversed"

    other = _pay("r4", 50)["payment_id"]
    conflict = client.post("/orders/r4/reversals", json={"reversal_id": "a", "payment_id": other},
                           headers={"X-Tenant": "rv"})
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["reason"] == "reversal_id_conflict"

    # 被拒绝后状态不变，金额仍闭合
    order = client.get("/orders/r4", headers={"X-Tenant": "rv"}).json()
    assert order["paid_cents"] == 50 and order["paid_cents"] + order["outstanding_cents"] == 300

def test_reversal_unknown_order_and_cross_tenant_are_404() -> None:
    resp = client.post("/orders/nope/reversals", json={"reversal_id": "x", "payment_id": 1},
                       headers={"X-Tenant": "rv"})
    assert resp.status_code == 404

    _make_order("r5", 100, tenant="owner")
    pid = _pay("r5", 100, tenant="owner")["payment_id"]
    cross = client.post("/orders/r5/reversals", json={"reversal_id": "x", "payment_id": pid},
                        headers={"X-Tenant": "other"})
    assert cross.status_code == 404
    # 原订单收款未被动过
    assert client.get("/orders/r5", headers={"X-Tenant": "owner"}).json()["paid_cents"] == 100

def test_reversal_persists_across_connections() -> None:
    _make_order("r6", 250)
    pid = _pay("r6", 250)["payment_id"]
    assert client.post("/orders/r6/reversals", json={"reversal_id": "persist", "payment_id": pid},
                       headers={"X-Tenant": "rv"}).status_code == 200
    # 每次请求都是全新连接（模拟重启）；结果可重放查询
    replay = client.post("/orders/r6/reversals", json={"reversal_id": "persist", "payment_id": pid},
                         headers={"X-Tenant": "rv"})
    assert replay.status_code == 200 and replay.json()["paid_cents"] == 0
    conn = connect()
    try:
        row = conn.execute("SELECT reversal_id, payment_id, amount_cents FROM reversals WHERE reversal_id='persist'").fetchone()
    finally:
        conn.close()
    assert dict(row) == {"reversal_id": "persist", "payment_id": pid, "amount_cents": 250}  # 留存原收款金额

def test_concurrent_payments_and_reversals_keep_invariants() -> None:
    amount = 1000
    _make_order("r7", amount, tenant="cc")
    errors: list[Exception] = []

    def pay(i: int) -> None:
        try:
            orders.add_payment("cc", "r7", 100)
        except orders.LedgerError as exc:  # 超额拒绝是允许的并发结果
            errors.append(exc)

    registered: list[int] = []
    for _ in range(10):
        result = orders.add_payment("cc", "r7", 100)
        registered.append(result["payment_id"])
    assert orders.get("cc", "r7")["paid_cents"] == amount

    def reverse(pid: int) -> None:
        try:
            orders.reverse_payment("cc", "r7", f"rev-{pid}", pid)
        except orders.LedgerError as exc:
            errors.append(exc)

    import threading
    threads = [threading.Thread(target=pay, args=(i,)) for i in range(10)]
    threads += [threading.Thread(target=reverse, args=(pid,)) for pid in registered]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 业务允许的并发失败只有“超出未收金额”，不得出现数据库错误
    assert all("exceeds outstanding" in str(e) for e in errors), errors
    order = orders.get("cc", "r7")
    assert 0 <= order["paid_cents"] <= amount
    assert order["paid_cents"] + order["outstanding_cents"] == amount

    # 逐笔核对：未被冲正的收款合计恰等于 paid_cents
    conn = connect()
    try:
        alive = conn.execute("SELECT COALESCE(SUM(amount_cents),0) AS s FROM payments WHERE tenant='cc' AND order_id='r7' AND reversed=0").fetchone()["s"]
    finally:
        conn.close()
    assert alive == order["paid_cents"]

def test_concurrent_same_reversal_id_single_effect() -> None:
    _make_order("r8", 300, tenant="cc")
    pid = orders.add_payment("cc", "r8", 300)["payment_id"]
    results: list[dict] = []

    def hit() -> None:
        results.append(orders.reverse_payment("cc", "r8", "same-rev", pid))

    import threading
    threads = [threading.Thread(target=hit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    assert all(r["paid_cents"] == 0 for r in results)
    assert orders.get("cc", "r8")["paid_cents"] == 0
