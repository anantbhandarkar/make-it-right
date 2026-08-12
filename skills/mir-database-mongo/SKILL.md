---
name: mir-database-mongo
description: "Make It Right (MongoDB engine module). MongoDB 8.x document-store mechanics that the engine-independent database pillar deliberately omits: embed-vs-reference decided per relationship by access pattern, cardinality and update frequency; the 16 MB BSON limit and the unbounded-array antipattern; bucket and extended-reference patterns; $jsonSchema validators (a collection accepts any shape until you add one); write concern w:1 silently losing acknowledged writes on failover; read concern levels, stale secondary reads, causal sessions, retryable writes (updateMany/deleteMany are NOT retryable); the 60-second transactionLifetimeLimitSeconds and when a single-document atomic update is the right answer instead of a transaction; findAndModify, version-field optimistic concurrency, $inc vs read-then-write, upsert duplicate-key races; compound-index prefix rules, ESR ordering, multikey/partial/TTL limits, covered queries, collation mismatch, why an index is not used; aggregation stage ordering, the 100 MB per-stage limit, allowDiskUse, $lookup cost; shard-key selection (a monotonic key creates a hot shard) and what reshardCollection actually costs; and NoSQL operator injection via a {\"$ne\": null} object arriving where a string was expected. TRIGGER only when the datastore is MongoDB itself (Community, Enterprise, or Atlas) — designing a collection or document schema, picking a shard key, writing an aggregation pipeline or index, or debugging a Mongo consistency, concurrency, or query-plan problem. Wire-compatible services (Amazon DocumentDB, Cosmos DB Mongo API, FerretDB) implement a subset: the document modeling transfers, but every server mechanic here — shard commands, transaction and concern semantics, aggregation support, server parameters, CVE tables — must be checked against that service's compatibility matrix first. Loads TOGETHER WITH mir-database, which owns the gates. SKIP for relational engines (Postgres, MySQL, SQL Server — each gets its own mir-database-<engine> module), for DynamoDB/Cassandra/Redis, for application-layer transaction, retry and idempotency design (mir-backend and its framework modules), for ODM/driver wiring (Mongoose, Beanie, Spring Data MongoDB — framework-module territory), and for analytics or warehouse modeling."
trigger: /mir-database-mongo
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-database-mongo · Make It Right (MongoDB)

Engine module. `mir-database` runs the gates and decides what is true of the data. **This file carries only MongoDB's mechanics.** Use it at Gate 0 (engine fitness), Gate 5 (design), Gate 6 (collections, validators, indexes), Gate 7 (review).

**Release state, verified 13 Aug 2026** (mongodb.com/docs/manual/release-notes, /reference/versioning):

| Release | Status | What it changes for you |
|---|---|---|
| **8.3** (8.3.8, Aug 2026) | current | Cost-Based Ranker is now the **default** plan selector — query plans can change on upgrade. `removeShard` replaced by `startShardDraining` / `shardDrainingStatus` / `commitShardRemoval`. Only single-version downgrades |
| **8.2** | supported minor | Queryable Encryption prefix/suffix/substring queries are **public preview** — not for production; GA will be incompatible with the preview format |
| **8.0** | major, 5-year lifecycle | `j:true` now requires **every member counted by `w`** to journal (pre-8.0 only the primary did). `w:"majority"` members apply asynchronously after durably writing the oplog entry |
| **7.0** | major, supported | `analyzeShardKey` / `configureQueryAnalyzer` land here |

Majors ship every 2 years with a 5-year lifecycle. Minors (8.1 → 8.2 → 8.3) must be upgraded through **sequentially**, each step a binary upgrade **plus** an FCV bump. Pin a major if you need Atlas Live Migration or `mongosync` — minors do not always support them.

---

## 1. Schema design

**Embed vs reference, decided per relationship — not per project.**

| Signal | Embed | Reference |
|---|---|---|
| Access pattern | Child is read every time the parent is | Child is queried on its own |
| Cardinality | One-to-few, with a bound you can name | Unbounded, or many-to-many |
| Update frequency | Both change in the same write | Child changes far more often |
| Lifecycle | Child has no meaning without the parent | Child is shared, or outlives the parent |
| Atomicity | They must change together (single-document writes are atomic; cross-document ones are not) | Eventual consistency is acceptable |

**If you cannot state the maximum number of elements an array will ever hold, do not embed it.**

### The 16 MB limit and the unbounded array

