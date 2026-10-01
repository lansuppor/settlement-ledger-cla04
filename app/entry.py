import argparse
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import imports, orders
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

class PaymentImportLineIn(BaseModel):
    # 行内序号：批次内唯一，与批次号共同构成导入行标识；序号本身必须合法，
    # 否则整份请求无法建立标识，按请求格式错误（422）处理
    line_no: int = Field(gt=0)
    order_id: str = Field(min_length=1)
    # 金额放宽容错：非法金额（非正整数等）作为该行的逐行拒绝结果返回，不拖垮整批
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
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    return result

@app.post("/payment-imports")
def submit_payment_import(body: PaymentImportIn, x_tenant: str = Header(default="")) -> dict:
    """批量导入收款：逐笔校验、部分成功落库、可凭同批次号安全续跑。

    租户由请求头 X-Tenant 指定；批次号 batch_id 与每行行内序号 line_no 共同
    构成导入行标识。返回本批总数、成功/拒绝计数与逐条结果。业务拒绝落在逐行
    结果里（200），只有内部错误才返回 5xx。
    """
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    lines = [
        {"line_no": line.line_no, "order_id": line.order_id, "amount_cents": line.amount_cents}
        for line in body.lines
    ]
    # 业务拒绝逐行落库并在 200 响应中给出；非预期内部错误向上抛为 5xx，
    # 且出错行已整行回滚——调用方用同一批次号重放即可安全续跑，不会重复登记。
    return imports.submit(x_tenant, body.batch_id, lines)

@app.get("/payment-imports/{batch_id}")
def read_payment_import(batch_id: str, x_tenant: str = Header(default="")) -> dict:
    """按批次号查询一次导入的逐行受理结果；跨租户/不存在返回 404。"""
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    result = imports.get_batch(x_tenant, batch_id)
    if result is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return result

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
