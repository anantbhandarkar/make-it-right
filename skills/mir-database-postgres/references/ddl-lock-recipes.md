# PostgreSQL DDL lock classes and safe migration recipes

Read at **Gate 6**, before writing any `ALTER TABLE`. Every recipe assumes a populated table and a rolling deploy in which the currently deployed application version keeps running against the new schema.

**Version state, verified 13 August 2026 against postgresql.org.**

Current stable **18** (18.4); 17, 16, 15 and 14 supported; 14 EOL 12 November 2026; 19 at Beta 2 — do not design against it. Full release table in `../SKILL.md`.

Behaviour marked ***PG 18+*** does not exist on 17 and below. Behaviour marked ***all supported*** is true on 14 through 18. Nothing here is verified against 13 or older, which are out of support.

---

## 0. Establish the version before you trust any of this

Three of the recipes below change shape between 17 and 18. Run this first and put the answer in the migration plan:

```sql
SHOW server_version;          -- e.g. 18.4
SELECT current_setting('server_version_num')::int >= 180000 AS pg18_or_later;
```

PG 18-only syntax does not degrade gracefully on an older major — `ADD CONSTRAINT … NOT NULL c NOT VALID` and `GENERATED … VIRTUAL` are **syntax errors** on 17 and below, not slow paths. Version-gate every statement whose shape changes, and write the 14–17 path out separately.

---

## 1. Lock class per statement

Table-level lock modes, weakest to strongest, with the commands that take each.

| Lock | Blocks | Taken by |
|---|---|---|
| ACCESS SHARE | only ACCESS EXCLUSIVE | `SELECT` |
| ROW SHARE | EXCLUSIVE, ACCESS EXCLUSIVE | `SELECT … FOR UPDATE / FOR NO KEY UPDATE / FOR SHARE / FOR KEY SHARE` |
| ROW EXCLUSIVE | SHARE and above | `INSERT`, `UPDATE`, `DELETE`, `MERGE` |
| SHARE UPDATE EXCLUSIVE | itself and above | `VACUUM` (not `FULL`), `ANALYZE`, `CREATE INDEX CONCURRENTLY`, `REINDEX … CONCURRENTLY`, `CREATE STATISTICS`, `COMMENT ON`, and the `ALTER TABLE` forms: `VALIDATE CONSTRAINT`, `SET STATISTICS`, `SET (…)`/`RESET (…)`, `CLUSTER ON`/`SET WITHOUT CLUSTER`, `ATTACH PARTITION`, `DETACH PARTITION CONCURRENTLY` (first phase) |
| SHARE | writes | `CREATE INDEX` (non-concurrent) |
| SHARE ROW EXCLUSIVE | writes and itself | `ALTER TABLE … ADD FOREIGN KEY` (on **both** tables), `CREATE TRIGGER`, `ALTER TABLE … ENABLE/DISABLE TRIGGER`, `ENABLE/DISABLE RULE` |
| EXCLUSIVE | everything except `SELECT` | `REFRESH MATERIALIZED VIEW CONCURRENTLY` |
| **ACCESS EXCLUSIVE** | **everything, including `SELECT`** | **every `ALTER TABLE` form not listed above**, `DROP TABLE`, `TRUNCATE`, `REINDEX` (plain), `CLUSTER`, `VACUUM FULL`, `DROP INDEX`, `REFRESH MATERIALIZED VIEW` (plain), the partition side of `ATTACH PARTITION`, the **second** phase of `DETACH PARTITION CONCURRENTLY` (on the partition being detached), and `LOCK TABLE` with no mode given |

The documented rule is that most `ALTER TABLE` forms take ACCESS EXCLUSIVE and the exceptions are named explicitly. Treat any subform you have not checked as ACCESS EXCLUSIVE.

---

## 2. The lock queue is the outage, not the statement

A lock request that cannot be granted waits. **While it waits, every later request for that table that conflicts with the waiting request also waits** — including plain `SELECT`, which would not have conflicted with the current holder.

The sequence that takes a site down:

1. A reporting query holds ACCESS SHARE on `orders` for 90 seconds.
2. `ALTER TABLE orders ADD COLUMN note text` asks for ACCESS EXCLUSIVE. It cannot be granted, so it waits.
3. Every `SELECT` on `orders` arriving after step 2 conflicts with the *waiting* ACCESS EXCLUSIVE request and waits behind it.
4. The `ALTER` itself runs in 2 ms once it is granted. The outage was 90 seconds of stalled reads, plus however long the backlog takes to drain.

This is why the staging timing tells you nothing. Staging has no 90-second reporting query. The statement duration and the outage duration are different numbers, and only the second one matters.