A BSON document cannot exceed **16 MiB**. That hard limit is not what hurts first:

- **Every update rewrites the whole document.** A 4 MB document costs 4 MB of write and oplog per `$push`. Replication lag follows.
- **A multikey index stores one key per array element per document.** A 50 k-element array is 50 k index keys for one document.
- **`$push` works until it doesn't.** The failure is `BSONObjectTooLarge` on a document a customer has used for a year, and the fix is a data migration, not a deploy. On 8.3 an oversized upsert returns error code `10334`.

Three fixes, full shapes in `references/schema-patterns.md`:

| Pattern | Use when | Shape |
|---|---|---|
| **Reference** | The child set is unbounded (orders, events, messages) | Child collection with an indexed `parent_id` |
| **Bucket** | High-volume time-ordered writes (readings, ticks, log lines) | One document per (source, hour) with a **bounded** array plus `count`/`min`/`max`. The bound is enforced by putting `count: {$lt: N}` in the upsert filter |
| **Subset** | Only the newest few are ever displayed | Embed the last N with `$push` + `$slice`; keep all of them in a separate collection |

### Extended reference — the correct denormalization

A `$lookup` on every read of a hot path means the schema is wrong. Copy the display fields next to the reference: `{ customer_id: ObjectId("c1"), customer: { name: "Ada Lovelace", tier: "gold" } }`. Copy only fields that are immutable or that you are content to show stale. Name the authoritative copy in the Assumption Ledger, and name the mechanism that updates the copies (change stream, scheduled job, or "never — it is a snapshot"). An extended reference with no update mechanism and no snapshot decision is a bug with a schedule.

### `$jsonSchema` — a collection accepts anything until you add one

A new collection has **no validation**. Any document, any types, any fields. No `NOT NULL`, no `CHECK`, no column list. The only server-side enforcement is a validator:

```js
db.createCollection("orders", {
  validator: { $jsonSchema: {
    bsonType: "object", required: ["tenant_id","status","total_minor","currency"],
    properties: {
      _id:         { bsonType: "objectId" },              // omit this and EVERY insert fails
      tenant_id:   { bsonType: "objectId" },
      status:      { enum: ["PENDING","PAID","FULFILLED","REFUNDED"] },
      total_minor: { bsonType: "long", minimum: 0 },      // minor units, never "double"
      currency:    { bsonType: "string", pattern: "^[A-Z]{3}$" },
      tags:        { bsonType: "array", maxItems: 20 }    // the unbounded-array guard
    },
    additionalProperties: false                            // the mass-assignment guard
  }},
  validationLevel: "strict", validationAction: "error"     // both are the defaults
})
```

- **Adding a validator does not scan or reject what is already there.** Violating documents stay. Find them first: `db.c.find({ $nor: [ { $jsonSchema: <schema> } ] })`. Change a validator with `collMod`, not by recreating the collection.
- Use `bsonType`, not JSON Schema's `type` — `"number"` accepts a double where you meant `long`. This is JSON Schema **draft 4** with MongoDB's own extensions and omissions.
- **`additionalProperties: false` without `_id` in `properties` rejects every insert**, because the server adds `_id` and the validator has not been told it is allowed.
- `validationAction: "warn"` writes the bad document and logs. It is a migration tool, not a destination. Validators cannot be set on `admin`, `local`, `config`, or system collections.
- **Uniqueness is not a validator concern** — it is a `unique: true` index, and without one concurrent inserts produce duplicates.

---

## 2. Consistency

### `w:1` loses acknowledged writes

`w:1` means "the primary wrote it." If that primary steps down before a secondary replicates, the write is rolled back on rejoin — **after your application already returned 200.**

| Setting | Guarantee | Use for |
|---|---|---|
| `w:1` | Primary only. **Rollback-able on failover** | Telemetry, best-effort metrics. Nothing a user is told succeeded |
| `w:"majority"` | A majority of data-bearing voting members durably wrote the oplog entry | The default answer. Money, state transitions, user-visible writes |
| `w:"majority", j:true` | Plus on-disk journal on every member counted by `w` (8.0+ behaviour) | Writes that must survive a simultaneous majority crash |
| `w:0` | Fire and forget; no error, no operation time returned, so **causal consistency does not apply** | Nothing |

