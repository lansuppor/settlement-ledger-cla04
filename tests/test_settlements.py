import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_settlements.sqlite"))
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


def _live_paid(oid: str, tenant: str = "t1") -> int:
    conn = connect()
    try:
        return conn.execute(
            "SELECT COALESCE(SUM(p.amount_cents),0) AS s FROM payments p "
            "WHERE p.tenant=? AND p.order_id=? AND NOT EXISTS ("
            "SELECT 1 FROM reversals v WHERE v.tenant=p.tenant AND v.order_id=p.order_id "
            "AND v.payment_id=p.payment_id)",
            (tenant, oid),
        ).fetchone()["s"]
    finally:
        conn.close()


def test_settle_fully_paid_order_creates_doc() -> None:
    _make_order("s1", 500)
    _pay("s1", 200)
    _pay("s1", 300)
    resp = _settle("s1", "set-1")
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["settlement_id"] == "set-1"           # 调用方指定的结算标识原样回显
    assert data["settlement_doc_id"]                  # 服务端分配的单据标识
    assert data["order_id"] == "s1"
    assert data["amount_cents"] == 500                # 核销金额快照闭合
    assert data["status"] == "active"
    assert data["revoked_at"] is None
    # 订单收款状态不因核销改变
    order = client.get("/orders/s1", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 0


def test_settle_partially_paid_is_rejected() -> None:
    _make_order("s2", 500)
    _pay("s2", 200)
    resp = _settle("s2", "set-2")
    assert resp.status_code == 409 and resp.json()["detail"] == "order not fully paid"
    order = client.get("/orders/s2", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 200 and order["status"] == "accepted"
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS c FROM settlements WHERE order_id='s2'").fetchone()["c"]
    finally:
        conn.close()
    assert count == 0


def test_settle_unknown_order_is_404() -> None:
    resp = _settle("missing-order", "set-x")
    assert resp.status_code == 404 and resp.json()["detail"] == "order not found"


def test_cross_tenant_settle_is_not_found() -> None:
    _make_order("s3", 100)
    _pay("s3", 100, tenant="t1")
    resp = _settle("s3", "set-3", tenant="t2")
    assert resp.status_code == 404
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS c FROM settlements WHERE tenant='t2'").fetchone()["c"]
    finally:
        conn.close()
    assert count == 0


def test_second_active_settlement_is_rejected() -> None:
    _make_order("s4", 100)
    _pay("s4", 100)
    assert _settle("s4", "set-4a").status_code == 201
    resp = _settle("s4", "set-4b")
    assert resp.status_code == 409 and resp.json()["detail"] == "active settlement already exists for order"
    conn = connect()
    try:
        rows = conn.execute("SELECT settlement_key FROM settlements WHERE order_id='s4'").fetchall()
    finally:
        conn.close()
    assert [r["settlement_key"] for r in rows] == ["set-4a"]


def test_settle_is_idempotent_same_key_same_order() -> None:
    _make_order("s5", 300)
    _pay("s5", 100)
    _pay("s5", 200)
    first = _settle("s5", "set-5")
    second = _settle("s5", "set-5")
    assert first.status_code == 201 and second.status_code == 201
    assert first.json() == second.json()
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS c FROM settlements WHERE order_id='s5'").fetchone()["c"]
    finally:
        conn.close()
    assert count == 1


def test_settle_same_key_other_order_is_conflict() -> None:
    _make_order("s6a", 100)
    _make_order("s6b", 100)
    _pay("s6a", 100)
    _pay("s6b", 100)
    assert _settle("s6a", "set-6").status_code == 201
    resp = _settle("s6b", "set-6")
    assert resp.status_code == 409 and "another order" in resp.json()["detail"]
    # 被指向的订单不产生结算单
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT order_id FROM settlements WHERE settlement_key='set-6'"
        ).fetchall()
    finally:
        conn.close()
    assert [r["order_id"] for r in rows] == ["s6a"]


def test_reversal_blocked_while_settlement_active() -> None:
    _make_order("s7", 500)
    pid = _pay("s7", 500)["payment_id"]
    assert _settle("s7", "set-7").status_code == 201
    resp = _reverse("s7", "rev-blocked", pid)
    assert resp.status_code == 409 and "active settlement" in resp.json()["detail"]
    assert _live_paid("s7") == 500


def test_revoke_lifts_verification_and_allows_reverse_and_resettle() -> None:
    _make_order("s8", 500)
    p1 = _pay("s8", 200)["payment_id"]
    _pay("s8", 300)
    doc = _settle("s8", "set-8").json()["settlement_doc_id"]

    revoked = _revoke(doc, "revoke-8")
    assert revoked.status_code == 200, revoked.text
    data = revoked.json()
    assert data["status"] == "revoked"
    assert data["revocation_id"] == "revoke-8"
    assert data["settlement_doc_id"] == doc
    assert data["revoked_at"]

    # 撤销不改变订单收款与已收金额
    order = client.get("/orders/s8", headers={"X-Tenant": "t1"}).json()
    assert order["paid_cents"] == 500 and order["outstanding_cents"] == 0

    # 撤销后可冲正收款，再收满后可用新结算标识重新核销
    assert _reverse("s8", "rev-8", p1).status_code == 200
    _pay("s8", 200)
    again = _settle("s8", "set-8b")
    assert again.status_code == 201 and again.json()["status"] == "active"
    assert again.json()["settlement_doc_id"] != doc
    assert again.json()["amount_cents"] == 500


def test_revoke_is_idempotent() -> None:
    _make_order("s9", 100)
    _pay("s9", 100)
    doc = _settle("s9", "set-9").json()["settlement_doc_id"]
    first = _revoke(doc, "revoke-9")
    second = _revoke(doc, "revoke-9")
    assert first.status_code == 200 and second.status_code == 200
    assert first.json() == second.json()
    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM settlement_revocations WHERE settlement_id=?", (doc,)
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1


def test_revoke_same_id_other_settlement_is_conflict() -> None:
    for oid, key in (("s10a", "set-10a"), ("s10b", "set-10b")):
        _make_order(oid, 100)
        _pay(oid, 100)
    doc_a = _settle("s10a", "set-10a").json()["settlement_doc_id"]
    doc_b = _settle("s10b", "set-10b").json()["settlement_doc_id"]
    assert _revoke(doc_a, "revoke-10").status_code == 200
    resp = _revoke(doc_b, "revoke-10")
    assert resp.status_code == 409 and "another settlement" in resp.json()["detail"]
    # 另一张结算单仍处于 active
    conn = connect()
    try:
        status = conn.execute(
            "SELECT status FROM settlements WHERE settlement_id=?", (doc_b,)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert status == "active"


def test_revoke_unknown_settlement_is_404() -> None:
    resp = _revoke("no-such-doc", "revoke-x")
    assert resp.status_code == 404 and resp.json()["detail"] == "settlement not found"


def test_revoke_already_revoked_with_new_id_is_409() -> None:
    _make_order("s11", 100)
    _pay("s11", 100)
    doc = _settle("s11", "set-11").json()["settlement_doc_id"]
    assert _revoke(doc, "revoke-11a").status_code == 200
    resp = _revoke(doc, "revoke-11b")
    assert resp.status_code == 409 and resp.json()["detail"] == "settlement already revoked"


def test_cross_tenant_revoke_is_not_found() -> None:
    _make_order("s12", 100)
    _pay("s12", 100, tenant="t1")
    doc = _settle("s12", "set-12", tenant="t1").json()["settlement_doc_id"]
    resp = _revoke(doc, "revoke-12", tenant="t2")
    assert resp.status_code == 404
    conn = connect()
    try:
        status = conn.execute(
            "SELECT status FROM settlements WHERE settlement_id=?", (doc,)
        ).fetchone()["status"]
    finally:
        conn.close()
    assert status == "active"


def test_settlement_without_tenant_header_is_400() -> None:
    resp = client.post("/orders/s1/settlements", json={"settlement_id": "x"})
    assert resp.status_code == 400
    resp = client.post("/settlements/some-doc/revocations", json={"revocation_id": "x"})
    assert resp.status_code == 400


def test_settlement_persists_after_restart() -> None:
    _make_order("s13", 700)
    _pay("s13", 700)
    created = _settle("s13", "set-13").json()
    migrate()  # 等价于服务重启后重连同一数据库
    # 用同一结算标识重放仍返回同一张结算单，不重复生成
    replay = _settle("s13", "set-13")
    assert replay.status_code == 201
    assert replay.json()["settlement_doc_id"] == created["settlement_doc_id"]
    assert replay.json()["amount_cents"] == 700
    conn = connect()
    try:
        count = conn.execute("SELECT COUNT(*) AS c FROM settlements WHERE order_id='s13'").fetchone()["c"]
    finally:
        conn.close()
    assert count == 1


def test_revoke_persists_and_resettle_matches_sequential_result() -> None:
    _make_order("s14", 400)
    _pay("s14", 400)
    doc = _settle("s14", "set-14a").json()["settlement_doc_id"]
    assert _revoke(doc, "revoke-14").status_code == 200
    migrate()
    # 重启后旧结算单保持 revoked，原撤销标识幂等重放
    replay = _revoke(doc, "revoke-14")
    assert replay.status_code == 200 and replay.json()["status"] == "revoked"
    # 同一订单可再次结算
    again = _settle("s14", "set-14b")
    assert again.status_code == 201 and again.json()["status"] == "active"


def test_concurrent_settle_revoke_payment_keeps_closure() -> None:
    _make_order("sc", 1000)
    _pay("sc", 400)
    _pay("sc", 600)
    errors: list[Exception] = []

    def settle_once(key: str) -> None:
        try:
            resp = _settle("sc", key)
            assert resp.status_code in (201, 409), resp.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def revoke_when_active() -> None:
        try:
            # 找到 active 结算单即撤销；可能尚未生成或已撤销，都属合法调度结果
            conn = connect()
            try:
                row = conn.execute(
                    "SELECT settlement_id FROM settlements WHERE order_id='sc' AND status='active'"
                ).fetchone()
            finally:
                conn.close()
            if row is not None:
                resp = _revoke(row["settlement_id"], f"revoke-{row['settlement_id'][:8]}")
                assert resp.status_code in (200, 409), resp.text
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=settle_once, args=(f"set-c{i}",)) for i in range(8)]
    threads += [threading.Thread(target=revoke_when_active) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    # 不变量：任一时刻至多一张 active 结算单；active 时金额快照必须等于未冲正收款合计与订单金额
    conn = connect()
    try:
        actives = conn.execute(
            "SELECT amount_cents FROM settlements WHERE order_id='sc' AND status='active'"
        ).fetchall()
        snapshots = conn.execute(
            "SELECT amount_cents, status FROM settlements WHERE order_id='sc'"
        ).fetchall()
    finally:
        conn.close()
    assert len(actives) <= 1
    live = _live_paid("sc")
    for row in actives:
        assert row["amount_cents"] == live == 1000
    # 已撤销的结算单快照保留首次核销金额，便于事后对账
    assert all(row["amount_cents"] == 1000 for row in snapshots)
