---
name: mir-backend-python-django
description: "Make It Right (Django module). Django 6.1 / 5.2 LTS + Django REST Framework specific reliability augmentation. Use alongside the mir-backend skill when the target stack is Django — it carries the mechanical footguns that the framework-agnostic skill deliberately omits: ORM N+1 with select_related/prefetch_related and the new QuerySet.fetch_mode() (FETCH_PEERS / FETCH_RAISE), queryset laziness and result caching, migration safety on populated tables (lock_timeout, AddIndexConcurrently, db_default), transaction.atomic() and on_commit() boundaries, mass assignment through ModelForm and DRF serializers, async views and the async ORM (transactions do NOT work in async; CONN_MAX_AGE must be off), the built-in django.tasks background framework added in 6.0, signal side-effect traps, and Django's own 2026 security advisories. TRIGGER only when the Python backend stack is Django — building, reviewing, or debugging a Django view, model, serializer, migration, task, or admin. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-python (CPython runtime concerns: GIL, async/sync, fork-safety, cold start, packaging supply chain); this module only adds Django/DRF library mechanics. SKIP for FastAPI (mir-backend-python-fastapi), Flask (mir-backend-python-flask), any other non-Django stack, and non-Python runtimes."
trigger: /mir-backend-python-django
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-python-django · Make It Right (Django)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-python` (CPython runtime model) → **this** (Django/DRF library mechanics). Run the gates first; load the Python runtime tier for the concurrency/process model; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (GIL, async-vs-sync, blocking the event loop, fork-safe pools, cold start) live in `mir-backend-python` — not here.**

**Stack assumed**, versions verified 13 Aug 2026:

| | Version | Notes |
|---|---|---|
| Current release | **Django 6.1**, released 5 Aug 2026 | Mainstream support to Apr 2027, extended to Dec 2027. Requires **Python 3.12–3.14**. |
| Current LTS | **Django 5.2 LTS** | Extended support to **Apr 2028**. Last line to support Python 3.10/3.11. |
| Previous | Django 6.0 | Mainstream support ended 4 Aug 2026 — security-only now. |
| Next LTS | Django 6.2 LTS, due Apr 2027 | |

Django 6.1 also dropped PostgreSQL < 15, MySQL < 8.4, MariaDB < 10.11, and SQLite < 3.37. Rest of the stack: DRF · PostgreSQL (psycopg 3) · Celery, or the built-in `django.tasks` framework added in 6.0. **Say which Django version the project is on before applying any of this** — 5.2 and 6.1 differ materially in the ORM and in the background-task story.

## The Django footguns AI walks into most

These are the stack-specific cousins of the failure-mode catalog. Each is something Django/DRF code gets wrong even when the *logic* is right.

### 1. ORM N+1 — the silent query multiplier

Accessing a related object inside a loop fires one query per iteration because the ORM resolves relations lazily by default. AI writes `for order in orders: print(order.user.email)` and ships an N+1 without noticing — the Django ORM makes it invisible until a profiler surfaces it.

**select_related vs prefetch_related:**
- `select_related(*fields)` — for **FK and one-to-one** relations; performs a SQL `JOIN`, one query total. Use for "to-one" traversal depth you already know. **In 6.1, calling `select_related()` with no arguments is deprecated** — enumerate the fields.
- `prefetch_related(*fields)` — for **many-to-many and reverse FK** (one-to-many) relations; runs a separate `IN` query and stitches in Python. Use for `ManyToManyField`, `related_name` reverse accessors, or deeply nested paths.
- Combine both: `queryset.select_related("user").prefetch_related("tags")`.

**Bound columns:** `only("id", "email")` loads a sparse model (accessing an un-fetched field triggers a lazy query); `defer("body")` is the inverse. `values("id", "email")` returns dicts — no model overhead, no lazy traps, best for read-only serialization.

```python
# WRONG — N+1
orders = Order.objects.filter(status="pending")
for order in orders:
    send_email(order.user.email)   # query per iteration

# RIGHT
orders = Order.objects.filter(status="pending").select_related("user")
for order in orders:
    send_email(order.user.email)   # no extra queries
