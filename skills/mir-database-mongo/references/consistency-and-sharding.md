# MongoDB consistency, transactions, and sharding — runbook

Read at Gate 5. Verified against MongoDB 8.3 documentation, 13 August 2026.

---

## 1. Write and read concern per operation class

Pick per operation, not once per application. Put the table in the design review.

| Operation class | Write concern | Read concern | Read preference | Why |
|---|---|---|---|---|
| Money, balances, ledger | `{w:"majority", j:true}` | `"majority"` (or `"snapshot"` in a transaction) | `primary` | Must survive a failover and a crash |
| State transitions (order status, subscription) | `{w:"majority"}` | `"majority"` | `primary` | A rolled-back transition corrupts the state machine |
| User-visible CRUD | `{w:"majority"}` | `"local"` on the primary is fine | `primary` | The default. Do not downgrade to `w:1` for latency |
| Read-after-write off a replica | `{w:"majority"}` | `"majority"` **in a causal session** | `secondary` | The only combination that gives read-your-writes off a secondary |
| Analytics / reporting | n/a | `"local"` or `"available"` | `secondaryPreferred` | Stale is acceptable — write that down |
| Telemetry, metrics, best-effort logs | `{w:1}` | `"local"` | any | The only place `w:1` is defensible |
| Anything | never `{w:0}` | — | — | No error is reported and no operation time is returned, so causal consistency does not apply |

**The `w:1` failure, concretely:** the client writes with `w:1`, the primary acknowledges, the application returns HTTP 200 and sends a confirmation email. The primary loses network before any secondary replicates. A new primary is elected. The old primary rejoins, rolls the write back into a rollback file, and the document does not exist. Nothing in the application ever hears about it.

**The implicit default is `{w:"majority"}` — with one exception.** On a topology with arbiters where the data-bearing members do not exceed the voting majority (the classic Primary-Secondary-Arbiter set), the default silently becomes `{w:1}`. Check `db.adminCommand({getDefaultRWConcern:1})`. Prefer P-S-S; a P-S-A set also cannot satisfy `w:"majority"` while one data-bearing member is down.

**MongoDB 8.0 changed two things.** `w:"majority"` members now apply changes asynchronously after durably writing the oplog entry (previously the server waited for the apply). And `j:true` now requires every member counted by `w` to journal, where before 8.0 only the primary did — so `{w:"majority", j:true}` is stronger and slower on 8.0+ than the same string was on 7.0.

---

## 2. Read concern levels

| Level | Guarantee | Cost |
|---|---|---|
| `"local"` (default) | Returns the node's most recent data. **May be rolled back** | none |
| `"available"` | Like `local` but on a sharded cluster skips the orphan filter — can return orphaned documents | lowest latency, weakest |
| `"majority"` | Only data acknowledged by a majority. Will not be rolled back | requires majority-committed snapshot |
| `"snapshot"` | A single consistent point-in-time view across the whole operation | transactions and long pipelines |
| `"linearizable"` | Real-time correct on a single document; reflects every write that completed before it started | **primary only**, single-document reads only, slow. Always pair with `maxTimeMS` |

The default isolation is read-uncommitted: **a client using `"local"` can see a write before that write has been acknowledged to its own author.** If your test asserts "the other request cannot see this yet," it is asserting something MongoDB does not promise.

---

## 3. Causally consistent sessions

```js
const session = client.startSession({ causalConsistency: true });   // default is true
await orders.updateOne({ _id }, { $set: { status: "PAID" } },
                       { session, writeConcern: { w: "majority" } });
const doc = await orders.findOne({ _id },
                       { session, readConcern: { level: "majority" },
                         readPreference: "secondary" });   // sees the write
await session.endSession();
```

Guarantees, only with `w:"majority"` **and** `readConcern:"majority"`: read-your-writes, monotonic reads, monotonic writes, writes-follow-reads.

Constraints:

- **One thread per session at a time.** Sharing a session across concurrent tasks breaks the ordering it exists to provide, and in most drivers throws.
- `w:0` writes return no operation time, so they establish no causal relationship at all.
- Operations inside a causal session are **not isolated** from operations outside it. This is ordering, not a transaction.
- The session must be threaded through every call. A helper that quietly opens its own connection loses the guarantee with no error.

---

## 4. Transactions — the retry loop

Do not hand-roll it. Every driver ships `withTransaction`, which handles both retryable error labels:

```js
const session = client.startSession();
try {
  await session.withTransaction(async () => {
    await accounts.updateOne({ _id: from, balance_minor: { $gte: amt } },
                             { $inc: { balance_minor: -amt } }, { session });
    const r = await accounts.updateOne({ _id: to },
                             { $inc: { balance_minor:  amt } }, { session });
    if (r.matchedCount === 0) throw new Error("destination missing");  // aborts
  }, { readConcern: { level: "snapshot" },
       writeConcern: { w: "majority" },
       readPreference: "primary" });
} finally { await session.endSession(); }
```

What the helper retries:

