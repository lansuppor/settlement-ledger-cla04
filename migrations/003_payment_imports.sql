-- 批量导入的逐行受理结果：(批次号, 行内序号) 为本次导入标识，全局唯一。
-- 成功行持有生成的 payment_id（订单内唯一，可被冲正接口点名）；
-- 被拒绝行记录结构化拒绝原因，不生成收款。
-- 服务中断后可用同一批次号重放续跑：已落库的行按原结果返回，未落库的行继续受理。
CREATE TABLE IF NOT EXISTS payment_imports(
  tenant TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  line_no INTEGER NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER,              -- 金额非法被拒的行存 NULL；受理成功/其他拒绝行为提交值
  status TEXT NOT NULL,             -- accepted（成功受理）/ rejected（被拒绝）
  payment_id TEXT,                  -- status='accepted' 时非空
  reject_reason TEXT,               -- status='rejected' 时非空
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY(tenant, batch_id, line_no)
);
