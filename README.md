# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /payment-imports`：批量导入收款。租户通过请求头 `X-Tenant` 传入；body 给出调用方指定的批次号 `batch_id` 与待登记收款清单 `lines`，每行含 `line_no`（本批内唯一的行内序号，正整数）、`order_id`、`amount_cents`。服务端按清单顺序逐行受理，固定返回 200 与整批汇总、逐条结果（业务拒绝落在逐行结果里，不是 HTTP 错误）。
  - 批次标识：`batch_id`（调用方指定）与每行 `line_no` 共同构成导入行标识，即 `(租户, batch_id, line_no)`，用于重复提交判定与后续查询。
  - 整批汇总字段：`total`（受理总行数）、`accepted_count`（成功数）、`rejected_count`（拒绝数）；`lines` 为按提交顺序排列的逐行结果。
  - 成功行：`status="accepted"`，含 `payment_id`（该笔收款标识，订单内唯一，可用于冲正点名）、`order_status`、`paid_cents`、`outstanding_cents`（登记后订单最新状态，规则与单笔收款一致：不得超过未收金额，收满时为 `settled`）。
  - 失败行：`status="rejected"`，含 `reject_reason` 与可读的 `reject_message`。可区分原因：`order_not_found`（订单不存在或跨租户，不泄漏对象是否存在）、`invalid_amount`（金额非法）、`exceeds_outstanding`（超过未收金额）、`duplicate_order`（同批次内后一条复用前一条的订单标识，后一条记为重复被拒、不覆盖前一条）、`line_no_taken`（同一清单内行内序号重复）、`identifier_conflict`（同一 `(batch_id, line_no)` 带不同金额/订单再次提交）。
  - 幂等与续跑：同一份清单整体重放、或同一行标识再次提交，已成功的行不重复登记、不重复扣减，返回与首次一致（`payment_id` 不变）；同一标识带不同金额按 `identifier_conflict` 拒绝且既有状态不变。服务中断后用同一 `batch_id` 重新提交即可续跑——已落库的行按原结果回放、未落库的行继续受理，最终结果与一次连续跑完一致。
  - 原子性：每一行在独立事务内“登记收款 + 落批次结果”原子完成，不存在半行生效；业务拒绝只拒绝该行、不影响其他行；非预期内部错误回滚当前行并返回 5xx（绝不伪装成逐行拒绝），已提交的行保留。
- `GET /payment-imports/{batch_id}`：按批次号查询一次导入的逐行受理结果。租户通过请求头 `X-Tenant` 传入；返回结构与提交响应一致；批次不存在或跨租户返回 404（不泄漏对象是否存在）。
- `GET /health`：返回服务与数据库状态。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优（写事务经 `BEGIN IMMEDIATE` 串行化，登记/冲正在单事务内完成，失败整笔回滚）。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入为同步逐行受理，适合中小批次（每行一次事务），未做异步分片与超大文件导入。
- 收款支持多笔登记与单笔冲正，未实现分期计划与自动对账。

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

# 3. 批量导入：一份清单逐行受理，部分成功也返回 200；用同一 batch_id 重放即可续跑/查结果
curl -s -XPOST localhost:8000/payment-imports \
  -H 'X-Tenant: t1' -H 'Content-Type: application/json' \
  -d '{"batch_id":"imp-20261001-01","lines":[
        {"line_no":1,"order_id":"ord-1","amount_cents":500},
        {"line_no":2,"order_id":"ord-2","amount_cents":999}]}'
# {"tenant":"t1","batch_id":"imp-20261001-01","total":2,
#  "accepted_count":1,"rejected_count":1,
#  "lines":[
#    {"batch_id":"imp-20261001-01","line_no":1,"order_id":"ord-1","status":"accepted",
#     "payment_id":"a1b2...","order_status":"settled","paid_cents":500,"outstanding_cents":0},
#    {"batch_id":"imp-20261001-01","line_no":2,"order_id":"ord-2","status":"rejected",
#     "reject_reason":"exceeds_outstanding",
#     "reject_message":"payment exceeds outstanding amount"}]}

# 4. 按批次号查询逐行受理结果
curl -s localhost:8000/payment-imports/imp-20261001-01 -H 'X-Tenant: t1'
```

## 金额闭合与并发语义

- 已收金额 `paid_cents` 恒等于该订单**未被冲正**的收款合计；已收 + 未收恒等于订单金额，两值均不为负。
- 收款与冲正落库后即持久化，服务重启后按原结果重放查询；冲正记录保留原收款金额、冲正标识与被冲正收款标识。
