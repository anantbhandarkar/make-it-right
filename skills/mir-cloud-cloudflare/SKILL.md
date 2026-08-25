---
name: mir-cloud-cloudflare
description: "Make It Right (Cloudflare module). The reliability and cost review layer for a Cloudflare deployment, loaded after the provider is chosen - not a how-to for Workers. Covers: the isolate contract and where CPU-time billing stops applying; compatibility_date as the real version pin; the limits block as the only spend bound there is - budget alerts do not cap; KV vs D1 vs Durable Objects as three different consistency contracts; Durable Object single-thread throughput, sharding, alarms and gates; R2 Class A ops on small-object ingest, r2.dev as a non-origin, jurisdiction vs location hint for residency; secrets vs plaintext vars; and the 18 November 2025 outage as the 'config data is a deploy' lesson. Chains: mir-cloud then this. TRIGGER once mir-cloud Gate 5 has settled on Cloudflare - a wrangler or Terraform Cloudflare change, a Durable Object class, R2 bucket layout and public access, KV/D1 consistency, bindings and secrets, Containers, or a residency review. SKIP for the other providers - mir-cloud-aws, mir-cloud-gcp and mir-cloud-azure each get their own module. SKIP while the provider is undecided: comparison, elimination and the cost model are mir-cloud, and loading this before Gate 5 biases the choice. SKIP for app code - handlers are mir-backend, UI is mir-frontend, schema is mir-database. SKIP for pipeline controls identical on every provider (action pinning, SBOM, secret scanning, plan-vs-apply review) - mir-devsecops."
trigger: /mir-cloud-cloudflare
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-cloud-cloudflare · Make It Right (Cloudflare)

Bottom tier of a two-tier chain: `mir-cloud` decides **where the workload belongs** (provider-neutral, Gates 0–7) → **this** carries Cloudflare mechanics. Reach for it at **Gate 5** (service mapping, residency and exit cost, after sign-off), **Gate 6** (Wrangler/Terraform), and **Gate 7** (security, reliability and cost review).

**This is a review layer, not a tutorial.** Cloudflare ships excellent first-party skills for *how* to write a Worker, a Durable Object or a `wrangler` command; load one of those to build. This module asks the other question — what will this deployment do under load, under failure, and on the bill — and it asks it inside the pillar's gates.

**This module contributes nothing before Gate 5, and that is deliberate.** The pillar's rule is that naming a provider before the workload is characterized turns the rest of the conversation into justification. Cloudflare is the provider most often named for aesthetic reasons, so loading its mechanics during Gates 0–4 is the *worst* case of that bias, not a mild one. If the provider is still open, close this and go back to `mir-cloud`.

**Comparison facts stay in the pillar.** The 5-minute CPU ceiling, the 128 MB isolate, the 10 GB Durable Object cap, the ~1,000 req/s single-object soft limit, R2's zero egress, Containers' billed egress, R2 Class A/B rates and the exit-cost table are load-bearing for `mir-cloud` Gate 3 eliminations and Gate 4 rankings. This module cites them; it does not restate them.

**Surface state, verified 25 August 2026.**

| Thing | State | Why it matters here |
|---|---|---|
| Terraform Cloudflare provider | **5.24.0** (24 Aug 2026). v5.0.0 was a ground-up rewrite (29 Jan 2025); the v4 line still gets patches (4.52.8, 24 Jun 2026) | A v4 configuration is not a v5 configuration. §12 |
| Migration tooling | `tf-migrate` supersedes the in-provider `cmd/migrate`, deprecated in **5.19.0** (24 Apr 2026); state upgraders run automatically from 5.19 | Following a 2025-era migration write-up now costs you a manual state rewrite you did not need |
| Wrangler | **4.125.0** (20 Aug 2026); v4.0.0 landed 13 Mar 2025; v3 is the `legacy` dist-tag | Wrangler ships every few days. Pin it in `devDependencies`; `npx wrangler` unpinned is a moving deploy tool |
| Durable Objects storage backend | New namespaces **must** be SQLite-backed (changelog 9 Jul 2026). SQLite GA 7 Apr 2025 | Any KV-backed-DO guidance you find is now legacy-only. §5 |
| Config format | Cloudflare recommends `wrangler.jsonc` for new projects, and says some newer features are JSON-config only | A `.toml` example from a blog post may not express a newer binding |

## The Cloudflare footguns AI walks into most

### 1. The isolate contract — and it is not "a small container"

Workers run as V8 isolates. The consequences AI treats as incidental are the contract:

