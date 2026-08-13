---
name: mir-database-postgres
description: "Make It Right (PostgreSQL module). Postgres mechanics the engine-agnostic pillar omits: which ALTER TABLE subforms take ACCESS EXCLUSIVE and the lock queue where a blocked DDL stalls every read behind it; lock_timeout/statement_timeout migration discipline; NOT VALID then VALIDATE; CREATE INDEX CONCURRENTLY and invalid-index cleanup; MVCC bloat, autovacuum, XID wraparound; isolation levels and 40001/40P01 retry; SELECT FOR UPDATE vs FOR NO KEY UPDATE, SKIP LOCKED queues, advisory locks; b-tree/GIN/GiST/BRIN choice, partial and covering indexes, why an index is unused; PgBouncer pooling modes and transaction-pooling breakage; RLS multi-tenancy and its bypass paths. Chains: mir-database (the gates) → this. TRIGGER when the engine is PostgreSQL or Postgres-compatible (RDS/Aurora, Cloud SQL, Neon, Supabase) and the task writes DDL, a migration, an index, a locking or isolation decision, a partitioning or RLS layout, or diagnoses a slow query, EXPLAIN plan, missing index, bloat, vacuum, deadlock, or connection pooler. SKIP for MySQL, SQL Server, Oracle, SQLite, MongoDB (mir-database-mongo), DynamoDB and every other engine; for application-side transaction/idempotency/retry code and ORM or migration-tool wiring (Alembic, Prisma — mir-backend and its framework modules); and for analytics/warehouse modeling (mir-data)."
trigger: /mir-database-postgres
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-database-postgres · Make It Right (PostgreSQL)

Bottom tier of a two-tier chain: `mir-database` decides **what is true of the data** (any engine) → **this** carries Postgres mechanics. Run the gates first. Reach for this at Gate 5 (design decisions that depend on lock behaviour and index types), Gate 6 (DDL), and Gate 7 (review).

**Engine state, verified 13 August 2026.**

| Release | Status | What matters |
|---|---|---|
| **18** (18.4, released 2026-05-14) | current stable | Async I/O (`io_method`); b-tree **skip scan**; `uuidv7()`; generated columns default to **VIRTUAL**; `NOT NULL` stored in `pg_constraint` so it can be named and marked `NOT VALID`; `WITHOUT OVERLAPS`/`PERIOD` temporal constraints; `NOT ENFORCED` CHECK/FK; `OLD`/`NEW` in `RETURNING`; data checksums on by default; `pg_upgrade` keeps planner statistics; `VACUUM`/`ANALYZE` now recurse into partitions unless you write `ONLY`; `idle_replication_slot_timeout` |
| 17.10 · 16.14 · 15.18 · 14.23 (all 2026-05-14) | supported | 14 reaches **end of life 12 Nov 2026** — plan the upgrade now |
| 13 and older | end of life | no security patches |
| **19** | **Beta 2** (2026-07-16), no GA date confirmed at time of writing | `REPACK` / `REPACK CONCURRENTLY` in core, `MERGE`/`SPLIT PARTITIONS`, parallel autovacuum, `pg_stat_lock`, `log_lock_waits` on by default, JIT off by default, logical sequence sync. Do not design against a beta |

Run the current minor. The May 2026 batch (CVE-2026-6472 … CVE-2026-6638) and the February 2026 batch (CVE-2026-2003 … 2007) are only fixed there. See Security.

---

## 1. Locking and migrations

### The lock queue is the outage, not the DDL

A lock request that cannot be granted **queues, and every later request queues behind it.** `ALTER TABLE t ADD COLUMN …` waiting on one slow `SELECT` blocks every *subsequent* `SELECT` on `t`, because the pending ACCESS EXCLUSIVE request sits in front of them. The statement itself may be instant. The outage is the queue draining after it. This is why "the migration ran in 8 ms in staging" tells you nothing.

| Statement | Lock | What still works |
|---|---|---|
| `ALTER TABLE` — most subforms (`ADD COLUMN`, `DROP COLUMN`, `SET NOT NULL`, `ALTER TYPE`, `SET DEFAULT`, `ADD CONSTRAINT`, `RENAME`) | **ACCESS EXCLUSIVE** | nothing, not even `SELECT` |
| `ADD FOREIGN KEY`, `ENABLE`/`DISABLE TRIGGER` | SHARE ROW EXCLUSIVE | reads; writes blocked |
| `CREATE INDEX` (plain) | SHARE | reads; writes blocked |
| `VALIDATE CONSTRAINT`, `SET STATISTICS`, `SET (…)`, `CREATE INDEX CONCURRENTLY`, `REINDEX … CONCURRENTLY` | SHARE UPDATE EXCLUSIVE | reads **and** writes; blocks other DDL and VACUUM |
| `ATTACH PARTITION` | SHARE UPDATE EXCLUSIVE on parent + ACCESS EXCLUSIVE on the partition | reads/writes on other partitions |
| `VACUUM FULL`, `CLUSTER`, plain `REINDEX`, `DROP INDEX` | ACCESS EXCLUSIVE | nothing |

