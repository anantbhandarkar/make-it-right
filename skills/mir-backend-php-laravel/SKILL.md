---
name: mir-backend-php-laravel
description: "Make It Right (Laravel module). Laravel 13 / 12 + Eloquent ORM + MySQL/PostgreSQL + Redis + Laravel Queues + Octane + the Laravel AI SDK — mechanical reliability augmentation. Use alongside mir-backend and mir-backend-php when the target stack is Laravel; it carries the footguns the framework-agnostic tiers deliberately omit: Eloquent N+1 and automatic eager loading, mass assignment via $fillable/$guarded and the forceFill bypass, queued vs. inline work with the Laravel 13 job attributes, DB::transaction() boundaries and afterCommit semantics, migrations that are NOT transactional on MySQL, Octane container/request/config injection bleed, and prompt injection plus tool authorization in the Laravel AI SDK. TRIGGER only when the PHP backend stack is Laravel — building, reviewing, or debugging a Laravel controller, Eloquent model, Job, migration, policy, middleware, or AI agent/tool. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-php (Zend Engine runtime concerns: shared-nothing lifecycle, FPM worker model, worker-runtime state bleed, opcache/JIT, php.ini security, Composer supply chain); this module only adds Laravel/Eloquent library mechanics. SKIP for Symfony/Doctrine/Messenger/API Platform work (that is mir-backend-php-symfony), for WordPress, Slim, or any non-Laravel PHP stack (each gets its own mir-backend-php-<framework> module), and for non-PHP runtimes."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-php` (Zend Engine runtime model) → **this** (Laravel/Eloquent library mechanics). Run the gates first; load the PHP runtime tier for the lifecycle/process model; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (shared-nothing lifecycle, FPM sizing, worker-runtime state bleed, opcache/JIT, php.ini hardening, Composer supply chain) live in `mir-backend-php` — not here.**

**Versions, verified 13 August 2026:**

| Release | Date | PHP | Bug fixes until | Security until |
|---|---|---|---|---|
| Laravel 13 | 17 Mar 2026 | 8.3 – 8.5 | Q3 2027 | 17 Mar 2028 |
| Laravel 12 | 24 Feb 2025 | 8.2 – 8.5 | **13 Aug 2026 — just lapsed** | 24 Feb 2027 |
| Laravel 11 | 12 Mar 2024 | 8.2 – 8.4 | ended | **ended 12 Mar 2026** |
| Laravel 10 | 14 Feb 2023 | 8.1 – 8.3 | ended | ended |

**There is no LTS release.** Laravel has not shipped an LTS since version 6. Every major gets 18 months of bug fixes and 2 years of security fixes. If a project is on Laravel 11 or older it is receiving nothing — that is a Gate 5 risk-register entry, not a "later" item.

**Stack assumed:** Laravel 13 (or 12) · Eloquent ORM · MySQL or PostgreSQL · Redis · Laravel Queues (Horizon or plain `queue:work`). Note that some Laravel 13.3+ patch versions pull Symfony 8 components that require PHP 8.4, so a project pinned to PHP 8.3 may not be able to take the newest 13.x patch. If the project uses a different queue driver or a raw PDO layer, note the divergence before applying these.

## The Laravel footguns AI walks into most

### 1. Eloquent N+1 — the silent query multiplier

Eloquent lazy-loads relationships on first access. Inside a loop over a collection, accessing `$order->items` for each order fires a separate SELECT per iteration — N orders produce N+1 queries.

- **Fix:** eager-load with `with()` in the initial query. Turn on `Model::preventLazyLoading()` outside production — it throws `LazyLoadingViolationException` the moment a lazy load is attempted, so N+1 fails in CI instead of in prod.
- `Model::shouldBeStrict()` is the one-liner: it calls `preventLazyLoading()`, `preventSilentlyDiscardingAttributes()`, and `preventAccessingMissingAttributes()`. The last two are correctness checks and are safe to leave on everywhere; the lazy-loading one is usually scoped to non-production.

```php
// WRONG — fires 1 + N queries (1 for orders, N for items)
$orders = Order::all();
foreach ($orders as $order) {
    echo $order->items->count(); // lazy load per iteration
}

