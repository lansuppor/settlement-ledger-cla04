import os
import tempfile

os.environ.setdefault("APP_DB", os.path.join(tempfile.mkdtemp(), "test.sqlite"))

import pytest
from fastapi.testclient import TestClient

from app.entry import app
from app.store import imports
from app.store.db import connect, migrate

migrate()
client = TestClient(app)

TENANT = "ti"


def _make_order(oid: str, amount: int = 500, tenant: str = TENANT) -> None:
    body = {"tenant": tenant, "order_id": oid, "amount_cents": amount, "currency": "CNY"}
    assert client.post("/orders", json=body).status_code == 201


def _order(oid: str, tenant: str = TENANT) -> dict:
    return client.get(f"/orders/{oid}", headers={"X-Tenant": tenant}).json()


def _import(batch_id: str, lines: list[dict], tenant: str = TENANT):
    return client.post(
        "/payment-imports",
        json={"batch_id": batch_id, "lines": lines},
        headers={"X-Tenant": tenant},
    )


def _reasons(resp) -> dict:
    return {line["line_no"]: line.get("reject_reason") for line in resp.json()["lines"]}


def _by_line(resp) -> dict:
    return {line["line_no"]: line for line in resp.json()["lines"]}


def test_batch_partial_success_with_distinguishable_reasons() -> None:
    _make_order("i1", 500)
    _make_order("i2", 500)
    _make_order("i3", 500)
    resp = _import("b-partial", [
        {"line_no": 1, "order_id": "i1", "amount_cents": 200},
        {"line_no": 2, "order_id": "i-missing", "amount_cents": 100},
        {"line_no": 3, "order_id": "i2", "amount_cents": 600},   # 超过未收
        {"line_no": 4, "order_id": "i2", "amount_cents": 300},   # 同订单更靠前的行已存在 → 重复
        {"line_no": 5, "order_id": "i3", "amount_cents": 300},   # 其他订单的行不受影响
    ])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["total"] == 5 and data["accepted_count"] == 2 and data["rejected_count"] == 3
    reasons = _reasons(resp)
    assert reasons[2] == "order_not_found"
    assert reasons[3] == "exceeds_outstanding"
    assert reasons[4] == "duplicate_order"

    first = _by_line(resp)[1]
    assert first["status"] == "accepted" and first["payment_id"]
    assert first["order_status"] == "accepted" and first["paid_cents"] == 200
    assert first["outstanding_cents"] == 300

    fifth = _by_line(resp)[5]
    assert fifth["status"] == "accepted" and fifth["payment_id"]
    assert fifth["paid_cents"] == 300

    # 被拒行不产生收款，闭合不变量保持
    assert _order("i1")["paid_cents"] == 200
    assert _order("i2")["paid_cents"] == 0
    assert _order("i3")["paid_cents"] == 300


def test_settles_order_when_fully_paid_in_batch() -> None:
    _make_order("i-full", 400)
    resp = _import("b-full", [{"line_no": 1, "order_id": "i-full", "amount_cents": 400}])
    line = resp.json()["lines"][0]
    assert line["order_status"] == "settled"
    assert line["paid_cents"] == 400 and line["outstanding_cents"] == 0
    assert _order("i-full")["status"] == "settled"


def test_duplicate_order_within_batch_later_line_rejected() -> None:
    _make_order("i-dup", 500)
    resp = _import("b-dup", [
        {"line_no": 1, "order_id": "i-dup", "amount_cents": 100},
        {"line_no": 2, "order_id": "i-dup", "amount_cents": 100},  # 后一条不得覆盖前一条
    ])
    assert resp.json()["accepted_count"] == 1 and resp.json()["rejected_count"] == 1
    assert _reasons(resp)[2] == "duplicate_order"
    assert _order("i-dup")["paid_cents"] == 100  # 只登记一笔


def test_invalid_amount_is_line_level_rejection() -> None:
    _make_order("i-amt", 500)
    _make_order("i-amt-ok", 500)
    resp = _import("b-amt", [
        {"line_no": 1, "order_id": "i-amt", "amount_cents": 0},
        {"line_no": 2, "order_id": "i-amt", "amount_cents": -5},
        {"line_no": 3, "order_id": "i-amt", "amount_cents": "50"},
        {"line_no": 4, "order_id": "i-amt-ok", "amount_cents": 100},  # 其他订单的行不受影响
    ])
    reasons = _reasons(resp)
    assert reasons[1] == reasons[2] == reasons[3] == "invalid_amount"
    assert _by_line(resp)[4]["status"] == "accepted"
    assert resp.json()["accepted_count"] == 1 and resp.json()["rejected_count"] == 3
    assert _order("i-amt")["paid_cents"] == 0
    assert _order("i-amt-ok")["paid_cents"] == 100


