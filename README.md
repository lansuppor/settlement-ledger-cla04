# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额、收款冲正与批量导入，以及按已收满订单发起结算单进行对账核销与撤销；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/{order_id}/payments`：登记收款。租户通过请求头 `X-Tenant` 传入；请求字段 `amount_cents`；超过未收金额返回 409；订单不存在/跨租户返回 404。成功返回 200，body 在订单的 `paid_cents`、`outstanding_cents` 之外附带本笔收款标识 `payment_id`（订单内唯一），供后续冲正点名。
- `POST /orders/{order_id}/reversals`：冲正一笔已登记的收款。租户通过请求头 `X-Tenant` 传入；请求字段 `reversal_id`（本次冲正标识，订单内唯一，由调用方指定，用于幂等）、`payment_id`（要冲正的收款标识，来自登记收款响应）。成功返回 200，body 含订单最新状态以及 `reversal_id`、`payment_id`、`reversed_amount_cents`（复制自原收款金额）。
  - 重复使用相同 `reversal_id` + `payment_id` 请求为幂等重放，不重复扣减已收金额，返回与首次一致；
  - 相同 `reversal_id` 指向另一笔收款返回 409（`reversal id already used for another payment`）；
  - 收款标识不存在返回 404（`payment not found`），含跨租户点名，不泄漏对象是否存在；
  - 收款已被冲正返回 409（`payment already reversed`）；订单不存在/跨租户返回 404（`order not found`）；
  - 冲正只影响被点名的那一笔收款，订单其他收款不变；冲正后订单回到 `accepted`，可继续登记收款，再次收满时状态回到 `settled`。
- `POST /payment-imports`：批量导入收款。租户通过请求头 `X-Tenant` 传入；一次提交一份待登记收款清单，服务端按记录给出的顺序逐笔受理、部分成功落库。
- `GET /payment-imports/{batch_id}`：按批次号查询本租户一批导入的逐行受理结果；批次不存在或属于其他租户返回 404（`import batch not found`）。
- `POST /orders/{order_id}/settlements`：按订单发起结算（对账核销）。租户通过请求头 `X-Tenant` 传入；请求字段 `settlement_id`（**结算标识**，租户内唯一，由调用方指定，用于幂等）。成功返回 201 与结算单对象；其中 `settlement_doc_id` 为**服务端分配的结算单单据标识**（撤销时点名使用），`settlement_id` 原样回显调用方指定的结算标识，`amount_cents` 为核销时的金额快照，`status` 为 `active`/`revoked`。
  - 未收满（账面已收不等于订单金额）返回 409（`order not fully paid`），订单与收款状态不变；
  - 同一订单已存在未撤销结算单返回 409（`active settlement already exists for order`），不重复生成；
  - 同一结算标识指向另一订单返回 409（`settlement id already used for another order`），既有状态不变；同一结算标识 + 同一订单重复请求为幂等重放，返回与首次一致；
  - 账面已收金额与未被冲正的收款合计不闭合返回 409（`reconciled payments do not match booked amount`），不留下半笔生效的核销；
  - 订单不存在/跨租户返回 404（`order not found`）。
  - 核销闭合期间（存在未撤销结算单）冲正收款返回 409（`payment reversal blocked by active settlement`）；请先撤销结算再冲正。
- `POST /settlements/{settlement_doc_id}/revocations`：撤销结算单。租户通过请求头 `X-Tenant` 传入；请求字段 `revocation_id`（**撤销标识**，租户内唯一，由调用方指定，用于幂等），路径中的 `settlement_doc_id` 为要撤销的结算单单据标识。成功返回 200，body 为结算单最新状态（`status` 变为 `revoked`，带 `revoked_at`）并回显 `revocation_id`。
  - 同一撤销标识 + 同一结算单重复请求为幂等重放，返回与首次一致；指向另一结算单返回 409（`revocation id already used for another settlement`）；
  - 撤销不存在的结算单（含跨租户点名）返回 404（`settlement not found`）；撤销已被撤销的结算单（新撤销标识）返回 409（`settlement already revoked`）；
  - 撤销只解除核销状态，不改变订单收款与已收金额；撤销后订单可再次发起结算。