// RIGHT — 2 queries total
$orders = Order::with('items')->get();

// Dev/CI guard — AppServiceProvider::boot()
Model::shouldBeStrict(! app()->isProduction());
Model::preventSilentlyDiscardingAttributes();   // keep this one on in prod too
```

Nested relationships: `with('items.product')`. Aggregate counts without hydrating the relation: `withCount()`.

**Automatic eager loading (added in Laravel 12.0.8, still labelled beta):** `Model::automaticallyEagerLoadRelationships()` makes Eloquent batch-load a relationship the first time any model in a loaded collection touches it. It removes most manual `with()` calls, and it no longer conflicts with `preventLazyLoading` — accessing a relation under automatic eager loading does not raise the violation. Two cautions: it only helps when the models are in one loaded collection (a relation accessed on a single model still fires its own query), and turning it on globally changes query shapes across the whole app, so verify the query log before shipping it to a hot path.

### 2. Mass assignment — $fillable / $guarded, and the bypasses

`Model::create($request->all())` or `$model->fill($request->all())` passes the entire HTTP payload to Eloquent.

Get the default right, because AI states it wrong in both directions. The base `Illuminate\Database\Eloquent\Model` ships `$fillable = []` and `$guarded = ['*']`. That combination is "totally guarded": `fill()` throws `MassAssignmentException` rather than writing anything. **Laravel is closed by default.** The dangerous states are all things a developer opts into:

| Pattern | Effect |
|---|---|
| `protected $guarded = [];` | every column is mass-assignable — this is the actual overposting hole, and it is common because tutorials recommend it |
| `Model::unguard()` in a service provider | same, globally, for every model |
| `$model->forceFill($request->all())` / `forceCreate()` | bypasses `$fillable` **and** `$guarded` entirely, including `id` |
| `$fillable` that includes `role`, `is_admin`, `tenant_id`, `user_id`, `stripe_customer_id` | the allow-list itself is the hole |

Non-fillable attributes are **silently discarded** by default — no error, no log, the field just does not save. That hides bugs. `Model::preventSilentlyDiscardingAttributes()` turns it into an exception.

```php
// WRONG — open to overposting; any field in the body is written
class User extends Model {
    protected $guarded = []; // nothing guarded
}
User::create($request->all()); // client sends is_admin=1 → it sticks

// RIGHT — explicit allow-list + Form Request validation
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

