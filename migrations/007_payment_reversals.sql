-- 收款撤销能力演进（在 006_debts 之上）：
--
-- 撤销只做账务冲回，不是退款：按一笔已登记收款的业务标识撤销其全部或部分金额，
-- 已收随撤销等额减少、未收等额增加，已退金额不变；撤销不删除原收款流水，而是在
-- 订单账务历史追加一条可辨识的撤销条目。同一笔收款可被多笔不同撤销标识分次冲回，
-- 累计撤销不得超过该笔收款的净额（原收款金额 − 累计已撤销）。
--
-- 1) 收款台账。每笔已登记收款一行，pay_no 为订单内从 1 开始的收款序号，业务标识为
--    pay-<pay_no>（与账务历史中既有收款条目的 biz_ref 一致）；amount_cents 为原收款
--    金额，reversed_cents 为累计已撤销金额，恒有 0 <= reversed_cents <= amount_cents，
--    净额 = amount_cents − reversed_cents。
--
-- 2) 撤销登记。撤销标识由调用方提供，以（租户, 撤销标识）幂等：同标识对同一收款、
--    同一金额重复提交返回首次快照，换收款业务标识或换金额拒绝。快照留存撤销后该笔
--    收款净额与订单已收/已退/未收/状态，使重放结果可解释、与首次一致。撤销同时向
--    订单账务历史追加一条类型为 payment_reversal 的条目。
--
-- 升级前已登记的收款由迁移逻辑从账务历史逐笔回填（业务标识即 pay-<序号>，
-- 金额取流水金额，初始累计撤销 0）；之后每笔收款登记同事务写入本表。

CREATE TABLE IF NOT EXISTS payments(
  tenant         TEXT NOT NULL,
  order_id       TEXT NOT NULL,
  pay_no         INTEGER NOT NULL,              -- 订单内从 1 开始按序编号
  biz_ref        TEXT NOT NULL,                 -- 收款业务标识 pay-<pay_no>
  amount_cents   INTEGER NOT NULL,              -- 原收款金额，不因撤销改变
  reversed_cents INTEGER NOT NULL DEFAULT 0,    -- 累计已撤销金额
  currency       TEXT NOT NULL,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, order_id, pay_no),
  CHECK(amount_cents > 0),
  CHECK(reversed_cents >= 0 AND reversed_cents <= amount_cents)
);

-- 按收款业务标识定位一笔收款；同订单内业务标识唯一。
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_order_ref
  ON payments(tenant, order_id, biz_ref);

CREATE TABLE IF NOT EXISTS payment_reversals(
  tenant            TEXT NOT NULL,
  reversal_id       TEXT NOT NULL,             -- 调用方提供，同一租户内唯一
  order_id          TEXT NOT NULL,
  payment_ref       TEXT NOT NULL,             -- 被撤销收款的业务标识
  amount_cents      INTEGER NOT NULL,
  payment_net_after INTEGER NOT NULL,          -- 撤销后该笔收款的净额（首次快照）
  paid_after        INTEGER NOT NULL,          -- 撤销后订单已收（首次快照）
  refunded_cents    INTEGER NOT NULL,          -- 撤销时订单已退（撤销不改变已退）
  outstanding_after INTEGER NOT NULL,          -- 撤销后订单未收（首次快照）
  status_after      TEXT NOT NULL,             -- 撤销后订单状态（首次快照）
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, reversal_id),
  CHECK(amount_cents > 0)
);

-- 账务历史中同一订单的撤销流水以撤销标识为业务标识；唯一索引兜底并发下的重复落库。
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_reversals_order_ref
  ON payment_reversals(tenant, order_id, reversal_id);

-- 3) 老数据回填：升级前已登记的收款从账务历史的 payment 条目逐笔补建台账行。
--    幂等：只处理尚无任何收款台账行的订单；biz_ref/金额/序号直接取自流水，初始撤销 0。
INSERT INTO payments(tenant, order_id, pay_no, biz_ref, amount_cents, reversed_cents, currency)
SELECT le.tenant, le.order_id, le.seq_no_for_pay, le.biz_ref, le.amount_cents, 0, o.currency
FROM (
  SELECT tenant, order_id, biz_ref, amount_cents,
         ROW_NUMBER() OVER w AS seq_no_for_pay
  FROM ledger_entries
  WHERE entry_type = 'payment'
  WINDOW w AS (PARTITION BY tenant, order_id ORDER BY seq_no)
) le
JOIN orders o ON o.tenant = le.tenant AND o.order_id = le.order_id
WHERE NOT EXISTS (
  SELECT 1 FROM payments p WHERE p.tenant = le.tenant AND p.order_id = le.order_id
);
