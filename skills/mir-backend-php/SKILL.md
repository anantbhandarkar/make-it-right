---
name: mir-backend-php
description: "Make It Right (PHP runtime tier). Zend Engine / PHP 8.4–8.5 runtime reliability footguns that are shared across EVERY PHP backend framework (Laravel, Symfony, WordPress, Slim, Lumen) — distinct from the generic backend gates and from any one framework's mechanics. Covers: shared-nothing request lifecycle under PHP-FPM and why static/global state does not persist, concurrency = pm.max_children (not threads), long-running worker runtimes (FrankenPHP, Swoole, RoadRunner, Laravel Octane) and the state-bleed/memory-leak inversion they introduce, max_execution_time not counting blocked I/O, memory_limit, opcache plus the PHP 8.4 opcache.jit default flip, persistent PDO connection state, SIGTERM handling in queue workers, PHP's error/exception model in production, and runtime-level security settings (register_argc_argv, session.use_strict_mode, unserialize, parse_url/SSRF, Composer supply chain). TRIGGER when the backend runtime is PHP and the concern is the engine, the php.ini, or the process model — sits between mir-backend (generic) and the framework module. SKIP for Node/JVM/Go/Rust/.NET/Python/Ruby/BEAM runtimes (each has its own mir-backend-<runtime> tier). SKIP for Laravel library mechanics (Eloquent N+1, $fillable, Queues, Octane container bindings → mir-backend-php-laravel) and for Symfony/Doctrine library mechanics (Unit of Work, Serializer groups, Messenger, MapRequestPayload → mir-backend-php-symfony)."
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

**Runtime versions, verified 13 August 2026:**

| Branch | Status | Latest | Dates |
|---|---|---|---|
| 8.5 | current stable | 8.5.9 (30 Jul 2026) | active support to 31 Dec 2027, EOL 31 Dec 2029 |
| 8.4 | active support | 8.4.24 | JIT INI defaults changed in this branch |
| 8.3 | security fixes only | 8.3.33 | |
| 8.2 | security fixes only | 8.2.33 | security support ends Dec 2026 |
| ≤ 8.1 | end of life | — | no fixes at all; treat as a finding |

Every branch gets 2 years of active support then 2 years of security-only fixes (the cycle was extended from 3 years to 4 in March 2024). PHP 8.6 is in development for late 2026.

**Process model assumed:** PHP-FPM (still the dominant production model). Long-running worker runtimes — FrankenPHP (adopted by the PHP Foundation in May 2025, source now under the `php` GitHub org), Swoole, RoadRunner, Laravel Octane, Symfony Runtime — are called out explicitly because they **invert** several of the default rules. Load order: `mir-backend` → `mir-backend-php` → `<framework module>`.

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