- `GET /orders/{order_id}/settlements`：按订单检索结算单列表（核销时间线）。租户通过请求头 `X-Tenant` 传入；可选查询参数 `status` 过滤结算单状态：`active`（未撤销）、`revoked`（已撤销）、`all`（全部，缺省值），其他取值返回 400。过滤只影响返回范围，不改变任何单据状态与金额。列表按结算单生成时间从早到晚稳定排序，同一订单多次结算（首次核销、撤销后重新核销）按先后全部保留；每项含 `settlement_id`（结算标识）、`settlement_doc_id`（结算单单据标识）、`amount_cents`（核销金额快照，不随后续撤销/再结算改变）、`status`、`created_at`、`revoked_at`，已撤销项另含 `revocation_id`。订单不存在/跨租户查询返回 404（`order not found`），不泄漏对象是否存在；结果只含本租户数据。
- `GET /orders/{order_id}/reconciliation`：查询订单的对账核对结果。租户通过请求头 `X-Tenant` 传入。返回 `amount_cents`（订单金额）、`paid_cents`（账面已收金额）、`live_paid_cents`（该订单未被冲正的收款合计）、`outstanding_cents`（未收金额）与闭合结论 `closed`。闭合不变量在结果中直接体现：`paid_cents` 恒等于 `live_paid_cents`，`paid_cents + outstanding_cents` 恒等于 `amount_cents`；任一不成立时 `closed` 为 `false`，且 `discrepancies` 逐项给出差异所在（核对项、两侧金额与差额），而不是静默给出成功结论。订单不存在/跨租户查询返回 404（`order not found`）。
  - 两个查询均为只读：重复查询结果一致，查询后再次发起结算或撤销行为不变，既有结算单、收款与订单状态不受影响；结果持久化，服务重启后同一查询返回与重启前一致。
- `GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优（写事务经 `BEGIN IMMEDIATE` 串行化，登记/冲正在单事务内完成，失败整笔回滚）。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入为同步逐行受理，适合中小批量清单。
- 收款支持多笔登记与单笔冲正，并支持按订单发起结算核销与撤销；未实现分期计划。

## 调用示例

```bash
# 1. 登记一笔收款，响应中的 payment_id 需由调用方保存
curl -s -XPOST localhost:8000/orders/ord-1/payments \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"amount_cents": 200}'
# {"tenant":"t1","order_id":"ord-1","amount_cents":500,"paid_cents":200,
#  "currency":"CNY","status":"accepted","outstanding_cents":300,
#  "payment_id":"9f2c..."}