### Timeout discipline — every migration session, no exceptions

```sql
BEGIN;
SET LOCAL lock_timeout = '3s';        -- abort if we WAIT for a lock longer than 3s
SET LOCAL statement_timeout = '30s';  -- abort if the statement RUNS longer than 30s
ALTER TABLE orders ADD COLUMN note text;
COMMIT;
```

- `lock_timeout` bounds the **wait**; `statement_timeout` bounds the **run**. They are different failures and you need both.
- `lock_timeout` applies per lock acquisition, not cumulatively. When it fires, retry the whole statement with jittered backoff — do **not** raise the timeout. A statement that keeps timing out is telling you there is a long-running transaction to kill first.
- Set `statement_timeout = 0` for `CREATE INDEX CONCURRENTLY` **only** — a timeout mid-build aborts it and leaves an invalid index behind. Backfill batches get the opposite treatment: a bounded `statement_timeout` per batch, sized so one batch finishes inside it. An unbounded backfill statement holds its snapshot, blocks vacuum cluster-wide, and outruns replication with nothing to stop it.
- Use `SET LOCAL`, not `SET`. Under a transaction pooler a plain `SET` leaks into the next request that gets the connection.
- **One DDL statement per transaction.** A transaction holds every ACCESS EXCLUSIVE it has taken until commit, so batching five `ALTER TABLE`s multiplies the blocked window by five.
- Give the migration role `idle_in_transaction_session_timeout`; a half-finished migration holding a lock overnight is a real incident shape.

### ADD COLUMN

| What you write | What happens (PG 11+) |
|---|---|
| `ADD COLUMN c int` | catalog only, fast |
| `ADD COLUMN c int DEFAULT 0`, `DEFAULT now()` — **non-volatile** | catalog only; the default is stored as metadata and returned for old rows. Safe on any size table |
| `ADD COLUMN c uuid DEFAULT gen_random_uuid()`, `DEFAULT clock_timestamp()` — **volatile** | **full table and index rewrite** under ACCESS EXCLUSIVE |
| `ADD COLUMN … GENERATED ALWAYS AS (…) STORED`, an identity column, a domain type with constraints | full rewrite |
| `ADD COLUMN … GENERATED ALWAYS AS (…)` **VIRTUAL** (PG 18 default) | never rewrites |
| `ADD COLUMN c int NOT NULL` with no default | fails outright on a populated table |

`now()` is stable, so it does not rewrite. `gen_random_uuid()`, `clock_timestamp()`, `random()` are volatile, so they do. Backfill volatile values in batches after adding the column nullable.

### Changing a column type

- Normally rewrites the whole table **and every index** under ACCESS EXCLUSIVE. No rewrite only when the `USING` clause does not change contents and the old type is binary-coercible to the new one — and indexes are still rebuilt unless Postgres can prove logical equivalence (`text` ↔ `varchar` sort identically so their indexes survive; a collation change does not).
- `varchar(50)` → `varchar(100)` is free. `varchar(100)` → `varchar(50)` rewrites and can fail on data. **Prefer `text` plus a named `CHECK` for the length** — widening is then a `NOT VALID` CHECK swap, not a rewrite.
- `int` → `bigint` rewrites. On a large table use expand/contract: add a nullable `bigint`, dual-write, backfill in bounded batches, swap, drop. Recipe in `references/ddl-lock-recipes.md`.

### NOT NULL without the full-table scan

`SET NOT NULL` normally scans the whole table under ACCESS EXCLUSIVE. Two ways out, depending on version:

```sql
-- PG 14–17: a valid CHECK proves no NULLs, so SET NOT NULL skips the scan
ALTER TABLE t ADD CONSTRAINT t_c_nn CHECK (c IS NOT NULL) NOT VALID;  -- brief ACCESS EXCLUSIVE, no scan
ALTER TABLE t VALIDATE CONSTRAINT t_c_nn;                             -- SHARE UPDATE EXCLUSIVE, scans, reads+writes continue
ALTER TABLE t ALTER COLUMN c SET NOT NULL;                            -- brief ACCESS EXCLUSIVE, scan skipped

-- PG 18+: the NOT NULL constraint itself can be NOT VALID
ALTER TABLE t ADD CONSTRAINT t_c_nn NOT NULL c NOT VALID;
ALTER TABLE t VALIDATE CONSTRAINT t_c_nn;
```

