# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款、按笔登记退款、批量导入订单与条件检索，并核对未收金额；在此之上提供订单结算、冲正、订单账务历史与租户对账核销，形成可解释、可重放的账务闭环；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/import`：批量导入订单（异步受理）。请求字段 `tenant`、`batch_id`（调用方提供，同一租户内唯一）、`rows`（每行含 `order_id`、`amount_cents`、`currency`）。逐行校验、部分成功：单行不合法或与库内已有订单重复只拒绝该行，不影响同批其它行，也不改动既有订单。提交立即返回 202 与受理回执 `{tenant, batch_id, status: "accepted", total_rows}`；同一租户以同一批次标识、同一内容重复提交返回完全一致的回执且不重复受理；同标识不同内容返回 409。服务中断重启后自动续跑未完成的批次，已生效的行不重复受理，最终 `success_count + failure_count = total_rows`。
- `GET /orders/import/{batch_id}`：查询批次进度与结果。租户通过请求头 `X-Tenant` 传入；不存在或跨租户返回 404。返回 `status`（`processing`/`completed`）、`total_rows`、`processed_rows`、`success_count`、`failure_count` 与 `failures`（每个失败行的 `row_no`、`order_id`、`error`）。
- `GET /orders`：条件检索。租户通过请求头 `X-Tenant` 传入，结果严格限定在该租户内。查询参数均可选、可任意组合：`order_id_prefix`（订单标识前缀）、`status`（`accepted`/`settled`）、`min_paid_cents`（已收金额下限）、`page_size`（每页条数，默认 50，上限 200，超过按上限截断）、`cursor`（下一页游标）。按订单标识升序返回 `{orders, next_cursor}`；`next_cursor` 为 null 表示没有下一页。游标只在同租户、同过滤条件下有效，非法、跨租户、换过滤条件、过期的游标分别返回 400 与可区分的原因：`cursor_malformed`、`cursor_tenant_mismatch`、`cursor_filter_mismatch`、`cursor_expired`。
- `GET /orders/{order_id}`：按标识读取订单。租户通过请求头 `X-Tenant` 传入；不存在返回 404；跨租户读取返回 404（不泄漏对象是否存在）。
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。
- `POST /orders/{order_id}/refunds`：按笔登记退款。租户通过请求头 `X-Tenant` 传入；请求字段 `refund_id`（调用方提供，同一租户内唯一）、`amount_cents`。成功返回 200 与订单的 `paid_cents`、`refunded_cents`、`outstanding_cents`；同一退款标识重复提交返回与首次一致的结果（不重复扣减）；不同订单复用同一退款标识返回 409；退款超过当前可退净额（已收 − 已退）返回 409；订单不存在或跨租户返回 404（不泄漏对象是否存在）；缺少租户头/参数不合法返回 400。
- `POST /orders/{order_id}/settlements`：对订单发起结算。租户通过请求头 `X-Tenant` 传入；请求字段 `settlement_id`（调用方提供，同一租户内唯一）、`amount_cents`（须等于订单应收金额）。成功返回 200 与结算记录（结算金额、结算时点的已收/已退、未收为 0、状态 `settled`），订单进入已结算状态；订单未收金额大于 0 返回 409 且不产生任何账务或状态变化；同一结算标识对同一订单、同一金额重复提交返回与首次一致的结果；同一标识用于不同订单或不同金额返回 409；订单已有生效结算时换新标识结算返回 409；订单不存在或跨租户返回 404。
- `POST /orders/{order_id}/reversals`：冲正订单当前生效的结算。租户通过请求头 `X-Tenant` 传入；请求字段 `reversal_id`（调用方提供，同一租户内唯一，与结算标识互不影响）、`reason`（原因文本，留存可查）。成功返回 200，对应结算记录作废、订单退回未结算状态；同一冲正标识重复提交返回与首次一致的结果；对同一结算重复冲正（含无生效结算）返回 409；订单不存在或跨租户返回 404。冲正后可用新结算标识重新结算，同一订单任一时刻至多一条生效结算记录，历史记录保留可查。
- `GET /orders/{order_id}/ledger`：订单账务历史。租户通过请求头 `X-Tenant` 传入；按（时间, 业务标识）升序返回该订单的收款、退款、结算、冲正条目，每条含 `entry_id`（业务标识，收款为服务生成的 `pm-<序号>`）、`type`、`amount_cents`、`outstanding_after`（操作后未收金额）与 `created_at`；跨租户或不存在返回 404。重放该序列可得到与当前一致的最终未收金额与状态。
- `POST /reconciliations`：按租户发起对账。请求字段 `tenant`、`reconciliation_id`（调用方提供，同一租户内唯一）。成功返回 201 与该租户全部订单汇总：`order_count`、`amount_cents`（应收合计）、`paid_cents`（已收合计）、`refunded_cents`（已退合计）、`outstanding_cents`（未收合计）及逐单快照（每张订单金额与 `has_active_settlement`，与订单读取接口一致）。守恒：应收 = 已收 − 已退 + 未收（未收沿用现有定义，含退款回冲部分）。同一对账标识重复发起返回与首次一致的汇总，不重复计算；对账期间新发生的账务不改变已生成结果，换新标识重新发起才反映。
- `GET /reconciliations/{reconciliation_id}`：查询对账汇总。租户通过请求头 `X-Tenant` 传入；不存在或跨租户返回 404（不泄漏对象是否存在）。
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