```

**New in Django 6.1 — `QuerySet.fetch_mode()`.** This changes what "forgot to eager-load" costs, and it is the first mechanical defence Django ships against this bug.

| Mode (`django.db.models`) | Behaviour on accessing an unfetched FK or deferred field |
|---|---|
| `FETCH_ONE` | Default. One query for the current instance — the classic N+1. |
| `FETCH_PEERS` | Fetches the field for **every instance that came from the same queryset**, `prefetch_related`-style. Turns N+1 into 2 queries. |
| `FETCH_RAISE` | Raises `django.core.exceptions.FieldFetchBlocked`. |

```python
from django.db import models

# Turn accidental lazy loads into a hard failure on a performance-critical path
for book in Book.objects.fetch_mode(models.FETCH_RAISE):
    print(book.author.name)          # FieldFetchBlocked
```

Use `FETCH_RAISE` on hot read paths so the N+1 fails in CI rather than in production; use `FETCH_PEERS` (set on a custom `Manager.get_queryset()`) as the safety net elsewhere. `FETCH_PEERS` is not a substitute for `select_related` — it still costs an extra round trip and it only helps instances from one queryset.

**Detection on 5.2 and earlier:** `django-debug-toolbar` (dev), `nplusone` (test-time, raises on N+1 unless explicitly allowed), `assertNumQueries` in tests.

### 2. QuerySet laziness and caching — know what actually re-queries

A queryset is lazy: it hits the DB only on iteration, `list()`, `len()`, `bool()`, slicing an unevaluated queryset, or explicit evaluation. Once evaluated, the rows are cached **on that queryset object** in `_result_cache`.

What that means, precisely — this is where the common folklore is wrong:

- `if qs:` followed by `for obj in qs:` is **one** query, not two. `__bool__` evaluates and caches; the loop reuses the cache. The real cost is different: `if qs:` fetches **every row and builds every model instance** just to answer "any?". Use `.exists()` — a single `SELECT 1 … LIMIT 1`.
- The cache belongs to the object, not the query. **Any method that returns a new queryset discards it** — `.filter()`, `.exclude()`, `.order_by()`, `.all()`. So `Model.objects.filter(x=1)` written twice is two queries even though the SQL is identical. Assign it to a variable once.
- **Slicing an unevaluated queryset** issues `LIMIT`/`OFFSET` and returns a new queryset. Slicing an **already evaluated** one slices the cached list in Python. Same syntax, different cost.
- `.count()` on an unevaluated queryset issues `SELECT COUNT(*)`; on an evaluated one it uses `len(_result_cache)`. So `.count()` when you don't need the rows, `len(qs)` when you already have them.
- `.iterator()` bypasses the cache entirely and streams with a server-side cursor. Right for a million-row export; wrong if you iterate twice, because the second pass re-queries.

```python
# WRONG — two queries, because .filter() builds a new queryset each time
if MyModel.objects.filter(active=True).exists():
    for obj in MyModel.objects.filter(active=True):
        ...

# RIGHT — one queryset object, one query, cached
qs = MyModel.objects.filter(active=True)
for obj in qs:
    ...

# RIGHT — existence only, no rows built
if MyModel.objects.filter(active=True).exists():   # SELECT 1 … LIMIT 1
    ...