- **No host filesystem, no native binaries.** Cloudflare's security model says it plainly: only JavaScript and WebAssembly are accepted, and the sandbox uses an empty mount namespace with `seccomp` blocking filesystem syscalls. **The bare line "Workers have no filesystem" is now stale, though** — `node:fs` exposes an in-memory *virtual* filesystem (`/bundle` read-only, a writable `/tmp`, `/dev/null|random|full|zero`), on by default with `nodejs_compat` and a compatibility date of **2025-09-01 or later**. `/tmp` is per-request and non-persistent. Say "no *host* filesystem and no native binaries," or you will be corrected by a Worker that imports `node:fs` and works.
- **128 MB is per *isolate*, not per invocation** — and an isolate serves many concurrent requests. On exceeding it the runtime drains in-flight requests and starts a new isolate (error 1102, Logpush outcome `exceededMemory`). A memory bug therefore shows up as intermittent 1102s under concurrency, not as a clean OOM on one request.
- **Global scope is shared state across requests in the same isolate.** Anything you cache at module scope is visible to the next tenant request that lands on that isolate. Never cache per-user data there. This is the single most common security defect in AI-generated Worker code, and it is invisible in local development where you are the only user.
- **Startup time is 1 second, enforced at upload, on the global scope only.** Exceeding it fails the deploy with `Script startup exceeded CPU time limit` (error 10021), and Wrangler emits a CPU profile plus `startup_time_ms`. **The widely repeated 400 ms figure is pre-October-2025** — it was raised on 10 Oct 2025. Move expensive init out of module scope into the handler or build time.
- **CPU time is not wall time, and neither is one number.** Waiting on `fetch()`, KV, D1 or a socket does not accrue CPU. HTTP-triggered Workers have **no wall-clock cap** while the client stays connected; `ctx.waitUntil()` buys up to 30 s past the response; runtime updates give in-flight requests a 30 s grace period. Cron triggers, queue consumers and Durable Object alarm handlers cap at **15 minutes of wall time** — and note that Cloudflare's *pricing* page calls that same 15 minutes CPU time while the *limits* page calls it wall time. **Trust the limits page and say the pricing page disagrees;** do not quote either silently.
- **Six connections may be waiting for response headers at once.** Once headers arrive a connection stops counting, so a Worker can hold many streams open — a seventh *pending* connection is queued, not rejected. Workers invoked through a service binding share the parent's budget. And `fetch()` from a Worker to another Worker on the same zone without a service binding **fails**; it is not a counting question.

### 2. `compatibility_date` is the version pin, and an unset one is the worst case

Cloudflare's stability promise is real and it is narrow: *"The Workers runtime will support old compatibility dates forever,"* and behaviour changes ship behind flags that a date enables. Two things follow that AI never states.

- **If a date is not specified on upload via the API, it defaults to `2021-11-02`** — the oldest date, before any flag took effect. A Worker deployed by a script that forgot the field is not on "current"; it is on five-year-old semantics, silently.
- **A stale date is a silent downgrade, not a frozen-in-amber safety.** The docs warn that the documentation itself only describes the current date, so an old Worker's actual behaviour has to be reconstructed from the compatibility-flags page. Two live examples: `nodejs_compat` + `nodejs_compat_v2` are on by default from **2026-08-04**; the `node:fs` VFS from **2025-09-01**. Same code, different runtime, decided by one string.

Treat the date as a dependency pin: it is reviewed, it is bumped deliberately in its own change, and the change is tested — never edited in the same commit as a feature. `compatibility_flags` is the escape hatch in both directions: enable one change ahead of its date, or **disable** one that already became default. Cloudflare reserves the right to break a live Worker and says it will contact affected developers first — a strong intent, not a guarantee. Flag table: `references/workers-platform-mechanics.md`.

### 3. `limits` is the only spend bound Cloudflare gives you

Budget alerts are explicit about what they are not: *"Budget alerts are informational only. They do not pause or cap usage."* They are also Pay-as-you-go only — Enterprise contract accounts are unsupported. There is no account-level hard cap.

The one enforceable lever is the `limits` block, and it has exactly two keys:

```jsonc
// wrangler.jsonc
{
  "limits": {
    "cpu_ms": 50000,      // default 30000; max 300000 (5 min). Bounds a runaway loop.
    "subrequests": 200    // paid default 10000, paid max 10000000; free default and max both 50
  }
}
```

Three things to say out loud in the Gate 5 design:

