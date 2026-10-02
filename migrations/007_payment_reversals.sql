-- 收款撤销能力演进（在 006_debts 之上）：
--
-- 1) 收款流水。收款登记此前只写入账务历史（biz_ref 为 pay-<订单内收款序号>），
--    现单独成表并以（租户, 收款业务标识）唯一：撤销按该业务标识定位一笔收款。
--    amount_cents 为原始收款金额，reversed_cents 为累计已撤销金额，
--    净额 = amount_cents - reversed_cents；撤销不删除原收款流水，只累加 reversed_cents。
--    pay_seq 与账务历史 pay-N 的序号一致，用于按笔追踪占用与稳定排序。
--
-- 2) 收款占用明细。记录每笔收款在每个欠款条目上占用的核销金额，使撤销时可以
--    “按欠款编号逆序从该笔收款占用的尾部释放”，并让收款核销改配同步收窄/迁移占用。
--    恒有 Σallocation = 该收款净额；全部收款的 Σallocation = 订单已收金额。
--
-- 3) 收款撤销登记。撤销标识由调用方提供，以（租户, 撤销标识）幂等；存首次生效
--    快照（撤销后该笔收款净额、订单已收/已退/未收、订单状态），重复提交原样返回；
--    同标识换收款业务标识或换金额拒绝。撤销同时向账务历史追加一条 payment_reversal 条目。
--
-- 升级前已存在的收款由迁移逻辑从 ledger_entries / debt_entries 回填，
-- 占用按欠款编号升序摊到各条目（与收款时的自动占用规则一致）。

CREATE TABLE IF NOT EXISTS payments(
  tenant         TEXT NOT NULL,
  payment_ref    TEXT NOT NULL,             -- 收款业务标识 pay-<订单内收款序号>
  order_id       TEXT NOT NULL,
  pay_seq        INTEGER NOT NULL,          -- 订单内收款序号（与历史 biz_ref 一致）
  amount_cents   INTEGER NOT NULL,          -- 原始收款金额，一经登记不再变化
  reversed_cents INTEGER NOT NULL DEFAULT 0,-- 累计已撤销金额
  currency       TEXT NOT NULL,
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, order_id, payment_ref),
  CHECK(amount_cents > 0),
  CHECK(reversed_cents >= 0 AND reversed_cents <= amount_cents)
);

-- 按订单按序号查询收款（撤销、占用追踪均以订单内序号稳定排序）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_order_seq
  ON payments(tenant, order_id, pay_seq);

CREATE TABLE IF NOT EXISTS payment_allocations(
  tenant       TEXT NOT NULL,
  order_id     TEXT NOT NULL,
  payment_ref  TEXT NOT NULL,
  debt_no      INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,            -- 该笔收款在该条目上占用的核销金额（> 0）
  PRIMARY KEY(tenant, order_id, payment_ref, debt_no),
  CHECK(amount_cents > 0),
  FOREIGN KEY(tenant, order_id, payment_ref) REFERENCES payments(tenant, order_id, payment_ref)
);

-- 按欠款条目反查占用（核销改配、撤销尾部释放均按 debt_no 排序扫描）。
CREATE INDEX IF NOT EXISTS idx_payment_allocations_debt
  ON payment_allocations(tenant, order_id, debt_no);

CREATE TABLE IF NOT EXISTS payment_reversals(
  tenant              TEXT NOT NULL,
  reversal_id         TEXT NOT NULL,        -- 撤销标识（调用方提供，租户内唯一）
  payment_ref         TEXT NOT NULL,        -- 被撤销收款的业务标识
  order_id            TEXT NOT NULL,
  amount_cents        INTEGER NOT NULL,     -- 本次撤销金额
  payment_net_after   INTEGER NOT NULL,     -- 撤销后该笔收款净额（首次快照）
  paid_after          INTEGER NOT NULL,     -- 撤销后订单已收金额
  refunded_after      INTEGER NOT NULL,     -- 撤销后订单已退金额（撤销不改变已退）
  outstanding_after   INTEGER NOT NULL,     -- 撤销后订单未收金额
  status_after        TEXT NOT NULL,        -- 撤销后订单状态
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY(tenant, reversal_id),
  CHECK(amount_cents > 0)
);

