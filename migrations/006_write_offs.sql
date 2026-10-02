-- 收款核销能力演进（在 005_tickets 之上）：
--
-- 1) 欠款条目。订单受理时生成欠款编号 1（金额 = 受理时订单金额），每笔退款成功后追加一条
--    （金额 = 该笔退回金额，状态未核销）；欠款编号在订单内从 1 开始按序编号。条目一经生成
--    其欠款金额不再变化；已核销金额随核销单调增加，恒有 已核销 <= 欠款金额（CHECK 兜底）。
--    状态：open（未核销，含部分核销）| closed（余额为 0，已核销）。
--
-- 2) 核销登记。核销标识由调用方提供，以（租户, 核销标识）唯一幂等：同标识对同一订单、
--    同一欠款编号、同一金额重复提交返回首次结果；同标识换订单、换欠款编号或换金额拒绝。
--    留存该条目核销后未核销余额与该订单核销后仍未核销的欠款合计，使结果可解释、可重放。

CREATE TABLE IF NOT EXISTS debt_items(
  tenant       TEXT NOT NULL,
  order_id     TEXT NOT NULL,
  debt_no      INTEGER NOT NULL,              -- 订单内从 1 开始按序编号
  amount_cents INTEGER NOT NULL,              -- 欠款金额，生成后不再变化
  written_off_cents INTEGER NOT NULL DEFAULT 0, -- 已核销金额
  status       TEXT NOT NULL DEFAULT 'open',  -- open（未核销）| closed（已核销）
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, order_id, debt_no),
  CHECK (written_off_cents >= 0 AND written_off_cents <= amount_cents)
);

CREATE TABLE IF NOT EXISTS write_offs(
  tenant        TEXT NOT NULL,
  write_off_id  TEXT NOT NULL,                -- 调用方提供的核销标识
  order_id      TEXT NOT NULL,
  debt_no       INTEGER NOT NULL,
  amount_cents  INTEGER NOT NULL,             -- 核销金额（正整数）
  item_balance_after    INTEGER NOT NULL,     -- 该条目核销后未核销余额
  order_unwritten_after INTEGER NOT NULL,     -- 该订单核销后仍未核销的欠款合计
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, write_off_id)
);

-- 存量数据回填：既有订单补欠款编号 1（金额 = 订单金额），既有退款逐笔补一条欠款条目，
-- 编号按退款落库先后续排；已核销金额从 0 起（历史收款未曾逐笔核销）。可重入。
INSERT OR IGNORE INTO debt_items(tenant, order_id, debt_no, amount_cents)
  SELECT tenant, order_id, 1, amount_cents FROM orders;

INSERT OR IGNORE INTO debt_items(tenant, order_id, debt_no, amount_cents)
  SELECT tenant, order_id,
         2 + (SELECT COUNT(*) FROM refunds prior
               WHERE prior.tenant = refunds.tenant
                 AND prior.order_id = refunds.order_id
                 AND prior.rowid < refunds.rowid),
         amount_cents
  FROM refunds;
