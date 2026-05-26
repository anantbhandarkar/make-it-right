---
name: mir-backend-php-laravel
description: "Make It Right (Laravel module). Laravel 10/11 + Eloquent ORM + MySQL/PostgreSQL + Redis + Laravel Queues specific reliability augmentation. Use alongside mir-backend and mir-backend-php when the target stack is Laravel — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: Eloquent N+1 queries, mass assignment via $fillable/$guarded, synchronous vs. queued work, DB::transaction() boundaries and afterCommit event semantics, Octane state bleed from singletons surviving requests, and migration safety on populated tables. TRIGGER only when the PHP backend stack is Laravel — building, reviewing, or debugging a Laravel controller, Eloquent model, Job, migration, or middleware. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-php (Zend Engine runtime concerns: shared-nothing lifecycle, FPM worker model, Octane state bleed, opcache, error model); this module only adds Laravel/Eloquent library mechanics. SKIP for Symfony, WordPress, Slim, or any non-Laravel PHP stack (those get their own mir-backend-php-<framework> module), and for non-PHP runtimes."
trigger: /mir-backend-php-laravel
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-php-laravel · Make It Right (Laravel)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-php` (Zend Engine runtime model) → **this** (Laravel/Eloquent library mechanics). Run the gates first; load the PHP runtime tier for the lifecycle/process model; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (shared-nothing lifecycle, FPM sizing, Octane state bleed, opcache, pconnect, error model) live in `mir-backend-php` — not here.**

**Stack assumed:** Laravel 10/11 · Eloquent ORM · MySQL or PostgreSQL · Redis · Laravel Queues (Horizon or plain `queue:work`). If the project uses a different queue driver or a raw PDO layer, note the divergence before applying these.

## The Laravel footguns AI walks into most

### 1. Eloquent N+1 — the silent query multiplier

Eloquent lazy-loads relationships on first access. Inside a loop over a collection, accessing `$order->items` for each order fires a separate SELECT per iteration — N orders produce N+1 queries total.

- **Fix:** eager-load with `with()` in the initial query. Use `Model::preventLazyLoading()` in `AppServiceProvider::boot()` during local/CI environments — it throws an exception the moment a lazy load is attempted, making N+1 impossible to miss before production.

```php
// WRONG — fires 1 + N queries (1 for orders, N for items)
$orders = Order::all();
foreach ($orders as $order) {
    echo $order->items->count(); // lazy load per iteration
}

// RIGHT — 2 queries total (1 for orders, 1 for all related items)
$orders = Order::with('items')->get();

// Dev/CI guard — add to AppServiceProvider::boot()
Model::preventLazyLoading(! app()->isProduction());
```

Also applies to nested relationships — `with('items.product')` eager-loads the chain. Use `withCount()` for aggregate counts without loading the related models.

### 2. Mass assignment — $fillable / $guarded and overposting

`Model::create($request->all())` or `$model->fill($request->all())` passes the entire HTTP payload directly to Eloquent. If `$guarded` is an empty array (`[]`) or the `$fillable` list is too broad, a client can set `is_admin = 1`, `role = "superuser"`, or `stripe_customer_id = "..."` in the request body.

- **Fix (defense in depth):** (a) define explicit `$fillable` lists on every model — only the columns users are permitted to set; (b) validate the request via a Form Request class with explicit field rules before it reaches the model; (c) never reference `$request->all()` directly in a `create`/`fill` call — pass only validated, named fields.

```php
// WRONG — open to overposting; any field in the request body is written
class User extends Model {
    protected $guarded = []; // nothing guarded
}
User::create($request->all()); // client sends is_admin=1 → it sticks

// RIGHT — explicit allowlist + Form Request validation
class User extends Model {
    protected $fillable = ['name', 'email', 'password'];
}

class StoreUserRequest extends FormRequest {
    public function rules(): array {
        return [
            'name'     => ['required', 'string', 'max:255'],
            'email'    => ['required', 'email', 'unique:users'],
            'password' => ['required', 'min:12', 'confirmed'],
        ];
    }
}

// In the controller — only validated fields reach the model
User::create($request->validated());
```

### 3. Queue jobs for heavy / durable work — not inline in the request

Laravel's HTTP workers are sized and budgeted for fast responses. Heavy computation (PDF generation, bulk email, image processing, third-party API calls with retry logic) must not run synchronously inside a controller action. If the process dies or the request times out, the work is lost.

- **Fix:** dispatch a Job. Jobs are durable (stored in Redis/DB queue), retryable (`$tries`, `$backoff`), and monitorable (Horizon). Make handlers **idempotent** — a job can be retried after partial success; ensure re-running it does not double-charge, double-email, or double-insert.

```php
// WRONG — slow/failable work in the request lifecycle
public function store(Request $request): JsonResponse {
    $report = $this->reportService->generate($request->validated()); // 30 s of CPU
    Mail::to($request->user())->send(new ReportReady($report));
    return response()->json(['status' => 'done']);
}

// RIGHT — dispatch, respond immediately, let the worker handle it
public function store(Request $request): JsonResponse {
    GenerateReport::dispatch($request->validated(), $request->user()->id)
                  ->onQueue('reports');
    return response()->json(['status' => 'queued'], 202);
}

class GenerateReport implements ShouldQueue {
    public int $tries = 3;
    public array $backoff = [30, 60, 120];