```

### 3. Migrations on populated tables — the big one

AI writes migrations as if the table is empty. On a table with millions of rows the danger is not usually the statement's own duration — it is that **an `ACCESS EXCLUSIVE` lock request queues every other query behind it**, including reads. One slow report holds the lock, your `ALTER` waits behind it, and everything else waits behind your `ALTER`.

**Bound the wait first.** In any migration touching an existing table:
```python
operations = [migrations.RunSQL("SET lock_timeout = '3s'", reverse_sql=migrations.RunSQL.noop), ...]
```
If the lock can't be taken in 3 seconds the migration fails and you retry — instead of taking production down while it waits.

**What is and is not expensive on a currently-supported Postgres:**

| Operation | Cost |
|---|---|
| `AddField` with a constant `default=` | The `ADD COLUMN` itself is metadata-only on PG 11+. Brief lock. |
| `AddField(..., db_default=...)` (Django 5.0+) | Same, and the default lands in the **schema**, so old code inserting rows during a rolling deploy still gets a value. Prefer this over `default=` for rolling deploys. |
| `AlterField` making an existing nullable column `NOT NULL` | **Full table scan under lock.** The real hazard. |
| `AddIndex` (plain) | Blocks writes for the whole build. |
| `AlterField` changing the column type | Full table rewrite. |
| `AddConstraint` (unique / check / FK) | Validates every row under lock. |

**Safe pattern for making a column NOT NULL — three separate migrations:**
1. Add the column **nullable** (`null=True`), with `db_default` if old code will insert rows before it deploys.
2. Backfill in a `RunPython` step, in batches to bound transaction size:
   ```python
   def backfill(apps, schema_editor):
       Model = apps.get_model("myapp", "MyModel")
       batch = 1000
       while True:
           ids = list(Model.objects.filter(new_field__isnull=True).values_list("id", flat=True)[:batch])
           if not ids:
               break
           Model.objects.filter(id__in=ids).update(new_field="default_value")
   ```
   Always use the historical model from `apps.get_model(...)`, never the imported one — the imported model is the *current* code's shape, which will not match when the migration replays on an old database.
3. `AlterField` to drop `null=True`. To avoid the scan on PG 12+, first add `CHECK (col IS NOT NULL) NOT VALID` and `VALIDATE CONSTRAINT` it via `migrations.RunSQL` wrapped in `migrations.SeparateDatabaseAndState`, so Django's state matches while the SQL does the cheap path.

**Indexes on large tables:** use `AddIndexConcurrently` (and `RemoveIndexConcurrently`) from `django.contrib.postgres.operations` in a migration with `atomic = False`:
```python
from django.contrib.postgres.operations import AddIndexConcurrently

class Migration(migrations.Migration):
    atomic = False   # required for CONCURRENTLY
    operations = [
        AddIndexConcurrently(
            model_name="order",
            index=models.Index(fields=["status"], name="order_status_idx"),
        ),
    ]
```
An interrupted `CONCURRENTLY` build leaves an `INVALID` index that the planner ignores but writes still maintain, and re-running fails on the duplicate name. The retry is `DROP INDEX CONCURRENTLY IF EXISTS …` then rebuild — put it in the runbook.

**Other rules:**
- Keep schema and data migrations in **separate files**. A data migration holding a long transaction accumulates schema locks behind it.
- Always write `reverse_code` for `RunPython`, or `migrations.RunPython.noop` with a comment explaining why reversal is safe or impossible.
- `DEFAULT_AUTO_FIELD` defaults to `BigAutoField` from Django 6.0 (it was `AutoField`). New models get bigint PKs; existing projects that never set it explicitly will see `makemigrations` propose a change.
- Run migrations as a **single pre-deploy job**, not from the app's entrypoint. N replicas booting at once means N concurrent `migrate` runs racing.

### 4. Transactions — atomic blocks, on_commit, and side effects

Django wraps each request in a transaction only when `ATOMIC_REQUESTS = True`. Without it, you're in autocommit and every `save()` / `update()` commits immediately. AI often forgets to scope transactions explicitly.

**`transaction.atomic()`** — as a decorator or context manager, wraps a block in a `SAVEPOINT` (nested) or `BEGIN` (top-level). If an exception propagates, the whole block rolls back.

```python
from django.db import transaction

@transaction.atomic
def transfer(from_account, to_account, amount):
    from_account.balance -= amount
    from_account.save()
    to_account.balance += amount
    to_account.save()   # if this raises, both saves roll back
```

**`transaction.on_commit()`** — registers a callback that runs *after the outermost transaction commits successfully*. Use for any side effect that cannot be rolled back (send email, enqueue a task, push a webhook). **Never enqueue tasks inside the atomic block — a rollback does not un-enqueue them.**

```python
# WRONG — task fires even on rollback
with transaction.atomic():
    order.save()
    send_receipt_task.delay(order.id)   # may run even if save rolls back

# RIGHT
with transaction.atomic():
    order.save()
    transaction.on_commit(lambda: send_receipt_task.delay(order.id))