- **`cpu_ms` bounds the CPU dimension of the bill, not the request dimension.** A request flood still bills per million requests with no ceiling. Cloudflare's own framing — "prevent accidental runaway bills or denial-of-wallet attacks" — over-promises relative to what the knob actually constrains. Put a WAF rate limit in front of the Worker if request volume is the exposure.
- **Limits are enforced on Cloudflare's network only, never in local development,** and are documented as supported for the Standard Usage Model. A `wrangler dev` run that passes proves nothing about the limit.
- **Set `cpu_ms` to slightly above the measured p99, not to the maximum.** The default 30 s is already ~13,000× the ~2.2 ms an average Worker uses. Raising it to 300,000 "to be safe" converts a bug into a five-figure invoice.

An older Worker may carry a limit nobody set: Cloudflare automatically applied a **50 ms** CPU limit to Workers that were on the legacy Bundled usage model before the 1 March 2024 move to Standard pricing. If an inherited Worker terminates at a suspiciously round number, check the configured limit before profiling the code.

### 4. KV, D1 and Durable Objects are three different consistency contracts

AI routinely swaps one for another because all three are "Cloudflare storage." They are not interchangeable, and the difference is the correctness of your application.

| | Consistency | Write model | Where it breaks |
|---|---|---|---|
| **Workers KV** | Eventually consistent. Changes are *usually* immediately visible at the writing location — the docs say **do not rely on it** — and **"up to 60 seconds or more"** elsewhere | 1 write/sec **per key**, both plans | Read-modify-write. Counters. Anything where a stale read is a bug rather than a slower page |
| **D1** | Single primary, **single-threaded, one query at a time**, backed by one Durable Object. Read replication adds **sequential consistency** with bookmarks — and only if you use the Sessions API | 30 s max query duration; 10 GB hard database cap that *cannot* be raised | Concurrency. At 100 ms/query you get ~10 queries/sec; the queue then returns `overloaded` |
| **Durable Objects** | Serialized per object. Storage writes are coalesced into implicit atomic transactions; gates order them | One thread per object; ~200–1,000 req/s depending on work per request | A single global object used as a coordinator |
| **R2** | Object store; see §7 | | Using it as a database |

Three traps worth naming:

- **KV caches negative lookups too.** A read that found nothing is cached like any other, so *creating* a key is as delayed as changing one. Code that writes then immediately re-reads to confirm is not confirming anything.
- **`cacheTtl` trades freshness for cost linearly.** Minimum 30 s, default 60 s; raising it raises the staleness window by the same amount. Cloudflare's own recommendation for write-heavy keys is to route writes through a Durable Object and read from KV elsewhere.
- **D1 read replication is not automatic and is not confirmed GA.** Queries go to the primary unless you call `withSession()`; a replica "may be arbitrarily out of date"; the Sessions API is binding-only, not REST. The only status Cloudflare has formally published is *public beta* (10 Apr 2025) even though the current docs page reads as GA. Note also that Cloudflare never uses the words "strongly consistent" about D1 — do not put them in a design.

### 5. A Durable Object is one thread, and that is the design constraint

The pillar eliminates on the 10 GB cap and the single-object ceiling. What belongs here is what to do about it.

- **Model the object as the *atom of coordination*** — one room, one document, one order, one game session — and shard. Cloudflare's own worked example: 50,000 players at 10 updates/sec is 500,000 req/s, which is 500–1,000 session objects, *not* one coordinator. `Required DOs = total req/s ÷ per-DO capacity`, where per-DO capacity is ~1,000 for pass-through, ~500–750 with JSON parsing and validation, and **~200–500 once you transform data or write storage.** The limits page's flat "soft limit of 1,000 req/s" is the optimistic end of a documented range; plan against the range.
- **Location is decided by the first `get()` and never changes.** A Durable Object is instantiated near where its first request came from, and Cloudflare states objects do not currently relocate. That makes a CI smoke test from a US runner a permanent US placement for an EU customer's object. `locationHint` is best-effort. **Only `jurisdiction()` is enforced** — `env.NS.jurisdiction("eu").idFromName(name)` — and the same name yields a different ID per jurisdiction, so retrofitting it is a data migration.
- **Hibernation resets in-memory state.** After 70–140 seconds of inactivity an object is evicted; WebSocket clients stay connected to the edge but the constructor re-runs and in-memory fields are gone. Persist per-connection state with `serializeAttachment` / `deserializeAttachment`. Outbound `connect()` or WebSocket connections keep an object alive for up to 15 minutes — which is a cost decision, since duration billing does not accrue during hibernation.
- **Durable Objects break the "you are not billed for waiting" rule.** Workers bill CPU; Durable Objects bill **wall-clock duration** while running or resident and unable to hibernate, at 128 MB regardless of actual use. An object holding an idle connection is on the meter in a way an idle Worker is not. Co-located instances of one class may share an isolate's 128 MB and are each still billed for the full amount.

