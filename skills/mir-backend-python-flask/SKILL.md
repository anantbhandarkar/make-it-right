---
name: mir-backend-python-flask
description: "Make It Right (Flask module). Flask 3 specific reliability augmentation. Use alongside the mir-backend skill when the target stack is Flask — it carries the mechanical footguns that the framework-agnostic skill deliberately omits: app/request context misuse (current_app/request/g outside context), missing input validation and object-level authorization, SQLAlchemy session scoping and teardown, app-factory pattern and circular import avoidance, offloading heavy work to Celery/RQ, config/secret safety (debug=True RCE, SECRET_KEY), and Alembic migration safety via Flask-Migrate. TRIGGER only when the Python backend stack is Flask — building, reviewing, or debugging a Flask route, blueprint, extension, SQLAlchemy session, or migration. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-python (CPython runtime concerns: GIL, async/sync, fork-safety, cold start); this module only adds Flask library mechanics. SKIP for Django, FastAPI, or any non-Flask stack (those get their own mir-backend-python-<framework> module), and for non-Python runtimes."
trigger: /mir-backend-python-flask
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-python-flask · Make It Right (Flask)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-python` (CPython runtime model) → **this** (Flask library mechanics). Run the gates first; load the Python runtime tier for the concurrency/process model; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (GIL, async-vs-sync, blocking the event loop, fork-safe pools, cold start) live in `mir-backend-python` — not here.**

**Stack assumed:** Flask 3 · Flask-SQLAlchemy (SQLAlchemy 2.x) · PostgreSQL · Flask-Migrate / Alembic · Celery or RQ for background work. If the project uses a bare SQLAlchemy `scoped_session` without Flask-SQLAlchemy, apply the session-scoping rules directly — the pattern is the same, the scaffold differs.

## The Flask footguns AI walks into most

These are the stack-specific cousins of the failure-mode catalog. Each is something Flask code gets wrong even when the *logic* is right.

### 1. App and request context — the three magic proxies and where they die

`current_app`, `request`, and `g` are thread-local (or context-var-local) proxies that only resolve inside an active application or request context. Using them at import time, in a module-level expression, or in a background thread/task raises `RuntimeError: Working outside of application context` or `RuntimeError: Working outside of request context`. AI writes context-access code at class-definition time or in a `threading.Thread` target and ships a crash that never surfaces in a single-threaded dev run.

**`current_app`** — resolves to the app bound on the current context stack. Access it inside a view function, a CLI command, or after explicitly pushing a context.

**`g`** — request-scoped scratch space. It is torn down at the end of each request. Do not use `g` as an inter-request cache or a way to share state between requests — it is not. Each request gets a fresh `g`.

**Background threads:** any thread spawned from a request (e.g., `threading.Thread(target=fn).start()`) has no context by default. Push one explicitly:
```python
# WRONG — raises RuntimeError in the thread
def background():
    result = some_db_query()   # uses current_app implicitly

thread = threading.Thread(target=background)
thread.start()

# RIGHT
app = current_app._get_current_object()   # get the real app, not the proxy

def background(app):
    with app.app_context():
        result = some_db_query()

thread = threading.Thread(target=background, args=(app,))
thread.start()
```

**Celery/RQ tasks:** same issue — tasks run in a worker process with no Flask context unless the task sets one up. The standard pattern is a `ContextTask` base class that pushes `app.app_context()` before calling the task body.

### 2. Unopinionated by design — validation and authorization are easy to forget

Flask has no built-in request validation, no enforced authentication scheme, and no object-level permission model. AI builds a route that parses `request.json` directly and applies business logic, forgetting both layers. The result is a service that accepts malformed input and happily returns another user's data.

**Input validation:** use Pydantic (v2 `model_validate`) or marshmallow to parse and validate request bodies before they touch the business layer. Never use raw `request.json.get("field")` for fields that have type or range constraints.
```python
from pydantic import BaseModel, ValidationError

class CreateOrderBody(BaseModel):
    product_id: int
    quantity: int

@app.post("/orders")
def create_order():
    try:
        body = CreateOrderBody.model_validate(request.get_json())
    except ValidationError as e:
        abort(422, description=e.errors())
    ...
