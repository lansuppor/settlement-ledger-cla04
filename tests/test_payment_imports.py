import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test_imports.sqlite"))

import pytest
from fastapi.testclient import TestClient

from app.entry import app
from app.store import payment_imports
from app.store.db import connect, migrate

migrate()
# 关闭服务端异常上抛，使内部错误以 500 返回，便于模拟“服务中断后续跑”
client = TestClient(app, raise_server_exceptions=False)


def _make_order(oid: str, amount: int = 500, tenant: str = "t1") -> None:
    body = {"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201


def _import(batch_id: str, lines: list, tenant: str = "t1"):
    return client.post(
        "/payment-imports",
        json={"batch_id": batch_id, "lines": lines},
        headers={"X-Tenant": tenant},
    )


def test_partial_success_with_distinguishable_reasons() -> None:
    _make_order("i1", 500)
    _make_order("i2", 100)
    _make_order("i2b", 100)
    resp = _import(
        "b1",
        [
            {"line_seq": 1, "order_id": "i1", "amount_cents": 200},          # 成功
            {"line_seq": 2, "order_id": "missing", "amount_cents": 100},     # 订单不存在
            {"line_seq": 3, "order_id": "i2", "amount_cents": 0},            # 金额非法
            {"line_seq": 4, "order_id": "i2b", "amount_cents": 300},         # 超过未收
        ],
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 4 and data["accepted"] == 1 and data["rejected"] == 3
    reasons = {r["line_seq"]: r.get("reject_reason") for r in data["results"] if r["result"] == "rejected"}
    assert reasons == {2: "order_not_found", 3: "invalid_amount", 4: "exceeds_outstanding"}

    ok = data["results"][0]
    assert ok["result"] == "accepted" and ok["payment_id"]
    assert ok["paid_cents"] == 200 and ok["outstanding_cents"] == 300 and ok["order_status"] == "accepted"

    # 被拒绝的行不产生收款，不影响已收金额
    state = client.get("/orders/i2", headers={"X-Tenant": "t1"}).json()
    assert state["paid_cents"] == 0 and state["outstanding_cents"] == 100
    assert client.get("/orders/i2b", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0


def test_full_payment_settles_order() -> None:
    _make_order("i3", 500)
    resp = _import("b2", [{"line_seq": 1, "order_id": "i3", "amount_cents": 500}])
    line = resp.json()["results"][0]
    assert line["order_status"] == "settled" and line["outstanding_cents"] == 0
    state = client.get("/orders/i3", headers={"X-Tenant": "t1"}).json()
    assert state["status"] == "settled" and state["paid_cents"] + state["outstanding_cents"] == 500


def test_duplicate_order_in_batch_second_line_rejected() -> None:
    _make_order("i4", 500)
    resp = _import(
        "b3",
        [
            {"line_seq": 1, "order_id": "i4", "amount_cents": 100},
            {"line_seq": 2, "order_id": "i4", "amount_cents": 100},
        ],
    )
    data = resp.json()
    assert data["accepted"] == 1 and data["rejected"] == 1
    second = data["results"][1]
    assert second["result"] == "rejected" and second["reject_reason"] == "duplicate_order_in_batch"
    # 后一条不得覆盖/重复扣减
    assert client.get("/orders/i4", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 100


def test_duplicate_line_seq_within_request() -> None:
    _make_order("i5", 500)
    resp = _import(
        "b4",
        [
            {"line_seq": 7, "order_id": "i5", "amount_cents": 50},
            {"line_seq": 7, "order_id": "i5", "amount_cents": 60},
        ],
    )
    data = resp.json()
    assert data["results"][0]["result"] == "accepted"
    assert data["results"][1]["reject_reason"] == "duplicate_line_seq"
    assert client.get("/orders/i5", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 50


def test_whole_batch_resubmit_is_idempotent() -> None:
    _make_order("i6", 500)
    payload = [
        {"line_seq": 1, "order_id": "i6", "amount_cents": 200},
        {"line_seq": 2, "order_id": "i6", "amount_cents": 400},  # 超额
    ]
    first = _import("b5", payload).json()
    second = _import("b5", payload).json()
    assert first == second
    # 成功行不重复登记、不重复扣减
    assert client.get("/orders/i6", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 200
    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE tenant='t1' AND order_id='i6'"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 1


def test_same_identifier_different_amount_is_conflict() -> None:
    _make_order("i7", 500)
    assert _import("b6", [{"line_seq": 1, "order_id": "i7", "amount_cents": 100}]).status_code == 200
    conflict = _import("b6", [{"line_seq": 1, "order_id": "i7", "amount_cents": 150}])
    line = conflict.json()["results"][0]
    assert line["result"] == "rejected" and line["reject_reason"] == "identifier_conflict"
    # 既有状态保持不变
    assert client.get("/orders/i7", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 100


def test_same_identifier_different_order_is_conflict() -> None:
    _make_order("i8a", 500)
    _make_order("i8b", 500)
    assert _import("b7", [{"line_seq": 1, "order_id": "i8a", "amount_cents": 100}]).status_code == 200
    resp = _import("b7", [{"line_seq": 1, "order_id": "i8b", "amount_cents": 100}])
    assert resp.json()["results"][0]["reject_reason"] == "identifier_conflict"
    assert client.get("/orders/i8b", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0


def test_cross_tenant_lines_look_like_order_not_found() -> None:
    _make_order("i9", 500, tenant="t1")
    _make_order("i9-own", 500, tenant="t2")
    resp = _import(
        "b8",
        [
            {"line_seq": 1, "order_id": "i9", "amount_cents": 100},       # t2 点 t1 的单
            {"line_seq": 2, "order_id": "i9-own", "amount_cents": 100},   # t2 自己的单
        ],
        tenant="t2",
    )
    data = resp.json()
    assert data["results"][0]["reject_reason"] == "order_not_found"
    assert data["results"][1]["result"] == "accepted"
    # t1 的订单不受影响，不泄漏存在性
    assert client.get("/orders/i9", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0


def test_resume_after_interruption_matches_continuous_run() -> None:
    _make_order("i10a", 1000)
    _make_order("i10b", 1000)
    _make_order("i10c", 300)
    payload = [
        {"line_seq": 1, "order_id": "i10a", "amount_cents": 300},
        {"line_seq": 2, "order_id": "i10b", "amount_cents": 400},
        {"line_seq": 3, "order_id": "i10c", "amount_cents": 500},  # 超过未收 300
    ]

    real_uuid4 = payment_imports.uuid4
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 2:
            # 在第二行受理途中中断：本行整体回滚，第一行已提交保留
            raise RuntimeError("simulated crash")
        return real_uuid4()

    payment_imports.uuid4 = flaky
    crashed = _import("b9", payload)
    payment_imports.uuid4 = real_uuid4
    assert crashed.status_code == 500

    # 中断点之后用同一批次标识续跑（提交完整清单）
    resumed = _import("b9", payload).json()
    assert resumed["accepted"] == 2 and resumed["rejected"] == 1
    assert resumed["results"][2]["reject_reason"] == "exceeds_outstanding"

    # 金额逐单闭合
    assert client.get("/orders/i10a", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 300
    assert client.get("/orders/i10b", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400
    state_c = client.get("/orders/i10c", headers={"X-Tenant": "t1"}).json()
    assert state_c["paid_cents"] == 0 and state_c["outstanding_cents"] == 300
    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE order_id IN ('i10a','i10b','i10c')"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 2


def test_resume_by_supplying_remaining_lines() -> None:
    _make_order("i11a", 1000)
    _make_order("i11b", 1000)
    first = _import("b10", [{"line_seq": 1, "order_id": "i11a", "amount_cents": 400}]).json()
    assert first["accepted"] == 1
    # 补交：已落库的行原样重放（不重复受理），后续行继续受理
    second = _import(
        "b10",
        [
            {"line_seq": 1, "order_id": "i11a", "amount_cents": 400},
            {"line_seq": 2, "order_id": "i11b", "amount_cents": 1000},
        ],
    ).json()
    assert second["total"] == 2 and second["accepted"] == 2
    assert client.get("/orders/i11a", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 400
    state_b = client.get("/orders/i11b", headers={"X-Tenant": "t1"}).json()
    assert state_b["paid_cents"] == 1000 and state_b["status"] == "settled"


def test_get_batch_returns_journal() -> None:
    _make_order("i12", 500)
    _import("b11", [{"line_seq": 1, "order_id": "i12", "amount_cents": 50}])
    resp = client.get("/payment-imports/b11", headers={"X-Tenant": "t1"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["batch_id"] == "b11" and data["total"] == 1 and data["accepted"] == 1
    assert data["results"][0]["payment_id"]

    assert client.get("/payment-imports/nope", headers={"X-Tenant": "t1"}).status_code == 404


def test_batch_is_tenant_scoped() -> None:
    _make_order("i13", 500, tenant="t1")
    _import("b12", [{"line_seq": 1, "order_id": "i13", "amount_cents": 50}], tenant="t1")
    # 另一租户看不到该批次
    assert client.get("/payment-imports/b12", headers={"X-Tenant": "t2"}).status_code == 404


def test_import_without_tenant_header_is_400() -> None:
    resp = client.post("/payment-imports", json={"batch_id": "b", "lines": [{"line_seq": 1, "order_id": "x", "amount_cents": 1}]})
    assert resp.status_code == 400


def test_empty_batch_is_rejected() -> None:
    resp = _import("b13", [])
    assert resp.status_code == 422


def test_replay_after_restart_matches_first_result() -> None:
    _make_order("i14", 500)
    payload = [{"line_seq": 1, "order_id": "i14", "amount_cents": 250}]
    first = _import("b14", payload).json()
    migrate()  # 等价服务重启后的重连/重放
    second = _import("b14", payload).json()
    assert first == second
    assert client.get("/orders/i14", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 250


def test_results_are_in_submission_order() -> None:
    _make_order("i15a", 1000)
    _make_order("i15b", 1000)
    _make_order("i15c", 1000)
    resp = _import(
        "b15",
        [
            {"line_seq": 30, "order_id": "i15a", "amount_cents": 100},
            {"line_seq": 10, "order_id": "i15b", "amount_cents": 100},
            {"line_seq": 20, "order_id": "i15c", "amount_cents": 100},
        ],
    )
    data = resp.json()
    assert [r["line_seq"] for r in data["results"]] == [30, 10, 20]
    assert [r["line_no"] for r in data["results"]] == [1, 2, 3]
    assert data["accepted"] == 3


@pytest.mark.parametrize("bad", [-5, 0, 1.5, "100", True, None])
def test_invalid_amount_shapes_are_line_rejections(bad) -> None:
    oid = f"i16-{type(bad).__name__}-{bad!s}"
    _make_order(oid, 500)
    batch = f"b16-{type(bad).__name__}-{bad!s}"
    resp = _import(batch, [{"line_seq": 1, "order_id": oid, "amount_cents": bad}])
    assert resp.status_code == 200
    assert resp.json()["results"][0]["reject_reason"] == "invalid_amount"
    assert client.get(f"/orders/{oid}", headers={"X-Tenant": "t1"}).json()["paid_cents"] == 0


def test_imported_payment_works_with_existing_reversal_flow() -> None:
    _make_order("i17", 500)
    pid = _import("b17", [{"line_seq": 1, "order_id": "i17", "amount_cents": 500}]).json()["results"][0]["payment_id"]
    assert client.get("/orders/i17", headers={"X-Tenant": "t1"}).json()["status"] == "settled"

    # 导入产生的收款与单笔收款无差别，可被既有冲正接口点名冲正
    resp = client.post(
        "/orders/i17/reversals",
        json={"reversal_id": "rev-import-1", "payment_id": pid},
        headers={"X-Tenant": "t1"},
    )
    assert resp.status_code == 200, resp.text
    state = client.get("/orders/i17", headers={"X-Tenant": "t1"}).json()
    assert state["status"] == "accepted" and state["paid_cents"] == 0 and state["outstanding_cents"] == 500
    # 冲正不影响导入日记账的历史受理结果
    journal = client.get("/payment-imports/b17", headers={"X-Tenant": "t1"}).json()
    assert journal["results"][0]["result"] == "accepted"
    assert journal["results"][0]["payment_id"] == pid