A `NOT VALID` constraint is **enforced immediately for inserts and updates**; only pre-existing rows go unchecked. Until it is validated the planner cannot rely on it and the column cannot be a primary key. Same two-step shape for foreign keys: `ADD CONSTRAINT … FOREIGN KEY … NOT VALID` (SHARE ROW EXCLUSIVE, no scan) then `VALIDATE CONSTRAINT` (SHARE UPDATE EXCLUSIVE).

### CREATE INDEX CONCURRENTLY and its cleanup path

- Two table scans, waits for existing transactions before each, does not block reads or writes. **Cannot run inside a transaction block** — most migration frameworks need an explicit escape hatch for this, and getting it wrong is why it silently runs non-concurrently.
- On failure (deadlock, unique violation, `statement_timeout`, a cancelled deploy) it **leaves an invalid index**. `\d` shows `INVALID`; `pg_index.indisvalid = false`. That index is ignored by the planner but **still costs on every write**, and a failed *unique* index **still enforces uniqueness** — so a "failed" migration can start rejecting legitimate inserts.
- Cleanup is part of the migration, not an afterthought:

```sql
SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE NOT i.indisvalid;
DROP INDEX CONCURRENTLY idx_orders_customer;   -- then retry the CREATE
-- or: REINDEX INDEX CONCURRENTLY idx_orders_customer;
```

- `DROP INDEX` takes ACCESS EXCLUSIVE; use `DROP INDEX CONCURRENTLY` — which does not accept `CASCADE`, cannot run in a transaction block, and therefore **cannot drop an index backing a `UNIQUE` or `PRIMARY KEY` constraint** (drop the constraint) or any index on a partitioned table.
- **Partitioned tables: `CREATE INDEX CONCURRENTLY` is not supported on the parent.** Build concurrently on each partition, then create the parent index non-concurrently — that last step is metadata only.

Full per-statement recipes (expand/contract, batched backfill, renames, rolling-deploy compatibility): read `references/ddl-lock-recipes.md`.

---

## 2. MVCC, bloat, and vacuum

- Every `UPDATE` writes a new row version and leaves the old one dead; `DELETE` marks dead. `VACUUM` makes that space **reusable**, it does not return it to the OS. Bloat is dead space vacuum never got to.
- Autovacuum fires at `autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor × reltuples`. The default scale factor of 0.2 means a 100 M-row table waits for 20 M dead rows. On large or hot tables set it per table: `ALTER TABLE t SET (autovacuum_vacuum_scale_factor = 0.01, autovacuum_analyze_scale_factor = 0.01);`. PG 18 adds `autovacuum_vacuum_max_threshold`, a fixed dead-tuple cap that does the same job cluster-wide.
- **Transaction ID wraparound.** The XID counter is 32-bit. At 40 M XIDs from wraparound the server warns; at 3 M it **refuses to assign new XIDs** — every write fails with `database is not accepting commands that assign new XIDs`, only read-only transactions start, and the fix is a slow vacuum while you are already down. Alert on it:

```sql
SELECT datname, age(datfrozenxid) FROM pg_database ORDER BY 2 DESC;   -- vs autovacuum_freeze_max_age (default 200M)
```

  Multixact IDs wrap on their own counter (`autovacuum_multixact_freeze_max_age`) and are consumed by **row locking** — heavy `FOR SHARE` / foreign-key locking is what gets you there. Their exhaustion blocks only the write transactions that need a multixact, which makes it harder to spot.

- **Anything holding an old xmin stops vacuum from removing dead tuples across the cluster.** Four sources; check all four before blaming autovacuum:

| Source | Find it |
|---|---|
| Long-running transaction | `pg_stat_activity` where `age(backend_xmin)` is large |
| Idle in transaction | `pg_stat_activity` where `state = 'idle in transaction'` |
| Orphaned replication slot | `pg_replication_slots` / `pg_stat_replication` where `age(xmin)` or `age(catalog_xmin)` is large |
| Orphaned prepared transaction | `pg_prepared_xacts` where `age(transactionid)` is large |

  `hot_standby_feedback = on` also propagates a standby's xmin to the primary — one long analytics query on a replica bloats the primary.

- **Idle-in-transaction is the application bug behind most of the above.** A connection that opens a transaction, calls an HTTP API, then commits holds its snapshot for the length of that call. Set `idle_in_transaction_session_timeout = '30s'` server-wide or per role, and `transaction_timeout` (PG 17+) to bound the whole transaction. Move external calls outside the transaction — the same rule `mir-backend` states as "irreversible external effects go after commit".
- PG 18's `idle_replication_slot_timeout` auto-invalidates inactive slots, so "a forgotten logical slot pinned xmin and filled the disk" finally has a server-side guard. Set it.
- Reclaiming space needs a rewrite. `VACUUM FULL` / `CLUSTER` take ACCESS EXCLUSIVE (offline). Online: **pg_repack** (1.5.3) or **pg_squeeze** (1.9.4, July 2026). PG 19 beta brings `REPACK CONCURRENTLY` into core — not GA at time of writing.

