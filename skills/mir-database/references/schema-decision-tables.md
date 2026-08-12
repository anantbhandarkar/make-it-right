# Schema decision tables

Three decisions that are expensive to reverse: the engine class, the tenancy model, and the primary-key strategy. Read at **Gate 0** (engine fitness) and **Gate 5** (design review). Each decision goes in the Assumption Ledger with the consequence you accepted.

Currency verified 13 August 2026.

---

## 1. Relational vs document

The 2026 default for an application database is a relational engine with a JSON column for the genuinely variable parts. Reach for a document store when the *access pattern*, not the developer's preference, says so.

| Pick | When all of these are true | Do NOT use when | What it costs you |
|---|---|---|---|
| **Relational (Postgres, MySQL, SQL Server, Oracle)** | Entities relate to each other; you need joins, even occasionally; you need multi-row transactions; amounts must be exact; requirements will drift and someone will ask an analytics question | Documents are genuinely 3+ levels deep with per-record schema variance, and you need write scale past one primary | Schema changes are migrations; deeply nested JSON querying is more awkward than a document store's |
| **Relational + JSON column** | The core is relational but a subset of fields is sparse, per-customer, or vendor-shaped (webhook payloads, product attributes, feature flags) | The JSON is the whole record, or documents routinely exceed ~8 KB and are updated field-by-field at high rate | Constraints do not reach inside the JSON — validate in the app or with a check constraint on extracted values; indexing the JSON has more limits than indexing columns |
| **Document (MongoDB, DocumentDB)** | Records are read and written whole; nesting is deep; schema legitimately varies per record; you need horizontal write scale from day one; a good shard key exists | You need cross-entity transactions as the norm, ad-hoc joins, or exact financial arithmetic; documents grow without a bound | No foreign keys — referential integrity is your code's job; per-document size limits (16 MB on MongoDB) turn unbounded arrays into an outage; you own the join |
| **Key-value / wide-column (DynamoDB, Cassandra)** | The access patterns are known, few, and fixed; scale and single-digit-ms latency dominate; you can model the table per query | Access patterns are unknown or will change; you need ad-hoc queries; you need joins | The schema *is* the query set — a new access pattern means a new table or a full backfill |

**Test that settles most arguments:** write down the ten queries the product needs in its first year. If more than two of them correlate data across entity types, use a relational engine.

**If you choose a document store, decide embed vs reference per relationship, not per project:**

| Embed the child in the parent when | Reference the child when |
|---|---|
| The child is read every time the parent is read | The child is queried on its own |
| The child count is bounded and small (one-to-few) | The count is unbounded or grows over time (one-to-many/many) |
| The child has no independent lifecycle | The child is shared by several parents |
| The child is written in the same operation as the parent | The child changes far more often than the parent |

The failure mode is the unbounded array: a `comments` array embedded in a post grows until the document is too big to update efficiently and eventually breaches the size limit. If a child list has no natural ceiling, reference it.

---

## 2. Tenancy model

| Model | Isolation | Migration cost | Practical tenant ceiling | Noisy-neighbor risk | Per-tenant restore | Cost per tenant | Pick when |
|---|---|---|---|---|---|---|---|
| **Shared schema + `tenant_id` column** | Logical only — one missing predicate leaks data | One DDL run for everyone; the risk is a long lock on a large shared table | Millions | High — one tenant's workload hits everyone's tables | Hard: filtered export, no restore-in-place | Lowest | **Default for B2B SaaS.** Many tenants, similar shape, no per-tenant regulatory boundary |
| **Schema per tenant** | Better — a query in the wrong schema returns nothing rather than someone else's rows | N schemas × every migration; must be batched and resumable; partial failure leaves schemas at mixed versions | Low thousands. Beyond that, system-catalog size degrades query planning and connection startup | Medium — shared buffers and connections | Medium: dump/restore one schema | Medium | Hundreds-to-low-thousands of tenants, per-tenant customization, or a contractual "separate schema" requirement |
| **Database (or cluster) per tenant** | Strongest — a connection to the wrong database is a connection error | N databases × every migration, plus N connection pools and N monitoring targets | Hundreds, unless you build tenant-provisioning automation first | Lowest — separable compute | Easy: restore one database | Highest | Regulated data, data-residency requirements, white-label, or a handful of large enterprise tenants |
| **Hybrid tiering** | Mixed | Two migration paths to maintain | — | Isolated for the large tenants | Easy for the isolated ones | Medium | Mature product: shared schema for the long tail, dedicated database for the largest or most regulated tenants |

**If you pick shared schema, these are not optional:**

- `tenant_id` is `NOT NULL` on every tenant-owned table, including join tables and audit tables.
- Every unique constraint is scoped: `UNIQUE (tenant_id, email)`, not `UNIQUE (email)`.
- Every foreign key relationship stays inside one tenant. A child row whose parent belongs to another tenant is a data breach that the FK alone will not catch — add a composite FK `(tenant_id, parent_id) REFERENCES parent (tenant_id, id)` so the database enforces it.
- Enforce the filter in the database, not only in the ORM. On Postgres that means row-level security with `FORCE ROW LEVEL SECURITY` and a non-owner, non-`BYPASSRLS` application role; see the Security section of `SKILL.md`.
- Have one test that connects as tenant B and asserts zero rows of tenant A, for every table.
- If tables grow large, `tenant_id` is a good partition key — but note that on Postgres the partition key must be part of the primary key of a partitioned table, which changes your key design.

