# Alembic migration safety on populated tables

The governing assumption: **the table already has millions of rows, and the currently-deployed code is still running the old schema during a rolling deploy.** AI writes migrations as if the table is empty and the new code is the only code. Both are false in production.

Read by the migration-reviewer agent at Gate 7.

---

## The expand / contract pattern (the core discipline)

Never change a column's shape in one step while old code runs. Split every breaking change into phases across separate deploys:

1. **Expand** — add the new thing, nullable/optional, no constraint. Old + new code both work.
2. **Backfill** — populate existing rows in batches (below). Dual-write from new code if needed.
3. **Migrate** — switch reads to the new column; add the constraint once data is clean.
4. **Contract** — drop the old column *after* no deployed code references it (a later deploy).

A rename is expand+contract: add new column → dual-write → backfill → switch reads → drop old. Never a bare `ALTER ... RENAME` while old code reads the old name.

---

## Operations that lock a large table (avoid / rewrite)

| Dangerous statement | Why | Safe alternative |
|---|---|---|
| `ADD COLUMN x NOT NULL` (no default) | Fails / rewrites; old code inserting rows breaks | Add nullable (or with default), backfill, then `SET NOT NULL` after validation |
| `ADD COLUMN x NOT NULL DEFAULT <volatile>` | Table rewrite on old Postgres | Constant default is metadata-only on PG 11+; volatile/expression still rewrites — backfill instead |
| `CREATE INDEX` (plain) | Exclusive lock blocks writes for the build | `CREATE INDEX CONCURRENTLY` (outside a tx — see below) |
| `ALTER COLUMN TYPE` | Full table rewrite + lock | Add new column, backfill, swap |
| `ADD CONSTRAINT ... CHECK/FK` | Validates all rows under lock | `ADD CONSTRAINT ... NOT VALID`, then `VALIDATE CONSTRAINT` separately (short locks) |
| `SET NOT NULL` on big table | Full scan under lock (older PG) | PG 12+: add a validated CHECK (col IS NOT NULL) first, then SET NOT NULL is cheap |

### `CONCURRENTLY` and Alembic transactions
`CREATE INDEX CONCURRENTLY` cannot run inside a transaction block. Alembic wraps migrations in a tx by default. Either set `transactional_ddl`/autocommit for that migration or use:

```python
def upgrade():
    with op.get_context().autocommit_block():
        op.create_index("ix_orders_status", "orders", ["status"], postgresql_concurrently=True)
```

---

## Batched backfill (don't lock the whole table)

```python
# WRONG — single UPDATE locks/holds a huge transaction
op.execute("UPDATE orders SET status = 'PENDING' WHERE status IS NULL")

# RIGHT — batch in chunks, commit between (run as a data migration or out-of-band script)
conn = op.get_bind()
while True:
    res = conn.execute(text(
        "UPDATE orders SET status='PENDING' "
        "WHERE id IN (SELECT id FROM orders WHERE status IS NULL LIMIT 5000)"))
    if res.rowcount == 0:
        break
```
For very large tables, run the backfill as a separate, resumable job — not inside the schema migration at all.

---

## Rolling-deploy compatibility checklist

- During the deploy window, **old code runs against the new schema.** Will it break? (Dropped/renamed column it reads; NOT NULL on a column it inserts NULL into.)
- After rollback, **new-schema data must survive old code.** Does the down-migration lose data written under the new schema? If it would, the down-migration should be a no-op and you roll forward instead.
- Additive changes (new nullable column, new table, new index) are rolling-safe. Destructive changes (drop, rename, tighten constraint) are not — defer to the contract phase.

## Down-migrations
Provide one, but treat data-losing down-migrations as a red flag. For destructive changes the honest `downgrade()` often can't restore dropped data — say so explicitly and rely on roll-forward + backup, rather than pretending the down-migration is safe.