// Controller — only validated fields reach the model
User::create($request->validated());
```

`$request->validated()` returns only fields that had rules. `$request->all()` returns everything the client sent. Never pass `all()` to `create`, `fill`, `update`, or `forceFill`.

### 3. Queue jobs for heavy / durable work — not inline in the request

Laravel's HTTP workers are budgeted for fast responses. Heavy computation (PDF generation, bulk email, image processing, third-party API calls with retries) must not run synchronously in a controller action. If the process dies or the request times out, the work is lost.

- **Fix:** dispatch a Job. Jobs are durable (Redis/DB queue), retryable, and monitorable (Horizon). Make handlers **idempotent** — a job can be retried after partial success; re-running must not double-charge, double-email, or double-insert.
- Laravel 13 adds attribute equivalents for the job control properties: `#[Tries]`, `#[Backoff]`, `#[Timeout]`, `#[FailOnTimeout]`. Use whichever form the codebase already uses; do not mix.
- `Queue::route(ProcessPodcast::class, connection: 'redis', queue: 'podcasts')` (Laravel 13) centralises which queue a job class lands on, instead of scattering `onQueue()` at every dispatch site.
- `ShouldBeUnique` (with `uniqueId()` and `uniqueFor()`) prevents a duplicate job being queued. It is a de-duplication convenience backed by a cache lock — it is **not** an idempotency guarantee. Still write the handler to be safe on redelivery.

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
        // Idempotent: the key is derived from the business event, not from dispatch time
        if (Report::where('key', $this->key)->exists()) { return; }
        $service->generate($this->payload, $this->userId);
    }
}
```

Worker restarts are part of this: `queue:restart` after every deploy, and `ext-pcntl` must be present or `SIGTERM` kills jobs mid-flight (see `mir-backend-php`).

### 4. DB::transaction() — boundaries, deadlock retries, and afterCommit

`DB::transaction()` wraps a closure in BEGIN/COMMIT/ROLLBACK. AI gets three things wrong:

**Wrong:** firing irreversible side effects (email, HTTP call, charge) *inside* the transaction. If the commit succeeds but the outer code throws, or you roll back for another reason, the email is already sent.

**Wrong:** assuming deadlock retries happen. The `$attempts` parameter defaults to 1 — **no retry**. Raise it on write-heavy paths: `DB::transaction(fn() => ..., attempts: 3)`. Retries re-run the whole closure, so the closure must be safe to run twice.

**Wrong:** model events (`created`, `updated`) that dispatch jobs or send notifications. The event fires on `save()`, inside the transaction. The transaction can still roll back, and the job then runs against data that was never committed. It can also run *before* commit and read stale rows.

```php
// WRONG — email fires inside the transaction; rollback cannot un-send it
DB::transaction(function () use ($order, $payment) {
    $order->markPaid($payment);
    Mail::to($order->user)->send(new PaymentConfirmation($order)); // wrong place
});

// RIGHT — side effects after commit
DB::transaction(function () use ($order, $payment) {
    $order->markPaid($payment);
    SendPaymentConfirmation::dispatch($order)->afterCommit(); // queued only on commit
}, attempts: 3);
```

`->afterCommit()` on a dispatch, `$afterCommit = true` on the Job class, or `DB::afterCommit(fn () => ...)` for a plain callback. For state transitions under concurrency, prefer a conditional UPDATE and check the affected-row count over read-modify-write:

```php
$changed = Order::where('id', $id)->where('status', 'PENDING')
                ->update(['status' => 'PAID']);
if ($changed === 0) { /* someone else already transitioned it */ }
```

### 5. Octane — container, request, and config injection bleed

Under Laravel Octane (FrankenPHP, Swoole, Open Swoole, or RoadRunner) the application boots once and stays in memory. Service providers' `register` and `boot` run once, at worker boot. This is the Laravel face of the runtime-level state bleed footgun in `mir-backend-php`. Octane resets first-party framework state between requests; it does not know how to reset yours.

The three specific traps named in Laravel's own Octane documentation:

| Injected into a `singleton` constructor | What goes wrong |
|---|---|
| the container (`Application $app`) | the service holds the container as it existed at boot — later bindings and per-request bindings are missing |
| the request (`$app['request']`) | headers, input, query string, and the authenticated user are all frozen at request 1 for every subsequent request |
| the config repository (`$app->make('config')`) | config changes between requests are invisible to the service |

Fixes, in the order the docs recommend:

1. **Pass what you need at call time** — `$service->method($request->input('name'))`. Best option; the object holds no request state at all.
2. **Inject a resolver closure** — `new Service(fn () => $app['request'])` or `fn () => Container::getInstance())`. The globals `app()`, `request()`, and `config()` always return the current instance and are safe.
3. **Stop binding it as a singleton** — plain `bind()` gets a fresh instance per resolution. `scoped()` gets one instance per Octane request / queue job lifecycle, which is what "one per request" usually means. **`scoped` only holds if nothing long-lived captures it**: the moment a `singleton` constructor-injects a scoped service, the singleton pins request 1's copy and the scoping is defeated. Audit what injects what, not just how each binding is declared.

```php
// WRONG — the singleton captures request 1's authenticated user forever
$this->app->singleton(CurrentUser::class, fn ($app) => new CurrentUser($app['request']->user()));