- **`TransientTransactionError`** — the whole transaction is retried from the top. Causes: a write conflict, a primary election, `maxTransactionLockRequestTimeoutMillis` (5 ms default) expiring under contention.
- **`UnknownTransactionCommitResult`** — the *commit* is retried. The commit is idempotent, so this is safe; a hand-written loop that retries the whole transaction here can double-apply.

Rules:

- Every operation inside must be passed the `session`. An operation without it runs outside the transaction and is not rolled back. This is the most common transaction bug in application code.
- The body must be **idempotent**, because it can run more than once.
- **No external side effects inside** — no HTTP call, no email, no queue publish. A rollback cannot un-send them. Do the external effect after commit, guarded by an idempotency key (that is `mir-backend` territory).
- Keep it under the **60-second** `transactionLifetimeLimitSeconds`. Atlas does not accept `setParameter`, but it does expose this value as the "Set Transaction Lifetime" cluster setting (minimum 1 s), so it is raisable there — through the cluster config, not the shell.
- Each oplog entry stays capped at 16 MiB, but that is **not** a cap on the transaction: since 4.2 MongoDB writes as many oplog entries as the transaction needs. What actually kills an oversized transaction is the WiredTiger cache — `TransactionTooLargeForCache`. Split the work.
- Writes inside a transaction are **not** retryable writes; the commit and abort are.

**Before writing any of this, check whether the change fits in one document.** A single-document update is atomic across every field and array element, needs no session, no snapshot read concern, no retry loop, and no lock timeout. Most transactions in Mongo codebases are compensating for an embed-vs-reference decision made wrong earlier.

---

## 5. Concurrency without transactions

| Need | Mechanism | Note |
|---|---|---|
| Counter, balance | `$inc` with a guard in the filter | `updateOne({_id, bal: {$gte: n}}, {$inc: {bal: -n}})` — check `matchedCount` |
| Claim a work item | `findOneAndUpdate` with `sort` and `returnDocument:"after"` | Atomic dequeue; add a `lease_until` so a dead worker's item is reclaimed |
| Prevent lost update | Version field: filter on `version: seen`, `$inc: {version: 1}` | `matchedCount === 0` is a conflict — reload and retry, do not overwrite |
| State transition | Filter on the current state: `{_id, status:"PENDING"}` | Concurrent transitions: exactly one wins |
| Create-or-get | `updateOne(..., {upsert:true})` **plus a `unique` index** | Two concurrent upserts can both insert; the unique index turns the loser into `E11000`. Catch code 11000 and retry once as a plain update |
| Bounded array | `$push` with `$slice` | Server-side; the equivalent read-modify-write in the app is a race |

`matchedCount` / `modifiedCount` is the return value that matters. An update whose filter matched nothing is a success at the protocol level and a silent no-op in your business logic.

---

## 6. Retryable writes — what is and is not covered

Enabled by default on drivers for MongoDB 4.2+. One automatic retry by default (more if `timeoutMS` is set, until the timeout).

| Retryable | Not retryable |
|---|---|
| `insertOne`, `insertMany` | **`updateMany`** |
| `updateOne`, `replaceOne` | **`deleteMany`** |
| `deleteOne` | any `bulkWrite` containing a multi-document write |
| `findAndModify` and the `findOneAnd*` family | individual writes inside a transaction (commit/abort *are* retryable) |
| `bulkWrite` of single-document operations only | anything with `w:0`, or on a standalone `mongod` |

Consequences to design around:

- A batch job built on `updateMany` gets no retry through an election. Batch on `_id` ranges with `updateOne`, or make the job re-runnable and re-run it.
- Since 6.1, a write that failed both attempts without applying anything carries the `NoWritesPerformed` label — that is how you distinguish "nothing happened" from "part of the batch happened."
- Writes to the `local` database fail while retryable writes are enabled.
- Retryable writes are deduplicated by the *server* using the statement id. Your own retry loop is a different statement and gets no such protection — that needs an idempotency key.

---

## 7. Shard key selection

Three properties, all required:

| Property | Failure if wrong | Check |
|---|---|---|
| **Cardinality** | Few distinct values caps the number of useful shards. `{country:1}` limits you to the number of countries | `db.c.distinct(field).length`, or `analyzeShardKey` |
| **Frequency** | One dominant value creates a jumbo chunk that cannot be split or migrated | Value histogram |
| **Monotonicity** | **A monotonic key routes every insert to the chunk with `maxKey` as its upper bound — one shard takes all writes** | Is it a timestamp, an ObjectId `_id`, or a counter? |

The fourth, unlisted property is **query targeting**: if the shard key is not in your common query predicate, every query is a scatter-gather to every shard. Hashed sharding distributes writes perfectly and destroys range targeting.

```js
// WRONG — monotonic; one hot shard
sh.shardCollection("app.events", { created_at: 1 })

// RIGHT — leading high-cardinality, low-frequency field; range queries within a tenant stay targeted
sh.shardCollection("app.events", { tenant_id: 1, created_at: 1 })

// ALSO RIGHT when there is no natural leading field — even write spread, no range targeting
sh.shardCollection("app.events", { _id: "hashed" })
```

On 7.0+, measure instead of arguing:

