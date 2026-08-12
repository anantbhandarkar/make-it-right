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

So a session that's "build a FastAPI endpoint" loads `mir-backend` + `mir-backend-python` + `mir-backend-python-fastapi` and **nothing else** — not `mir-frontend`, not `mir-mobile`, not `mir-database`, not the React or Postgres modules. Their *descriptions* don't match, so their *bodies* never load. Going from 31 skills to 45 cost that session nothing but the extra Tier 1 descriptions. The whole design rests on two rules below.

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

The **backend pillar has three tiers** (generic → runtime → framework), because runtime concerns (GIL, event loop, GC) are shared across every framework on that runtime. The **frontend pillar** is also three-tier, but its middle tier is the **reactivity library**, not a runtime. Other pillars are two-tier until a middle layer earns its place. **Tool is an orthogonal axis handled by the installer, never by content.**

What exists today — 45 skills: 6 pillars, 13 middle tiers, 26 leaf modules.

```
pillar      generic          middle tier                     leaf module
backend     mir-backend      mir-backend-python              -fastapi · -django · -flask
                             mir-backend-node                -express · -fastify · -nestjs
                             mir-backend-bun                 -hono
                             mir-backend-jvm                 -spring · -quarkus · -micronaut
                             mir-backend-dotnet              -aspnetcore
                             mir-backend-go                  -gin · -fiber · -echo
                             mir-backend-php                 -laravel · -symfony
                             mir-backend-ruby                -rails
                             mir-backend-rust                -axum · -actix
                             mir-backend-beam                -phoenix
frontend    mir-frontend     mir-frontend-react              -next
                             mir-frontend-vue                -nuxt
                             mir-frontend-vanilla            (none — there is no framework layer)
mobile      mir-mobile       (two-tier)                      mir-mobile-android · mir-mobile-ios
database    mir-database     (two-tier)                      mir-database-postgres · mir-database-mongo
cloud       mir-cloud        (two-tier)                      none yet
devsecops   mir-devsecops    (two-tier)                      none yet
```

Still planned — no directory yet, so nothing loads for them:

```
frontend    mir-frontend-react-remix · -tanstack-start · -spa · mir-frontend-angular
cloud       mir-cloud-aws · -gcp · -azure
data        mir-data → mir-data-spark · -kafka
```

Of those, only the slugs an existing `SKILL.md` already names belong in `.mir-planned` — see "Validating the tree" below.

Runtime slugs (see `skills/mir-backend/references/runtime-map.md`): `node · jvm · dotnet · python · php · go · ruby · rust · beam · bun`.

## Naming convention (pillar-prefixed, runtime-segmented)

- **Generic pillar:** `mir-<pillar>` — e.g. `mir-backend`.
- **Runtime tier:** `mir-<pillar>-<runtime>` — e.g. `mir-backend-python`, `mir-backend-node`. On the frontend the middle segment is the reactivity library instead: `mir-frontend-react`.
- **Framework module:** `mir-<pillar>-<runtime>-<framework>` — e.g. `mir-backend-python-fastapi`, `mir-backend-node-express`.
- **Two-tier pillar:** the leaf sits directly under the pillar — `mir-database-postgres`, `mir-mobile-ios`. `validate.py` reads the chain from the name, so `mir-database-postgres` requires `mir-database` to exist and nothing else.

Why prefixed/segmented: hosts scan `skills/` **only one level deep**, so you cannot nest folders — the *name* is the only grouping mechanism. Segmenting by runtime makes everything sort hierarchically in the flat list (`mir-backend`, `mir-backend-python`, `mir-backend-python-fastapi`, `mir-backend-python-django`, `mir-backend-node`, …) and self-documents the chain. The longer trigger is rarely typed — skills auto-activate by description.

## Repo layout

```
AGENTS.md                       # always-on baseline (thin, pillar-scoped)
EXTENDING.md                    # this file
install.sh                      # --tool=claude|cursor|codex|antigravity|all  (auto-discovers skills/agents)
validate.py                     # checks the tree; install.sh runs it first and refuses on errors
.mir-planned                    # slugs a SKILL.md names but nobody has written yet
skills/
  mir-backend/                       # generic pillar (gates) + references/ (incl. runtime-map.md)
  mir-backend-python/                # runtime tier — CPython concerns, all Python frameworks
  mir-backend-python-fastapi/        # framework module (chains: backend → python → fastapi)
    SKILL.md  references/
  mir-frontend/ mir-mobile/ mir-database/ mir-cloud/ mir-devsecops/   # the other five pillars
agents/                              # shared reviewers, reused by every tier/pillar
  constraint-interrogator.md  reliability-reviewer.md  security-reviewer.md  migration-reviewer.md
  a11y-reviewer.md  frontend-perf-reviewer.md
```

`install.sh` globs `skills/*/` and `agents/*.md`, so **new skills/agents are picked up automatically** — no installer edits needed.

## Validating the tree (`validate.py`)

A Gate 5 instruction in this repo told the model to load a skill that was never written. It shipped that way and nothing caught it: when a name does not resolve, the host loads nothing, says nothing, and the agent continues without the content. Every rule on this page was a convention with no check behind it. `validate.py` is that check.

```bash
./validate.py            # errors, then warnings, then a one-line summary
./validate.py --quiet    # errors only
./validate.py --json     # {"stats": {...}, "problems": [...]}
```

Exit codes: **0** clean (warnings allowed) · **1** errors found · **2** the tree is unreadable (no `skills/`).

`install.sh` runs `./validate.py --quiet` first and refuses to install on any non-zero exit. An install that ships a broken chain is the failure this check exists to prevent.