// RIGHT — APCu for a single-worker hot cache, Redis for cross-worker
$user = apcu_fetch("user:$id", $found) ?: tap(DB::find($id), fn($u) => apcu_store("user:$id", $u, 60));
```

### 2. FPM worker model — concurrency = pm.max_children, not threads

PHP-FPM processes one request per worker process at a time. There are no threads handling concurrent requests within a worker — **concurrency is purely the number of live FPM worker processes**.

- `pm.max_children` sets the ceiling. A slow DB query or a hung third-party API call ties up an entire worker for the full duration. With 20 workers and a downstream that takes 10 seconds, you saturate the pool after 20 simultaneous slow requests.
- **Fix:** size `pm.max_children` to `floor(available_RAM / per_worker_peak_RSS)`. Typical PHP-FPM workers use 20–100 MB each. Measure with `ps aux --sort=rss`. Also use `pm = dynamic` with sensible `pm.min_spare_servers` / `pm.max_spare_servers` to avoid cold-start churn.
- A single blocking call (sync HTTP, slow query, filesystem wait) eats a whole worker slot. Set socket/HTTP timeouts aggressively; offload slow/durable work to queues. Never let a web worker sit blocked on a third-party service.
- `pm.max_requests` recycles a worker after N requests. It bounds leaks in C extensions; it is not a substitute for fixing them.

```ini
; php-fpm.conf — example sizing for a 1 GB container, ~50 MB/worker
[www]
pm = dynamic
pm.max_children = 18
pm.start_servers = 4
pm.min_spare_servers = 2
pm.max_spare_servers = 6
pm.max_requests = 500
request_terminate_timeout = 60s   ; wall-clock kill; see footgun 4
```

### 3. Long-running worker runtimes (FrankenPHP / Swoole / RoadRunner / Octane) flip the model

Under FrankenPHP worker mode, Swoole/OpenSwoole, RoadRunner, or Laravel Octane the PHP process is **persistent** — it handles many requests without dying. The shared-nothing guarantee is gone. Three failure classes emerge that do not exist under FPM.

`symfony/runtime` is **not** itself one of these: it is a bootstrap abstraction, and the process only persists when the selected runner loops over requests (`FrankenPhpWorkerRunner`, a RoadRunner or Swoole bridge). The default runner terminates per request like FPM. Check which runner is configured before deciding which set of rules applies.

**State bleed.** Static properties, container singletons, and class-level state set during request A are still set during request B. A singleton that holds `$request`, `$user`, or `$tenant` bleeds that data across users.

**Under coroutine-enabled Swoole/OpenSwoole it is worse than staleness: requests interleave inside one worker.** A coroutine yields at every I/O boundary and another request resumes on the same statics, superglobals, and singletons. Request-scoped state is then a *race*, not just a leftover — request A can read the tenant request B just wrote, mid-request. Anything request-, user-, tenant-, or session-scoped must live in coroutine-local context (`Swoole\Coroutine::getContext()`) or be passed as an argument. Never a process global.

```php
// WRONG under any worker runtime — the singleton retains request 1's user
class AuthContext {
    private static ?User $user = null;
    public static function set(User $u): void { self::$user = $u; }
    public static function get(): ?User { return self::$user; }
}
// After request 1 sets AuthContext::set($alice), request 2 sees $alice
```

Reset mechanism per stack (framework specifics live in the framework module):

| Runtime | Reset mechanism |
|---|---|
| Symfony, **when the runner drives the HttpKernel request loop** (`FrankenPhpWorkerRunner`, the maintained RoadRunner/Swoole bridges) | services implementing `Symfony\Contracts\Service\ResetInterface` are auto-tagged `kernel.reset` and reset between requests. The kernel and container object instances themselves survive. **A hand-rolled worker loop gets none of this** — implementing `ResetInterface` does nothing unless something calls `Kernel::reset()`. Verify the bridge does, or the previous user/tenant/token stays live. |
| Symfony 8.1+ escape hatch | `FRANKENPHP_RESET_KERNEL=1` makes `FrankenPhpWorkerRunner` clone the application after each request instead of reusing the kernel. |
| Laravel Octane | Octane resets first-party framework state only. Your bindings are your problem — see `mir-backend-php-laravel`. |
| Anything | a static property, a function-level `static $x`, or a class-level cache is **never** reset by any of the above. |

**Superglobals are not all reset.** FrankenPHP resets `$_GET`, `$_POST`, `$_COOKIE`, `$_FILES`, `$_SERVER`, `$_REQUEST` between requests. **`$_ENV` is not reset** — writes during request A are visible in request B on the same thread. Never store request-scoped or sensitive data in `$_ENV`. `$_SESSION` has been the subject of two separate cross-request leak advisories — see Security below.

**Memory leaks.** Objects appended to a static list, or an event dispatcher that is never flushed, grow the process heap without bound. GC cannot collect what a static reference holds. Set a request ceiling as a circuit-breaker and monitor per-worker RSS:

| Runtime | Request ceiling |
|---|---|
| FrankenPHP | `max_requests <n>` in the `frankenphp` Caddyfile block (marked experimental; default 0 = unlimited) |
| Laravel Octane | `--max-requests` on `octane:start` (default 500) |
| PHP-FPM | `pm.max_requests` |

FrankenPHP sizing rule from its own docs: `num_threads × memory_limit < available_memory`. Its default is 2× CPU cores for both threads and workers, which is usually wrong for your app — measure.

- **Why AI errs:** AI generates normal-looking singleton/static code that is safe under FPM but becomes a correctness bug under a worker runtime. Always ask at Gate 0: "Is this running under FrankenPHP worker mode / Octane / Swoole / RoadRunner?" If yes, audit every static and every container binding.

### 4. max_execution_time does NOT count blocked I/O — memory_limit is per request

Both limits are in `php.ini` and both kill the request with a `Fatal error` when exceeded. The trap is what `max_execution_time` actually measures.

- **`max_execution_time` (default 30) counts script execution only.** Time spent inside `system()`, stream operations, `sleep()`, and **database queries** is not counted on Unix-like systems. A request blocked for 10 minutes on a hung DB query is never killed by `max_execution_time`. Windows is the exception — there it measures wall clock.
- **The wall-clock kill is `request_terminate_timeout` in the FPM pool config.** If you only set `max_execution_time`, a hung downstream still holds the worker forever. Set both, and set the client/driver timeout too (`PDO::ATTR_TIMEOUT`, cURL `CURLOPT_TIMEOUT`, `default_socket_timeout`).
- **`memory_limit` (default 128M) is per request.** AI-generated code that loads large result sets into arrays (`$all = Model::all()`) blows this on tables with millions of rows.
- **Fix:** queue long tasks. Use `set_time_limit(0)` only in CLI/worker scripts, never in web-facing code. Stream or chunk large data sets (`cursor()`/`chunk()` in Laravel, `toIterable()` in Doctrine) instead of loading the full result into memory.

```php
// WRONG — loads every row into memory at once
$users = User::all(); // Fatal: allowed memory size exhausted