The **implicit default is `{w:"majority"}` — except** on a topology with arbiters where the data-bearing members do not exceed the voting majority (a Primary-Secondary-Arbiter set), where it silently becomes `{w:1}`. If you run P-S-A you are on `w:1` and probably do not know it. Check `db.adminCommand({getDefaultRWConcern:1})`.

### Read concern and stale secondary reads

Default read concern is `"local"`: **you can read a write that has not been acknowledged to its own writer and may still be rolled back.** `"majority"` returns only what survived. `"snapshot"` gives a point-in-time view. `"linearizable"` (primary, single document) is real-time correct and slow.

Read preference is separate. `secondary` / `secondaryPreferred` / `nearest` read asynchronously replicated data, so **a read-after-write against a secondary can legitimately return the old value.** The usual cause is a `readPreference=secondaryPreferred` added to the connection string for load and never scoped to the reads that tolerate it.

**To read your own writes off a secondary** you need all three: a causally consistent session, `w:"majority"` on the write, `readConcern:"majority"` on the read. Two constraints: **only one thread may use a session at a time**, and `w:0` writes establish no causal relationship. Code in `references/consistency-and-sharding.md`.

### Retryable writes are narrower than you think

Drivers for MongoDB 4.2+ set `retryWrites=true` by default: one automatic retry through an election or transient network error, on **single-document** writes only. **`updateMany`, `deleteMany`, and any bulk operation containing them are not retryable.** Neither are individual writes inside a transaction (the commit and abort are). Requires a replica set or sharded cluster and `w` ≠ 0. Since 6.1 a total failure carries the `NoWritesPerformed` label, so you can tell "nothing happened" from "part of the batch happened." A batch job built on `updateMany` gets no retry through an election — batch on `_id` ranges with `updateOne`, or make the job re-runnable.

Retryable is not idempotent: the server deduplicates by statement id. Your own retry loop is a new statement with no such protection — that is where `mir-backend`'s idempotency key belongs.

---

## 3. Transactions

Multi-document transactions work, and are the wrong first answer most of the time.

| Limit | Value | Consequence |
|---|---|---|
| `transactionLifetimeLimitSeconds` | **60 s** | A long transaction is killed mid-flight. Atlas blocks arbitrary `setParameter`, but **does expose this one** as the "Set Transaction Lifetime" cluster setting (minimum 1 s) — raise it there, not with `setParameter` |
| `maxTransactionLockRequestTimeoutMillis` | **5 ms** | Under contention transactions abort rather than queue; you must handle `TransientTransactionError` |
| Oplog entry | 16 MiB **each** | Not a cap on the transaction. Since 4.2 MongoDB emits as many oplog entries as it needs, so the 16 MiB total-per-transaction limit is gone. The real ceiling is the WiredTiger cache: an oversized transaction aborts with `TransactionTooLargeForCache` |

**Before writing one, ask whether the change fits in a single document.** A single-document write is atomic across every field and every embedded array element — no session, no snapshot read concern, no retry loop, no lock timeout. Most "I need a transaction" cases are an embed-vs-reference decision made wrong two weeks earlier.

When the write genuinely spans referenced documents (a ledger entry plus a balance), use the driver's `withTransaction` helper so `TransientTransactionError` (retry the whole body) and `UnknownTransactionCommitResult` (retry only the commit) are handled correctly. Pass the `session` to **every** operation inside — one that omits it runs outside the transaction and is not rolled back, the single most common transaction bug. Keep the body idempotent; it can run twice. No HTTP call, email, or queue publish inside — a rollback cannot un-send them.

---

## 4. Concurrency

**Read-modify-write is a race.** `findOne` → mutate in application code → `replaceOne` loses the other writer's update whenever two requests overlap. Four correct mechanisms:

```js
// 1. Atomic operator with a guard — the server does the arithmetic. No read, no race.
db.accounts.updateOne({ _id, balance_minor: { $gte: 500 } }, { $inc: { balance_minor: -500 } })

// 2. findAndModify — atomic read-modify-write that returns the document
db.jobs.findOneAndUpdate({ status: "QUEUED" }, { $set: { status: "RUNNING", worker: wid } },
                         { sort: { priority: -1 }, returnDocument: "after" })

// 3. Optimistic concurrency — a version field
const r = db.docs.updateOne({ _id, version: seen }, { $set: patch, $inc: { version: 1 } })
if (r.matchedCount === 0) throw new ConflictError()   // someone wrote first: reload and retry

// 4. Guarded state transition — the same shape, on a status
db.orders.updateOne({ _id, status: "PENDING" }, { $set: { status: "PAID" } })
```

