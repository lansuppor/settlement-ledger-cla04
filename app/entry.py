import argparse
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders, payment_imports, settlements
from app.store.db import connect, migrate

app = FastAPI(title="settlement-ledger")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

class ReversalIn(BaseModel):
    reversal_id: str = Field(min_length=1)
    payment_id: str = Field(min_length=1)

class SettlementIn(BaseModel):
    settlement_id: str = Field(min_length=1)

class SettlementRevocationIn(BaseModel):
    revocation_id: str = Field(min_length=1)

class PaymentImportLineIn(BaseModel):
    # 行内字段保持宽容：金额非法等业务问题按行拒绝，而不是让整批 422
    line_seq: int
    order_id: str
    amount_cents: Any

class PaymentImportIn(BaseModel):
    batch_id: str = Field(min_length=1)
    lines: list[PaymentImportLineIn] = Field(min_length=1)

@app.get("/health")
def health() -> dict:
    conn = connect()
    try:
        conn.execute("SELECT 1")
    finally:
        conn.close()
    return {"status": "ok"}

@app.post("/orders", status_code=201)
def create_order(body: OrderIn) -> dict:
    order_rules.assert_currency(body.currency)
    try:
        orders.insert(body.tenant, body.order_id, body.amount_cents, body.currency)
    except Exception as error:
        if "UNIQUE" in str(error):
            raise HTTPException(status_code=409, detail="order already accepted")
        raise
    return orders.get(body.tenant, body.order_id)

@app.get("/orders/{order_id}")
def read_order(order_id: str, x_tenant: str = Header(default="", alias=None)) -> dict:
    tenant = x_tenant or ""
    if not tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    order = orders.get(tenant, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/payments")
def add_payment(order_id: str, body: PaymentIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        order = orders.add_payment(x_tenant, order_id, body.amount_cents)
    except orders.PaymentExceedsOutstanding as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/reversals")
def reverse_payment(order_id: str, body: ReversalIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        result = orders.reverse_payment(x_tenant, order_id, body.reversal_id, body.payment_id)
    except orders.PaymentNotFound:
        # 收款不存在（含跨租户点名）一律按不存在处理，不泄漏对象是否存在
        raise HTTPException(status_code=404, detail="payment not found")
    except orders.PaymentAlreadyReversed as error:
        raise HTTPException(status_code=409, detail=str(error))
    except orders.ReversalConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    except orders.ReversalBlockedBySettlement as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    return result

@app.get("/orders/{order_id}/settlements")
def list_order_settlements(order_id: str, status: str = "all", x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if status not in ("all", "active", "revoked"):
        raise HTTPException(status_code=400, detail="status must be one of: active, revoked, all")
    rows = settlements.list_for_order(x_tenant, order_id, None if status == "all" else status)
    if rows is None:
        # 订单不存在/跨租户点名统一按不存在处理，不泄漏对象是否存在
        raise HTTPException(status_code=404, detail="order not found")
    return {
        "tenant": x_tenant,
        "order_id": order_id,
        "status_filter": status,
        "settlements": [_settlement_response(row) for row in rows],
    }

@app.get("/orders/{order_id}/reconciliation")
def reconcile_order(order_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    result = settlements.reconcile(x_tenant, order_id)
    if result is None:
        # 订单不存在/跨租户点名统一按不存在处理，不泄漏对象是否存在
        raise HTTPException(status_code=404, detail="order not found")
    return result

@app.post("/orders/{order_id}/settlements", status_code=201)
def settle_order(order_id: str, body: SettlementIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        view, _created = settlements.settle(x_tenant, order_id, body.settlement_id)
    except settlements.SettlementNotFound as error:
        # 订单不存在/跨租户点名统一按不存在处理
        raise HTTPException(status_code=404, detail=str(error))
    except settlements.OrderNotFullyPaid as error:
        raise HTTPException(status_code=409, detail=str(error))
    except settlements.SettlementNotBalanced as error:
        raise HTTPException(status_code=409, detail=str(error))
    except settlements.SettlementAlreadyActive as error:
        raise HTTPException(status_code=409, detail=str(error))
    except settlements.SettlementKeyConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    return _settlement_response(view)

@app.post("/settlements/{settlement_doc_id}/revocations")
def revoke_settlement(settlement_doc_id: str, body: SettlementRevocationIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        view = settlements.revoke(x_tenant, settlement_doc_id, body.revocation_id)
    except settlements.SettlementNotFound:
        # 结算单不存在（含跨租户点名、撤销不存在的结算单）按不存在处理
        raise HTTPException(status_code=404, detail="settlement not found")
    except settlements.SettlementAlreadyRevoked as error:
        raise HTTPException(status_code=409, detail=str(error))
    except settlements.RevocationConflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    return _settlement_response(view)

def _settlement_response(view: dict) -> dict:
    # 对外字段：settlement_id 为调用方指定的结算标识（原样回显），
    # settlement_doc_id 为服务端分配的结算单单据标识（撤销时点名使用）
    return {
        "settlement_doc_id": view["settlement_id"],
        "settlement_id": view["settlement_key"],
        "order_id": view["order_id"],
        "tenant": view["tenant"],
        "amount_cents": view["amount_cents"],
        "status": view["status"],
        "created_at": view["created_at"],
        "revoked_at": view["revoked_at"],
        **({"revocation_id": view["revocation_id"]} if view.get("revocation_id") is not None else {}),
    }

@app.post("/payment-imports")
def import_payments(body: PaymentImportIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    lines = [
        payment_imports.ImportLine(
            line_no=line_no,
            line_seq=item.line_seq,
            order_id=item.order_id,
            amount_cents=item.amount_cents,
        )
        for line_no, item in enumerate(body.lines, start=1)
    ]
    # 逐行独立事务：业务拒绝落到逐行结果；内部错误时出错行整笔回滚并以 5xx 返回，
    # 此前已提交的行保持生效，调用方用同一批次标识续跑即可补齐剩余行
    return payment_imports.submit(x_tenant, body.batch_id, lines)

@app.get("/payment-imports/{batch_id}")
def read_payment_import(batch_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    batch = payment_imports.get_batch(x_tenant, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="import batch not found")
    return batch

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    migrate()
    if args.migrate:
        print("migrated")
        return
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)

if __name__ == "__main__":
    main()