---

## 3. Concurrency

### Isolation levels as actually implemented

| You ask for | You get |
|---|---|
| `READ UNCOMMITTED` | **`READ COMMITTED`.** Postgres implements three levels, not four. Dirty reads cannot happen |
| `READ COMMITTED` (default) | A fresh snapshot **per statement**. Two reads in one transaction can legitimately disagree. Any read-modify-write spanning two statements is a race unless you lock |
| `REPEATABLE READ` | Snapshot taken at the first statement. **Stronger than the standard — no phantom reads.** Writing a row another transaction changed raises `40001 could not serialize access due to concurrent update` |
| `SERIALIZABLE` | SSI. Adds `SIReadLock` predicate locks, which never block and so never deadlock. Raises `40001 could not serialize access due to read/write dependencies among transactions` |

- REPEATABLE READ and SERIALIZABLE both **require the caller to retry**. Retry on SQLSTATE `40001` (serialization_failure) and `40P01` (deadlock_detected), from the **beginning of the transaction**, with jittered backoff and an attempt cap. Retrying one statement is wrong — the snapshot is what is stale.
- Under SSI, predicate locks escalate page → relation when the lock table runs short, and escalation raises the false-positive rate. If `40001` climbs, raise `max_pred_locks_per_transaction` / `max_pred_locks_per_relation` / `max_pred_locks_per_page` before you rewrite the application.
- SERIALIZABLE is a defensible default for money **if** the retry loop exists. Explicit row locks under READ COMMITTED is the alternative. Pick one at Gate 5 and write down which.

### Row locks and the foreign-key trap

| Requested | Conflicts with |
|---|---|
| `FOR KEY SHARE` | `FOR UPDATE` only |
| `FOR SHARE` | `FOR NO KEY UPDATE`, `FOR UPDATE` |
| `FOR NO KEY UPDATE` | `FOR SHARE`, `FOR NO KEY UPDATE`, `FOR UPDATE` |
| `FOR UPDATE` | all four |

- **Inserting a child row takes `FOR KEY SHARE` on the referenced parent row.** An ordinary `UPDATE` of the parent takes `FOR NO KEY UPDATE`, which does *not* conflict — so parent updates and child inserts normally coexist.
- **`SELECT … FOR UPDATE` on the parent does conflict with `FOR KEY SHARE`.** Locking a `users` row `FOR UPDATE` while inserting into `orders` serializes every concurrent insert of any child row of that user. The symptom is a lock queue on a hot parent with no visible writer. **Write `FOR NO KEY UPDATE` unless you are actually changing a key column.**
- `DELETE`, and any `UPDATE` that changes a column carrying a unique index usable in a foreign key, take `FOR UPDATE` implicitly.

### Queue tables: SKIP LOCKED

```sql
UPDATE jobs SET status = 'running', locked_at = now()
WHERE id IN (
  SELECT id FROM jobs WHERE status = 'queued'
  ORDER BY priority, id
  FOR UPDATE SKIP LOCKED
  LIMIT 10
)
RETURNING *;
```

- Without `SKIP LOCKED`, every worker queues on the same first row and throughput collapses to one worker. Without `FOR UPDATE`, they all claim the same rows.
- `NOWAIT` errors instead of skipping — right for "give me this row or fail now", wrong for a worker pool.
- Commit the claim before doing the work, and run a reaper over stale `locked_at`. A queue table is fine to a few thousand jobs/second; past that the update churn bloat is the real cost and a broker is cheaper.

### Advisory locks

- **Default to `pg_advisory_xact_lock(key)`** — it releases at commit or rollback. `pg_advisory_lock(key)` is session-scoped, survives rollback, and a leaked one is held until the connection dies.
- PgBouncer lists session-level advisory locks as **never** working under transaction pooling: the connection returns to the pool at commit while the lock is still held on someone else's session. Transaction-scoped ones are fine.
- The `(bigint)` and `(int, int)` overloads are **different lock spaces**. Two services that think they share a lock and use different overloads do not. Pick one, derive keys with `hashtext()` over a namespaced string, and document the mapping.
- An advisory lock is an application convention. It protects nothing from code that does not take the same lock.

### Deadlocks

