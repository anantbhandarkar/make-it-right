---
name: mir-backend-python-fastapi
description: "Make It Right (FastAPI module). FastAPI + Async SQLAlchemy 2.0 + Postgres + Alembic + Redis specific reliability augmentation. Use alongside the mir-backend skill when the target stack is FastAPI — it carries the mechanical footguns that the framework-agnostic skill deliberately omits: async session lifecycle and scope, Pydantic v2 validation boundaries, Depends()-based auth and authorization, BackgroundTasks vs a real queue, async N+1 with selectinload, greenlet/sync-driver-in-async traps, Alembic migration safety on populated tables, and Redis idempotency/locking patterns. TRIGGER only when the Python backend stack is FastAPI — building, reviewing, or debugging a FastAPI endpoint, dependency, SQLAlchemy session, or Alembic migration. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-python (CPython runtime concerns: GIL, async/sync, fork-safety, cold start); this module only adds FastAPI/SQLAlchemy library mechanics. SKIP for Django, Flask, or any non-FastAPI stack (those get their own mir-backend-python-<framework> module), and for non-Python runtimes."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-python` (CPython runtime model) → **this** (FastAPI/SQLAlchemy library mechanics). Run the gates first; load the Python runtime tier for the concurrency/process model; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (GIL, async-vs-sync, blocking the event loop, fork-safe pools, cold start) live in `mir-backend-python` — not here.**

**Stack assumed:** FastAPI · SQLAlchemy 2.0 async (`AsyncSession`, `asyncpg`) · PostgreSQL · Alembic · Redis. If the project uses sync SQLAlchemy or a different DB, note the divergence before applying these.

## The FastAPI footguns AI walks into most

These are the stack-specific cousins of the failure-mode catalog. Each is something async-FastAPI code gets wrong even when the *logic* is right.

### 1. Async session lifecycle — the #1 source of mystery bugs
- **One `AsyncSession` per request, via a `Depends` dependency.** Never a module-level/global session — it's not concurrency-safe and you'll get cross-request data bleed or `InterfaceError`.
- **Never share one session across `asyncio.gather` tasks.** A session is a single connection's unit of work; concurrent use corrupts it. Give each task its own session from the factory.
- **Commit/rollback boundary belongs in the dependency or service, explicitly** — don't rely on autocommit. `expire_on_commit=False` if you return ORM objects after commit, or you'll trigger lazy loads on detached instances.

### 2. The async N+1 — worse than sync, because lazy loads *raise*
- In async SQLAlchemy, accessing an unloaded relationship outside a session raises `MissingGreenlet` / `DetachedInstanceError` — it doesn't silently query. Eager-load with `selectinload()` / `joinedload()` in the query. AI routinely writes `order.items` in a loop and ships an N+1 (or a crash).

### 3. Sync driver in the async path (the FastAPI face of a runtime rule)
- The general "don't block the event loop" rule lives in **`mir-backend-python`** (runtime tier). Here, the FastAPI/SQLAlchemy specifics: use the **async** driver `asyncpg` (not `psycopg2`) with `create_async_engine`; for unavoidable blocking work use Starlette's `run_in_threadpool` (or define the route as plain `def` so Starlette runs it in a threadpool automatically). Don't put a sync `Session` behind an `async def` route.

### 4. Pydantic v2 is the boundary, not the security layer
- Validation ≠ authorization. A valid body can still be a request to mutate someone else's data.
- **Mass assignment:** never build an ORM object from `model_dump()` blindly — use a separate `Create`/`Update` schema that *excludes* `id`, `is_admin`, `tenant_id`, `role`. Use `response_model` to avoid leaking fields (password hashes, internal flags) on the way out.
- `model_config` with `extra="forbid"` on input schemas to reject unexpected fields.

### 5. `Depends()` auth — get the chain right
- Authentication (`get_current_user`) and **authorization** (does this user own this object?) are different dependencies. AI implements the first and forgets the second → IDOR. Object-level checks go in the path that loads the object, not just the route guard.
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

## Alembic migration safety

See `references/alembic-migration-safety.md` for the full expand/contract patterns. Headline rule: **AI writes migrations as if the table is empty.** It isn't. The migration-reviewer agent enforces this at Gate 7.

## How this slots into the core pipeline

- **Gate 5 (Design):** when you state transaction boundaries and the idempotency mechanism, use the async-SQLAlchemy patterns above (session scope, `with_for_update`, conditional UPDATE, Redis key shape).
- **Gate 6 (Implementation):** code against `references/fastapi-gotchas.md` alongside the core codegen checklist.
- **Gate 7 (Review):** the reliability-reviewer should additionally check items 1–8 here; the migration-reviewer reads `references/alembic-migration-safety.md`.

## References

- `references/fastapi-gotchas.md` — expanded code-level examples of the 8 footguns above (right vs wrong).
- `references/alembic-migration-safety.md` — expand/contract, batched backfill, `CONCURRENTLY`, `NOT VALID` then `VALIDATE`, rolling-deploy compatibility.

## Edit boundary (what belongs here vs. the core)

**This module holds ONLY one library's mechanics — FastAPI · Async SQLAlchemy 2.0 · Postgres · Alembic · Redis.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability principle)? → **generic core** (`mir-backend`).
- True for every Python framework on CPython (GIL, async-vs-sync, blocking the loop, fork-safe pools, cold start, asyncio task hygiene)? → **runtime tier** (`mir-backend-python`).
- A mechanical footgun of *this library* (async session scope, `selectinload` N+1, `with_for_update`, Pydantic boundaries, Alembic-on-populated-table, Redis `SET NX`)? → **here**.
- A *different* framework on Python (Django, Flask) → new `mir-backend-python-<framework>` module. A *different* runtime → its own tier. Never widen this one.

Full layered edit map: see `mir-backend/SKILL.md` → "Where these instructions live".