def test_valid_line_after_invalid_line_on_same_order_is_still_duplicate() -> None:
    # 位置规则：同批次内同一订单标识只受理最靠前的一条；首条即便因金额非法未登记，
    # 后一条同订单仍记为重复，保证中断续跑与一次连续跑完结果一致
    _make_order("i-amt-dup", 500)
    resp = _import("b-amt-dup", [
        {"line_no": 1, "order_id": "i-amt-dup", "amount_cents": 0},
        {"line_no": 2, "order_id": "i-amt-dup", "amount_cents": 100},
    ])
    reasons = _reasons(resp)
    assert reasons[1] == "invalid_amount" and reasons[2] == "duplicate_order"
    assert _order("i-amt-dup")["paid_cents"] == 0


def test_duplicate_line_no_within_submission_is_rejected() -> None:
    _make_order("i-ln1", 500)
    _make_order("i-ln2", 500)
    resp = _import("b-ln", [
        {"line_no": 1, "order_id": "i-ln1", "amount_cents": 100},
        {"line_no": 1, "order_id": "i-ln2", "amount_cents": 100},  # 序号重复
    ])
    assert resp.status_code == 200
    lines = resp.json()["lines"]  # 同序号两行都保留，按提交顺序
    assert lines[0]["status"] == "accepted" and lines[0]["payment_id"]
    # 后一条同序号：按本次提交位置给出重复拒绝，不覆盖第一条
    assert lines[1]["status"] == "rejected" and lines[1]["reject_reason"] == "line_no_taken"
    assert _order("i-ln1")["paid_cents"] == 100
    assert _order("i-ln2")["paid_cents"] == 0


def test_whole_list_resubmit_is_idempotent() -> None:
    _make_order("i-id1", 500)
    _make_order("i-id2", 500)
    _make_order("i-id3", 500)
    payload = [
        {"line_no": 1, "order_id": "i-id1", "amount_cents": 200},
        {"line_no": 2, "order_id": "i-id2", "amount_cents": 600},  # 超过未收，被拒
        {"line_no": 3, "order_id": "i-id3", "amount_cents": 100},
    ]
    first = _import("b-idem", payload)
    second = _import("b-idem", payload)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()  # 返回结果与首次一致

    assert _order("i-id1")["paid_cents"] == 200
    assert _order("i-id2")["paid_cents"] == 0
    assert _order("i-id3")["paid_cents"] == 100

    # 明细层不重复登记
    conn = connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS c FROM payments WHERE order_id IN ('i-id1','i-id2','i-id3')"
        ).fetchone()["c"]
    finally:
        conn.close()
    assert count == 2


def test_same_identifier_different_amount_is_conflict() -> None:
    _make_order("i-conf", 500)
    first = _import("b-conf", [{"line_no": 1, "order_id": "i-conf", "amount_cents": 100}])
    pid = first.json()["lines"][0]["payment_id"]

    # 同一 (批次号, 行内序号) 带不同金额：冲突拒绝，既有状态不变
    resp = _import("b-conf", [{"line_no": 1, "order_id": "i-conf", "amount_cents": 200}])
    line = resp.json()["lines"][0]
    assert line["status"] == "rejected" and line["reject_reason"] == "identifier_conflict"
    state = _order("i-conf")
    assert state["paid_cents"] == 100

    # 原金额重放仍幂等，收款标识不变
    replay = _import("b-conf", [{"line_no": 1, "order_id": "i-conf", "amount_cents": 100}])
    assert replay.json()["lines"][0]["payment_id"] == pid


