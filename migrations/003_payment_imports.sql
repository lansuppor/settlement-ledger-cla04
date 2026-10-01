-- 批量导入收款的逐行受理日记账。
-- 主键（租户, 批次号, 行内序号）即本次导入的标识：
--   * 每行在“收款落明细 + 订单余额更新”的同一事务内写入，行要么整体生效要么整体不生效；
--   * 已落库的行（无论受理还是拒绝）即中断点，重复提交时原样重放，不再重复登记/扣减；
--   * 成功行快照受理时订单最新状态，保证重放结果与首次一致；
--   * amount_json 保存提交金额的规范 JSON（可能为非法值），供原样重放与冲突比对。
CREATE TABLE IF NOT EXISTS payment_import_lines(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  line_seq INTEGER NOT NULL,
  order_id TEXT NOT NULL,
  amount_json TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  status TEXT NOT NULL,              -- accepted（成功受理）| rejected（拒绝）
  reject_reason TEXT,                -- order_not_found | invalid_amount | exceeds_outstanding
                                     -- | duplicate_order_in_batch | identifier_conflict
  payment_id TEXT,                   -- 成功行：本笔收款标识（订单内唯一）
  order_status TEXT,                 -- 成功行：受理后订单状态 accepted | settled
  paid_cents INTEGER,                -- 成功行：受理后订单已收金额
  outstanding_cents INTEGER,         -- 成功行：受理后订单未收金额
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY(tenant, batch_id, line_seq)
);
