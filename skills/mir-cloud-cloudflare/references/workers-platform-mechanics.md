# Cloudflare platform mechanics — execution model, storage contracts, and the config-data lesson

Read at **Gate 5** when mapping workloads to products, and at **Gate 7** for the reliability review.

> Retrieved **25 August 2026** from Cloudflare's own documentation, changelog and post-incident report. Limits and prices move. **Re-check the product's own limits page before a number enters a design.** Every `developers.cloudflare.com` page below is also fetchable as raw markdown by appending `index.md` to the URL — use that when exact wording matters.
>
> Cross-provider comparison numbers (the 5-minute CPU ceiling against Lambda and Cloud Run, the 128 MB isolate, the 10 GB Durable Object cap, R2's zero egress, Class A/B rates, Containers' billed egress, accelerator availability) live in `mir-cloud/references/provider-decision-tables.md` and `cost-model.md`. They are not repeated here.

---

## 1. Execution model — the two clocks

Cloudflare bills and limits on **CPU time**; it limits some trigger types on **wall time**. Confusing them is the most common sizing error.

| Trigger | Wall-time limit | CPU-time limit | Notes |
|---|---|---|---|
| Incoming HTTP request | **None** while the client stays connected | 30 s default, raisable to 5 min via `limits.cpu_ms` | `ctx.waitUntil()` extends work up to **30 s** past the response or client disconnect |
| Cron trigger | 15 min | 30 s at intervals under 1 hour; 15 min at intervals of 1 hour or more | A frequent cron doing heavy compute dies at 30 s of **CPU**, not at 15 min |
| Queue consumer | 15 min | 30 s default, configurable to 5 min | |
| Durable Object alarm handler | 15 min | as configured | |
| Durable Object HTTP / RPC | Unlimited while the caller is connected | each incoming request or WebSocket message **resets** the remaining CPU budget to 30 s | Consuming more than 30 s of compute *between* incoming requests raises the chance the object is evicted and reset |
| Workflow step | Unlimited | | |

**Known documentation conflict.** The limits page presents the 15 minutes for cron / queue / DO alarm as **wall time**; the pricing page describes the same 15 minutes as *"CPU time per Cron Trigger or Queue Consumer invocation."* The Queues limits page splits them again — 15 min wall clock, CPU configurable to 5 min. **Use the limits page and say the pricing page disagrees.** Do not quote either number without naming which page it came from.

Other execution facts that change a design:

- **CPU excludes I/O wait.** *"Waiting on network requests (such as `fetch()` calls, KV reads, or database queries) does not count toward CPU time."* Stated most bluntly on the Durable Objects and Queues limits pages.
- **Durable Objects are the exception.** They bill **wall-clock duration** while running, or resident and unable to hibernate, at 128 MB regardless of actual use. "You are not billed for waiting" is true of Workers and false of a non-hibernating Durable Object.
- **Memory is 128 MB per *isolate*, not per invocation,** and an isolate serves many concurrent requests. On exceeding it the runtime lets in-flight requests finish and creates a new isolate; under extreme load it may cancel incoming requests. Surfaces as error **1102** / dashboard `Exceeded Memory` / Logpush outcome `exceededMemory`. Buffering an oversized response body gives `Memory limit would be exceeded before EOF`.
- **Many Durable Object instances of one class may share an isolate's 128 MB** — and are each still billed as if allocated the full 128 MB.
- **Startup time is 1 second**, applied to global-scope parse and execute, enforced **at upload**. Failure is `Script startup exceeded CPU time limit`, error code **10021**; Wrangler emits a CPU profile and reports `startup_time_ms`. Raised from 400 ms on **10 October 2025** — treat any 400 ms citation as stale.
- **Six connections may be waiting for response headers simultaneously.** Once headers arrive a connection stops counting; a seventh pending connection is **queued**, not rejected. Counted APIs: `fetch()`, KV `get/put/list/delete`, Cache `put/match/delete`, R2 `list/get/put/delete/head`, Queues `send/sendBatch`, `connect()` TCP, and outbound WebSockets. Workers invoked via a service binding share the parent's budget.
- **`fetch()` to another Worker on the same zone without a service binding fails.** Use a service binding, or a Custom Domain.
- **Runtime updates ship a few times per week** with a 30-second grace period for in-flight requests.