### 6. Gates, `blockConcurrencyWhile`, and alarms

- **Input gates order events around *storage* operations only.** *"Input gates only protect during storage operations. Non-storage I/O like `fetch()` or writing to R2 allows other requests to interleave."* Awaiting a `fetch()` **opens the gate**. The check-then-act you wrote is atomic against storage and racy against the network. Fix with optimistic locking: read a version, make the call, verify the version has not moved before writing.
- **Output gates hold outbound messages — including your own `fetch()` calls — until a pending write commits;** if the write fails the messages are replaced with errors and the object is restarted. That is what makes "I told the client it worked" safe. It does not help two calls issued from the *same* event without an intervening `await`; Cloudflare documents that gap and calls it an acceptable caveat.
- **`blockConcurrencyWhile()` has a 30-second timeout and failure is fatal by design.** A throw inside the callback terminates and resets the object, deliberately, so it cannot be stuck half-initialized; exceeding 30 s also resets it. Wrap the body in `try/catch` if you want to survive. Use it in the constructor for schema migration and init — and cost it: ~5 ms in there caps the object at ~200 req/s.
- **Storage writes without an intervening `await` are coalesced into one implicit atomic transaction.** That is a correctness guarantee you get for free and lose the moment you `await` between them — so an "obvious" refactor that awaits each `put()` in a loop converts one atomic commit into N independent ones, each of which can be the last one that succeeded.
- **Alarms are at-least-once with a bounded retry budget.** One alarm per object; `setAlarm` overwrites; retries use exponential backoff from a 2-second delay for **up to 6 attempts**, only on an uncaught exception, and only for the most recent `setAlarm`. Past six, the work is silently gone. If the job must not be lost, catch inside `alarm()` and schedule the next one before returning. Cloudflare also warns that alarms may in rare cases fire more than once — the handler must be idempotent, which is a `mir-backend` concern in the same design.

### 7. R2 — the bill moved from bandwidth to operations, and IA is not "cheaper"

Egress is free; that is the pillar's Gate 4 row. What lands here is where the money went instead.

- **Multipart ingest is Class A three ways.** `CreateMultipartUpload`, **every** `UploadPart`, and `CompleteMultipartUpload` are each Class A, alongside `PutObject`, `CopyObject`, `ListObjects`, `ListParts` and the bucket `Put*` calls. A 100-part upload is 102 Class A operations. Reads (`GetObject`, `HeadObject`, `HeadBucket`, the bucket `Get*` calls) are Class B. Count **objects and parts written per month**, not gigabytes, before you call R2 cheap for an ingest workload.
- **Infrequent Access is a trap for write-heavy small objects.** IA halves nothing that matters: storage drops, but Class A is **2× Standard** and Class B **2.5×**, plus a per-GB retrieval fee and a **30-day minimum duration**. The free-tier allowances are Standard-class only. IA wins for large, cold, rarely-read objects and loses for everything else — and a lifecycle rule that transitions eagerly can cost more than it saves.
- **One write per second to the same key.** Objects and storage per bucket are unlimited; the per-key write rate is not.
- **Multipart part sizes are documented on a different page from every other R2 limit.** The limits page omits them; minimum part size, maximum part size and the requirement that all parts except the last be the same size live on the multipart-objects page. An uploader written against the limits page alone will produce parts R2 rejects.
- **Location Hints are best effort and are not residency.** *"Jurisdictional Restrictions guarantee objects in a bucket are stored within a specific jurisdiction"* — `eu`, `us`, `fedramp` — addressed by a jurisdiction-specific S3 endpoint or the `jurisdiction` field on the binding. **Jurisdiction cannot be changed after the bucket is created.** If Gate 1 recorded a residency requirement, this is a Gate 5 decision, not a Gate 6 detail.

### 8. `r2.dev` is a development URL, and public access is a decision with three parts

Cloudflare's wording is unambiguous: *"Public access through `r2.dev` subdomains is rate-limited and should only be used for development purposes,"* and *"Avoid creating a CNAME record pointing to the `r2.dev` subdomain. This is an unsupported access path."* No numeric rate is published — do not quote one.