// RIGHT — scoped: one instance per Octane request lifecycle
$this->app->scoped(CurrentUser::class, fn ($app) => new CurrentUser($app['request']->user()));

// STILL WRONG — `scoped` does not protect a singleton that captured it.
// This singleton is built once and holds request 1's CurrentUser forever.
$this->app->singleton(Reporter::class, fn ($app) => new Reporter($app->make(CurrentUser::class)));

// ALSO RIGHT — no stored state at all
class OrderService {
    public function place(Cart $cart, User $actor): Order { /* pure */ }
}
```

Static arrays leak. `Service::$data[] = ...` in a controller grows the worker heap on every request until the worker dies. Octane gracefully recycles a worker after 500 requests by default (`--max-requests`); that bounds the damage, it does not fix the leak. Deploy with `octane:reload` so workers pick up new code — without it the old code stays resident. Check `octane:status`. `Octane::concurrently()`, ticks, the Octane cache driver, and Swoole tables **require Swoole**; they are unavailable on FrankenPHP and RoadRunner.

### 6. Migrations on populated tables — and MySQL has no transactional DDL

AI writes migrations as if the table is empty. It isn't.

**Correct the common assumption first:** Laravel wraps a migration in a transaction only when the database grammar supports transactional schema changes. **PostgreSQL does. MySQL and MariaDB do not** — DDL causes an implicit commit, so a migration that fails halfway on MySQL leaves the schema partially changed and cannot be rolled back. Plan every MySQL migration to be resumable and to make one change at a time.

Dangerous patterns on tables with millions of rows:

- **Adding an index.** A plain index build blocks writes for the whole build. On PostgreSQL use `DB::statement('CREATE INDEX CONCURRENTLY ...')` in a migration marked `public $withinTransaction = false;` (`CONCURRENTLY` cannot run inside a transaction). On MySQL use `pt-online-schema-change` or `gh-ost`.
- **Adding a `NOT NULL` column with no default.** This is a *different* failure and `CONCURRENTLY` does nothing for it: on PostgreSQL the statement simply **fails** on a populated table (there is no value for the existing rows), and on MySQL it rebuilds or locks the table. Add nullable, backfill, then enforce `NOT NULL` — the sequence below.
- Dropping or renaming a column while deployed code still references the old name → 500s during the rolling deploy window.
- Running a data backfill in the migration itself — it blocks the deploy, holds the migration open, and times out on large tables.

**Fix pattern (expand/contract):**
1. Add the new column as nullable. Cheap, but not free: it still takes a metadata/exclusive lock for an instant, so a long-running transaction on that table makes the DDL wait and every query behind it queue. Bound the wait (`SET lock_timeout`) rather than assuming "instant" means "safe".
2. Deploy code that writes both old and new columns.
3. Backfill in a queued job or an artisan command, in chunks, throttled.
4. Add `NOT NULL` and the index in a follow-up migration after the backfill completes.
5. Deploy code that reads only the new column; drop the old column last.

```php
// WRONG — NOT NULL column with no default on a 50M-row table
Schema::table('orders', function (Blueprint $table) {
    $table->string('currency', 3)->after('total'); // table copy, minutes of lock
});