## 2. Compatibility dates and flags

The versioning surface. Cloudflare's promise: *"The Workers runtime will support old compatibility dates forever"* — with the reservation that if a breaking change becomes necessary, *"Cloudflare will actively contact affected developers."* Strong intent, not an absolute guarantee.

| Situation | What actually happens |
|---|---|
| Date omitted on **API** upload | Defaults to **`2021-11-02`** — the oldest date, before any flag took effect |
| Worker created in the **dashboard** | Gets the current date automatically |
| Date never bumped | Keeps working. But new features may require a current date, and the docs *only describe current behaviour* — reconstructing an old Worker's semantics means reading the compatibility-flags page |
| Flag set ahead of its date | Opts that one change in early |
| Flag set to the `disable_` form | Opts **out** of a change that already became default |

Dated defaults worth knowing because they change behaviour under identical code:

- `nodejs_compat` + `nodejs_compat_v2` — enabled by default for compatibility dates **2026-08-04** or later. Between **2024-09-23** and **2026-08-03**, `nodejs_compat` implied `nodejs_compat_v2`.
- `node:fs` virtual filesystem — available by default with `nodejs_compat` from **2025-09-01**.

**Review rule:** the date is a dependency pin. It gets its own commit, its own test pass, and a named owner. Bundling a date bump into a feature change means a behaviour change and a code change land together with one rollback.

## 3. The sandbox, and what "no filesystem" means now

- **Only JavaScript and WebAssembly are accepted.** *"Workers does not allow our customers to upload native-code binaries to run on the Cloudflare network."* Other languages reach the platform by compiling or transpiling to one of the two. Python runs via Pyodide and cannot load most native-extension packages.
- **The layer-2 sandbox uses Linux namespaces and `seccomp`** with a totally empty mount namespace, blocking all filesystem syscalls. Cloudflare notes this is possible precisely *because* guest programs are not native binaries.
- **But `node:fs` now exposes an in-memory virtual filesystem**: `/bundle` (read-only, one file per bundled module), a writable `/tmp`, and `/dev/null|random|full|zero`. **`/tmp` contents are per-request and not persistent** — files created for one request are unavailable to concurrent or later requests.
- The security-model page still carries the absolute wording *"Cloudflare does not expose a filesystem API at all"* (page last updated Aug 2026). It predates the VFS and is reconcilable only in the sense that the *host* filesystem remains unreachable. Phrase the constraint as "no host filesystem, no native binaries, and an ephemeral in-memory VFS."

**Global scope is shared.** Module-scope variables persist across requests handled by the same isolate and across tenants. Cache immutable derived data there if you must; never cache anything keyed to a user. Local development, with one user, cannot show you this defect.

## 4. Spend controls

| Mechanism | What it does | What it does not do |
|---|---|---|
| Budget alerts | Notify at a threshold; Pay-as-you-go accounts only, Enterprise contract accounts unsupported | *"Budget alerts are informational only. They do not pause or cap usage."* |
| `limits.cpu_ms` | Terminates an invocation past the configured CPU budget. Default 30,000 ms; max 300,000 ms (5 min). Also settable at **Workers & Pages → your Worker → Settings** | Nothing about request count. A request flood still bills per million requests |
| `limits.subrequests` | Caps subrequests per invocation. Paid default 10,000, paid max 10,000,000; free default and max both 50 | On Free you can only lower it |
| WAF rate limiting | The control for the request dimension | Not part of the Worker's own configuration — it has to be designed in |

