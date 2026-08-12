# MongoDB schema patterns

Full shapes for the patterns named in SKILL.md section 1. Read at Gate 5, before the design is signed off. Verified against MongoDB 8.3 documentation, 13 August 2026.

---

## 1. Bucket pattern

**Problem:** one document per event. 100 sensors × one reading per second = 8.6 M documents/day, each with its own `_id` index entry and per-document storage overhead. Queries over a day scan millions of documents.

**Shape:** one document per (source, time window), holding a bounded array.

```js
{
  _id: ObjectId("..."),
  sensor_id: "s-0417",
  bucket_start: ISODate("2026-08-13T14:00:00Z"),
  bucket_end:   ISODate("2026-08-13T15:00:00Z"),
  count: 3600,                       // maintained by $inc, never recomputed
  sum_c: 79212.4, min_c: 20.1, max_c: 24.8,   // precomputed rollups
  readings: [ { t: ISODate("...:00:00Z"), c: 21.4 }, ... ]   // bounded by construction
}
```

Write with a guarded upsert so the array cannot exceed the window:

```js
db.readings.updateOne(
  { sensor_id: id, bucket_start: hourStart, count: { $lt: 3600 } },
  { $push: { readings: { t: ts, c: v } },
    $inc:  { count: 1, sum_c: v },
    $min:  { min_c: v }, $max: { max_c: v },
    $setOnInsert: { bucket_end: hourEnd } },
  { upsert: true })
```

The `count: { $lt: 3600 }` in the filter is what makes the bound real — when the bucket is full the filter misses and the upsert creates the next one. Without it you are back to an unbounded array.

**Index:** `{ sensor_id: 1, bucket_start: -1 }`. A range query over time reads a handful of documents instead of millions.

**Consider `timeseries` collections instead** (`db.createCollection(name, {timeseries: {timeField, metaField, granularity}})`) — the server does the bucketing, compression, and rollup for you. Constraints to check first: you cannot update or delete individual measurements freely, `refineCollectionShardKey` on 8.3 only accepts logical meta/time fields, the `timeField` cannot start with `$`, and an index cannot be named `"_id_"`. Two 2026 advisories (CVE-2026-18692, and an out-of-bounds write in time-series bucket handling) were in this code path — patch level matters more here than elsewhere.

---

## 2. Extended reference

**Problem:** `$lookup` on every read of a hot path.

```js
// orders
{ _id: ..., customer_id: ObjectId("c1"),
  customer: { name: "Ada Lovelace", tier: "gold" },   // copied, display-only
  items: [ { sku: "A-1", qty: 2, unit_price_minor: 1299 } ],   // price snapshot, not a lookup
  total_minor: 2598, currency: "GBP" }
```

Rules that keep the copy honest:

| Copied field class | Update mechanism | Example |
|---|---|---|
| **Immutable** | none needed | `sku`, the price at purchase time |
| **Snapshot by design** | none — write it in the ledger as intentional | `customer.name` on a historical invoice |
| **Stale-tolerant** | scheduled job or change stream | `customer.tier` on an open order |
| **Must be current** | do not copy — reference it | account balance, permissions |

A change stream is the mechanism when currency matters:

```js
db.customers.watch([{ $match: { "updateDescription.updatedFields.name": { $exists: true } } }])
// → for each event, updateMany on orders where customer_id matches and status is open
```

Never copy anything in the "must be current" row. If a reviewer cannot tell which row a copied field is in, the design is not finished.

---

## 3. Subset pattern

**Problem:** a product document embeds 4000 reviews; the page shows 5.

```js
// products — carries only what the page renders
{ _id: "p1", name: "...", review_count: 4021, rating_avg: 4.3,
  recent_reviews: [ /* 5 documents, capped */ ] }

// reviews — the full set
{ _id: ..., product_id: "p1", body: "...", stars: 5, created_at: ISODate(...) }
```

Cap the embedded array on write with `$push` + `$slice`:

```js
db.products.updateOne({ _id: pid },
  { $push: { recent_reviews: { $each: [review], $sort: { created_at: -1 }, $slice: 5 } },
    $inc:  { review_count: 1 } })
```

`$slice: 5` keeps the first 5 after the sort; `$slice: -5` keeps the last 5. This is the mechanism that makes "embed the newest few" safe — the array is bounded by the server, not by a code path that someone will later bypass.

---

## 4. Computed pattern

Storing a value you could derive is correct when the derivation is expensive and read far more often than written — a total, a count, a rolling average. It is the same decision as a relational denormalization and it carries the same obligation: name the authoritative source, name the mechanism that keeps the copy true, and name the drift check.