// RIGHT step 1 — nullable first
Schema::table('orders', function (Blueprint $table) {
    $table->string('currency', 3)->nullable()->after('total');
});
// Backfill in a Job/command, then: $table->string('currency', 3)->nullable(false)->change();
```

### 7. Laravel AI SDK — the tool boundary is the control, not the prompt

Laravel 13 ships a first-party AI SDK (`Laravel\Ai`) with text generation, embeddings, images, audio, and **tool-calling agents**. Agents make a new class of mistake possible: the model decides which of your tools to call, with arguments it chose, based on text that may have come from an attacker (a support ticket, an uploaded document, a scraped page).

- **You cannot fix this with prompt wording.** To the model, your system instructions and a hostile support ticket are the same kind of text. Instructions that say "never reveal other users' data" are not a control.
- **Scope every tool to the authenticated user in the constructor**, from `auth()->user()` on the server — never from a parameter the model supplies. If the tool takes a `userId` argument, the model can change it.
- **Allow-list what a tool can touch:** explicit column list, explicit operator list, a read-only database connection for query tools. Laravel's own guidance is that the query builder should be physically incapable of producing DELETE or DROP. Then the guarantee is structural, not persuasive.
- **Treat model output as untrusted input.** Escape it on render, strip raw HTML from markdown, and validate structured output fields before acting on them. A manipulated model can emit a phishing link or HTML into your page.
- **Require human confirmation for anything irreversible** — refunds, deletions, outbound email, permission changes.
- **Test the boundary, not the model.** Write a test that asserts a tool called in user A's context cannot return user B's row. It needs no LLM call and it fails the build when a refactor drops the scoping.

```php
// WRONG — the model supplies the tenant; prompt injection changes it
public function handle(int $userId, string $status): array {
    return Order::where('user_id', $userId)->where('status', $status)->get()->toArray();
}

// RIGHT — the tenant comes from the server; the model only picks the filter
public function __construct(private readonly User $actor) {}

public function handle(string $status): array {
    abort_unless(in_array($status, ['pending', 'paid', 'shipped'], true), 422);
    return $this->actor->orders()->where('status', $status)
                 ->get(['id', 'status', 'total'])->toArray();
}
```

## Security

Laravel-specific and mechanical. php.ini, Composer, and worker-runtime hardening live in `mir-backend-php`.

### The settings that ship dangerous, named exactly

| Setting | Danger | Correct value |
|---|---|---|
| `APP_DEBUG=true` in production | the exception page prints the full environment, including `APP_KEY` and DB credentials | `false` — and confirm via a deliberately broken route |
| `$guarded = []` on a model | full overposting | explicit `$fillable` |
| `config/cors.php` after `php artisan config:publish cors` | published defaults are `allowed_origins => ['*']`, `allowed_methods => ['*']`, `allowed_headers => ['*']` | explicit origin list, or `allowed_origins_patterns` for subdomains |
| `supports_credentials => true` **with** `allowed_origins => ['*']` | browsers reject it, so teams "fix" it by reflecting the `Origin` header — that is any-origin-with-cookies | explicit origins only; never reflect `Origin` |
| `$except` wildcards in the CSRF middleware | one `'api/*'` disables forgery protection on every stateful route under it | exempt individual webhook routes and verify their signature instead |
| `SESSION_DRIVER=cookie` | the session lives in a client-held encrypted cookie; a leaked `APP_KEY` becomes deserialization of attacker-controlled data | `database` or `redis` |

### APP_KEY is a root credential

`APP_KEY` is the AES-256 key behind encrypted cookies, the session cookie, `Crypt::encrypt()`, `encrypted` model casts, signed URLs, and queue payloads. If it leaks — committed `.env`, `.env` served over HTTP, a build image, a screenshot — an attacker can forge session cookies for any user, forge signed URLs, and decrypt stored ciphertext. With `SESSION_DRIVER=cookie` it escalates further, because decrypting a session cookie unserializes its contents.

- Rotate immediately on any exposure. Deleting the file from the repo is not enough; git history keeps the value.
- **Laravel 11+ supports graceful rotation via `APP_PREVIOUS_KEYS`** — decryption tries `APP_KEY` first, then the listed previous keys. Use it so rotation does not log everyone out at once. Application code that encrypts its own data still needs a decrypt-with-old / re-encrypt-with-new pass.
- Keep `.env` out of source control and out of images. Verify `https://yoursite/.env` returns 404, not 200.

### Object-level authorization (IDOR / BOLA)

Route-model binding resolves the record. It does not check that the caller may have it. `auth` middleware proves who they are, not what they may touch.