- Detected after `deadlock_timeout` (1 s); one transaction dies with `40P01`. It is not an engine defect — it is two transactions taking the same locks in different orders.
- **Fix by ordering.** Sort keys before locking: `SELECT … WHERE id = ANY($1) ORDER BY id FOR UPDATE`, then act in that order. A bare `UPDATE … WHERE id IN (…)` locks in whatever order the plan produces.
- Take the strongest lock you will need **first**. Escalating `FOR SHARE` → `FOR UPDATE` on the same row from two transactions deadlocks reliably.
- Turn on `log_lock_waits` (it is default-on only from PG 19 beta) — it logs the blocking query, which is the only thing that makes this debuggable after the fact.

---

## 4. Indexes

| Type | Right when | Wrong when |
|---|---|---|
| **B-tree** | equality and range on scalars, `ORDER BY`, unique constraints, index-only scans | you need containment or overlap semantics |
| **GIN** | `jsonb @>`, full text `tsvector`, array `&&`/`@>`, `pg_trgm` for `LIKE '%x%'` | write-heavy tables — GIN inserts are expensive, and `fastupdate` defers them into a pending list so one unlucky reader pays for everyone |
| **GiST** | ranges and `&&`, PostGIS geometry, nearest-neighbour `ORDER BY a <-> b`, and **exclusion constraints** | plain equality — b-tree is smaller and faster |
| **BRIN** | very large tables where physical row order correlates with the column (append-only `created_at`, event logs) | any table where insert order does not track the value. Update churn or a rewrite destroys the correlation and a BRIN index on uncorrelated data is worse than none |
| **Hash** | equality only, on large values | almost always — b-tree does equality *and* ordering |

- **Partial index:** `CREATE INDEX … WHERE status = 'queued'`. If 99% of rows are `done`, this is 1% of the size and stays cached. The planner uses it only when it can prove the query predicate implies the index predicate — so the query needs a matching literal predicate; a bound parameter that merely happens to equal `'queued'` at runtime does not match when a generic plan is used.
- **Expression index:** `CREATE INDEX ON users (lower(email))` is required for `WHERE lower(email) = $1`. The expression must match exactly.
- **Covering / index-only scan:** `CREATE INDEX ON t (x) INCLUDE (y)` stores `y` in the leaf without making it a key, so `CREATE UNIQUE INDEX … (x) INCLUDE (y)` still enforces uniqueness on `x` alone. An index-only scan additionally needs the heap page marked all-visible in the **visibility map**; if vacuum has not run, `EXPLAIN (ANALYZE, BUFFERS)` shows a nonzero `Heap Fetches` and you get no benefit. `INCLUDE` works on b-tree, GiST, SP-GiST. **GIN never supports index-only scans.**

**Why the index is not used** — in the order it is usually the answer:

1. **Type mismatch.** Look for a cast on the *column* side in `EXPLAIN` (`col::text = …`). A `bigint` column compared against a driver-supplied `numeric`, or a `varchar` column compared to an integer, silently disables the index.
2. **A function wrapping the column.** `WHERE date(created_at) = '2026-01-01'` cannot use an index on `created_at`. Write the range: `created_at >= '2026-01-01' AND created_at < '2026-01-02'`.
3. **Low selectivity.** Past roughly 5–10% of the table a sequential scan really is faster. The planner is right; change the query or add a partial index.
4. **Stale statistics.** `ANALYZE` after any bulk load. `pg_upgrade` preserves planner statistics from PG 18 on but never extended statistics — re-`ANALYZE` after a major upgrade regardless.
5. **Missing leading column.** PG 18's b-tree **skip scan** makes a multi-column index usable without a restriction on the leading column. On 17 and below it does not — do not carry a PG 18 assumption onto a 16 cluster.

`LIKE 'x%'` under a non-C collation, `OR` across columns, and generic-plan parameter cases are in `references/index-and-query-tuning.md` with the `EXPLAIN` output to look for.

**Index bloat** is separate from table bloat: b-tree indexes bloat from update churn on their own. Measure with `pgstattuple` / `pgstatindex`; fix with `REINDEX INDEX CONCURRENTLY` (SHARE UPDATE EXCLUSIVE), never plain `REINDEX`. Find unused indexes in `pg_stat_user_indexes` where `idx_scan = 0` — check every replica too, and remember the counters reset on restart.

---

## 5. Types and constraints

- **Exclusion constraint** — the only correct way to say "no two rows may overlap". Check-then-insert in application code cannot do this under concurrency:

  ```sql
  CREATE EXTENSION IF NOT EXISTS btree_gist;   -- needed to mix = on a scalar with && in one GiST index
  ALTER TABLE bookings ADD CONSTRAINT bookings_no_overlap
    EXCLUDE USING gist (room_id WITH =, during WITH &&);
  ```

  PG 18's `PRIMARY KEY (id, valid_period WITHOUT OVERLAPS)` is the standardized form for the temporal case.
