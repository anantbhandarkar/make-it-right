---
name: mir-backend-python-django
description: "Make It Right (Django module). Django 5 + Django REST Framework specific reliability augmentation. Use alongside the mir-backend skill when the target stack is Django — it carries the mechanical footguns that the framework-agnostic skill deliberately omits: ORM N+1 with select_related/prefetch_related, queryset laziness and caching semantics, migration safety on populated tables (NOT NULL / index locking), transaction.atomic() and on_commit() boundaries, mass assignment through ModelForm and DRF serializers, async views with the Django 4.1+ async ORM, and signal side-effect traps. TRIGGER only when the Python backend stack is Django — building, reviewing, or debugging a Django view, model, serializer, migration, or admin. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-python (CPython runtime concerns: GIL, async/sync, fork-safety, cold start); this module only adds Django/DRF library mechanics. SKIP for FastAPI, Flask, or any non-Django stack (those get their own mir-backend-python-<framework> module), and for non-Python runtimes."
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

**Stack assumed:** Django 5 · Django REST Framework · PostgreSQL (psycopg3 or psycopg2) · Celery (for async work). If the project uses a different DB adapter or a pure-Django (no DRF) API surface, note the divergence before applying these.

## The Django footguns AI walks into most

These are the stack-specific cousins of the failure-mode catalog. Each is something Django/DRF code gets wrong even when the *logic* is right.

### 1. ORM N+1 — the silent query multiplier

Accessing a related object inside a loop fires one query per iteration because the ORM resolves relations lazily by default. AI writes `for order in orders: print(order.user.email)` and ships an N+1 without noticing — the Django ORM makes it invisible until a profiler surfaces it.

**select_related vs prefetch_related:**
- `select_related(*fields)` — for **FK and one-to-one** relations; performs a SQL `JOIN`, one query total. Use for "to-one" traversal depth you already know.
- `prefetch_related(*fields)` — for **many-to-many and reverse FK** (one-to-many) relations; runs a separate `IN` query and stitches in Python. Use for `ManyToManyField`, `related_name` reverse accessors, or deeply nested paths.
- Combine both: `queryset.select_related("user").prefetch_related("tags")`.

**Bound columns:** `only("id", "email")` loads a sparse model (accessing un-fetched fields triggers a lazy query); `defer("body")` is the inverse. `values("id", "email")` returns dicts — no model overhead, no lazy traps, best for read-only serialization.

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

**Detection:** `django-debug-toolbar` (dev), `nplusone` (test-time, raises on N+1 unless explicitly allowed), `django.db.connection.queries` in tests with `assertNumQueries`.

### 2. QuerySet laziness and caching — evaluation is deferred and fragile

A queryset is lazy: it hits the DB only on iteration, `list()`, `len()`, slicing, or explicit evaluation. Once evaluated, the result is cached *on the queryset object* — but re-evaluating the queryset in a new expression re-hits the DB.

AI mistakes:

- **Truthiness test on a queryset:** `if qs:` evaluates the full queryset and caches. Use `.exists()` instead — single `SELECT 1` with a `LIMIT 1`. Similarly, use `.count()` over `len(qs)` when you don't need the objects.
- **Storing a queryset and iterating twice:** the second iteration replays from cache *only if the queryset was already evaluated and the object is the same instance*. If you slice (`qs[:10]`) before evaluating, the slice returns a new queryset — the cache is gone.
- **Slicing hits the DB:** `qs[5:10]` issues `LIMIT 5 OFFSET 5`. It does not re-use a prior full evaluation.
- **Queryset in a `list` vs queryset reference:** convert to `list(qs)` when you need repeated in-Python iteration; keep the queryset reference if you want to chain filters later.

```python
# WRONG — two DB hits
qs = MyModel.objects.filter(active=True)
if qs:                # hit 1: full SELECT
    for obj in qs:   # hit 2: full SELECT again (new expression context)
        ...

# RIGHT
qs = list(MyModel.objects.filter(active=True))   # one hit, cached as a list
if qs:
    for obj in qs:   # no extra queries
        ...

# OR for existence only
if MyModel.objects.filter(active=True).exists():  # SELECT 1 LIMIT 1
    ...
```

### 3. Migrations on populated tables — the big one

AI writes migrations as if the table is empty. On a table with millions of rows, naive schema changes lock the table and block production traffic.

**NOT NULL column with a default — the rewrite lock trap:**
Adding `NOT NULL` with `default=...` in a single migration causes Postgres (< 11) to rewrite every row while holding an `ACCESS EXCLUSIVE` lock. Even on Postgres 11+, Django generates `ALTER TABLE … SET DEFAULT` + `UPDATE` + `SET NOT NULL` in a transaction, which holds the lock for the duration of the backfill.

Safe pattern — three separate migrations:
1. Add the column **nullable** (`null=True`), no default at the DB level: near-instant lock.
2. Backfill in a `RunPython` step, processing in batches to bound lock time:
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
3. Set `NOT NULL` in a third migration (`AlterField` removes `null=True`): lock is brief because no rows are null.

**Indexes on large tables:** `CREATE INDEX` (non-concurrent) holds a `SHARE` lock for the build duration, blocking writes. Use `AddIndexConcurrently` from `django.contrib.postgres.operations` in a migration that sets `atomic = False`:
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

**Schema vs data migrations:** keep them in separate migration files. A data migration holding rows in a transaction can cause the schema lock to accumulate behind it.

**Reversibility:** always write a `reverse_code` for `RunPython` or mark `migrations.RunPython.noop` with a comment explaining why reversal is safe/impossible.

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