# 2. 用订单内唯一的冲正标识点名冲正该收款
curl -s -XPOST localhost:8000/orders/ord-1/reversals \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"reversal_id":"rev-20261001-01","payment_id":"9f2c..."}'
# {"...订单最新状态...","paid_cents":0,"outstanding_cents":500,"status":"accepted",
#  "reversal_id":"rev-20261001-01","payment_id":"9f2c...","reversed_amount_cents":200}
```

## 批量导入收款

`POST /payment-imports` 一次提交一批待登记收款，服务端按清单顺序逐行受理，某行被拒绝不影响其他行。

请求体：

- `batch_id`：调用方指定的批次号（非空字符串）。
- `lines[].line_seq`：行内序号，**本批内唯一**的整数。
- `lines[].order_id`：订单标识。
- `lines[].amount_cents`：收款金额，正整数。

**批次标识**由请求头租户、`batch_id`、`line_seq` 三者共同构成（`X-Tenant` + 批次号 + 行内序号），用于重复提交判定与后续查询。

响应（HTTP 始终 200，逐行业务成败在 `results` 中表达）：

- 汇总：`total`（受理总行数）、`accepted`（成功数）、`rejected`（拒绝数）。
- `results[]` 按提交顺序给出：
  - 公共字段：`line_no`（提交位置，从 1 起）、`line_seq`、`order_id`、`amount_cents`、`result`（`accepted` / `rejected`）。
  - 成功行另含：`payment_id`（本笔收款标识，订单内唯一，可用于既有冲正接口）、`order_status`（受理后订单最新状态 `accepted`/`settled`）、`paid_cents`、`outstanding_cents`。
  - 失败行另含 `reject_reason`：
    - `order_not_found`：订单不存在或跨租户点名（统一按不存在处理，不泄漏对象是否存在）；
    - `invalid_amount`：金额非法（非正整数，如 0、负数、小数、非整数类型）；
    - `exceeds_outstanding`：超过订单未收金额；
    - `duplicate_order_in_batch`：同一批次内该订单标识已被更早的行使用，后一条记为重复被拒，不覆盖前一条；
    - `duplicate_line_seq`：同一次提交内出现重复的 `line_seq`；
    - `identifier_conflict`：同一批次标识（租户 + `batch_id` + `line_seq`）再次提交但订单或金额与首次不同；既有状态保持不变。
- 业务拒绝与内部错误严格区分：拒绝是行结果的一部分；若发生内部错误，出错行所在事务整体回滚（不存在半行生效），接口返回 5xx，此前已提交的行保持生效，用同一标识续跑即可。

幂等与续跑：

- 同一份清单用相同租户请求头与 `batch_id` 整体重放，已成功的行不重复登记、不重复扣减，响应与首次一致（含同一 `payment_id` 与受理时订单快照）。
- 中断后用同一标识续跑：可以重发完整清单，也可以只补发送未处理的行；已落库的行原样重放，未落库的行继续受理，最终结果与一次连续跑完一致。
- 每个批次内同一订单至多受理一笔收款（后一条按 `duplicate_order_in_batch` 拒绝）；同一订单的多笔收款请分批次提交或使用单笔登记接口。

```bash
curl -s -XPOST localhost:8000/payment-imports \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"batch_id":"imp-20261001-01","lines":[
        {"line_seq":1,"order_id":"ord-1","amount_cents":200},
        {"line_seq":2,"order_id":"ord-2","amount_cents":999}]}'
# {"tenant":"t1","batch_id":"imp-20261001-01","total":2,"accepted":1,"rejected":1,
#  "results":[
#    {"line_no":1,"line_seq":1,"order_id":"ord-1","amount_cents":200,"result":"accepted",
#     "payment_id":"a1..","order_status":"accepted","paid_cents":200,"outstanding_cents":300},
#    {"line_no":2,"line_seq":2,"order_id":"ord-2","amount_cents":999,"result":"rejected",
#     "reject_reason":"exceeds_outstanding"}]}

# 按批次号查询逐行结果（中断后可用它核对已受理的行）
curl -s localhost:8000/payment-imports/imp-20261001-01 -H 'X-Tenant: t1'
```

## 结算与撤销

订单收满后，调用方用自管的**结算标识**发起结算；服务端返回**结算单单据标识**，撤销时点名它。

```bash
# 订单 ord-1 已收满（两笔 200 + 300 = 500），发起结算核销
curl -s -XPOST localhost:8000/orders/ord-1/settlements \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"settlement_id":"set-20261001-01"}'
# 201 {"tenant":"t1","settlement_doc_id":"3f9a...","settlement_id":"set-20261001-01",
#      "order_id":"ord-1","amount_cents":500,"status":"active","created_at":"...","revoked_at":null}

# 幂等重放：同一结算标识 + 同一订单，返回与首次完全一致，不重复生成/核销
curl -s -XPOST localhost:8000/orders/ord-1/settlements \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"settlement_id":"set-20261001-01"}'

