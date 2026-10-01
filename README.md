# 经营单据与结算服务

本地可运行的多租户经营单据服务。当前支持受理订单、按标识读取订单、登记收款、冲正收款并核对未收金额；数据落本地 SQLite 文件库，服务为单进程 HTTP 服务。

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
- `POST /orders/{order_id}/payments`：登记收款。租户通过请求头 `X-Tenant` 传入；请求字段 `amount_cents`；超过未收金额返回 409；成功返回 200，响应在订单的 `paid_cents`、`outstanding_cents` 之外额外返回 `payment_id`（该笔收款在本服务内的整数标识，后续冲正用它点名）。
- `POST /orders/{order_id}/reversals`：冲正一笔已登记的收款。租户通过请求头 `X-Tenant` 传入；请求字段 `reversal_id`（本次冲正标识，在同一订单内唯一，用于幂等重放）、`payment_id`（要冲正的收款标识，来自登记收款的响应）。成功返回 200，响应含 `reversal_id`、`payment_id`、`reversed_amount_cents`（被冲正收款的原金额）及冲正后的订单字段（`paid_cents`、`outstanding_cents`、`status` 等）。业务拒绝一律返回 409，并在 `detail.reason` 给出可区分的原因码：`reversal_payment_not_found`（收款不存在或不属于该订单/租户）、`reversal_payment_already_reversed`（该收款已被冲正）、`reversal_id_conflict`（同一 `reversal_id` 已用于冲正另一笔收款）；订单不存在或跨租户访问返回 404（不泄漏对象是否存在）。同一 `reversal_id` + `payment_id` 重复请求为幂等重放，不重复扣减，响应与首次一致。
- `GET /health`：返回服务与数据库状态。

### 冲正语义

- 已收金额 `paid_cents` 只合计未被冲正的收款；未收金额 `outstanding_cents = amount_cents − paid_cents`，二者之和始终等于订单金额。
- 冲正成功后订单由 `settled` 回到 `accepted`，可继续登记收款；再次收满时状态回到 `settled`。
- 一次冲正只影响被点名的那一笔收款，同订单其他收款不变。冲正记录保留原收款金额、冲正标识与被冲正收款标识，便于事后核对（落库于 `reversals` 表）。
- 收款与冲正均在单个数据库事务内串行提交：并发下已收金额不会超过订单金额、不会为负；任一请求失败不会留下半笔生效的收款或冲正；服务重启后已完成的收款与冲正仍可按原结果查询与重放。

## 数据与配置

- 数据库文件默认 `var/app.sqlite`（不入库）。
- 环境变量：`APP_DB`（数据库路径）、`APP_PORT`（监听端口）、`APP_TENANT_HEADER`（默认 `X-Tenant`）。

## 当前限制

- 单进程运行，单库写入，未做连接池与写并发调优。
- 租户通过请求头声明，未接入真实身份提供方。
- 无缓存层；批量导入只支持小样本同步方式。
- 收款支持整单/分次登记与冲正，未实现独立退款流程与自动对账。