- **Generated columns.** `GENERATED ALWAYS AS (expr) STORED` is computed on write, indexable, and **rewrites the table when added**. In PG 18 the default is **VIRTUAL** — computed on read, no storage, no rewrite on add, and **not indexable**. On PG 18 write `STORED` explicitly whenever you intend to index it; on 17 and below `VIRTUAL` does not exist.
- **jsonb is the wrong answer when a key is queried, constrained, or joined.** It has no per-key statistics, so containment selectivity is estimated badly; it supports no `NOT NULL`, no foreign key, no per-key `CHECK`; and reading one field detoasts the whole document. Anything appearing in a `WHERE`, `ORDER BY`, or `JOIN`, or carrying an invariant, belongs in a column. jsonb is right for open-ended, read-whole payloads — webhook bodies, third-party responses, per-tenant custom fields you never filter on. If you do query it: `USING gin (doc jsonb_path_ops)` for `@>` only (smaller, faster), default `gin` when you also need key-existence operators.
- **Enums cost more than they look.** `ALTER TYPE … ADD VALUE` runs inside a transaction block since PG 12, but **the new value cannot be used until that transaction commits** — "add the value, then backfill rows to it" in one migration fails. There is no `DROP VALUE`: removing one means a new type and a rewrite of every column using it. Ordering is fixed at creation. For a status list that will change, `text` plus a named `CHECK` is cheaper — adding a value is then a `NOT VALID` CHECK swap.
- **Money is `numeric(19,4)` or a `bigint` count of minor units.** Never `float`/`double precision`/`real` — binary rounding makes totals irreproducible. Never the `money` type: its parsing and output depend on `lc_monetary`, so the same row reads differently on two servers.
- **`timestamptz`, always.** It stores a UTC instant. `timestamp` stores a wall-clock reading with no zone and means different instants to different sessions. `timestamptz` does **not** store the originating zone — if you need the user's local zone, that is a separate `text` column holding an IANA name. Compare against `now()`, which is already `timestamptz`.

---

## 6. Connections and pooling

- Every Postgres connection is an **OS process** with its own memory. A few hundred idle connections cost real RAM and slow down every snapshot and lock-table operation. `max_connections` is a safety limit, not a capacity target — keep it in the low hundreds and put a pooler in front.
- Postgres has no built-in pooler. **PgBouncer 1.25.2** (May 2026) is the default answer; Supavisor and pgcat are alternatives.

| Mode | Server connection returned when | Use for |
|---|---|---|
| session | the client disconnects | migrations, `LISTEN`, anything session-stateful |
| **transaction** | the transaction ends | normal web/API traffic — the only mode that gives real multiplexing |
| statement | the statement ends | autocommit-only; multi-statement transactions are rejected |

What breaks under **transaction** pooling, from PgBouncer's own feature map:

| Feature | Transaction pooling |
|---|---|
| `SET` / `RESET` | **never** — use `SET LOCAL` inside the transaction |
| `PREPARE` / `DEALLOCATE` (SQL-level) | **never** |
| Protocol-level prepared statements | works, but only with `max_prepared_statements > 0` (default 200; the setting exists only in transaction/statement mode) |
| Session-level advisory locks | **never** — use `pg_advisory_xact_lock` |
| `LISTEN` | **never** (`NOTIFY` works) |
| `WITH HOLD` cursor | **never** |
| Temp tables that are not `ON COMMIT DROP` | **never** |
| `LOAD` | **never** |

- Two of these are correctness bugs, not performance bugs, and both are silent: a `SET search_path` / `SET ROLE` / `SET app.tenant_id` tenant context that leaks into the next request's transaction, and a session advisory lock released to the pool while still held. **`SET LOCAL`, always.**
- Driver prepared-statement caches are the third. asyncpg names statements per connection; under transaction pooling that name is on a different backend next time. Either set PgBouncer's `max_prepared_statements`, or turn the driver cache off (`statement_cache_size=0` for asyncpg, `prepareThreshold=0` for pgjdbc, `prefer_simple_protocol` / `prepared_statements=false` for many pgx and node setups) and accept a parse per execution.
- **Sizing.** The server-side pool is small — roughly `(cores × 2) + effective_spindle_count`, i.e. a few dozen, *not* one per application thread. PgBouncer's `default_pool_size` is per `(user, database)` pair, so total server connections = pools × size, which must stay under `max_connections` minus `superuser_reserved_connections`. On the application side the number that actually arrives at the database is `instances × per_instance_pool` — that is the arithmetic that hits `max_connections` in production.

---

## 7. Partitioning and RLS

**Partitioning.** Partition for a reason you can name — dropping old data with `DROP TABLE` instead of `DELETE`, or bounding index size. Not for "performance" on a 10 M-row table.