    public function handle(ReportService $service): void {
        // Idempotent: check if report already exists before generating
        if (Report::where('key', $this->key)->exists()) { return; }
        $service->generate($this->payload, $this->userId);
    }
}
```

### 4. DB::transaction() — boundaries, deadlock retries, and afterCommit

`DB::transaction()` wraps a closure in BEGIN/COMMIT/ROLLBACK. AI frequently gets three things wrong:

**Wrong:** firing irreversible side effects (email, HTTP call, charge) *inside* the transaction — if the commit succeeds but the outer code throws, or you roll back for another reason, the email is already sent.

**Wrong:** forgetting that `DB::transaction()` retries on deadlock by default (`$attempts` param, default 1 — no retry). Increase for write-heavy paths: `DB::transaction(fn() => ..., attempts: 3)`.

**Wrong:** using Model events (like `created`, `updated`) that dispatch jobs or send notifications *before* the transaction commits — the event fires on `save()`, the transaction can still roll back, and the job runs against data that was never committed.

```php
// WRONG — email fires inside the transaction; rollback can't un-send it
DB::transaction(function () use ($order, $payment) {
    $order->markPaid($payment);
    Mail::to($order->user)->send(new PaymentConfirmation($order)); // wrong place
});

// RIGHT — side effects after commit, using afterCommit on the event/job
DB::transaction(function () use ($order, $payment) {
    $order->markPaid($payment);
    SendPaymentConfirmation::dispatch($order)->afterCommit(); // fires only on commit
}, attempts: 3);
```

Use `->afterCommit()` on dispatched jobs to guarantee the job is only queued after the surrounding transaction commits. Use `$this->afterCommit = true` in the Job class itself for a model-event-triggered dispatch.

### 5. Octane state bleed — singletons holding request-scoped data

When running under Laravel Octane (Swoole or RoadRunner), the application container boots once and persists across requests. This is the **framework-specific face** of the runtime-level state bleed footgun (covered in `mir-backend-php`). The Laravel-specific mechanics:

- Container bindings registered as **singletons** that capture `$request`, `$user`, or `$tenantId` in a constructor inject request-1's data into request-2.
- Static properties on service classes are never reset between requests.
- **Fix:** bind request-scoped services as `scoped` (not `singleton`) in `AppServiceProvider` — `app()->scoped(...)` creates a new instance per request lifecycle under Octane. Use `$octane->flush()` / `Octane::flushState()` hooks for explicit resets.

```php
// WRONG — singleton captures first request's authenticated user
$this->app->singleton(CurrentUser::class, function ($app) {
    return new CurrentUser($app->make(Request::class)->user()); // stale after req 1
});

// RIGHT — scoped: new instance per Octane request lifecycle
$this->app->scoped(CurrentUser::class, function ($app) {
    return new CurrentUser($app->make(Request::class)->user());
});
```

Audit every `singleton` binding when adding Octane. Use `php artisan octane:status` and Horizon metrics to spot memory growth indicating leaks.

### 6. Migrations on populated tables — big-table safety

Eloquent migrations run inside a transaction (on MySQL/PostgreSQL). AI writes migrations as if the table is empty — it isn't. Dangerous patterns on tables with millions of rows:

- `$table->index(...)` / `$table->addColumn(...)` — adding an index or a NOT NULL column without a default takes a full table lock in MySQL (use `pt-online-schema-change` or `gh-ost`) and a rewrite in PostgreSQL without `CONCURRENTLY`.
- Dropping or renaming a column while code still references the old name → immediate 500s on deploy.
- Running a data backfill in the migration itself (loops over every row) — blocks the deployment, holds the migration transaction open, and risks timeout on large tables.

**Fix pattern (expand/contract):**
1. Add the new column as nullable (no lock).
2. Deploy code that writes to both old and new columns.
3. Backfill old rows in a separate queued job / `php artisan` command in chunks (never in the migration).
4. Add the NOT NULL constraint + index as a follow-up migration after backfill is complete.
5. Deploy code that only reads the new column; drop the old column in a final migration.

```php
// WRONG — adds NOT NULL column with no default on a 50M-row table → full lock
Schema::table('orders', function (Blueprint $table) {
    $table->string('currency', 3)->after('total'); // MySQL table-copy, minutes of downtime
});

// RIGHT step 1 — nullable first, zero lock
Schema::table('orders', function (Blueprint $table) {
    $table->string('currency', 3)->nullable()->after('total');
});
// Backfill in a Job/artisan command, then follow up with:
// $table->string('currency', 3)->nullable(false)->change();
```

## How this slots into the core pipeline

- **Gate 5 (Design):** state transaction boundaries, identify which jobs are queue-bound, confirm `$fillable` scope and Form Request guards, audit Octane-unsafe singletons if Octane is in the stack.
- **Gate 6 (Implementation):** code with `with()` eager-loading, explicit `$fillable`, Form Request validation, `dispatch()->afterCommit()` for post-commit side effects, `scoped` bindings under Octane.
- **Gate 7 (Review):** reliability-reviewer checks items 1–6 here; migration-reviewer applies the expand/contract discipline to every Schema change on a populated table.

## Edit boundary (what belongs here vs. the core)

**This module holds ONLY Laravel/Eloquent library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability principle)? → **generic core** (`mir-backend`).
- True for every PHP framework on Zend Engine (shared-nothing lifecycle, FPM sizing, Swoole/Octane state bleed at the runtime level, opcache, pconnect, error model)? → **runtime tier** (`mir-backend-php`).
- A mechanical footgun of *this library* (Eloquent N+1, `$fillable`, `DB::transaction` + `afterCommit`, Octane `scoped` bindings, migration lock patterns)? → **here**.
- A *different* PHP framework (Symfony, WordPress) → new `mir-backend-php-<framework>` module. A *different* runtime → its own tier. Never widen this one.