# 用撤销标识点名撤销该结算单（只解除核销，不动收款）
curl -s -XPOST localhost:8000/settlements/3f9a.../revocations \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"revocation_id":"cancel-20261001-01"}'
# 200 {"settlement_doc_id":"3f9a...","settlement_id":"set-20261001-01",
#      "status":"revoked","revoked_at":"...","revocation_id":"cancel-20261001-01", ...}

# 撤销后可冲正收款、重新收满，再用新的结算标识重新核销（生成新的结算单单据标识）
```

字段含义：

- `settlement_id`（结算标识）：调用方指定、租户内唯一的本次结算标识，承担幂等键作用；同一标识只能指向同一订单。
- `settlement_doc_id`（结算单单据标识）：服务端分配的结算单主键，撤销接口路径点名使用；重新结算会得到新的单据标识。
- `amount_cents`：核销时的金额快照，恒等于核销时未被冲正的收款合计与订单金额，撤销后也保留，便于事后对账。
- `revocation_id`（撤销标识）：调用方指定、租户内唯一的本次撤销标识，承担幂等键作用；同一标识只能指向同一结算单。

## 结算单检索与对账核对

两个只读查询入口，供调用方核对本租户订单的核销历史与对账依据；均通过 `X-Tenant` 请求头指定租户，跨租户查询与订单不存在返回相同的 404 结论。

```bash
# 1. 按订单检索结算单列表（核销时间线），可选 status=active|revoked|all（缺省 all）
curl -s 'localhost:8000/orders/ord-1/settlements?status=all' -H 'X-Tenant: t1'
# {"tenant":"t1","order_id":"ord-1","status_filter":"all","settlements":[
#   {"settlement_doc_id":"3f9a...","settlement_id":"set-20261001-01","order_id":"ord-1",
#    "tenant":"t1","amount_cents":500,"status":"revoked","created_at":"...",
#    "revoked_at":"...","revocation_id":"cancel-20261001-01"},
#   {"settlement_doc_id":"8c2e...","settlement_id":"set-20261001-02","order_id":"ord-1",
#    "tenant":"t1","amount_cents":500,"status":"active","created_at":"...","revoked_at":null}]}

# 2. 查询对账核对结果
curl -s localhost:8000/orders/ord-1/reconciliation -H 'X-Tenant: t1'
# 闭合：{"order_id":"ord-1","amount_cents":500,"paid_cents":500,"live_paid_cents":500,
#        "outstanding_cents":0,"closed":true,"discrepancies":[], ...}
# 未闭合（账面与明细脱节）：closed=false，discrepancies 给出差异，例如
# {"check":"paid_equals_live_payments","paid_cents":400,"live_paid_cents":500,
#  "difference_cents":-100}
```

- 列表按结算单生成时间从早到晚稳定排序；撤销后重新核销形成的多张结算单全部保留，旧单金额快照不变，可从结果还原核销时间线。
- 对账核对的可观察结论：闭合时 `closed=true` 且 `discrepancies` 为空，`paid_cents == live_paid_cents` 且 `paid_cents + outstanding_cents == amount_cents`；未闭合时 `closed=false`，`discrepancies` 逐项标明核对项与差额。
- 查询为只读：重复查询、查询后再发起结算或撤销，都不改变既有结算单、收款与订单状态；重启后同一查询返回一致结果。

## 金额闭合与并发语义

- 已收金额 `paid_cents` 恒等于该订单**未被冲正**的收款合计；已收 + 未收恒等于订单金额，两值均不为负。
- 收款与冲正落库后即持久化，服务重启后按原结果重放查询；冲正记录保留原收款金额、冲正标识与被冲正收款标识。
- 结算单的核销金额恒等于未被冲正的收款合计与订单金额（三者闭合才生效）；任一时刻同一订单至多一张未撤销结算单。发起结算、撤销结算与登记收款/冲正同一订单时，单次操作在单事务（`BEGIN IMMEDIATE`）内串行完成、失败整笔回滚，不允许半笔生效的结算或撤销。结算与撤销落库后持久化，重启后按原结果重放。
