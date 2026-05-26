# Extending Make It Right

How the family stays **context-cheap** as it grows to many frameworks and pillars. Read this before adding a skill.

## How loading works (why this is already lazy)

Every host (Claude Code, Cursor, Codex, Antigravity) uses **progressive disclosure**:

| Tier | What's in context | Size | Loaded |
|---|---|---|---|
| 0 — always-on | `AGENTS.md` only | tiny | every request |
| 1 — index | every skill's `name` + `description` | ~tens of tokens each | session start |
| 2 — skill body | one `SKILL.md`, in full | ~1–3k tokens | **only when its description matches the task** |
| 3 — references | `references/*.md` | as big as needed | **only when the SKILL.md says "read X"** |

So a session that's "build a FastAPI endpoint" loads `mir-backend` + `mir-backend-python-fastapi` and **nothing else** — not `mir-frontend`, not `mir-database`, not the React or Postgres modules. Their *descriptions* don't match, so their *bodies* never load. The whole design rests on two rules below.

## Rule 1 — the description is the router

The agent only sees `name` + `description` at Tier 1. That text **is** the routing logic. Every skill's description MUST contain explicit positive and negative triggers:

```
… <what it does> …
TRIGGER <the exact stacks/tasks that should load this>.
SKIP    <the adjacent things that should NOT load it — name them>.
```

- Generic pillar (`mir-backend`): `TRIGGER for backend in any language … SKIP for frontend/UI, read-only/compute, standalone DB or data work.`
- Framework module (`mir-backend-python-fastapi`): `TRIGGER only when the stack is FastAPI … SKIP for Node/Express, Django, Go … each gets its own module.`

If two reasonable stacks would load the same module, the SKIP line is too weak — tighten it.

## Rule 2 — AGENTS.md stays thin and pillar-scoped

`AGENTS.md` is the only always-on cost. It must hold **persona + the one hard rule + gate names** and *defer all depth to the skills*. Never paste framework specifics or full catalogs into it (Codex caps it at 32 KiB; Cursor bills its tokens on every request). Adding pillars must **not** grow the always-on footprint — see "Adding a pillar".

## The content matrix

The **backend pillar has three tiers** (generic → runtime → framework), because runtime concerns (GIL, event loop, GC) are shared across every framework on that runtime. Other pillars may be two-tier until a runtime layer earns its place. **Tool is an orthogonal axis handled by the installer, never by content.**

```
pillar      generic            runtime tier              framework module
backend     mir-backend        mir-backend-python        mir-backend-python-fastapi, -django, -flask
                               mir-backend-node          mir-backend-node-express, -fastify, -nestjs
                               mir-backend-jvm           mir-backend-jvm-spring …
                               mir-backend-go / -rust / -ruby / -php / -dotnet / -beam …
frontend    mir-frontend       (runtime usually n/a)     mir-frontend-react, -vue, -angular
database    mir-database                                  mir-database-postgres, -mongo …
data        mir-data                                      mir-data-spark, -kafka …
cloud       mir-cloud                                     mir-cloud-aws, -gcp, -azure …
```

Runtime slugs (see `skills/mir-backend/references/runtime-map.md`): `node · jvm · dotnet · python · php · go · ruby · rust · beam`.

## Naming convention (pillar-prefixed, runtime-segmented)

- **Generic pillar:** `mir-<pillar>` — e.g. `mir-backend`.
- **Runtime tier:** `mir-<pillar>-<runtime>` — e.g. `mir-backend-python`, `mir-backend-node`.
- **Framework module:** `mir-<pillar>-<runtime>-<framework>` — e.g. `mir-backend-python-fastapi`, `mir-backend-node-express`.

Why prefixed/segmented: hosts scan `skills/` **only one level deep**, so you cannot nest folders — the *name* is the only grouping mechanism. Segmenting by runtime makes everything sort hierarchically in the flat list (`mir-backend`, `mir-backend-python`, `mir-backend-python-fastapi`, `mir-backend-python-django`, `mir-backend-node`, …) and self-documents the chain. The longer trigger is rarely typed — skills auto-activate by description.

