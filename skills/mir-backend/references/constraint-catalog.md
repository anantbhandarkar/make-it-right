# Constraint Catalog — the question bank

The constraint-interrogator sweeps these dimensions, then returns only the 2–4 questions whose answers most change the implementation. **This file is the source pool, not the question list** — never dump all of it at the user. Rank by leverage: a question is high-leverage if two reasonable answers would produce materially different code.

For every question the interrogator surfaces, it must attach 2–4 concrete options, mark one `[DEFAULT — Recommended]`, and give a one-line reason an expert would choose it.

---

## Dimension 1 — Domain

The questions LLMs skip because the answers live in someone's head, not the codebase.

- What business event is actually happening? (The verb, not the endpoint.)
- What are the **invariants** — things that must be true before AND after, no matter what? (one active subscription; balance ≥ 0; refund ≤ charge)
- Which actions are **irreversible**? (charge captured, email sent, inventory shipped) — these can't be inside a tx that might roll back.
- What **states** does the entity have, and which transitions are **invalid**?
- What is the source of truth when two systems disagree?
- Who is allowed to perform this action, and on whose data?

## Dimension 2 — Data

- Source of truth for this entity?
- Is **strong consistency** required, or is eventual consistency acceptable? (Be specific: "can the user see a stale balance for 2s?")
- What must be **unique**, and is uniqueness enforced at the DB level or just the app?
- Retention: how long does this data live? Is there a deletion requirement (GDPR/CCPA)?
- Audit: do we need an immutable record of who changed what, when?
- What's the read/write ratio? (Drives indexing and caching decisions.)
- Does this data already exist in production? (If yes → migration safety is mandatory.)

If the answers change the *schema* — new tables, new keys, a tenancy layout, a constraint, an index, a migration against populated tables — that is `mir-database`'s pipeline, and it runs before this one. Ask enough here to know whether that handoff is needed; don't design the schema inside a backend interrogation.

## Dimension 3 — Scale

- Expected steady-state QPS and **peak** QPS? (Peak, not average, breaks systems.)
- Multi-region? Single-region is a very different consistency story.
- Hot partitions / hot keys? (One celebrity user, one popular product.)
- How big does the largest table/collection get in 1 year?
- Is there a fan-out? (One write triggering N downstream writes.)

## Dimension 4 — Failure

The dimension AI most reliably ignores. For every external dependency:

- What happens if this dependency is **down**? Slow? Returns a 500? Times out after the DB commit but before the response?
- Retry policy: how many, with what backoff, with what **budget** (when do we stop)?
- Is the operation **idempotent**? If retried, does it double-charge / double-send / double-create?
- Timeout for each call? (No timeout = a hung thread under load.)
- Is compensation/rollback logic needed? (Saga: if step 3 fails, undo steps 1–2.)
- Partial success: Kafka ACK succeeds but consumer fails — what then?

## Dimension 5 — Security

Beyond JWT/OAuth (which AI over-focuses on):

- Tenant model: single-tenant, shared-DB-with-row-scoping, or DB-per-tenant? And *where* is the tenant filter enforced — database policy, a data-access layer, or hand review of each query?
- Authorization: is it enforced **server-side on every object access** (BOLA/IDOR), or just at the route? Do admin and internal routes check a role, or only a valid token?
- RBAC / ABAC? Who can escalate to whom?
- Is there PII? How is it classified, logged (or NOT logged), encrypted?
- Mass assignment: can a client set fields it shouldn't (`is_admin`, `account_balance`) via the request body? And does the response return more than the caller should see?
- SSRF: does this take a URL or fetch a remote resource on the user's behalf?
- Untrusted input reaching a native object deserializer, a shell command, or a server-side template?
- Does anything here rely on a client-side check (a hidden control, a form validator, a UI role gate) that the endpoint does not re-enforce?
- Secret handling: any chance secrets land in logs or error messages?
- Inbound webhooks: is the signature verified against the raw body before parsing?

The pillar's `## Security` section states the rules these questions test for; `checklists.md` has the checkable form.

## Dimension 6 — Operations

- SLO/SLA for this path? (Latency p99, availability.)
- Observability: correlation IDs, structured logs, metrics, traces — which are required?
- Deployment constraints: rolling, blue/green, zero-downtime? (Affects migration shape.)
- Rollback strategy if this deploy is bad?
- Who operates this at 2am, and what tooling do they need? (Admin actions, manual replay, kill switch.)

---

## Invariant patterns — the rules nobody writes down

AI breaks these because they're tribal knowledge. When you see the entity on the left, *suspect* the invariant on the right and ask:

| Entity / domain | Likely hidden invariant |
|---|---|
| Subscription / membership | At most one ACTIVE per user; no overlapping periods |
| Inventory / stock | `available >= 0` even after concurrent reserves and reservation expiry |
| Wallet / balance / credits | Never negative; sum of ledger entries == balance; no lost updates |
| Refund / chargeback | Total refunded ≤ original; tax reversed proportionally |
| Order / fulfillment | Immutable after terminal state; no transition out of REFUNDED |
| Booking / seat / slot | No double-booking of the same resource for overlapping time |
| Rate limit / quota | Counter resets atomically; no off-by-one at the boundary |
| Idempotent endpoint | Same idempotency key ⇒ same result, exactly one side effect |
| Multi-tenant row | Every query filtered by tenant_id; no cross-tenant read/write/cache |

If the user can't confirm an invariant, that's not permission to guess — it's a flag that the requirement is genuinely undefined. Surface it as a risk.
