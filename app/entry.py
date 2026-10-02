import argparse
import time

from fastapi import FastAPI, Header, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import cursor_ttl_seconds
from app.rules import cursor as cursors
from app.rules import order_rules
from app.store import debts, imports, ledger, orders, payments, reconciliations, settlements, tickets
from app.store.db import connect, migrate

app = FastAPI(title="settlement-ledger")

@app.exception_handler(RequestValidationError)
def validation_error_handler(_request, error: RequestValidationError) -> JSONResponse:
    # 参数不合法统一返回 400（与既有公开约定一致），不暴露内部校验细节。
    return JSONResponse(status_code=400, content={"detail": "invalid request parameters"})

ORDER_STATUSES = ("accepted", "settled")

class OrderIn(BaseModel):
    tenant: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)

class PaymentIn(BaseModel):
    amount_cents: int = Field(gt=0)

class PaymentReversalIn(BaseModel):
    reversal_id: str = Field(min_length=1)
    payment_ref: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class RefundIn(BaseModel):
    refund_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class WriteoffIn(BaseModel):
    writeoff_id: str = Field(min_length=1)
    debt_no: int = Field(gt=0)
    amount_cents: int = Field(gt=0)

class SettlementIn(BaseModel):
    settlement_id: str = Field(min_length=1)
    amount_cents: int = Field(gt=0)

class ReversalIn(BaseModel):
    reversal_id: str = Field(min_length=1)
    reason: str = ""

class ReconciliationIn(BaseModel):
    tenant: str = Field(min_length=1)
    reconciliation_id: str = Field(min_length=1)

class ImportIn(BaseModel):
    tenant: str = Field(min_length=1)
    batch_id: str = Field(min_length=1)
    # 行内容逐行校验、部分成功，这里不做整批预校验。
    rows: list[dict] = Field(default_factory=list)

class TicketIn(BaseModel):
    tenant: str = Field(min_length=1)
    ticket_id: str = Field(min_length=1)
    order_id: str = Field(min_length=1)
    ticket_type: str = Field(min_length=1)
    description: str = Field(min_length=1)

class TicketProcessIn(BaseModel):
    status: str = Field(min_length=1)
    note: str | None = None

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

@app.post("/orders/import", status_code=202)
def import_orders(body: ImportIn) -> dict:
    try:
        return imports.submit(body.tenant, body.batch_id, body.rows)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error))