**`transaction.on_commit()`** — registers a callback that runs *after the outermost transaction commits successfully*. Use for any side effect that cannot be rolled back (send email, enqueue a Celery task, push a webhook). **Never enqueue tasks inside the atomic block — a rollback does not un-enqueue them.**

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

**`ATOMIC_REQUESTS` tradeoff:** convenient but holds a DB connection per request for the request's lifetime, even for read-only views. At high concurrency this exhausts the connection pool. Selectively opt out with `@transaction.non_atomic_requests` on read-heavy views.

**`select_for_update()`** — issues `SELECT … FOR UPDATE`, locking selected rows until the transaction ends. Only meaningful inside `transaction.atomic()`. Use to prevent lost-update races on a small set of rows; don't lock large result sets.

### 5. Mass assignment and over-exposure — ModelForm and DRF serializers

**ModelForm:** `Meta.fields = "__all__"` binds every model field to the form — an attacker can POST `is_staff=True` or `tenant_id=<other>`. Always enumerate `fields = ["name", "email"]` or use `exclude` defensively.

**DRF serializer:** same trap with `fields = "__all__"`. Enumerate fields explicitly. Mark fields the client must not write as `read_only=True` in `read_only_fields` or as `serializers.ReadOnlyField()`:
```python
class OrderSerializer(serializers.ModelSerializer):
    class Meta:
        model = Order
        fields = ["id", "status", "total", "user_id"]
        read_only_fields = ["id", "user_id"]   # never user-supplied
```

**Response leakage:** `response_model` (FastAPI) has no Django equivalent by default — serializer controls what leaves. Audit every `to_representation` override and every nested serializer for fields that expose internal state (password hashes, internal flags, other users' data).

**Object-level authorization:** DRF `has_object_permission` in a `BasePermission` subclass is distinct from `has_permission`. AI implements authentication and forgets object-level ownership checks → IDOR. Always call `self.check_object_permissions(request, obj)` in `get_object()`, or inherit from `GenericAPIView` which does it automatically.

### 6. Async views (Django 4.1+ async ORM) — sync ORM in an async view raises

Django 4.1+ supports `async def` views and async ORM methods. The pitfall: calling the **synchronous** ORM API (`Model.objects.filter(...).first()`) inside an `async def` view raises `SynchronousOnlyOperation` — Django detects the event loop and refuses to block it.

Use the async ORM counterparts:
```python
# WRONG — raises SynchronousOnlyOperation in an async view
async def my_view(request):
    obj = MyModel.objects.get(pk=1)   # sync — blocks the loop

# RIGHT
async def my_view(request):
    obj = await MyModel.objects.aget(pk=1)
    # or
    obj = await sync_to_async(MyModel.objects.get)(pk=1)  # escape hatch for legacy code
```

Async ORM methods: `aget()`, `acreate()`, `asave()`, `adelete()`, `aupdate()`, `aiterator()`, `aexists()`, `acount()`. Most ORM queryset evaluations have an `a`-prefixed counterpart in Django 4.1+.

**Deployment note:** the runtime-level choice (ASGI server — Daphne, Uvicorn — vs WSGI — gunicorn) lives in `mir-backend-python`. Most Django projects still run on WSGI workers; `async def` views only get true async execution under ASGI. Under WSGI, Django runs async views in a synchronous adapter — you get correctness but no I/O concurrency.

### 7. Signals — implicit side effects and traceability

Django signals (`post_save`, `pre_delete`, etc.) register callbacks that fire implicitly whenever a model is saved or deleted, anywhere in the codebase. AI adds signals to hook in business logic because it feels clean, then that logic becomes invisible to the reader of the view or serializer.

Problems:
- **Implicit execution:** a `post_save` on `Order` fires inside any `save()` call — including tests, management commands, data migrations, and shell operations. Side effects in those contexts are almost always wrong.
- **Transaction coupling:** a signal fires inside the open transaction of the triggering `save()`. Sending an email or enqueueing a task in a `post_save` signal is the same anti-pattern as doing it inside `transaction.atomic()` — use `transaction.on_commit()` inside the signal handler if the side effect must survive a rollback.
- **Hard to trace:** the causal chain from `order.save()` to "email was sent" is invisible unless you know to search for signal receivers.

Prefer explicit calls: a service function that saves the model and then calls the downstream logic directly. Reserve signals for genuine cross-cutting concerns (audit logging, cache invalidation) where the explicitness would require modifying every caller. Keep signal handlers idempotent.

## How this slots into the pipeline

- **Gate 5 (Design):** state ORM access patterns (select_related/prefetch_related), transaction boundaries (`atomic`, `on_commit`), and the serializer field lists. A migration plan for populated tables must name the three-step pattern above.
- **Gate 6 (Implementation):** code against the footguns above; run `assertNumQueries` in tests for any queryset-heavy path; confirm `on_commit` wraps all post-save side effects.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–7 here. The migration-reviewer checks each new migration for NOT NULL columns, missing `CONCURRENTLY`, and missing batched backfills.

## Edit boundary (what belongs here vs. the runtime tier)

**This module holds ONLY Django/DRF library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability)? → **generic core** (`mir-backend`).
- True for every Python framework on CPython (GIL, async-vs-sync, blocking the event loop, fork-safe pools, cold start, asyncio task hygiene)? → **runtime tier** (`mir-backend-python`). Note: the async/ASGI deployment decision lives there; here we only cover the `SynchronousOnlyOperation` Django-specific trap.
- A mechanical footgun of *this library* (ORM N+1, queryset laziness, migration locking, `on_commit`, mass assignment, signal coupling)? → **here**.
- A *different* framework on Python (FastAPI, Flask) → its own `mir-backend-python-<framework>` module. A *different* runtime → its own tier. Never widen this one.