**If you pick schema- or database-per-tenant, build the provisioning and migration runner before the tenth tenant**, not after the hundredth. The thing that fails is not the model, it is running 900 migrations where 14 fail halfway.

---

## 3. Key strategy

The primary key choice is mostly an index-locality choice, and on engines whose table storage is clustered by the primary key (InnoDB, SQL Server clustered index), it is also a table-layout choice. On InnoDB every secondary index stores the primary key value; on SQL Server every nonclustered index stores the *clustering* key, which is the primary key only when the primary key is also the clustered index. Either way, key width multiplies across every secondary index.

| Strategy | Width | Insert locality | Guessable | Client can generate before insert | Notes |
|---|---|---|---|---|---|
| **`bigint` identity / sequence** | 8 bytes | Best — appends to the right-hand edge of the B-tree | Yes — sequential IDs leak volume and allow enumeration | No | Smallest and fastest. Needs a round trip for the value. Merging two databases collides. Never expose the raw value in a public URL without an object-level authorization check |
| **UUIDv4 (random)** | 16 bytes | Worst — every insert lands at a random point in the index, causing page splits, poor cache hit rate, and index bloat that grows with table size | No | Yes | Fine for small or low-write tables. On a large high-insert table this is the measurable cost people are actually complaining about when they say "UUIDs are slow" |
| **UUIDv7 (time-ordered, RFC 9562)** | 16 bytes | Near-sequential — the leading 48 bits are a millisecond Unix timestamp, so inserts cluster | Partly — the creation time is readable from the value | Yes | The 2026 default when you need a client-generated or merge-safe key. Sortable by creation time |
| **ULID** | 16 bytes binary / 26 chars text | Same as UUIDv7 — same 48-bit timestamp prefix | Partly — same timestamp exposure | Yes | Shorter as text (26 vs 36 chars). No standard database type; you store it as `uuid`-compatible bytes or a fixed-width string. Prefer UUIDv7 for new work; there is no reason to migrate an existing ULID system |
| **Composite natural key** | Varies | Depends on leading column | Depends | Yes | Correct for join tables (`(order_id, product_id)`) and for genuinely immutable identifiers. Wrong whenever the business can change any component |

### Rules

1. **Surrogate primary key by default; natural keys become `UNIQUE` constraints.** Emails, SKUs, tax IDs, and usernames are things the business changes. A changed natural key rewrites every referencing row and breaks every cached or externally-stored identifier. Keep the natural key — enforce it with a unique constraint, do not make it the primary key.
2. **Do not use a random UUID as the primary key of a large, high-insert table.** Use `bigint` if the key is internal, UUIDv7 if it must be client-generated, merge-safe, or non-enumerable.
3. **Store UUIDs in the native binary type**, never `CHAR(36)`/`VARCHAR(36)`. Postgres: `uuid`. SQL Server: `uniqueidentifier`. MySQL/MariaDB: `BINARY(16)` with `UUID_TO_BIN(v, 0)` — the swap flag is `1` only for reordering UUIDv1, and applying it to a v7 value destroys the time ordering you chose it for.
4. **UUIDv7's timestamp is a disclosure.** It reveals when the row was created. That is usually harmless or useful; if IDs are public and creation time is sensitive (signup order, deal volume), use UUIDv4 for that table or expose a separate opaque public identifier.
5. **A non-guessable key is not authorization.** Object-level permission checks are still required on every read and write. An unguessable ID only raises the cost of enumeration.

### Where UUIDv7 can be generated (verified August 2026)

| Platform | Native UUIDv7 |
|---|---|
| PostgreSQL | Yes, 18+: `uuidv7()`. (`uuidv4()` added as an alias for `gen_random_uuid()`.) Earlier versions: the `pg_uuidv7` extension or generate in the application |
| MariaDB | Yes, 11.7+: `UUID_v7()`. Not safe for statement-based replication |
| MySQL | No — generate in the application, store as `BINARY(16)` |
| Oracle | No — v4 only (23ai). Generate in the application |
| SQL Server | No. Note that `uniqueidentifier` sort order is not byte order, so a UUIDv7 stored in that type does **not** get index locality automatically — use `NEWSEQUENTIALID()` or store the value as `binary(16)` in the sort order the index needs |
| MongoDB | No generator; use a v7 value as `_id` (as `BinData` subtype 4) for time-ordered inserts |
| Python | 3.14+: `uuid.uuid7()`. Earlier: a third-party package |
| .NET | 9+: `Guid.CreateVersion7()` (overload takes a `DateTimeOffset`, so it is testable with a `TimeProvider`) |
| Go / Rust / Node | `google/uuid` 1.4+, `uuid` crate 1.3+, npm `uuid` **v10+** (v7 landed in 10.0.0, not v9) |
| Java | No JDK support — `com.fasterxml.uuid` (java-uuid-generator 5.0+) or `uuid-creator` |

Application-side generation is the portable choice and is required for the mixed case. Database-side generation is only available on PostgreSQL 18+ and MariaDB 11.7+.