**Always check `matchedCount` / `modifiedCount`.** An update whose filter matched nothing returns success. Code that ignores it treats "someone else already paid this order" as "payment applied."

**Upserts race.** Two concurrent `updateOne(..., {upsert:true})` on the same key can both find no match and both insert. Without a `unique` index both succeed and you have two documents. With one, the loser gets `E11000 duplicate key` — that error is the correct outcome, and the handler is: catch code 11000, retry once as a plain update. Do not "fix" it by dropping the index.

`$inc`, `$push` with `$slice`, `$addToSet`, and `arrayFilters` are all server-side and race-free. The equivalent read-modify-write in application code is not.

---

## 5. Indexes

**Prefix rule.** `{a:1,b:1,c:1}` serves queries on `{a}`, `{a,b}`, `{a,b,c}` — any prefix. It does **not** serve `{b}`, `{c}`, or `{b,c}`. A query that omits the leading field cannot use it.

**ESR ordering.** Within the index: **E**quality fields, then the **S**ort field, then **R**ange fields. Range before sort is what turns an index-ordered scan into an in-memory `SORT` stage.

```js
// find({ tenant_id: t, amount_minor: {$gte: n} }).sort({ created_at: -1 })
db.orders.createIndex({ tenant_id: 1, created_at: -1, amount_minor: 1 })   // E, S, R
```

| Index type | The constraint that catches people |
|---|---|
| **Multikey** (any indexed array field) | **At most one array field per compound index** — the server rejects an insert that would create a second. Cannot be a shard-key index; hashed cannot be multikey; `$expr` does not use multikey. Covers a query only if the array field is not projected and there is no `$elemMatch` |
| **Partial** | The planner uses it **only if the query predicate guarantees a subset of `partialFilterExpression`**. `find({name:"x"})` cannot use an index filtered on `{email:{$exists:true}}`. Cannot combine with `sparse`; prefer it over `sparse` in all new work |
| **TTL** | **Single-field only** — `expireAfterSeconds` is silently ignored on a compound index. Field must be a Date (or array of Dates); a document missing it never expires. The reaper runs **every 60 s** and **only on the primary**, so expiry is a floor, not an SLA. Adding a TTL index to a populated collection, or lowering `expireAfterSeconds`, makes the next pass delete every already-expired document at once — replication lag and cache pressure. Pre-delete in bounded batches first |
| **Unique** | The only uniqueness enforcement there is. On a sharded collection it must be prefixed by the shard key |
| **Collation** | An operation must specify **the same collation as the index** to use it for string comparison. Default is `simple` (binary), so a case-insensitive query against a default index scans the collection |

**Covered query:** every field in the filter *and* the projection is in the index, and `_id` is explicitly excluded unless indexed. `explain()` shows `totalDocsExamined: 0`. Usually the largest single win on a read-heavy collection.

**Why the index is not used** — work this before adding another one. Full procedure with `explain("executionStats")` in `references/indexes-and-aggregation.md`: the query omits the leading field (prefix rule) or the sort has mixed directions the index lacks · query collation ≠ index collation · `$ne`/`$nin`/`$not` scan nearly the whole index so the planner correctly prefers a collection scan · an unanchored or case-insensitive regex has no index bounds · type mismatch, because BSON compares by type first, so `"123"` never matches `123` · the predicate does not imply a partial index's filter · one branch of an `$or` is unindexed, so the whole query scans · on 8.3 the Cost-Based Ranker is the default plan selector, so re-check `explain()` after a major upgrade before blaming a code change.

`setParameter: {notablescan: 1}` on a CI instance turns any unindexed query into an error. On Atlas the same control exists as the "Require Indexes for All Queries" cluster setting — it is not reachable via `setParameter`, but it is not unavailable either.

---

## 6. Aggregation

**Stage order decides whether an index is used at all.** `$match` and `$sort` use an index only while they are still at the front. The optimizer hoists a `$match` up past `$project`, `$addFields` and `$unwind` **only for the predicates that do not reference what those stages produce** — so the written order is not the executed order, and a predicate on a computed or unwound field can never be hoisted. That one is stuck behind the reshape, with no index.

