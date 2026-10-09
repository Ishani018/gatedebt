-- Synthetic schema v41 and data for the db-migration-recovery rehearsal.
-- Loaded into a throwaway SQLite file inside a temporary directory only.
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version (version) VALUES (41);

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
INSERT INTO customers (id, email, name, created_at) VALUES
    (1, 'ada@example.test', 'Ada', '2026-01-04T09:00:00Z'),
    (2, 'grace@example.test', 'Grace', '2026-01-05T10:30:00Z'),
    (3, 'linus@example.test', 'Linus', '2026-02-11T14:15:00Z'),
    (4, 'margaret@example.test', 'Margaret', '2026-03-20T08:45:00Z'),
    (5, 'ken@example.test', 'Ken', '2026-04-02T16:00:00Z');

CREATE TABLE invoices (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0)
);
INSERT INTO invoices (id, customer_id, amount_cents) VALUES
    (1, 1, 12000),
    (2, 1, 4500),
    (3, 2, 9900),
    (4, 3, 0),
    (5, 4, 250000),
    (6, 5, 7300);
