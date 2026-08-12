# MongoDB indexes and aggregation — diagnosis

Read at Gate 6, and whenever a query is slow. Verified against MongoDB 8.3 documentation, 13 August 2026.

---

## 1. Reading `explain("executionStats")`

```js
db.orders.find({ tenant_id: t, status: "PAID" }).sort({ created_at: -1 })
  .explain("executionStats")
```

Four numbers decide everything:

| Field | What it means | What "good" looks like |
|---|---|---|
| `executionStats.nReturned` | documents returned | the baseline |
| `totalKeysExamined` | index entries read | close to `nReturned` |
| `totalDocsExamined` | documents fetched from disk/cache | close to `nReturned`; **0** means the query is covered |
| `executionTimeMillis` | wall clock for the winning plan | compare across plans, not in isolation |

Stage names, worst to best:

- `COLLSCAN` — no index used. Every document read.
- `IXSCAN` → `FETCH` — index used, then documents fetched. Normal.
- `IXSCAN` → `PROJECTION_COVERED` — covered query, no documents fetched. Best.
- `SORT` — an in-memory sort. Capped at 100 MB; spills to disk since 6.0 rather than erroring, so it shows up as latency, not as a failure. A `SORT` stage means the index does not satisfy the sort order.
- `SORT_MERGE` — an `$in` or `$or` with a sort, merged across index bounds. Acceptable.
- `SHARDING_FILTER` — orphan filtering on a sharded cluster. Expected.

Ratio to watch: `totalDocsExamined / nReturned`. Anything above ~10 means the index is not selective enough for this query.

`explain()` on 8.3 also exposes memory tracking (`inUseTrackedMemBytes`, `peakTrackedMemBytes` in `serverStatus`) — use it instead of guessing whether a blocking stage is near the 100 MB cap.

---

## 2. "Why is my index not being used" — the full procedure

Work these in order. Stop at the first hit.

1. **Does the query include the index's leading field?**
   `{a:1,b:1,c:1}` serves `{a}`, `{a,b}`, `{a,b,c}`. It does not serve `{b}`, `{c}`, `{b,c}`. There is no "skip the first column" — a query without `a` cannot use it.

2. **Does the sort order match?**
   `{a:1, b:1}` serves `.sort({a:1,b:1})` and `.sort({a:-1,b:-1})` (whole-index reversal). It does **not** serve `.sort({a:1,b:-1})` — mixed directions need an index with those exact directions.

3. **Collation.** An operation must specify the same collation as the index to use it for string comparison. Default is `simple` (binary). A case-insensitive search against a default index scans the collection.
   ```js
   db.users.createIndex({ email: 1 }, { collation: { locale: "en", strength: 2 } })
   db.users.find({ email: "A@B.com" }).collation({ locale: "en", strength: 2 })  // both sides
   ```

4. **Negation.** `$ne`, `$nin`, `$not` match nearly everything, so the index scan reads nearly every key. The planner often correctly chooses a `COLLSCAN` instead. Rewrite as a positive predicate or an `$in` over the values you do want.

5. **Regex.** Only a left-anchored, case-sensitive regex (`/^prefix/`) produces index bounds. `/substring/` and `/^prefix/i` scan the index end to end. For real text search use a text index or Atlas Search, not a regex.

6. **Type mismatch.** BSON sorts by type before value. `find({age: "30"})` never matches `age: 30`. Also bites `_id`: a string `"66b..."` is not an `ObjectId("66b...")`.

7. **Partial index filter not implied.** The planner uses a partial index only when the query predicate guarantees a subset of `partialFilterExpression`. `find({name:"x"})` cannot use an index filtered on `{email:{$exists:true}}` — add `email: {$exists: true}` to the query, or drop the partial filter.

8. **Multikey limits.** At most one array field per compound index (the server rejects an insert that would create a second). Hashed indexes cannot be multikey. `$expr` does not use multikey indexes. Sorting on an array field usually forces an in-memory sort.

9. **`$or` needs an index per branch.** `find({$or:[{a:1},{b:2}]})` uses indexes only if *every* branch has one; a single unindexed branch turns the whole thing into a collection scan.

10. **The index is not built yet.** `db.c.getIndexes()` and `$currentOp` — a rolling or background build is not usable until it completes.

11. **The plan changed on upgrade.** On 8.3 the Cost-Based Ranker is the default plan selector for eligible queries. A plan that was stable on 8.0 may differ. Re-run `explain()` after a major upgrade, before blaming a code change. `db.c.getPlanCache().clear()` after adding an index if you suspect a cached plan.

**Catch it in CI, not in production:** `setParameter: { notablescan: 1 }` on a test instance makes any query without an index error instead of scanning. `setParameter` is unavailable on Atlas, but the same control ships there as the "Require Indexes for All Queries" cluster setting. Run the local instance in CI; use the Atlas setting on a staging cluster.

---

## 3. ESR worked examples

Order the compound index **E**quality, then **S**ort, then **R**ange.

```js
// Query: equality on tenant, sort by date desc, range on amount
db.orders.find({ tenant_id: t, amount_minor: { $gte: 10000 } }).sort({ created_at: -1 })
db.orders.createIndex({ tenant_id: 1, created_at: -1, amount_minor: 1 })
//                       E              S                R
```

Putting the range before the sort (`{tenant_id:1, amount_minor:1, created_at:-1}`) still uses the index for filtering but forces a `SORT` stage, because once the scan is inside a range the keys are no longer in `created_at` order.

