# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款、按笔登记退款、收款核销（逐笔核销到指定欠款条目）、批量导入订单、条件检索、订单结算与冲正、订单账务历史与按租户对账核销，并核对未收金额；同时支持针对订单各环节问题的工单登记、处理与检索，形成从受理到处理完成的闭环；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/{order_id}/payments`：登记收款。请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200 与订单的 `paid_cents`、`outstanding_cents`。每笔收款在订单内按登记顺序编号，业务标识为 `pay-<订单内收款序号>`（可在账务历史中查到）。
- `POST /orders/{order_id}/payments/reversals`：收款撤销（账务冲回，不是退款）。租户通过请求头 `X-Tenant` 传入；请求字段 `reversal_id`（撤销标识，调用方提供，同一租户内唯一）、`payment_ref`（被撤销收款的业务标识，如 `pay-1`）、`amount_cents`（正整数）。成功返回 200 与撤销结果：`reversal_id`、`payment_ref`、`amount_cents`、`payment_net_cents`（撤销后该笔收款的净额 = 原收款金额 − 累计已撤销金额）、订单的 `paid_cents`、`refunded_cents`、`outstanding_cents` 与 `status`。已收随撤销等额减少、未收等额增加，已退不变；撤销后未收大于 0 时订单状态回到待收。同一撤销标识对同一收款、同一金额重复提交返回与首次一致的结果（不重复冲回）；同标识换收款业务标识或换金额返回 409。同一笔收款可被多笔不同撤销标识的部分撤销分次冲回，累计撤销不得超过该笔收款净额，超出整笔拒绝（409）且不产生任何记录。被撤销收款不存在、订单不存在或跨租户返回 404（不泄漏对象是否存在）；缺少租户头/参数不合法返回 400。订单存在生效结算时不可撤销（409，须先冲正结算）。撤销按欠款编号逆序收窄该笔收款占用的核销金额，不删除原收款流水，而在账务历史追加一条 `payment_reversal` 条目（含撤销标识、撤销金额与操作后未收金额）。撤销后可正常再收款；退款仍以撤销后的已收净额为上限。
- `POST /orders/{order_id}/refunds`：按笔登记退款。租户通过请求头 `X-Tenant` 传入；请求字段 `refund_id`（调用方提供，同一租户内唯一）、`amount_cents`。成功返回 200 与订单的 `paid_cents`、`refunded_cents`、`outstanding_cents`；同一退款标识重复提交返回与首次一致的结果（不重复扣减）；不同订单复用同一退款标识返回 409；退款超过当前可退净额（已收 − 已退）返回 409；订单存在生效结算时返回 409（须先冲正结算）；订单不存在或跨租户返回 404（不泄漏对象是否存在）；缺少租户头/参数不合法返回 400。
- `POST /orders/{order_id}/writeoffs`：收款核销，把一笔已到账金额逐笔核销到指定欠款条目。租户通过请求头 `X-Tenant` 传入；请求字段 `writeoff_id`（核销标识，调用方提供，同一租户内唯一）、`debt_no`（订单内欠款编号，从 1 开始）、`amount_cents`（正整数）。成功返回 201 与核销结果：`writeoff_id`、`order_id`、`debt_no`、`amount_cents`、目标条目核销后的 `settled_cents`（已核销金额）、`remaining_cents`（该条目未核销余额）、`remaining_total_cents`（该订单核销后仍未核销的欠款合计，恒等于订单未收金额）与条目 `status`（`unsettled`/`settled`）。同一核销标识对同一订单、同一欠款编号、同一金额重复提交返回与首次一致的结果，不重复核销；同标识换订单、换欠款编号或换金额返回 409。核销金额超过该条目当前未核销余额、或没有对应的到账金额可核销时整笔拒绝（409）且不产生任何记录；订单存在生效结算时拒绝核销（409，须先冲正结算）；订单或欠款编号不存在、跨租户返回 404（不泄漏对象是否存在）；缺少租户头/参数不合法返回 400。核销不改变订单的已收、已退、未收金额与订单状态：核销只是把已到账款项从收款时按编号自动占用的条目改配到指定条目。
- `GET /orders/{order_id}/debts`：按订单查询欠款条目。租户通过请求头 `X-Tenant` 传入；按欠款编号升序返回每条的 `debt_no`、`amount_cents`（欠款金额）、`settled_cents`（已核销金额）、`remaining_cents`（未核销余额）与 `status`。订单受理时生成第 1 条（金额等于订单金额）；每笔退款成功后追加一条（金额等于退回金额），条目金额一经生成不再变化。恒有 欠款金额之和 = 订单金额 + 已退金额、未核销余额之和 = 订单未收金额。订单不存在或跨租户返回 404；缺少租户头返回 400。
- `POST /orders/{order_id}/settlements`：发起结算。租户通过请求头 `X-Tenant` 传入；请求字段 `settlement_id`（调用方提供，同一租户内唯一）、`amount_cents`（结算金额）。订单未收金额大于 0 返回 409，不产生任何账务或状态变化；成功返回 201 与结算记录（含结算金额、结算时已收 `paid_cents`、已退 `refunded_cents`、未收恒为 0、结算代数 `seq`、`status: "effective"`），订单进入已结算状态。同一结算标识对同一订单、同一金额重复提交返回与首次一致的结果；同一标识用于不同订单或不同金额返回 409；订单不存在或跨租户返回 404。
- `POST /orders/{order_id}/settlements/{settlement_id}/reversals`：冲正结算。请求字段 `reversal_id`（调用方提供，同一租户内唯一）、`reason`（原因文本，可空）。成功返回 201 与冲正记录，对应结算记录作废（`voided`），订单退回未结算状态；同一冲正标识重复提交返回与首次一致的结果；对同一结算重复冲正返回 409；结算不存在、不属于该订单或跨租户返回 404。冲正标识与结算标识是两个独立请求身份，可同名，互不去重。冲正后可凭新结算标识重新结算；同一订单任一时刻至多一条生效结算记录，历史记录保留可查。
- `GET /orders/{order_id}/ledger`：订单账务历史。按时间与业务标识升序返回收款、收款撤销、退款、结算、冲正、核销条目，每条含 `biz_ref`（业务标识；收款无外部标识时为 `pay-<订单内收款序号>`、撤销为撤销标识、核销为核销标识）、`type`、`amount_cents`、`outstanding_cents`（操作后未收金额；核销条目中为核销后仍未核销的欠款合计）与时间。跨租户或订单不存在返回 404。按该序列逐笔重放即得到与订单读取接口一致的最终未收金额与状态。
- `POST /reconciliations`：按租户发起对账。请求字段 `tenant`、`reconciliation_id`（调用方提供，同一租户内唯一）。在单个事务内取该租户全部订单的一致性快照并落库，返回 201 与汇总：`order_count`、`total_receivable_cents`（应收合计）、`total_paid_cents`、`total_refunded_cents`、`total_outstanding_cents` 与逐订单行（含当前是否存在生效结算 `has_active_settlement`）。恒有 应收 = 已收 + 未收（未收沿用订单口径，含退款回冲），逐订单金额与 `GET /orders/{order_id}` 一致。同一对账标识重复发起返回与首次一致的汇总，不重复计算；对账期间新发生的账务不改变已生成结果，须以新标识重新发起才反映。
- `GET /reconciliations/{reconciliation_id}`：查询对账汇总。租户通过请求头 `X-Tenant` 传入；不存在或跨租户返回 404。
- `POST /tickets`：登记工单。请求字段 `tenant`、`ticket_id`（调用方提供，同一租户内唯一）、`order_id`、`ticket_type`（`accept`/`payment`/`refund`/`settlement`/`reversal`，对应受理、收款、退款、结算、冲正五类环节）、`description`（问题描述，不能为空）。成功返回 201 与工单对象（含工单标识、订单标识、工单类型、处理状态 `pending`、创建时间）；参数不合法返回 400；订单不存在或跨租户返回 404（不泄漏对象是否存在）。同一工单标识对同一订单、同一工单类型重复登记返回与首次一致的结果，不重复受理；同标识换订单或换工单类型返回 409。
- `POST /tickets/{ticket_id}/process`：处理工单。租户通过请求头 `X-Tenant` 传入；请求字段 `status`（目标处理状态）、`note`（处理备注，可空）。状态只在 `pending`（待处理）、`processing`（处理中）、`resolved`（已解决）、`rejected`（已驳回）之间按序推进：待处理可转处理中或直接转已解决/已驳回；处理中可转已解决/已驳回；已解决与已驳回是终态，不再变化。同一工单重复提交相同目标状态且备注一致返回与首次一致的结果，不重复处理；目标状态与当前状态相同但处理备注不同返回 409；跨状态跳跃或终态再变更返回 409，工单状态与备注不变。处理工单不改动订单的金额、状态与账务，也不影响收款、退款、结算、冲正、导入、检索与对账的任何结果。终态工单留存最后一次处理备注与处理时间。工单不存在或跨租户返回 404。
- `GET /tickets`：工单检索。租户通过请求头 `X-Tenant` 传入，结果严格限定在该租户内（跨租户按不存在处理）。查询参数均可选、可任意组合：`order_id`（订单标识）、`status`（处理状态）、`page_size`（每页条数，默认 50，上限 200，超过按上限截断）、`cursor`（下一页游标）。按订单标识升序返回 `{tickets, next_cursor}`；`next_cursor` 为 null 表示没有下一页。游标规则与订单检索一致：只在同租户、同过滤条件下有效，非法、跨租户、换过滤条件、过期的游标分别返回 400 与可区分的原因。
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