- **Every unique constraint and primary key must include the partition key.** There are no global unique indexes. Partitioning by `created_at` turns a PK on `id` into `(id, created_at)`, and uniqueness on `id` alone is no longer enforced anywhere. Decide this at Gate 5; it cannot be retrofitted cheaply.
- Pruning happens only when the partition key appears in `WHERE`. A query without it touches every partition, and past a few hundred partitions planning time alone becomes the bottleneck. Keep the count in the low hundreds.
- `ATTACH PARTITION` scans the new partition to prove it matches the bound **unless a matching CHECK constraint already exists** — add that constraint first, validated, so the attach is metadata only. `DETACH PARTITION CONCURRENTLY` avoids the parent ACCESS EXCLUSIVE.
- PG 18 changed `VACUUM`/`ANALYZE` to recurse into partitions by default; `ONLY` restores the old behaviour. Anything that assumed analyzing the parent was cheap now costs more.

**Row-level security for multi-tenancy.**

```sql
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE orders FORCE  ROW LEVEL SECURITY;   -- without this, the table owner is exempt
CREATE POLICY tenant_isolation ON orders
  USING      (tenant_id = current_setting('app.tenant_id')::uuid)
  WITH CHECK (tenant_id = current_setting('app.tenant_id')::uuid);
```

- `USING` decides which rows are visible; `WITH CHECK` decides what may be written. For `ALL` and `UPDATE` policies, omitting `WITH CHECK` makes Postgres reuse the `USING` expression as the check, so the omission is not itself a tenant-escape hole. Write both anyway: the moment the two need to differ (a tenant that may read a shared row but not write it), the implicit copy is silently wrong. `INSERT` policies take **only** `WITH CHECK`, and `SELECT`/`DELETE` take **only** `USING` — a `WITH CHECK` written on a `SELECT` policy is a syntax error, not a no-op.
- **Four bypass paths, all silent.** Superusers and `BYPASSRLS` roles ignore policies unconditionally. The **table owner** ignores them unless `FORCE ROW LEVEL SECURITY` — and the application role owning its own tables is the common setup. Views run with the definer's rights unless created `WITH (security_invoker = true)` (PG 15+). And **referential-integrity checks always bypass RLS**, which the documentation names as a covert channel: a unique-violation error confirms a row exists in a tenant you cannot see.
- The tenant variable must be `SET LOCAL` inside the transaction (see §6). Treat `current_setting('app.tenant_id', true)` returning NULL as a hard error — `tenant_id = NULL` denies everything, which is safe, but `COALESCE`ing it to a default is the bug that opens the whole table.
- **Performance:** the policy expression runs per row, *before* the user's own conditions — except for `leakproof` functions, which the planner may hoist ahead of it, and that exception is the information leak (a non-leakproof error can fire on rows the caller cannot see). Keep the policy a simple comparison against a session variable; a sub-`SELECT` against another table adds that lookup to every row of every query. Cast the setting once, never the column, and make `tenant_id` the leading column of the indexes the policy filters on — otherwise RLS turns index scans into filtered sequential scans.
- RLS is defence in depth. Keep `WHERE tenant_id = $1` in the query too.

---

## Security

Postgres engine mechanics. Schema-level discipline (PII classification, least-privilege role design, soft delete vs erasure) is in `mir-database`; application-side authorization is in `mir-backend`.

**Run the current minor — extension code is the recurring arbitrary-code-execution path.**

| Batch | Fixed in | Notable entries |
|---|---|---|
| May 2026 | 18.4 / 17.10 / 16.14 / 15.18 / 14.23 | **CVE-2026-6637** `refint` stack buffer overflow **and** SQL injection · **CVE-2026-6638** SQL injection via table name in `REFRESH PUBLICATION` (18/17/16) · **CVE-2026-6476** SQL injection via subscription name in `pg_createsubscriber` (18/17) · **CVE-2026-6478** MD5-hashed password disclosure via a timing channel · **CVE-2026-6477** `libpq` `lo_*` lets a server superuser overwrite client stack memory · plus CVE-2026-6479 (SSL/GSS init DoS), -6475, -6474, -6473, -6472, -6575 |
| February 2026 | 18.2 / 17.8 / 16.12 / 15.16 / 14.21 | **CVE-2026-2005** `pgcrypto` heap overflow → arbitrary code execution · **CVE-2026-2006** multibyte length validation → arbitrary code execution · **CVE-2026-2007** `pg_trgm` heap overflow (18) · CVE-2026-2004 `intarray` · CVE-2026-2003 `oidvector` memory disclosure |

