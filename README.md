# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款与退款并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

## 环境与安装

- Python 3.11
- `python3 -m venv .venv && . .venv/bin/activate && pip install -e .`

## 启动

- `python3 -m app.entry --port 8000`
- 健康检查：`GET /health`

## 测试

- `pytest -q`
- 静态检查：`ruff check .`

## 已有公开接口

- `POST /orders`：受理订单。请求字段 `tenant`、`order_id`、`amount_cents`、`currency`。成功返回 201 与订单对象；参数不合法返回 400；同一租户重复受理返回 409。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /orders/{order_id}/refunds`：登记退款。请求字段 `refund_id`、`amount_cents`；退款不得超过当前可退净额（已收 − 已退），超出返回 409（`refund exceeds refundable amount`）；同一租户内 `refund_id` 唯一，被其他订单占用返回 409（`refund id already used`）；同一订单重复提交同一 `refund_id` 不重复扣减，返回首次登记时的同样结果；成功返回 200 与订单的 `paid_cents`、`refunded_cents`、`outstanding_cents`。退回金额重新回到未收金额，可再次收款。
- `GET /health`：返回服务与数据库状态。

### 退款调用示例

```sh
curl -X POST http://127.0.0.1:8000/orders/o1/refunds \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant: t1' \
  -d '{"refund_id": "r1", "amount_cents": 100}'
# => {"order_id": "o1", "paid_cents": 300, "refunded_cents": 100, "outstanding_cents": 100}
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 未实现分期与对账。
