-- Migration 0042 as originally shipped. The runner applies statements one by
-- one in autocommit mode, so the first statement sticks when the second fails:
-- SQLite refuses to add a NOT NULL column without a default to a table that
-- already has rows. Result: a partially applied migration.
CREATE TABLE customer_tiers (
    customer_id INTEGER PRIMARY KEY REFERENCES customers(id),
    changed_at TEXT NOT NULL
);
ALTER TABLE customers ADD COLUMN tier TEXT NOT NULL;
UPDATE schema_version SET version = 42;
