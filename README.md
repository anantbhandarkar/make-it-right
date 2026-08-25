# Make It Right

Make It Right is a repository of engineering skills for AI coding agents. The skills install into Claude Code, Cursor, Codex, and Antigravity.

The repository addresses a specific failure mode: a model can write code that passes the happy-path test while violating a constraint that nobody stated. The skills make the agent discover those constraints, ask the user to confirm the important assumptions, obtain design approval, and only then write implementation code.

It is not a framework or a runtime. It is a flat collection of pillar, runtime, reactivity, engine, platform, and framework skills plus read-only reviewer agents.

## Contents

- [Quickstart](#quickstart)
- [Why use this](#why-use-this)
- [When to use it, and when not to](#when-to-use-it-and-when-not-to)
- [How it works](#how-it-works)
- [The eight gates](#the-eight-gates)
- [Pillars and their differences](#pillars-and-their-differences)
- [Skill selection and the three-tier chain](#skill-selection-and-the-three-tier-chain)
- [Progressive disclosure and token cost](#progressive-disclosure-and-token-cost)
- [Skill inventory](#skill-inventory)
- [Reviewer sub-agents](#reviewer-sub-agents)
- [Validation](#validation)
- [Tool support at a glance](#tool-support-at-a-glance)
- [Installation](#installation)
- [Project harness (`mir init`)](#project-harness-mir-init)
- [Extending the repository](#extending-the-repository)
- [Security posture](#security-posture)
- [Honest limits](#honest-limits)
- [License](#license)

## Quickstart

```bash
git clone https://github.com/anantbhandarkar/make-it-right.git ~/src/make-it-right
cd ~/src/make-it-right
./install.sh --tool=claude                  # or --tool=cursor|codex|antigravity|all
./bin/mir init /path/to/your/repo           # optional, Claude Code only: write policy + baseline
```

Restart the agent so it indexes the new resources, then describe a task in plain language — "add a checkout endpoint that charges a card and decrements inventory". The matching skills load from their descriptions; you do not have to name them. Typing `/mir-backend <task>` forces a specific one.

`install.sh` does not put `bin/mir` on `PATH`, so run it by path or alias it. What each tool actually receives differs, and the differences are not cosmetic — read [Tool support at a glance](#tool-support-at-a-glance) before assuming parity.

## Why use this

The thesis the skills state directly: a model does not fail at writing code, it fails at knowing which code to write. Pattern completion produces something locally correct that violates a rule nobody wrote down, and a passing happy-path test says nothing about that rule.

Consider an order endpoint that charges a card and decrements the last unit of inventory. Locally correct code can still do all of the following:

1. Charge the card.
2. Time out before the client receives the response.
3. Retry without an idempotency key and charge the card again.
4. Let two concurrent requests decrement the same last unit.
5. Send an email before the database transaction commits.

Each step can look reasonable in isolation. The combined behavior is wrong under a dropped response, a race, or a partial failure, and nothing in the test suite goes red.

The reason to adopt the kit is that these failures are enumerated in advance, written down as named classes, and attached to the gate that has to answer them. Every row below is a rule an active skill actually carries, not a category:

| Failure class | The concrete form the skills name | Where it is written |
|---|---|---|
| Retry without deduplication | A model adds retries but not dedup. Production is at-least-once, so the result is a duplicate charge, a double email, or a replayed webhook. Every retryable state-changing endpoint needs a key, a store for it, and a time to live. | `mir-backend` Gate 5, failure-mode catalog |
| Partial failure | "Redis is down but the database is up." The email sent but the transaction rolled back. The payment webhook arrived before the row existed. For each external call, enumerate the half-success states. | `mir-backend` Gate 3, failure-mode catalog |
| Concurrency on a lifecycle | Asked for a status field, a model generates CRUD; the domain is a state machine. Two concurrent transitions both succeed unless the update is conditional on the current state. | `mir-backend` Gate 3 |
| Authorization beyond authentication | A valid token says *who* is calling and nothing about whether that caller may touch *this* row. The ownership check belongs in the same query that loads the row, not in a branch after it. OWASP ranks broken object-level authorization first in its API Security Top 10. | `mir-backend` Security, `security-reviewer` |
| Mass assignment | Binding a request body onto a persisted object is how a user sets their own `role`, `is_admin`, `tenant_id`, `price`, or `verified`. The response side is the same bug reversed. | `mir-backend` Security |
| Tenant isolation | Enforced per query, not per login. Cache keys, rate-limit counters, search indexes, storage prefixes, background jobs, and exported files are queries too, and are where the missing filter usually is, because nobody reviews them. | `mir-backend` Security, `mir-database` |
| Migration safety on populated tables | Migrations get written as if the table is empty. During a rolling deploy the old code and the new code run at the same time against one schema, so every statement has to be correct for both. | `mir-database`, `migration-reviewer` |
| Async UI state | A slow search response arrives after a fast one and replaces fresh data with stale data. The debounce interval, the empty state, the error copy, and the cancellation semantics were never specified, so the model invented them. | `mir-frontend` Gates 0–1 |
| Client-side authorization | `if (user.isAdmin)` decides what is **drawn**, never what is **fetched**. A record that reached the client cache was in a network response and is readable in the browser. | `mir-frontend` Security |
| Hydration and shared client state | A module-scope cache, store, or client instance on a server-rendered path is shared by every request, which is a cross-user bleed risk rather than a caching detail. Server rendering also forces the hydration-mismatch and double-fetch-waterfall questions to be answered before code. | `mir-frontend` Gate 0 |
| Secrets in the browser bundle | `NEXT_PUBLIC_*`, `VITE_*`, `PUBLIC_*` and their siblings are inlined as string literals at build time, so rotating one needs a rebuild and every build already shipped still carries the old value. | `mir-frontend` Security |
| Process death on mobile | The operating system kills the app mid-upload. Delivering the file exactly once across that needs a checkpoint and an idempotency key that survives the process, plus a defined branch for a permanently denied permission. | `mir-mobile` Gate 0 |
| Provider chosen before the constraints | A provider gets named from familiarity and justified afterwards, before anyone checked data residency, a runtime-duration ceiling, egress volume, idle and cold-start cost, or the cost of leaving a managed service. | `mir-cloud` Gates 1 and 3 |
| Delivery-path trust | A pull request runs with release credentials, an action pinned to a tag follows that tag to a new commit, or a scanner reports findings without blocking the build. | `mir-devsecops` Gate 0 |
| Observability | Functionality ships before operability. A missing correlation ID, an absent business metric, and an unwritten alert condition are all invisible in a passing test. | `mir-backend` Gate 5 |

The mechanism is the same in every case. The pillar makes the agent state the invariants, state transitions, transaction boundaries, idempotency key and store, external-call behavior, and observability plan before implementation. The user confirms the assumptions and approves the design. Reviewers then read the actual diff for duplicate side effects, missing ownership checks, unsafe migrations, and missing telemetry.

**What it costs.** Turns, spent before any code exists. Gate 1 asks up to four questions. Gate 2 requires you to confirm a numbered ledger. Gate 5 requires you to approve a design. That is three explicit stops on a task a bare model would have answered in one reply, and Gate 7 then spends further turns reviewing the diff it produced. Context is the second cost: a matching task loads a pillar body, a tier body, and a module body plus the references they call for, and every installed skill's description is read at session start in every repository whether it is relevant or not — see [Install scope and token cost](#install-scope-and-token-cost). That trade pays when a wrong answer is expensive to discover in production. It does not pay otherwise, which is the next section.

## When to use it, and when not to

The skills encode this themselves. Each pillar's Gate 0 opens with a risk table, and every pillar instructs the agent to drop to `--advisory` and proceed lightly when zero boxes tick. Each description carries a `SKIP` clause naming the adjacent work it must not load for. The lists below are those rules restated for a human deciding before the agent runs.

### Use it when

- **The change writes persistent state.** Anything that can be retried, raced, or half-applied.
- **Money, inventory, credits, quotas, authentication, or authorization are involved.** These are where an invented default becomes a chargeback or a breach.
- **The path runs under concurrency or has retries.** Two requests on the last unit; a client that retries a timeout.
- **The work spans more than one table or more than one service.** Transaction boundaries and partial failure stop being implicit.
- **An external dependency is in the path** — payment, email, queue, third-party API. Every one of them has a half-success state.
- **The thing has a lifecycle.** States, valid transitions, and the transitions that must be rejected.
- **A migration will run against populated tables.** Lock class per statement, expand and contract, compatibility with the currently deployed code.
- **The system is multi-tenant, or stores PII or regulated data.** Isolation, retention, deletion, and audit are design decisions, not later work.
- **Frontend work carries async state** — data fetching, forms, server rendering and hydration, shared client state, auth-gated UI, or anything rendering untrusted content.
- **Native mobile work touches process death, runtime permissions, offline sync, background execution, or store submission.**
- **The provider or compute model is still open.** `mir-cloud` triggers only while that decision is unmade.
- **The change is to the delivery path** — a dependency bump, a third-party action, a secret, a published artifact, IaC, or a container base image.

### Do not use it when

This list is not shorter than the one above, and the skills are the reason.

- **It is a one-line fix.** A typo, a copy change, a constant, a log line, a rename the compiler verifies.
- **The task is read-only or pure compute.** A report query, a formatter, a parser. `mir-backend` Gate 0 says it plainly: do not bureaucratize a CSV parser.
- **The component is stateless and presentational.** `mir-frontend` Gate 0 sends that case to `--advisory` before Gate 1 runs.
- **The table is a scratch table or a ten-row static lookup.** `mir-database` Gate 0 says the same about schema work with no risk surface.
- **The deployment is hobby scale.** `mir-cloud` Gate 0 is explicit that a personal site does not get a procurement process.
- **The pipeline change ticks no boxes and involves no credential.** `mir-devsecops` Gate 0 drops to `--advisory` there.
- **The code is throwaway.** A one-off script against your own data, a fixture generator, a migration you will run once and delete.
- **You are exploring, not building.** Reading code to understand it, reproducing a bug, or running a spike whose output you intend to throw away. The gates want decisions; a spike exists to find out what the decisions are.
- **You already know the constraints and want speed.** The interrogation round is designed to extract what you have not said. If you have said it, the round asks questions whose answers you already gave, and the honest move is to state the constraints up front and use an opt-out.
- **The design is already approved elsewhere.** If a human architect has settled the transaction boundaries and the idempotency mechanism, running Gate 5 to re-approve them is ceremony.
- **The work belongs to a pillar you are not running.** A `SKIP` clause is a real instruction: schema decisions do not belong in a framework module, and pipeline security does not belong in a request handler. A task that spans two pillars gets two gate runs, not one merged pipeline.

### The explicit opt-outs

Two flags exist so that skipping the process is a stated choice rather than a silent bypass. Neither turns the skill off.

| Flag | What it lifts | What it still requires |
|---|---|---|
| `--advisory` | The one hard rule. Code, DDL, or infrastructure configuration may be written without a passed Gate 5, and Gate 2 may pass without an explicit confirmation. | The skills document it only as those two overrides. They do not describe it as suppressing Gate 3, Gate 4, or the Gate 7 review, and the pillars invoke it themselves as "proceed lightly" rather than "stop". |
| `--skip-interrogation` | The Gate 1 question round, including the `constraint-interrogator` sub-agent. | The Gate 2 Assumption Ledger is still written, from the defaults, and still has to be confirmed. Skipping the questions does not skip the record of what was assumed. |

Read the coverage precisely rather than assuming parity. All six gated pillars name `--advisory` as the override on their Gate 5 rule; the Gate 2 silence override is written in `mir-backend`, `mir-frontend`, `mir-mobile`, `mir-database`, and `mir-cloud`, but not in `mir-devsecops`. Those same five carry `--skip-interrogation` in their `argument-hint`; `mir-devsecops` documents only `--advisory`. `mir-init` takes neither, because it is not a gated pipeline.

## How it works

Two mechanisms ship in this repository, and they are not the same thing. The first is skill routing, which decides what instructions the agent reads. The second is the `mir init` harness, which decides what files the agent is permitted to write. They are independent, and conflating them is the easiest mistake a reader can make here.

### The routing mechanism, in the order a task flows through it

1. **The description is the router.** You describe the task in plain language; you do not name a skill. At index time the host has seen only each skill's `name` and `description`, and every description must state both a `TRIGGER` clause and a `SKIP` clause. That text is the routing logic, and `validate.py` makes both clauses mandatory rather than conventional. Typing `/mir-backend <task>` forces a specific skill when you want to override the match. See [Rule 1](#rule-1-the-description-is-the-router).

2. **The matching skills load coarse to fine.** A backend task loads the pillar (`mir-backend`), then the runtime tier (`mir-backend-python`), then the framework module (`mir-backend-python-fastapi`). The pillar owns what is true in any language, the tier owns what is true for every framework on that runtime, and the module owns one library's mechanics. They load *together*, never instead of one another. Hosts scan `skills/` one level deep, so the hierarchy lives in the name; `validate.py` treats a name whose parent chain does not exist as an error, downgraded to a warning only when the missing parent is listed in `.mir-planned`. See [Skill selection and the three-tier chain](#skill-selection-and-the-three-tier-chain).

3. **The loaded pillar runs eight gates.** Gates 0 through 5 establish what is true — intent and risk surface, the interrogation round, the confirmed ledger, invariants and failure modes, the risk register, the approved design. Gate 6 is the first place code may appear. Gate 7 reviews it. Three of the eight require explicit user input and cannot be self-approved. See [The eight gates](#the-eight-gates).

4. **Gate 7 dispatches read-only reviewers.** `reliability-reviewer` and `security-reviewer` run on the backend, frontend, mobile, database, and cloud pillars; `mir-devsecops` runs `security-reviewer` alone against its pipeline threat checklist. `migration-reviewer` runs only when migration files changed. `a11y-reviewer` runs for frontend and mobile, `frontend-perf-reviewer` for frontend. They report severity-tagged findings with file and line references and a proposed fix, and they do not edit code — the orchestrating agent triages and fixes, and the pillars tell it to read the flagged diffs rather than relay a reviewer's summary as fact. Where a host has no sub-agent facility, each skill instructs the agent to run the same checklist inline. See [Reviewer sub-agents](#reviewer-sub-agents).

Everything in that list is instruction. A model follows it; nothing in this repository forces it to.

### The harness mechanism, run once per repository

`mir init` is separate and does not participate in the gates at all. It runs once against a repository, detects and confirms the stack, resolves the matching skills, and generates a write policy plus the `PreToolUse` hook that enforces it, then runs a probe to check that the hook actually blocks the denied paths. See [Project harness (`mir init`)](#project-harness-mir-init).

The distinction that matters:

| | Skill routing | The `mir init` harness |
|---|---|---|
| What it controls | Which instructions enter the agent's context | Which paths the agent may write |
| How it takes effect | The model reads a skill body and follows it | A hook process returns a blocking exit code before the write happens |
| Installed by | `install.sh`, globally — but what each tool actually receives differs, so read [Tool support at a glance](#tool-support-at-a-glance) | `mir init`, per repository, Claude Code only |
| If it fails | The agent proceeds without the guidance | On its own errors the guard fails open, allows the call, and says so on stderr |

The guard has no notion of a gate, a ledger, or an approval, and the skills have no notion of the write policy. So the harness cannot stop an agent from writing code before Gate 5, and the gates cannot stop a write to a denied path. Each covers what it covers, and the rest of this document is careful about which one is doing the work in any given claim.

## The eight gates

The normal pipeline has eight gates, numbered 0 through 7. The names and artifacts vary by pillar, but the ordering and the approval points remain explicit.

| Gate | Name | What it produces | User input |
|---:|---|---|---|
| 0 | Intent and Triage | A restatement of the real outcome, risk categories, stack or platform fitness, and the pillars involved. | No |
| 1 | Constraint Interrogation | Two to four ranked, high-leverage questions. Each has concrete options, one `[DEFAULT — Recommended]`, and a reason. | **Yes.** The agent asks the questions; the interrogator only proposes them. |
| 2 | Assumption Ledger | A numbered record of every answer and accepted default. The user confirms or corrects it. | **Yes.** Silence does not approve the ledger in normal mode. |
| 3 | Invariants and Failure Modes | Rules that must always hold, valid and invalid state transitions, and the response to each partial failure. | No |
| 4 | Risk Register | `Risk | Severity | Likelihood | Mitigation`. A Critical or High risk cannot remain undecided. | No |
| 5 | Design Review | The approved design: boundaries, consistency, idempotency, observability, migration or release plan, and the decisions specific to the pillar. | **Yes.** The user approves or changes the design. |
| 6 | Implementation | Code, DDL, infrastructure configuration, or pipeline configuration written against the relevant checklist and confirmed scope. | No |
| 7 | Production-Readiness Review | Read-only findings, fixes for Critical and High findings, and a short testing guide covering the happy path, each invariant, and retry or failure cases. | No |

The one hard rule is simple: in normal mode, no implementation code is written until Gate 5 has passed. Gate 6 is the first gate where code may appear. This applies to application code, components, DDL, infrastructure code, Dockerfiles, and delivery configuration. `--advisory` is the explicit opt-out provided by the skills; it is not a silent bypass. `--skip-interrogation` skips the question round but still requires the Gate 2 ledger to be confirmed.

## Pillars and their differences

The six active pillars use the same eight-gate discipline but do not produce the same design artifacts.

| Pillar | Owns | Its design review emphasizes |
|---|---|---|
| `mir-backend` | State-changing application behavior in any backend language. | Transaction boundaries, consistency, idempotency, external calls, concurrency, ownership, tenancy, and observability. |
| `mir-frontend` | Browser UI, including mobile web and progressive web applications. | UX and interaction contracts, UI state machines, state ownership, rendering ownership, accessibility, performance budgets, and field telemetry. Its middle tier is a reactivity library, not a runtime. |
| `mir-database` | What is true of the data: entities, relationships, keys, constraints, tenancy, indexes, and migrations. | The database-versus-application enforcement boundary, nullability, cardinality, normalization, query-derived indexes, and expand/contract migration phases. |
| `mir-mobile` | Native applications shipped through the Apple App Store or Google Play. | Process-death recovery, storage and reinstall semantics, background execution, permission states, offline sync, persisted idempotency keys, release compatibility, and store requirements. |
| `mir-cloud` | Where a workload runs and what that choice costs. | Workload numbers, hard provider constraints, scored tradeoffs, egress, idle and cold-start cost, residency of every data sink, guardrails, and exit cost. |
| `mir-devsecops` | The path from commit to a running artifact. | Trust ledgers, untrusted-input paths, dependency and artifact controls, secret handling, enforcement location, and whether each control blocks or only warns. |

### The cloud decision table

`mir-cloud` is the newest pillar and its decision method is deliberately different from the other pillars. It does not name a provider from familiarity and justify the choice afterward.

At Gate 1 it records nine inputs: request shape, egress volume, latency target and user geography, state, execution duration, accelerator needs, compliance and residency, existing commitments, and team operating capacity. Gate 3 applies hard constraints first. Missing regions, authorization requirements, runtime-duration limits, unavailable accelerator families, missing managed-service equivalents, incompatible state models, and contractual commitments can eliminate a provider. Gate 4 scores only the survivors by workload class and records the risks. Gate 5 must include a dated cost model with egress on its own line, idle and cold-start behavior, the cost of leaving every managed service, residency for logs and backups as well as primary data, and spend guardrails.

The active cloud skill is provider-neutral. Provider modules are planned but not written, so the decision table cannot be replaced by a provider-specific recommendation that does not exist yet.

## Skill selection and the three-tier chain

The hosts scan `skills/` only one level deep. The directory name therefore encodes the hierarchy:

- Generic pillar: `mir-<pillar>`, such as `mir-backend`.
- Runtime or reactivity tier: `mir-<pillar>-<runtime>`, such as `mir-backend-python` or `mir-frontend-react`.
- Framework module: `mir-<pillar>-<runtime>-<framework>`, such as `mir-backend-python-fastapi`.
- Direct two-tier module: `mir-database-postgres` or `mir-mobile-ios`, where no shared middle tier exists.

The frontend middle tier names React, Vue, or plain DOM work because the shared rules are about reactivity. Database, mobile, cloud, and DevSecOps currently use two tiers: a generic pillar and direct engine or platform modules where those modules exist. The tool is a separate installer concern; it does not create tool-specific skill names.

### Rule 1: the description is the router

At index time, the agent sees a skill's `name` and `description`. That description is the routing logic. Every description must state both:

```text
TRIGGER <the stacks and tasks that should load this skill>.
SKIP    <the adjacent stacks and tasks that should not load it>.
```

For example, `mir-backend` triggers for state-changing backend work in any language and skips frontend, pure read-only work, and standalone schema or data-pipeline work. `mir-backend-python-fastapi` triggers only for FastAPI and skips Django, Flask, other runtimes, and unrelated framework mechanics.

The positive and negative clauses are not documentation decoration. They decide which bodies enter the context. `validate.py` makes both clauses mandatory.

### Worked selection example

Task: **add a FastAPI checkout endpoint that charges a card and decrements inventory; the schema is unchanged.**

| Disclosure level | What loads |
|---|---|
| Always on | `AGENTS.md`, the cross-tool baseline. |
| Index | The names and descriptions for all 45 active skills. This is an index, not 45 full skill bodies. |
| Matching skill bodies | Exactly `mir-backend`, `mir-backend-python`, and `mir-backend-python-fastapi`. The first owns the generic gates, the second owns CPython concurrency and process behavior, and the third owns FastAPI, Starlette, Async SQLAlchemy, Postgres, Alembic, and Redis mechanics. |
| Conditional references | The matching bodies direct the agent to `mir-backend/references/runtime-map.md` at Gate 0, the constraint catalog at Gate 1, the failure-mode catalog as needed at Gates 3–4, the generic checklists at Gates 6–7, and `mir-backend-python-fastapi/references/fastapi-gotchas.md` at implementation. The Alembic migration reference is not needed because this example does not change the schema. |
| Not loaded as bodies | The other 42 active skill bodies: other backend runtimes and frameworks, `mir-frontend` and its tiers, `mir-mobile`, `mir-database`, `mir-cloud`, and `mir-devsecops`. Their descriptions do not match this task or explicitly skip it. Planned skills also have no directory and no body to load. |

If the same change also adds a table or migration, `mir-database` must run for the schema decisions, followed by the appropriate engine module. That is a second pillar run, not a reason to put database rules into the FastAPI module.

## Progressive disclosure and token cost

Progressive disclosure is the main context constraint behind the flat naming scheme.

| Level | Context | When it is loaded |
|---:|---|---|
| 0 | `AGENTS.md` only. It carries the always-on persona, the hard rule, and the gate names. | Every request. |
| 1 | Every skill's `name` and `description`, usually tens of tokens per description. | Session index time. |
| 2 | One matching `SKILL.md` body in full. | Only when its description matches the task. The repository aims for roughly 1–3 thousand tokens per body; `validate.py` warns above 380 body lines. |
| 3 | A referenced file such as a constraint catalog, failure-mode catalog, checklist, or framework gotcha file. | Only when the loaded body explicitly says to read it. |

Adding a skill increases the index descriptions seen at Level 1. It does not make an unrelated skill body load. A FastAPI task therefore pays for three matching bodies, not all 45 bodies. References are split out because a question catalog, migration checklist, or framework example is useful only at the gate that needs it.

This is why `AGENTS.md` stays thin, why the directory is flat, why names encode parent tiers, and why Rule 1 requires precise `TRIGGER` and `SKIP` text.

## Skill inventory

There are **45 directories under `skills/`**. The tables below list every active skill. The validator currently classifies them as 6 pillars, 17 tiers, and 22 modules. Its classification uses hyphen count: a direct leaf under a two-tier pillar is counted as a tier in the validator summary even though the inventory calls it a direct engine or platform module.

### Backend — 31 skills

| Layer | Skill | Focus |
|---|---|---|
| Pillar | `mir-backend` | Language-independent gates for state, money, inventory, authentication, concurrency, retries, multi-table work, external dependencies, and observability. |
| Runtime tier | `mir-backend-beam` | Erlang VM and BEAM supervision, mailbox growth, GenServer bottlenecks, distributed Erlang, term handling, and Hex supply chain. |
| Framework module | `mir-backend-beam-phoenix` | Phoenix, LiveView, Ecto, and Plug: connection process memory, event authorization, changesets, PubSub, async assigns, and safe migrations. |
| Runtime tier | `mir-backend-bun` | Bun and JavaScriptCore differences from Node, Bun.serve defaults, native API gaps, one-process `bun:test`, install scripts, and backpressure. |
| Framework module | `mir-backend-bun-hono` | Hono request-body and context lifetime, validator behavior, adapter differences, streaming errors, edge-isolate state, and Hono advisories. |
| Runtime tier | `mir-backend-dotnet` | CLR and .NET runtime versions, async and thread-pool behavior, cancellation, dependency lifetimes, garbage collection, native compilation, and NuGet security. |
| Framework module | `mir-backend-dotnet-aspnetcore` | ASP.NET Core and Entity Framework Core middleware order, dependency injection, binding, overposting, queries, antiforgery, authorization, and migrations. |
| Runtime tier | `mir-backend-go` | Goroutine lifecycle, context cancellation, race testing, channels, panic recovery, HTTP timeouts, path containment, checksums, and vulnerability checks. |
| Framework module | `mir-backend-go-echo` | Echo context pooling, the v4/v5 rewrite, binding and validation, middleware order, graceful shutdown, real-IP handling, and security headers. |
| Framework module | `mir-backend-go-fiber` | Fiber context pooling, the v2/v3 rewrite, binding, immutable request data, `fasthttp` differences, and shutdown behavior. |
| Framework module | `mir-backend-go-gin` | Gin context copying, cancellation, binding and validation, recovery order, route-group authorization, trusted proxies, CORS, and uploads. |
| Runtime tier | `mir-backend-jvm` | Java and Kotlin runtime behavior: thread pools, virtual threads, garbage collection, heap sizing, cold start, visibility, `ThreadLocal`, deserialization, SSRF, and dependency verification. |
| Framework module | `mir-backend-jvm-micronaut` | Micronaut compile-time dependency injection, Netty blocking, AOP scope, Micronaut Data transactions, native image behavior, and security configuration. |
| Framework module | `mir-backend-jvm-quarkus` | Quarkus build-time configuration and injection, Vert.x blocking, Mutiny composition, native image registration, Panache, and HTTP authorization policy. |
| Framework module | `mir-backend-jvm-spring` | Spring Boot, Spring Data, Hibernate, and Spring Security transactions, proxy behavior, N+1 queries, bean scope, async tasks, DTOs, and authorization. |
| Runtime tier | `mir-backend-node` | Node and V8 event-loop blocking, worker parallelism, promise failures, bounded concurrency, streams, timeouts, heap limits, async context, shutdown, and npm supply chain. |
| Framework module | `mir-backend-node-express` | Express 5 and 4 behavior, route syntax, middleware order, validation, error-handler arity, CORS, proxy trust, sessions, uploads, and object authorization. |
| Framework module | `mir-backend-node-fastify` | Fastify schema validation and response serialization, Ajv behavior, v5 JSON Schema requirements, plugin scope, hook order, defaults, reply lifecycle, and validation bypasses. |
| Framework module | `mir-backend-node-nestjs` | NestJS dependency-injection scope, execution order, validation and serialization, adapter behavior, route syntax, TypeScript build changes, and durable queues. |
| Runtime tier | `mir-backend-php` | PHP-FPM shared-nothing behavior, worker counts, long-running runtimes, memory and execution limits, persistent connections, signals, and Composer security. |
| Framework module | `mir-backend-php-laravel` | Laravel, Eloquent, Redis, queues, Octane, transactions, after-commit behavior, mass assignment, N+1 queries, and AI tool authorization. |
| Framework module | `mir-backend-php-symfony` | Symfony, Doctrine, Messenger, API Platform, unit-of-work memory, serializer groups, DTO mapping, worker resets, and idempotent handlers. |
| Runtime tier | `mir-backend-python` | CPython GIL and free-threaded builds, async and sync boundaries, fork safety, worker processes, cold starts, task exceptions, archive and shell safety, and packaging. |
| Framework module | `mir-backend-python-django` | Django and Django REST Framework query loading, transactions, migrations, serializers, async ORM limits, tasks, signals, and security advisories. |
| Framework module | `mir-backend-python-fastapi` | FastAPI, Starlette, Async SQLAlchemy, Postgres, Alembic, and Redis session scope, validation, authorization, background work, transactions, locks, and advisories. |
| Framework module | `mir-backend-python-flask` | Flask and Werkzeug contexts, application factories, validation, SQLAlchemy sessions, Celery or RQ work, host and body limits, configuration, and migrations. |
| Runtime tier | `mir-backend-ruby` | YARV and the GVL, Ractors, Puma process and thread behavior, fork safety, copy-on-write memory, job retries, GC, and Bundler security. |
| Framework module | `mir-backend-ruby-rails` | Rails and Active Record loading, strong parameters, callback timing, jobs and transactions, connection pools, migrations, Active Storage, and security defaults. |
| Runtime tier | `mir-backend-rust` | Tokio blocking, mutex guards across `await`, cancellation safety, panic and shared state behavior, `spawn_blocking`, channel bounds, and timeouts. |
| Framework module | `mir-backend-rust-actix` | Actix-web worker-local state, `web::Data`, blocking work, body limits, route middleware order, and `ResponseError`. |
| Framework module | `mir-backend-rust-axum` | Axum 0.8 route syntax, extractor ordering, typed state, `FromRef`, response errors, body limits, and Tower ordering. |

### Frontend — 6 skills

| Layer | Skill | Focus |
|---|---|---|
| Pillar | `mir-frontend` | Browser UI gates for interaction contracts, async states, hydration, accessibility, performance, raw HTML, client authorization, public variables, and third-party scripts. |
| Reactivity tier | `mir-frontend-react` | React hooks, effects, stale closures, keys, Actions, Suspense, Error Boundaries, transitions, Compiler behavior, server state, and React security. |
| Framework module | `mir-frontend-react-next` | Next.js App Router server/client boundaries, Server Actions, proxy and middleware limits, opt-in caching, revalidation, rendering waterfalls, public variables, and image/font behavior. |
| Reactivity tier | `mir-frontend-vanilla` | Plain DOM and Web Components: listener and observer cleanup, timers, detached nodes, safe markup, layout work, state rendering, custom-element lifecycle, and focus. |
| Reactivity tier | `mir-frontend-vue` | Vue reactivity, `ref` and `reactive`, computed purity, watcher timing and cleanup, injection, keys, lifecycle, SSR state, and client-bundle security. |
| Framework module | `mir-frontend-vue-nuxt` | Nuxt and Nitro universal rendering, fetch deduplication, cross-request state, hydration, payload size, server routes, runtime configuration, route middleware, and plugin order. |

### Mobile — 3 skills

| Layer | Skill | Focus |
|---|---|---|
| Pillar | `mir-mobile` | Native app gates for process death, permissions, offline work, storage, background execution, network retries, deep links, privacy declarations, and store release rules. |
| Direct platform module | `mir-mobile-android` | Kotlin and Android lifecycle, Compose state, saved state, coroutines, WorkManager, foreground-service limits, permissions, Keystore, intents, WebView, and Play requirements. |
| Direct platform module | `mir-mobile-ios` | Swift 6, SwiftUI, concurrency, task lifetime, state identity, background tasks, Keychain, universal links, transport security, privacy manifests, and App Store requirements. |

### Database — 3 skills

| Layer | Skill | Focus |
|---|---|---|
| Pillar | `mir-database` | Engine-independent data semantics, relationships, keys, nullability, enforcement, tenancy, normalization, indexes, soft deletion, audit, and migration safety. |
| Direct engine module | `mir-database-mongo` | MongoDB document shape, the 16 MB limit, validators, write and read concerns, retryable writes, transactions, indexes, aggregation, shard keys, and operator injection. |
| Direct engine module | `mir-database-postgres` | PostgreSQL constraints, row-level security, lock behavior, `CONCURRENTLY`, partial and expression indexes, generated values, partitions, and migration operations. |

### Cloud — 1 skill

| Layer | Skill | Focus |
|---|---|---|
| Pillar | `mir-cloud` | Provider-neutral workload characterization, hard elimination rules, scored provider tradeoffs, egress, cold starts, commitments, residency, and exit cost. |

### DevSecOps — 1 skill

| Layer | Skill | Focus |
|---|---|---|
| Pillar | `mir-devsecops` | Dependency and lockfile integrity, install scripts, action pinning, provenance and attestations, secrets, pipeline trust, infrastructure state, container posture, IAM, egress, and incident response. |

### Planned but not written

`.mir-planned` contains six slugs that existing skill bodies reference or offer but that do not have a directory yet:

| Planned skill | Intended work |
|---|---|
| `mir-frontend-react-remix` | React Router or Remix framework mechanics. |
| `mir-frontend-angular` | Angular reactivity and framework mechanics. |
| `mir-cloud-aws` | AWS service mechanics after provider selection. |
| `mir-cloud-gcp` | Google Cloud service mechanics after provider selection. |
| `mir-cloud-azure` | Azure service mechanics after provider selection. |
| `mir-cloud-cloudflare` | Cloudflare service mechanics after provider selection. |

These names do not load anything. The validator downgrades a matching missing-reference or missing-parent error to a warning. The file is not a way to silence a typo; remove a slug when its skill is written, and `PLN001` warns if a planned slug is already present on disk.

## Reviewer sub-agents

All six files under `agents/` are read-only. They report severity-tagged findings with file and line references and a concrete fix. The orchestrating agent remains responsible for asking the user, changing code, and verifying the fix.

| Agent | Pipeline position | Role |
|---|---|---|
| `constraint-interrogator` | Gate 1 | Reads the task and relevant code, sweeps the constraint catalog, and proposes no more than four high-leverage user questions. It does not speak to the user or write code. |
| `reliability-reviewer` | Gate 7 | Checks idempotency, partial failure, concurrent state transitions, backpressure, timeouts, cache consistency, observability, and invariant enforcement. |
| `security-reviewer` | Gate 7 | Checks object-level authorization, tenant isolation, mass assignment, secret and PII leakage, SSRF, injection, unsafe deserialization, privilege escalation, insecure defaults, and the frontend security cases when relevant. DevSecOps also uses it for delivery-path security. |
| `migration-reviewer` | Gate 7, only when migrations changed | Checks locks on populated tables, expand/contract compatibility with old code, rollback safety, batched backfills, and constraint validation. |
| `a11y-reviewer` | Gate 7 for frontend and mobile work | Checks keyboard operation, focus, semantics, target size, contrast, reduced motion, dynamic announcements, and screen-reader behavior. It labels findings that automation can catch versus manual testing. |
| `frontend-perf-reviewer` | Gate 7 for frontend work | Checks interaction latency risks, largest-content timing, layout shift, fetch waterfalls, bundle size, images and fonts, Compiler interaction, and field performance telemetry. |

The cloud pillar also requires an inline cost review. The mobile pillar requires an inline store-submission check. Neither has a separate reviewer file in this repository.

## Validation

The repository once shipped a Gate 5 instruction that pointed at a skill that had never been written. Nothing caught it. When a host cannot resolve a skill name, it loads no content and usually reports no error. `validate.py` now makes a missing skill reference or broken parent chain an error, and `install.sh` refuses to install when validation returns an error.

Run it before installing or committing:

```bash
./validate.py            # human-readable errors, warnings, and summary
./validate.py --quiet    # problem lines for errors, plus the summary
./validate.py --json     # machine-readable stats and problem objects
```

Exit codes are:

| Code | Meaning |
|---:|---|
| 0 | No errors. Warnings are allowed. |
| 1 | At least one validation error. |
| 2 | `skills/` is not a directory, so the tree cannot be read. |

The script performs these checks:

| Code | Level | Check |
|---|---|---|
| `SK001` | Error | Every directory under `skills/` has a `SKILL.md`. |
| `FM001`, `FM002` | Error | `SKILL.md` starts with `---` and has a closing `---`. |
| `FM003` | Error | Frontmatter contains `name`, `description`, `trigger`, `argument-hint`, and `allowed-tools`. |
| `FM004`, `FM005` | Error | `name` matches the directory and `trigger` is exactly `/<directory-name>`. |
| `RTR001`, `RTR002` | Error | The description contains both `TRIGGER` and `SKIP`. |
| `RTR003`, `RTR004` | Warning | The description is shorter than 200 characters or longer than 2,400 characters. |
| `CHN001` | Error | Every parent prefix of a name such as `mir-a-b-c` exists. |
| `CHN002` | Warning | A missing parent is listed in `.mir-planned`. |
| `REF001` | Error | Every `mir-*` skill name found in a skill body resolves to a directory. |
| `REF002` | Warning | A missing `mir-*` reference is listed in `.mir-planned`. |
| `REF003` | Error | Every `references/<file>.md` named in a body exists in the skill's references or in another skill's references. |
| `AGT001` | Error | A recognized `subagent_type` has a matching `agents/<name>.md`; `general-purpose` is allowed. |
| `SEC001` | Error | Every skill body has a heading named Security, case-insensitively. |
| `CTX001` | Warning | A skill body is longer than 380 lines. It loads in full and competes with the task for context. |
| `REF004` | Warning | A reference Markdown file exists but its filename never appears in the owning skill body. |
| `PLN001` | Warning | A slug remains in `.mir-planned` after a directory with that name was written. |

The frontmatter parser implements only the small YAML subset used by this repository. It is not a general YAML validator. The summary reports skill, pillar, tier, module, reviewer, planned, error, and warning counts. `--quiet` suppresses warning problem lines; `--json` returns the same error status as the human mode and includes the structured stats and problems.

## Tool support at a glance

The four supported tools do not receive the same thing. The table states what `install.sh` and `mir init` actually deliver today, so the asymmetry is visible before you rely on it.

| Capability | Claude Code | Cursor | Codex CLI | Antigravity |
|---|---|---|---|---|
| Skill bodies linked by `install.sh` | Yes, into `~/.claude/skills` | Yes, the same `~/.claude/skills` links | No — register them yourself | Yes, into the Antigravity skills directory |
| Reviewer sub-agents linked | Yes, into `~/.claude/agents` | Yes, the same `~/.claude/agents` files | No — register them yourself | No files installed; checklists run inline |
| Always-on `AGENTS.md` | Not linked by `install.sh`; `mir init` generates a per-repo one | Not linked; copy this repository's `AGENTS.md` into a project root | Yes, `~/.codex/AGENTS.md` | Yes, `~/.gemini/AGENTS.md` |
| `mir init` harness generated | Yes | Not targeted, but Cursor reads the same `.claude/settings.json` | No | No |
| Enforced write policy | Yes — `PreToolUse` guard plus a probe that verifies it | Conditional — only if third-party configs are enabled | Not emitted by this repository | Not emitted by this repository |

Two rows deserve the sentence behind them. Cursor can load Claude Code's hooks from `.claude/settings.json` and honours exit code `2` as a block, but only when *Include third-party Plugins, Skills, and other configs* is enabled in Settings → Rules, Skills, Subagents. That setting is off unless you turn it on, so a generated harness enforces on Cursor conditionally and silently does nothing otherwise. Codex CLI and Antigravity both have a real `PreToolUse` mechanism — the tools support enforcement, `make-it-right` does not emit anything for either yet.

## Installation

`install.sh` creates symlinks, not copies. Edits in the repository are therefore visible to the installed tool after the tool reloads its resources. It runs `python3 validate.py --quiet` first. Any non-zero result stops the install. If `python3` is unavailable, it warns and proceeds without validation.

The script will not replace an existing non-symlink target. It prints `SKIP` and asks you to remove or move that target first. Override the default locations with `CLAUDE_HOME`, `CODEX_HOME`, and `GEMINI_HOME`.

### Claude Code

```bash
./install.sh                         # same as --tool=claude
./install.sh --tool=claude
```

Default targets are `~/.claude/skills/<skill-name>` and `~/.claude/agents/<agent-name>.md`.

Verify the links:

```bash
ls -l ~/.claude/skills/mir-backend
ls -l ~/.claude/agents/reliability-reviewer.md
```

**What you get.** Skill bodies: linked globally and loaded on demand by description. Reviewer sub-agents: linked, and the pillars dispatch them natively at Gate 7. Always-on `AGENTS.md`: `install.sh` links none — run `mir init` to generate a per-repository `AGENTS.md` plus a `CLAUDE.md` that imports it. Generated write-policy harness: yes, and Claude Code is the only tool that gets one. See [Project harness](#project-harness-mir-init).

### Cursor

```bash
./install.sh --tool=cursor
```

Cursor uses the Claude resource directories, so the script installs skills and reviewer agents under `~/.claude`. Cursor reads `AGENTS.md` from a project root; keep this repository's `AGENTS.md` in the project where its baseline should apply.

Verify the Claude links and the project file:

```bash
ls -l ~/.claude/skills/mir-backend ~/.claude/agents/security-reviewer.md
test -f AGENTS.md
```

**What you get.** Skill bodies: the same `~/.claude/skills` links as Claude Code. Reviewer sub-agents: the same `~/.claude/agents` files; where native sub-agent dispatch is unavailable, run each reviewer's checklist inline. Always-on `AGENTS.md`: only if you place this repository's `AGENTS.md` in the project root yourself. Generated write-policy harness: none. `mir init` emits nothing for Cursor.

**The write policy is conditional here.** Cursor can load hooks configured for Claude Code and supports exit code `2` to block an action, so a `.claude/settings.json` written by `mir init` can enforce in Cursor too. It only does so when *Include third-party Plugins, Skills, and other configs* is enabled in Settings → Rules, Skills, Subagents. Until you enable it, the harness is present but inert: the gates and the `AGENTS.md` baseline are instructions the model may follow or ignore, and nothing mechanically blocks a write to a denied path.

Treat this as unverified until you check it yourself. The claim comes from Cursor's documentation; this repository does not test against Cursor, and `mir init` neither detects the setting nor reports whether it is on. A harness that enforces only under a toggle you cannot see from here is not the same as one that enforces.

### Codex CLI

```bash
./install.sh --tool=codex
```

The script links `AGENTS.md` to `~/.codex/AGENTS.md`. It does not copy the skills or reviewer files into a Codex-native registry. Register those through Codex's `/skills` and custom-agent configuration as appropriate for the local installation.

Verify the baseline link:

```bash
ls -l ~/.codex/AGENTS.md
```

**What you get.** Skill bodies: none installed by the script; register them through Codex's `/skills` configuration. Reviewer sub-agents: none installed; register them through the custom-agent configuration, and run their checklists inline until you do. Always-on `AGENTS.md`: yes, the global `~/.codex/AGENTS.md` link. Generated write-policy harness: none.

**On enforcement.** Codex CLI has a real `PreToolUse` hook mechanism (`.codex/hooks.json`) and an OS-enforced sandbox, so a write policy is enforceable on this tool. `make-it-right` does not generate either yet. The tool supports it; `mir init` does not emit it. Until it does, nothing in this repository enforces the write policy on Codex.

### Antigravity

```bash
./install.sh --tool=antigravity
```

Default targets are `~/.gemini/config/skills/<skill-name>` and `~/.gemini/AGENTS.md`. When native sub-agent dispatch is unavailable, the baseline directs the tool to run reviewer checklists inline.

Releases up to and including `v1.0.0` linked into `~/.gemini/antigravity/skills` instead. Neither installed Antigravity product reads that path, so those installs loaded nothing. `antigravity/` is the legacy Antigravity 2.0 product directory; global user skills moved to `~/.gemini/config/skills`, which both the CLI and the IDE read. If you installed before this change, the old location holds dead links. Remove only the symlinks this repository created, and look under your own `GEMINI_HOME` if you set one:

```bash
find "${GEMINI_HOME:-$HOME/.gemini}/antigravity/skills" -maxdepth 1 -type l -name 'mir-*' -delete
```

`-type l` matters: it deletes symlinks and nothing else, so a real directory that happens to match `mir-*` is left alone. Re-run the install command above afterwards.

Verify both link types:

```bash
ls -l ~/.gemini/config/skills/mir-backend
ls -l ~/.gemini/AGENTS.md
```

**What you get.** Skill bodies: linked into the skills directory named above. Reviewer sub-agents: no `agents/` files are installed; the baseline directs the tool to run the reviewer checklists inline. Always-on `AGENTS.md`: yes, the `~/.gemini/AGENTS.md` link. Generated write-policy harness: none. `mir init` emits nothing for Antigravity, so the write policy this repository can generate does not reach this tool.

**On enforcement.** Antigravity has a real `PreToolUse` hook, and it fires on file-write tools, not only on `run_command` — verified against the shipped binaries rather than the documentation, whose examples all use `run_command`. So a write policy is enforceable here. `make-it-right` does not generate one yet. The tool supports it; `mir init` does not emit it.

One property matters if you write your own hook: Antigravity's harness fails open. A hook that exits non-zero, or carries a matcher that will not compile, is logged and skipped, and the write proceeds. Timeout behaviour was not tested and is assumed to take the same path. A guard for this tool has to be fail-closed inside itself, because the host will not close for it.

### All supported installations

```bash
./install.sh --tool=all
```

This runs the Claude, Codex, and Antigravity installers. Cursor uses the Claude resources and does not have a separate install pass.

After any installation, restart the agent so it indexes the changed resources. Run `./validate.py` independently when you want the warnings as well as the error status.

### Install scope and token cost

`--scope` controls how much of the tree lands in the global configuration. It combines with `--tool`.

```bash
./install.sh --scope=all       # default: link every skill globally
./install.sh --scope=pillars   # link only the depth-1 pillar skills
```

The reason to care is the Level 1 index described in [Progressive disclosure and token cost](#progressive-disclosure-and-token-cost). A host reads every installed skill's `name` and `description` at session start, in every repository, whether or not that skill is relevant. Skill bodies stay lazy; the index does not.

| Scope | What is linked globally | Index cost per session |
|---|---|---|
| `all` (default) | Every directory under `skills/`. | About 15 thousand tokens. |
| `pillars` | Only the depth-1 slugs — a name with exactly one hyphen, which is how the naming convention encodes a pillar. | About 2 thousand tokens. |

Both figures are approximations measured by summing the `name` and `description` characters of the linked tree. Measure your own install rather than quoting these if the number has to be exact.

### Keeping the install in sync

Installing is `ln -sfn`, which overwrites but never removes. Three things follow. A skill renamed or deleted in a later release leaves a symlink that resolves to nothing, so the user sees a skill name that loads no content — the failure this repository exists to prevent, reappearing one layer down at the install boundary, where `validate.py` cannot see it because it validates the repository rather than your `$HOME`. Reducing `--scope` used to be a no-op that reported success: `--scope=pillars` wrote 7 links, left the other 39 in place, and printed `linked 7 pillar(s) globally` while the global index was unchanged. And moving or re-cloning the repository orphaned every link with no command to repair it.

| Flag | Effect |
|---|---|
| `--prune` | Remove this checkout's stale links, then install. The upgrade path, and the one that makes `--scope=pillars` actually cut the global index. |
| `--prune-only` | Remove them and stop. The uninstall path. Skips `validate.py`, because the one command that removes a broken install must not be gated on the install not being broken. |
| `--dry-run` | Print every removal and link and change nothing on disk. Combines with either flag above. |

```bash
./install.sh --prune --dry-run                    # see what would go
./install.sh --tool=all --scope=pillars --prune   # upgrade, and actually shrink the index
./install.sh --tool=all --prune-only              # uninstall
```

**Removal is opt-in, always.** A plain `./install.sh` never deletes anything under your home directory. It counts what looks stale and prints a `WARN` pointing at `--prune --dry-run`.

A link is removed only when the installer can prove it owns it: the basename is `mir-*` for skills or `*.md` for agents, the entry is a symlink — a real file or directory is never touched — and the link target resolves inside this checkout. A `mir-*` link pointing at a different checkout belongs to another setup and prints `KEEP`. `CLAUDE_HOME`, `CODEX_HOME`, and `GEMINI_HOME` are honoured.

`--prune` also cleans the legacy `~/.gemini/antigravity/skills` path that installs before `v1.0.0` wrote to, and never installs there.

**The progressive model.** Install the pillar floor once, globally, and let each repository top itself up with only the tiers and modules it uses:

```bash
./install.sh --tool=claude --scope=pillars   # once: every repo gets the gates
./bin/mir init /path/to/repo --install       # per repo: that repo's tiers and modules
```

`mir init --install` symlinks the resolved tiers and modules into `<repo>/.claude/skills/` and removes `mir-*` links that the repository's current stack no longer resolves, so a stack change does not leave the old stack loading forever. Depth-1 slugs are skipped locally because they already come from the global floor. Claude Code merges `~/.claude/skills` with `<repo>/.claude/skills`, so the two combine.

A repository that never runs `mir init` still gets the pillar gates. That is the intended degradation: fewer skills, not none. `--install` is Claude Code only, like the rest of `mir init`.

## Project harness (`mir init`)

`install.sh` puts skills on the machine. `mir init` prepares one repository: it records the confirmed stack, writes a thin always-on baseline, and generates a write policy plus the hook that enforces it. It installs no software and writes no application code, because an agent that installs software is the exact thing a containment harness exists to stop.

`install.sh` never links `bin/mir` onto `PATH`, so there is no bare `mir` command until you make one. Run the CLI by path, or alias the shim:

```bash
python3 init/cli.py init .                 # from the make-it-right checkout
~/src/make-it-right/bin/mir init .         # the same CLI through the shim, by absolute path
alias mir='~/src/make-it-right/bin/mir'    # optional; then `mir init .` works
```

### What it does

Five steps, in order. Each exists because the step before it can be wrong.

1. **Detect.** Reads `package.json`, `go.mod`, `Cargo.toml`, `requirements.txt`, `pyproject.toml`, `Gemfile`, `composer.json`, `mix.exs`, and `*.xcodeproj`. Detection proposes with a stated reason and a confidence; it never decides silently. Two frameworks in one pillar is a conflict to resolve, not a guess to make.
2. **Confirm.** The picker's options are derived from the installed skill tree, so they cannot drift from what exists. A stack with no matching skill is an explicit choice recorded as a gap, never dropped. The interactive picker is presented by the `/mir-init` skill, which asks the questions through the agent; `init/cli.py` never prompts. What the CLI does instead is refuse. If any pillar collected more than one candidate, or a detected stack has no skill, it stops with exit `3` and writes nothing, prints that pillar's options, and hands you a paste-ready `--answers` stub. A guessed stack loads the wrong gates, which is worse than no gates, because it reads as verified.
3. **Resolve.** The confirmed answers map to a coarse-to-fine skill set — pillar, then tier, then module. `mir-devsecops` is always included, so security is not an opt-in.
4. **Generate.** Writes the files in the table below.
5. **Verify.** Runs `.mir/probe.py` against the generated guard. The probe derives its attacks from the manifest, so it tests your policy rather than a fixed list. It also reads `.claude/settings.json` and `settings.local.json` to confirm the hook is actually registered for every tool the guard covers — every other row invokes the guard directly, so a stale matcher would leave structured writes unguarded while all of them still blocked.

   Four exit codes, and the difference between the last two matters:

   | Code | Meaning |
   |---|---|
   | `0` | Clean. |
   | `1` | **Leak.** A denied path reached the target — because the guard allowed it, or because the hook is not registered for the tool that would write it. |
   | `2` | The probe could not run: no manifest, or no guard. Not a passing harness, an unchecked one. |
   | `3` | **Inconclusive.** A positive control was blocked, the guard returned an unexpected code, or the wiring could not be confirmed. |

   Exit `3` exists because a guard that blocks everything is not simply "too tight". When the positive control is blocked, every `BLOCK` row becomes uninformative — you can no longer tell "blocked because the policy denies it" from "blocked because the guard is broken". The positive control is the only discriminating row in the table, so a run that loses it proves nothing in either direction. Pass `--allow-false-blocks` if the over-tightening is deliberate; it never silences a leak. `mir init` passes the probe's code through rather than flattening every failure to `1`.

### Commands

```bash
python3 init/cli.py init .                                          # the full five steps
python3 init/cli.py init . --dry-run                                # print the plan, write nothing
python3 init/cli.py init . --answers answers.json --noninteractive  # scripted, no picker
python3 init/cli.py init . --install                                # also link this repo's tiers/modules
python3 init/cli.py detect .                                        # show what the repo looks like
python3 init/cli.py catalog                                         # print the derived picker as JSON
python3 .mir/probe.py --repo .                                      # re-check an existing harness
```

`--noninteractive` no longer means "fail on ambiguity" — ambiguity is a hard stop in both modes, because it is the user's to resolve either way and the only thing a flag could change is whether mir admits it resolved it for them. The flag now only forbids prompting. `--answers` is the way forward, and deliberately the only one: there is no `--accept-detection`, because a "just guess" flag re-creates the defect behind a name that makes it sound approved.

`--dry-run` writes nothing at all, including the probe step, and reports `REFUSED: …` for any destination the real run would decline.

### What it writes

| File | What it is |
|---|---|
| `AGENTS.md` | The thin baseline: the pillars that apply here, the one hard rule, and the recorded stack. Never skill content. Anything below the ownership marker is preserved on re-run. |
| `CLAUDE.md` | An `@AGENTS.md` import plus room for repository notes. Like `AGENTS.md`, anything below the ownership marker survives a re-run. Before `v1.0.1` it was rewritten whole despite that promise. |
| `.mir/manifest.json` | The write policy: allowed roots, denied paths, the recorded stack, and the resolved skills. |
| `.mir/guard.py` | The `PreToolUse` hook that enforces the manifest. It lives under `.mir/`, which is itself a denied path, so the agent cannot rewrite the guard to widen its own permissions. |
| `.mir/probe.py` | The manifest-derived verifier, so anyone can re-check with `python3 .mir/probe.py --repo .`. |
| `.claude/settings.json` | Registers the hook. Merged, not overwritten, so an existing hook set survives. |

Re-running is idempotent: files above the ownership marker regenerate and your edits below it stay. It also **reconciles** rather than merely detecting. If the hook's matcher or command has gone stale, it is rewritten to the current one instead of being left alone because the tag was present, and duplicate entries from older runs collapse to one. The marker is an address, not a flag — treating it as a flag is how a repository ends up with a registered hook that covers the wrong tools and a re-run that reports success while changing nothing.

**Destination safety.** `mir init` inspects every destination before writing any of them, and refuses the whole run — exit `3`, nothing written — if one is a symlink, including a dangling one and including a symlinked parent directory; is not a regular file; is a `settings.json` it cannot parse; or is an `AGENTS.md` or `CLAUDE.md` carrying no mir ownership marker. Refused regular files are copied to `<name>.mir-backup` first; symlinks and device nodes are never read. Generation is all-or-nothing because a partial harness is worse than none — it looks installed.

### It is Claude Code only

Every enforcement artifact in that table — `.claude/settings.json`, `.mir/guard.py`, `.mir/probe.py`, and the manifest they read — is consumed by Claude Code. `mir init` emits nothing for Cursor, Codex CLI, or Antigravity: no `.codex/hooks.json`, no Cursor rule file, no Antigravity equivalent. The generated `AGENTS.md` is the one artifact those tools can still read, and an `AGENTS.md` is a set of instructions, not an enforcement mechanism.

So running `mir init` in a repository you drive with one of the other three gives you a baseline and a manifest that nothing enforces. Cross-tool generation is planned; it is not shipped.

### Restart before you rely on the guard

Claude Code snapshots hooks at session start. The `.claude/settings.json` that `mir init` just wrote therefore does not protect the session that generated it. Restart Claude Code and approve the new settings first. `mir init` prints this at the end of every run for the same reason it is repeated here: a harness you believe is live and is not is worse than no harness.

### What the guard actually covers

The policy is deny-by-default outside the allowed roots, and a denied path wins even inside an allowed root — so `src/.env` is blocked though `src` is writable. `.git`, `.mir`, `.claude/settings.json`, `**/.env*`, and the SSH, cloud, kube, and agent-tool config directories under `$HOME` are denied by default. The policy protecting itself is asserted by the probe, not assumed.

A denied entry is matched one of two ways, and the difference is worth knowing. A literal entry is a path prefix from the repository root: it protects that path and everything beneath it, and nothing else. An entry containing glob metacharacters is matched as a pattern at any depth, which is what `**/.env*` needs in order to cover `src/.env`, `.env.development`, and `a/b/.env.local` rather than only the exact root file. Releases up to `v1.0.0` listed the three literals `.env`, `.env.local`, and `.env.production`, so secrets at any other depth or suffix were writable while this section claimed otherwise. `.envrc` and `.env.example` are also denied: direnv exports live credentials, and a template filled in place is a common way a real key reaches a repository.

Two limits to read literally. `.claude/settings.json` is denied, but `.claude/` as a whole is not — `.claude/skills/` has to stay writable for `mir init --install`, so `.claude/settings.local.json` is currently writable even though the host reads it. And a clean probe run proves the guard enforces the paths the probe tested; it is not evidence about the paths it did not. The report prints both lists for that reason.

Coverage of the *tools* is partial by construction, and the guard reports that rather than implying more:

| Tool | Coverage | Why |
|---|---|---|
| `Write`, `Edit`, `MultiEdit`, `NotebookEdit`, `Update` | Full | The write target is a structured path field the guard can read directly. |
| `Bash` | Partial | The target is inside a shell string. The guard matches explicit `>`, `>>`, `tee`, `dd of=`, `cp`, `mv`, and `install` forms. A shell can hide a write from a regex — `eval`, a script, a heredoc to a variable path — so a clean Bash result is not proof. |
| MCP tool writes, `apply_patch`, other specialized write tools | None | Not parsed at all. |

The guard also fails open on its own errors. A missing manifest or unparseable JSON allows the call and says so on stderr, because a policy that bricks the agent when the policy has a bug is worse than one that is honest about not being loaded.

A clean probe proves the guard enforces the paths the probe tested. It does not prove the untested paths are safe, which is why the probe prints its own blind spots beside its results. Read the two lists together.

## Extending the repository

Read [EXTENDING.md](EXTENDING.md) before adding a skill. It documents the placement test, the naming convention, the progressive-disclosure rules, and copy-and-edit recipes.

### Placement test

Ask these questions in order:

1. Is the rule about code running inside an application request or job? If not, place it in the sibling pillar that owns it: schema in `mir-database`, infrastructure choice in `mir-cloud`, delivery controls in `mir-devsecops`, browser UI in `mir-frontend`, or native apps in `mir-mobile`.
2. Is it true for every supported runtime? Put it in the generic pillar.
3. Is it true for every framework on one runtime or every framework using one reactivity model? Put it in the runtime or reactivity tier.
4. Does it affect only one library, engine, platform, or framework? Put it in a module below that tier.

Do not widen a higher tier because one framework has a problem. The rule belongs at the lowest tier where it remains true for every task that should receive it.

### Add a runtime or reactivity tier

1. Copy the nearest tier directory under `skills/`.
2. Change frontmatter `name` and `trigger` to the new pillar-prefixed slug.
3. Rewrite the description with exact `TRIGGER` and `SKIP` clauses. Name adjacent runtimes or libraries that must not load it.
4. Replace the body with rules shared by every framework on that runtime or reactivity model. Keep library-specific rules out.
5. Update a runtime or rendering map if the pillar has one.
6. Run `./validate.py`, then install with the relevant `--tool` option.

### Add a framework, engine, or platform module

1. Confirm that the parent pillar and middle tier exist. A name such as `mir-backend-node-express` cannot be written before `mir-backend-node`.
2. Copy the nearest module shape, then change the frontmatter name, trigger, and description.
3. Keep only mechanics specific to that library, engine, or platform. Put new reference files in that module's `references/` directory and mention each file in the body so it can load.
4. Keep the Security and edit-boundary sections. Any dispatched reviewer must exist in `agents/`.
5. Run `./validate.py` before `install.sh`.

### Add a pillar

1. Create `skills/mir-<pillar>/SKILL.md` from the generic pillar shape and rewrite the gates for that domain.
2. Define the domain's trigger and the adjacent pillars in its `SKIP` clause.
3. Add a middle tier only when the same rules apply to every framework below it. Otherwise use direct modules.
4. Reuse the shared reviewers or add a reviewer file for a genuinely new review concern. Every `subagent_type` used by a skill must resolve in `agents/`.
5. Keep `AGENTS.md` thin. Prefer a project-scoped baseline; do not concatenate every pillar's detailed rules into the always-on file.
6. Add deliberate future references to `.mir-planned` only when a written skill already names them. Delete the entry when the skill lands.
7. Run `./validate.py`, then `./install.sh`. The installer already globs `skills/*/` and `agents/*.md`; no installer edit is required for a normal new skill or reviewer.

## Security posture

Every active skill has a Security section. `validate.py` enforces that with `SEC001`; it does not treat security as a single backend-only concern.

The repository's security guidance covers the correct ownership boundary for each problem:

- Application security: object-level authorization, tenant isolation, mass assignment, injection, SSRF, unsafe deserialization, and secret or PII leakage.
- Browser security: raw HTML and unsafe Markdown, client-visible build variables, content security policy, Trusted Types, cross-site request forgery, clickjacking, and third-party scripts.
- Database security: row-level enforcement, role privileges, tenant context, injection, backups, PII copies, and erasure semantics.
- Mobile security: device storage, backups, deep links, WebViews, transport settings, biometric binding, and privacy declarations.
- Delivery and cloud security: dependency integrity, action pinning, OIDC trust, artifact verification, infrastructure state, default-open resources, IAM, metadata-service access, and spend controls.

Across the skills, the repository cites about **290** real `CVE`, `GHSA`, and `RUSTSEC` advisory identifiers. They were machine-verified against OSV, NVD, and the relevant repository-level GitHub advisory data. Advisory identifiers and version floors are dated content, not permanent guarantees; the currency pass described below is still required.

## Honest limits

- This does not make model output deterministic. It makes skill selection more controlled through names and `TRIGGER`/`SKIP` descriptions. The model can still misunderstand a confirmed requirement or write a defective implementation.
- Guidance ages. Framework versions, security advisories, cloud prices, store rules, and runtime behavior change. Run a currency pass before relying on a dated claim, especially in a Security section or a cloud cost model.
- Published evidence on structured guidance shows that strong models improve at least as much as weak models. This is a quality tool. It is not a way to make a cheap model equal an expensive one.
- The skills do not replace tests, operational telemetry, human design approval, a migration rehearsal, a threat review, or provider pricing verification.

## License

Licensed under the [Apache License 2.0](LICENSE).