### 收款撤销调用示例

```bash
# 订单金额 800，分两笔收款：pay-1 收 600，pay-2 收 200
curl -s -X POST localhost:8000/orders/demo-1/payments \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' -d '{"amount_cents": 600}'
curl -s -X POST localhost:8000/orders/demo-1/payments \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' -d '{"amount_cents": 200}'
# 录单金额错误：撤销 pay-1 中的 200（撤销标识由调用方生成，同一租户内唯一）
curl -s -X POST localhost:8000/orders/demo-1/payments/reversals \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"reversal_id": "rv-pay-20261003-0001", "payment_ref": "pay-1", "amount_cents": 200}'
# => 200 {"reversal_id":"rv-pay-20261003-0001","payment_ref":"pay-1","amount_cents":200,
#         "payment_net_cents":400,"paid_cents":600,"refunded_cents":0,
#         "outstanding_cents":200,"status":"accepted",...}
# 已收 800 -> 600、未收 0 -> 200、已退不变；pay-1 净额 600 -> 400。
# 同一撤销标识对同收款同金额重复提交：原样返回首次结果，不重复冲回；
# 换收款标识/换金额返回 409；累计撤销超过该笔收款净额整笔拒绝（409）不留记录。
# 撤销不是退款：不新增欠款条目、不改变已退金额，只做账务冲回与欠款核销逆序收窄。
```

