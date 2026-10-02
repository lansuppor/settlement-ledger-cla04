# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款、按笔登记退款、批量导入订单、条件检索、订单结算与冲正、订单账务历史与按租户对账核销，并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/{order_id}/refunds`：按笔登记退款。租户通过请求头 `X-Tenant` 传入；请求字段 `refund_id`（调用方提供，同一租户内唯一）、`amount_cents`。成功返回 200 与订单的 `paid_cents`、`refunded_cents`、`outstanding_cents`；同一退款标识重复提交返回与首次一致的结果（不重复扣减）；不同订单复用同一退款标识返回 409；退款超过当前可退净额（已收 − 已退）返回 409；订单存在生效结算时返回 409（须先冲正结算）；订单不存在或跨租户返回 404（不泄漏对象是否存在）；缺少租户头/参数不合法返回 400。
- `POST /orders/{order_id}/settlements`：发起结算。租户通过请求头 `X-Tenant` 传入；请求字段 `settlement_id`（调用方提供，同一租户内唯一）、`amount_cents`（结算金额）。订单未收金额大于 0 返回 409，不产生任何账务或状态变化；成功返回 201 与结算记录（含结算金额、结算时已收 `paid_cents`、已退 `refunded_cents`、未收恒为 0、结算代数 `seq`、`status: "effective"`），订单进入已结算状态。同一结算标识对同一订单、同一金额重复提交返回与首次一致的结果；同一标识用于不同订单或不同金额返回 409；订单不存在或跨租户返回 404。
- `POST /orders/{order_id}/settlements/{settlement_id}/reversals`：冲正结算。请求字段 `reversal_id`（调用方提供，同一租户内唯一）、`reason`（原因文本，可空）。成功返回 201 与冲正记录，对应结算记录作废（`voided`），订单退回未结算状态；同一冲正标识重复提交返回与首次一致的结果；对同一结算重复冲正返回 409；结算不存在、不属于该订单或跨租户返回 404。冲正标识与结算标识是两个独立请求身份，可同名，互不去重。冲正后可凭新结算标识重新结算；同一订单任一时刻至多一条生效结算记录，历史记录保留可查。
- `GET /orders/{order_id}/ledger`：订单账务历史。按时间与业务标识升序返回收款、退款、结算、冲正条目，每条含 `biz_ref`（业务标识；收款无外部标识时为 `pay-<订单内收款序号>`）、`type`、`amount_cents`、`outstanding_cents`（操作后未收金额）与时间。跨租户或订单不存在返回 404。按该序列逐笔重放即得到与订单读取接口一致的最终未收金额与状态。
- `POST /reconciliations`：按租户发起对账。请求字段 `tenant`、`reconciliation_id`（调用方提供，同一租户内唯一）。在单个事务内取该租户全部订单的一致性快照并落库，返回 201 与汇总：`order_count`、`total_receivable_cents`（应收合计）、`total_paid_cents`、`total_refunded_cents`、`total_outstanding_cents` 与逐订单行（含当前是否存在生效结算 `has_active_settlement`）。恒有 应收 = 已收 + 未收（未收沿用订单口径，含退款回冲），逐订单金额与 `GET /orders/{order_id}` 一致。同一对账标识重复发起返回与首次一致的汇总，不重复计算；对账期间新发生的账务不改变已生成结果，须以新标识重新发起才反映。
- `GET /reconciliations/{reconciliation_id}`：查询对账汇总。租户通过请求头 `X-Tenant` 传入；不存在或跨租户返回 404。
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

退款成功后退回的 200 重新计入未收金额，可再次收款；退款累计不得超过已收净额。订单存在生效结算期间不可退款，须先冲正结算。

### 结算、冲正与对账调用示例

```bash
# 订单全额收讫后发起结算（结算标识由调用方生成）
curl -s -X POST localhost:8000/orders/demo-1/settlements \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"settlement_id": "st-20261002-0001", "amount_cents": 1200}'
# => 201 {"settlement_id":"st-20261002-0001","amount_cents":1200,"paid_cents":1200,
#         "refunded_cents":0,"outstanding_cents":0,"seq":1,"status":"effective",...}
# 未收 > 0 时返回 409 且账面不变；同标识对同订单同金额重放返回首次结果。

# 查看订单账务历史（收款/退款/结算/冲正，按时间升序，可逐笔重放）
curl -s localhost:8000/orders/demo-1/ledger -H 'X-Tenant: t1'
# => {"tenant":"t1","order_id":"demo-1","entries":[{"biz_ref":"pay-1","type":"payment",...},...]}

# 冲正结算（冲正标识独立于结算标识），订单退回未结算，历史仍可查
curl -s -X POST localhost:8000/orders/demo-1/settlements/st-20261002-0001/reversals \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"reversal_id": "rv-20261002-0001", "reason": "客户争议"}'
# 冲正后可用新结算标识重新结算；同一订单任一时刻至多一条生效结算。

# 按租户发起对账（标识由调用方生成），结果为发起时刻快照
curl -s -X POST localhost:8000/reconciliations -H 'Content-Type: application/json' \
  -d '{"tenant": "t1", "reconciliation_id": "rec-20261002-0001"}'
# => 201 {"order_count":N,"total_receivable_cents":...,"total_paid_cents":...,
#         "total_refunded_cents":...,"total_outstanding_cents":...,"orders":[...]}
# 恒有 total_receivable_cents = total_paid_cents + total_outstanding_cents
# 查询：GET /reconciliations/rec-20261002-0001 -H 'X-Tenant: t1'
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
- 收款支持按未收金额多笔登记、按笔退款、结算与冲正、订单账务历史与按租户对账，未实现分期计划。
