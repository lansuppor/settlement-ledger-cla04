-- 工单能力演进（在 004_ledger 之上）：
-- 针对订单受理、收款、退款、结算、冲正环节出现的问题登记工单，形成从受理到处理完成的闭环。
-- 工单标识由调用方提供，同一租户内唯一；同标识对同一订单、同一工单类型重复登记返回首次结果，
-- 同标识换订单或换工单类型拒绝。status 按 pending -> processing -> resolved/rejected 推进，
-- resolved/rejected 为终态不再变化；note 与 processed_at 留存最后一次处理备注与处理时间。
-- 工单只记录问题与处理过程，不回写订单金额、状态与账务。

CREATE TABLE IF NOT EXISTS tickets(
  tenant       TEXT NOT NULL,
  ticket_id    TEXT NOT NULL,
  order_id     TEXT NOT NULL,
  ticket_type  TEXT NOT NULL,                -- accept | payment | refund | settlement | reversal
  description  TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending', -- pending | processing | resolved | rejected
  note         TEXT,                         -- 最后一次处理备注，可空
  processed_at TEXT,                         -- 最后一次处理时间，可空
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, ticket_id)
);

-- 检索按订单标识升序分页，(order_id, ticket_id) 为稳定排序键（同一订单可有多张工单）。
CREATE INDEX IF NOT EXISTS idx_tickets_order ON tickets(tenant, order_id, ticket_id);
