# db-migration-recovery

Sandbox rehearsal (`mode: sandbox`). Everything runs against a throwaway SQLite
file in a fresh temporary directory; no project or production database is
touched.

1. Seed schema v41 + synthetic data (`seed.sql`) and take a backup.
2. **Inject**: apply `migration_0042_broken.sql` statement-by-statement in
   autocommit mode. SQLite rejects the `NOT NULL` column without a default,
   leaving `customer_tiers` created but the version still at 41.
3. **Detect/classify**: the error must match `fixture.json` *and* the partial
   state must be observed. A different error is an unexpected infrastructure
   failure; no error means the injected failure was not detected.
4. **Recover**: restore the pre-migration backup, then apply
   `migration_0042_fixed.sql` in a single transaction.
5. **Verify**: schema, `PRAGMA integrity_check`, foreign keys, every original
   row unchanged, new column defaulted.
6. **Waived check** `integration:migration_0042`: on a fresh database the fixed
   migration applies cleanly, and a failure mid-migration rolls back fully.
7. **Cleanup**: all connections closed, temporary directory removed, verified.
