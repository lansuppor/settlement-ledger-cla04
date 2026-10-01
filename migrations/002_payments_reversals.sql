-- 收款明细：每笔登记的收款都有订单内唯一标识，可供后续点名冲正
CREATE TABLE IF NOT EXISTS payments(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  payment_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY(tenant, order_id, payment_id)
);

-- 冲正明细：一笔收款至多被冲正一次（见下方 UNIQUE 索引）；
-- 冲正标识在订单内唯一；金额复制自原收款，便于事后核对
CREATE TABLE IF NOT EXISTS reversals(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  reversal_id TEXT NOT NULL,
  payment_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  PRIMARY KEY(tenant, order_id, reversal_id)
);

-- 一笔收款只能被冲正一次；该索引同时作为“收款已被冲正”的判定依据
CREATE UNIQUE INDEX IF NOT EXISTS idx_reversals_payment
  ON reversals(tenant, order_id, payment_id);
