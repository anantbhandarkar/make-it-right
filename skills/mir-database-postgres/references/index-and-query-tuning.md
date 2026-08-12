# PostgreSQL index selection and query tuning

Read at **Gate 5** (choosing the index set from the enumerated query list) and **Gate 7** (verifying it held up). The order of work is fixed: find the queries that actually cost you (§1), read the plan (§2), then change the index set (§3–§8). Adding indexes before §1 is guessing.

**Version state, verified 13 August 2026 against postgresql.org.** Current stable major is **PostgreSQL 18** (18.4, released 2026-05-14). Also supported: 17.10, 16.14, 15.18, 14.23. PostgreSQL 14 reaches end of life 12 November 2026. PostgreSQL 19 was at Beta 2 (2026-07-16) with no GA date.

Behaviour marked ***PG 18+*** does not exist on 17 and below. Behaviour marked ***all supported*** is true on 14 through 18. Check first:

```sql
SHOW server_version;
```

---

## 1. Find the queries that matter — pg_stat_statements

The query someone complains about is rarely the query costing the most. `pg_stat_statements` aggregates by normalized query text, so a 4 ms query run 90 million times shows up above a 20-second report run twice a day. That ranking is the point.

### Setup

```ini
# postgresql.conf — requires a restart, the library must be preloaded
shared_preload_libraries = 'pg_stat_statements'
pg_stat_statements.track = top      # 'all' also counts statements inside functions
track_io_timing = on                # otherwise every I/O timing column reads 0
```

```sql
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

`pg_stat_statements.max` defaults to **5000** tracked statements (PG 18). When the table is full the least-executed entries are evicted, so a system with a lot of unparameterized SQL evicts the interesting rows before you look. If `SELECT count(*) FROM pg_stat_statements` sits at the cap, either raise it or fix the unparameterized SQL — one of those is the real bug.

### The workflow

**Step 1 — reset, then wait a full business cycle.** Cumulative counters since the last restart mix a Monday morning with a batch job from three weeks ago.

```sql
SELECT pg_stat_statements_reset();
-- wait: a full day, or a full week if month-end jobs matter
```

The signature is `pg_stat_statements_reset(userid, dbid, queryid, minmax_only)`; all arguments are optional. `minmax_only => true` resets just the min/max timing fields, which is how you get a fresh peak without losing the totals.

**Step 2 — rank by total time, not by mean time.** Total time is the number that shows where the server actually went.

```sql
SELECT
  substr(query, 1, 90)                          AS query,
  calls,
  round(total_exec_time)                        AS total_ms,
  round(mean_exec_time::numeric, 2)             AS mean_ms,
  round(stddev_exec_time::numeric, 2)           AS stddev_ms,
  rows,
  round(rows::numeric / nullif(calls, 0), 1)    AS rows_per_call,
  round(100.0 * shared_blks_hit
        / nullif(shared_blks_hit + shared_blks_read, 0), 1) AS cache_hit_pct