- **`r2.dev` gets no cache, no WAF, no bot management, no Access.** Those require a custom domain on a zone you control. A production origin on `r2.dev` is un-cached, un-protected and rate-limited.
- **Enabling a custom domain does not disable `r2.dev`.** The docs warn explicitly: if you put WAF or Access in front of a custom domain and leave the `r2.dev` subdomain enabled, the bucket stays publicly reachable around your controls. Turn it off, then verify by fetching it.
- A signed URL or a Worker in front of the bucket is object-level authorization and carries the signer's authority — the pillar's presigned-URL rule applies unchanged.

### 9. Secrets — `vars` is plaintext configuration, and that is all it is

Cloudflare's own guidance is direct: *"Do not use `vars` to store sensitive information in your Worker's Wrangler configuration file. Use secrets instead."* `vars` are *"not encrypted and are useful for storing application configuration"*; secrets are the same binding shape except *"secret values are not visible within Wrangler or Cloudflare dashboard after you define them."* There is no documented read-back path for a secret once set — `wrangler secret list` returns names.

- `wrangler secret put KEY` (stdin-friendly), `wrangler secret bulk` (JSON or `.env`, up to 100 per request, `null` deletes — deletion unsupported from `.env`), `wrangler secret delete`. **Every `wrangler secret` subcommand deploys a new version immediately**; use `wrangler versions secret` when you need the version without the deploy.
- Local secrets go in `.dev.vars` or `.env`, and the docs tell you to gitignore `.dev.vars*` and `.env*`. Check the repo's ignore file in the review — this is the leak that actually happens.
- **Secrets Store is open beta**, account-scoped, and unavailable on the Cloudflare China Network. Do not write it into a design as GA.

### 10. "Cloudflare is zero egress" is a product claim, not a platform claim

R2 and Workers do not bill egress. **Containers do** — per GB beyond an included monthly allowance that differs by region, with memory and disk billed on *provisioned* resources and CPU on actual use. A design that reaches for Containers to escape the isolate's constraints (§1) has quietly re-acquired the cost line the pillar's Gate 4 ranked Cloudflare #1 for not having. The per-region egress rates and allowances are in `mir-cloud/references/cost-model.md`; the instance-type table and the compute rates are only on Cloudflare's Containers pricing page. Put Containers egress in the Gate 5 cost model on its own line, the way the pillar makes you do for a hyperscaler.

### 11. A config-data change is a deploy — 18 November 2025

The provider's own post-incident report, which is the primary source, not a vendor blog.

**What happened.** At **11:05 UTC** a ClickHouse permissions change made users' access to underlying `r0` tables explicit. A metadata query that built the Bot Management **feature file** — `SELECT name, type FROM system.columns WHERE table = 'http_requests_features'` — had always assumed it would only see the `default` database. It now returned the `r0` columns as well, *"effectively more than doubling the rows in the response."* The proxy preallocates memory for a bounded feature count: *"Currently that limit is set to 200, well above our current use of ~60 features."* The oversized file blew the bound, and the FL2 proxy's Rust code panicked — `thread fl2_worker_thread panicked: called Result::unwrap() on an Err value` — returning 5xx network-wide. Impact from **11:20 UTC**; core traffic largely normal by **14:30**; all systems functioning **17:06 UTC**.

**Why it was hard to diagnose.** The file regenerated every five minutes and the ClickHouse nodes were being updated gradually, so good and bad files alternated and the network recovered and failed repeatedly. Cloudflare's status page went down independently at the same time by coincidence. The team *"initially wrongly suspected"* a hyper-scale DDoS attack. It was not an attack: *"The issue was not caused, directly or indirectly, by a cyber attack or malicious activity."*

**A discrepancy in the primary source, stated because it matters.** ~60 features doubled is ~120, which does not exceed 200. The published report does not reconcile that arithmetic. **Take the mechanism from the PIR and do not quote the arithmetic as if it closed.**

**What to take into the design.**

