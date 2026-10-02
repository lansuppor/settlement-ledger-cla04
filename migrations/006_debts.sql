-- 收款核销能力演进（在 005_tickets 之上）：
--
-- 1) 欠款条目。订单受理时生成第 1 条（金额等于订单金额）；此后每笔退款成功生成一条新条目，
--    金额等于该笔退回金额；订单内编号 debt_no 从 1 开始按序递增。条目金额一经生成不再变化，
--    账务历史里原有条目不被改写。
--
--    settled_cents 为该条目已核销金额：收款登记按 debt_no 升序自动占用各条目未核销余额；
--    “收款核销”登记则把一笔已到账金额改配（re-pin）到指定条目。恒有
--    0 <= settled_cents <= amount_cents，状态在余额为 0 时为 settled，否则 unsettled。
--
-- 2) 核销登记。核销标识由调用方提供，以（租户, 核销标识）幂等；同标识对同订单、同欠款编号、
--    同金额重复提交返回首次快照，换订单/欠款编号/金额拒绝。登记快照留存目标条目核销后的
--    已核销/未核销金额与该订单核销后仍未核销的欠款合计，使重放结果可解释、与首次一致。
--    核销同时向订单账务历史追加一条类型为 writeoff 的条目。
--
-- 升级前已存在的订单由迁移逻辑补齐欠款条目（第 1 条为订单金额，其后按退款时间逐笔补条），
-- 已收款按编号顺序占用，保证升级后 Σ未核销余额 仍与订单未收金额一致。

CREATE TABLE IF NOT EXISTS debt_entries(
  tenant        TEXT NOT NULL,
  order_id      TEXT NOT NULL,
  debt_no       INTEGER NOT NULL,              -- 订单内从 1 开始按序编号
  amount_cents  INTEGER NOT NULL,              -- 欠款金额，一经生成不再变化
  settled_cents INTEGER NOT NULL DEFAULT 0,    -- 已核销金额（收款自动占用 + 登记核销改配）
  currency      TEXT NOT NULL,
  status        TEXT NOT NULL,                 -- unsettled | settled
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, order_id, debt_no),
  CHECK(amount_cents > 0),
  CHECK(settled_cents >= 0 AND settled_cents <= amount_cents)
);

-- 按订单查询欠款条目，编号升序。
CREATE INDEX IF NOT EXISTS idx_debts_order
  ON debt_entries(tenant, order_id, debt_no);

CREATE TABLE IF NOT EXISTS writeoffs(
  tenant         TEXT NOT NULL,
  writeoff_id    TEXT NOT NULL,
  order_id       TEXT NOT NULL,
  debt_no        INTEGER NOT NULL,
  amount_cents   INTEGER NOT NULL,
  settled_after  INTEGER NOT NULL,             -- 目标条目核销后已核销金额（首次快照）
  remaining_after INTEGER NOT NULL,            -- 目标条目核销后未核销余额（首次快照）
  remaining_total_after INTEGER NOT NULL,      -- 该订单核销后仍未核销的欠款合计
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, writeoff_id),
  CHECK(amount_cents > 0)
);

-- 账务历史中同一订单的核销流水以核销标识为业务标识；唯一索引兜底并发下的重复落库。
CREATE UNIQUE INDEX IF NOT EXISTS idx_writeoffs_order_ref
  ON writeoffs(tenant, order_id, writeoff_id);

-- 3) 老数据补齐：升级前已存在的订单补建第 1 条欠款（金额=订单金额），已收款按编号顺序占用。
--    幂等：只处理尚无任何欠款条目的订单。
INSERT INTO debt_entries(tenant, order_id, debt_no, amount_cents, settled_cents, currency, status)
SELECT o.tenant, o.order_id, 1, o.amount_cents,
       MIN(o.paid_cents, o.amount_cents),
       o.currency,
       CASE WHEN MIN(o.paid_cents, o.amount_cents) >= o.amount_cents THEN 'settled' ELSE 'unsettled' END
FROM orders o
WHERE NOT EXISTS (
  SELECT 1 FROM debt_entries d WHERE d.tenant = o.tenant AND d.order_id = o.order_id
);

-- 退款条目按退款时间逐笔补建（编号自 2 起）；已收超出订单金额的部分（即退款后又补收的钱）
-- 同样按编号顺序占用各退款条目，保证  Σ未核销余额 = 订单未收金额  在升级后立刻成立。
INSERT INTO debt_entries(tenant, order_id, debt_no, amount_cents, settled_cents, currency, status)
WITH numbered AS (
  SELECT r.tenant, r.order_id, r.amount_cents AS rf_amt, r.currency,
         ROW_NUMBER() OVER w AS rn,
         SUM(r.amount_cents) OVER w AS cum,
         o.amount_cents AS order_amt, o.paid_cents AS paid
  FROM refunds r
  JOIN orders o ON o.tenant = r.tenant AND o.order_id = r.order_id
  WHERE NOT EXISTS (
    SELECT 1 FROM debt_entries d WHERE d.tenant = r.tenant AND d.order_id = r.order_id AND d.debt_no > 1
  )
  WINDOW w AS (PARTITION BY r.tenant, r.order_id ORDER BY r.created_at, r.refund_id
               ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
)
SELECT tenant, order_id, rn + 1, rf_amt,
       MIN(rf_amt, MAX(0, paid - order_amt - (cum - rf_amt))),
       currency,
       CASE WHEN MIN(rf_amt, MAX(0, paid - order_amt - (cum - rf_amt))) >= rf_amt
            THEN 'settled' ELSE 'unsettled' END
FROM numbered;
