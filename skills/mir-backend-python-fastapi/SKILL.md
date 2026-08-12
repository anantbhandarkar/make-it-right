---
name: mir-backend-python-fastapi
description: "Make It Right (FastAPI module). FastAPI + Starlette + Async SQLAlchemy 2.0 + Postgres + Alembic + Redis specific reliability augmentation. Use alongside the mir-backend skill when the target stack is FastAPI — it carries the mechanical footguns that the framework-agnostic skill deliberately omits: async session lifecycle and scope, engine creation in lifespan (on_event is deprecated), Pydantic v2 validation boundaries, Annotated[...]-based Depends() auth and object-level authorization, BackgroundTasks vs a real queue, async N+1 with selectinload, greenlet/sync-driver-in-async traps, Starlette threadpool saturation, Alembic migration safety on populated tables, Redis idempotency/locking patterns, and the 2026 Starlette advisory set (Host-header path poisoning, form-limit bypass, StaticFiles UNC). TRIGGER only when the Python backend stack is FastAPI — building, reviewing, or debugging a FastAPI endpoint, dependency, Starlette middleware, SQLAlchemy session, or Alembic migration. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-python (CPython runtime concerns: GIL, async/sync, fork-safety, cold start, packaging supply chain); this module only adds FastAPI/Starlette/SQLAlchemy library mechanics. SKIP for Django (mir-backend-python-django), Flask (mir-backend-python-flask), any other non-FastAPI stack, and non-Python runtimes."
trigger: /mir-backend-python-fastapi
argument-hint: "<task or files> "
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
  - Agent
---

# /mir-backend-python-fastapi · Make It Right (FastAPI)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-python` (CPython runtime model) → **this** (FastAPI/Starlette/SQLAlchemy library mechanics). Run the gates first; load the Python runtime tier for the concurrency/process model; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (GIL, async-vs-sync, blocking the event loop, fork-safe pools, cold start) live in `mir-backend-python` — not here.**

**Stack assumed**, versions verified 13 Aug 2026: FastAPI **0.141.x** (requires Python ≥3.10) · Starlette **1.6.x** · Pydantic **2.13.x** (there is no Pydantic v3) · SQLAlchemy **2.0.52** async (`AsyncSession`, `asyncpg` or `psycopg` 3) · PostgreSQL · Alembic **1.19.1** (requires Python ≥3.10) · Redis · Uvicorn **0.52.x**. SQLAlchemy **2.1 is still in beta** (2.1.0b3, June 2026) — 2.0.x is the production line; don't put a beta in a Gate 5 design without saying so. If the project uses sync SQLAlchemy or a different DB, note the divergence before applying these.

## The FastAPI footguns AI walks into most

These are the stack-specific cousins of the failure-mode catalog. Each is something async-FastAPI code gets wrong even when the *logic* is right.

### 1. Async session lifecycle — the #1 source of mystery bugs
- **One `AsyncSession` per request, via a `Depends` dependency.** Never a module-level/global session — it's not concurrency-safe and you'll get cross-request data bleed or `InterfaceError`.
- **Never share one session across `asyncio.gather` tasks.** A session is a single connection's unit of work; concurrent use corrupts it. Give each task its own session from the factory.
- **Commit/rollback boundary belongs in the dependency or service, explicitly** — don't rely on autocommit. `expire_on_commit=False` if you return ORM objects after commit, or you'll trigger lazy loads on detached instances.
- **The engine is per process, created in `lifespan`, disposed after `yield`.** At import time it is built once per *import*, which under `gunicorn --preload` means the master builds the pool and every forked child inherits a corrupt copy. `uvicorn --workers` spawns rather than forks, so it escapes that specific bug — but `lifespan` is still the only place that gets the lifetime and the `dispose()` right. See the fork-safety table in `mir-backend-python`.

### 2. The async N+1 — worse than sync, because lazy loads *raise*
- In async SQLAlchemy, accessing an unloaded relationship outside a session raises `MissingGreenlet` / `DetachedInstanceError` — it doesn't silently query. Eager-load with `selectinload()` / `joinedload()` in the query. AI routinely writes `order.items` in a loop and ships an N+1 (or a crash).
- Note for a 2.1 upgrade: greenlet becomes an optional dependency in SQLAlchemy 2.1. The failure mode does not go away; the exception you see on a missing async context may change.