Find the blocker before you retry:

```sql
-- who is blocking a given pid
SELECT a.pid, a.state, now() - a.xact_start AS tx_age, left(a.query, 120) AS query
FROM pg_stat_activity a
WHERE a.pid = ANY (pg_blocking_pids(<blocked_pid>));

-- everything currently waiting on a lock
SELECT l.pid, l.mode, l.relation::regclass, now() - a.xact_start AS tx_age, left(a.query, 120)
FROM pg_locks l JOIN pg_stat_activity a USING (pid)
WHERE NOT l.granted;
```

Turn on `log_lock_waits` (default off through 18; default on in the 19 beta). It logs the blocking query, which is the only thing that makes this debuggable after the fact.

---

## 3. The migration wrapper — every statement, no exceptions

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';        -- abort if we WAIT for the lock longer than 3s
SET LOCAL statement_timeout = '30s';  -- abort if the statement RUNS longer than 30s
ALTER TABLE orders ADD COLUMN note text;   -- exactly one DDL statement
COMMIT;
```

Rules:

- `lock_timeout` bounds the **wait**. `statement_timeout` bounds the **run**. Different failures. Set both.
- `lock_timeout` applies per lock acquisition, not cumulatively across the transaction.
- Use `SET LOCAL`, not `SET`. Under a transaction pooler a plain `SET` leaks into whichever request gets the connection next.
- **One DDL statement per transaction.** A transaction holds every lock it has taken until commit, so batching five `ALTER TABLE`s multiplies the blocked window by five.
- Give the migration role `idle_in_transaction_session_timeout`, and on ***PG 17+*** also `transaction_timeout`. A half-finished migration holding ACCESS EXCLUSIVE overnight is a real incident shape.

### The retry wrapper

A `lock_timeout` abort raises SQLSTATE **`55P03` (`lock_not_available`)**. That is the retryable case, and the retry is the whole transaction.

```python
import random, time, psycopg

RETRYABLE = {"55P03"}          # lock_not_available (lock_timeout fired)
                               # add "40001"/"40P01" only if the migration also does DML

def run_ddl(dsn: str, stmt: str, attempts: int = 10) -> None:
    for attempt in range(attempts):
        try:
            with psycopg.connect(dsn) as conn, conn.transaction():
                with conn.cursor() as cur:
                    cur.execute("SET LOCAL lock_timeout = '3s'")
                    cur.execute("SET LOCAL statement_timeout = '30s'")
                    cur.execute(stmt)
            return
        except psycopg.errors.Error as exc:
            if exc.sqlstate not in RETRYABLE or attempt == attempts - 1:
                raise
            # exponential backoff with full jitter, capped
            time.sleep(random.uniform(0, min(30.0, 0.5 * 2 ** attempt)))