Maintain it in the **same update** as the source change, using atomic operators so there is no read-modify-write:

```js
db.products.updateOne({ _id: pid },
  { $inc: { review_count: 1, rating_sum: stars },
    $set: { rating_avg: null } })     // or compute avg on read from sum/count — cheaper and never drifts
```

Preferring `sum` + `count` over a stored `avg` removes a whole class of drift: two integers updated by `$inc` cannot disagree with each other the way a recomputed float can.

**Drift detection is part of the design.** A scheduled aggregation that recomputes the value for a sample of documents and alerts on mismatch. A computed field with no drift check is a bug with a schedule.

---

## 5. Schema versioning — how to change document shape on a live collection

MongoDB has no `ALTER TABLE`, which sounds like an advantage until two shapes exist simultaneously and every reader has to handle both forever.

Add a version marker from day one:

```js
{ _id: ..., schema_v: 2, ... }
```

The migration is expand/contract, exactly as on a relational engine:

1. **Expand.** Deploy code that *writes* v2 and *reads* both v1 and v2. No data change yet.
2. **Backfill in bounded batches.** Never one `updateMany` over the whole collection — it is not retryable, it holds no transaction so a failure leaves it half-done, and it produces a large oplog burst that lags secondaries. Iterate on `_id` ranges with a bounded batch size and a delay, checking `modifiedCount` each pass. An aggregation pipeline in `updateMany` (`[{ $set: { ... } }]`) can compute the new field from the old one server-side.
3. **Verify.** `db.c.countDocuments({schema_v: {$lt: 2}})` reaches 0.
4. **Contract.** Deploy code that reads only v2. Then add or tighten the `$jsonSchema` validator to require the v2 shape. Then `$unset` the dead fields, in batches.

The validator goes on **last**, after the backfill — adding it first rejects the writes that the old deployed code is still making.

---

## 6. `$jsonSchema` validator library

```js
// money — integer minor units, never "double"
total_minor: { bsonType: "long", minimum: 0 }
currency:    { bsonType: "string", pattern: "^[A-Z]{3}$" }

// enum / lifecycle
status: { enum: ["PENDING","PAID","FULFILLED","REFUNDED"] }

// bounded array — the antipattern guard, enforced by the server
tags: { bsonType: "array", maxItems: 20, uniqueItems: true,
        items: { bsonType: "string", maxLength: 32 } }

// required reference
tenant_id: { bsonType: "objectId" }

// nested object with its own closed shape
address: { bsonType: "object", required: ["line1","postcode"],
           additionalProperties: false,
           properties: { line1: {bsonType:"string"}, postcode: {bsonType:"string"} } }

// nullable, deliberately — "unknown", not "empty"
deleted_at: { bsonType: ["date","null"] }
```

Gotchas:

- Use `bsonType`, not JSON Schema's `type`. `type: "number"` accepts a double where you meant a 64-bit integer; `bsonType: "long"` does not.
- `additionalProperties: false` is the mass-assignment guard. Omitting it means any field a client sends is accepted.
- `maxItems` on an array is the only server-side defence against the unbounded-array antipattern. Use it wherever you embed.
- MongoDB implements **JSON Schema draft 4** with its own extensions and omissions — `$ref`, `$schema`, `definitions`, `format`, `id`, and hypermedia keywords are not supported.
- Find the documents that would fail before turning the validator on:
  ```js
  db.orders.find({ $nor: [ { $jsonSchema: <the schema> } ] }).count()
  ```
- Change a validator with `collMod`, not by recreating the collection:
  ```js
  db.runCommand({ collMod: "orders", validator: {...}, validationLevel: "strict", validationAction: "error" })
  ```
- `validationAction: "warn"` writes the invalid document and logs. It is a migration tool, not a destination. Anything left on `warn` in production is unvalidated.
- Validators cannot be applied to `admin`, `local`, `config`, or system collections, and have extra restrictions on CSFLE / Queryable Encryption collections.

---

## 7. The embed-vs-reference worksheet

Answer these per relationship and put the answers in the Assumption Ledger. If any answer is "we don't know," that is the question for Gate 1.

1. What is the **maximum** number of children one parent will ever have? (No number → reference.)
2. Is the child ever read without its parent? (Yes → reference.)
3. Is the child written on a different schedule from the parent? (Yes → reference.)
4. Do the parent and child have to change atomically? (Yes → embed, or accept a transaction.)
5. Is the child shared by more than one parent? (Yes → reference.)
6. What is the p99 size of the parent document with the child embedded, in a year?
7. If we embed, which `$jsonSchema` `maxItems` bound goes on the array?
