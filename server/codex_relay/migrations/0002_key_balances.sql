ALTER TABLE client_keys ADD COLUMN balance_usd TEXT;
ALTER TABLE client_keys ADD COLUMN balance_blocked INTEGER NOT NULL DEFAULT 0;
ALTER TABLE client_keys ADD COLUMN balance_updated_at TEXT;
CREATE TABLE key_balance_entries (
    entry_id TEXT PRIMARY KEY,
    key_id TEXT NOT NULL REFERENCES client_keys(key_id),
    request_id TEXT UNIQUE REFERENCES requests(request_id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK(kind IN ('charge','set','unpriced')),
    amount_usd TEXT,
    balance_after_usd TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_key_balance_entries_key_at ON key_balance_entries(key_id,created_at);