Multiple equality fields go first in any order (put the most selective first for readability; the planner does not care). Multiple range fields: only the first one gets index bounds — the rest are filtered after the fetch.

```js
// Query: two equalities, a sort, and a range
db.events.find({ tenant_id: t, type: "login", ts: { $gte: from } }).sort({ ts: -1 })
db.events.createIndex({ tenant_id: 1, type: 1, ts: -1 })
// ts serves as both S and R here — a sort on the same field as the range is free
```

---

## 4. Covered queries

All of these must hold:

- Every field in the **filter** is in the index.
- Every field in the **projection** is in the index.
- `_id` is explicitly excluded (`{_id: 0}`) unless it is in the index.
- The index is not multikey **for a field being returned**.
- Not against a sharded collection through `mongos` unless the shard key is in the index (the shard filter needs it).

```js
db.users.createIndex({ tenant_id: 1, email: 1, name: 1 })
db.users.find({ tenant_id: t, email: e }, { _id: 0, name: 1 })   // PROJECTION_COVERED
```

`explain()` shows `totalDocsExamined: 0`. On a read-heavy collection this is usually a larger win than any other single change, because it removes the random-IO fetch entirely.

---

## 5. Index cost — what you pay

- Every index is written on every insert, and on every update that touches an indexed field.
- Every index consumes RAM in the working set. An index that does not fit in RAM is random IO per lookup.
- A multikey index stores **one key per array element per document**.
- Minimum ~8 kB of data space per index.
- Collation-aware index keys are larger than plain ones (ICU collation keys).

Rule from the pillar, applied here: **every index names the query it serves.** Delete the ones that do not. `db.c.aggregate([{$indexStats:{}}])` reports the access count per index since the last server restart — an index with `accesses.ops: 0` after a full business cycle is a write tax with no reader. Check it on every replica set member; the counters are per-node.

---

## 6. Aggregation pipeline ordering

The pipeline uses an index only for stages that are still at the front. After the first stage that reshapes documents, nothing downstream can use one.

| Rule | Why |
|---|---|
| `$match` first, as narrow as possible | The only place an index filter can apply |
| `$sort` immediately after `$match` | Can use the same index; otherwise it is a blocking in-memory sort |
| `$limit` immediately after `$sort` | Enables a bounded top-k sort instead of sorting the whole input |
| `$project` / `$addFields` **after** filtering | Reshaping before filtering discards the index |
| `$unwind` as late as possible | It multiplies the document count; every stage after it does more work |
| `$lookup` last, on an indexed foreign field | Without an index it is a collection scan per input document |
| `$group` at the end | Blocking, and 100 MB-capped |

```js
// WRONG — the $match cannot use an index, and $unwind runs over everything
[ { $unwind: "$items" },
  { $match: { status: "PAID", "items.sku": "A-1" } },
  { $group: { _id: "$customer_id", n: { $sum: 1 } } } ]

// RIGHT
[ { $match: { status: "PAID", "items.sku": "A-1" } },   // index: {status:1, "items.sku":1}
  { $sort: { created_at: -1 } },
  { $limit: 1000 },
  { $unwind: "$items" },
  { $match: { "items.sku": "A-1" } },                    // re-filter after unwind
  { $group: { _id: "$customer_id", n: { $sum: 1 } } } ]
```

Note the second `$match` after `$unwind`: the first one selects *documents containing* the sku; only the second selects the *elements*.

---

## 7. Memory and limits

| Limit | Value | Behaviour |
|---|---|---|
| Blocking stage memory | **100 MB** | `$group`, `$sort` (unindexed), `$bucket`, `$bucketAuto`, `$setWindowFields`, `$sortByCount` |
| `allowDiskUseByDefault` | **`true`** since 6.0 | Over the cap, the stage spills to temporary files instead of erroring. The failure becomes latency, not an exception — alert on execution time, not on error rate |
| `$search` | exempt | Runs in a separate process |
| Pipeline stages | **1000** max | |
| Returned document | **16 MiB** | A `$group` that accumulates everything into one document hits this |
| Atlas M0 / Flex | `allowDiskUse` **ignored**; `aggregate` capped at `maxTimeMS` 300 s | A pipeline that works locally can fail only on the free tier |

`allowDiskUse: false` on a specific command turns silent spilling back into a loud error — useful in tests to catch a pipeline that outgrew its budget.

---

## 8. `$lookup`

```js
{ $lookup: { from: "customers", localField: "customer_id",
             foreignField: "_id", as: "customer" } }
```

- **Index the `foreignField`.** Unindexed, this is one full scan of `customers` per input document.
- `$lookup` cannot use an index for the `let`/`pipeline` form unless the inner pipeline's `$match` is index-eligible on its own.
- On a sharded cluster, `$lookup` against a sharded collection is supported but pays a scatter-gather per lookup.
- **A `$lookup` on a hot read path is a schema finding.** Raise it at Gate 5: either embed, or use the extended reference pattern (`references/schema-patterns.md`). Query tuning cannot fix a model that requires a join on every page load.

`$graphLookup` is the recursive form and is memory-bound in the same 100 MB envelope (over the cap it spills to disk unless `allowDiskUse: false`, which turns it into an error). Since **5.1 its `from` collection may be sharded**; the remaining restriction is that you cannot run it against a sharded collection inside a transaction.