```js
db.events.configureQueryAnalyzer({ mode: "full", samplesPerSecond: 5 })   // let it run
db.events.analyzeShardKey({ key: { tenant_id: 1, created_at: 1 } })
// → cardinality, frequency, monotonicity, and read/write distribution from the real workload
```

Notes: the shard key value **can** be changed on a document unless the key is `_id`. Documents missing a shard-key field land in the same chunk range as null values. A `unique` index on a sharded collection must be prefixed by the shard key. A multikey index cannot be the shard-key index.

---

## 8. Chunk migration and the balancer

- Default range size **128 MB**. The balancer moves a range only once two shards differ by **3× that (384 MB)** for the collection.
- A shard participates in at most one migration at a time → at most **n/2** concurrent migrations for *n* shards.
- Each migration: destination builds indexes, copies documents, syncs the tail, then the source **briefly pauses reads and writes on the collection** while the config servers are updated. The pause is short but real — it is not a background-only operation.
- Orphan cleanup on the source is asynchronous; since 8.2 the balancer does not wait for the delete phase before starting the next migration, so deletes can lag behind.
- Bulk-loading into a badly sharded collection means writing the data twice: once on insert, once on rebalance. Pre-split ranges, or shard before loading.
- `sh.shardAndDistributeCollection()` (8.0+) shards and distributes in one step instead of waiting on the balancer.

---

## 9. Resharding

`reshardCollection` exists from 5.0, so the key is reversible. Plan as though it is not — not because you cannot change it, but because of what changing it costs and what has to be true before you may start:

**Preconditions**

- Free storage per shard: `((collection_storage_size + index_size) × 2) / shard_count`. A 2 TB collection with 400 GB of indexes across 4 shards needs **1.2 TB free on every shard**.
- I/O below 50%, CPU below 80%.
- `writeConcernMajorityJournalDefault: true`.
- Balancer disabled; no index builds running (`$currentOp`).
- The application must tolerate **~2 seconds of blocked writes** on that collection at the end.

**During and after**

- Minimum **5 minutes**; large collections take hours. **One collection at a time.**
- `collMod`, `convertToCapped`, `createIndexes`, `drop`, `dropIndexes`, `renameCollection` block or fail on that collection.
- Search indexes are unavailable afterwards and must be rebuilt manually.
- Queryable Encryption collections cannot be resharded.
- Retryable writes started before or during resharding can retry for up to 5 minutes after it completes; after that they fail with `IncompleteTransactionHistory`.
- On Atlas: **M30+ sharded clusters only**.

```js
db.adminCommand({ reshardCollection: "app.events", key: { tenant_id: 1, created_at: 1 } })

// progress
db.getSiblingDB("admin").aggregate([
  { $currentOp: { allUsers: true, localOps: false } },
  { $match: { type: "op", "originatingCommand.reshardCollection": "app.events" } }
])
// → totalOperationTimeElapsedSecs, remainingOperationTimeEstimatedSecs (-1 at the start)
```

**Cheaper alternatives:** `refineCollectionShardKey` appends suffix fields to the existing key without redistributing data. It fixes an *incomplete* key (`{tenant_id:1}` → `{tenant_id:1, created_at:1}`); it cannot fix a *wrong* one. On 8.0+, `reshardCollection` with `forceRedistribution: true` redistributes on the same key (to take up new shards or zones) without changing it, and `unshardCollection` moves a collection back onto a single shard.

**Application compatibility during the operation.** Writes keep flowing throughout, so the deployed code must issue filters that stay targetable under *both* keys — otherwise targeted updates become scatter-gather at exactly the moment the cluster is least able to absorb it. Ship the query changes before the reshard, not with it.

---

## 10. Atlas differences that change the design

| Concern | Self-hosted | Atlas |
|---|---|---|
| `setParameter` | available | **unsupported at every tier, M10+ included** — but `transactionLifetimeLimitSeconds` ("Set Transaction Lifetime", min 1 s), `notablescan` ("Require Indexes for All Queries") and server-side JS are exposed as cluster settings. Sort-memory limits are fixed |
| `db.createUser` / `db.createRole` | available | unsupported on M10+; use Atlas database users and roles (API, not Mongo commands) |
| `setDefaultRWConcern` | available | unsupported on M10+ — set concerns per operation or in the connection string |
| `allowDiskUse` | honoured | **ignored on M0/Flex**; `aggregate` capped at `maxTimeMS` 300 s there |
| `$where`, `$function`, `$accumulator`, `mapReduce` | controlled by `security.javascriptEnabled` | unavailable on M0/Flex |
| `replSetStepDown`, `replSetReconfig`, `rs.*` | available | unsupported — Atlas manages the topology |
| `fsync`, `compact`, `validate`, `applyOps`, `shutdown` | available | unsupported (M0/Flex) or restricted (M10+) |
| `db.killOp()` | any operation | only operations run by the same user |
| `reshardCollection`, `unshardCollection` | available | M30+ sharded clusters only |
| Patching | yours | managed, but you still pick the major and the window |

Anything in a runbook that calls `setParameter` or an `rs.*` command does not exist on Atlas. Find that out at Gate 5, not during the incident.