```

- **Do not raise `lock_timeout` when this keeps failing.** A statement that times out ten times is telling you there is a long-running transaction to find and kill. Raising the timeout converts a fast failure into the outage described in §2.
- Cap the attempts. An unbounded retry loop against a permanently held lock is the same outage with more log lines.
- Jitter matters when several migrations run at once against the same table.

### Statements that cannot use this wrapper

These **cannot run inside a transaction block**. Run each with autocommit and `statement_timeout = 0`; a timeout mid-build aborts the build and leaves an invalid index behind (§9).

`CREATE INDEX CONCURRENTLY` · `DROP INDEX CONCURRENTLY` · `REINDEX … CONCURRENTLY` · `VACUUM` · `ALTER TABLE … DETACH PARTITION CONCURRENTLY`

Every migration tool has an escape hatch for this. Find yours before you need it:

| Tool | Escape hatch |
|---|---|
| Alembic | `with op.get_context().autocommit_block():` |
| Rails | `disable_ddl_transaction!` |
| Django | `atomic = False` on the `Migration` class, plus `AddIndexConcurrently` |
| Flyway | `executeInTransaction = false` |
| golang-migrate, dbmate, goose | a per-file "no transaction" directive |

If the tool wraps `CREATE INDEX CONCURRENTLY` in a transaction, Postgres errors. That is the good case. The bad case is a tool or a code review that quietly drops the `CONCURRENTLY` keyword to make the error go away.

---

## 4. Operation index

Look up the operation, then read its section. "Rewrites" means the whole table and all its indexes are rewritten, which needs up to double the table's disk space and holds the lock for the duration.

| Operation | Lock | Rewrites? | Safe as one step on a populated table? | Section |
|---|---|---|---|---|
| `ADD COLUMN` nullable, no default | ACCESS EXCLUSIVE, catalog-only | No | Yes, with the wrapper | §5 |
| `ADD COLUMN … DEFAULT <non-volatile>` | ACCESS EXCLUSIVE, catalog-only | No (*all supported*) | Yes, with the wrapper | §6 |
| `ADD COLUMN … DEFAULT <volatile>` | ACCESS EXCLUSIVE | **Yes** | **No** — add nullable, backfill, set default | §6 |
| `ADD COLUMN … GENERATED … STORED`, identity, constrained domain | ACCESS EXCLUSIVE | **Yes** | **No** | §6 |
| `ADD COLUMN … GENERATED … VIRTUAL` (***PG 18+***, and the 18 default) | ACCESS EXCLUSIVE, catalog-only | No | Yes | §6 |
| `ALTER COLUMN … SET NOT NULL`, done directly | ACCESS EXCLUSIVE, full scan | No, but scans | **No** | §7 |
| `ALTER COLUMN … TYPE` | ACCESS EXCLUSIVE | **Usually yes** | **No** — expand/contract | §8 |
| `CREATE INDEX` | SHARE (blocks writes for the whole build) | n/a | **No** — use `CONCURRENTLY` | §9 |
| `CREATE INDEX CONCURRENTLY` | SHARE UPDATE EXCLUSIVE | n/a | Yes, with cleanup written in | §9 |
| `DROP INDEX` | ACCESS EXCLUSIVE | n/a | Use `DROP INDEX CONCURRENTLY` | §9 |
| `ADD FOREIGN KEY` | SHARE ROW EXCLUSIVE on **both** tables, full scan of the child | No, but scans | **No** — `NOT VALID` then `VALIDATE` | §10 |
| `ADD CONSTRAINT … UNIQUE` directly | ACCESS EXCLUSIVE, builds the index under it | No, but blocks | **No** — build the index concurrently, then adopt it | §11 |
| `ADD CONSTRAINT … CHECK` | ACCESS EXCLUSIVE, full scan | No, but scans | **No** — `NOT VALID` then `VALIDATE` | §10 |
| `VALIDATE CONSTRAINT` | SHARE UPDATE EXCLUSIVE, full scan | No | Yes — reads and writes continue | §10 |
| `DROP COLUMN` | ACCESS EXCLUSIVE, catalog-only | No (space reclaimed at the next rewrite) | Lock is fine; the deployed code is the risk | §12 |
| `RENAME COLUMN` / `RENAME TABLE` | ACCESS EXCLUSIVE, catalog-only | No | **No** — the lock is fine, the deployed code breaks instantly | §13 |
| `SET DEFAULT` / `DROP DEFAULT` | ACCESS EXCLUSIVE, catalog-only | No | Yes | §6 |
| `ADD CONSTRAINT … NOT ENFORCED` (***PG 18+***) | ACCESS EXCLUSIVE, catalog-only | No | Yes — records intent, validates nothing | §10 |

---

## 5. Add a column

**Lock:** ACCESS EXCLUSIVE, catalog-only. **Rewrite:** no.

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';
ALTER TABLE orders ADD COLUMN currency text;
COMMIT;
```

One step is correct here. The only hazard is the queue in §2, which the wrapper handles.

`ADD COLUMN c int NOT NULL` with no default **fails outright** on a populated table. Even where it would succeed, it breaks the currently deployed code the moment it commits, because old `INSERT` statements omit the column. Use §7.

---

## 6. Add a column with a default

**Lock:** ACCESS EXCLUSIVE. **Rewrite:** depends entirely on volatility.

***All supported versions (14–18):*** a non-volatile `DEFAULT` is evaluated once, stored in the table's metadata, and returned for pre-existing rows. No rewrite. The stored value is only written into rows at the next rewrite.

| What you write | Volatility | Rewrite |
|---|---|---|
| `DEFAULT 'USD'`, `DEFAULT 0`, `DEFAULT now()`, `DEFAULT current_timestamp` | immutable / stable | **No** — catalog-only, safe at any table size |
| `DEFAULT gen_random_uuid()`, `DEFAULT clock_timestamp()`, `DEFAULT random()` | volatile | **Yes** — whole table and every index, under ACCESS EXCLUSIVE |
| `GENERATED ALWAYS AS (…) STORED`, `GENERATED … AS IDENTITY`, a domain type carrying constraints | — | **Yes** |
| `GENERATED ALWAYS AS (…) VIRTUAL` (***PG 18+***) | — | **No**, never |

`now()` is stable — it returns the transaction start time, so it is fixed for the statement and does not rewrite. `clock_timestamp()` is volatile and does. This distinction is the whole difference between a 5 ms migration and a 40-minute outage, and it is invisible in the DDL text.