```

Two traps: a lambda closing over a loop variable captures the last value — use `functools.partial`. And `on_commit` callbacks **do not run under `TestCase`**, because the test wraps everything in a transaction that is rolled back. Wrap the code under test in the context manager — `with self.captureOnCommitCallbacks(execute=True) as callbacks:` — and assert on `callbacks`, or use `TransactionTestCase`. Calling `self.captureOnCommitCallbacks(execute=True)` as a bare statement builds a context manager and runs nothing. Tests that "pass" while the callback never ran are a standard false green.

**`ATOMIC_REQUESTS` tradeoff:** convenient but holds a DB connection per request for the request's lifetime, even for read-only views. At high concurrency this exhausts the connection pool. Selectively opt out with `@transaction.non_atomic_requests` on read-heavy views.

**`select_for_update()`** — issues `SELECT … FOR UPDATE`, locking selected rows until the transaction ends. Only meaningful inside `transaction.atomic()`. Use to prevent lost-update races on a small set of rows; don't lock large result sets.

### 5. Mass assignment and over-exposure — ModelForm and DRF serializers

**ModelForm:** `Meta.fields = "__all__"` binds every model field to the form — an attacker can POST `is_staff=True` or `tenant_id=<other>`. Always enumerate `fields = ["name", "email"]`.

**DRF serializer:** same trap with `fields = "__all__"`. Enumerate fields explicitly. Mark fields the client must not write as read-only:
```python
class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = ["id", "status", "total", "user_id"]
        read_only_fields = ["id", "user_id"]   # never user-supplied
```

**Response leakage:** the serializer is the only thing controlling what leaves — there is no separate output model. Audit every `to_representation` override and every nested serializer for fields that expose internal state (password hashes, internal flags, another user's data).

**Object-level authorization:** DRF's `has_object_permission` on a `BasePermission` subclass is distinct from `has_permission`. AI implements authentication and forgets object-level ownership → IDOR. Call `self.check_object_permissions(request, obj)` in a custom `get_object()`, or use `GenericAPIView`'s, which does it. And scope `get_queryset()` by the caller: `return Order.objects.filter(user=self.request.user)`. A permission class alone still lets a list endpoint return everyone's rows.

### 6. Async views and the async ORM — and the two things that do not work

Django supports `async def` views on both ASGI and WSGI, and the ORM has `a`-prefixed counterparts: `aget()`, `acreate()`, `asave()`, `adelete()`, `aupdate()`, `aiterator()`, `aexists()`, `acount()`, plus `AsyncPaginator`/`AsyncPage` (added in 6.0).

Calling the **synchronous** ORM API inside an `async def` view raises `SynchronousOnlyOperation` — Django detects the running loop and refuses to block it.

```python
# WRONG — raises SynchronousOnlyOperation
async def my_view(request):
    obj = MyModel.objects.get(pk=1)

# RIGHT
async def my_view(request):
    obj = await MyModel.objects.aget(pk=1)
    # or, for legacy sync code:
    obj = await sync_to_async(MyModel.objects.get)(pk=1)
```

Two limits that decide whether async is viable for your view at all:

- **Transactions do not work in async.** There is no `async with transaction.atomic()`. Anything that needs a transaction must live in a sync function called through `sync_to_async(...)`. A state-changing endpoint is therefore usually *not* a good async candidate on Django today. Say so at Gate 5 rather than discovering it at Gate 6.
- **Persistent connections must be off.** Set `CONN_MAX_AGE = 0` in async mode and use the database backend's own pooling (`"OPTIONS": {"pool": True}` with psycopg 3). Django's connection-per-thread model does not map onto an event loop, and a `DatabaseWrapper` created in one thread cannot be used in another — never pass a connection or cursor across a `sync_to_async` boundary; wrap the whole DB-touching operation instead.

`DJANGO_ALLOW_ASYNC_UNSAFE=true` disables the `SynchronousOnlyOperation` guard. It exists for notebooks. Setting it in production removes the only thing stopping concurrent corruption — treat its presence in any deployment config as a Gate 7 blocker.

**Deployment note:** `async def` views only get real concurrency under ASGI. Under WSGI, Django runs them through a synchronous adapter — correct, but with no I/O concurrency and a small per-request adaptation cost. The ASGI-vs-WSGI server choice itself lives in `mir-backend-python`.

### 7. Background work — `django.tasks` (6.0+) vs Celery

Django 6.0 added a first-party background-task API: `@task` from `django.tasks`, `.enqueue()`, and a `TASKS` setting selecting a backend.

```python
from django.tasks import task

