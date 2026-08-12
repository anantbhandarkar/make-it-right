---
name: mir-backend-python-flask
description: "Make It Right (Flask module). Flask 3.1 + Werkzeug 3.1 specific reliability augmentation. Use alongside the mir-backend skill when the target stack is Flask — it carries the mechanical footguns that the framework-agnostic skill deliberately omits: app/request context misuse (current_app/request/g outside a context, background threads, Celery tasks), missing input validation and object-level authorization, SQLAlchemy session scoping and teardown, the app-factory pattern and circular imports, offloading heavy work to Celery/RQ, Flask 3.1 config safety (SECRET_KEY_FALLBACKS key rotation, TRUSTED_HOSTS after the SERVER_NAME behaviour change, MAX_CONTENT_LENGTH / MAX_FORM_MEMORY_SIZE / MAX_FORM_PARTS, debug-mode RCE), Alembic migration safety via Flask-Migrate, and Flask's own 2026 advisories. TRIGGER only when the Python backend stack is Flask — building, reviewing, or debugging a Flask route, blueprint, extension, SQLAlchemy session, or Flask-Migrate revision. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-python (CPython runtime concerns: GIL, async/sync, fork-safety, cold start, packaging supply chain); this module only adds Flask library mechanics. SKIP for Django (mir-backend-python-django), FastAPI (mir-backend-python-fastapi), Quart or any other non-Flask stack, and non-Python runtimes."
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

**Stack assumed**, versions verified 13 Aug 2026: Flask **3.1.3** (released 18 Feb 2026; requires Python ≥3.9) · Werkzeug **3.1.8** · Flask-SQLAlchemy over SQLAlchemy **2.0.52** · PostgreSQL · Flask-Migrate / Alembic **1.19.1** · Celery or RQ for background work. If the project uses bare SQLAlchemy with a `scoped_session` instead of Flask-SQLAlchemy, apply the session-scoping rules directly — the pattern is the same, the scaffold differs. **Flask 3.1 changed `SERVER_NAME` semantics and added several limit settings** — see item 6; a codebase written against 3.0 has a real gap.

## The Flask footguns AI walks into most

These are the stack-specific cousins of the failure-mode catalog. Each is something Flask code gets wrong even when the *logic* is right.

### 1. App and request context — the three magic proxies and where they die

`current_app`, `request`, and `g` are context-local proxies that only resolve inside an active application or request context. Using them at import time, in a module-level expression, or in a background thread/task raises `RuntimeError: Working outside of application context` or `RuntimeError: Working outside of request context`. AI writes context-access code at class-definition time or in a `threading.Thread` target and ships a crash that never surfaces in a single-threaded dev run.

**`current_app`** — resolves to the app bound on the current context. Access it inside a view function, a CLI command, or after explicitly pushing a context.

**`g`** — scratch space on the *application* context, which is pushed and popped around each request. It is torn down at the end of each request. Do not use `g` as an inter-request cache or to share state between requests — each request gets a fresh `g`, and each worker process has its own.

**Background threads:** any thread spawned from a request has no context. Push one explicitly:
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
If the thread genuinely needs the *request* (not just the app), `flask.copy_current_request_context` wraps the callable — but a thread that outlives the response and still reads `request` is a design smell; copy the values you need instead.

**Celery/RQ tasks:** same issue — tasks run in a worker process with no Flask context unless the task sets one up. The standard pattern is a `ContextTask` base class that pushes `app.app_context()` before calling the task body.

### 2. Unopinionated by design — validation and authorization are easy to forget

Flask has no built-in request validation, no enforced authentication scheme, and no object-level permission model. AI builds a route that parses `request.json` directly and applies business logic, forgetting both layers. The result is a service that accepts malformed input and happily returns another user's data.

**Input validation:** use Pydantic (v2 `model_validate`) or marshmallow to parse and validate request bodies before they touch the business layer. Never use raw `request.json.get("field")` for fields that have type or range constraints. Note `request.get_json()` raises a 415 if the content type isn't JSON and a 400 on malformed JSON — decide which you want rather than letting the default surprise the client.
```python
from pydantic import BaseModel, ConfigDict, ValidationError

class CreateOrderBody(BaseModel):
    model_config = ConfigDict(extra="forbid")   # unknown fields are a client bug, not a silent drop
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
        abort(404)           # 404 not 403 — 403 confirms the row exists
    return order_schema.dump(order)
```
Better still, scope the query so the row is invisible: `db.one_or_404(db.select(Order).filter_by(id=order_id, user_id=current_user.id))`.

