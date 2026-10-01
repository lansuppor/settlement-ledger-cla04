CREATE TABLE IF NOT EXISTS orders(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  paid_cents INTEGER NOT NULL DEFAULT 0,
  currency TEXT NOT NULL,
  status TEXT NOT NULL,
  PRIMARY KEY(tenant, order_id)
);

CREATE TABLE IF NOT EXISTS payments(
  id INTEGER NOT NULL PRIMARY KEY,
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  amount_cents INTEGER NOT NULL,
  reversed INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id)
);

CREATE INDEX IF NOT EXISTS idx_payments_order ON payments(tenant, order_id);

CREATE TABLE IF NOT EXISTS reversals(
  tenant TEXT NOT NULL,
  order_id TEXT NOT NULL,
  reversal_id TEXT NOT NULL,
  payment_id INTEGER NOT NULL,
  amount_cents INTEGER NOT NULL,
  PRIMARY KEY(tenant, order_id, reversal_id),
  UNIQUE(payment_id),
  FOREIGN KEY(tenant, order_id) REFERENCES orders(tenant, order_id),
  FOREIGN KEY(payment_id) REFERENCES payments(id)
);