| Lesson | The check to run on your own system |
|---|---|
| Configuration *data* propagates faster and with less review than code | List every artefact that reaches production without a code deploy: feature files, rule sets, ML models, allow-lists, remote flags. Each one needs the same staged rollout and rollback as code |
| Internally-generated data is still untrusted input | Cloudflare's own first remediation: *"Hardening ingestion of configuration files ... like user-generated input."* Validate schema, row count and size at the consumer, and reject rather than parse |
| A bound that is "well above current use" is still a bound | Fail soft at the limit. `unwrap()` on a `Result` — or its equivalent in your language — turns a data anomaly into a global outage |
| Blast radius follows the shared dependency, not the org chart | Turnstile, Workers KV, Access, the dashboard and Email Security all failed through one proxy module. Name what sits in front of everything you run and what happens when it panics |
| Partial failure reads as an attack | Alternating recovery cost roughly an hour of misdirected investigation. Build the signal that distinguishes "our config changed" from "traffic changed" |

Cloudflare's committed remediations — global kill switches for features, bounding core dumps, reviewing failure modes across proxy modules — are a usable checklist for anyone running a shared data plane.

### 12. Terraform and Wrangler — the v5 rewrite, and pinning a tool that ships weekly

- **The Cloudflare Terraform provider v5 is a rewrite, not a bump.** v5.0.0 (29 Jan 2025) regenerated the provider from Cloudflare's API schema, changing attribute and resource shapes. From **5.19.0** the provider carries automatic state upgraders and `tf-migrate` handles the HCL; the older Grit-based and in-provider `cmd/migrate` routes are deprecated. Run the migration as its own change and expect benign `(known after apply)` plan noise on the first apply — then re-plan and require it to be empty.
- **Pin both, and commit the lock files.** `cloudflare = { source = "cloudflare/cloudflare", version = "~> 5.24" }` plus `.terraform.lock.hcl`; Wrangler in `devDependencies` plus the package lock. `npx wrangler` unpinned means the tool that deploys production changed since the last release and nobody chose it.
- **The API token is the blast radius.** Scope it to the account, the zone and the specific permission groups the plan needs; never use a Global API Key in CI. Terraform state holds token values and binding contents in plaintext — encrypt the backend and restrict read access as tightly as production data. The pillar's OIDC-over-static-keys rule applies to whatever CI mints the token.
- **Read the plan, not the HCL.** The v5 provider derives resource shapes from Cloudflare's API schema, so a field you did not write can appear in the diff and a field you did write can be computed away. A plan that shows a replacement on a bucket, a namespace or a Durable Object migration is a data event, not a formatting one.
- **Two control planes for one Worker is a drift generator.** Wrangler deploys code, bindings and secrets; Terraform manages zone, DNS, WAF, R2 buckets and namespaces. Draw the line explicitly in the Gate 5 design and write it in the repo, or `wrangler deploy` will silently revert a Terraform-managed binding. Split and worked examples: `references/wrangler-and-bindings.md`.

## How this slots into the pipeline

- **Gate 5 (Architecture Review, after sign-off):** map each workload from the pillar's Gate 0 restatement to concrete Cloudflare products; state the storage-consistency choice per data set with the reason; state residency as *jurisdiction*, not location hint; state the exit cost per adopted service — Durable Objects, D1 and Workers KV have no equivalent elsewhere and the pillar rates them High. Read `references/workers-platform-mechanics.md`.

  The Cloudflare-specific rows the design must carry before Gate 6 opens:

  ```
  RUNTIME      compatibility_date, the flags set, and who owns bumping it
  SPEND        limits.cpu_ms per Worker, plus the rate limit covering request volume
  STATE        per data set: KV | D1 | Durable Object | R2, and the consistency it needs
  OBJECTS      the DO sharding key, expected req/s per object, and the eviction plan
  RESIDENCY    R2 bucket jurisdiction and DO jurisdiction, per data set, fixed at creation
  CONFIG DATA  every artefact that reaches production without a code deploy (§11)
  EGRESS       zero for R2/Workers; a real line for Containers
  EXIT         per adopted service: engineering months + data transfer to leave
  ```
- **Gate 6 (Implementation):** IaC only, provider and Wrangler pinned, secrets via `wrangler secret` or Secrets Store and never `vars`. `compatibility_date` explicit on every Worker. `limits.cpu_ms` set from a measured p99. `r2.dev` disabled on any bucket with a custom domain. Durable Object migrations declared in config, not applied by hand. Read `references/wrangler-and-bindings.md` before writing config.
- **Gate 7 (Production-Readiness):** the security-reviewer works §§1, 8, 9 and the Security section — global-scope tenant leakage, public buckets, plaintext `vars`, token scope. The reliability-reviewer works §§4–6 and §11 — the consistency contract per data set, DO sharding and alarm retry budgets, and the config-data blast radius. The cost review works §§3, 7, 10 and diffs the first real bill against the pillar's Gate 5 model, with operations and Containers egress on their own lines.