### 3. SQLAlchemy session scoping — leaked sessions corrupt state

A `Session` is a unit of work bound to a single connection. Sharing one session across requests or threads corrupts it — you get stale data, `DetachedInstanceError`, or silent cross-request data bleed.

**Flask-SQLAlchemy** handles scoping automatically: `db.session` is a `scoped_session` keyed on the current app context. It is removed in `teardown_appcontext` — you do not need to manage the lifecycle manually. The corollary is that touching `db.session` from a background thread with no app context raises `RuntimeError: Working outside of application context` — it does not quietly hand you a second session. Push a context (`with app.app_context():`) and let it exit so the session is removed, or move the work to a real worker.

**Bare SQLAlchemy:** if you're not using Flask-SQLAlchemy, create a `scoped_session` and register teardown explicitly:
```python
from sqlalchemy.orm import scoped_session, sessionmaker

Session = scoped_session(sessionmaker(bind=engine))

@app.teardown_appcontext
def shutdown_session(exception):
    Session.remove()   # returns the session to the pool and clears the scope
```

**Commit/rollback boundary must be explicit.** Flask-SQLAlchemy does not autocommit. Call `db.session.commit()` when the work should persist; if an exception propagates, call `db.session.rollback()`. Teardown will roll back an unfinished transaction, which is correct — but an explicit rollback in an `except` block is clearer and it is the only way to keep working with the session afterwards.

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

**Pool sizing is per worker process.** Total connections = `gunicorn workers × (pool_size + max_overflow)`. The pool belongs to the Engine, which belongs to the process — threads inside a worker *share* it, they do not each get one, so do not multiply by the thread count. What threads change is contention: more threads than `pool_size + max_overflow` means requests block on `pool_timeout` instead of on the database. This is how a small Flask service exhausts Postgres `max_connections`. Set `SQLALCHEMY_ENGINE_OPTIONS` deliberately, including `pool_pre_ping=True` if anything between you and the database drops idle connections.

### 4. Blueprint organization and the app factory — avoid import-time global state