Safe sequence for a volatile default:

```sql
-- M1: add nullable, no default. Catalog-only.
ALTER TABLE orders ADD COLUMN request_id uuid;

-- Deploy: new code sets request_id on every insert. Old code is still running and leaves it NULL.

-- M2: batched backfill (§14), NOT one UPDATE.

-- M3: attach the default for future inserts. Catalog-only, no rewrite.
ALTER TABLE orders ALTER COLUMN request_id SET DEFAULT gen_random_uuid();

-- M4: constrain, once the backfill is complete (§7).
```

***PG 18+*** made `VIRTUAL` the default for generated columns. On 18, write `STORED` explicitly whenever you intend to index the column — a `VIRTUAL` generated column is computed on read and **cannot be indexed**. On 17 and below `VIRTUAL` does not exist and every generated column is `STORED`, so adding one always rewrites.

---

## 7. Add NOT NULL

**Lock if done directly:** ACCESS EXCLUSIVE held for a full table scan. **Rewrite:** no, but the scan is the problem.

The documented escape: `SET NOT NULL` skips the scan when a **valid** `CHECK` constraint already proves the column cannot be NULL.

***All supported versions (14–18):***

```sql
-- 1. Add the proof, unvalidated. Brief ACCESS EXCLUSIVE, no scan.
ALTER TABLE orders ADD CONSTRAINT orders_currency_nn CHECK (currency IS NOT NULL) NOT VALID;

-- 2. Validate it. SHARE UPDATE EXCLUSIVE — scans, but reads AND writes continue.
ALTER TABLE orders VALIDATE CONSTRAINT orders_currency_nn;

-- 3. Now the real thing. Brief ACCESS EXCLUSIVE, scan skipped because the CHECK proves it.
ALTER TABLE orders ALTER COLUMN currency SET NOT NULL;

-- 4. Optional: drop the now-redundant CHECK. Catalog-only.
ALTER TABLE orders DROP CONSTRAINT orders_currency_nn;
```

***PG 18+*** stores `NOT NULL` in `pg_constraint`, so the constraint can be named and added `NOT VALID` directly. Two statements instead of four:

```sql
ALTER TABLE orders ADD CONSTRAINT orders_currency_nn NOT NULL currency NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_currency_nn;
```

Facts that govern both paths:

- A `NOT VALID` constraint is **enforced immediately for inserts and updates**. Only pre-existing rows go unchecked. This is what makes the two-step safe: nothing new can violate it while you validate.
- Until it is validated, the planner will not rely on it and the column cannot be part of a primary key.
- `NOT VALID` is available only on `ADD CONSTRAINT`. There is no `ALTER CONSTRAINT … SET NOT VALID` — `ALTER CONSTRAINT` currently alters foreign keys only.
- Run this **after** the backfill and after the deploy that stops writing NULLs, or `VALIDATE` fails on rows the old code is still producing.

---

## 8. Change a column type

**Lock:** ACCESS EXCLUSIVE. **Rewrite:** normally the whole table *and every index*, needing up to double the table's disk space.

No rewrite happens only when the `USING` clause does not change the column contents **and** the old type is binary-coercible to the new one (or an unconstrained domain over it). Even then, indexes are still rebuilt unless the system can prove the new index is logically equivalent to the old one. `text` ↔ `varchar` sort identically, so their indexes survive; a collation change forces a rebuild.

| Change | Rewrite? |
|---|---|
| `varchar(50)` → `varchar(100)` (widening) | No |
| `varchar(100)` → `varchar(50)` (narrowing) | Yes, and it can fail on data |
| `varchar(n)` → `text` | No |
| `text` → `varchar(n)` | Yes |
| `int` → `bigint` | **Yes** |
| any change to the collation | Yes (index rebuild at minimum) |

**Design note that avoids this entirely:** use `text` plus a named `CHECK` for length instead of `varchar(n)`. Widening then becomes a `NOT VALID` CHECK swap (§10), not a rewrite.

Expand/contract for `int` → `bigint` on a large table:

