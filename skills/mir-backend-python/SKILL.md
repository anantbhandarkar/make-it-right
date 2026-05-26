---
name: mir-backend-python
description: "Make It Right (Python runtime tier). CPython/PyPy runtime reliability footguns that are shared across EVERY Python backend framework (FastAPI, Django, Flask, Celery) — distinct from the generic backend gates and from any one framework's mechanics. Covers: the GIL (threads give no CPU parallelism), async-vs-sync 'coloring', blocking the event loop, choosing asyncio vs threads vs multiprocessing vs a worker queue, fork-safety of connection pools, serverless cold starts, and dropped-task exceptions. TRIGGER when the backend runtime is Python — sits between mir-backend (generic) and the framework module (e.g. mir-backend-python-fastapi). SKIP for Node/JVM/Go/Rust/.NET/Ruby/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics (those live in the framework module)."
trigger: /mir-backend-python
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-python · Make It Right (Python runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-python-fastapi`) knows the **library's mechanics**. This tier owns what's true for **all Python backends because they run on CPython** — the concurrency model and process model that FastAPI, Django, Flask, and Celery all inherit.

**Runtime assumed:** CPython 3.11+ (the notes hold for PyPy except where GIL specifics differ). Load order: `mir-backend` → `mir-backend-python` → `<framework module>`.

## The CPython footguns AI walks into (framework-agnostic)

### 1. The GIL — threads are NOT CPU parallelism
Python threads share one interpreter lock, so **CPU-bound work does not run in parallel across threads** — it serializes and you pay context-switch overhead on top. Threads only help **I/O-bound** work (the GIL releases during blocking I/O).
- CPU-bound (parsing, crypto, image/ML compute) → `multiprocessing` / `ProcessPoolExecutor`, a native extension that releases the GIL, or offload to a separate service. Never "add threads" expecting speedup.
- I/O-bound → threads or asyncio are fine.
- This is the runtime-level reason the runtime-map says "SKIP Python for heavily CPU-bound / microsecond-latency paths."

### 2. Async/sync "coloring" — pick one model per path and don't mix carelessly
An `async def` function can only be awaited from async context; a sync function that calls blocking I/O must not run on the event loop. Mixing them is the #1 source of "the server mysteriously stalls under load."
- Don't call **blocking** I/O (sync DB driver, `requests`, `time.sleep`, big CPU) inside `async def` — it freezes the **entire** event loop for every concurrent request. Offload with `await loop.run_in_executor(...)` / `anyio.to_thread.run_sync(...)`, or use an async client.
- Don't call `asyncio.run()` inside an already-running loop.
- Decide per endpoint/job: **async all the way** (async web framework + async drivers) **or sync all the way** (sync framework + thread workers). Half-async is where the bugs live.

### 3. Concurrency-model decision (state it in Gate 5)
| Workload | Right model | Wrong model (the trap) |
|---|---|---|
| I/O-bound, high concurrency | asyncio (single thread, many awaits) | thread-per-request at huge counts |
| I/O-bound, moderate | thread pool | — |
| CPU-bound | multiprocessing / native ext / separate service | threads (GIL serializes) |
| Long/durable/at-least-once work | worker queue (Celery/RQ/arq) | request thread / asyncio task that dies with the process |

### 4. Process model — workers are shared-NOTHING
Production runs N worker processes (gunicorn/uvicorn `--workers`, Celery prefork). **In-process global state is per-worker and is lost on restart** — never use a module-level dict/counter as if it were shared across workers. Shared state goes in Redis/DB.
- **Fork-safety:** connection pools, async event loops, and client sockets created **before** a fork are corrupt in the child. Initialize DB/Redis pools **after** fork (gunicorn `post_fork`, or lazily on first use in the worker), never at import time before the server forks.

### 5. Serverless / cold starts
- Heavy work at **import time** (loading a model, building a pool, importing huge deps) is paid on every cold start. Lazy-init expensive resources on first request; keep import side-effect-free.
- Reuse connections across invocations via a module-level client created **lazily** (warm-container reuse) — but cap pool size to the platform's concurrency.

### 6. Dropped task & coroutine exceptions
- A bare `asyncio.create_task(...)` whose result is never awaited will **swallow exceptions** and can be garbage-collected mid-flight. Keep a reference and attach a done-callback / await it, or use a task group (`asyncio.TaskGroup` / `anyio` nursery).
- An un-awaited coroutine is a silent no-op (`coroutine was never awaited` warning) — the work simply never ran.

## How this slots into the pipeline
- **Gate 0/5 (model choice):** state the concurrency model (async/threads/multiprocessing/queue) and justify it against the workload table above. A mismatch here (e.g. threads for CPU-bound) is a runtime-level design defect — flag it.
- **Gate 6 (implementation):** never block the event loop; init pools post-fork; offload CPU work.
- **Gate 7 (review):** the reliability-reviewer should additionally check items 1–6 here for any Python service.

## Edit boundary (what belongs here vs. above/below)
- Generic, all-language rules (idempotency, invariants, gates) → **up** to `mir-backend`.
- A specific library's mechanics (FastAPI `Depends`, SQLAlchemy `selectinload`, Django ORM `select_related`, Alembic) → **down** to the framework module (`mir-backend-python-<framework>`).
- **Here:** only what every Python backend shares because of CPython (concurrency model, process/fork model, GIL, cold start, asyncio task hygiene).
- A different runtime (Node, Go, JVM…) → its own `mir-backend-<runtime>` tier. Never widen this one.
