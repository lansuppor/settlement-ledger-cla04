# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款与按笔登记退款、批量异步导入订单与条件检索订单清单，并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/batches`：批量异步导入订单。请求字段 `tenant`、`batch_id`（调用方提供，同一租户内唯一）、`rows`（每行 `order_id`、`amount_cents`、`currency`，沿用单笔受理的全部规则）。受理成功返回 202 与受理回执 `{tenant, batch_id, status: "accepted", total_rows}`；导入逐行校验、部分成功，单行不合法或与库内已有订单重复只拒绝该行。同一租户重复提交同一批次标识且内容一致时返回与首次完全一致的回执、不重复受理；内容不一致返回 409。
- `GET /orders/batches/{batch_id}`：查询批次进度与结果。租户通过请求头 `X-Tenant` 传入；返回 `status`（`processing`/`completed`）、`succeeded_rows`、`failed_rows` 与每一失败行的 `line_no`、`order_id`、`error`；恒有 成功行数 + 失败行数 = 提交总行数。跨租户查询返回 404。服务中断重启后自动续跑未完成的批次，已生效行不重复受理。
- `GET /orders`：条件检索订单清单。租户通过请求头 `X-Tenant` 传入；查询参数 `prefix`（订单标识前缀）、`status`（`accepted`/`settled`）、`min_paid_cents`（已收金额下限）可任意组合，未提供表示不限制；`page_size` 指定每页条数（默认 50，上限 200，超过按上限返回）。结果按订单标识升序返回，还有下一页时响应携带 `next_cursor`，将其作为 `cursor` 参数继续读取。游标仅在同租户、同过滤条件下有效：跨租户或条件变化返回 400（原因分别为 `cursor tenant mismatch`/`cursor filters mismatch`/`invalid cursor`），过期返回 410（`cursor expired`）。检索严格限定在调用方租户内，跨租户订单永不出现。
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

### 批量导入调用示例

```bash
# 提交批次（异步受理，立即返回回执）
curl -s -X POST localhost:8000/orders/batches \
  -H 'Content-Type: application/json' \
  -d '{"tenant": "t1", "batch_id": "imp-20261002-01", "rows": [
        {"order_id": "demo-1", "amount_cents": 1200, "currency": "CNY"},
        {"order_id": "demo-2", "amount_cents": 800,  "currency": "CNY"}
      ]}'
# => {"tenant":"t1","batch_id":"imp-20261002-01","status":"accepted","total_rows":2}
# 查询进度与逐行结果（失败行带行号与原因）
curl -s localhost:8000/orders/batches/imp-20261002-01 -H 'X-Tenant: t1'
# => {"status":"completed","succeeded_rows":2,"failed_rows":0,"failures":[],...}
# 同一租户重复提交同一批次标识且内容一致：原样返回首次回执，不重复受理
```

### 条件检索调用示例

```bash
# 组合过滤：前缀 + 状态 + 已收金额下限，按订单标识升序
curl -s 'localhost:8000/orders?prefix=demo-&status=accepted&min_paid_cents=100&page_size=2' \
  -H 'X-Tenant: t1'
# => {"items":[...],"page_size":2,"next_cursor":"eyJ..."}
# 携带游标读取下一页（同租户、同过滤条件）
curl -s 'localhost:8000/orders?prefix=demo-&status=accepted&min_paid_cents=100&page_size=2&cursor=eyJ...' \
  -H 'X-Tenant: t1'
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）、`APP_CURSOR_SECRET`（检索游标签名密钥，默认开发值，生产应覆盖）、`APP_CURSOR_TTL_SECONDS`（游标有效期，默认 1800 秒）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入为单进程内异步逐行处理，单批上限 5000 行。
- 收款支持按未收金额多笔登记与按笔退款，未实现分期计划与对账。