FROM pg_stat_statements
WHERE query NOT LIKE 'EXPLAIN%'
ORDER BY total_exec_time DESC
LIMIT 20;
```

**Step 3 — read the top 20 for these four shapes**, in this order:

| What you see | What it means | Where to go |
|---|---|---|
| High `calls`, low `mean_ms`, `rows_per_call` ≈ 1 | N+1 from the application, not a database problem | fix the caller — batch or join |
| High `shared_blks_read`, low `cache_hit_pct` | reading from disk, usually a missing or unusable index | §2, then §8 |
| `mean_ms` low but `stddev_ms` many times `mean_ms` | plan instability, lock waits, or a generic plan losing a partial index | §5, and check `pg_locks` |
| `temp_blks_written` non-zero | sorts or hashes spilling to disk | raise `work_mem` for that query, or index the sort |

**Step 4 — take one query from that list and `EXPLAIN (ANALYZE, BUFFERS)` it (§2).** Not the whole list. One at a time, measured before and after.

**Step 5 — re-rank after the change.** Fixing the top query promotes the next one; the absolute numbers only mean something against a fresh reset.

### Columns worth knowing (PG 18)

| Column | Use |
|---|---|
| `calls`, `rows` | volume, and rows per call |
| `total_exec_time`, `mean_exec_time`, `min_exec_time`, `max_exec_time`, `stddev_exec_time` | execution timing |
| `plans`, `total_plan_time`, `mean_plan_time` | planning cost. High planning time relative to execution points at too many partitions or too many indexes |
| `shared_blks_hit` / `shared_blks_read` / `shared_blks_dirtied` / `shared_blks_written` | buffer cache behaviour |
| `shared_blk_read_time`, `shared_blk_write_time` | actual I/O wait. Zero unless `track_io_timing = on` |
| `temp_blks_read`, `temp_blks_written` | spill to disk — `work_mem` is too small for this query |
| `wal_records`, `wal_fpi`, `wal_bytes` | write amplification. `wal_fpi` spikes right after a checkpoint |
| `wal_buffers_full` | ***PG 18+*** — WAL buffer pressure |
| `parallel_workers_to_launch`, `parallel_workers_launched` | ***PG 18+*** — the gap between the two means the worker pool is exhausted |
| `queryid`, `toplevel` | join key; `toplevel = false` rows are statements inside functions |
| `stats_since`, `minmax_stats_since` | when this row's counters started — do not compare rows with different windows |

**Column names have changed across major versions** (the I/O timing columns in particular were renamed). Run `\d pg_stat_statements` on the actual server before copying any query, including the ones above.

***PG 18+*** also tracks `CREATE TABLE AS` and `DECLARE`, and parameterizes the values in `SET` statements so they collapse into one entry instead of thousands.

### Related views

- `pg_stat_user_tables` — `seq_scan` vs `idx_scan` per table, and `n_dead_tup` / `last_autovacuum`. A large table with a high `seq_scan` count is a direct index candidate.
- `pg_stat_user_indexes` — `idx_scan` per index. Drives retirement (§11).
- `pg_stat_activity` — what is running right now, with `wait_event_type` / `wait_event`. `pg_stat_statements` is history; this is the present.

---

## 2. Reading `EXPLAIN (ANALYZE, BUFFERS)`

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT … ;
```

***PG 18+*** includes `BUFFERS` in `EXPLAIN ANALYZE` automatically. On **17 and below you must write it**, and a plan captured without it is missing the only evidence of where the I/O went. Write `BUFFERS` explicitly regardless — it is a no-op on 18 and essential on 17.

Options worth adding:

| Option | Gives you | Version |
|---|---|---|
| `ANALYZE` | actually runs the query and reports real timings and row counts | all supported |
| `BUFFERS` | shared / local / temp block counts per node | all supported; automatic with `ANALYZE` on ***PG 18+*** |
| `SETTINGS` | planner settings that are not at their default — catches a session-level `enable_seqscan = off` someone left behind | all supported |
| `VERBOSE` | output column lists, schema-qualified names | all supported |
| `WAL` | WAL generated, for `INSERT`/`UPDATE`/`DELETE` plans | all supported |
| `GENERIC_PLAN` | plan a parameterized query without supplying values — the plan a prepared statement may actually get (§5). Cannot be combined with `ANALYZE` | PG 16+ |
| `FORMAT JSON` | machine-readable, for storing plans in a regression test | all supported |

**`EXPLAIN ANALYZE` executes the statement.** For anything that writes, wrap it:

```sql
BEGIN;
EXPLAIN (ANALYZE, BUFFERS) UPDATE orders SET status = 'x' WHERE id = 1;
ROLLBACK;
```

### The node line

```
Index Scan using idx_orders_customer on orders  (cost=0.43..8.45 rows=1 width=68)
                                                (actual time=0.021..0.108 rows=412 loops=3)
  Index Cond: (customer_id = 42)
  Rows Removed by Filter: 1877
  Buffers: shared hit=1204 read=318 dirtied=2
```

| Field | Meaning |
|---|---|
| `cost=0.43..8.45` | estimated startup cost .. estimated total cost, in arbitrary planner units. Useful only for comparing plans of the same query |
| `rows=1` | the planner's **estimate** |
| `width=68` | estimated average row width in bytes |
| `actual time=0.021..0.108` | real milliseconds: time to first row .. time to last row, **per loop** |
| `rows=412` | real rows returned, **per loop** |
| `loops=3` | how many times this node ran |

**The single most common misreading: `actual rows` and `actual time` are per loop.** This node returned 412 × 3 = 1236 rows and took 0.108 × 3 ≈ 0.32 ms in total. On the inner side of a nested loop, `loops` is often in the thousands, and a node that looks like 0.1 ms is the whole runtime.

### What to compare, in order

