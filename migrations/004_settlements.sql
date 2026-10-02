-- 结算、冲正与对账核销能力演进（在 003_imports 之上）：
-- 1) 结算记录。结算标识由调用方提供，同一租户内唯一；存结算时点的已收/已退快照，
--    重复提交直接返回与首次一致的结果。status: active | reversed；
--    同一订单任一时刻至多一条 active 记录，历史记录保留可查。
CREATE TABLE IF NOT EXISTS settlements(
  tenant        TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  order_id      TEXT NOT NULL,
  amount_cents  INTEGER NOT NULL,
  paid_cents     INTEGER NOT NULL,   -- 结算时点已收
  refunded_cents INTEGER NOT NULL,   -- 结算时点已退
  status        TEXT NOT NULL,       -- active | reversed
  created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
  PRIMARY KEY(tenant, settlement_id)
);
CREATE INDEX IF NOT EXISTS idx_settlements_order ON settlements(tenant, order_id, status);

-- 2) 冲正记录。冲正标识与结算标识是两个独立请求身份，各自在租户内唯一、互不去重。
--    留存原因文本与被作废的结算标识，重复提交返回与首次一致的结果。
CREATE TABLE IF NOT EXISTS reversals(
  tenant            TEXT NOT NULL,
  reversal_id       TEXT NOT NULL,
  settlement_id     TEXT NOT NULL,
  order_id          TEXT NOT NULL,
  reason            TEXT NOT NULL,
  amount_cents      INTEGER NOT NULL,  -- 被冲正结算的金额
  outstanding_after INTEGER NOT NULL,  -- 冲正后的未收金额
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
  PRIMARY KEY(tenant, reversal_id)
);

-- 3) 统一账务流水：收款、退款、结算、冲正各写一条，与状态变更同事务，
--    失败整体回滚不留半截。历史按（时间, 业务标识）升序可解释、可重放。
CREATE TABLE IF NOT EXISTS ledger_entries(
  seq               INTEGER PRIMARY KEY AUTOINCREMENT,
  tenant            TEXT NOT NULL,
  order_id          TEXT NOT NULL,
  entry_id          TEXT NOT NULL,     -- 业务标识：退款/结算/冲正标识，收款为生成的 pm-<序号>
  entry_type        TEXT NOT NULL,     -- payment | refund | settlement | reversal
  amount_cents      INTEGER NOT NULL,
  outstanding_after INTEGER NOT NULL,  -- 操作后未收金额
  created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_order ON ledger_entries(tenant, order_id);

-- 4) 对账批次。对账标识由调用方提供，同一租户内唯一；汇总在生成时落库，
--    之后新发生的账务不改变已生成结果，同标识重复发起返回已存结果、不重复计算。
CREATE TABLE IF NOT EXISTS reconciliations(
  tenant            TEXT NOT NULL,
  reconciliation_id TEXT NOT NULL,
  order_count       INTEGER NOT NULL,
  amount_cents      INTEGER NOT NULL,  -- 应收合计
  paid_cents        INTEGER NOT NULL,  -- 已收合计
  refunded_cents    INTEGER NOT NULL,  -- 已退合计
  outstanding_cents INTEGER NOT NULL,  -- 未收合计
  created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now')),
  PRIMARY KEY(tenant, reconciliation_id)
);

-- 5) 对账逐单快照：每张订单的金额与是否存在生效结算，逐张与订单读取接口一致。
CREATE TABLE IF NOT EXISTS reconciliation_orders(
  tenant                TEXT NOT NULL,
  reconciliation_id     TEXT NOT NULL,
  order_id              TEXT NOT NULL,
  amount_cents          INTEGER NOT NULL,
  paid_cents            INTEGER NOT NULL,
  refunded_cents        INTEGER NOT NULL,
  outstanding_cents     INTEGER NOT NULL,
  has_active_settlement INTEGER NOT NULL,  -- 0 | 1
  PRIMARY KEY(tenant, reconciliation_id, order_id)
);
