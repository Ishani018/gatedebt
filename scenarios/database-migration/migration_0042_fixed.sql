-- Remediated migration 0042: the new column has a default, and the runner
-- applies the whole migration inside a single transaction.
CREATE TABLE customer_tiers (
    customer_id INTEGER PRIMARY KEY REFERENCES customers(id),
    changed_at TEXT NOT NULL
);
ALTER TABLE customers ADD COLUMN tier TEXT NOT NULL DEFAULT 'standard';
UPDATE schema_version SET version = 42;
