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
- [ ] Authorization enforced **server-side on every object access** (no trusting client-supplied IDs without ownership check — BOLA/IDOR)
- [ ] Input validation strict at the boundary; no mass assignment (allow-list fields, never bind raw body to model)
- [ ] Secrets never logged; PII never logged (or explicitly redacted)
- [ ] Tenant isolation enforced on every query and cache key
- [ ] No SSRF on any user-supplied URL/host

### Operations
- [ ] Structured logging on the path (not bare prints)
- [ ] Correlation/request ID propagated through the flow and into downstream calls
- [ ] Business + technical metrics emitted (success/failure counts, latency)
- [ ] Health check / readiness reflects real dependency state
- [ ] Graceful shutdown drains in-flight work; no lost messages on deploy

### Database
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
- Object-level authorization on every fetch/update by ID
- No mass assignment; request models are allow-lists
- Tenant isolation verified on queries AND cache keys
- Secrets/PII not in logs or error responses
- SSRF / deserialization / injection surfaces checked
- Privilege-escalation paths (can a user grant themselves a role?)

### Migration-reviewer focus (only if migrations changed)
- Safe on populated tables (no blocking `ADD COLUMN NOT NULL` without default on large tables)
- Backward-compatible with the currently-deployed code (expand/contract)
- Rollback path exists and is tested
- No data loss on down-migration (or down-migration explicitly forbidden)

---

## Final hand-off — the testing guide (always end with this)

The Prompt Architect rule: never finish without telling the user how to know it worked. Produce a short guide covering:

1. **Golden path** — the one command/request that proves the feature works.
2. **The invariant tests** — one test per INV-* from Gate 3 (e.g., "fire two concurrent orders at the last unit → exactly one succeeds").
3. **The failure-mode tests** — retry the same idempotency key twice → one side effect; kill the dependency mid-flow → graceful degradation.
4. **What was deferred** — any Med/Low finding or `pending` risk you consciously did not address, so the user decides.