@task(priority=2, queue_name="emails")
def email_users(emails, subject, message): ...

email_users.enqueue(emails=[...], subject="…", message="…")
```

What it does **not** give you, and this is the part AI gets wrong:
- **The built-in backends are not production backends.** `ImmediateBackend` runs the task inline, synchronously, right there in the request. `DummyBackend` never runs it at all. Both are for development. Production needs a third-party backend that supplies a worker and a durable queue.
- **No built-in retries.** Retry semantics come from the backend you choose.
- **Arguments and return values must be JSON-serializable.** A `datetime`, a `tuple`, or a model instance raises `TypeError` at enqueue time; a tuple that round-trips becomes a list. Pass IDs, not objects — which is the right pattern anyway, since the object may have changed by the time the worker runs.
- **Enqueue on commit, not inside the transaction.** There is no automatic on-commit behaviour to rely on: `transaction.on_commit(partial(my_task.enqueue, thing_id=1))`. Otherwise a worker can pick the job up before the row it needs is committed — and win.

If the project is on 5.2 LTS, this API does not exist; Celery/RQ with the same discipline (`on_commit`, idempotent handlers, IDs not objects) is the answer.

### 8. Signals — implicit side effects and traceability

Django signals (`post_save`, `pre_delete`, etc.) register callbacks that fire implicitly whenever a model is saved or deleted, anywhere in the codebase. AI adds signals to hook in business logic because it feels clean, then that logic becomes invisible to the reader of the view or serializer.

Problems:
- **Implicit execution:** a `post_save` on `Order` fires inside any `save()` call — including tests, management commands, data migrations, and shell operations. Side effects in those contexts are almost always wrong.
- **Transaction coupling:** a signal fires inside the open transaction of the triggering `save()`. Sending an email or enqueueing a task in a `post_save` handler is the same anti-pattern as doing it inside `transaction.atomic()` — use `transaction.on_commit()` inside the handler.
- **They don't fire for the fast paths.** `QuerySet.update()`, `bulk_create()`, and `bulk_update()` do not send `post_save`. `QuerySet.delete()` does send `post_delete`, but the new `on_delete=models.DB_CASCADE` (Django 6.1) pushes deletion into the database with a SQL `ON DELETE` clause and therefore **does not fire `pre_delete`/`post_delete` at all**. Any invariant you were maintaining in a delete signal silently stops being maintained the day someone switches to `DB_CASCADE` for speed.
- **Hard to trace:** the causal chain from `order.save()` to "email was sent" is invisible unless you know to search for receivers.

Prefer explicit calls: a service function that saves the model and then calls the downstream logic directly. Reserve signals for genuine cross-cutting concerns (audit logging, cache invalidation) where explicitness would require modifying every caller. Keep handlers idempotent.

### 9. Deprecations that will bite an upgrade

Checked against the 6.0 and 6.1 release notes:

| Removed / deprecated | Replacement |
|---|---|
| `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS`, `mail.get_connection()`, the `connection=` and `fail_silently=` arguments (deprecated 6.1) | the `MAILERS` setting and `using="…"` |
| `select_related()` with no arguments (deprecated 6.1) | enumerate the relations |
| `values_list(flat=True)` with no field name (deprecated 6.1) | name the field |
| `QuerySet.first()` / `.last()` auto-ordering by PK when ordering was cleared (changed 6.1) | order explicitly — results silently changed |
| `Field.pre_save()` may now be called more than once (6.0) | make it idempotent |
| `cx_Oracle`, `FORMS_URLFIELD_ASSUME_HTTPS`, `ModelAdmin.lookup_allowed()` without `request` (removed 6.0) | — |
| `urlize`/`urlizetrunc` default protocol moves HTTP → HTTPS in 7.0 | opt in now with `URLIZE_ASSUME_HTTPS` |

## How this slots into the pipeline

- **Gate 5 (Design):** state ORM access patterns (`select_related`/`prefetch_related`/`fetch_mode`), transaction boundaries (`atomic`, `on_commit`), the serializer field lists, and — if any endpoint is `async def` — how it does without transactions. A migration plan for populated tables must name `lock_timeout`, the three-step NOT NULL pattern, and `AddIndexConcurrently`.
- **Gate 6 (Implementation):** code against the footguns above; `assertNumQueries` on every queryset-heavy path; confirm `on_commit` wraps all post-save side effects and that tests use `captureOnCommitCallbacks`.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–9 here. The security-reviewer works the Security section below and runs `manage.py check --deploy`. The migration-reviewer checks each new migration for NOT NULL transitions, missing `CONCURRENTLY`, missing `lock_timeout`, and missing batched backfills.

## Security

Django/DRF mechanics. Runtime-level items (unsafe deserialization, archive extraction, SSRF plumbing, packaging supply chain) are in `mir-backend-python`.

**Run the built-in check.** `python manage.py check --deploy` is a real gate, not a formality — it catches `DEBUG`, a weak `SECRET_KEY`, empty `ALLOWED_HOSTS`, and the cookie/HSTS/SSL flags below. Wire it into CI and fail the build on findings. It does **not** check CSP, `SameSite`, whether the secret came from a manager rather than a committed file, CORS, proxy trust, or any authorization question — a clean `check --deploy` is not a clean security review.

**Settings that ship insecure by default from `startproject`**

| Setting | `startproject` default | Production value |
|---|---|---|
| `DEBUG` | `True` | `False`. With `True`, an unhandled exception returns a page containing your settings, SQL, and local variables. |
| `SECRET_KEY` | a literal string in `settings.py` | from env/secret manager. It signs sessions, password-reset tokens, and everything using `django.core.signing` — a leaked key is account takeover. Rotate via `SECRET_KEY_FALLBACKS`. |
| `ALLOWED_HOSTS` | `[]` | the exact hostnames. Empty fails **closed** under `DEBUG=False` (every request 400s) and **open-ish** under `DEBUG=True` (localhost variants allowed) — so it looks fine in dev and takes the site down on deploy. Set it explicitly. |
| `SESSION_COOKIE_SECURE` | `False` | `True` |
| `CSRF_COOKIE_SECURE` | `False` | `True` |
| `SECURE_SSL_REDIRECT` | `False` | `True` (behind a proxy, together with `SECURE_PROXY_SSL_HEADER`) |
| `SECURE_HSTS_SECONDS` | `0` | a real value, plus `SECURE_HSTS_INCLUDE_SUBDOMAINS` |
| `SECURE_CSP` (6.0+) | unset — no CSP header | a policy using `django.utils.csp.CSP` constants, via `django.middleware.csp.ContentSecurityPolicyMiddleware`. Roll out with `SECURE_CSP_REPORT_ONLY` first. |
| `SESSION_COOKIE_SAMESITE` / `CSRF_COOKIE_SAMESITE` | `"Lax"` | keep `Lax`; only loosen to `"None"` with a written reason, and never without `Secure`. |

**Current advisories on the default path**

| Advisory | Affected | Fixed in | What it is |
|---|---|---|---|
| **CVE-2026-15307** | 6.1 prereleases, ≥6.0 <6.0.8, ≥5.2 <5.2.17 | **6.1 / 6.0.8 / 5.2.17** (4 Aug 2026) | Server-side file write and request forgery via spatial lookups (GIS). |
| **CVE-2026-15337** | same | same | DoS in `django.utils.translation.check_for_language()` with many long language codes. |
| **CVE-2026-15830** | same | same | DoS via nested geometry collections. |
| **CVE-2026-15920** | same | same | Stored XSS: the admin rendered `URLField` values as links without validating the scheme. Now validated with `URLValidator` and shown as plain text if it fails. |
| **CVE-2026-1207** | ≥6.0 <6.0.2, ≥5.2 <5.2.11, ≥4.2 <4.2.28 | **6.0.2 / 5.2.11 / 4.2.28** (3 Feb 2026) | SQL injection on PostGIS: untrusted data used as a raster band index. |
| **CVE-2026-1287** | same | same | SQL injection via control characters in **column aliases** — a crafted dict expanded into `annotate()`/`aggregate()` alongside a `FilteredRelation`. |
| **CVE-2026-1312** | same | same | SQL injection via `QuerySet.order_by()`: an alias **containing periods**, introduced through a `FilteredRelation` and reused via dict expansion. Narrower than "the client picks the sort column" — but allow-list sort keys anyway, on every version. |
| **CVE-2025-14550** | same | same | Repeated HTTP headers under ASGI cause super-linear work — resource exhaustion. |
| **CVE-2026-1285** | same | same | `Truncator.chars()`/`.words()` go quadratic on many unmatched HTML end tags. |
| **CVE-2025-13473** | same | same | Username enumeration by timing under mod_wsgi. |
| **CVE-2026-5766** | ≥6.0 <6.0.5, ≥5.2 <5.2.14 | 6.0.5 / 5.2.14 | Under ASGI, a missing or understated `Content-Length` bypasses `FILE_UPLOAD_MAX_MEMORY_SIZE`, so an oversized upload is buffered in memory. Unauthenticated. |

**Django 4.2 LTS itself went out of extended support on 7 April 2026** and gets nothing further — 4.2.28 was among its last security releases. As of 13 Aug 2026 the supported lines are **6.1, 6.0 and 5.2 LTS**. The Django security team's own observation in 2026 is that new CVEs are mostly variations on previous ones — which means the mitigation is "be on a supported release and upgrade promptly", not "audit for this specific bug".

**SQL injection — where it actually happens in Django**
The ORM parameterizes values. Identifiers are the hole, and two 2026 CVEs are exactly that:
- `order_by(request.GET["sort"])` — user-controlled ordering. CVE-2026-1312 made this exploitable through `FilteredRelation` on unpatched versions, but the rule holds on every version: map the client's string through a fixed dict to a real field name; never pass it through.
- `annotate(**{user_alias: ...})` — user-controlled aliases expanded from a client dict (CVE-2026-1287).
- `.raw()`, `.extra()`, `RawSQL()`, and `connection.cursor().execute(f"…")`. Use `params=[...]`; string-formatting into any of them is injection.
- `filter(**request.GET.dict())` hands the client the lookup language, including `__in`, relation traversal into other tables, and `password__startswith` for a character-by-character oracle. Allow-list the filter keys.

**Object-level authorization (IDOR/BOLA)**
`IsAuthenticated` says *who*, never *what they may touch*. `get_object_or_404(Order, pk=pk)` with a valid token is the canonical Django IDOR. Scope the queryset by the caller (`Order.objects.filter(user=request.user)`) so the row simply isn't visible, and implement `has_object_permission` for anything the queryset can't express. Do the same in the admin — a staff user with `view_order` sees every tenant unless `ModelAdmin.get_queryset()` filters.

**Mass assignment**
The allow-list mechanisms Django gives you are `Meta.fields` (enumerate) and `read_only_fields`. `fields = "__all__"` on a `ModelForm` or `ModelSerializer` is the vulnerability. `Model.objects.create(**request.data)` and `setattr` loops over client keys are the same bug without the framework.

**CSRF**
- `CsrfViewMiddleware` protects session-cookie auth. **DRF's `SessionAuthentication` enforces CSRF; `TokenAuthentication` and JWT-in-a-header do not need it** — but the moment you move a token into a cookie, you need it again.
- `@csrf_exempt` on a state-changing view is the footgun. Grep for it at review; every occurrence needs a written reason (usually "this is a signed webhook", which then needs its own signature check).
- `CSRF_TRUSTED_ORIGINS` entries must include the scheme (`https://app.example.com`). Bare hostnames are ignored, and the resulting failures push people to `@csrf_exempt`.