```

**Object-level authorization (IDOR):** decorating a route with `@login_required` (flask-login) confirms the caller is authenticated; it does **not** confirm the caller owns the resource being loaded. Always assert ownership after loading the object:
```python
@app.get("/orders/<int:order_id>")
@login_required
def get_order(order_id):
    order = db.get_or_404(Order, order_id)
    if order.user_id != current_user.id:
        abort(403)           # object-level check — not implied by @login_required
    return order_schema.dump(order)
```

### 3. SQLAlchemy session scoping — leaked sessions corrupt state

A `Session` is a unit of work bound to a single connection. Sharing one session across requests or threads corrupts it — you get stale data, `DetachedInstanceError`, or silent cross-request data bleed.

**Flask-SQLAlchemy** handles scoping automatically: `db.session` is a `scoped_session` keyed on the current thread (or async context). It is removed in `teardown_appcontext` — you do not need to manage the lifecycle manually beyond `db.session.remove()` being called by the extension.

**Bare SQLAlchemy:** if you're not using Flask-SQLAlchemy, create a `scoped_session` and register teardown explicitly:
```python
from sqlalchemy.orm import scoped_session, sessionmaker

Session = scoped_session(sessionmaker(bind=engine))

@app.teardown_appcontext
def shutdown_session(exception):
    Session.remove()   # returns the session to the pool and clears the scope
```

**Commit/rollback boundary must be explicit.** Flask-SQLAlchemy does not autocommit. Call `db.session.commit()` when the work should persist; if an exception propagates, call `db.session.rollback()` (or rely on `teardown_appcontext` to roll back via `Session.remove()` — but only if you haven't already committed half the work). An unfinished transaction that is only `remove()`d without commit is rolled back, which is correct — but explicit rollback in an `except` block is clearer and safer.

```python
@app.post("/transfer")
def transfer():
    try:
        from_acct.balance -= amount
        to_acct.balance += amount
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
```

### 4. Blueprint organization and the app factory — avoid import-time global state

A module-level `app = Flask(__name__)` and extensions bound directly to it at import time (`db = SQLAlchemy(app)`) create a global singleton that makes testing impossible (you can't swap config between tests) and causes circular imports as blueprints try to import from the module that imports them.

**App factory pattern:** `create_app(config=None)` instantiates the app, configures it, calls `extension.init_app(app)`, and registers blueprints — all inside the function. Extensions are created at module level but bound to the app lazily via `init_app`:

```python
# extensions.py — no app reference at module level
from flask_sqlalchemy import SQLAlchemy
db = SQLAlchemy()

# factory.py
from .extensions import db

def create_app(config=None):
    app = Flask(__name__)
    app.config.from_object(config or "config.ProductionConfig")
    db.init_app(app)
    from .orders import orders_bp
    app.register_blueprint(orders_bp, url_prefix="/orders")
    return app
```

**Circular imports:** blueprints that import `db` from `extensions.py` (not from the app module) break the cycle. Never import from the factory module inside a blueprint.

**Testing:** each test calls `create_app("config.TestingConfig")`, so isolation is trivial. A global `app` singleton makes this impossible without monkey-patching config.

### 5. Heavy and durable work — Flask workers block

Flask runs one request per thread (or coroutine in async mode). A route that does a 30-second video encode, a large data export, or a third-party API call with no timeout blocks that worker for the duration, reducing throughput to `workers - 1` for all other requests.

**Offload to Celery or RQ:**
- The route creates the job record, enqueues the task with an idempotency key, and returns `202 Accepted` with a job ID.
- The worker executes the task; its result is stored in the DB or Redis.
- The client polls or receives a webhook.

```python
@app.post("/exports")
def start_export():
    job = ExportJob(user_id=current_user.id, status="queued")
    db.session.add(job)
    db.session.commit()
    run_export.apply_async(args=[job.id], task_id=str(job.id))
    return {"job_id": job.id}, 202