// RIGHT — cursor yields one hydrated model at a time
User::cursor()->each(function (User $user) {
    processUser($user);
});
```

### 5. opcache in production — and the PHP 8.4 JIT default flip

Without opcache, PHP parses and compiles every `.php` file from disk on **every request**. A 100-file framework bootstrap runs 100 file reads and 100 compilations per request. That overhead disappears entirely with a warm opcache.

```ini
; php.ini — production
opcache.enable=1
opcache.memory_consumption=256       ; MB — size to fit your codebase's compiled output
opcache.max_accelerated_files=20000  ; must exceed the number of .php files in the project
opcache.validate_timestamps=0        ; prod only — leave at the default 1 in dev
opcache.revalidate_freq=0            ; irrelevant when validate_timestamps=0

; JIT — PHP 8.4 changed how it is turned on. Both lines are now required.
opcache.jit=tracing                  ; 8.4+ default is `disable`; was `tracing` before 8.4
opcache.jit_buffer_size=128M         ; 8.4+ default is 64M; was 0 before 8.4
```

- **PHP 8.4 flipped the JIT INI defaults.** Before 8.4: `opcache.jit=tracing`, `opcache.jit_buffer_size=0` — you enabled JIT by raising the buffer size. From 8.4: `opcache.jit=disable`, `opcache.jit_buffer_size=64M`. **JIT is disabled by default in both cases**, but any config that enabled it by setting only `opcache.jit_buffer_size` silently stops enabling it after an 8.4 upgrade. If you want JIT on 8.4/8.5, set `opcache.jit=tracing` explicitly.
- Use the string forms (`disable`, `off`, `tracing`, `function`), not the legacy 4-digit CRTO numbers.
- `opcache.jit_buffer_size` is part of OPcache's shared memory, allocated **once per FPM master** (all pools and all workers under that master share it) and separately per CLI invocation. It is not per-pool, so you cannot budget it per pool — run separate FPM masters if two workloads genuinely need different sizes. A CLI-heavy host multiplies it by concurrent CLI processes.
- **JIT rarely helps a typical request-response web app.** It helps CPU-bound loops. Measure before enabling; do not treat it as free throughput.
- `validate_timestamps=0` removes the per-request `stat()` on every file. It also means **the opcache still holds old bytecode after a deploy until FPM is reloaded** (`systemctl reload php8.4-fpm`) or the container is replaced. Make the reload an explicit deploy step. Deploy by replacing the container, not by editing files in place.

### 6. Persistent DB connections — pconnect leaks transaction state

`PDO::ATTR_PERSISTENT = true` reuses a DB connection across requests in the same FPM worker process. The connection is not reset between requests — **any uncommitted transaction, `SET SESSION` variable, temp table, or advisory lock from request A is still live when request B picks up the connection**.

- A request that crashes mid-transaction leaves an open transaction. The next request on the same worker inherits it and may read/write inside it unknowingly.
- **Fix:** default to non-persistent. The connect cost is small compared to the transaction-state risk, which is why most PHP frameworks default to non-persistent. If you must use persistent connections, roll back and reset session state at request start.

```php
// Risky — a transaction from a previous crashed request survives
$pdo = new PDO($dsn, $user, $pass, [PDO::ATTR_PERSISTENT => true]);