```js
// WRONG — the predicate is on the unwound path, so it cannot be hoisted and scans everything
[ { $unwind: "$items" }, { $match: { "items.sku": "A-1" } }, ... ]
// RIGHT — filter and sort on indexed document fields first, narrow, then reshape
[ { $match: { status: "PAID", created_at: { $gte: d } } },
  { $sort: { created_at: -1 } }, { $limit: 1000 }, { $unwind: "$items" },
  { $match: { "items.sku": "A-1" } }, ... ]
```

Read the optimized pipeline out of `explain()` rather than inferring index use from the stages you typed.

`$limit` immediately after `$sort` gives a bounded top-k sort instead of sorting everything.

**Memory.** Blocking stages (`$group`, unindexed `$sort`, `$bucket`, `$bucketAuto`, `$setWindowFields`, `$sortByCount`) are capped at **100 MB of RAM**. Since 6.0 `allowDiskUseByDefault` is `true`, so they **spill to disk instead of erroring** — the failure becomes latency nobody notices, so alert on execution time, not error rate. `$search` is exempt. Max **1000 stages**; each returned document still under 16 MiB. On 8.3, `serverStatus` exposes `inUseTrackedMemBytes` / `peakTrackedMemBytes`. On Atlas M0/Flex, `allowDiskUse` is **ignored** and `aggregate` is capped at `maxTimeMS` 300 s.

**`$lookup` is the join you said you did not need.** Without an index on the foreign field it is a collection scan **per input document**. Index the foreign field, place `$lookup` as late as possible, and never run one inside a loop over results. **A `$lookup` on a hot read path is a schema finding, not a tuning task** — raise it at Gate 5 and embed or use an extended reference.

---

## 7. Operations

### Shard key — reversible since 5.0, never cheaply

Choose on three properties: **cardinality** (few distinct values caps how many shards can help — `{country:1}` limits you to the number of countries), **frequency** (a dominant value creates a jumbo chunk that cannot be split or migrated), and **monotonicity**.

**A monotonically increasing shard key sends every insert to the chunk with `maxKey` as its upper bound — one shard takes 100% of the writes while the rest idle.** `_id` (ObjectId is time-prefixed), timestamps and counters are all monotonic.

```js
sh.shardCollection("app.events", { created_at: 1 })              // WRONG — hot shard
sh.shardCollection("app.events", { tenant_id: 1, created_at: 1 }) // RIGHT — spread, still range-targeted
sh.shardCollection("app.events", { _id: "hashed" })               // even spread, no range targeting
```

The fourth, unlisted property is **query targeting**: a shard key absent from your common predicate makes every query a scatter-gather. On 7.0+, measure instead of arguing — `configureQueryAnalyzer()` then `analyzeShardKey()` reports cardinality, frequency, monotonicity and read/write distribution from the real workload. Run it at Gate 5.

**Chunk migration is not free.** Default range size **128 MB**; the balancer acts only once two shards differ by 3× that (384 MB). A shard participates in one migration at a time, so *n* shards give at most *n*/2 concurrent migrations. Each one copies documents, builds destination indexes, and **briefly pauses reads and writes on the source** while config servers update. Bulk-loading into a badly sharded collection means writing the data twice — shard before you load.

**`reshardCollection` (5.0+) is not a cheap undo.** It needs `((collection_storage + index_size) × 2) / shard_count` free on **every** shard, I/O under 50%, CPU under 80%, the balancer disabled and no index builds running; it blocks writes ~2 seconds at the end, takes a **minimum of 5 minutes**, runs on one collection at a time, forces a search-index rebuild, cannot touch Queryable Encryption collections, and on Atlas needs **M30+ sharded**. Two cheaper escapes exist: `refineCollectionShardKey` appends suffix fields without moving data when the key is merely *incomplete* rather than wrong, and on 8.0+ `unshardCollection` returns a collection to a single shard. Budget the resharding cost at Gate 5 as if you will need it, because the alternative is discovering the preconditions during an incident. Runbook in `references/consistency-and-sharding.md`.

### Atlas vs self-hosted — what changes the design