**CORS**
Django has no CORS support built in; the near-universal `django-cors-headers` does. `CORS_ALLOW_ALL_ORIGINS = True` together with `CORS_ALLOW_CREDENTIALS = True` reflects the caller's origin and lets any site make authenticated requests as your user. Enumerate `CORS_ALLOWED_ORIGINS`. `CORS_ALLOWED_ORIGIN_REGEXES` must be anchored — `r"^https://.*\.example\.com$"` still matches `https://evil.example.com`.

**Path traversal in file serving and uploads**
- `django.views.static.serve` is documented as unsuitable for production. Never route `MEDIA_URL` through it.
- A `FileField` with `upload_to` derived from user input, or `open(os.path.join(MEDIA_ROOT, request.GET["f"]))`, is traversal. Generate storage names server-side; keep the client's filename as metadata only.
- Uploaded files are served from `MEDIA_ROOT`. If that directory is web-reachable and you accept arbitrary content types, an uploaded `.html` becomes stored XSS on your origin. Serve user uploads from a separate domain or through a view that sets `Content-Disposition: attachment` and a fixed `Content-Type`.
- `FILE_UPLOAD_MAX_MEMORY_SIZE` and `DATA_UPLOAD_MAX_MEMORY_SIZE` are the size knobs; `DATA_UPLOAD_MAX_NUMBER_FIELDS` and `DATA_UPLOAD_MAX_NUMBER_FILES` are the count knobs. CVE-2026-5766 above is the reason to check them against your Django version under ASGI.