// Safer — default (non-persistent); or explicitly reset on borrow
if ($pdo->inTransaction()) { $pdo->rollBack(); }
```

### 7. Error and exception model — PHP has both, and both must be handled

PHP has two error systems: **exceptions** (`Throwable`, catchable) and **engine diagnostics** (`E_WARNING`, `E_NOTICE`, `E_DEPRECATED`). PHP 8 promoted most previously-fatal engine errors to `Error`/`Throwable`, but warnings and notices are still not exceptions.

- Catch `Throwable`, not just `Exception`, to capture `TypeError`, `ValueError`, `ArithmeticError`.
- Register `set_error_handler()` to convert `E_WARNING`/`E_NOTICE` into exceptions or structured log entries. Register `set_exception_handler()` and `register_shutdown_function()` + `error_get_last()` so fatals still produce one structured log line.
- AI-generated code that suppresses with `@` (`@file_get_contents(...)`) silently swallows failures. `@` also suppresses the error inside your custom error handler unless you check `error_reporting()`.
- PHP 8.4 deprecated implicit nullable parameters (`function f(Foo $x = null)` → write `?Foo $x = null`). Under `error_reporting = E_ALL` this floods logs after an 8.4 upgrade. Fix the signatures; do not silence `E_DEPRECATED` and lose real deprecations.

```ini
; php.ini — production
display_errors = Off
display_startup_errors = Off
log_errors = On
error_log = /var/log/php/error.log
error_reporting = E_ALL
```

```php
// WRONG — @ hides the failure; $contents is false and you never know why
$contents = @file_get_contents('/path/to/file');