def test_resume_after_interruption_matches_continuous_run() -> None:
    # 三组同构订单：中断批用 i-rs*，对照批（一次连续跑完）用 i-ok*
    for oid in ("i-rs1", "i-rs2", "i-rs3", "i-ok1", "i-ok2", "i-ok3"):
        _make_order(oid, 500)
    rs_lines = [
        {"line_no": 1, "order_id": "i-rs1", "amount_cents": 500},
        {"line_no": 2, "order_id": "i-rs2", "amount_cents": 600},  # 超额，本应被拒
        {"line_no": 3, "order_id": "i-rs3", "amount_cents": 200},
    ]
    ok_lines = [
        {"line_no": 1, "order_id": "i-ok1", "amount_cents": 500},
        {"line_no": 2, "order_id": "i-ok2", "amount_cents": 600},
        {"line_no": 3, "order_id": "i-ok3", "amount_cents": 200},
    ]

    # 首次提交在处理第 2 行时中断：第 1 行已落库保留
    with pytest.raises(RuntimeError):
        imports.submit(TENANT, "b-rs", rs_lines, internal_failure={"line_no": 2})
    assert _order("i-rs1")["paid_cents"] == 500
    assert _order("i-rs2")["paid_cents"] == 0
    assert _order("i-rs3")["paid_cents"] == 0

    # 用同一批次号续跑（不带故障钩子）
    resumed = imports.submit(TENANT, "b-rs", rs_lines)
    clean = imports.submit(TENANT, "b-ok", ok_lines)

    def shape(result: dict) -> list[tuple]:
        return [
            (line["line_no"], line["status"], line.get("reject_reason"),
             line.get("order_status"), line.get("paid_cents"), line.get("outstanding_cents"))
            for line in result["lines"]
        ]

    # 逐行成败与订单最新状态与一次连续跑完一致（仅订单标识/收款标识不同）
    assert shape(resumed) == shape(clean)
    assert resumed["accepted_count"] == clean["accepted_count"] == 2
    assert resumed["rejected_count"] == clean["rejected_count"] == 1

    # 已落库行未重复受理：第 1 行收款标识续跑前后一致，金额不重复扣减
    stored = client.get("/payment-imports/b-rs", headers={"X-Tenant": TENANT}).json()
    line1 = stored["lines"][0]
    assert line1["payment_id"] and _order("i-rs1")["paid_cents"] == 500

    # 闭合：已收 + 未收 = 订单金额
    for oid in ("i-rs1", "i-rs2", "i-rs3"):
        state = _order(oid)
        assert state["paid_cents"] + state["outstanding_cents"] == state["amount_cents"]


def test_cross_tenant_lines_are_order_not_found() -> None:
    _make_order("i-x", 500, tenant="t1")
    _make_order("i-y", 500, tenant="t2")
    # t2 提交：点名 t1 的订单一律按不存在；t2 自己的行正常受理
    resp = _import("b-cross", [
        {"line_no": 1, "order_id": "i-x", "amount_cents": 100},
        {"line_no": 2, "order_id": "i-y", "amount_cents": 100},
    ], tenant="t2")
    reasons = _reasons(resp)
    assert reasons[1] == "order_not_found"
    assert _by_line(resp)[2]["status"] == "accepted"
    assert _order("i-x", tenant="t1")["paid_cents"] == 0
    assert _order("i-y", tenant="t2")["paid_cents"] == 100


def test_get_batch_returns_persisted_lines_and_cross_tenant_is_404() -> None:
    _make_order("i-get", 500)
    _import("b-get", [
        {"line_no": 1, "order_id": "i-get", "amount_cents": 100},
        {"line_no": 2, "order_id": "nope", "amount_cents": 100},
    ])
    resp = client.get("/payment-imports/b-get", headers={"X-Tenant": TENANT})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2 and data["accepted_count"] == 1 and data["rejected_count"] == 1
    assert data["lines"][0]["payment_id"] and data["lines"][1]["reject_reason"] == "order_not_found"

    assert client.get("/payment-imports/b-get", headers={"X-Tenant": "other"}).status_code == 404
    assert client.get("/payment-imports/missing", headers={"X-Tenant": TENANT}).status_code == 404


def test_batch_request_validation() -> None:
    # 无租户头
    resp = client.post("/payment-imports", json={"batch_id": "b", "lines": [
        {"line_no": 1, "order_id": "x", "amount_cents": 1}]})
    assert resp.status_code == 400
    # 空清单
    assert client.post("/payment-imports", json={"batch_id": "b", "lines": []},
                       headers={"X-Tenant": TENANT}).status_code == 422
    # 非法行内序号 / 缺少订单标识：整单请求格式错误
    assert client.post("/payment-imports", json={"batch_id": "b", "lines": [
        {"line_no": 0, "order_id": "x", "amount_cents": 1}]},
        headers={"X-Tenant": TENANT}).status_code == 422
    assert client.post("/payment-imports", json={"batch_id": "b", "lines": [
        {"line_no": 1, "order_id": "", "amount_cents": 1}]},
        headers={"X-Tenant": TENANT}).status_code == 422


def test_batch_payment_is_reversible_via_existing_api() -> None:
    _make_order("i-rev", 500)
    resp = _import("b-rev", [{"line_no": 7, "order_id": "i-rev", "amount_cents": 500}])
    pid = resp.json()["lines"][0]["payment_id"]
    assert _order("i-rev")["status"] == "settled"
    r = client.post("/orders/i-rev/reversals",
                    json={"reversal_id": "rev-batch-1", "payment_id": pid},
                    headers={"X-Tenant": TENANT})
    assert r.status_code == 200 and r.json()["status"] == "accepted"
    assert _order("i-rev")["paid_cents"] == 0