```sql
-- M1: add the new column and keep it in sync. Catalog-only + SHARE ROW EXCLUSIVE for the trigger.
ALTER TABLE events ADD COLUMN id_new bigint;

CREATE OR REPLACE FUNCTION events_sync_id() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.id_new := NEW.id;
  RETURN NEW;
END $$;

CREATE TRIGGER events_sync_id BEFORE INSERT OR UPDATE ON events
  FOR EACH ROW EXECUTE FUNCTION events_sync_id();
-- One-directional on purpose: `id` stays the value source until the swap.
-- It therefore OVERWRITES any write to id_new. Drop it in the SAME deploy that
-- switches the application to writing id_new, or use the two-way form in §13.

-- M2: batched backfill of id_new where it is NULL (§14).

-- M3: build the replacement index and constraint without blocking.
CREATE UNIQUE INDEX CONCURRENTLY events_pkey_new ON events (id_new);
ALTER TABLE events ADD CONSTRAINT events_id_new_nn CHECK (id_new IS NOT NULL) NOT VALID;
ALTER TABLE events VALIDATE CONSTRAINT events_id_new_nn;
ALTER TABLE events ALTER COLUMN id_new SET NOT NULL;

-- M4: swap. One short ACCESS EXCLUSIVE, catalog-only — no scan, no rewrite.
BEGIN;
SET LOCAL lock_timeout = '3s';
ALTER TABLE events DROP CONSTRAINT events_pkey;
ALTER TABLE events ADD CONSTRAINT events_pkey PRIMARY KEY USING INDEX events_pkey_new;
COMMIT;

-- Deploy: code reads and writes id_new only. Drop the trigger.
-- M5, a LATER RELEASE: ALTER TABLE events DROP COLUMN id;   -- see §12
```

Two things govern M4:

- **`ADD CONSTRAINT … PRIMARY KEY USING INDEX` does not *require* `NOT NULL` — it silently imposes it.** Documented: *"If `PRIMARY KEY` is specified, and the index's columns are not already marked `NOT NULL`, then this command will attempt to do `ALTER COLUMN SET NOT NULL` against each such column. That requires a full table scan."* That scan runs under ACCESS EXCLUSIVE — exactly the outage this recipe exists to avoid. M3 marks the column `NOT NULL` first so M4 stays catalog-only.
- **`DROP CONSTRAINT events_pkey` fails if any foreign key references it.** Inventory incoming FKs first (`pg_constraint` where `confrelid = 'events'::regclass`); each one has to be dropped and re-added against the new key, and that re-add is itself a `NOT VALID` → `VALIDATE` pair (§10). Check for publications, identity/sequence ownership on `id`, and views before you schedule the swap.

---

## 9. Add an index

**Lock:** plain `CREATE INDEX` takes SHARE, which blocks all writes for the entire build. `CREATE INDEX CONCURRENTLY` takes SHARE UPDATE EXCLUSIVE and blocks neither reads nor writes.

`CONCURRENTLY` costs **two full table scans** and waits for all existing transactions that could use the index to finish, before each scan. It is slower in wall-clock terms and correct in availability terms.

```sql
-- autocommit, statement_timeout = 0
CREATE INDEX CONCURRENTLY idx_orders_customer_created ON orders (customer_id, created_at DESC);
```

### The cleanup path is part of the migration

A failed concurrent build — deadlock, unique violation, `statement_timeout`, a cancelled deploy — **leaves an invalid index behind**. The documented consequences:

- The planner ignores it, so you get none of the benefit.
- Every write still maintains it, so you pay all of the cost.
- **A failed *unique* index continues to enforce its uniqueness constraint.** Documented: uniqueness starts being enforced against other transactions when the *second* scan begins, and *"if a failure does occur in the second scan, the 'invalid' index continues to enforce its uniqueness constraint afterwards."* A migration that "failed" can start rejecting legitimate inserts. This is the one that turns a bad deploy into a customer-visible error. Do not assume the reverse either — never leave an invalid index in place on the theory that it is inert.

Check for leftovers at the **start** of every migration, not after a failure:

```sql
SELECT c.relname AS invalid_index, t.relname AS table_name
FROM pg_index i
JOIN pg_class c ON c.oid = i.indexrelid
JOIN pg_class t ON t.oid = i.indrelid
WHERE NOT i.indisvalid;
```

Recovery, in the documented order of preference:

```sql
DROP INDEX CONCURRENTLY idx_orders_customer_created;   -- then re-run the CREATE
-- or, to rebuild in place:
REINDEX INDEX CONCURRENTLY idx_orders_customer_created;
```

### Other index facts that change the plan

