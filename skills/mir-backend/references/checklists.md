# Checklists — Gate 6 (codegen) & Gate 7 (production-readiness)

These are **strict, checked-against** lists. For each item: either it's satisfied (point to the code), it's consciously N/A (say why), or it's a finding. "Looks fine" is not an allowed state.

---

## Gate 6 — Codegen checklist (while writing)

### Correctness
- [ ] Every state-changing path is **idempotent** OR explicitly documented as unsafe-to-retry
- [ ] Concurrency-safe: contended rows locked (`FOR UPDATE`) or guarded by optimistic version / conditional UPDATE
- [ ] **Transaction boundaries explicit** — you can point to where each tx begins and commits
- [ ] Irreversible actions (charge, send, ship) are OUTSIDE any tx that may roll back, and gated by idempotency keys
- [ ] Invalid state transitions are rejected, not silently allowed
- [ ] Rollback / compensation behavior defined for multi-step writes

### Security

The framework-agnostic rules and the reasoning behind them are in the pillar's `## Security` section. This is the checkable form.

- [ ] **Object-level authorization** on every access keyed by a client-supplied ID — the ownership/membership predicate is in the query that loads the row, not in a branch after it (BOLA/IDOR)
- [ ] **Function-level authorization** on admin and internal routes — a role check, not just a valid token
- [ ] Input validation strict at the boundary; **no mass assignment** (allow-list the writable fields per endpoint and per role; never bind a raw body to a persisted model)
- [ ] **Response serialization is an allow-list too** — no password hashes, internal flags, soft-deleted rows, or other users' columns leaking through a "return the model" handler
- [ ] Every client-side check (hidden control, disabled field, form validation, UI role gate) is **re-enforced server-side**; the endpoint is callable directly
- [ ] **Tenant isolation** on every query, cache key, rate-limit counter, search index, storage prefix, background job, and export
- [ ] Secrets never logged; PII never logged (or explicitly redacted at the logging boundary, not per call site)
- [ ] Error responses carry an opaque message + correlation ID — **no stack trace, SQL, or upstream response body**; debug mode off in production
- [ ] **Injection**, per interpreter — bound parameters for SQL/NoSQL (and JSON values type-coerced before reaching a document query); a fixed executable plus an **argv array** for shell-outs, with operands validated so a leading `-` cannot inject an option; context-correct encoding for templates and LDAP/XPath; CR/LF rejected in response headers and mail fields; identifiers (table/column/sort field) allow-listed
- [ ] **No untrusted bytes reach a native object deserializer** (Java serialization, PHP `unserialize`, Ruby `Marshal`, unsafe YAML loaders, the Python stdlib object serializer) — including cache entries, queue messages, and session cookies
- [ ] **No SSRF** on any user-supplied URL/host: host allow-list; every resolved A/AAAA checked against private/loopback/link-local/reserved ranges **and the connection made to that validated IP** (with `Host`/SNI preserved) so the client's own re-resolution cannot rebind; redirects disabled or re-validated per hop; connect and read timeouts set
- [ ] Webhook signatures verified against the **raw body**, in **constant time**, before parsing
- [ ] Rate limit, request-body size limit, and bounded pagination on every public endpoint
- [ ] No privilege-escalation path — a user cannot grant themselves or another user a role, a tenant, or a scope

Out of scope for this pillar (do not file findings here): dependency pinning, install scripts, CI secrets, and image provenance → `mir-devsecops`. Cloud IAM, metadata-endpoint hardening, and network defaults → `mir-cloud`. Row-level security policies and schema-level constraints → `mir-database`.

### Operations
- [ ] Structured logging on the path (not bare prints)
- [ ] Correlation/request ID propagated through the flow and into downstream calls
- [ ] Business + technical metrics emitted (success/failure counts, latency)
- [ ] Health check / readiness reflects real dependency state
- [ ] Graceful shutdown drains in-flight work; no lost messages on deploy

### Database

If this change also creates or alters the schema, `mir-database` owns that decision and runs first. These items cover what the application code has to get right against a schema that already exists.

- [ ] Migration is safe on **populated** tables (see migration-reviewer)
- [ ] Every index is justified by an actual query pattern (no speculative indexes)
- [ ] No N+1 (eager-load or batch); fan-out bounded
- [ ] Lock contention considered; long transactions avoided

### Failure handling
- [ ] Retries are **bounded** and have a budget (not infinite)
- [ ] Timeouts defined on every external call
- [ ] Circuit breaker / fallback where a slow dependency would exhaust resources
- [ ] Dead-letter / manual-replay path for un-processable messages

---

## Gate 7 — Production-readiness review (after writing)

Run by the reviewer sub-agents. Each finding: **severity (Critical/High/Med/Low) + file:line + the fix**. Reviewers do not edit code; they report. The orchestrator fixes Critical/High before declaring done.

### Reliability-reviewer focus
- Idempotency actually implemented (not just retries)
- Partial-failure paths handled (the "half succeeded" cases from the failure-mode catalog)
- State machine: every transition guarded against concurrency
- Backpressure/timeouts/circuit-breakers present where Gate 4 flagged them
- Cache invalidation strategy matches the consistency guarantee promised in Gate 5
- Observability matches the Gate 5 plan (correlation IDs, metrics, alerts)

### Security-reviewer focus

Priority order — the first three are data-breach class and are Critical by default.

- Object-level authorization on every fetch/update/delete by ID; function-level authorization on admin and internal routes
- Tenant isolation verified on queries AND cache keys AND background jobs
- No mass assignment; request models are allow-lists — and response models are too
- Secrets/PII not in logs or error responses; no stack trace or SQL returned to a client; debug mode off
- Injection checked in every form present: SQL, NoSQL operator, command, template, LDAP/XPath, response header, log line. Identifiers allow-listed
- Untrusted input never deserialized by a native object deserializer
- SSRF: allow-list; connection pinned to the validated resolved IP (not just a pre-flight check the client re-resolves past); redirects re-validated; timeouts
- Client-side checks not relied on — every UI gate has a server-side equivalent
- Webhook signature verification against the raw body, constant-time compare
- Rate limits, body-size limits, bounded pagination
- Privilege-escalation paths (can a user grant themselves a role, a tenant, or a scope?)

### Migration-reviewer focus (only if migrations changed)
- Safe on populated tables (no blocking `ADD COLUMN NOT NULL` without default on large tables)
- Backward-compatible with the currently-deployed code (expand/contract)
- Rollback path exists and is tested
- No data loss on down-migration (or down-migration explicitly forbidden)

Engine-agnostic depth is in `mir-database` (its `references/migration-safety.md` maps DDL statements to the lock each one takes). Engine mechanics are in `mir-database-postgres` / `mir-database-mongo`; ORM-tool mechanics are in the framework module (e.g. Alembic in `mir-backend-python-fastapi`).

---

## Final hand-off — the testing guide (always end with this)

The Prompt Architect rule: never finish without telling the user how to know it worked. Produce a short guide covering:

1. **Golden path** — the one command/request that proves the feature works.
2. **The invariant tests** — one test per INV-* from Gate 3 (e.g., "fire two concurrent orders at the last unit → exactly one succeeds").
3. **The failure-mode tests** — retry the same idempotency key twice → one side effect; kill the dependency mid-flow → graceful degradation.
4. **What was deferred** — any Med/Low finding or `pending` risk you consciously did not address, so the user decides.
