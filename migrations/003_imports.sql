-- 批量导入能力演进（在 002_refunds 之上）：
-- 1) 导入批次。批次标识由调用方提供，同一租户内唯一；payload 落库以便中断后续跑。
--    request_hash 用于识别“同批次不同内容”的冲突提交。
CREATE TABLE IF NOT EXISTS import_batches(
  tenant       TEXT NOT NULL,
  batch_id     TEXT NOT NULL,
  total_rows   INTEGER NOT NULL,
  status       TEXT NOT NULL,            -- processing | completed
  request_hash TEXT NOT NULL,
  payload      TEXT NOT NULL,            -- 提交行原文（JSON），重启后据此续跑
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, batch_id)
);

-- 2) 逐行结果。每行处理完原子落库：成功行与订单同事务，失败行保留行号与原因。
--    已落库的行在续跑时跳过，保证不重复受理、计数闭合。
CREATE TABLE IF NOT EXISTS import_rows(
  tenant     TEXT NOT NULL,
  batch_id   TEXT NOT NULL,
  row_no     INTEGER NOT NULL,
  order_id   TEXT,
  outcome    TEXT NOT NULL,              -- success | failed
  error      TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, batch_id, row_no)
);