- **Only one concurrent index build may run on a table at a time.** Documented: *"Regular index builds permit other regular index builds on the same table to occur simultaneously, but only one concurrent index build can occur on a table at a time."* Two migrations adding indexes to the same hot table serialize, and the second can sit for hours. Build them in one ordered step, and watch `pg_stat_progress_create_index` rather than cancelling — a cancelled build is an invalid index.
- `DROP INDEX` takes ACCESS EXCLUSIVE, so prefer `DROP INDEX CONCURRENTLY` — but it does not apply everywhere. Documented caveats: one index name only, **no `CASCADE`** (so *an index backing a `UNIQUE` or `PRIMARY KEY` constraint cannot be dropped this way* — drop the constraint instead, which takes ACCESS EXCLUSIVE), it cannot run inside a transaction block, and **indexes on partitioned tables cannot be dropped concurrently**.
- **`CREATE INDEX CONCURRENTLY` is not supported on a partitioned parent.** Build concurrently on each partition, then create the parent index non-concurrently — that final step is metadata only and brief.
- `ALTER INDEX … RENAME TO …` is catalog-only and fast.
- `INCLUDE` (covering) columns are supported on B-tree, GiST and SP-GiST only, and are **disregarded for uniqueness and exclusion enforcement**. `CREATE UNIQUE INDEX … (x) INCLUDE (y)` enforces uniqueness on `x` alone.

---

## 10. Add a foreign key (and any CHECK)

**Lock:** `ADD FOREIGN KEY` takes SHARE ROW EXCLUSIVE on **both** the child and the referenced table, and scans the child to validate. Reads continue; writes to both tables stop for the scan.

Two steps. Step 1 does not scan:

```sql
-- 1. SHARE ROW EXCLUSIVE on both tables, brief, no scan.
ALTER TABLE orders ADD CONSTRAINT orders_user_fk
  FOREIGN KEY (user_id) REFERENCES users (id) NOT VALID;

-- 2. SHARE UPDATE EXCLUSIVE, scans the child, reads and writes continue.
ALTER TABLE orders VALIDATE CONSTRAINT orders_user_fk;
```

Same two-step shape for `CHECK`:

```sql
ALTER TABLE orders ADD CONSTRAINT orders_total_nonneg CHECK (total_minor >= 0) NOT VALID;
ALTER TABLE orders VALIDATE CONSTRAINT orders_total_nonneg;
```

Notes:

- The referenced column needs a unique index or the FK creation fails.
- Validate only after the deploy that stops writing violating rows. Otherwise `VALIDATE` fails on rows the currently deployed code is still creating, and you have a half-applied migration.
- A foreign key makes **every child insert take `FOR KEY SHARE` on the parent row.** If anything does `SELECT … FOR UPDATE` on a hot parent, every concurrent child insert serializes behind it. Use `FOR NO KEY UPDATE` unless you are changing a key column. See SKILL.md §3.
- `ON DELETE CASCADE` on a large child table turns one parent delete into an unbounded write under lock. Choose it deliberately, and know the child row count.
- ***PG 18+*** adds `NOT ENFORCED`, which records the constraint in the catalog and never checks it. It is metadata-only and instant. It documents intent; it guarantees nothing. Do not use it as a substitute for `NOT VALID` + `VALIDATE`, which does enforce going forward.

---

## 11. Add a unique constraint

**Lock:** `ALTER TABLE … ADD CONSTRAINT … UNIQUE` builds the index under ACCESS EXCLUSIVE. On a populated table that blocks everything for the length of the build.

Build the index concurrently first, then adopt it. The `ALTER` is then catalog-only with no scan:

```sql
-- 1. autocommit, statement_timeout = 0. Blocks nothing.
CREATE UNIQUE INDEX CONCURRENTLY uq_users_tenant_email ON users (tenant_id, email);

-- 2. Brief ACCESS EXCLUSIVE, catalog-only — the index already exists and is valid.
BEGIN;
SET LOCAL lock_timeout = '3s';
ALTER TABLE users ADD CONSTRAINT uq_users_tenant_email UNIQUE USING INDEX uq_users_tenant_email;
COMMIT;
```

**`USING INDEX` only adopts a plain index.** The documented restriction: *"The index cannot have expression columns nor be a partial index. Also, it must be a b-tree index with default sort ordering."* It is also not supported on partitioned tables. So the two shapes this skill set recommends most — `lower(email)` for case-insensitive uniqueness, and `WHERE deleted_at IS NULL` for soft delete — **cannot become constraints at all**:

```sql
-- Correct, and final. The unique INDEX is the enforcement object; there is no ALTER TABLE step.
CREATE UNIQUE INDEX CONCURRENTLY uq_users_tenant_email_active
  ON users (tenant_id, lower(email)) WHERE deleted_at IS NULL;
```

A unique index enforces uniqueness exactly as a unique constraint does. The only things you give up are the `pg_constraint` entry and the ability to reference it from a foreign key. Do not add an `ALTER TABLE … ADD CONSTRAINT … USING INDEX` line after an expression or partial index — it fails, and the failure lands mid-migration.

