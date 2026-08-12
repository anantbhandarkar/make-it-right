# Alembic migration safety on populated tables

The governing assumption: **the table already has millions of rows, and the currently-deployed code is still running the old schema during a rolling deploy.** AI writes migrations as if the table is empty and the new code is the only code. Both are false in production.

Alembic 1.19.1 verified current 13 Aug 2026 (requires Python ≥3.10). Postgres statements below assume a currently-supported server (15+); the `SET NOT NULL` shortcut needs 12+, the fast `ADD COLUMN … DEFAULT <constant>` needs 11+.

Read by the migration-reviewer agent at Gate 7.

---

## Before anything else: bound the lock wait

A migration that waits for an `ACCESS EXCLUSIVE` lock **queues every subsequent query behind it**, including plain `SELECT`s. One long-running report can turn a two-millisecond `ALTER` into a full outage. Fail fast instead:

```python
def upgrade():
    op.execute("SET LOCAL lock_timeout = '3s'")
    op.execute("SET LOCAL statement_timeout = '30s'")
    ...
```

If the migration can't get the lock in 3 seconds it errors out and you retry — instead of silently holding the queue open. Do this in every migration that touches an existing table.

Use `SET LOCAL`, not `SET`: `SET LOCAL` is scoped to the transaction Alembic already opened, so it cannot leak onto a pooled connection and silently time out a later revision or an application query. The exception is an `autocommit_block` (there is no transaction, so `SET LOCAL` warns and does nothing) — there, use plain `SET` immediately before the statement and reset it in a `finally`.

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
| `ADD COLUMN x NOT NULL` (no default) | Fails outright on a non-empty table; old code inserting rows breaks | Add nullable, backfill, then `SET NOT NULL` after validation |
| `ADD COLUMN x NOT NULL DEFAULT <volatile>` | A **volatile** default (`now()`, `random()`, `gen_random_uuid()`) must be evaluated per row, so it rewrites the whole table | PG 11+ stores any **non-volatile** default as a metadata-only "missing value" — a literal or a stable expression is cheap. Volatile → add nullable and backfill |
| `CREATE INDEX` (plain) | Holds a lock that blocks writes for the whole build | `CREATE INDEX CONCURRENTLY` (outside a tx — see below) |
| `ALTER COLUMN TYPE` | Full table rewrite + `ACCESS EXCLUSIVE` lock | Add new column, backfill, swap, drop old |
| `ADD CONSTRAINT … CHECK/FOREIGN KEY` | Validates every row under lock | `ADD CONSTRAINT … NOT VALID`, then `VALIDATE CONSTRAINT` in a later statement (short locks) |
| `SET NOT NULL` on a big table | Full scan under lock | PG 12+: add `CHECK (col IS NOT NULL) NOT VALID`, `VALIDATE` it, then `SET NOT NULL` reuses the proof and skips the scan |
| `DROP COLUMN` | Metadata-only and fast — but old code still selecting that column starts erroring the instant it runs | Contract phase only, after the deploy that stopped referencing it |

### `CONCURRENTLY` and Alembic transactions
`CREATE INDEX CONCURRENTLY` cannot run inside a transaction block. Alembic wraps migrations in a tx by default. Use an autocommit block:

```python
def upgrade():
    with op.get_context().autocommit_block():
        op.create_index("ix_orders_status", "orders", ["status"], postgresql_concurrently=True)
```

**It can fail and leave a mess.** A `CONCURRENTLY` build that is interrupted leaves an `INVALID` index behind that the planner ignores but writes still maintain. The retry path is: check `SELECT indisvalid FROM pg_index WHERE indexrelid = 'ix_orders_status'::regclass`, then `REINDEX INDEX CONCURRENTLY ix_orders_status` or `DROP INDEX CONCURRENTLY IF EXISTS ix_orders_status` and rebuild. Say this in the runbook.

**Do not "fix" this with `if_not_exists`.** `CREATE INDEX IF NOT EXISTS` sees the invalid index, decides the name is taken, and succeeds as a no-op — you keep the write overhead and get none of the reads, with a green migration. Postgres documents that `IF NOT EXISTS` gives no guarantee the existing index resembles the one you asked for. The reviewer should check that `postgresql_concurrently=True` is paired with a documented validity check and drop-and-retry, never with `if_not_exists`.

---

## Batched backfill (don't lock the whole table)

```python
# WRONG — single UPDATE holds one huge transaction
op.execute("UPDATE orders SET status = 'PENDING' WHERE status IS NULL")

# ALSO WRONG — looks batched, but every batch is still inside Alembic's one
# migration transaction, so nothing commits until the end. Locks, WAL and dead
# tuples accumulate exactly as if you had written the single UPDATE above.
conn = op.get_bind()
while True:
    res = conn.execute(text("UPDATE orders SET status = :val WHERE id IN (…LIMIT 5000)"),
                       {"val": "PENDING"})
    if res.rowcount == 0:
        break
```

**Batching only helps if each batch commits.** Alembic wraps `upgrade()` in a transaction by default, so a loop over `op.get_bind()` does not. Either commit per batch explicitly, or — better for a large table — don't put the backfill in the migration at all:

```python
# RIGHT — autocommit_block ends Alembic's transaction, so each UPDATE commits on its own
def upgrade():
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        while True:
            res = conn.execute(text(
                "UPDATE orders SET status = :val "
                "WHERE id IN (SELECT id FROM orders WHERE status IS NULL LIMIT 5000)"),
                {"val": "PENDING"})
            if res.rowcount == 0:
                break
```

`begin_nested()` is not a substitute — a `SAVEPOINT` release does not commit anything; it still all lands or rolls back with the outer transaction.

For anything large, run the backfill as a **separate resumable program** outside `upgrade()`: record progress (last id processed) so a kill -9 resumes instead of restarting, and make each batch safe to re-run. The migration then only adds the column and, in a later deploy, the constraint.

Bind the values. An `op.execute(f"UPDATE … SET name = '{value}'")` built from application data is SQL injection with the migration user's privileges, which are usually the highest in the system.

---

## Rolling-deploy compatibility checklist

- During the deploy window, **old code runs against the new schema.** Will it break? (Dropped/renamed column it reads; NOT NULL on a column it inserts NULL into; a new enum value it doesn't know.)
- After rollback, **new-schema data must survive old code.** Does the down-migration lose data written under the new schema? If it would, the down-migration should be a no-op and you roll forward instead.
- Additive changes (new nullable column, new table, new index) are rolling-safe. Destructive changes (drop, rename, tighten constraint) are not — defer to the contract phase.
- **Run migrations as one job, not in the app's entrypoint.** N replicas starting at once means N concurrent `alembic upgrade head` runs racing on the same revision. Use a pre-deploy job, or take an explicit advisory lock (`SELECT pg_advisory_lock(<id>)`) around the upgrade.
- **Multiple heads** appear the moment two branches each add a revision off the same `down_revision`. `alembic heads` in CI; fail the build on more than one. Merging with `alembic merge` after the fact is a resolution, not a plan.

## Down-migrations
Provide one, but treat data-losing down-migrations as a red flag. For destructive changes the honest `downgrade()` often can't restore dropped data — say so explicitly and rely on roll-forward + backup, rather than pretending the down-migration is safe.