**Authentication.** Use `scram-sha-256` and set `password_encryption = 'scram-sha-256'`. MD5 is deprecated in PG 18 (it warns on `CREATE ROLE`/`ALTER ROLE`), CVE-2026-6478 leaks the hashes, and PG 19 beta removes RADIUS as insecure. `trust` in `pg_hba.conf` is a full bypass — grep for it in every environment, not just production.

**Injection at the engine layer.** Identifiers cannot be parameterized. In PL/pgSQL and dynamic SQL use `format('%I', name)` / `quote_ident()` for identifiers and `%L` / `quote_literal()` for values, plus an allow-list for anything the client chooses (a sort column, a partition name, a tenant schema). Three of the 2026 CVEs above are this exact bug inside Postgres' own tooling.

**`SECURITY DEFINER` functions must pin their search path** — `SET search_path = pg_catalog, pg_temp` in the definition. Without it a caller creates a shadowing object in an earlier schema and your function runs their code with the definer's privileges.

**PgBouncer CVE-2025-12819** (fixed in 1.25.1; run **1.25.2**). An unauthenticated attacker could execute arbitrary SQL during authentication via a malicious `search_path` in the startup message. All three had to be true: `track_extra_parameters` includes `search_path` (non-default, but Citus and PG 18 setups enable it), `auth_user` is set, and `auth_query` uses object names that are not schema-qualified. **Schema-qualify every object in `auth_query`.**

**Least privilege.** The application role owns nothing, has no `CREATE`, and runs no DDL; migrations use a separate role. From PG 15 the `public` schema no longer grants `CREATE` to `PUBLIC` — on 14 (EOL 12 Nov 2026) revoke it yourself. Never grant `BYPASSRLS` to an application role, and never let it own an RLS table without `FORCE ROW LEVEL SECURITY`.

**PII in logs.** `log_statement = 'all'`, `log_min_duration_statement` with bound parameters, ORM statement echo, and error details that quote the failing row all copy column values into a log with different retention and different access control. Set `log_parameter_max_length = 0` for anything touching PII, and keep tokens out of `application_name`.

**Transport and network defaults.** Bind to a private interface, require `hostssl` in `pg_hba.conf`, and use `sslmode=verify-full` on the client — `require` encrypts but does **not** verify the certificate, so it does not stop a man in the middle. Extensions run as the database OS user: install only what you use and pin the version.

---

## How this slots into the pipeline

- **Gate 5 (Schema Design Review):** state the Postgres major version, the isolation level and the retry policy, the row-lock mode for each contended update (`FOR UPDATE` vs `FOR NO KEY UPDATE`), the index set with its type per index, the tenancy mechanism (RLS + `FORCE` + `SET LOCAL`, or an application `WHERE`), and the pooling mode with the connection arithmetic. Name the lock class of every planned DDL statement.
- **Gate 6 (DDL & Migration):** every migration sets `lock_timeout` and `statement_timeout`; one DDL statement per transaction; `CONCURRENTLY` for index create/drop with the invalid-index cleanup written into the same change; `NOT VALID` then `VALIDATE` for constraints on populated tables. Read `references/ddl-lock-recipes.md`.
- **Gate 7 (Production-Readiness):** the migration-reviewer checks §1 against real row counts; the reliability-reviewer checks §2–§4 (vacuum settings, retry on `40001`/`40P01`, lock ordering, index coverage of the query list); the security-reviewer works the Security section — the minor version, `FORCE ROW LEVEL SECURITY`, `SECURITY DEFINER` search paths, and `auth_query` qualification are the four most commonly missed.

## References

| File | Purpose |
|---|---|
| `references/ddl-lock-recipes.md` | Per-statement lock class table and step-by-step safe recipes: expand/contract type change, batched backfill, adding a NOT NULL / FK / unique constraint, renames, dropping a column, and rolling-deploy compatibility with the currently deployed application version. Read at Gate 6. |
| `references/index-and-query-tuning.md` | Index type selection in depth, operator classes, the full "why is my index not used" checklist with `EXPLAIN` output to look for, bloat measurement, and unused-index retirement. Read at Gate 5 and Gate 7. |

## Edit boundary

- True of MySQL and MongoDB too (cardinality, keys, normalization, nullability, tenancy model, soft delete, audit) → **up** to `mir-database`.
- Application-side transaction boundaries, idempotency keys, retry code, ORM and migration-tool wiring (Alembic revisions live in `mir-backend-python-fastapi`, not here) → **across** to `mir-backend` and its framework modules.
- **Here:** only what is true because the engine is PostgreSQL — lock classes per DDL subform, MVCC and vacuum, SSI and row-lock modes, Postgres index types and operator classes, Postgres type and constraint mechanics, PgBouncer pooling behaviour, declarative partitioning, and RLS.
- A different engine → its own `mir-database-<engine>` module. Never widen this one.