@app.get("/orders/import/{batch_id}")
def import_status(batch_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    batch = imports.get_batch(x_tenant, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="batch not found")
    return batch

@app.get("/orders")
def search_orders(
    order_id_prefix: str | None = None,
    status: str | None = None,
    min_paid_cents: int | None = None,
    page_size: int = 50,
    cursor: str | None = None,
    x_tenant: str = Header(default=""),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if status is not None and status not in ORDER_STATUSES:
        raise HTTPException(status_code=400, detail="unsupported status")
    if min_paid_cents is not None and min_paid_cents < 0:
        raise HTTPException(status_code=400, detail="min_paid_cents must be non-negative")
    filters = {"order_id_prefix": order_id_prefix, "status": status, "min_paid_cents": min_paid_cents}
    after = None
    if cursor is not None:
        # 游标只在同租户、同过滤条件且未过期时有效，各类失败原因可区分。
        try:
            data = cursors.decode(cursor)
        except cursors.CursorError as error:
            raise HTTPException(status_code=400, detail=error.reason)
        if data["tenant"] != x_tenant:
            raise HTTPException(status_code=400, detail="cursor_tenant_mismatch")
        if time.time() - float(data["iat"]) > cursor_ttl_seconds():
            raise HTTPException(status_code=400, detail="cursor_expired")
        if data["filters"] != filters:
            raise HTTPException(status_code=400, detail="cursor_filter_mismatch")
        after = data["last"]
    page, has_more = orders.search(x_tenant, order_id_prefix, status, min_paid_cents, page_size, after)
    next_cursor = None
    if has_more and page:
        next_cursor = cursors.encode(
            {"v": 1, "tenant": x_tenant, "filters": filters, "last": page[-1]["order_id"], "iat": time.time()}
        )
    return {"tenant": x_tenant, "orders": page, "next_cursor": next_cursor}

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

@app.post("/orders/{order_id}/payments/reversals")
def reverse_payment(order_id: str, body: PaymentReversalIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        result = payments.reverse_payment(
            x_tenant, order_id, body.reversal_id, body.payment_ref, body.amount_cents
        )
    except payments.Conflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        # 被撤销收款不存在、订单不存在或跨租户统一按不存在处理，不泄漏对象是否存在。
        raise HTTPException(status_code=404, detail="payment not found")
    return result

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

@app.post("/orders/{order_id}/writeoffs", status_code=201)
def register_writeoff(order_id: str, body: WriteoffIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        result = debts.register(
            x_tenant, order_id, body.writeoff_id, body.debt_no, body.amount_cents
        )
    except debts.Conflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        # 订单或欠款条目不存在、跨租户统一按不存在处理，不泄漏对象是否存在。
        raise HTTPException(status_code=404, detail="debt entry not found")
    return result

@app.get("/orders/{order_id}/debts")
def list_order_debts(order_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    items = debts.list_debts(x_tenant, order_id)
    if items is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"tenant": x_tenant, "order_id": order_id, "debts": items}

@app.post("/orders/{order_id}/settlements", status_code=201)
def settle_order(order_id: str, body: SettlementIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        result = settlements.settle(x_tenant, order_id, body.settlement_id, body.amount_cents)
    except settlements.Conflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="order not found")
    return result

@app.post("/orders/{order_id}/settlements/{settlement_id}/reversals", status_code=201)
def reverse_settlement(
    order_id: str, settlement_id: str, body: ReversalIn, x_tenant: str = Header(default="")
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    try:
        result = settlements.reverse(
            x_tenant, order_id, settlement_id, body.reversal_id, body.reason
        )
    except settlements.Conflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        # 结算不存在、不属于该订单或跨租户统一按不存在处理，不泄漏对象是否存在。
        raise HTTPException(status_code=404, detail="settlement not found")
    return result

@app.get("/orders/{order_id}/ledger")
def order_ledger(order_id: str, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    entries = ledger.list_entries(x_tenant, order_id)
    if entries is None:
        raise HTTPException(status_code=404, detail="order not found")
    return {"tenant": x_tenant, "order_id": order_id, "entries": entries}

@app.post("/reconciliations", status_code=201)
def start_reconciliation(body: ReconciliationIn) -> dict:
    # 租户在请求体内声明（与订单受理、批量导入的现有约定一致），查询入口则使用 X-Tenant 头。
    return reconciliations.start(body.tenant, body.reconciliation_id)

@app.get("/reconciliations/{reconciliation_id}")
def read_reconciliation(
    reconciliation_id: str, x_tenant: str = Header(default="")
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    result = reconciliations.get(x_tenant, reconciliation_id)
    if result is None:
        raise HTTPException(status_code=404, detail="reconciliation not found")
    return result

@app.post("/tickets", status_code=201)
def register_ticket(body: TicketIn) -> dict:
    # 租户在请求体内声明（与订单受理、对账发起的现有约定一致）。
    if body.ticket_type not in tickets.TICKET_TYPES:
        raise HTTPException(status_code=400, detail="unsupported ticket_type")
    try:
        result = tickets.register(
            body.tenant, body.ticket_id, body.order_id, body.ticket_type, body.description
        )
    except tickets.Conflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        # 订单不存在或跨租户统一按不存在处理，不泄漏对象是否存在。
        raise HTTPException(status_code=404, detail="order not found")
    return result

@app.post("/tickets/{ticket_id}/process")
def process_ticket(ticket_id: str, body: TicketProcessIn, x_tenant: str = Header(default="")) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if body.status not in tickets.STATUSES:
        raise HTTPException(status_code=400, detail="unsupported status")
    try:
        result = tickets.process(x_tenant, ticket_id, body.status, body.note)
    except tickets.Conflict as error:
        raise HTTPException(status_code=409, detail=str(error))
    if result is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return result

@app.get("/tickets")
def search_tickets(
    order_id: str | None = None,
    status: str | None = None,
    page_size: int = 50,
    cursor: str | None = None,
    x_tenant: str = Header(default=""),
) -> dict:
    if not x_tenant:
        raise HTTPException(status_code=400, detail="tenant header is required")
    if status is not None and status not in tickets.STATUSES:
        raise HTTPException(status_code=400, detail="unsupported status")
    filters = {"order_id": order_id, "status": status}
    after = None
    if cursor is not None:
        # 游标只在同租户、同过滤条件且未过期时有效，各类失败原因可区分（与订单检索一致）。
        try:
            data = cursors.decode(cursor)
        except cursors.CursorError as error:
            raise HTTPException(status_code=400, detail=error.reason)
        if data["tenant"] != x_tenant:
            raise HTTPException(status_code=400, detail="cursor_tenant_mismatch")
        if time.time() - float(data["iat"]) > cursor_ttl_seconds():
            raise HTTPException(status_code=400, detail="cursor_expired")
        if data["filters"] != filters:
            raise HTTPException(status_code=400, detail="cursor_filter_mismatch")
        after = (data["last"][0], data["last"][1])
    page, has_more = tickets.search(x_tenant, order_id, status, page_size, after)
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = cursors.encode(
            {
                "v": 1,
                "tenant": x_tenant,
                "filters": filters,
                "last": [last["order_id"], last["ticket_id"]],
                "iat": time.time(),
            }
        )
    return {"tenant": x_tenant, "tickets": page, "next_cursor": next_cursor}

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    migrate()
    imports.resume_incomplete()
    if args.migrate:
        print("migrated")
        return
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port)

if __name__ == "__main__":
    main()