**SSRF**
`URLValidator` checks *shape*, not destination — it happily accepts `http://169.254.169.254/`. Any feature that fetches a user-supplied URL (avatar import, webhook test, "preview this link") needs the address-level checks in `mir-backend-python`. Django itself does no outbound fetching.

**Secret and error leakage**
- `DEBUG=True` in production returns settings, SQL, and local variables in the error page. That is the single highest-impact Django misconfiguration.
- Even with `DEBUG=False`, the traceback goes to `ADMINS` by email and to your logger with local variables attached. Decorate with `@sensitive_variables("password", "token")` and `@sensitive_post_parameters("password")`, and set `DEFAULT_EXCEPTION_REPORTER_FILTER` — otherwise credentials land in the error mail.
- DRF's default exception handler returns `exc.detail` verbatim. Built-in field errors are messages keyed by field name, not the submitted value — but several field types *do* interpolate the input (`ChoiceField`: `"{input}" is not a valid choice`), and any custom validator that formats the value into its message ends up in the response body. On login and token endpoints, audit every validator on the path for that.

**Deserialization**
Django removed its code-executing session serializer in 4.1; `JSONSerializer` is the default. Don't reintroduce the risk through a custom `SESSION_SERIALIZER` or a cache backend that serializes arbitrary objects — a cache an attacker can write to becomes remote code execution on read. `django.core.signing.loads` is safe against tampering only while `SECRET_KEY` is secret.

**Admin**
`/admin/` on a public hostname is a credential-stuffing target with a known URL and known error strings. Move the path, restrict by network, and require MFA. `ModelAdmin.get_queryset()` and `has_change_permission(obj=...)` are where multi-tenant isolation has to be repeated — the admin does not inherit your API's permission classes.

## Edit boundary (what belongs here vs. the runtime tier)

**This module holds ONLY Django/DRF library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability)? → **generic core** (`mir-backend`).
- True for every Python framework on CPython (GIL, async-vs-sync, blocking the event loop, fork-safe pools, cold start, asyncio task hygiene, stdlib/packaging security)? → **runtime tier** (`mir-backend-python`). Note: the ASGI-vs-WSGI server choice lives there; here we cover only the Django-specific consequences (`SynchronousOnlyOperation`, no async transactions, `CONN_MAX_AGE`).
- A mechanical footgun of *this library* (ORM N+1, `fetch_mode`, queryset caching, migration locking, `on_commit`, `django.tasks`, mass assignment, signal coupling, Django settings)? → **here**.
- A *different* framework on Python (FastAPI, Flask) → its own `mir-backend-python-<framework>` module. A *different* runtime → its own tier. Never widen this one.