// RIGHT — check the return value or throw
$contents = file_get_contents('/path/to/file');
if ($contents === false) {
    throw new RuntimeException('Failed to read file: /path/to/file');
}
```

### 8. Queue workers and SIGTERM — graceful shutdown needs pcntl

Every PHP framework's queue worker (`php artisan queue:work`, `php bin/console messenger:consume`) is a long-running CLI process. On deploy, your orchestrator sends `SIGTERM` and then `SIGKILL` after a grace period.

- **Without ext-pcntl loaded, the worker cannot install a signal handler.** `SIGTERM` terminates it immediately, mid-job. The job is neither completed nor released back to the queue — it sits invisible until its visibility timeout expires, and only then is it retried. `pcntl` is missing from several slim container images. Check with `php -m | grep pcntl`.
- **Fix:** load `ext-pcntl` in the worker image, and set the orchestrator's grace period longer than your longest job (`terminationGracePeriodSeconds` in Kubernetes, `stopwaitsecs` in Supervisor). The framework's handler finishes the current job and then exits.
- Combine with the at-least-once rule from `mir-backend`: a `SIGKILL` mid-job means the job **will** be redelivered. Handlers must be idempotent regardless.
- Restart workers after every deploy. A worker holds old code in memory exactly like an Octane request worker does.

## Security

Runtime-level and process-level only. Framework mechanics (mass assignment, policies, serializer groups) live in the framework module.

### php.ini settings that ship insecure or that AI leaves at the default

| Setting | Default | Set to | What breaks otherwise |
|---|---|---|---|
| `display_errors` | `On` in the shipped `php.ini-development` | `Off` | Stack traces in HTTP responses leak absolute paths, class names, SQL fragments, and env values |
| `expose_php` | `On` | `Off` | `X-Powered-By: PHP/8.4.x` hands an attacker your exact patch level |
| `register_argc_argv` | `On` in the official `php` Docker images | `Off` | Turns a query string into `$_SERVER['argv']`. This is the precondition for the Symfony/Laravel environment-override advisories below. Nothing in a web app needs it. |
| `session.use_strict_mode` | `0` | `1` | With `0`, PHP accepts a session ID the client invented → session fixation |
| `session.cookie_httponly` | `0` | `1` | Session cookie readable from JavaScript |
| `session.cookie_secure` | `0` | `1` | Session cookie sent over plain HTTP |
| `session.cookie_samesite` | `""` (empty) | `Lax` or `Strict` | Cookie attached to cross-site requests |
| `allow_url_fopen` | `On` | `Off` where possible | `include`/`file_get_contents` on a user-controlled string reaches remote hosts |
| `cgi.fix_pathinfo` | `1` | `0` (php-cgi only) | Path-info confusion lets `/upload.jpg/x.php` be executed as PHP |
| `security.limit_extensions` (FPM pool) | `.php` | keep explicit | Widening this lets FPM execute uploaded files with other extensions |

Do not solve this with `disable_functions`; it is a mitigation of last resort, not a control. Use it only alongside the settings above.

### Current advisories affecting the default path

| Identifier | What | Affected | Fixed |
|---|---|---|---|
| CVE-2026-17543 | SQL injection in ext-pgsql `pg_insert()`, `pg_update()`, `pg_select()`, `pg_delete()` | any app using those helpers against PostgreSQL | PHP 8.2.33 / 8.3.33 / 8.4.24 / 8.5.9 (30 Jul 2026) |
| CVE-2026-24894 | FrankenPHP worker mode does not reset `$_SESSION` between requests — request B can read request A's session data before `session_start()` | FrankenPHP < 1.11.2 | 1.11.2 |
| CVE-2026-24895 | FrankenPHP CGI path splitting computes the `.php` index on a lowercased copy and applies it to the original path; Go's `ToLower` can change byte length, so `SCRIPT_FILENAME` misaligns and a non-PHP file executes | FrankenPHP < 1.11.2 | 1.11.2 |
| GHSA-v3ph-cgqh-r8p5 | FrankenPHP worker mode skips session reset when ext-session is a **shared module** (`#ifdef HAVE_PHP_SESSION` is false at build time); `PS(id)` survives, so `session_start()` reuses the previous request's session ID | FrankenPHP ≤ 1.12.4 | later 1.12.x — check the release notes for your build |
| GHSA-cj57-c655-p798 | Official FrankenPHP Docker images shipped a placeholder `index.php` running `phpinfo()`, disclosing `DATABASE_URL`, `APP_SECRET`, API keys, ini directives, and paths | FrankenPHP Docker images ≤ 1.12.4 | rebuild derivative images against the patched base — pulling the base alone does not fix already-built layers |