- If step 1 fails on a duplicate, you get an invalid index that **still enforces uniqueness** (§9). Clean it up before retrying, or new duplicate inserts start failing while you think nothing is deployed.
- Deduplicate the existing data *and* stop the source of duplicates before step 1. A unique index cannot be added while the currently deployed code is still writing duplicates.
- The constraint takes the index's name. Naming the index deliberately in step 1 is how you get a predictable constraint name to drop later.
- For a partitioned table, a unique constraint **must include the partition key**. There are no global unique indexes.

---

## 12. Drop a column

**Lock:** ACCESS EXCLUSIVE, catalog-only. **Rewrite:** no — Postgres marks the attribute dropped and reclaims the space at the next rewrite.

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';
ALTER TABLE orders DROP COLUMN legacy_status;
COMMIT;
```

The lock is not the risk. The deployed code is:

1. **Deploy the code that stops reading and writing the column.** Verify no deployed instance references it.
2. **Wait until that deploy is complete everywhere,** including any worker fleet, cron host, or analytics job on a separate release cadence.
3. **Drop it in a later release.**

`SELECT *` in old code turns a column drop into an immediate error for every row fetched. If you cannot audit for `SELECT *`, treat every drop as contract-phase-only with no exceptions.

Dropping a column does not shrink the table. If the column was large and you need the space back, that is `pg_repack` or a rewrite (§15), scheduled separately.

---

## 13. Rename a column or table

**Lock:** ACCESS EXCLUSIVE, catalog-only, instant. **Rewrite:** no.

```sql
ALTER TABLE orders RENAME COLUMN amount TO amount_minor;   -- do NOT do this in a rolling deploy
```

The statement is cheap and the deploy is an outage. The instant it commits, every currently deployed instance still issuing `SELECT amount` gets an error. Rolling back the application does not help, because the schema stayed renamed.

A rename is expand/contract:

```sql
-- M1: add the new column. Catalog-only.
ALTER TABLE orders ADD COLUMN amount_minor bigint;

-- M1b: keep both in sync for the length of the rollout, in both directions,
--      because old code writes the old name and new code writes the new one.
CREATE OR REPLACE FUNCTION orders_sync_amount() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    -- whichever column the writer filled, copy it to the other one
    NEW.amount_minor := coalesce(NEW.amount_minor, NEW.amount);
    NEW.amount       := coalesce(NEW.amount, NEW.amount_minor);
  ELSE  -- UPDATE
    IF NEW.amount_minor IS DISTINCT FROM OLD.amount_minor THEN
      NEW.amount := NEW.amount_minor;        -- new code wrote the new column
    ELSIF NEW.amount IS DISTINCT FROM OLD.amount THEN
      NEW.amount_minor := NEW.amount;        -- old code wrote the old column
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER orders_sync_amount BEFORE INSERT OR UPDATE ON orders
  FOR EACH ROW EXECUTE FUNCTION orders_sync_amount();

