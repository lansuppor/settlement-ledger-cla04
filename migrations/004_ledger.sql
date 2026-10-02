-- 结算、冲正、订单账务历史与对账核销能力演进（在 003_imports 之上）：
--
-- 1) 结算记录。结算标识由调用方提供，同一租户内唯一；同标识对同一订单、同一金额重复
--    提交直接返回首次结果，同标识用于不同订单/金额拒绝。seq 记录同一订单上的结算代数，
--    voided 标识是否已被冲正；任一时刻同一订单至多一条 voided=0 的生效记录（部分索引）。
--    快照结算时的已收、已退金额，使历史记录可解释、可重放。
--
-- 2) 冲正记录。冲正标识同样以（租户, 冲正标识）唯一，与结算标识是两个独立请求身份，
--    不互相复用或去重；reason 留存原因文本；同标识重复提交返回首次结果，对同一结算
--    重复冲正拒绝。
--
-- 3) 订单账务历史流水。收款/退款/结算/冲正四类业务条目统一入流水表，每条存业务标识、
--    类型、金额与操作后未收金额；按时间与业务标识升序即可重放并解释订单当前状态。
--
-- 4) 对账汇总。对账标识以（租户, 对账标识）唯一，同一标识重复发起返回首次生成的快照，
--    不重复计算；快照含租户全量订单汇总与逐订单行，后续账务不改变已生成结果。

CREATE TABLE IF NOT EXISTS settlements(
  tenant       TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  order_id     TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  paid_at_settlement     INTEGER NOT NULL,
  refunded_at_settlement INTEGER NOT NULL,
  outstanding_after      INTEGER NOT NULL,   -- 结算要求为 0，留存以便解释/重放
  currency     TEXT NOT NULL,
  status       TEXT NOT NULL,                -- effective | voided
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, settlement_id)
);

-- 任一时刻同一订单至多一条生效结算记录；冲正后可重新结算，历史行 voided=1 保留可查。
CREATE UNIQUE INDEX IF NOT EXISTS idx_settlements_one_active
  ON settlements(tenant, order_id) WHERE status='effective';

CREATE TABLE IF NOT EXISTS reversals(
  tenant        TEXT NOT NULL,
  reversal_id   TEXT NOT NULL,
  settlement_id TEXT NOT NULL,
  order_id      TEXT NOT NULL,
  reason        TEXT NOT NULL,
  amount_cents  INTEGER NOT NULL,            -- 对应结算记录的结算金额
  outstanding_after INTEGER NOT NULL,        -- 冲正后未收金额快照（重放返回首次结果）
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, reversal_id)
);

-- 同一结算至多被冲正一次：并发重复冲正在此撞唯一索引，整笔回滚。
CREATE UNIQUE INDEX IF NOT EXISTS idx_reversals_one_per_settlement
  ON reversals(tenant, settlement_id);

CREATE TABLE IF NOT EXISTS ledger_entries(
  tenant      TEXT NOT NULL,
  order_id    TEXT NOT NULL,
  seq_no      INTEGER NOT NULL,              -- 订单内严格递增的重放序号
  biz_ref     TEXT NOT NULL,                 -- 调用方提供的业务标识
  entry_type  TEXT NOT NULL,                 -- payment | refund | settlement | reversal
  amount_cents INTEGER NOT NULL,
  outstanding_after INTEGER NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, order_id, seq_no)
);

-- 历史按时间与业务标识升序列出；时间并列时以序号兜底，顺序与重放顺序一致。
CREATE INDEX IF NOT EXISTS idx_ledger_order
  ON ledger_entries(tenant, order_id, created_at, seq_no);

CREATE TABLE IF NOT EXISTS reconciliations(
  tenant            TEXT NOT NULL,
  reconciliation_id TEXT NOT NULL,
  order_count       INTEGER NOT NULL,
  total_receivable_cents   INTEGER NOT NULL,  -- 应收合计 = Σ(订单金额 + 已退)，恒等于 已收 + 未收
  total_paid_cents       INTEGER NOT NULL,
  total_refunded_cents   INTEGER NOT NULL,
  total_outstanding_cents INTEGER NOT NULL,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, reconciliation_id)
);

CREATE TABLE IF NOT EXISTS reconciliation_orders(
  tenant            TEXT NOT NULL,
  reconciliation_id TEXT NOT NULL,
  order_id          TEXT NOT NULL,
  amount_cents      INTEGER NOT NULL,
  paid_cents        INTEGER NOT NULL,
  refunded_cents    INTEGER NOT NULL,
  outstanding_cents INTEGER NOT NULL,
  currency          TEXT NOT NULL,
  status            TEXT NOT NULL,
  has_active_settlement INTEGER NOT NULL,    -- 0/1
  PRIMARY KEY(tenant, reconciliation_id, order_id)
);