A module-level `app = Flask(__name__)` and extensions bound directly to it at import time (`db = SQLAlchemy(app)`) create a global singleton that makes testing impossible (you can't swap config between tests), causes circular imports as blueprints try to import from the module that imports them, and builds the connection pool *before* the server forks its workers — which is the fork-safety bug in `mir-backend-python`.

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

Flask runs one request per thread. A route that does a 30-second video encode, a large data export, or a third-party API call with no timeout blocks that worker for the duration, reducing throughput to `workers - 1` for all other requests. Every outbound HTTP call needs an explicit `timeout=` — `requests` has no default and will wait forever.

**Offload to Celery or RQ:**
- The route creates the job record, enqueues the task with an idempotency key, and returns `202 Accepted` with a job ID.
- The worker executes the task; its result is stored in the DB or Redis.
- The client polls or receives a webhook.

```python
@app.post("/exports")
def start_export():
    job = ExportJob(user_id=current_user.id, status="queued")
    db.session.add(job)
    db.session.commit()                       # commit BEFORE enqueue
    run_export.apply_async(args=[job.id], task_id=str(job.id))
    return {"job_id": job.id}, 202
```

Order matters: enqueue after the commit, or the worker can pick the job up and query for a row that doesn't exist yet — and win the race often enough to look intermittent.

**Idempotency:** Celery tasks must be re-delivery safe. Store the outcome keyed on the job ID; a retry that finds a completed record returns without re-executing. This is the at-least-once / idempotency rule from `mir-backend` landing in Flask's Celery integration.

**Flask's async support:** `async def` route handlers work but require the `flask[async]` extra, and Flask is still WSGI. Each async view is run through `asgiref.async_to_sync` in **a fresh event loop for that request**. Consequences: there is no shared loop, so you cannot hold an async connection pool or an `httpx.AsyncClient` across requests; you get correctness and no I/O concurrency; and background tasks created with `create_task` die when the loop closes at the end of the request. True async concurrency means Quart or a different framework. The runtime-level async/sync decision lives in `mir-backend-python`; here the point is only that Flask's `async def` does not give you a free event loop.

### 6. Config in Flask 3.1 — what changed and what to set

Flask 3.1 added several settings that a 3.0-era codebase does not have, and changed one behaviour that silently widens the app's exposure.

| Setting | Default | Set it to | Added |
|---|---|---|---|
| `DEBUG` | `False` | `False` in production, always. See Security. | — |
| `SECRET_KEY` | `None` | long random value from env/secret manager | — |
| `SECRET_KEY_FALLBACKS` | `None` | list of old keys during a rotation, oldest first; remove them after the session lifetime elapses | 3.1 |
| `TRUSTED_HOSTS` | `None` (= all hosts valid) | the exact hostnames, or `.example.com` for a subdomain wildcard | 3.1 |
| `MAX_CONTENT_LENGTH` | `None` (no limit) | a real byte cap; also settable per view via `request.max_content_length` | per-request in 3.1 |
| `MAX_FORM_MEMORY_SIZE` | `500_000` | usually fine; raise deliberately | 3.1 |
| `MAX_FORM_PARTS` | `1_000` | usually fine | 3.1 |
| `SESSION_COOKIE_SECURE` | `False` | `True` | — |
| `SESSION_COOKIE_SAMESITE` | `None` (attribute omitted) | `"Lax"` | — |
| `SESSION_COOKIE_HTTPONLY` | `True` | leave it | — |
| `SESSION_COOKIE_PARTITIONED` | `False` | `True` only if the app is embedded cross-site in an iframe; it implies `SECURE` | 3.1 |

**The behaviour change:** in Flask 3.1, setting `SERVER_NAME` **no longer restricts requests to that domain**, for both `subdomain_matching` and `host_matching`. Code written before 3.1 that relied on `SERVER_NAME` as a Host-header filter now accepts any Host. Use `TRUSTED_HOSTS` — it validates `Request.host` during routing and returns 400 on a mismatch.

```python
app.config.update(
    DEBUG=False,
    SECRET_KEY=os.environ["SECRET_KEY"],
    TRUSTED_HOSTS=["app.example.com"],
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,
    SQLALCHEMY_DATABASE_URI=os.environ["DATABASE_URL"],
)
```

### 7. Migrations via Flask-Migrate / Alembic — same populated-table discipline

Flask-Migrate wraps Alembic. The migration safety rules are identical to the ones in `mir-backend-python-fastapi/references/alembic-migration-safety.md` — read that file rather than reinventing it. The headline: AI writes migrations as if the table is empty; it isn't.

**The specifics that matter most here:**
- **Bound the lock wait.** `op.execute("SET lock_timeout = '3s'")` at the top of any migration touching an existing table. The danger is rarely the statement's own duration — it's that a queued `ACCESS EXCLUSIVE` lock request blocks every subsequent query, including reads.
- **NOT NULL is a three-step change:** `op.add_column(..., nullable=True)` → batched `UPDATE` in a separate data migration → `op.alter_column(..., nullable=False)`. On PG 12+ you can make the last step cheap by first adding `CHECK (col IS NOT NULL) NOT VALID` and validating it.
- **Indexes concurrently, outside a transaction:**
  ```python
  def upgrade():
      with op.get_context().autocommit_block():
          op.create_index("ix_order_status", "order", ["status"], postgresql_concurrently=True)
  ```
  An interrupted `CONCURRENTLY` build leaves an `INVALID` index that writes still maintain and the planner ignores; re-running then fails on the duplicate name. Retry with `DROP INDEX CONCURRENTLY IF EXISTS …`.
- **Bind parameters in data migrations.** `op.execute(f"UPDATE t SET name = '{value}'")` is SQL injection running as the migration user, which usually has the highest privileges in the system. Use `conn.execute(text("… :v"), {"v": value})`.
- **Separate schema and data migrations** into different files.
- **Reversibility:** provide `downgrade()`. If reversal is destructive or impossible, say so in a comment; don't leave a silent no-op.
- **Multiple heads** appear as soon as two branches add a revision from the same `down_revision`. Run `flask db heads` in CI and fail on more than one.
- Run `flask db upgrade` as a **single pre-deploy step**, not from the app's entrypoint — N replicas booting means N concurrent migration runs.

## How this slots into the pipeline

- **Gate 5 (Design):** state the context-push strategy for background work, the session teardown pattern, the app-factory shape, the validation layer (Pydantic/marshmallow), and the connection total (`workers × threads × pool`). Migration plans for populated tables must name `lock_timeout` and the three-step add-nullable → backfill → NOT NULL pattern.
- **Gate 6 (Implementation):** code against the footguns above; confirm `SECRET_KEY` and `DEBUG` come from env; confirm `TRUSTED_HOSTS` and the cookie flags are set; confirm the session is torn down in `teardown_appcontext`; confirm durable work is Celery/RQ and enqueued after commit.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–7 here. The security-reviewer works the Security section below. The migration-reviewer checks each new Alembic revision for NOT NULL transitions, missing `CONCURRENTLY`, missing `lock_timeout`, and missing batched backfills.

## Security

Flask/Werkzeug mechanics. Runtime-level items (unsafe deserialization, archive extraction, SSRF plumbing, packaging supply chain) are in `mir-backend-python`.

**Current advisories**

| Advisory | Affected | Fixed in | What it is |
|---|---|---|---|
| **CVE-2026-27205** (`GHSA-68rp-wp8r-4726`) | Flask ≤ 3.1.2 | **3.1.3** | Operations that read only the session's *keys* (`in`, `len`) didn't mark the session accessed, so Flask omitted `Vary: Cookie`. A shared cache in front of the app can then serve one user's response to another. Anything with a CDN or reverse-proxy cache is exposed. |
| `GHSA-4grg-w6v8-c28g` | Flask 3.1.0 | **3.1.1** | Wrong signing-key selection order when `SECRET_KEY_FALLBACKS` is used — i.e. the key-rotation feature itself. |
| **CVE-2026-21860** | Werkzeug < 3.1.5 | **3.1.5** | Path traversal in `werkzeug.security.safe_join`: Windows device names with extensions or trailing spaces bypassed sanitisation. Windows deployments only; Linux/Unix unaffected. |

Floor your own requirements at `flask>=3.1.3` and `werkzeug>=3.1.5`.

**Debug mode is a remote-code-execution surface**
Werkzeug's debugger exposes an interactive Python console in the browser on an unhandled exception. It is PIN-protected, but the PIN is derived from machine facts (`/proc/self/cgroup`, machine-id, the module path, MAC address) that are often themselves leakable — treat the PIN as a speed bump, not a control. Two separate rules:
- `DEBUG` is `False` in production. Drive it from the environment; better, use `flask run --debug` locally and never set it in code, because Flask's own docs note the config value behaves inconsistently when changed after startup.
- `app.run()` / `werkzeug.serving` is the **development** server. It is not for production regardless of debug mode. Run under gunicorn/uWSGI.

**Secrets**
`SECRET_KEY` signs the session cookie. A weak or committed key lets anyone forge a session and impersonate any user. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`, store in a secret manager or env var, never in a committed `config.py`. Rotate with `SECRET_KEY_FALLBACKS` (Flask 3.1) — put the new key in `SECRET_KEY`, the old one in the fallback list, and drop it after `PERMANENT_SESSION_LIFETIME` has elapsed. Extensions that use `SECRET_KEY` may not support the fallback list yet; check each one.

**The session cookie is signed, not encrypted**
Anything you put in `session` is base64 in the client's browser and readable by them. It is tamper-proof, not secret. Never put an internal user ID you treat as a capability, a role you don't re-check server-side, an email address you promised not to expose, or any token in it. The cookie is also capped around 4 KB — `MAX_COOKIE_SIZE` warns; browsers silently drop.

**CSRF**
Flask ships none. Cookie-based session auth needs it: `flask_wtf.CSRFProtect(app)`, plus `SESSION_COOKIE_SAMESITE="Lax"` and `Secure`. A bearer token in an `Authorization` header does not need CSRF protection — but the common "move the JWT into an httpOnly cookie so JS can't read it" refactor reintroduces the requirement. `@csrf.exempt` on a state-changing route is the footgun; every occurrence needs a written reason and, for webhooks, its own signature check.

**CORS**
Flask ships none; `flask-cors` is the usual add. `CORS(app, origins="*", supports_credentials=True)` reflects the caller's origin, which means any site can make authenticated requests as your user. Enumerate the origins. A regex origin must be anchored — `r"https://.*\.example\.com"` also matches `https://evil.example.com.attacker.net` unless you anchor both ends.

**Server-side template injection**
`render_template_string(user_input)` and `Template(user_input).render()` are remote code execution — Jinja2's sandbox is not enabled by default, and the standard escape reaches `__subclasses__` from any object in scope. Pass user data as a **context variable** to a fixed template file, never as the template itself. A user-controlled *template name* (`render_template(request.args["page"])`) is path traversal into your template directory.

Autoescaping is on for `.html`, `.htm`, `.xml`, `.xhtml` and `.svg` templates, and **on** for `render_template_string` (Flask autoescapes when there is no filename to judge). It is **off** for any other extension — a `.txt` or `.md` template used to build an email or a JSON fragment escapes nothing. `|safe`, `Markup()`, and `{% autoescape false %}` turn it off deliberately. Grep for all of those at review. Autoescaping is also irrelevant to the SSTI above: it escapes the *data*, not the template source.

**Path traversal in file serving and uploads**
- `send_from_directory(dir, filename)` applies `safe_join`; `send_file(f"{dir}/{request.args['f']}")` does not. Use the former, and note the Werkzeug CVE above means `safe_join` itself needed a patch on Windows.
- Never trust `FileStorage.filename` as a filesystem name. Generate your own name and keep the client's as metadata. `werkzeug.utils.secure_filename` strips directory components but also mangles non-ASCII names to nothing — check for an empty result.
- Uploads served back from a directory your app also serves statically turn an uploaded `.html` into stored XSS on your origin. Serve them from a separate domain, or through a view that forces `Content-Disposition: attachment` and a fixed `Content-Type`.
- Set `MAX_CONTENT_LENGTH`. Without it there is no body-size cap at the app level.

**Host header**
Set `TRUSTED_HOSTS` (Flask 3.1). Without it, `request.host`, `url_for(_external=True)`, and any password-reset link built from them follow whatever `Host` the client sent — the classic reset-link poisoning bug. `SERVER_NAME` no longer does this job as of 3.1.

**SQL injection**
`db.session.execute(text(f"SELECT … {user}"))` is injection. Bind: `text("SELECT … :u"), {"u": user}`. ORM `filter()` arguments are parameterized; a column name never can be — map client sort/filter strings through a fixed dict.

**Mass assignment**
`Model(**request.get_json())` and `for k, v in request.json.items(): setattr(obj, k, v)` hand the client every column, including `is_admin` and `user_id`. The allow-list is your Pydantic/marshmallow schema — define one with `extra="forbid"` and assign server-owned fields from the authenticated context.

**Deserialization of session and cache data**
Flask's own session serializer is JSON-based. The risk enters through extensions: a server-side session store or cache backend configured with a serializer that reconstructs arbitrary objects turns "attacker can write to Redis" into remote code execution on the next read. Keep JSON serialization on `flask-session`, `flask-caching`, and the Celery result backend.

**Error and secret leakage**
With `DEBUG=False` and `PROPAGATE_EXCEPTIONS` unset, Flask returns a bare 500 — good. What leaks instead is the log: `app.logger.exception(...)` records the traceback with the DSN and request body. Redact in a logging filter. Do not put `str(exc)` in the response body; return a correlation ID.

## Edit boundary (what belongs here vs. the runtime tier)

**This module holds ONLY Flask library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability)? → **generic core** (`mir-backend`).
- True for every Python framework on CPython (GIL, async-vs-sync, blocking the event loop, fork-safe pools, cold start, asyncio task hygiene, stdlib/packaging security)? → **runtime tier** (`mir-backend-python`). Note: the WSGI-vs-ASGI deployment decision lives there; here we cover only Flask-specific async semantics.
- A mechanical footgun of *this library* (context proxies, session scoping, app factory, `debug=True` RCE, `SECRET_KEY_FALLBACKS`, `TRUSTED_HOSTS`, template injection, Flask-Migrate discipline)? → **here**.
- A *different* framework on Python (FastAPI, Django, Quart) → its own `mir-backend-python-<framework>` module. A *different* runtime → its own tier. Never widen this one.