```php
// WRONG — a valid session plus someone else's id returns someone else's order
public function show(Order $order) { return new OrderResource($order); }

// RIGHT — policy check on the resolved object
public function show(Order $order) {
    $this->authorize('view', $order);          // or Gate::authorize('view', $order)
    return new OrderResource($order);
}
```

- Laravel 13 adds `#[Authorize('create', [Comment::class, 'post'])]` as a controller/method attribute. It is the same `Gate` check; the attribute form does not change what you must check.
- For nested routes use `Route::scopeBindings()` so `/users/{user}/orders/{order}` resolves the order **through** the user instead of by global id.
- For multi-tenancy, add a global scope keyed on the tenant, and assert it in a test. A forgotten `where('tenant_id', ...)` is invisible in code review and total in effect.
- Authorize **before** the write, not after. `$order->update(...)` followed by an `authorize()` is already too late.

### CSRF, SameSite, and which auth scheme needs it

- Session-cookie auth needs CSRF. Bearer-token auth (`Authorization: Bearer …`, Sanctum personal access tokens, Passport) does not — the browser does not attach the header automatically.
- Laravel 13 formalises the middleware as `PreventRequestForgery` and adds origin-aware verification alongside the existing token check. Do not disable the origin check to make a cross-origin call work; fix the origin configuration instead.
- Set `SESSION_SAME_SITE=lax` (or `strict`), `SESSION_SECURE_COOKIE=true`, and `SESSION_DOMAIN` to the exact host. `same_site=none` requires `secure=true` and should be a deliberate decision, not a copy-paste fix for a CORS error.
- **Sanctum SPA (cookie) mode** additionally requires `supports_credentials => true` in `config/cors.php`, explicit origins, `SANCTUM_STATEFUL_DOMAINS`, a shared parent domain between SPA and API, and `withCredentials` + `withXSRFToken` on the client. CORS is a browser policy, not authorization — protected endpoints must still reject missing or invalid credentials on their own.

### Injection

- Bound parameters everywhere. The holes are the raw helpers: `whereRaw`, `havingRaw`, `orderByRaw`, `selectRaw`, `DB::raw`, `DB::statement`. Any of them with string interpolation is SQL injection.
- **`orderBy($request->input('sort'))` is injection too** — the column is an identifier and cannot be bound. Map the request value through a hard-coded allow-list.
- `whereIn('id', $request->input('ids'))` with a non-array or nested array from the client produces surprising SQL. Validate the shape first (`'ids' => ['array'], 'ids.*' => ['integer']`).
- Blade escapes with `{{ }}`. `{!! !!}` does not. Never render user content with `{!! !!}`, and never build a Blade or Twig template string from user input.
- CRLF: see the advisory table — a `\r\n` in an email address reaches the mail transport.

### Current advisories on the default path

| Identifier | What | Affected | Fixed |
|---|---|---|---|
| CVE-2026-48019 (GHSA-5vg9-5847-vvmq), High | CRLF injection in `ValidatesAttributes::validateEmail` — the **default `email` validation rule**. A `\r\n` plus an extra header (e.g. `Bcc:`) in an address field reaches Symfony Mailer/Mime and silently copies outbound mail. No auth needed. | ≤ 13.9.0 and < 12.60.0 | 12.60.0 / 13.10.0 |
| GHSA-crmm-hgp2-wgrp, Moderate | Ambiguous parsing of **temporary signed URLs** in the local filesystem driver: expired URLs can keep working, and a request can resolve to a different resource than the one signed — including for uploads | 13.0.0 – 13.11.x and < 12.61.1 | 12.61.1 / 13.12.0 |
| CVE-2025-27515, Moderate | File validation bypass with wildcard array rules (`files.*`) — a crafted request skips the rules | < 10.48.29, 11.0.0 – 11.44.0, 12.0.0 – 12.1.0 | 10.48.29 / 11.44.1 / 12.1.1 |
| CVE-2024-52301, High | Environment manipulation via query string when PHP's `register_argc_argv` is on — an unauthenticated GET can flip `APP_ENV`/`APP_DEBUG` | < 6.20.45, < 9.52.17, < 10.48.23, < 11.31.0 | 11.31.0 and the listed backports; also set `register_argc_argv=Off` |
| Reflected XSS in the debug error page | route parameters not encoded on the debug page | 11.9.0 – 11.35.1 | 11.36.0 — and `APP_DEBUG=false` removes the page entirely |