1. **Estimate versus actual.** `rows=1` against `actual rows=412` is a 412× underestimate. That is the real defect — the planner picked a nested loop because it expected one row. A bad estimate low in the plan produces a bad join order high in the plan, and no index will fix that. Go to §9.
2. **Where the time is.** Subtract child node times from the parent to get a node's own cost. The slow node is usually not the top one.
3. **`Buffers`.** `shared hit=` came from the buffer cache. `shared read=` came from the OS or disk. A node reading tens of thousands of blocks to return ten rows is the missing-index signal, and it is more reliable than timing because timing depends on what happened to be cached.
4. **`Rows Removed by Filter`.** The index got you to the pages, but the predicate was applied afterwards, on the heap. A large number here means the filtered column belongs in the index, or the index should be partial (§5).
5. **`Heap Fetches`** on an `Index Only Scan` — see §7.
6. **`temp read=` / `temp written=`** in `Buffers`, or `Sort Method: external merge Disk: 84032kB`. The node spilled. Raise `work_mem` for that session, or provide an index that supplies the order.

### Node types and what they tell you

| Node | Read it as |
|---|---|
| `Seq Scan` | full table read. Correct for small tables and for predicates matching a large fraction of rows (§8, cause 3) |
| `Index Scan` | index lookup, then a heap fetch per row. Good for few rows |
| `Index Only Scan` | answered from the index alone — but check `Heap Fetches` (§7) |
| `Bitmap Heap Scan` + `Bitmap Index Scan` | too many rows for a plain index scan, too few for a seq scan. `Recheck Cond` plus a large `Rows Removed by Index Recheck` means `work_mem` was too small and the bitmap went lossy |
| `Nested Loop` | fine when the inner side is one indexed lookup and `loops` is small. Catastrophic when the outer estimate was wrong |
| `Hash Join` | builds a hash of the smaller side. Check `Batches:` — more than 1 means it spilled to disk |
| `Merge Join` | both inputs sorted. Cheap if indexes supply the order, expensive if it had to sort |
| `Materialize`, `Memoize` | caching an inner result across loops. `Memoize` reporting a low hit rate is wasted work |

***PG 18+*** additionally reports index lookups per index-scan node, fractional row counts for repeated nodes, memory and disk usage for `Material` / `WindowAgg` / CTE nodes, and marks nodes disabled by an `enable_*` setting. On 17 and below none of that appears.

---

## 3. Index type selection

| Type | Operators it serves | Choose it when | Do not choose it when |
|---|---|---|---|
| **B-tree** | `<`, `<=`, `=`, `>=`, `>`, `BETWEEN`, `IN`, `IS NULL`, prefix `LIKE 'x%'` (with the right opclass), `ORDER BY` | the default for scalars: equality, ranges, sorting, uniqueness, index-only scans | you need containment, overlap, or similarity |
| **GIN** | `@>`, `?`, `?&`, `?\|` on `jsonb`; `@@` on `tsvector`; `&&`, `@>` on arrays; `%` and `LIKE '%x%'` via `pg_trgm` | one row contains many indexable values: documents, tags, full text, substring search | the table is write-heavy. GIN inserts are expensive, and `fastupdate = on` (the default) defers them into a pending list that one unlucky reader then flushes |
| **GiST** | `&&` on ranges, PostGIS geometry, `<->` nearest-neighbour ordering, **exclusion constraints** | ranges, geometry, KNN, and any "no two rows may overlap" rule | plain scalar equality — b-tree is smaller and faster |
| **SP-GiST** | non-balanced structures: quadtrees, radix trees, `inet` prefix matching | text-prefix and IP-range workloads with a natural partitioning | general-purpose use |
| **BRIN** | `<`, `<=`, `=`, `>=`, `>` over a physically ordered column | hundreds of millions of rows where physical order tracks the value: append-only `created_at`, event logs, time series | correlation is weak, or was destroyed by update churn or a table rewrite. Uncorrelated BRIN is worse than no index — it identifies ranges and then discards them |
| **Hash** | `=` only | a very large value where a b-tree entry would be unwieldy | almost always. B-tree does equality **and** ordering, for a similar size |

Check BRIN's precondition before building one:

```sql
SELECT attname, correlation FROM pg_stats
WHERE tablename = 'events' AND attname = 'created_at';
-- |correlation| near 1.0 → BRIN works. Near 0 → BRIN is useless.
```

### Operator classes that change the answer