-- M2: batched backfill (§14).
-- Deploy: new code reads and writes amount_minor.
-- M3: verify no reader of `amount` remains, then drop the trigger.
-- M4, a LATER RELEASE: ALTER TABLE orders DROP COLUMN amount;
```

Two things about that trigger:

- **The `TG_OP` branch is required, not defensive style.** In a PL/pgSQL row trigger `OLD` is unassigned for `INSERT`, and reading `OLD.amount` there raises `record "old" is not assigned yet`. A dual-write trigger written without the branch passes an `UPDATE` test and then rejects every insert.
- `CREATE TRIGGER` takes SHARE ROW EXCLUSIVE — it blocks writes briefly, not reads. Use the wrapper from §3.

Renaming a **table** is the same problem. The alternatives are a new table with trigger-based dual-write, or — cheaper when reads dominate — create a view under the old name so old code keeps working:

```sql
-- BOTH statements in ONE transaction. Committed separately, there is a window in which
-- no relation named `customers` exists and every deployed instance errors.
BEGIN;
SET LOCAL lock_timeout = '3s';
ALTER TABLE customers RENAME TO accounts;
CREATE VIEW customers AS SELECT * FROM accounts;   -- old code keeps reading
COMMIT;
```

This is the one place the one-DDL-per-transaction rule in §3 does not apply: atomicity of the name swap outweighs the shorter lock window.

A simple view over one table is updatable, so old code can write through it too. Drop the view in the contract release. This does not survive `INSERT … RETURNING` on generated columns or anything that inspects `information_schema` for the table type — check before relying on it.

---

## 14. Batched backfill

Never update a large table in one statement. One `UPDATE` over 50 M rows holds a snapshot for the whole run, blocks vacuum cluster-wide for that duration, creates 50 M dead tuples, and can push replication lag past recovery.

```sql
-- One batch per transaction, from a script with autocommit. Repeat until 0 rows affected.
-- :last_id starts at 0 and becomes max(id) from the previous batch.
UPDATE orders SET currency = 'USD'
WHERE id IN (
  SELECT id FROM orders
  WHERE id > :last_id AND currency IS NULL
  ORDER BY id LIMIT 5000
)
RETURNING id;
```

- **Carry a watermark.** `WHERE currency IS NULL` alone restarts the scan from the first row every batch, so the backfill goes quadratic as the head of the table fills in — unless a partial index on `WHERE currency IS NULL` exists. `id > :last_id` bounds each batch regardless. Keyset only; never `OFFSET`.
- `UPDATE` itself takes no `ORDER BY` or `LIMIT` in PostgreSQL. The ordering and the bound go in the subquery, as above.
- Make each batch idempotent by predicate (`WHERE currency IS NULL`) so a crash resumes rather than double-writes.
- Size the batch so one batch finishes well inside `statement_timeout`. Start at a few thousand rows and measure; do not set `statement_timeout = 0` for backfill batches — let the loop handle a slow batch.
- Sleep 50–200 ms between batches and watch `pg_stat_replication`. A backfill that outruns replication is an availability incident on the replicas.
- Every batch makes dead tuples. Either lower `autovacuum_vacuum_scale_factor` on that table for the duration, or run `VACUUM` between batches.

```sql
ALTER TABLE orders SET (autovacuum_vacuum_scale_factor = 0.01);   -- during the backfill
-- ALTER TABLE orders RESET (autovacuum_vacuum_scale_factor);     -- after
```

- Run large backfills as a **separate resumable job**, not inside the schema migration. A migration that runs for two hours blocks the deploy pipeline and cannot be safely interrupted.
- Do not put `COMMIT` inside a `DO $$ … $$` block unless you know it is not running inside an outer transaction. Transaction control is not allowed there.
- Backfilling on a replica does nothing. It must run on the primary and replicate.

---

## 15. Rolling-deploy compatibility

Old and new application code run at the same time for the length of the rollout, and for longer after a rollback. Check every statement against the version that is **already deployed**.

| | Old code | New code |
|---|---|---|
| **Old schema** | in production now | must not require the new schema before it exists |
| **New schema** | **the rollout window — this is the cell that breaks** | the target state |

| Change | Safe order |
|---|---|
| Add a column | add nullable → deploy code that writes it → backfill → constrain |
| Add a `NOT NULL` column | only with a non-volatile default, or old code's inserts fail immediately |
| Drop a column | deploy code that stops using it → **next release** drop it |
| Rename a column or table | never in place (§13) |
| Change a type | expand/contract (§8) |
| Add an index | always `CONCURRENTLY`. Safe in any order relative to code |
| Add a constraint | `NOT VALID` first, `VALIDATE` only after the code that could violate it is gone |
| Drop a constraint | catalog-only and safe, but old code may depend on the error it produced |
| Add an enum value | `ALTER TYPE … ADD VALUE` commits before the value is usable — you cannot add it and backfill rows to it in the same transaction |

**Rollback stance.** Expand-phase migrations are reversible; contract-phase ones are not. That is a reason to keep them in separate deploys, not just a consequence of it. For a destructive change, write "roll forward only" plus the recovery path in the migration file, rather than shipping a `down()` that pretends to restore dropped data.

---

## 16. Online rewrite tooling

Reach for these only when the native path is a rewrite you cannot take.

| Tool | What it does | Cost |
|---|---|---|
| **pg_repack** | Removes table and index bloat online, without the ACCESS EXCLUSIVE of `VACUUM FULL`/`CLUSTER` | Needs a primary key or unique index and roughly double the disk. Takes a brief ACCESS EXCLUSIVE at the final swap |
| **pg_squeeze** | Same job via logical decoding, driven by a background worker on a schedule rather than a manual run | Logical decoding overhead; needs `wal_level = logical` |
| **pgroll** | Expand/contract as a product. Each migration version gets a schema of **views** over the physical tables, so two application versions each see the schema they expect. Breaking changes get a new column plus triggers propagating writes both ways, with a batched backfill | A view indirection on every query, and a second thing to operate |
| **`REPACK` / `REPACK CONCURRENTLY`** | Unifies `VACUUM FULL` and `CLUSTER` in core with a concurrent rebuild | **PostgreSQL 19 only, and 19 was at Beta 2 on 2026-07-16 with no GA date. Not a production path today** |

Extension version numbers were not re-verified in this pass — check each project's releases page before pinning one in a Dockerfile.

None of these replace the recipes above. They handle **rewrites**. The ordering, the deploy sequencing in §15, and the four-cell compatibility check are still yours.