| Code | Level | What it catches |
|---|---|---|
| `SK001` | error | a directory in `skills/` with no `SKILL.md` |
| `FM001` `FM002` | error | frontmatter missing, or never closed with `---` |
| `FM003` | error | missing a required key: `name` · `description` · `trigger` · `argument-hint` · `allowed-tools` |
| `FM004` `FM005` | error | `name` ≠ its directory, or `trigger` ≠ `/<name>` |
| `RTR001` `RTR002` | error | description with no `TRIGGER` clause / no `SKIP` clause — Rule 1, now enforced |
| `RTR003` `RTR004` | warn | description much shorter or longer than its siblings |
| `CHN001` | error | `mir-a-b-c` whose parent tier `mir-a-b` does not exist — the chain never loads |
| `CHN002` | warn | same, but the parent is in `.mir-planned` |
| `REF001` | error | the body names a `mir-*` skill that does not exist |
| `REF002` | warn | same, but the name is in `.mir-planned` |
| `REF003` | error | the body tells the model to read a `references/*.md` that is not there |
| `REF004` | warn | a `references/*.md` that no `SKILL.md` reads — it will never load |
| `AGT001` | error | dispatches a `subagent_type` with no matching file in `agents/` |
| `SEC001` | error | the skill has no Security section |
| `CTX001` | warn | body over 380 lines — it loads whole and competes with the task for context |
| `PLN001` | warn | a `.mir-planned` slug that now exists — delete the line |

**`.mir-planned`** holds slugs that some `SKILL.md` already points at but nobody has written yet: one per line, `#` comments allowed. Listing a slug downgrades `REF001` → `REF002` and `CHN001` → `CHN002`, so the roadmap stays visible instead of reading as clean. Delete the line the moment the skill lands. Do not use the file to silence a typo.

The summary line classifies by hyphen count, so a two-tier pillar's leaf (`mir-database-postgres`, `mir-mobile-ios`) is counted there as a tier, not a module.

## Recipe: add a runtime tier (e.g. `mir-backend-node`)

1. `cp -r skills/mir-backend-python skills/mir-backend-node`.
2. Frontmatter: `name: mir-backend-node`, `trigger: /mir-backend-node`.
3. Rewrite the **description** with TRIGGER/SKIP for the Node/V8 runtime (TRIGGER: any Node backend; SKIP: other runtimes + library specifics).
4. Replace the body with that runtime's shared footguns from `runtime-map.md` (Node: event-loop blocking, single-thread CPU, unhandled rejection, stream backpressure…). Add the slug to `runtime-map.md` too.
5. `./validate.py`, then `./install.sh`. Done.

## Recipe: add a framework module (e.g. `mir-backend-node-express`)

1. `cp -r skills/mir-backend-python-fastapi skills/mir-backend-node-express`.
2. Frontmatter: `name: mir-backend-node-express`, `trigger: /mir-backend-node-express`.
3. Rewrite the **description** with TRIGGER/SKIP for Express specifically; note it loads with `mir-backend` + `mir-backend-node`.
4. Replace `references/` with the library's footguns (middleware order, error-handling middleware, `async` route error propagation…); keep the "Edit boundary" and "Security" sections.
5. `./validate.py`, then `./install.sh`. Done — no other file changes. If the tier in step 3 does not exist yet, `validate.py` fails with `CHN001`; write the tier first.

## Recipe: add a pillar (e.g. `mir-data`, still unwritten)

1. `cp -r skills/mir-backend skills/mir-data`; rewrite the gates/references for that domain's reliability concerns (for data: schema drift, replay/backfill, exactly-once, late and out-of-order events…).
2. Description: `TRIGGER for pipeline/streaming work … SKIP for backend request handling, DB schema design, UI.` Name the adjacent pillars in the SKIP line — they are the ones it will over-trigger onto.
3. Add tiers or modules under it (`mir-data-spark`, `mir-data-kafka`) via the recipes above. A pillar can stay two-tier; add a middle tier only when the same content is true for every framework beneath it.
4. **AGENTS.md decision (keep always-on thin):** prefer **per-project** `AGENTS.md` — a backend repo gets the backend baseline, a frontend repo the frontend one — so a session never carries another pillar's always-on text. Only if you want one global baseline, keep `AGENTS.md` pillar-agnostic (persona + the hard rule + "the matching `mir-*` skill loads on demand") and let each pillar skill carry its own depth. Do **not** concatenate every pillar into one global `AGENTS.md`.
5. Reuse the same `agents/` reviewers, or add pillar-specific ones — the frontend pillar added `a11y-reviewer` and `frontend-perf-reviewer`. Any `subagent_type` a skill dispatches must have a file in `agents/`, or `validate.py` fails with `AGT001`.
6. `./validate.py`, then `./install.sh`.

## Worked example — what loads for "add a FastAPI checkout endpoint" on Cursor

- Tier 0: `AGENTS.md` (always).
- Tier 1: all `mir-*` descriptions visible (~a few hundred tokens total).
- Tier 2: three skills load — `mir-backend` (matches "backend, state-changing, money"), `mir-backend-python` (Python runtime), and `mir-backend-python-fastapi` (FastAPI endpoint). The other 42 do **not** — `mir-backend-node`, `mir-frontend*`, `mir-mobile*`, `mir-database*` and the rest are excluded by their SKIP lines.
- Tier 3: at Gate 1, `mir-backend/references/constraint-catalog.md` loads; at Gate 6, `mir-backend-python-fastapi/references/fastapi-gotchas.md`; the failure-mode catalog only if Gate 3 needs it.
- Tool axis: because you installed `--tool=cursor`, the same files are read from `~/.claude`; the question UI degrades to plain text. No content branched.
