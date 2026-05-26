---
name: mir-backend-php
description: "Make It Right (PHP runtime tier). Zend Engine / PHP 8.2+ runtime reliability footguns that are shared across EVERY PHP backend framework (Laravel, Symfony, WordPress, Slim, Lumen) — distinct from the generic backend gates and from any one framework's mechanics. Covers: shared-nothing request lifecycle and why static/global state does not persist, FPM worker model and concurrency = pm.max_children (not threads), long-running runtime modes (Swoole/RoadRunner/FrankenPHP/Octane) and the state-bleed/memory-leak inversion they introduce, max_execution_time and memory_limit per request, opcache correctness in production, persistent DB connection caveats, and PHP's error/exception model in prod. TRIGGER when the backend runtime is PHP — sits between mir-backend (generic) and the framework module (e.g. mir-backend-php-laravel). SKIP for Node/JVM/Go/Rust/.NET/Python/Ruby/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics (those live in the framework module)."
trigger: /mir-backend-php
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-php · Make It Right (PHP runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-php-laravel`) knows the **library's mechanics**. This tier owns what's true for **all PHP backends because they run on the Zend Engine** — the request lifecycle and process model that Laravel, Symfony, WordPress, and Slim all inherit.

**Runtime assumed:** PHP 8.2+ under PHP-FPM (the dominant production model). Long-running runtime notes (Swoole, RoadRunner, FrankenPHP, Laravel Octane) are called out explicitly because they **invert** several of the default rules. Load order: `mir-backend` → `mir-backend-php` → `<framework module>`.

## The PHP/Zend footguns AI walks into (framework-agnostic)

### 1. Shared-nothing per request — static/global state does NOT persist

The Zend Engine tears down every variable, object, and static property at the end of each request. **Nothing in PHP process memory survives to the next request under FPM** — there is no "warm" in-process store between requests.

- AI commonly writes `static $cache = []` inside a class, expecting it to persist across requests like an application-level cache. Under FPM each worker resets statics on every request — the "cache" is always empty at request start and is thrown away at request end.
- **Fix:** persist data externally — opcache for compiled bytecode, APCu for small in-process per-worker caches (single worker only, not shared across workers), Redis/Memcached for truly shared data, DB for durable data. Treat the PHP process as a pure function: input → output.
- **Why AI errs:** AI is trained on patterns from runtimes (Node, Python async) where module-level singletons genuinely survive across requests. That pattern is wrong in FPM-PHP.

```php
// WRONG — resets to [] every request under FPM
class UserRepo {
    private static array $cache = [];
    public static function find(int $id): User {
        if (!isset(self::$cache[$id])) {
            self::$cache[$id] = DB::find($id); // never warm on next request
        }
        return self::$cache[$id];
    }
}

// RIGHT — use APCu for single-worker hot cache, Redis for cross-worker
$user = apcu_fetch("user:$id", $found) ?: tap(DB::find($id), fn($u) => apcu_store("user:$id", $u, 60));
```

### 2. FPM worker model — concurrency = pm.max_children, not threads

PHP-FPM processes one request per worker process at a time. There are no threads handling concurrent requests within a worker — **concurrency is purely the number of live FPM worker processes**.

- `pm.max_children` sets the ceiling. A slow DB query or a hung third-party API call ties up an entire worker for the full duration. With 20 workers and a downstream that takes 10 seconds, you saturate the pool after 20 simultaneous slow requests.
- **Fix:** size `pm.max_children` to `floor(available_RAM / per_worker_peak_RSS)`. Typical PHP-FPM workers use 20–100 MB each. Measure with `ps aux --sort=rss`. Also use `pm = dynamic` with sensible `pm.min_spare_servers` / `pm.max_spare_servers` to avoid cold-start churn.
- A single blocking call (sync HTTP, slow query, filesystem wait) eats a whole worker slot. Set socket/HTTP timeouts aggressively; offload slow/durable work to queues. Never let a web worker sit blocked on a third-party service.

```ini
; php-fpm.conf — example sizing for a 1 GB container, ~50 MB/worker
[www]
pm = dynamic
pm.max_children = 18
pm.start_servers = 4
pm.min_spare_servers = 2
pm.max_spare_servers = 6
```

### 3. Long-running runtimes (Swoole / RoadRunner / FrankenPHP / Octane) flip the model

Under Swoole, RoadRunner, FrankenPHP, or Laravel Octane the PHP process is **persistent** — it handles many requests without dying. The shared-nothing guarantee is gone. Two new failure classes emerge that do not exist under FPM:

**State bleed:** Static properties, container singletons, and class-level state set during request A are still set during request B. A singleton that holds `$request`, `$user`, or `$tenant` bleeds that data across users.

```php
// WRONG under Octane/Swoole — singleton retains first request's user
class AuthContext {
    private static ?User $user = null;
    public static function set(User $u): void { self::$user = $u; }
    public static function get(): ?User { return self::$user; }
}
// After request 1 sets AuthContext::set($alice), request 2 sees $alice

// RIGHT — reset in the request lifecycle hook, or use request-scoped storage
// Laravel Octane: use ->flush() / ->forgetScopedInstances() in tick callbacks
// RoadRunner: reset via PSR-15 middleware that reinitializes per-request state
```

**Memory leaks:** Objects appended to a static list or an event dispatcher that is never flushed grow the process heap without bound. Each request adds more; GC cannot collect what static references hold. Monitor RSS of long-running workers and set a max-request limit (`--max-requests` / `max_requests` in FPM pool config) as a circuit-breaker.

- **Why AI errs:** AI generates normal-looking singleton/static code that is safe under FPM but becomes a correctness bug under persistent runtimes. Always ask: "Is this running under Octane/Swoole/RoadRunner?" If yes, audit every static and every container binding.

### 4. max_execution_time and memory_limit — hard runtime ceilings

PHP enforces per-request resource limits in `php.ini`:

- `max_execution_time` (default 30 s) — the Zend Engine raises a `Fatal error` when exceeded, killing the request mid-flight. **Long tasks (imports, report generation, bulk processing) must not run in a web request.**
- `memory_limit` (default 128 M) — exceeded → `Fatal error: Allowed memory size exhausted`. AI-generated code that loads large result sets into arrays (e.g., `$all = Model::all()`) blows this on tables with millions of rows.

**Fixes:**
- Queue long tasks (Horizon/Queue for Laravel, Messenger for Symfony, plain `php artisan queue:work`). Use `set_time_limit(0)` only in CLI/worker scripts, never in web-facing code.
- Stream or chunk large data sets (`cursor()` / `chunk()` in Laravel, `iterate()` in Doctrine) instead of loading the full result into memory.

```php
// WRONG — loads every row into memory at once
$users = User::all(); // Fatal on a large table

// RIGHT — cursor yields one hydrated model at a time
User::cursor()->each(function (User $user) {
    processUser($user);
});
```

### 5. opcache must be enabled and tuned in production

Without opcache, PHP parses and compiles every `.php` file from disk on **every request**. A 100-file framework bootstrap runs 100 file reads and 100 compilations per request — an enormous CPU and I/O overhead that disappears entirely with a warm opcache.

Critical production settings:

```ini
; php.ini — production opcache
opcache.enable=1
opcache.memory_consumption=256       ; MB — size to fit your codebase's compiled output
opcache.max_accelerated_files=20000  ; must be > number of .php files in project
opcache.validate_timestamps=0        ; NEVER 0 in dev; mandatory 0 in prod for performance
opcache.revalidate_freq=0            ; irrelevant when validate_timestamps=0
opcache.jit=tracing                  ; PHP 8+ JIT — useful for CPU-heavy paths
opcache.jit_buffer_size=128M
```

- `validate_timestamps=0` is the prod-critical flag — it disables per-request stat() calls to check file mtime. **Set it to 0 in prod and restart FPM/reload opcache after every deploy.** Without it you get ~50% of the cache benefit at best.
- After a code deploy, the opcache still holds old bytecode until FPM is reloaded (`systemctl reload php8.2-fpm`) or until opcache TTL expires. Add a deploy step: `php-fpm reload` or use `opcache_reset()` via a protected endpoint.

### 6. Persistent DB connections — pconnect leaks transaction state

`pconnect` (PDO's `PDO::ATTR_PERSISTENT = true`) reuses a DB connection across requests in the same FPM worker process. The connection is not reset between requests — **any uncommitted transaction, `SET SESSION` variable, or temp table from request A is still live when request B picks up the connection**.

- A request that crashes mid-transaction leaves an open transaction. The next request on the same worker inherits it and may read/write inside it unknowingly.
- **Fix:** if you use persistent connections, open the connection with an explicit `ROLLBACK` / health check at the top of request initialization, or avoid persistent connections entirely (the connection overhead is small compared to the transaction-state risk). Most PHP frameworks default to non-persistent for this reason.

```php
// Risky — transaction from a previous crashed request survives
$pdo = new PDO($dsn, $user, $pass, [PDO::ATTR_PERSISTENT => true]);

// Safer — default (non-persistent); or explicitly rollback on borrow
$pdo->rollBack(); // at request start if using persistent connections
```

### 7. Error and exception model — PHP has both, and both must be handled

PHP has two distinct error systems: **exceptions** (`Throwable` hierarchy, catchable) and **engine errors** (E_WARNING, E_NOTICE, E_DEPRECATED, E_ERROR — historically not catchable). PHP 8 unified most fatal errors under `Error`/`Throwable`, but `E_WARNING` and `E_NOTICE` are still non-exception signals.

- In production: `display_errors=Off` is mandatory — stack traces and error messages exposed to HTTP responses leak internal paths, class names, and query structure.
- Set `log_errors=On` and point `error_log` to your logging pipeline. Use `error_reporting(E_ALL)` in dev, `E_ALL & ~E_DEPRECATED & ~E_NOTICE` in prod (or E_ALL — fix the notices instead of silencing them).
- Register a `set_error_handler()` to convert E_WARNING/E_NOTICE to exceptions (or structured log entries) — AI-generated code that suppresses errors with `@` prefix (`@file_get_contents(...)`) silently swallows failures.
- Catch `Throwable` (not just `Exception`) to capture `Error` subclasses (`TypeError`, `ArithmeticError`, `ParseError`).

```ini
; php.ini — production
display_errors = Off
log_errors = On
error_log = /var/log/php/error.log
error_reporting = E_ALL
```

```php
// WRONG — @ suppresses the error, you never know the file wasn't read
$contents = @file_get_contents('/path/to/file');

// RIGHT — check return value or throw
$contents = file_get_contents('/path/to/file');
if ($contents === false) {
    throw new RuntimeException("Failed to read file: /path/to/file");
}
```

## How this slots into the pipeline

- **Gate 0/5 (model choice):** confirm the runtime mode (FPM vs. Swoole/Octane). If long-running: audit static state and singletons before writing any framework code. Size FPM workers to available memory. Flag any slow/durable work as queue-bound — not inline.
- **Gate 6 (implementation):** no static caches expecting cross-request persistence; no `Model::all()` on large tables; opcache tuned; `display_errors=Off`; explicit error handler; no `@`-suppression.
- **Gate 7 (review):** reliability-reviewer checks items 1–7 here for any PHP service, plus long-running runtime state-bleed audit if Octane/Swoole is in the stack.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability principles) → **up** to `mir-backend`.
- A specific library's mechanics (Eloquent N+1, Doctrine Unit of Work, Laravel Queues, Symfony Serializer groups) → **down** to the framework module (`mir-backend-php-<framework>`).
- **Here:** only what every PHP backend shares because of the Zend Engine and FPM/persistent-runtime process model (shared-nothing lifecycle, worker sizing, state-bleed under Octane/Swoole, resource limits, opcache, pconnect, error model).
- A different runtime (Node, Go, JVM…) → its own `mir-backend-<runtime>` tier. Never widen this one.