### 3. Sync driver in the async path (the FastAPI face of a runtime rule)
- The general "don't block the event loop" rule lives in **`mir-backend-python`** (runtime tier). Here, the FastAPI/SQLAlchemy specifics: use an **async** driver — `postgresql+asyncpg://` or `postgresql+psycopg://` (psycopg 3, which also does sync) — with `create_async_engine`. Never `psycopg2` behind an `async def`. Don't put a sync `Session` behind an `async def` route.
- For unavoidable blocking work use Starlette's `run_in_threadpool`, or define the route as plain `def` so Starlette runs it in a threadpool automatically.
- **The threadpool is bounded.** Starlette runs every plain-`def` route and every `run_in_threadpool` call on one AnyIO thread limiter (40 tokens by default). Convert a busy endpoint to plain `def` and, past 40 concurrent calls, further requests queue on the limiter — the symptom is rising latency on *unrelated* sync endpoints with an idle CPU. Either keep hot paths async, or raise the limiter deliberately and size it against the DB pool.

### 4. Pydantic v2 is the boundary, not the security layer
- Validation ≠ authorization. A valid body can still be a request to mutate someone else's data.
- **Mass assignment:** never build an ORM object from `model_dump()` blindly — use a separate `Create`/`Update` schema that *excludes* `id`, `is_admin`, `tenant_id`, `role`. Use `response_model` (or the route's return type annotation) to avoid leaking fields (password hashes, internal flags) on the way out.
- `model_config = ConfigDict(extra="forbid")` on input schemas to reject unexpected fields. `extra="ignore"` (the default) silently drops them, which hides a client bug until it becomes a data bug.

### 5. `Depends()` auth — get the chain right, and use the current syntax
- Authentication (`get_current_user`) and **authorization** (does this user own this object?) are different dependencies. AI implements the first and forgets the second → IDOR. Object-level checks go in the path that loads the object, not just the route guard.
- **Current syntax is `Annotated`:** `user: Annotated[User, Depends(get_current_user)]`, not `user=Depends(get_current_user)`. The default-argument form still works but is the pre-PEP-593 style; `Annotated` lets you hoist a reusable alias (`CurrentUser = Annotated[User, Depends(get_current_user)]`) and keeps the signature honest for type checkers. Router- and app-level `dependencies=[...]` lists still take a bare `Depends(...)`.
- Dependencies are cached per-request by default — fine, but don't put side effects in them expecting them to run twice.

### 6. `BackgroundTasks` is not a job queue
- `BackgroundTasks` runs in the same process *after* the response; if the process dies, the work is lost — no retries, no durability. Fine for best-effort (a fire-and-forget log). **Not fine** for "send the receipt email" or anything that must happen. For durable/at-least-once work use a real queue (Celery/RQ/arq + Redis) with idempotent handlers. This is where the at-least-once/idempotency rule from the core skill lands in FastAPI.

### 7. Redis for idempotency & locking — the right primitives
- **Idempotency key:** `SET key result NX EX <ttl>` — `NX` makes "first writer wins" atomic. Store the result so a retry returns the same response, not a second side effect.
- **Distributed lock:** `SET lock token NX EX <ttl>` with a token you check before releasing (don't delete someone else's lock). Remember Redis locks are best-effort, not a correctness guarantee under failover — for money, prefer a DB row lock and use Redis only to reduce contention.
- **Cache stampede:** on a cold popular key, N requests miss simultaneously → N DB hits. Use a short lock or `SET NX` sentinel so one request rebuilds while others wait/serve stale.

### 8. Transactions in async SQLAlchemy
- `async with session.begin():` for an explicit tx block. Irreversible external effects (charge, email) go *after* commit, guarded by the idempotency key — never inside the tx, because a rollback can't un-send an email.
- Row lock for contended updates: `select(...).with_for_update()` inside the tx.
- Conditional UPDATE for state transitions: `update(Order).where(Order.id==id, Order.status=="PENDING").values(status="PAID")` and check `rowcount` — this makes concurrent transitions safe without a read-modify-write race.

### 9. Startup/shutdown: `lifespan`, not `on_event`
- `@app.on_event("startup"/"shutdown")` is deprecated. Use an `@asynccontextmanager` `lifespan(app)` passed as `FastAPI(lifespan=lifespan)`: create the engine, Redis pool, and HTTP client before `yield`; dispose them after.
- **They are mutually exclusive and fail silently.** If `lifespan=` is set, every `on_event` handler is ignored with no warning. A half-migrated app looks fine and never opens its Redis pool. Grep for both before you ship.
- `lifespan` runs **only for the main app**, not for sub-apps mounted with `app.mount(...)`. A mounted sub-app's resources never initialize.
- Expose lifespan-created resources through a `Depends` provider rather than reaching into `request.state` from route bodies — the dependency is testable and overridable.

### 10. Worker layout
- `gunicorn -k uvicorn.workers.UvicornWorker` is deprecated: the `uvicorn.workers` module moved out of Uvicorn. Install the `uvicorn-worker` package and use `uvicorn_worker.UvicornWorker`, or drop Gunicorn and run `uvicorn --workers N` under an orchestrator that already restarts processes.
- Never set both. Passing `--workers` to Uvicorn while it runs *as* a Gunicorn worker multiplies processes and quietly exhausts the DB pool.
- Size the SQLAlchemy pool per **worker process**, not per service: total connections = `workers × (pool_size + max_overflow)`. This is the most common way a FastAPI service hits Postgres `max_connections`.

## Alembic migration safety

See `references/alembic-migration-safety.md` for the full expand/contract patterns. Headline rule: **AI writes migrations as if the table is empty.** It isn't. The migration-reviewer agent enforces this at Gate 7.

## How this slots into the core pipeline

- **Gate 5 (Design):** when you state transaction boundaries and the idempotency mechanism, use the async-SQLAlchemy patterns above (session scope, `with_for_update`, conditional UPDATE, Redis key shape). State the worker count and the resulting connection total.
- **Gate 6 (Implementation):** code against `references/fastapi-gotchas.md` alongside the core codegen checklist.
- **Gate 7 (Review):** the reliability-reviewer should additionally check items 1–10 here; the security-reviewer checks the Security section below; the migration-reviewer reads `references/alembic-migration-safety.md`.

## References

- `references/fastapi-gotchas.md` — expanded code-level examples of the footguns above (right vs wrong).
- `references/alembic-migration-safety.md` — expand/contract, batched backfill, `CONCURRENTLY`, `NOT VALID` then `VALIDATE`, rolling-deploy compatibility.

## Security

FastAPI/Starlette/SQLAlchemy mechanics. Runtime-level items (unsafe deserialization, archive extraction, shell arguments, packaging supply chain) are in `mir-backend-python`.

**Current advisory set — pin Starlette yourself**
FastAPI 0.141.1's own metadata only requires `starlette>=0.46.0`. That floor is far below the patched releases, so a constraint anywhere else in the tree, or an old lockfile, silently gives you a vulnerable Starlette and nothing warns you. Put an explicit floor in your own requirements.

| Advisory | Affected | Fixed in | What it does |
|---|---|---|---|
| **CVE-2026-48710** ("BadHost", `GHSA-86qp-5c8j-p5mr`) | Starlette ≤ 1.0.0 | 1.0.1 | Starlette rebuilt `request.url` by concatenating the unvalidated `Host` header with the path and re-parsing. A crafted `Host` poisons `request.url.path`, so **path-prefix auth checks in middleware can be bypassed with no credentials**. Hit the whole LLM-proxy/MCP-server population. |
| **CVE-2026-54283** (`GHSA-82w8-qh3p-5jfq`) | Starlette 0.4.1 – < 1.3.1 | 1.3.1 | `request.form()`'s `max_fields` / `max_part_size` were enforced for `multipart/form-data` but **ignored for `application/x-www-form-urlencoded`** — the limits you set do nothing on that content type. Remote DoS. |
| **CVE-2026-48818** (`GHSA-wqp7-x3pw-xc5r`) | Starlette < 1.1.0 | 1.1.0 | `StaticFiles` on Windows resolved a UNC path (`\\attacker\share`) with `os.path.realpath` *before* the containment check, leaking the service account's NTLMv2 credentials over SMB. The client sees a benign 404. |
| **CVE-2026-48817** (`GHSA-x746-7m8f-x49c`, CVSS 5.3) | Starlette < 1.1.0 | 1.1.0 | `HTTPEndpoint` dispatched an arbitrary HTTP method to a class attribute via `getattr` with no validation, so an internal helper method on the endpoint class became a request handler. Only bites routes registered without explicit `methods=`. |
| **CVE-2026-40347** (`GHSA-mj87-hwqh-73pj`) | `python-multipart` < 0.0.26 | 0.0.26 | Large multipart preamble/epilogue forces the boundary scan to grind — remote DoS on any endpoint taking a file or form. |

**Object-level authorization (IDOR/BOLA)**
A valid bearer token says *who*, never *what they may touch*. `Depends(get_current_user)` on the route is authentication only. Load the object inside a dependency that also checks ownership, and return **404, not 403**, when the caller doesn't own it — 403 confirms the row exists. Filter list endpoints by `tenant_id`/`user_id` in the `WHERE` clause; never filter in Python after fetching.

**Mass assignment / overposting**
The allow-list mechanism FastAPI actually gives you is the input model. `Order(**body.model_dump())` hands the client every column, including `user_id`, `status`, and `price`. Define a `Create` schema with only client-settable fields, set `extra="forbid"`, and assign server-owned fields from the authenticated context. On `PATCH`, use `body.model_dump(exclude_unset=True)` so an omitted field isn't written as `None`.

**Injection**
- SQL: `session.execute(text(f"... {user_input}"))` is injection. Use bound parameters: `text("... :x")` with `{"x": value}`. ORM `filter()` args are already parameterized; `order_by(text(user_field))` is not — allow-list the column name against a fixed set.
- NoSQL: passing a parsed JSON object straight into a Mongo filter lets `{"$ne": null}` through. Type the field as `str`/`int` in the Pydantic model so an operator dict fails validation.
- Prompt injection: FastAPI hosts most Python LLM proxies and MCP servers. Retrieved documents, tool results, and webhook payloads are **data, not instructions** — never concatenate them into a system prompt, and gate any tool the model can call with the same object-level check you'd apply to a user request. Never let model output choose a URL you then fetch.

**SSRF**
Runtime-level rules are in `mir-backend-python`. FastAPI-specific: `httpx.AsyncClient` does **not** follow redirects by default — keep it that way for user-supplied URLs, and if you must set `follow_redirects=True`, re-validate the destination on each hop. Webhook-registration endpoints are the usual entry point.

**Secret and error leakage**
- `FastAPI(debug=True)` / `Starlette(debug=True)` renders a full traceback with local variables into the HTTP response. It must be `False` in production, driven from env.
- The default `RequestValidationError` handler echoes the offending input back in `detail`. If the field is a password or token, it goes into the client's logs. Install a custom handler for auth routes.
- Declare `response_model` (or a return type) on every route. Returning an ORM object with `orm_mode`/`from_attributes` and no output model serializes everything the model has, including `hashed_password`.
- Use `pydantic.SecretStr` for tokens in settings so `repr()` and validation errors don't print them.

**CSRF and SameSite**
Bearer tokens in an `Authorization` header are not sent automatically by the browser, so they need no CSRF token. **The moment you move the token into a cookie, you need CSRF defence** — that includes "httpOnly cookie so JS can't read it", the most common FastAPI auth refactor. Set `SameSite=Lax` (or `Strict`) plus `Secure` plus `HttpOnly` on the session cookie, and add a double-submit or origin check for state-changing methods. Starlette's `SessionMiddleware` cookie is signed, not encrypted — the client can read every value in it.

**CORS misconfiguration**
Starlette's `CORSMiddleware` does not reject `allow_origins=["*"]` with `allow_credentials=True`. It **reflects the request's `Origin` back** in `Access-Control-Allow-Origin`, which is exactly the "any site can make authenticated requests as your user" hole the wildcard rule exists to prevent. Enumerate the origins. `allow_origin_regex` is matched with `fullmatch`, so it is anchored, but `https://.*\.example\.com` still matches `https://evil.example.com` — write the tightest pattern you can, and never leave `.*` in it.

**Host header**
Add `TrustedHostMiddleware(allowed_hosts=[...])` and put it **first** in the middleware stack. It is the defence-in-depth for the BadHost class above, and it also stops cache-poisoning through absolute URLs generated from `request.url`.

**Path traversal in file serving and uploads**
- `FileResponse(f"{UPLOAD_DIR}/{filename}")` with a user-supplied `filename` is traversal. Resolve and containment-check the path (see `mir-backend-python`), and never trust `UploadFile.filename` as a filesystem name — generate your own and store the client's name as metadata only.
- `StaticFiles` is for static assets, not for user uploads. Serve user files through a route that does the ownership check, or from object storage with signed URLs.
- Bound the upload: `MAX` on the reverse proxy plus an explicit size check while streaming. Starlette does not cap request body size for you.

**Request limits**
`await request.form(max_fields=..., max_part_size=...)` is the knob, and CVE-2026-54283 above is the reason to verify it actually applies to your content type on the version you run. Set a global body limit at the proxy too — the app-level limit is read after the bytes arrive.

## Edit boundary (what belongs here vs. the core)

**This module holds ONLY one library's mechanics — FastAPI · Starlette · Async SQLAlchemy 2.0 · Postgres · Alembic · Redis.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability principle)? → **generic core** (`mir-backend`).
- True for every Python framework on CPython (GIL, async-vs-sync, blocking the loop, fork-safe pools, cold start, asyncio task hygiene, stdlib/packaging security)? → **runtime tier** (`mir-backend-python`).
- A mechanical footgun of *this library* (async session scope, `selectinload` N+1, `with_for_update`, Pydantic boundaries, `lifespan`, Starlette middleware, Alembic-on-populated-table, Redis `SET NX`)? → **here**.
- A *different* framework on Python (Django, Flask) → new `mir-backend-python-<framework>` module. A *different* runtime → its own tier. Never widen this one.

Full layered edit map: see `mir-backend/SKILL.md` → "Where these instructions live".