```

**Idempotency:** Celery tasks must be idempotent (re-delivery safe). Store the outcome keyed on the job ID; a retry that finds a completed record returns without re-executing. This is the at-least-once / idempotency rule from `mir-backend` landing in Flask's Celery integration.

**Flask's async support (Flask 2+):** `async def` route handlers are supported but run in a threadpool under the hood — Flask is still a WSGI framework. True async concurrency requires ASGI + Quart or moving to FastAPI. The runtime-level async/sync decision lives in `mir-backend-python`; here the note is only that Flask's `async def` views do not give you a free event loop.

### 6. Config and secrets — debug mode is an RCE surface

Flask's Werkzeug debugger exposes an interactive Python console in the browser when `DEBUG=True` and an unhandled exception occurs. The console is PIN-protected in recent Werkzeug versions, but the PIN is derivable from `/proc/self/cgroup` and related system files on many Linux hosts. Running `app.run(debug=True)` in production is a remote code execution vulnerability.

**Rules:**
- `DEBUG` is `False` in production, always. Derive it from env: `app.config["DEBUG"] = os.environ.get("FLASK_DEBUG", "0") == "1"`.
- `SECRET_KEY` must be a long, random, secret value — it signs session cookies. A weak or committed key lets an attacker forge session cookies and impersonate any user. Generate once: `python -c "import secrets; print(secrets.token_hex(32))"`, store in a secret manager or environment variable, never in source.
- Load config from environment variables or a secret manager; never hardcode credentials in `config.py` that is committed. Use `python-dotenv` for local dev only, and `.gitignore` the `.env` file.

```python
# WRONG — RCE surface in prod
if __name__ == "__main__":
    app.run(debug=True)

# RIGHT — read from environment
app.config.update(
    DEBUG=os.environ.get("FLASK_DEBUG", "0") == "1",
    SECRET_KEY=os.environ["SECRET_KEY"],
    SQLALCHEMY_DATABASE_URI=os.environ["DATABASE_URL"],
)
```

### 7. Migrations via Flask-Migrate / Alembic — same populated-table discipline

Flask-Migrate wraps Alembic. The headline migration safety rules from the FastAPI/Alembic module apply without change: AI writes migrations as if the table is empty; it isn't.

**Key rules for Flask/Alembic:**
- Adding a `NOT NULL` column with a server default on a large table issues a full table rewrite under Postgres < 11, and a locking `UPDATE` under Postgres 11+ if Django-style ORMs generate it. Split into: add nullable → batched `UPDATE` in a `op.execute` data migration → `ALTER COLUMN … SET NOT NULL`.
- Create indexes with `op.create_index(..., postgresql_concurrently=True)` inside a migration that sets `down_revision` correctly and runs outside a transaction (`with op.get_context().autocommit_block():` or via `--no-transaction` in raw Alembic).
- **Separate schema and data migrations.** A data migration holding rows in a lock accumulates schema locks behind it.
- **Reversibility:** provide a `downgrade()` function. If reversal is destructive or impossible, document it explicitly in a comment; don't leave a silent no-op.

```python
# alembic migration — safe NOT NULL add
def upgrade():
    op.add_column("order", sa.Column("region", sa.String(), nullable=True))
    # data migration in a separate file, then:

def upgrade_set_not_null():
    op.alter_column("order", "region", nullable=False)
```

## How this slots into the pipeline

- **Gate 5 (Design):** state the context push strategy for background work, the session teardown pattern, the app-factory shape, and the validation layer (Pydantic/marshmallow). Migration plans for populated tables must name the three-step add-nullable → backfill → NOT NULL pattern.
- **Gate 6 (Implementation):** code against the footguns above; confirm `SECRET_KEY` and `DEBUG` come from env; confirm `db.session.remove()` is in `teardown_appcontext`; confirm background tasks are Celery/RQ, not inline.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–7 here. The migration-reviewer checks each new Alembic revision for NOT NULL columns, missing `CONCURRENTLY`, and missing batched backfills.

## Edit boundary (what belongs here vs. the runtime tier)

**This module holds ONLY Flask library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability)? → **generic core** (`mir-backend`).
- True for every Python framework on CPython (GIL, async-vs-sync, blocking the event loop, fork-safe pools, cold start, asyncio task hygiene)? → **runtime tier** (`mir-backend-python`). Note: the WSGI-vs-ASGI deployment decision lives there; here we only cover Flask-specific async semantics.
- A mechanical footgun of *this library* (context proxies, session scoping, app factory, `debug=True` RCE, `SECRET_KEY`, Flask-Migrate discipline)? → **here**.
- A *different* framework on Python (FastAPI, Django) → its own `mir-backend-python-<framework>` module. A *different* runtime → its own tier. Never widen this one.
