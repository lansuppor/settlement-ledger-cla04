import argparse
from typing import Any
from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from app.config import tenant_header
from app.store import batches, orders
from app.store.db import connect, migrate
from app.rules import order_rules
from app.usecase import batch_import
from app.usecase import cursor as cursor_mod

app = FastAPI(title="settlement-ledger")

# 检索每页条数上限：调用方指定值超过上限时按上限返回。
MAX_PAGE_SIZE = 200
ORDER_STATUSES = ("accepted", "settled")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

class RefundIn(BaseModel):
    refund_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class BatchRowIn(BaseModel):
    # 行内字段保持宽松：逐行校验在导入时进行，单行不合法只拒绝该行。
    order_id: Any = None
    amount_cents: Any = None
    currency: Any = None

class BatchIn(BaseModel):
    tenant: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    rows: list[BatchRowIn] = Field(max_length=5000)

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

@app.get("/orders")
def search_orders(
    x_tenant: str = Header(default=""),
    prefix: str | None = None,
    status: str | None = None,
    min_paid_cents: int | None = None,
    page_size: int = 50,
    cursor: str | None = None,
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if page_size < 1:
        raise HTTPException(status_code=400, detail="page_size must be positive")
    page_size = min(page_size, MAX_PAGE_SIZE)
    if status is not None and status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="unsupported status filter")
    if min_paid_cents is not None and min_paid_cents < 0:
        raise HTTPException(status_code=400, detail="min_paid_cents must be non-negative")
    filters = {
        "prefix": prefix or "",
        "status": status or "",
        "min_paid_cents": min_paid_cents or 0,
        "page_size": page_size,
    }
    after_order_id = None
    if cursor:
        try:
            payload = cursor_mod.decode(cursor)
        except cursor_mod.CursorError as error:
            if error.reason == "expired":
                raise HTTPException(status_code=410, detail="cursor expired") from error
            raise HTTPException(status_code=400, detail="invalid cursor") from error
        if payload.get("tenant") != x_tenant:
            raise HTTPException(status_code=400, detail="cursor tenant mismatch")
        if payload.get("filters") != filters:
            raise HTTPException(status_code=400, detail="cursor filters mismatch")
        after_order_id = payload.get("last")
    items, last_order_id, has_more = orders.search(
        x_tenant, prefix, status, min_paid_cents, page_size, after_order_id
    )
    next_cursor = cursor_mod.issue(x_tenant, filters, last_order_id) if has_more else None
    return {"items": items, "page_size": page_size, "next_cursor": next_cursor}

@app.post("/orders/batches", status_code=202)
def submit_batch(body: BatchIn) -> dict:
    rows = [
        {"order_id": row.order_id, "amount_cents": row.amount_cents, "currency": row.currency}
        for row in body.rows
    ]
    try:
        return batch_import.submit(body.tenant, body.batch_id, rows)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error

@app.get("/orders/batches/{batch_id}")
def read_batch(batch_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    view = batches.get_batch_view(x_tenant, batch_id)
    if view is None:
        raise HTTPException(status_code=404, detail="batch not found")
    if view["status"] == "processing":
        # 服务中断后查询即自愈续跑；已在途时为无操作。
        batch_import.ensure_worker(x_tenant, batch_id)
    return view

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
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))
    if order is None:
        raise HTTPException(status_code=404, detail="order not found")
    return order

@app.post("/orders/{order_id}/refunds")
def add_refund(order_id: str, body: RefundIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        result = orders.add_refund(x_tenant, order_id, body.refund_id, body.amount_cents)
    except ValueError as error:
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
    batch_import.resume_interrupted()
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)

if __name__ == "__main__":
    main()