| Concern | Atlas restriction |
|---|---|
| `setParameter` | **Unsupported at every tier, M10+ included** — but three of the parameters people reach for are exposed as cluster settings instead: "Set Transaction Lifetime" (`transactionLifetimeLimitSeconds`), "Require Indexes for All Queries" (`notablescan`), and "Allow Server-Side JavaScript". Sort-memory limits are genuinely fixed |
| `db.createUser` / `createRole` / `setDefaultRWConcern` | Commands unsupported on M10+. Users and roles come from the Atlas API; the default write concern is the "Default Write Concern" cluster setting, or set concerns per operation |
| `allowDiskUse` | **Ignored on M0/Flex**, where `aggregate` is also capped at `maxTimeMS` 300 s |
| `$where`, `$function`, `$accumulator`, `mapReduce` | Unavailable on M0/Flex (self-hosted equivalent is `security.javascriptEnabled`) |
| `rs.*`, `fsync`, `compact`, `applyOps` | Unsupported or restricted — Atlas manages the topology |
| `reshardCollection`, `unshardCollection` | M30+ sharded clusters only |

Anything in a runbook that calls `setParameter` or an `rs.*` command does not exist on Atlas. Find that out at Gate 5, not during the incident. Full list in `references/consistency-and-sharding.md`.

---

## Security

MongoDB-specific mechanics. Generic schema-level security (tenant fields, PII classification, least privilege) is in `mir-database`.

**NoSQL injection via operator objects — the real and common bug.** Mongo filters are documents. A value that should be a scalar, arriving as an object, becomes an operator.

```js
// VULNERABLE — parsed JSON passed straight into the filter
await users.findOne({ email: req.body.email, password: req.body.password });
// {"email":{"$gt":""},"password":{"$gt":""}}        → logs in as the first user in the collection
// {"email":"victim@x.com","password":{"$ne":null}}  → logs in as a named victim, no password
```

Query strings are the same hole: with a `qs`-style parser, `?email[$ne]=` arrives as `{$ne: ""}`.

Fixes, in order of reliability: **(1)** type the field at the boundary and reject anything else — a Zod/Pydantic/JSON-Schema `string` field turns an operator object into a 400 before it reaches the driver, and also covers `$regex` DoS and `$expr`; **(2)** coerce at the call site, `{ email: String(req.body.email) }`; **(3)** do not rely on sanitizer middleware that strips `$` by mutating `req.query` — on Express 5 `req.query` is a getter and in-place mutation silently does nothing, so verify it on your version; **(4)** never authenticate by querying for the password — fetch by identifier, then compare the hash in application code with a constant-time compare.

Turn server-side JavaScript off (`security.javascriptEnabled: false`, or `--noscripting`) so `$where`, `$function`, `$accumulator` and `mapReduce` do not exist. Two 2026 advisories land there: **CVE-2026-11933** (8.8, post-auth use-after-free in server-side JS BSON conversion, 4.4 through 8.3) and **CVE-2026-8336** (7.7, use-after-free in `$_internalJsEmit` and `mapReduce`).

**Authentication is off by default and the port is the perimeter.** `mongod` starts with no access control — anyone who reaches it is an administrator. Enable `security.authorization: enabled` (or `--auth`) and create the user administrator first; a Docker image started without `MONGO_INITDB_ROOT_USERNAME`/`_PASSWORD` runs with no auth at all. `mongod` binds to `127.0.0.1` by default since 3.6, and the breach pattern is uniform: someone sets `net.bindIpAll` or `0.0.0.0` to make it reachable and enables auth "later." Set `net.bindIp` to specific interfaces, require TLS on client *and* intra-cluster connections, use `security.clusterIpSourceAllowlist`, and add per-user IP allowlists via `authenticationRestrictions` in `db.createUser()`. On Atlas the equivalents are the IP access list and private endpoints — an open `0.0.0.0/0` entry is the same bug with a nicer UI.

**Object-level authorization (IDOR/BOLA) is your query's job.** There is no row-level security. Put `tenant_id` in **every** filter, not just the ones you remembered, and scope the application user to a custom role covering only the collections it uses — `readWriteAnyDatabase`, `dbOwner` and `root` on an application connection string are how one injection becomes a full dump. **CVE-2026-13059** (8.6) was an RBAC bypass in the server itself, so role design does not remove the need to patch.

**Mass assignment.** `updateOne({_id}, {$set: req.body})` lets a client set `role`, `tenant_id`, or `balance_minor`. Build an explicit `$set` from allow-listed keys, with `additionalProperties: false` in the validator as the server-side backstop. **Deserialization** is the same bug one layer down: handing a raw client hash to an ODM query builder. **CVE-2026-2302** was exactly that — unsafe reflection in `Mongoid::Criteria.from_hash` (fixed 7.6.1 / 8.0.12 / 8.1.12 / 9.0.10).

