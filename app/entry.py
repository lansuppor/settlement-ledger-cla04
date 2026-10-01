import argparse

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.rules import order_rules
from app.store import orders
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
