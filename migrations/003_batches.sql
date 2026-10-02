-- 批量导入能力演进：
-- 1) 批次头：批次标识由调用方提供，同一租户内唯一；payload_hash 为提交内容指纹，
--    重复提交同一批次标识且内容一致时原样返回首次受理结果，不一致则拒绝。
CREATE TABLE IF NOT EXISTS import_batches(
  tenant         TEXT NOT NULL,
  batch_id       TEXT NOT NULL,
  status         TEXT NOT NULL,               -- processing | completed
  total_rows     INTEGER NOT NULL,
  succeeded_rows INTEGER NOT NULL DEFAULT 0,
  failed_rows    INTEGER NOT NULL DEFAULT 0,
  payload_hash   TEXT NOT NULL,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, batch_id)
);

-- 2) 批次行：逐行落库（pending/imported/failed），失败行保留行号与原因，便于逐行定位。
--    每行结果与批次计数在同一事务内提交；服务中断后按 state='pending' 续跑，
--    已生效行不重复受理，恒有 成功行数 + 失败行数 = 提交总行数。
CREATE TABLE IF NOT EXISTS import_batch_rows(
  tenant    TEXT NOT NULL,
  batch_id  TEXT NOT NULL,
  line_no   INTEGER NOT NULL,               -- 1 起始，与提交顺序一致
  payload   TEXT NOT NULL,                  -- 原始行 JSON，可解释、可重放
  state     TEXT NOT NULL DEFAULT 'pending',
  error     TEXT,
  PRIMARY KEY(tenant, batch_id, line_no)
);
