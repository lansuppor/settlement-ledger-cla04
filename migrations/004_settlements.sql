-- 结算单：一笔已收满的订单可生成一张结算单进行对账核销。
--   * settlement_id 由服务端分配（单据标识），全局响应返回；
--   * settlement_key 由调用方在按订单发起结算时指定（结算标识），租户内唯一，
--     同一结算标识 + 同一订单为幂等重放，指向另一订单即冲突；
--   * amount_cents 为核销时的金额快照：核销时须同时等于该订单“未被冲正的收款合计”
--     与订单金额，任一不闭合则结算不生效；
--   * status: active（核销中）| revoked（已撤销）。撤销只解除核销状态，
--     不改变订单收款与已收金额，订单可再次发起结算。
CREATE TABLE IF NOT EXISTS settlements(
  tenant TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  settlement_key TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  revoked_at TEXT,
  PRIMARY KEY(tenant, settlement_id)
);

-- 结算标识租户内唯一：同一标识只能属于一个订单，换订单点名即冲突
CREATE UNIQUE INDEX IF NOT EXISTS idx_settlements_key
  ON settlements(tenant, settlement_key);

-- 同一订单至多存在一张未撤销（active）的结算单；撤销后该订单可再次结算
CREATE UNIQUE INDEX IF NOT EXISTS idx_settlements_active_order
  ON settlements(tenant, order_id) WHERE status = 'active';

-- 撤销记录：撤销标识由调用方指定，租户内唯一；
-- 同一撤销标识 + 同一结算单为幂等重放，指向另一结算单即冲突。
-- 一张结算单至多被撤销一次（见下方 UNIQUE 索引）。
CREATE TABLE IF NOT EXISTS settlement_revocations(
  tenant TEXT NOT NULL,
  revocation_id TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY(tenant, revocation_id)
);

-- 一张结算单只能被撤销一次；该索引同时作为“结算单已撤销”的判定依据
CREATE UNIQUE INDEX IF NOT EXISTS idx_settlement_revocations_settlement
  ON settlement_revocations(tenant, settlement_id);