撤销后该笔收款仍可被不同撤销标识继续部分冲回（累计不超过净额），订单未收大于 0 即回到待收，可按未收金额重新收款；订单存在生效结算时撤销被拒（409），须先冲正结算；撤销后的再退款仍以撤销后的已收净额（已收 − 已退）为上限。

### 收款核销调用示例

```bash
# 订单金额 800，先收 800（收款按欠款编号顺序自动占用第 1 条）
curl -s -X POST localhost:8000/orders/demo-1/payments \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' -d '{"amount_cents": 800}'
# 退款 600：生成第 2 条欠款（金额 600，未核销），第 1 条金额仍为 800
curl -s -X POST localhost:8000/orders/demo-1/refunds \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"refund_id": "rf-20261002-0001", "amount_cents": 600}'
# 补收 300：自动占用第 2 条的一半（第 1 条 800/800，第 2 条 300/600）
curl -s -X POST localhost:8000/orders/demo-1/payments \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' -d '{"amount_cents": 300}'

# 查询欠款条目：每条含编号、欠款金额、已核销金额、未核销余额与状态
curl -s localhost:8000/orders/demo-1/debts -H 'X-Tenant: t1'
# => {"tenant":"t1","order_id":"demo-1","debts":[
#       {"debt_no":1,"amount_cents":800,"settled_cents":800,"remaining_cents":0,"status":"settled"},
#       {"debt_no":2,"amount_cents":600,"settled_cents":300,"remaining_cents":300,"status":"unsettled"}]}

# 把一笔到账金额 300 核销到第 2 条（核销标识由调用方生成，同一租户内唯一）
curl -s -X POST localhost:8000/orders/demo-1/writeoffs \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"writeoff_id": "wo-20261003-0001", "debt_no": 2, "amount_cents": 300}'
# => 201 {"writeoff_id":"wo-20261003-0001","order_id":"demo-1","debt_no":2,"amount_cents":300,
#         "settled_cents":600,"remaining_cents":0,"remaining_total_cents":300,"status":"settled",...}
# 第 2 条核销满；300 从第 1 条自动占用的空间改配而来（第 1 条变为 500/800）。
# 订单已收 1100、已退 600、未收 300 与订单状态均不变；Σ未核销余额 300 恒等于订单未收。
# 同一核销标识对同订单、同欠款编号、同金额重复提交：原样返回首次结果，不重复核销；
# 换订单/欠款编号/金额返回 409；同一欠款编号并发核销至多一笔成功。
```

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

