# Migration safety on populated tables

Two governing assumptions. Both are true in production and both are false in the migration AI writes by default:

1. **The table already has millions of rows.** A statement that is instant on an empty table can hold an exclusive lock for minutes on a real one.
2. **The currently deployed application version is still running against the new schema.** During a rolling deploy, and for the whole window between the migration and the last pod restart, old code and new schema coexist. After a rollback, old code runs against the new schema again — for longer.

Read at **Gate 5** (migration plan) and **Gate 6** (writing it). The `migration-reviewer` agent reads this at Gate 7. Statement-level lock behavior for one engine belongs in that engine's module (`mir-database-postgres`, `mir-database-mongo`); this file is the engine-independent discipline plus the lock classes you must look up.

---

## 1. Expand / contract

Never change a column's shape in one step. Split every breaking change across separate deploys:

| Phase | What you do | Who can be running |
|---|---|---|
| **Expand** | Add the new thing — nullable, no constraint, additive only | Old code and new code both work |
| **Backfill** | Populate existing rows in bounded batches. New code dual-writes both old and new | Both |
| **Migrate** | Switch reads to the new column. Add the constraint once the data is clean | New code reads new; old code still reads old |
| **Contract** | Drop the old column — a **later, separate deploy**, after nothing deployed references it | New code only |

**A rename is expand + contract.** Add the new column → dual-write → backfill → switch reads → verify no reader remains → drop the old column. A bare `ALTER TABLE ... RENAME COLUMN` while old code selects the old name is an outage, and the rollback is worse than the deploy.

**A type change is expand + contract.** Add a new column of the new type, backfill with the conversion, dual-write, switch, drop. An in-place `ALTER COLUMN TYPE` rewrites the table under a lock on most engines.

**Splitting or merging columns is expand + contract**, with the conversion in the backfill and the dual-write covering both directions if either code version can write.

---

## 2. Which statements take which lock

The classes below are the ones you have to know before writing an `ALTER`. Verify the exact lock mode for your engine and version — this table is the shape of the problem, and the engine module has the specifics.

### PostgreSQL

| Statement | Lock | Safe on a large table? |
|---|---|---|
| `ADD COLUMN` nullable, no default | `ACCESS EXCLUSIVE`, but metadata-only — brief | Yes, if it does not queue behind a long query |
| `ADD COLUMN ... DEFAULT <constant>` | Metadata-only since PG 11 | Yes |
| `ADD COLUMN ... DEFAULT <volatile expression>` | Full table rewrite under `ACCESS EXCLUSIVE` | **No** — add nullable, backfill, then set the default |
| `ADD COLUMN ... NOT NULL` with no default | Fails, or rewrites; also breaks old code that inserts without the column | **No** — expand/contract |
| `SET NOT NULL` | Full scan under `ACCESS EXCLUSIVE` | **No** — PG 12+: add a validated `CHECK (col IS NOT NULL)` first, then `SET NOT NULL` is cheap. PG 18+ stores `NOT NULL` in `pg_constraint` and lets `ALTER TABLE` set its `NOT VALID` attribute, so the same two-step (add unvalidated, then `VALIDATE CONSTRAINT`) works directly — check the 18 syntax in the engine module |
| `ALTER COLUMN TYPE` | Full rewrite under `ACCESS EXCLUSIVE` | **No** — new column + backfill + swap |
| `CREATE INDEX` | `SHARE` — blocks writes for the whole build | **No** — `CREATE INDEX CONCURRENTLY`, which cannot run inside a transaction block |
| `DROP INDEX` | `ACCESS EXCLUSIVE` | Use `DROP INDEX CONCURRENTLY` |
| `ADD CONSTRAINT CHECK` / `FOREIGN KEY` | Validates every row under lock | **No** — `ADD CONSTRAINT ... NOT VALID`, then `VALIDATE CONSTRAINT` (takes only `SHARE UPDATE EXCLUSIVE`) |
| `DROP COLUMN` | `ACCESS EXCLUSIVE`, metadata-only — brief | Yes, but only in the contract phase |
| `ADD CONSTRAINT ... NOT ENFORCED` (PG 18+) | Metadata-only | Yes — records intent without validating; useful when the application already guarantees it and you plan to enforce later |

**The lock queue is the real hazard.** Even a metadata-only `ALTER` needs the lock, and while it waits, every subsequent query on that table queues behind it. One long-running `SELECT` turns a millisecond `ADD COLUMN` into a full stall. Always set a short `lock_timeout` (e.g. `SET lock_timeout = '3s'`) and retry, rather than letting the statement wait.

### MySQL / MariaDB (InnoDB)

| Statement class | Algorithm | Notes |
|---|---|---|
| Add/drop column, change default, extend `ENUM`/`SET` members | `INSTANT` (default since 8.0.12; arbitrary column position since 8.0.29) | Metadata-only. There is a cap on accumulated instant changes per table — `Maximum row versions reached` forces a rebuild, so track it |
| Add/drop secondary index | `INPLACE, LOCK=NONE` | No copy, but resource-greedy: uses as much CPU and I/O as it can, and needs temp space |
| Add or change the primary key | `INPLACE` with rebuild | Full rebuild; needs disk equal to the table |
| Drop the primary key with no replacement, some type changes | `COPY` | Blocks writes — use a tool |