Two structural lessons, not just version bumps: **PHP patch releases carry security fixes that never get a CVE** (the 8.5.9 changelog named 4 CVEs; the diff carried more), so track the branch, not the CVE feed. And **worker-runtime state resets are a security control**, not a performance detail — a missed reset is cross-user data disclosure.

### Deserialization

`unserialize()` on anything a client can influence is remote code execution when a gadget chain exists in your dependency tree. It runs `__wakeup`, `__destruct`, and `__unserialize` on attacker-chosen classes.

```php
// WRONG — any client-supplied string
$data = unserialize($_COOKIE['prefs']);

// RIGHT — use JSON for client-facing data
$data = json_decode($request->cookies->get('prefs') ?? '', true, 512, JSON_THROW_ON_ERROR);

// If you truly must unserialize, forbid object instantiation
$data = unserialize($blob, ['allowed_classes' => false]);
```

The same rule covers `phar://` — before PHP 8, stream functions on a `phar://` path unserialized its metadata. Never pass a user-controlled path to `file_exists`, `is_file`, `getimagesize`, or `file_get_contents`.

### SSRF — `parse_url()` is not a validator

`parse_url()` implements no standard. It does not validate. It disagrees with cURL and with browsers on edge cases, and that disagreement is the bypass: your allow-list check reads one host, the HTTP client connects to another.

- **PHP 8.5 ships a core `uri` extension** with two standards-compliant parsers: `Uri\Rfc3986\Uri` and `Uri\WhatWg\Url`. For anything a browser or a user typed, use `Uri\WhatWg\Url` — it handles IDNA/Unicode hosts and percent-encoding the way browsers do, normalizes dot-segments, and exposes `getOrigin()` for origin comparison. On 8.4 and below, use a maintained URI library, not `parse_url()`.
- Correct parsing is necessary, not sufficient. You still need: scheme allow-list (`https` only), host allow-list or a deny-list of private/link-local ranges resolved from DNS (`127.0.0.0/8`, `10/8`, `172.16/12`, `192.168/16`, `169.254.0.0/16`, `::1`, `fc00::/7`), **re-validation after every redirect**, and a short connect timeout.
- **The metadata endpoint is the payload that matters.** `169.254.169.254` (AWS/GCP/Azure IMDS) returns credentials to any process that can make an HTTP request. Block the whole link-local range, and require IMDSv2 on AWS.
- With cURL: set `CURLOPT_FOLLOWLOCATION => false` and follow redirects yourself so each hop is re-checked, and pin `CURLOPT_PROTOCOLS_STR => 'https'` so a redirect cannot switch to `file://` or `gopher://`.

### Injection

- **SQL:** use prepared statements with bound parameters. Set `PDO::ATTR_EMULATE_PREPARES => false` so the driver, not PHP's string interpolation, does the binding — emulated prepares re-introduce injection with some charsets. **Identifiers cannot be bound.** Any dynamic table name, column name, or `ORDER BY` direction must come from a hard-coded allow-list, never from the request.
- **Command:** `escapeshellarg()` is locale-dependent and does not make a shell string safe in general. Prefer `proc_open()` with an **array** command (PHP 7.4+), which skips the shell entirely. Never interpolate user input into `exec`, `shell_exec`, `system`, `passthru`, or backticks.
- **Header/CRLF:** `header()` rejects `\r\n` since PHP 5.1.2, but mail headers do not go through `header()`. A `\r\n` in an address field injects a `Bcc:` — see the Laravel CRLF advisory in `mir-backend-php-laravel`. Strip `\r` and `\n` from anything that reaches a mail transport.
- **Template:** never compile a user-supplied string as a template (`Twig\Environment::createTemplate()`, Blade `render` on user input). That is server-side template injection and it is remote code execution.

### Path traversal in file serving and uploads

