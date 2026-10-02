-- 退款能力演进（在 001_init 之上）：
-- 1) 订单增加已退金额；老数据默认 0，此时 金额 = 已收 − 已退 + 未收 仍成立。
ALTER TABLE orders ADD COLUMN refunded_cents INTEGER NOT NULL DEFAULT 0;

-- 2) 每笔退款流水。同一租户内退款标识全局唯一（不同订单复用也拒绝）。
--    存首次登记结果快照，重复提交直接返回与首次一致的订单结果（可解释、可重放）。
CREATE TABLE IF NOT EXISTS refunds(
  tenant       TEXT NOT NULL,
  refund_id    TEXT NOT NULL,
  order_id     TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  currency     TEXT NOT NULL,
  paid_after        INTEGER NOT NULL,
  refunded_after    INTEGER NOT NULL,
  outstanding_after INTEGER NOT NULL,
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, refund_id)
);
