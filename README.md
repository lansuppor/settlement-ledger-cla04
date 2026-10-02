# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款与按笔登记退款，并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/{order_id}/refunds`：按笔登记退款。租户通过请求头 `X-Tenant` 传入；请求字段 `refund_id`（调用方提供，同一租户内唯一）、`amount_cents`。成功返回 200 与订单的 `paid_cents`、`refunded_cents`、`outstanding_cents`；同一退款标识重复提交返回与首次一致的结果（不重复扣减）；不同订单复用同一退款标识返回 409；退款超过当前可退净额（已收 − 已退）返回 409；订单不存在或跨租户返回 404（不泄漏对象是否存在）；缺少租户头/参数不合法返回 400。
- `GET /health`：返回服务与数据库状态。

### 退款调用示例

```bash
# 先收款 600
curl -s -X POST localhost:8000/orders/demo-1/payments \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"amount_cents": 600}'
# 登记退款 200（退款标识由调用方生成）
curl -s -X POST localhost:8000/orders/demo-1/refunds \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"refund_id": "rf-20261002-0001", "amount_cents": 200}'
# => {"paid_cents":600,"refunded_cents":200,"outstanding_cents":800}
# 同一退款标识再次提交：原样返回首次结果，账面不发生变化
```

退款成功后退回的 200 重新计入未收金额，可再次收款；退款累计不得超过已收净额。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款支持按未收金额多笔登记与按笔退款，未实现分期计划与对账。
