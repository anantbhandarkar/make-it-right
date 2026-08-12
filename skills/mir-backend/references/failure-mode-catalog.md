# Failure-Mode Catalog

The recurring ways AI backend code is *locally correct but operationally broken*. Each entry: the trap, why AI walks into it, and the question/mitigation that defuses it. Used in Gate 3 (declare failure modes) and Gate 4 (risk register).

The meta-pattern: **LLMs reason in static snapshots; distributed systems fail in timelines and under partial failure.** Most of these are timeline or partial-failure bugs.

---

## 1. Hidden invariant violations
Code passes tests, compiles, looks clean — and silently breaks a rule nobody wrote down (one active subscription, balance ≥ 0, refund ≤ charge). **Why AI misses it:** invariants are tribal knowledge, scattered across people/docs. **Defuse:** declare invariants explicitly in Gate 3; enforce them at the DB level (constraints, unique indexes) not just app logic — a constraint survives a buggy deploy, a background job, and the second service that starts writing the same table. Deciding which invariant is enforced where is `mir-database`'s enforcement-boundary step; run it first when the task also touches the schema.

## 2. Temporal logic failures
Retry semantics, eventual consistency, delayed jobs, saga compensation, webhook ordering, stale reads after writes. **Why:** AI thinks in snapshots, not timelines. **Defuse:** for every async/multi-step flow ask "what's the ordering, and what if it's out of order?" Webhook-before-DB-commit is the canonical one.

## 3. Partial failure handling
Redis down but DB up. Kafka ACK ok but consumer dies. Email sent but tx rolls back. Stripe webhook arrives before the DB row exists. **Why:** AI assumes happy-path infra. **Defuse:** for each external call, enumerate the half-success states. Outbox pattern for "DB write + external effect must agree."

## 4. Idempotency
AI adds retries but not **deduplication**. Result: duplicate payments, double emails, duplicate fulfillment, replay attacks. **Why:** AI assumes exactly-once; reality is at-least-once. **Defuse:** every state-changing endpoint that can be retried needs an idempotency key — decide where it's stored and its TTL. Side effects must be guarded by the key, not just the request.

## 5. State machine corruption
AI generates CRUD; the domain is a lifecycle. Forgets invalid transitions, concurrent transitions, rollback semantics, auditability. **Defuse:** enumerate states + valid transitions + explicitly-rejected transitions. Guard transitions with a conditional UPDATE (`WHERE status = 'PENDING'`) so concurrent transitions can't both win.

## 6. Observability blindness
Functionality ships before operability: no correlation IDs, structured logs, traces, audit logs, business metrics, or alert conditions. **Why:** it's invisible in a passing test. **Defuse:** Gate 5 requires the observability plan *before* code. A backend you can't debug is broken.

## 7. Backpressure & load shedding
No queue limits, bounded concurrency, circuit breakers, retry budgets, or degradation modes. **Why:** AI assumes infinite capacity. **Defuse:** ask about peak QPS in Gate 1; decide what to shed and how to degrade when a dependency is slow (not just down — *slow* is worse, it ties up resources).

## 8. Multi-tenant isolation
Missing tenant scoping, row-level security, cache partitioning, noisy-neighbor prevention, billing isolation → catastrophic cross-tenant data leaks. **Defuse:** every query filtered by tenant; cache keys namespaced by tenant; verify in the security review. Background jobs, exports, search indexes, and storage prefixes are queries too — they are where the missing filter usually is, because nobody reviews them. Database-enforced isolation (row-level security, tenancy layout) is `mir-database`. This is a data-breach-class bug.

## 9. Cache invalidation & consistency
AI adds caching but not an invalidation strategy. Missing: stale-while-revalidate, write-through vs write-back, **cache stampede** prevention, eviction implications. **Defuse:** for any cache, answer "what writes invalidate this, and what happens to N concurrent misses on a cold key?"

## 10. Security beyond auth
AI over-indexes on JWT/OAuth and misses: SSRF, insecure deserialization, **mass assignment**, timing attacks, **broken object-level authorization (BOLA/IDOR)**, secret and PII leakage in logs and error responses, injection in its less-famous forms, insecure defaults, privilege escalation. It also treats a client-side check as if it were a control — a hidden button and a form validator are UX; the endpoint is still callable directly. **Defuse:** the pillar's `## Security` section for the rules, `checklists.md` for the checkable form, and the security-reviewer at Gate 7. Especially verify object-level authz on every fetch-by-id. Supply-chain and pipeline security is a different pillar (`mir-devsecops`); cloud IAM and metadata-endpoint hardening is `mir-cloud`.

## 11. Data lifecycle neglect
No retention policy, GDPR/CCPA deletion, archival, soft-delete semantics, audit retention, PII classification, legal holds. **Why:** initial requirements rarely mention end-of-life. **Defuse:** ask retention + deletion in Gate 1 if PII is present.

## 12. Schema evolution blindness
AI writes schemas as if data starts empty forever. Weak at backward compatibility, rolling deploys, nullable migration phases, contract evolution, blue/green constraints. During a rolling deploy the old code and the new code run at the same time against one schema — every migration has to be correct for both. **Defuse:** migration-reviewer + expand/contract pattern. Engine-agnostic detail is in `mir-database` (`references/migration-safety.md`); engine mechanics in `mir-database-postgres` / `mir-database-mongo`; the migration tool's own footguns in the framework module (Alembic lives in `mir-backend-python-fastapi`).

## 13. Cost-unaware architectures
N+1 queries, overusing queues, excessive reads, chatty microservices, embeddings/vector queries everywhere, over-indexing. **Why:** AI optimizes elegance, not the cloud bill. **Defuse:** flag N+1 and fan-out in review; question every new index and every new network hop.

## 14. Human workflow ignorance
Never asks: who operates this, who debugs it at 2am, who owns failures, can a junior understand it, does support need tooling, what admin actions exist. **Defuse:** the Operations dimension in Gate 1; design a kill switch / manual-replay path for anything money-touching.

## 15. Requirement hallucination — the root cause
When requirements are incomplete, AI **invents them, confidently.** This is why prompting alone is insufficient and why this whole skill exists: it replaces invention with extraction (Gate 1), explicit surfacing (Gate 2), and gating. If you catch yourself filling a gap with a plausible default that the user never confirmed — stop, and put it in the Assumption Ledger as a question instead.