## References

| File | What it holds | Read at |
|---|---|---|
| `references/workers-platform-mechanics.md` | Isolate and execution-model table with the CPU/wall-time split and the pricing-page conflict; compatibility date and flag mechanics with the dated defaults; the KV/D1/DO/R2 consistency matrix; Durable Object sharding arithmetic, lifecycle and eviction, gates, `blockConcurrencyWhile` and alarm retry semantics; residency via jurisdiction; the 18 Nov 2025 timeline in full with the config-data checklist | Gate 5 design, Gate 7 reliability review |
| `references/wrangler-and-bindings.md` | `wrangler.jsonc` anatomy and the binding table; secrets vs `vars` vs Secrets Store, and the `.dev.vars` leak; environments and versioned deploys; how to measure CPU time and startup time before setting `limits`; R2 operation classification, lifecycle and public-access checklist; Terraform provider v5 migration and the Wrangler/Terraform ownership split; API token scoping | Gate 6, Gate 7 security review |

## Security

Cloudflare-specific security. Provider-agnostic items — OIDC over static keys as a principle, Actions pinning, secret scanning, residency enumeration — are in `mir-cloud` and `mir-devsecops`; do not restate them here.

**Evidence note.** This module deliberately carries **no CVE table.** Cloud-provider vulnerabilities are overwhelmingly remediated server-side with no customer action, so a list of them is something an engineer can do nothing about. What is actionable on Cloudflare is a *default*, a *consistency contract*, a *dated limit or price*, or a *published post-incident report* — so that is what is cited.

The review list, in the order findings actually appear:

1. **Per-user data cached in module scope** — §1. The isolate is shared across requests; a global map keyed by nothing is a cross-tenant leak. Highest-value read of any Worker diff.
2. **`vars` holding anything sensitive** — §9. Grep the wrangler config for tokens, keys and connection strings; check `.dev.vars*` and `.env*` are gitignored.
3. **A public bucket still reachable on `r2.dev`** — §8. Enabling a custom domain does not disable it; fetch the `r2.dev` URL and confirm it fails.
4. **No `compatibility_date`, or one nobody chose** — §2. An API upload without it runs on 2021-11-02 semantics.
5. **No `limits.cpu_ms`, and no rate limit in front** — §3. Budget alerts do not cap. Both dimensions need a control or the design must say the exposure is accepted.
6. **A Global API Key, or an over-scoped API token, in CI** — §12. Scope to account, zone and permission group; state is plaintext, so restrict who can read the backend.
7. **Residency claimed from a location hint** — §5, §7. Only `jurisdiction` is enforced, and it is immutable after creation. A design that says "hinted to the EU" has not met a residency requirement.
8. **A Durable Object used as a global singleton** — §5. Not only a throughput bug: one object is one failure domain for every tenant routed through it.
9. **Config data that ships without a deploy path** — §11. Feature files, rule sets, model artefacts and remote flags need staged rollout, size and schema validation at the consumer, and a kill switch.
10. **No observability on the numbers you designed against.** Workers Logs / Logpush with the invocation outcome (`exceededMemory`, `exceededCpu`) and `startup_time_ms` from deploy output are how you learn the p99 before the bill does.

**If a token or secret is believed exposed:** roll the Cloudflare API token first (deleting it invalidates it immediately — there is no session to revoke separately), then `wrangler secret put` the affected Worker secrets, which deploys a new version, then read the Audit Log for what the token did. A Worker secret rotated without a redeploy is not rotated.

## You are wiring this wrong if…

- The Worker caches anything per-user at module scope, or you cannot say what in global scope survives between requests.
- The wrangler config has no `compatibility_date`, or has one nobody can explain, or bumping it is bundled into a feature commit.
- There is no `limits.cpu_ms` and no rate limit, and the design does not state that runaway spend is an accepted risk.
- You chose KV for something that reads its own writes, or D1 for something concurrent, or a Durable Object for something that does not need serialization.
- The Durable Object design has one object where it needs a shard key, or you cannot state expected requests per second per object.
- Residency is met with a location hint rather than a jurisdiction, or the buckets already exist and the jurisdiction cannot now be changed.
- An `alarm()` handler relies on retries to make progress, and nobody has counted that there are six of them.
- A bucket has a custom domain with WAF in front and `r2.dev` still enabled.
- The design says "no egress on Cloudflare" and also uses Containers.
- Both Wrangler and Terraform can write the same binding and nobody wrote down which one owns it.
- A Durable Object storage loop awaits every write, so what used to be one atomic commit is now N.
- The failover story is "Cloudflare is global" and nobody has named what a bad configuration artefact would do to every one of those locations at once.
- You reviewed the `.tf` files and not the `terraform plan`, or `npx wrangler` is unpinned in CI.

