# Make It Right

Make It Right is a repository of engineering skills for AI coding agents. The skills install into Claude Code, Cursor, Codex, and Antigravity.

The repository addresses a specific failure mode: a model can write code that passes the happy-path test while violating a constraint that nobody stated. The skills make the agent discover those constraints, ask the user to confirm the important assumptions, obtain design approval, and only then write implementation code.

It is not a framework or a runtime. It is a flat collection of pillar, runtime, reactivity, engine, platform, and framework skills plus read-only reviewer agents.

## Contents

- [What this solves](#what-this-solves)
- [The eight gates](#the-eight-gates)
- [Pillars and their differences](#pillars-and-their-differences)
- [Skill selection and the three-tier chain](#skill-selection-and-the-three-tier-chain)
- [Progressive disclosure and token cost](#progressive-disclosure-and-token-cost)
- [Skill inventory](#skill-inventory)
- [Reviewer sub-agents](#reviewer-sub-agents)
- [Validation](#validation)
- [Installation](#installation)
- [Extending the repository](#extending-the-repository)
- [Security posture](#security-posture)
- [Honest limits](#honest-limits)
- [License](#license)

## What this solves

Consider an order endpoint that charges a card and decrements the last unit of inventory. Locally correct code can still do all of the following:

1. Charge the card.
2. Time out before the client receives the response.
3. Retry without an idempotency key and charge the card again.
4. Let two concurrent requests decrement the same last unit.
5. Send an email before the database transaction commits.

Each step can look reasonable in isolation. The combined behavior is wrong under a dropped response, a race, or a partial failure.

The backend pillar makes the agent define the relevant invariants, state transitions, transaction boundaries, idempotency key and store, time to live, external-call behavior, and observability plan before implementation. The user confirms the assumptions and approves the design. Reviewers then check the actual diff for duplicate side effects, missing ownership checks, unsafe migrations, and missing telemetry.

The same method is adapted to other work:

| Work | Failure the matching pillar is designed to catch |
|---|---|
| Browser UI | A search result arrives out of order, a stale response replaces fresh data, or a keyboard user cannot operate the control. |
| Database schema | A nullable column hides an undefined domain rule, or a migration takes an exclusive lock on a populated table. |
| Native mobile app | The operating system kills the process, a permission is permanently denied, or a retried request creates a duplicate write. |
| Cloud choice | A provider is selected before checking residency, execution limits, egress, idle cost, or the cost of leaving a managed service. |
| Delivery pipeline | A pull request runs with release credentials, an action tag moves to a malicious commit, or a scanner reports findings without blocking the build. |

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

### Codex CLI

```bash
./install.sh --tool=codex
```

The script links `AGENTS.md` to `~/.codex/AGENTS.md`. It does not copy the skills or reviewer files into a Codex-native registry. Register those through Codex's `/skills` and custom-agent configuration as appropriate for the local installation.

Verify the baseline link:

```bash
ls -l ~/.codex/AGENTS.md
```

### Antigravity

```bash
./install.sh --tool=antigravity
```

Default targets are `~/.gemini/antigravity/skills/<skill-name>` and `~/.gemini/AGENTS.md`. When native sub-agent dispatch is unavailable, the baseline directs the tool to run reviewer checklists inline.

Verify both link types:

```bash
ls -l ~/.gemini/antigravity/skills/mir-backend
ls -l ~/.gemini/AGENTS.md
```

### All supported installations

```bash
./install.sh --tool=all
```

This runs the Claude, Codex, and Antigravity installers. Cursor uses the Claude resources and does not have a separate install pass.

After any installation, restart the agent so it indexes the changed resources. Run `./validate.py` independently when you want the warnings as well as the error status.

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