**Always name the algorithm explicitly** (`ALTER TABLE t ADD COLUMN c INT, ALGORITHM=INSTANT`). If the engine cannot honour it, the statement errors instead of silently falling back to a table copy. And as on Postgres: the metadata lock still queues behind in-flight queries, so "instant" is not "invisible".

### Online schema change tools

Reach for one only when the native path is a rebuild or a copy.

| Tool | Engine | Mechanism | Use when |
|---|---|---|---|
| `gh-ost` | MySQL | Triggerless; reads the binary log to replay changes onto a shadow table | High-write tables; you want true pause/throttle with zero added load while paused. Not compatible with Galera/PXC |
| `pt-online-schema-change` | MySQL | Triggers mirror writes to a shadow table | Galera/PXC clusters; simpler chunked copying. Costs trigger overhead and deadlock risk on hot tables |
| `pgroll` | PostgreSQL 14+ | Keeps two schema versions live as views over one physical table, with a backfill and dual-write triggers between old and new columns | You want old and new application versions to each see the schema they expect during a rollout, and instant rollback. Still pre-1.0 as of August 2026 — review the generated migration |

---

## 3. Backfill

```sql
-- WRONG: one statement, one enormous transaction, one long lock, no resume point
UPDATE orders SET status = 'PENDING' WHERE status IS NULL;

-- RIGHT: bounded batches, committed between, resumable, throttled
-- loop until zero rows affected:
UPDATE orders SET status = 'PENDING'
WHERE id IN (SELECT id FROM orders WHERE status IS NULL ORDER BY id LIMIT 5000);
```

- Batch size is chosen so one batch finishes well inside your statement timeout. Start small and measure.
- Sleep between batches on a busy system; a backfill that finishes in four hours without hurting anyone beats one that finishes in twenty minutes and pages someone.
- For very large tables, run the backfill as a **separate resumable job**, not inside the schema migration. A migration that runs for two hours blocks your deploy pipeline and cannot be safely interrupted.
- Record progress (a watermark row or a `WHERE id > :last`) so a restart resumes instead of rescanning.
- Backfill on a replica does not help — it must run on the primary and replicate.

---

## 4. Compatibility with the currently deployed application version

Check every statement against all four cells before it ships:

| | Old code | New code |
|---|---|---|
| **Old schema** | Currently in production | Must not run — new code must not require the new schema before it exists, or the first pod to restart crashes |
| **New schema** | **The rollout window.** Does old code break? Does it `INSERT` without the new `NOT NULL` column? Does it `SELECT *` on a dropped column? Does it read the renamed one? | The target state |

**Rules that follow:**

- Additive and nullable is rolling-safe. Destructive and tightening is not — defer it to the contract phase.
- New code must tolerate the old schema for the length of one deploy, or you need an ordered two-step release.
- `SELECT *` in old code turns any column drop into an error. If you cannot audit for it, treat every drop as contract-phase-only.
- Adding a `NOT NULL` column with no default breaks old code's `INSERT` statements immediately — that is a production error, not a migration error, and it happens on the first write after the migration commits.
- A new unique rule has **no** `NOT VALID` → `VALIDATE` path on PostgreSQL; `NOT VALID` covers `CHECK` and `FOREIGN KEY` only (plus `NOT NULL` on PG 18+). Duplicates are detected while the unique index is being built, so the build itself fails. Stop the duplicate at the source, deduplicate, then build.

## 5. Rollback stance

- **Roll forward by default.** For destructive changes an honest `down()` cannot restore dropped data — say so in the migration rather than shipping a `downgrade` that pretends otherwise.
- A down-migration that discards data written under the new schema is worse than no down-migration. Make it a no-op with a comment explaining the recovery path (restore from backup + replay).
- The genuinely reversible migrations are the expand-phase ones. That is a reason to keep expand and contract in separate deploys, not just a consequence of it.

## 6. Pre-flight checklist

- [ ] Row count and table size of every table touched, stated in the plan, not guessed.
- [ ] Lock class named per statement, with a comment in the migration file.
- [ ] `lock_timeout` (or equivalent) set, with a retry strategy for the queued case.
- [ ] Backfill is batched, resumable, and outside the schema migration if it is large.
- [ ] Every constraint that *supports* it is added unvalidated first, then validated separately — on PostgreSQL that is `CHECK` and `FOREIGN KEY`, plus `NOT NULL` on 18+. `UNIQUE`, `PRIMARY KEY` and `EXCLUDE` have no such path: build the index concurrently instead.
- [ ] Indexes on populated tables are built concurrently / with `LOCK=NONE`.
- [ ] Old-code compatibility checked against the four-cell table above.
- [ ] Every constraint is explicitly named, so a later drop can reference it.
- [ ] Rehearsed against a restored copy of production, timed, not just against an empty dev database.
- [ ] Rollback path written down — including "roll forward only" where that is the honest answer.