-- 账务历史中同一订单的撤销流水以撤销标识为业务标识；唯一索引兜底并发重复落库。
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_reversals_order_ref
  ON payment_reversals(tenant, order_id, reversal_id);

-- 4) 老数据回填收款流水：从账务历史的 payment 条目恢复每笔收款（序号与金额即历史原值）。
INSERT INTO payments(tenant, payment_ref, order_id, pay_seq, amount_cents, reversed_cents, currency)
SELECT l.tenant, l.biz_ref, l.order_id,
       CAST(SUBSTR(l.biz_ref, 5) AS INTEGER) AS pay_seq,
       l.amount_cents, 0, o.currency
FROM ledger_entries l
JOIN orders o ON o.tenant = l.tenant AND o.order_id = l.order_id
WHERE l.entry_type = 'payment'
  AND NOT EXISTS (
    SELECT 1 FROM payments p WHERE p.tenant = l.tenant AND p.payment_ref = l.biz_ref
  );

-- 4b) 更早的历史订单（004 账务历史之前受理）：已收金额没有对应收款流水，
--     合并补记一笔 pay-0（序号 0，排在真实收款之前），金额取“当前已收 − 已回填收款金额”
--     的差额；升级后的新收款自 pay-1 起，不会与 pay-0 冲突。
INSERT INTO payments(tenant, payment_ref, order_id, pay_seq, amount_cents, reversed_cents, currency)
SELECT o.tenant, 'pay-0', o.order_id, 0,
       o.paid_cents - COALESCE((
         SELECT SUM(p.amount_cents) FROM payments p
         WHERE p.tenant = o.tenant AND p.order_id = o.order_id
       ), 0),
       0, o.currency
FROM orders o
WHERE o.paid_cents > COALESCE((
  SELECT SUM(p.amount_cents) FROM payments p
  WHERE p.tenant = o.tenant AND p.order_id = o.order_id
), 0);

-- 5) 老数据回填占用：每笔收款按欠款编号升序摊入各条目当前已核销余额。
--    升级前的核销改配只体现在 debt_entries.settled_cents 上、未保留按笔出处，
--    这里按“先入账的钱先占用最早欠款”的同一生效规则，用各条目已核销总额作为
--    容量重放全部收款；恒有 Σ占用 = 已收、各条目占用合计 = 该条目 settled_cents。
INSERT INTO payment_allocations(tenant, order_id, payment_ref, debt_no, amount_cents)
WITH debts AS (
  SELECT tenant, order_id, debt_no, settled_cents AS capacity
  FROM debt_entries WHERE settled_cents > 0
),
pays AS (
  SELECT tenant, order_id, payment_ref, amount_cents,
         SUM(amount_cents) OVER (PARTITION BY tenant, order_id ORDER BY pay_seq
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum,
         SUM(amount_cents) OVER (PARTITION BY tenant, order_id ORDER BY pay_seq
                                 ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - amount_cents AS prev_cum
  FROM payments
),
debt_cum AS (
  SELECT tenant, order_id, debt_no, capacity,
         SUM(capacity) OVER (PARTITION BY tenant, order_id ORDER BY debt_no
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS dcum,
         SUM(capacity) OVER (PARTITION BY tenant, order_id ORDER BY debt_no
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) - capacity AS prev_dcum
  FROM debts
)
SELECT x.tenant, x.order_id, x.payment_ref, x.debt_no, x.take
FROM (
  SELECT p.tenant, p.order_id, p.payment_ref, d.debt_no,
         MIN(p.cum, d.dcum) - MAX(p.prev_cum, d.prev_dcum) AS take
  FROM pays p
  JOIN debt_cum d ON d.tenant = p.tenant AND d.order_id = p.order_id
) x
WHERE x.take > 0;