## Repo layout

```
AGENTS.md                       # always-on baseline (thin, pillar-scoped)
EXTENDING.md                    # this file
install.sh                      # --tool=claude|cursor|codex|antigravity|all  (auto-discovers skills/agents)
skills/
  mir-backend/                       # generic pillar (gates) + references/ (incl. runtime-map.md)
  mir-backend-python/                # runtime tier — CPython concerns, all Python frameworks
  mir-backend-python-fastapi/        # framework module (chains: backend → python → fastapi)
    SKILL.md  references/
agents/                              # shared reviewers, reused by every tier/pillar
  constraint-interrogator.md  reliability-reviewer.md  security-reviewer.md  migration-reviewer.md
```

`install.sh` globs `skills/*/` and `agents/*.md`, so **new skills/agents are picked up automatically** — no installer edits needed.

## Recipe: add a runtime tier (e.g. `mir-backend-node`)

1. `cp -r skills/mir-backend-python skills/mir-backend-node`.
2. Frontmatter: `name: mir-backend-node`, `trigger: /mir-backend-node`.
3. Rewrite the **description** with TRIGGER/SKIP for the Node/V8 runtime (TRIGGER: any Node backend; SKIP: other runtimes + library specifics).
4. Replace the body with that runtime's shared footguns from `runtime-map.md` (Node: event-loop blocking, single-thread CPU, unhandled rejection, stream backpressure…).
5. `./install.sh`. Done.

## Recipe: add a framework module (e.g. `mir-backend-node-express`)

1. `cp -r skills/mir-backend-python-fastapi skills/mir-backend-node-express`.
2. Frontmatter: `name: mir-backend-node-express`, `trigger: /mir-backend-node-express`.
3. Rewrite the **description** with TRIGGER/SKIP for Express specifically; note it loads with `mir-backend` + `mir-backend-node`.
4. Replace `references/` with the library's footguns (middleware order, error-handling middleware, `async` route error propagation…); keep the "Edit boundary" section.
5. `./install.sh`. Done — no other file changes.

## Recipe: add a pillar (e.g. `mir-frontend`)

1. `cp -r skills/mir-backend skills/mir-frontend`; rewrite the gates/references for frontend reliability (state, rendering, accessibility, data-fetching, hydration…).
2. Description: `TRIGGER for UI/component work … SKIP for backend/DB/data.`
3. Add framework modules under it (`mir-frontend-react`, …) via the recipe above.
4. **AGENTS.md decision (keep always-on thin):** prefer **per-project** `AGENTS.md` — a backend repo gets the backend baseline, a frontend repo the frontend one — so a session never carries another pillar's always-on text. Only if you want one global baseline, keep `AGENTS.md` pillar-agnostic (persona + the hard rule + "the matching `mir-*` skill loads on demand") and let each pillar skill carry its own depth. Do **not** concatenate every pillar into one global `AGENTS.md`.
5. Reuse the same `agents/` reviewers, or add pillar-specific ones (e.g. an `a11y-reviewer` for frontend).

## Worked example — what loads for "add a FastAPI checkout endpoint" on Cursor

- Tier 0: `AGENTS.md` (always).
- Tier 1: all `mir-*` descriptions visible (~a few hundred tokens total).
- Tier 2: three skills load — `mir-backend` (matches "backend, state-changing, money"), `mir-backend-python` (Python runtime), and `mir-backend-python-fastapi` (FastAPI endpoint). `mir-backend-node`, `mir-frontend*`, `mir-database*` do **not** — their SKIP lines exclude this task.
- Tier 3: at Gate 1, `mir-backend/references/constraint-catalog.md` loads; at Gate 6, `mir-backend-python-fastapi/references/fastapi-gotchas.md`; the failure-mode catalog only if Gate 3 needs it.
- Tool axis: because you installed `--tool=cursor`, the same files are read from `~/.claude`; the question UI degrades to plain text. No content branched.