## Edit boundary

Four questions, in order, before adding anything here:

1. **True on AWS, GCP and Azure too** (egress discipline, exit cost, residency enumeration, gate structure)? → **up** to `mir-cloud`.
2. **Is this fact used by a `mir-cloud` Gate 3 elimination or a Gate 4 ranking row** (the 5-minute CPU ceiling, the 128 MB isolate, the 10 GB Durable Object cap, R2's zero egress and Class A/B rates, Containers' billed egress, no rentable GPU)? → **it stays in `mir-cloud`.** Cite it from here; never repeat it. A number maintained in two files becomes wrong in one of them.
3. **Identical on every provider's pipeline** (Actions pinning, SBOM, secret scanning, plan-vs-apply review)? → **across** to `mir-devsecops`. Application behaviour — handler logic, transactions, idempotency implementation — is `mir-backend`.
4. **True only because the provider is Cloudflare** (the isolate contract, compatibility dates, the `limits` block, the KV/D1/DO consistency split, Durable Object gates and alarms, R2 operation classes, `r2.dev`, the Cloudflare Terraform provider)? → **here.**

A different provider → its own `mir-cloud-<provider>` module. Never widen this one.

## Provenance

Retrieved **25 August 2026**. Sources are Cloudflare's own documentation, changelog and post-incident report, the `cloudflare/terraform-provider-cloudflare` release history, and the `wrangler` npm release history. Verify before quoting:

- Workers limits, CPU vs wall time, memory, startup time — `developers.cloudflare.com/workers/platform/limits/` (prefer this over the pricing page where they disagree on the 15-minute cron/queue/alarm figure)
- Workers pricing and the custom-limits framing — `developers.cloudflare.com/workers/platform/pricing/`
- Budget alerts do not cap — `developers.cloudflare.com/billing/manage/budget-alerts/`
- The `limits` block and config keys — `developers.cloudflare.com/workers/wrangler/configuration/`
- Compatibility dates and flags — `developers.cloudflare.com/workers/configuration/compatibility-dates/` and `.../compatibility-flags/`
- Isolate sandbox, no native binaries, and the `node:fs` VFS — `developers.cloudflare.com/workers/reference/security-model/` and `.../runtime-apis/nodejs/fs/` (the security-model page's absolute "no filesystem API" wording predates the VFS)
- Durable Objects limits, gates, alarms, lifecycle, pricing, data location — `developers.cloudflare.com/durable-objects/platform/limits/`, `.../api/state/`, `.../api/alarms/`, `.../concepts/durable-object-lifecycle/`, `.../platform/pricing/`, `.../reference/data-location/`; gate *definitions* only exist in `blog.cloudflare.com/durable-objects-easy-fast-correct-choose-three/`, which the docs link to
- KV consistency and limits — `developers.cloudflare.com/kv/concepts/how-kv-works/` and `.../kv/platform/limits/`
- D1 limits and read replication — `developers.cloudflare.com/d1/platform/limits/` and `.../d1/best-practices/read-replication/` (public beta is the only formally announced status)
- R2 pricing, operation classes, limits, public buckets, data location — `developers.cloudflare.com/r2/pricing/`, `.../r2/platform/limits/`, `.../r2/buckets/public-buckets/`, `.../r2/reference/data-location/`
- Containers pricing including egress — `developers.cloudflare.com/containers/pricing/`
- Secrets and Secrets Store — `developers.cloudflare.com/workers/configuration/secrets/` and `developers.cloudflare.com/secrets-store/`
- The 18 November 2025 outage — `blog.cloudflare.com/18-november-2025-outage/` (primary source; prefer it over any secondary account, and note the ~60-features-versus-200-limit arithmetic is not reconciled in the published text)
- Terraform provider — `github.com/cloudflare/terraform-provider-cloudflare/releases` and `registry.terraform.io/providers/cloudflare/cloudflare/latest/docs/guides/version-5-upgrade`

**Quote nothing from this file you have not just confirmed at the source.** Limits, defaults, prices and provider versions here are a 25 Aug 2026 snapshot and are the highest-decay content in this module. Every page above is also fetchable as raw markdown by appending `index.md` to the URL, which is the fastest way to check exact wording.