**Field-level encryption.** Encryption at rest protects a stolen disk, not a compromised application credential. For fields that must stay unreadable to the database use **Queryable Encryption** (equality and range are production-ready; prefix/suffix/substring are **public preview in 8.2 and explicitly not for production** — GA will be format-incompatible), or CSFLE for equality-only with deterministic encryption. Keys live in a KMS, not in the database and not in the connection string. **CVE-2026-18712** (7.2) was improper authorization in Queryable Encryption maintenance operations.

**Secrets and PII in logs.** The connection string contains the password and appears in process listings, crash dumps, and any log line printing the client config — **CVE-2026-18710** (8.2) is cleartext storage of sensitive configuration in driver logging, fixed in driver 5.9.2. The profiler and slow-query log record the full query document, filter values included; turn command-body logging off on collections holding PII. **CVE-2025-11695** made `tlsInsecure=false` *disable* certificate validation in the Rust driver (fixed 3.2.5) — patch drivers with the same discipline as the server.

**Patch level is a control, not hygiene.** From mongodb.com/alerts, verified 13 Aug 2026:

| CVE | CVSS | What | Fixed in |
|---|---|---|---|
| CVE-2026-13072 | 9.2 | Memory corruption via external data processing input validation | 8.3.7 / 8.2.12 / 8.0.28 / 7.0.39 |
| CVE-2026-18691 | 9.0 | **Improper authentication on intra-cluster connections** exposes credentials | 8.3.8 / 8.0.29 / 7.0.40 |
| CVE-2026-18697 | 8.7 | **Unauthenticated** DoS on `mongos` via the aggregation framework | 8.3.8 / 8.0.29 / 7.0.40 |
| CVE-2026-9740 | 8.7 | **Pre-auth** stack overflow via unbounded BSONColumn recursion | 8.3.3 / 8.2.10 / 8.0.24 / 7.0.35 |

Two of these are reachable before authentication; one bypasses it outright.

---

## How this slots into the pipeline

- **Gate 0:** if the workload needs cross-entity transactions as the norm, ad-hoc joins, or exact arithmetic across documents, say so. Not a veto — a ledgered decision.
- **Gate 3:** MongoDB has no `CHECK`, no `FOREIGN KEY`, no `NOT NULL`. The enforcement column in the pillar's boundary table has three possible values here: `$jsonSchema` validator, `unique` index, or application code. **Referential integrity is always application code** — write that in the register.
- **Gate 5:** state explicitly — embed vs reference per relationship with a `maxItems` bound for every embedded array; the shard key plus `analyzeShardKey` output; the validator; the index set with the query each one serves; write and read concern per operation class; where a transaction is used and why a single-document update was not enough.
- **Gate 6:** create collections with validators in the same migration as the indexes. Backfills go in bounded `_id`-range batches, never one `updateMany` (not retryable, no transaction, large oplog burst). **Gate 7:** reliability review works sections 2–5; security review works the Security section, injection payload test and patch table first.

## References

| File | Purpose |
|---|---|
| `references/schema-patterns.md` | Bucket, extended reference, subset, computed and schema-versioning patterns with full documents; the `$jsonSchema` validator library; the embed-vs-reference worksheet. Read at Gate 5. |
| `references/indexes-and-aggregation.md` | Reading `explain("executionStats")`, the full "why is my index not used" procedure, ESR worked examples, covered-query construction, index cost and `$indexStats`, pipeline rewrites. Read at Gate 6 and when a query is slow. |
| `references/consistency-and-sharding.md` | Write/read concern matrix per operation class, causal session and `withTransaction` code, retryable-write coverage table, shard-key selection, chunk migration, the resharding runbook, and the full Atlas restriction list. Read at Gate 5. |

## Edit boundary

- True of Postgres and MySQL too (cardinality, ownership, tenancy, PII classification, denormalization needing a reconciliation mechanism)? → **up** to `mir-database`.
- Application transaction boundaries, idempotency keys, retry policy, queue design? → `mir-backend` and its framework modules.
- ODM and driver mechanics (Mongoose schema options and middleware, Beanie/Motor, Spring Data repositories, per-framework pool sizing)? → the framework module for that stack.
- **Here:** only what is true because the engine is MongoDB — BSON limits, `$jsonSchema`, write/read concern, sessions, `findAndModify`, index types and their limits, aggregation stages, shard keys, and the MongoDB server/driver advisories.
- A different document or key-value engine → its own `mir-database-<engine>` module. Never widen this one.
