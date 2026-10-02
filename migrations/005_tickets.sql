-- 工单能力演进（在 004_ledger 之上）：
--
-- 工单登记订单受理、收款、退款、结算、冲正环节出现的问题，形成从受理到处理完成、
-- 可解释的闭环。工单标识由调用方提供，以（租户, 工单标识）唯一：同标识对同一订单、
-- 同一工单类型重复登记返回首次结果，不重复受理；同标识换订单或换类型拒绝。
--
-- 处理状态只允许沿 待处理→处理中→已解决/已驳回 的方向推进，已解决与已驳回为终态。
-- note 留存最后一次处理备注（可空），processed_at 留存最后一次处理时间；
-- 工单只记录问题与处理过程，不改动订单的金额、状态与任何账务。

CREATE TABLE IF NOT EXISTS tickets(
  tenant       TEXT NOT NULL,
  ticket_id    TEXT NOT NULL,
  order_id     TEXT NOT NULL,
  ticket_type  TEXT NOT NULL,                -- acceptance | payment | refund | settlement | reversal
  description  TEXT NOT NULL,                -- 问题描述，非空
  status       TEXT NOT NULL,                -- pending | processing | resolved | rejected
  note         TEXT,                         -- 最后一次处理备注，可空
  processed_at TEXT,                         -- 最后一次处理时间，未处理为 NULL
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, ticket_id)
);

-- 检索按订单标识升序分页，同订单内以工单标识兜底保证顺序稳定、不重不漏。
CREATE INDEX IF NOT EXISTS idx_tickets_order
  ON tickets(tenant, order_id, ticket_id);