### 结算、冲正与账务历史调用示例

```bash
# 收齐后发起结算（结算标识由调用方生成，金额为订单应收金额）
curl -s -X POST localhost:8000/orders/demo-1/settlements \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"settlement_id": "st-20261002-0001", "amount_cents": 1200}'
# => {"settlement_id":"st-20261002-0001","status":"settled","paid_cents":1200,"outstanding_cents":0,...}
# 未收金额大于 0 时返回 409，账务与状态均不变化；同标识同订单同金额重复提交返回首次结果

# 冲正：作废当前生效结算，订单退回未结算状态，原因文本留存
curl -s -X POST localhost:8000/orders/demo-1/reversals \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"reversal_id": "rv-20261002-0001", "reason": "结算金额有误"}'
# => {"reversal_id":"rv-20261002-0001","settlement_id":"st-20261002-0001","status":"accepted",...}
# 冲正后可用新的结算标识重新结算

# 账务历史：收款/退款/结算/冲正按时间与业务标识升序，可重放核对未收金额与状态
curl -s localhost:8000/orders/demo-1/ledger -H 'X-Tenant: t1'
# => {"entries":[{"entry_id":"pm-1","type":"payment","amount_cents":1200,"outstanding_after":0},...]}
```

### 对账核销调用示例

```bash
# 发起对账（对账标识由调用方生成），返回该租户全部订单汇总与逐单快照
curl -s -X POST localhost:8000/reconciliations -H 'Content-Type: application/json' \
  -d '{"tenant": "t1", "reconciliation_id": "rc-20261002-01"}'
# => {"order_count":2,"amount_cents":2000,"paid_cents":1200,"refunded_cents":0,
#     "outstanding_cents":800,"orders":[{"order_id":"demo-1","has_active_settlement":true,...},...]}
# 同一标识重复发起返回与首次一致的汇总；之后新发生的账务不影响该结果

# 查询已生成的对账汇总
curl -s localhost:8000/reconciliations/rc-20261002-01 -H 'X-Tenant: t1'
```

### 批量导入调用示例

```bash
# 提交批次（异步受理，立即返回回执）
curl -s -X POST localhost:8000/orders/import -H 'Content-Type: application/json' \
  -d '{"tenant": "t1", "batch_id": "batch-20261002-01", "rows": [
        {"order_id": "demo-1", "amount_cents": 1200, "currency": "CNY"},
        {"order_id": "demo-2", "amount_cents": 800,  "currency": "CNY"}
      ]}'
# => {"tenant":"t1","batch_id":"batch-20261002-01","status":"accepted","total_rows":2}
# 同一批次标识同一内容重复提交：返回与首次完全一致的回执，不重复受理

# 查询进度与逐行结果
curl -s localhost:8000/orders/import/batch-20261002-01 -H 'X-Tenant: t1'
# => {"status":"completed","total_rows":2,"success_count":2,"failure_count":0,"failures":[],...}
```

### 条件检索调用示例

```bash
# 组合过滤：标识前缀 + 状态 + 已收金额下限，按订单标识升序返回一页
curl -s 'localhost:8000/orders?order_id_prefix=demo-&status=accepted&min_paid_cents=100&page_size=50' \
  -H 'X-Tenant: t1'
# => {"tenant":"t1","orders":[...],"next_cursor":"eyJ..."}  —— next_cursor 为 null 表示没有下一页

# 翻页：带上上一页返回的游标，过滤条件须保持不变
curl -s 'localhost:8000/orders?order_id_prefix=demo-&status=accepted&min_paid_cents=100&page_size=50&cursor=eyJ...' \
  -H 'X-Tenant: t1'
```

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）、`APP_CURSOR_SECRET`（检索游标签名密钥）、`APP_CURSOR_TTL`（游标有效期秒数，默认 3600）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入为单进程内异步执行，重启后按批次标识续跑。
- 收款支持按未收金额多笔登记与按笔退款，未实现分期计划。