| Opclass | Needed for |
|---|---|
| `jsonb_path_ops` | GIN for `@>` only. Substantially smaller and faster than the default `jsonb_ops`, but **cannot** answer key-existence (`?`) queries |
| `text_pattern_ops` / `varchar_pattern_ops` | `LIKE 'prefix%'` using a b-tree under any collation other than `C`. Under `en_US.UTF-8` the default opclass will not be used for pattern matching at all |
| `gin_trgm_ops` (extension `pg_trgm`) | leading-wildcard `LIKE '%x%'` and similarity `%` |
| `btree_gist` / `btree_gin` | mixing scalar equality into a GiST or GIN index. `btree_gist` is what makes `EXCLUDE USING gist (room_id WITH =, during WITH &&)` possible |

---

## 4. Composite index column order

In order of precedence:

1. **Equality columns first**, in any order among themselves.
2. **Then one range or sort column.**
3. Match the `ORDER BY` direction to avoid a sort node: `(customer_id, created_at DESC)` serves `WHERE customer_id = $1 ORDER BY created_at DESC LIMIT 20` with no `Sort` in the plan.

An index on `(a, b)` serves `WHERE a = …`, `WHERE a = … AND b = …`, and `ORDER BY a, b`. On **17 and below it does not efficiently serve `WHERE b = …`** — the leading column is required.

***PG 18+*** adds **b-tree skip scan**, which lets a multi-column index be used when the leading columns have no restriction but later columns do. It works by skipping between distinct leading values, so it helps when the leading column has **low cardinality** and does nothing useful when it has millions of distinct values. It narrows the rule; it does not remove it. Do not carry a PG 18 index design onto a 16 or 17 cluster and expect the same plans.

---

## 5. Partial indexes and the generic-plan trap

```sql
CREATE INDEX idx_jobs_queued ON jobs (priority, id) WHERE status = 'queued';
```

If 99% of rows are `done`, this index is 1% of the size and stays resident in cache.

The planner uses a partial index only when it can **prove**, at plan time, that the query predicate implies the index predicate.

| Query | Uses the index? |
|---|---|
| `WHERE status = 'queued' AND priority < 5` | Yes — the literal is present |
| `WHERE status = $1 AND priority < 5`, custom plan with `$1 = 'queued'` | Yes |
| `WHERE status = $1 AND priority < 5`, **generic plan** | **No** — `$1` is unknown at plan time, the proof fails |
| `WHERE status <> 'done'` | No — that does not imply `= 'queued'` |

Postgres builds a custom plan for the first five executions of a prepared statement, then may switch to a generic plan if the generic estimate looks no worse. The index quietly stops being used on the sixth execution. The symptom is a query that is fast in testing and slow in production, with no code change.

Diagnose and fix:

```sql
-- PG 16+: see the generic plan directly, without waiting for the sixth execution
EXPLAIN (GENERIC_PLAN) SELECT * FROM jobs WHERE status = $1 AND priority < 5;

-- Force custom planning where it matters
SET plan_cache_mode = force_custom_plan;   -- session or role scoped
```

Or inline the literal for the small fixed set of values that actually matter. Turning off the driver's prepared-statement cache also avoids it, at the cost of a parse per execution.

---

## 6. Expression indexes

`WHERE lower(email) = $1` cannot use an index on `email`. It needs the expression indexed:

```sql
CREATE INDEX idx_users_email_lower ON users (lower(email));
```

The match must be **exact**. The index above is used by `WHERE lower(email) = $1` and is **not** used by:

- `WHERE email ILIKE $1`
- `WHERE lower(trim(email)) = $1`
- `WHERE lower(email::text) = $1` if the cast changes the expression

The function must be `IMMUTABLE`. A `STABLE` or `VOLATILE` function cannot be indexed, which is why `WHERE created_at::date = current_date` cannot be fixed with an expression index on the left side — rewrite it as a range instead (§8, cause 2).

---

## 7. Covering indexes and index-only scans

```sql
CREATE INDEX idx_orders_cust ON orders (customer_id) INCLUDE (total_minor, status);
```

Three conditions must **all** hold for an index-only scan:

1. **The index type supports it.** B-tree always. GiST and SP-GiST for some operator classes. **GIN never** — a GIN entry holds only part of the original value.
2. **The query references only columns in the index** (key columns or the `INCLUDE` payload).
3. **The heap pages are marked all-visible in the visibility map.** Only `VACUUM` (and `COPY … FREEZE`) sets those bits.

Condition 3 is the one that bites, and it is invisible without `BUFFERS`:

```sql
EXPLAIN (ANALYZE, BUFFERS) SELECT total_minor FROM orders WHERE customer_id = 42;
--  Index Only Scan using idx_orders_cust on orders
--    Heap Fetches: 0        <- working
--    Heap Fetches: 18412    <- vacuum has not run; you are paying for the index and getting nothing
```