```php
// WRONG — basename() does not stop a traversal that survives decoding
$path = $baseDir . '/' . basename($_GET['file']);

// RIGHT — resolve, then verify the resolved path is still inside the base
$real = realpath($baseDir . '/' . $userPath);
$base = realpath($baseDir);
if ($real === false || !str_starts_with($real, $base . DIRECTORY_SEPARATOR)) {
    throw new RuntimeException('Path outside allowed directory');
}
```

For uploads: always `move_uploaded_file()` (it verifies the file came from an upload); **never trust `$_FILES['x']['type']`** — it is client-supplied. Sniff with `finfo_file()` and check against an allow-list. Generate the stored filename yourself; never derive it from the client name or extension. Store outside the document root and serve through a controller. If the upload directory must be inside the web root, deny PHP execution there in the web server config — `security.limit_extensions` in the FPM pool does not help if the file has a `.php` extension.

### Secret and PII leakage

- `.env` must never be reachable over HTTP. Deny it in nginx/Apache and verify with a request. If it was ever served, every secret in it is burned.
- Never ship `phpinfo()` — not as a health check, not behind an "internal" path. It prints the entire environment.
- Log the request ID, not the request body. `Log::info($request->all())` writes passwords and tokens into your log pipeline in plaintext.
- Error responses in production must carry a correlation ID and nothing else. `display_errors=Off` plus a framework exception handler that returns a generic body.

### Supply chain (Composer)

- Commit `composer.lock`. Deploy with `composer install --no-dev --prefer-dist`, never `composer update` on a production host.
- Run `composer audit --locked` in CI. It reads the lock file only, so it needs no install step. Data comes from the PHP Security Advisories Database.
- **Composer 2.9 blocks insecure packages during resolution by default** (`audit.block-insecure`, default `true`) on `update`, `require`, `remove`, and `install` without a lock file. Do not switch it off; `--no-audit` does not disable it in 2.9.0. Composer 2.10 moves these keys under a new `policy` block.
- Enumerate `config.allow-plugins` explicitly. Never set it to `true` — that lets any transitive dependency register a Composer plugin and run code at install time.
- Consider `--no-scripts` for first-time installs of untrusted packages; post-install scripts execute arbitrary PHP.
- **Packagist tags are mutable enough to matter.** In May 2026 an attacker republished 700+ versions of four `laravel-lang` localization packages under historical tags, then dissociated the tags from the malicious commits. Version numbers alone did not identify affected installs. Pin by lock file, and scan install history rather than trusting a version string.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** confirm the PHP branch (8.4/8.5 vs. an EOL one) and the process model (FPM vs. FrankenPHP worker mode / Octane / Swoole / RoadRunner). If a worker runtime: audit static state and container bindings before writing any framework code. Size FPM workers to available memory. Flag slow/durable work as queue-bound, not inline.
- **Gate 6 (implementation):** no static caches expecting cross-request persistence; no unbounded `all()`; both `max_execution_time` and `request_terminate_timeout` set; opcache tuned and `opcache.jit` set explicitly if JIT is wanted on 8.4+; `display_errors=Off`; explicit error and exception handlers; no `@`-suppression; `ext-pcntl` present in worker images.
- **Gate 7 (review):** reliability-reviewer checks items 1–8. security-reviewer walks the Security section: php.ini table, the advisory table against the deployed versions, `unserialize` on client input, URL validation, identifier allow-lists, upload handling, `composer audit --locked`.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability principles) → **up** to `mir-backend`.
- A specific library's mechanics (Eloquent N+1, Doctrine Unit of Work, Laravel Queues, Symfony Serializer groups) → **down** to the framework module (`mir-backend-php-<framework>`).
- **Here:** only what every PHP backend shares because of the Zend Engine and the FPM/worker process model — shared-nothing lifecycle, worker sizing, state bleed and superglobal resets under worker runtimes, resource limits, opcache/JIT, persistent PDO, signal handling, error model, and php.ini/Composer-level security.
- A different runtime (Node, Go, JVM…) → its own `mir-backend-<runtime>` tier. Never widen this one.
