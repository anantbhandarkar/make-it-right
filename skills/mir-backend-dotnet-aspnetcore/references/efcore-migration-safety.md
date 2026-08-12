# EF Core migration safety on populated tables

The governing assumption: **the table already has millions of rows, and the currently-deployed code is still running the old schema during a rolling deploy.** AI writes migrations as if the table is empty and the new code is the only code. Both are false in production.

EF Core 10 / `Microsoft.EntityFrameworkCore.Design`, verified 13 Aug 2026. Read by the migration-reviewer at Gate 7.

---

## Schema changes that break a live table

| Change | What EF generates by default | Write instead |
|---|---|---|
| Add a NOT NULL column | `ADD col TYPE NOT NULL` — fails or rewrites the table | Add nullable, backfill in a separate bounded step, then add the constraint. Or supply a default value |
| Drop a column | `DROP COLUMN` in the same deploy as the code change | Remove from the model and deploy first; drop in a later migration, so old replicas mid-rolling-deploy never reference a missing column |
| Rename a column or table | **`DROP` + `ADD` — silently destroys the data** | Edit the migration to call `migrationBuilder.RenameColumn(...)` / `RenameTable(...)` |
| Add an index on a large table | `CREATE INDEX` — takes an exclusive lock | On Postgres, `migrationBuilder.Sql("CREATE INDEX CONCURRENTLY ...", suppressTransaction: true)`. **`suppressTransaction: true` is mandatory** — EF wraps migrations in a transaction and Postgres rejects `CONCURRENTLY` inside one. Put it in its own migration, and check for an `INVALID` index afterwards |
| Change a column type | `ALTER COLUMN` — table rewrite plus lock | Expand/contract: add the new column, dual-write, backfill in batches, swap reads, drop the old one |

Always read the generated SQL with `dotnet ef migrations script` before applying to production, especially for any change that touches existing rows.

## Operational rules

- **Never call `Database.Migrate()` from `Program.cs` in a multi-instance deployment.** Every replica races to apply the same migration on boot. Run migrations as a separate step — `dotnet ef migrations bundle` produces a self-contained executable, or generate an idempotent script with `dotnet ef migrations script --idempotent`.
- **`EnsureCreated()` and migrations are mutually exclusive.** `EnsureCreated()` creates the schema without a `__EFMigrationsHistory` row, so the first real migration then fails on an "already exists" error. It is for tests only.
- A migration that both changes schema and moves data should be split: schema change, deploy, backfill job, then the constraint. A single migration that does all three holds a lock for the length of the backfill.