A table with steady write churn and default autovacuum settings will show non-zero `Heap Fetches` most of the time. Either lower `autovacuum_vacuum_scale_factor` on that table or stop counting on the index-only scan.

`INCLUDE` columns are **disregarded for uniqueness and exclusion enforcement**: `CREATE UNIQUE INDEX … (x) INCLUDE (y)` enforces uniqueness on `x` alone. They also cannot be used in an index scan search qualification — only as payload.

---

## 8. Why the index is not used — full checklist

Run `EXPLAIN (ANALYZE, BUFFERS, SETTINGS)` and work down in order. The first four account for most cases.

| # | Cause | `EXPLAIN` signature | Fix |
|---|---|---|---|
| 1 | **Type mismatch** | a cast on the **column** side: `Filter: ((id)::text = '42'::text)` | fix the parameter type in the driver. A `bigint` column compared against a driver-supplied `numeric`, or a `varchar` column compared to an integer, silently disables the index. Failing that, index that exact cast |
| 2 | **A function wrapping the column** | `Filter: (date(created_at) = '2026-01-01'::date)` | rewrite as a range: `created_at >= '2026-01-01' AND created_at < '2026-01-02'`. Or add the expression index (§6) if the function is `IMMUTABLE` |
| 3 | **Low selectivity** | `Seq Scan` whose `actual rows` is a large fraction of the table | the planner is right. Past roughly 5–10% of the table a sequential scan is genuinely faster. Change the query, or add a partial index (§5) |
| 4 | **Stale statistics** | estimated `rows` off from `actual rows` by 10× or more | `ANALYZE` the table. Always `ANALYZE` after a bulk load. Then §9 |
| 5 | Correlated predicates | two dependent `WHERE` columns; the estimate collapses toward 1 | `CREATE STATISTICS s (dependencies, ndistinct) ON city, postcode FROM addresses; ANALYZE addresses;` |
| 6 | Pattern match under a non-`C` collation | `Seq Scan` on `LIKE 'x%'` | build with `text_pattern_ops`, or set `COLLATE "C"` on the column |
| 7 | Leading wildcard | `LIKE '%x%'` | `pg_trgm` + GIN with `gin_trgm_ops` |
| 8 | `OR` across different columns | `Seq Scan` with an `OR` in `Filter` | rewrite as `UNION ALL` of two indexed branches |
| 9 | Generic plan defeating a partial index | plan differs between the 1st and 6th execution | §5 — `EXPLAIN (GENERIC_PLAN)`, then `plan_cache_mode = force_custom_plan` |
| 10 | `NOT IN` with a nullable subquery | no anti-join node — **and silently zero rows** | use `NOT EXISTS`. `NOT IN` cannot become an anti-join when NULLs are possible, and it is worse than slow: if the subquery yields even one NULL, `x NOT IN (…)` evaluates to NULL for every row and the query returns nothing. Wrong results, no error |
| 11 | The index is `INVALID` | the index appears in no plan at all | a failed `CREATE INDEX CONCURRENTLY`. See `ddl-lock-recipes.md` §9 |
| 12 | Missing leading column | `Seq Scan` despite an index on `(a, b)` and a predicate on `b` | ***PG 18+*** skip scan may handle it if `a` has low cardinality. On 17 and below, add an index led by `b` (§4) |
| 13 | Statistics differ on the replica | fast on the primary, `Seq Scan` on the replica | `ANALYZE` runs on the primary and its results replicate; check `pg_stat_user_tables.last_analyze`. Also check for a session-level `enable_*` override with `SETTINGS` |
| 14 | The index is on the wrong expression | the index exists but is never chosen and none of the above apply | compare the indexed expression character by character against the predicate (§6) |

---

## 9. Statistics and planner settings

| Setting | Default | When to change |
|---|---|---|
| `default_statistics_target` | 100 | raise per column, not globally: `ALTER TABLE t ALTER COLUMN c SET STATISTICS 1000` on a skewed column producing bad estimates. Raising it globally makes every `ANALYZE` slower |
| `CREATE STATISTICS` | none | correlated columns, or a badly wrong `ndistinct` on a `GROUP BY`. Requires an `ANALYZE` afterwards to populate |
| `effective_cache_size` | 4 GB | set to roughly 50–75% of machine RAM. Too low makes the planner avoid index scans it should choose. It reserves nothing — it is an estimate the planner uses |
| `random_page_cost` | 4.0 | 1.1 on SSD or NVMe. The default assumes spinning disks and systematically over-prices index scans. This one setting changes more plans than any index you will add |
| `work_mem` | 4 MB | per sort or hash **node**, per parallel worker — one query with several nodes can use many multiples. Raise per session for a known heavy report, never globally to a large value |
| `jit` | on (through 18) | JIT compilation on a query with an inflated cost estimate can add seconds of compile time to a millisecond query. If a plan shows a large `JIT` block, set `jit = off` for that workload. The 19 beta turns JIT off by default |