Both keys are enforced **only on Cloudflare's network, never in local development**, and are documented as supported for the Standard Usage Model. A legacy note: Workers on the pre-1 March 2024 Bundled usage model were automatically given a **50 ms** CPU limit.

There is **no account-level hard spend cap.** State that in the Gate 5 design as an accepted risk with its two mitigations, or the design has not addressed it.

## 5. The four storage products, and what each guarantees

| | Consistency contract | Hard caps that end designs | Best fit |
|---|---|---|---|
| **Workers KV** | Eventually consistent. *"At the ... location at which changes are made, these changes are usually immediately visible. However, this is not guaranteed."* Elsewhere, *"up to 60 seconds or more."* Negative lookups are cached too | 1 write/sec **per key** (both plans); key 512 bytes; value 25 MiB; 1,000 operations per Worker invocation; `cacheTtl` minimum 30 s, default 60 s | Read-heavy configuration and content that tolerates staleness |
| **D1** | Single primary, **single-threaded, one query at a time**, itself backed by a Durable Object. With replication: **sequential consistency** via bookmarks — monotonic reads and writes, read-my-own-writes | **10 GB per database and it cannot be raised**; 30 s max query; 100 columns per table; 2 MB max row/BLOB; 100 bound parameters; 1,000 queries per invocation | Relational data that fits in 10 GB with modest concurrency |
| **Durable Objects** | Serialized per object. Storage writes without an intervening `await` are coalesced into one implicit atomic transaction | 10 GB per SQLite-backed object; key+value combined ≤ 2 MB; 32,768 hibernating WebSockets per object | Coordination, per-entity state, real-time fan-out |
| **R2** | Object store, S3-compatible | 1 write/sec to the same key; 10,000 parts per multipart upload; 100 custom domains per bucket | Bytes. Not a database |

**Raising `cacheTtl` raises the staleness window by the same amount.** Cloudflare's documented pattern for a hot key is to route all writes for it through a Durable Object and read the value from KV elsewhere.

**D1 read replication is opt-in and its published status is public beta** (10 Apr 2025), even though the current docs page reads as GA and carries no beta banner. Queries hit the primary unless wrapped in `withSession()`; a replica *"may be arbitrarily out of date"*; writes always go to the primary; the Sessions API is Worker-binding-only, not REST. Cloudflare **never** describes D1 as "strongly consistent" — do not write that into a design.

**Throughput arithmetic to do at Gate 5, not after:** D1 processes one query at a time, so ~1 ms average query ≈ 1,000 queries/sec and ~100 ms average ≈ 10/sec. Past that the queue fills and returns `overloaded`. Same shape as a Durable Object, because it is one.

## 6. Durable Objects — sharding, lifecycle, gates, alarms

### Sharding

`Required objects = total requests per second ÷ per-object capacity`, where per-object capacity is roughly:

| Work per request | Capacity |
|---|---|
| Simple pass-through, minimal parsing | ~1,000 req/s |
| Moderate — JSON parsing, validation | ~500–750 req/s |
| Complex — transformation, storage writes | ~200–500 req/s |

The limits page states a flat *"soft limit of 1,000 requests per second"*; the Rules of Durable Objects page gives the range above. **Plan against the range, not the ceiling.** An overloaded object queues and then returns an *overloaded* error to callers.

Model the object as the **atom of coordination** — one room, one document, one order. Cloudflare's own anti-pattern: *"Do not use a single Durable Object as a global singleton."* That is a throughput bug and a blast-radius bug at once.

### Lifecycle and eviction

- Evicted from memory after **70–140 seconds of inactivity**. Hibernating WebSocket clients stay connected to the edge; in-memory state is reset and the constructor re-runs on the next event. Persist per-connection state with `serializeAttachment` / `deserializeAttachment`.
- Duration billing does **not** accrue during hibernation — which is why an outbound `connect()` or WebSocket matters: those prevent eviction and keep the object alive for up to **15 minutes** each.
- Location is chosen from the **first `get()`** and objects *"do not currently change locations after they are created."* `locationHint` is best effort. `jurisdiction()` is the enforced control, and the same name produces a **different ID** per jurisdiction — so adding one later is a data migration, not a config change.