Supply chain: in May 2026 four community `laravel-lang` Packagist packages had 700+ malicious versions published under historical tags, later dissociated from the malicious commits, so a version number alone does not tell you whether an install was affected. Audit install history for that window, and run `composer audit --locked` in CI.

### File handling

- `$request->file('doc')->store('docs')` generates a random name — safe. `storeAs('docs', $request->input('name'))` puts a client-controlled string in the path — that is traversal and overwrite. Never take the stored name from the request.
- Validate uploads with `File::types([...])` / `mimes:` (server-side sniff), not `mimetypes:` on the client-declared type alone. `getClientOriginalExtension()` is client input.
- Do not store uploads on the `public` disk unless they are genuinely public. Serve private files through a controller that runs the policy check.
- Never build a `Storage::get()` / `Storage::download()` path from raw request input.

### SSRF

`Http::get($request->input('url'))` follows redirects by default. Validate the URL against a scheme and host allow-list, resolve DNS and reject private and link-local ranges (including `169.254.169.254`), then re-check after each redirect — set `->withoutRedirecting()` and follow manually. Details and the metadata-endpoint case are in `mir-backend-php`.

### Secret and PII leakage

- `$hidden` on models keeps `password`, `remember_token`, and tokens out of `toArray()`/`toJson()`. Prefer an explicit API Resource that lists the fields you do want — allow-list beats deny-list.
- `Log::info($request->all())` writes passwords and tokens in plaintext. Log a correlation ID and named safe fields.
- Telescope, Pulse, Horizon, and `/_ignition` must not be reachable in production without auth. Telescope records request payloads including credentials.
- Queue payloads are serialized job properties. Do not put raw card data or plaintext secrets on a job; pass an id and re-read.

## How this slots into the core pipeline

- **Gate 5 (Design):** state transaction boundaries and whether the DB gives you transactional DDL; identify queue-bound work; confirm `$fillable` scope and Form Request coverage; name the policy for every object-level read and write; audit Octane-unsafe singletons if Octane is in the stack; for AI agents, write the tool authorization boundary down before writing the tool.
- **Gate 6 (Implementation):** `with()` eager-loading (or automatic eager loading, verified in the query log), explicit `$fillable`, `$request->validated()`, `dispatch()->afterCommit()`, `scoped` or resolver-closure bindings under Octane, expand/contract migrations, tools scoped from `auth()->user()`.
- **Gate 7 (Review):** reliability-reviewer checks items 1–7. security-reviewer walks the Security section: `APP_DEBUG`, `APP_KEY` handling and rotation, CORS and CSRF configuration, a policy check on every object-level route, raw query builders, upload paths, the advisory table against `composer.lock`. migration-reviewer applies expand/contract to every schema change on a populated table.

## Edit boundary (what belongs here vs. the core)

**This module holds ONLY Laravel/Eloquent library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability principle)? → **generic core** (`mir-backend`).
- True for every PHP framework on the Zend Engine (shared-nothing lifecycle, FPM sizing, worker-runtime state bleed, opcache/JIT, php.ini hardening, `unserialize`, Composer supply chain)? → **runtime tier** (`mir-backend-php`).
- A mechanical footgun of *this library* (Eloquent N+1 and automatic eager loading, `$fillable`/`forceFill`, `DB::transaction` + `afterCommit`, Octane container/request/config injection, Laravel migration/DDL behaviour, `APP_KEY`, policies, AI SDK tool scoping)? → **here**.
- A *different* PHP framework (Symfony, WordPress) → new `mir-backend-php-<framework>` module. A *different* runtime → its own tier. Never widen this one.
