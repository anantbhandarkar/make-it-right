<div align="center">

<img src="MIR-logo.png" alt="Make It Right" width="360"/>

<h3>AI makes it work.&nbsp;&nbsp;Make It Right.</h3>

<p><em>Constraint-first skills + reviewer agents that stop AI coding agents<br/>from shipping confident-but-wrong backend code.</em></p>

<p>
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/license-Apache_2.0-D4A017"></a>
  <img alt="skills" src="https://img.shields.io/badge/skills-29-555">
  <img alt="runtimes" src="https://img.shields.io/badge/runtimes-9-555">
  <img alt="tools" src="https://img.shields.io/badge/works_with-Claude%20Code%20%7C%20Cursor%20%7C%20Codex%20%7C%20Antigravity-D4A017">
  <a href="EXTENDING.md"><img alt="PRs welcome" src="https://img.shields.io/badge/PRs-welcome-2ea44f"></a>
</p>

</div>

---

AI is brilliant at making code that *works* — on the happy path, in the demo, in the passing test. It is weak at making code that's *right*: correct under concurrency, partial failure, retries, multi-tenancy, and real production data. Those aren't syntax failures — they're **assumption failures**. When requirements are incomplete, AI invents them, confidently.

**Make It Right** replaces "generate, then hope" with **"discover constraints → gate on confirmation → generate → review."**

## Why three tiers (and a family)

The discipline of *what makes code correct* is timeless. *Runtime* footguns (GIL, event loop, GC) are shared across every framework on that runtime. A *framework's* mechanics rot as the library evolves. So Make It Right separates all three:

- a **language-agnostic pillar** (`mir-backend`) — the thinking discipline (gates, invariants),
- a **runtime tier** (`mir-backend-python`) — what's true for every framework on that runtime,
- a **framework module** (`mir-backend-python-fastapi`) — the perishable, library-specific traps.

The agent loads exactly the tiers your stack needs (a FastAPI task loads all three; a Go task would load `mir-backend` + `mir-backend-go` + its framework, never the Python tiers). See [EXTENDING.md](EXTENDING.md).

This repo is the **backend pillar** of a planned family. Each domain is a sibling that reuses the same gate-and-reviewer pattern:

| Status | Skill | Tier | Covers |
|---|---|---|---|
| ✅ shipping | `mir-backend` | generic | Language-agnostic backend discipline (the gates) |
| ✅ shipping | `mir-backend-python` → `-fastapi` · `-django` · `-flask` | runtime + frameworks | CPython (GIL, async/sync, fork-safety, cold start) |
| ✅ shipping | `mir-backend-node` → `-express` · `-fastify` · `-nestjs` | runtime + frameworks | Node/V8 (event loop, worker threads, backpressure) |
| ✅ shipping | `mir-backend-jvm` → `-spring` · `-quarkus` · `-micronaut` | runtime + frameworks | JVM (thread pools, virtual threads, GC, cold start) |
| ✅ shipping | `mir-backend-dotnet` → `-aspnetcore` | runtime + framework | .NET CLR (async/await, captive deps, DbContext) |
| ✅ shipping | `mir-backend-go` → `-gin` · `-fiber` · `-echo` | runtime + frameworks | Go (goroutine leaks, context, data races) |
| ✅ shipping | `mir-backend-php` → `-laravel` · `-symfony` | runtime + frameworks | PHP/Zend (shared-nothing, FPM, Octane bleed) |
| ✅ shipping | `mir-backend-ruby` → `-rails` | runtime + framework | Ruby/YARV (GVL, Puma fork-safety, AR migrations) |
| ✅ shipping | `mir-backend-rust` → `-axum` · `-actix` | runtime + frameworks | Rust/tokio (blocking the runtime, guards across await) |
| ✅ shipping | `mir-backend-beam` → `-phoenix` | runtime + framework | BEAM (supervision, mailbox growth, GenServer bottleneck) |
| 🔜 planned | `mir-frontend` · `mir-database` · `mir-data` · `mir-cloud` | pillars | frontend, DB, data engineering, cloud |

## What's in this repo

```
skills/
  mir-backend/                  # generic: the thinking discipline (gates, interrogation)
    SKILL.md
    references/                 # constraint catalog, failure-mode catalog, checklists, runtime-map
  mir-backend-python/           # runtime tier: CPython concerns (GIL, async/sync, fork-safety, cold start)
    SKILL.md
  mir-backend-python-fastapi/   # framework module: FastAPI + Async SQLAlchemy 2.0 + Postgres + Alembic + Redis
    SKILL.md
    references/                 # fastapi gotchas (right/wrong code), alembic migration safety
agents/
  constraint-interrogator.md  # proposes the 2-4 highest-leverage questions (with defaults)
  reliability-reviewer.md     # idempotency, partial-failure, concurrency, observability
  security-reviewer.md        # IDOR/BOLA, mass assignment, tenant isolation, SSRF, leaks
  migration-reviewer.md       # safe migrations on populated tables (expand/contract)
```