### Gates

| Gate | Guarantee | Gap |
|---|---|---|
| **Input** | While a storage operation is executing, no events are delivered except storage completions | *"Input gates only protect during storage operations. Non-storage I/O like `fetch()` or writing to R2 allows other requests to interleave."* Awaiting a `fetch()` **opens** the gate |
| **Output** | Outgoing network messages — responses *and* new `fetch()` calls — are held until a pending write commits. If the write fails, they become errors and the object restarts | Does not order two calls issued from the same event with no `await` between them; Cloudflare documents this and calls it an acceptable caveat |

The fix for the input-gate gap is optimistic locking: read a version, make the external call, verify the version has not moved, then write.

### `blockConcurrencyWhile()`

- Blocks all other event delivery until the callback resolves; guarantees ordering and prevents concurrent requests.
- **A throw terminates and resets the object**, deliberately, so it cannot be left half-initialized. Wrap the body in `try/catch` to survive.
- **30-second timeout**; exceeding it also resets the object.
- Intended for constructor-time schema migration and init. Rarely needed in request handling — SQLite storage calls are synchronous and do not yield, and input gates already cover async KV storage. Reserve it for external async calls you cannot tolerate interleaving.
- Cost it: ~5 ms in the constructor caps the object at ~200 req/s.

### Alarms

- **One alarm per object.** `setAlarm()` overwrites any existing one. `getAlarm()` returns `null` while an alarm is running unless `setAlarm` was called since the handler started.
- **At-least-once, with a bounded budget:** retried on an uncaught exception with exponential backoff from a **2-second** delay, **up to 6 retries**, and only for the most recent `setAlarm()`. Past six, the work is gone silently.
- `alarm(alarmInfo)` receives `retryCount` and `isRetry` — use them.
- `deleteAlarm()` inside the handler prevents retries only on a best-effort basis; it is **not guaranteed**.
- Cloudflare's own guidance: catch inside `alarm()` and schedule the next alarm before returning if the work must not be lost. And *"in rare cases, alarms may fire more than once"* — the handler must be idempotent.
- Alarms are storage operations, follow storage rules, and re-activate an evicted object. Each `setAlarm()` bills as one row written.

### Storage backend

New namespaces **must** be SQLite-backed — Cloudflare stopped allowing new KV-backed namespaces on **9 July 2026**. SQLite in Durable Objects went GA **7 April 2025**. Any guidance you find about KV-backed Durable Objects applies only to namespaces provisioned before that cutoff. At the storage cap, writes fail with `database or disk is full: SQLITE_FULL` while reads and `DELETE` continue, so you can free space.

Billing note to state carefully: Cloudflare **announced** SQLite storage billing with a target date of **7 January 2026 (no earlier)** in a 12 Dec 2025 changelog entry. The pricing page still uses future tense as of 25 Aug 2026 and no changelog entry confirms it commenced. **Say "announced target," not "began."** Compute billing has applied since the public beta.

## 7. Residency — hints are not guarantees

| Product | Best-effort | Enforced |
|---|---|---|
| **R2** | Location Hints — *"a best effort and not a guarantee, and they should only be used as a way to optimize performance"* | Jurisdictional Restrictions — *"guarantee objects in a bucket are stored within a specific jurisdiction."* `eu`, `us`, `fedramp` (Enterprise). Addressed by `https://<ACCOUNT_ID>.<JURISDICTION>.r2.cloudflarestorage.com` or the `jurisdiction` field on the `r2_buckets` binding |
| **Durable Objects** | `locationHint` — the object is placed to minimize latency *from* the hint, not necessarily in it | `env.NS.jurisdiction("eu").idFromName(name)` — *"only run and store data within a region to comply with local regulations"* |