After any bulk load or restore:

```sql
ANALYZE VERBOSE orders;
```

***PG 18+*** `pg_upgrade` preserves optimizer statistics across a major upgrade by default (disable with `--no-statistics`). It does **not** preserve **extended** statistics. The `CREATE STATISTICS` objects themselves survive the upgrade — their collected data does not, so they read as empty until you `ANALYZE` the tables that own them. Do not recreate the objects; re-`ANALYZE`. On 17 and below nothing is preserved: a major upgrade with no `ANALYZE` afterwards means the whole database plans as if every table were empty, which is a common post-upgrade "the database got slow" report.

***PG 18+*** also changed `VACUUM` and `ANALYZE` to recurse into partitions by default; `ONLY` restores the previous behaviour. A maintenance job that assumed analyzing a partitioned parent was cheap now costs proportionally more.

---

## 10. Index bloat and rebuilding

Index bloat is independent of table bloat. B-tree indexes fragment from update churn even when the table itself is fine.

```sql
-- Dead tuples and when autovacuum last ran
SELECT relname, n_live_tup, n_dead_tup, last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
ORDER BY n_dead_tup DESC
LIMIT 20;

-- Real numbers. pgstattuple scans the whole object — run it off-peak.
CREATE EXTENSION IF NOT EXISTS pgstattuple;
SELECT * FROM pgstattuple('orders');
SELECT * FROM pgstatindex('idx_orders_customer_created');   -- read avg_leaf_density
```

Rebuild:

```sql
REINDEX INDEX CONCURRENTLY idx_orders_customer_created;
```

- `REINDEX INDEX CONCURRENTLY` takes **SHARE UPDATE EXCLUSIVE** — reads and writes continue.
- Plain `REINDEX` takes **ACCESS EXCLUSIVE**. On a production table that is an outage. There is no case where it is the right default.
- `REINDEX … CONCURRENTLY` cannot run inside a transaction block, and needs disk for both copies at once.
- **A failed concurrent reindex is not the same as a failed concurrent create.** The original index normally survives and stays valid; what is left behind is a *second*, invalid index. Read the suffix before dropping anything: `_ccnew` is the transient replacement — drop it and retry. `_ccold` is the original that could not be dropped — the rebuild succeeded, so drop that one. A number may be appended (`_ccnew1`). Dropping the wrong one removes a live production index.

Table bloat needs a rewrite, not a reindex: `VACUUM FULL` and `CLUSTER` take ACCESS EXCLUSIVE, so use `pg_repack` or `pg_squeeze` online. See `ddl-lock-recipes.md` §16.

---

## 11. Retiring unused indexes

Every index costs write throughput, WAL volume, disk, and planning time. An unused index is a permanent tax paid for nothing.

```sql
SELECT s.relname AS table_name,
       s.indexrelname AS index_name,
       s.idx_scan,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS size
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE NOT i.indisunique
  AND NOT i.indisprimary
ORDER BY s.idx_scan, pg_relation_size(s.indexrelid) DESC;
```

Before dropping:

1. **`idx_scan = 0` on the primary and on every replica.** Read replicas serve different queries and the counters are per node. An index dropped because the primary never used it can be the only thing keeping a reporting replica alive.
2. **Confirm the observation window.** Counters reset on crash and on `pg_stat_reset()`. Check `pg_stat_get_db_stat_reset_time(oid)` and make sure the window covers a full business cycle, including month-end and quarter-end jobs.
3. **Never drop an index backing a constraint** — unique, primary key, exclusion, or the referenced side of a foreign key — without dropping the constraint. The filter above excludes unique and primary indexes for exactly this reason; exclusion and FK-referenced indexes still need a manual check.
4. **Check for redundancy rather than disuse.** An index on `(a)` is redundant when `(a, b)` exists, even though both show scans. Drop the narrower one.

```sql
DROP INDEX CONCURRENTLY idx_orders_legacy;
```

Keep the exact `CREATE INDEX` statement in the migration's down path, so restoring it is one command rather than an archaeology session.