### 工单调用示例

```bash
# 针对收款环节的问题登记工单（工单标识由调用方生成，同一租户内唯一）
curl -s -X POST localhost:8000/tickets -H 'Content-Type: application/json' \
  -d '{"tenant": "t1", "ticket_id": "tk-20261002-0001", "order_id": "demo-1",
       "ticket_type": "payment", "description": "客户称已付款但订单未到账"}'
# => 201 {"ticket_id":"tk-20261002-0001","order_id":"demo-1","ticket_type":"payment",
#         "status":"pending","note":null,"processed_at":null,"created_at":"...",...}
# 同一工单标识对同一订单、同一类型重复提交：原样返回首次结果，不重复受理

# 处理工单：待处理 -> 处理中 -> 已解决（也可由待处理直接转已解决/已驳回）
curl -s -X POST localhost:8000/tickets/tk-20261002-0001/process \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"status": "processing", "note": "已联系渠道核实"}'
curl -s -X POST localhost:8000/tickets/tk-20261002-0001/process \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"status": "resolved", "note": "渠道补单完成，已到账"}'
# 终态后不再变化；重复提交相同目标状态且备注一致返回首次结果；
# 跨状态跳跃或同状态换备注返回 409，工单状态与备注不变

# 检索工单：按订单标识与处理状态任意组合过滤，按订单标识升序分页
curl -s 'localhost:8000/tickets?order_id=demo-1&status=resolved&page_size=50' \
  -H 'X-Tenant: t1'
# => {"tenant":"t1","tickets":[...],"next_cursor":null}
```

工单只记录问题与处理过程：登记与处理均不改动订单金额、状态与账务，收款、退款、结算、冲正、批量导入、条件检索、账务历史与对账行为不受工单影响。

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
- 收款支持按未收金额多笔登记、按笔撤销（全部或部分账务冲回）、按笔退款、收款核销到指定欠款条目、结算与冲正、订单账务历史与按租户对账，未实现分期计划。