**Both are immutable after creation.** An R2 bucket's jurisdiction cannot be changed; a Durable Object never relocates and its jurisdictional ID differs from its non-jurisdictional one. If Gate 1 recorded a residency requirement, this decision belongs in the Gate 5 design — retrofitting it is a data migration in both products.

The pillar's rule still applies on top: enumerate **every** sink, not just the primary store. Logs, analytics, traces and any third-party APM leave the boundary by default.

## 8. 18 November 2025 — a config-data change is a deploy

Source: `blog.cloudflare.com/18-november-2025-outage/`. Primary source; prefer it over any secondary account.

### Timeline (UTC)

| Time | Event |
|---|---|
| 11:05 | ClickHouse database permissions change deployed — users' access to underlying `r0` tables made explicit |
| 11:20 | Impact begins; core CDN and security services return HTTP 5xx |
| 11:28 | Deployment reaches customer environments, first errors observed |
| 13:05 | Workers KV and Access bypassed around the core proxy |
| 14:24 | Creation and propagation of the bad Bot Management file stopped |
| 14:30 | Core traffic largely flowing as normal |
| 17:06 | *"As of 17:06 all systems at Cloudflare were functioning"* |

### The chain

A metadata query built the Bot Management feature file: `SELECT name, type FROM system.columns WHERE table = 'http_requests_features'`. It had always assumed only `default`-database columns would come back. After the permissions change it also saw `r0`, *"effectively more than doubling the rows in the response ultimately affecting the number of rows (i.e. features) in the final file output."* The proxy preallocates memory for a bounded feature count: *"Currently that limit is set to 200, well above our current use of ~60 features. ... the limit exists because for performance reasons we preallocate memory."* The oversized file exceeded the bound and the FL2 proxy panicked — `thread fl2_worker_thread panicked: called Result::unwrap() on an Err value`.

**A discrepancy in the primary source.** ~60 doubled is ~120, which does not exceed 200. The published report does not reconcile this. **Cite the mechanism, not the arithmetic.**

### Why it was hard to read

The file regenerated **every five minutes** and the ClickHouse nodes were updated gradually, so *"every five minutes there was a chance of either a good or a bad set of configuration files being generated."* The network recovered and failed repeatedly. Cloudflare's status page — hosted entirely off Cloudflare infrastructure — went down at the same time by coincidence. The team *"initially wrongly suspected"* a hyper-scale DDoS attack. It was not one: *"The issue was not caused, directly or indirectly, by a cyber attack or malicious activity."*

### Blast radius

Core CDN/security (5xx, full duration) · Turnstile (failed to load) · Workers KV (elevated 5xx) · Access (widespread authentication failures) · the Dashboard (Turnstile in the login flow, KV behind it) · Email Security (lost an IP reputation source).

**The nuance worth carrying:** Cloudflare was mid-migration to the FL2 proxy. FL2 customers got 5xx. Customers still on the older FL engine did **not** error — their bot scores were generated as **zero**, so anyone with bot-blocking rules saw mass false positives instead. Two completely different symptoms from one root cause, decided by which fleet you were on. A partial rollout does not halve an incident; it splits it into two incidents you have to diagnose separately.

### The checklist this produces

- [ ] Enumerate every artefact that reaches production **without a code deploy** — feature files, rule sets, ML models, allow-lists, remote flags, generated config. Each needs staged rollout, a rollback path, and a named owner.
- [ ] Validate internally-generated data at the consumer as if it were user input: schema, row count, size. Cloudflare's first remediation was *"hardening ingestion of configuration files ... like user-generated input."*
- [ ] Fail soft at every preallocation bound. A bound "well above current use" is still a bound, and an `unwrap()` on it is a global outage.
- [ ] Name what sits in front of everything you run, and write down what happens when it panics.
- [ ] Build the signal that distinguishes "our configuration changed" from "traffic changed." An hour of the response went to the wrong hypothesis.
- [ ] Host the status page off the infrastructure it reports on — Cloudflare does, and it still coincidentally failed. Verify yours independently.