## The pipeline (hard-gated)

```
Gate 0  Intent & Triage          restate real intent, classify risk surface
Gate 1  Constraint Interrogation  interrogator → ask user 2-4 Qs w/ [DEFAULT]   [USER GATE]
Gate 2  Assumption Ledger         write assumptions → user confirms             [USER GATE]
Gate 3  Invariants & Failure      invariants, state machine, failure modes
Gate 4  Risk Register             Risk | Severity | Likelihood | Mitigation
Gate 5  Design Review             tx boundaries, consistency, observability      [USER GATE]
──────────── code may now be written ────────────
Gate 6  Implementation            against the codegen checklist
Gate 7  Production-Readiness       reviewers in parallel → fix Critical/High
```

**No implementation code is written until Gate 5 is approved.** Three gates require explicit user input — the agent never self-approves them.

## Why sub-agents

A sub-agent can't ask you questions — it returns to the orchestrator. So:
- the **constraint-interrogator** does the noisy catalog sweep off the main context and hands back only the distilled questions (each with a recommended default + rationale); the orchestrator asks *you* via the question UI.
- the **reviewers** are read-only (no edit tools) — they *report* severity-tagged findings; the orchestrator triages and fixes. This keeps "decide what matters" under your control.

Mirrors the 2026 pattern: `Planner → Architect → Implementer → Reliability Reviewer → Security Reviewer`, replacing one-shot "prompt → giant code dump."

## Install

```bash
git clone <your-fork-url> make-it-right
cd make-it-right
./install.sh                      # default: Claude Code (~/.claude)
./install.sh --tool=cursor        # Cursor (reads ~/.claude resources)
./install.sh --tool=codex         # Codex CLI (AGENTS.md → ~/.codex)
./install.sh --tool=antigravity   # Antigravity (skills → ~/.gemini/antigravity, AGENTS.md → ~/.gemini)
./install.sh --tool=all           # everything
```

Symlinks, not copies — edits in the repo are live immediately. Override targets with `CLAUDE_HOME` / `CODEX_HOME` / `GEMINI_HOME`.

## Works across four agents

The ecosystem has converged on one model — *skills* (`SKILL.md` loaded on-demand by name+description), *sub-agents*, *hooks*, and the cross-tool **`AGENTS.md`** standard. Make It Right ships **one tool-agnostic core**; the skill bodies degrade gracefully where a tool lacks a feature.

| | Skills | Sub-agents | Question UI | How it loads |
|---|---|---|---|---|
| **Claude Code** | ✅ native | ✅ native | clickable (`AskUserQuestion`) | `~/.claude/{skills,agents}` |
| **Cursor** | ✅ | ✅ | plain text | reads `~/.claude/{skills,agents}` + `AGENTS.md` |
| **Codex CLI** | ✅ (`/skills`) | ✅ (config) | plain text | `AGENTS.md` baseline; skills via `/skills` |
| **Antigravity** | ✅ (`SKILL.md`) | personas / inline | plain text / plan | `~/.gemini/antigravity/skills` + `AGENTS.md` |

The two Claude-specific mechanisms — the clickable question tool and the sub-agent dispatch — are written as graceful fallbacks: on other tools the agent asks questions in plain text and runs the reviewer checklists inline. The *discipline* (gates, hard rule, catalogs) is identical everywhere. `AGENTS.md` alone gives a working baseline in all four.

## Use

```
/mir-backend Build an order checkout endpoint that charges a card and decrements inventory
```

The agent restates your intent, asks 2–4 sharp questions with recommended defaults, writes an Assumption Ledger for you to confirm, produces a risk register and design for sign-off, *then* implements, then runs the reviewers.

Flags: `--advisory` (soft gate, proceeds on defaults) · `--skip-interrogation` (skip the Q round, still requires ledger confirmation).

For FastAPI projects, `/mir-backend-python-fastapi` is pulled in automatically at the design/implementation gates.

## Composing with other planning frameworks

Make It Right is the **backend-specific planning layer**. If you use a broader planner (e.g. GSD or a master plan skill), run this *inside* a backend phase's planning — it produces the Assumption Ledger and Risk Register the phase plan should cite. It references those frameworks rather than duplicating them.

## Customizing

- Adjust the constraint question bank in `skills/mir-backend/references/constraint-catalog.md` for your domain's recurring invariants.
- Tune severity thresholds and checklist items in `skills/mir-backend/references/checklists.md`.
- Add a new framework module or pillar (`mir-backend-express`, `mir-frontend`, `mir-database`, …) by following **[EXTENDING.md](EXTENDING.md)** — it covers the lazy-loading model, the `TRIGGER`/`SKIP` description rule that keeps unrelated skills from loading, the pillar-prefixed naming convention, and copy-paste recipes.

## License

Licensed under the [Apache License 2.0](LICENSE) — © 2026 Anant Bhandarkar. Use it, fork it, ship it; contributions welcome.
