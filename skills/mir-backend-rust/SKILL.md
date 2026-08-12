---
name: mir-backend-rust
description: "Make It Right (Rust runtime tier). Async Rust on Tokio runtime reliability footguns that are shared across EVERY Rust backend framework (Axum, Actix-web, Warp, Poem) — distinct from the generic backend gates and from any one framework's mechanics. Covers: blocking the async runtime (std::thread::sleep / blocking I/O inside async tasks starves Tokio worker threads), holding a std::sync::MutexGuard across an .await point (Send error on a multi-thread runtime, silent deadlock on a current-thread one), cancellation safety (futures dropped at any .await under timeout/select!/disconnect leaving partial state), panic-poisoned Mutexes, Arc-based shared state with 'static bounds on spawned tasks, async fn in traits and the still-unsolved Send-bound problem, spawn_blocking thread-pool exhaustion, bounded vs unbounded channels for backpressure, and timeout discipline on every outbound call. TRIGGER when the backend runtime is Rust — sits between mir-backend (generic) and the framework module. SKIP for Python/Node/JVM/Go/.NET/Ruby/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for one library's mechanics: Axum extractor order, State/FromRef and Tower layers belong to mir-backend-rust-axum; the Actix App-factory worker trap, web::Data and ResponseError belong to mir-backend-rust-actix. Load this tier alongside them, never instead of them."
trigger: /mir-backend-rust
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-rust · Make It Right (Rust runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-rust-axum`) knows the **library's mechanics**. This tier owns what is true for **all Rust async backends because they run on Tokio** — the concurrency model and ownership rules that Axum, Actix-web, Warp, and Poem all inherit.

**Runtime assumed:** async Rust on Tokio (current-thread or multi-thread scheduler). The notes hold for any Tokio-based framework. Load order: `mir-backend` → `mir-backend-rust` → `<framework module>`.

**Versions verified 13 Aug 2026** — state these, don't guess:

| Thing | Current | Note |
|---|---|---|
| Rust stable | 1.97.1 (1.97.0 released 2026-07-09) | 1.97 made v0 symbol mangling the default and added Cargo's `build.warnings` (`allow`/`warn`/`deny`), replacing `RUSTFLAGS=-Dwarnings` in CI |
| Rust edition | 2024 (stabilized in 1.85) | `edition = "2024"` in `Cargo.toml`. There is no 2027 edition yet |
| Tokio | 1.53.1 | Still 1.x. **There is no Tokio 2.0** — anything claiming a 2.x API is wrong |

Pin the MSRV in `Cargo.toml` with `rust-version = "..."`. Framework MSRVs already exceed some CI images: actix-web 4.13+ needs 1.88, sqlx 0.9 needs 1.94, axum 0.8.9 needs 1.80.

## The Tokio async Rust footguns AI walks into (framework-agnostic)

### 1. DON'T BLOCK THE ASYNC RUNTIME

Tokio runs async tasks on a fixed pool of worker threads (by default, one per CPU core). A blocking call on any of those threads **stalls every task scheduled to that thread** — the async "event loop" analog.

Blocking operations that must never appear inside an `async fn` on a Tokio thread:

- `std::thread::sleep` → use `tokio::time::sleep`
- Synchronous file I/O (`std::fs::read`, `std::fs::write`) → use `tokio::fs` or `spawn_blocking`
- Blocking DB or socket calls (any sync driver, e.g. `postgres` crate's sync API) → use async driver (`sqlx`, `tokio-postgres`) or `spawn_blocking`
- Heavy CPU computation (hashing, encoding, ML inference, large sort) — blocks a Tokio thread even though it's "not I/O" → use `tokio::task::spawn_blocking` (runs on a separate blocking thread pool) or a dedicated `rayon` threadpool

```rust
// WRONG — blocks a Tokio worker thread
async fn handler() -> &'static str {
    std::thread::sleep(std::time::Duration::from_secs(1)); // starves executor
    "done"
}

// RIGHT — yields to the runtime
async fn handler() -> &'static str {
    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
    "done"
}

// RIGHT — offload blocking I/O / CPU
async fn hash_password(pw: String) -> String {
    tokio::task::spawn_blocking(move || bcrypt::hash(pw, 12).unwrap())
        .await
        .unwrap()
}
```

AI routinely copies sync stdlib code into async handlers without noticing the runtime impact. Flag every `std::thread::sleep`, `std::fs`, and sync driver call found inside `async fn`.

**`spawn_blocking` is not free — it has a hard cap of 512 threads.** `max_blocking_threads` defaults to 512 (verified in `tokio::runtime::Builder`, 1.53.1). Each blocking thread is a real OS thread with its own stack. Two consequences:

- Put a per-request `spawn_blocking` call on a hot path and 512 concurrent slow requests exhaust the pool. Call 513 queues with no timeout and no backpressure signal — it looks like a hang, not an error.
- The blocking pool is shared by `tokio::fs`, `spawn_blocking`, and anything else that uses it. One slow consumer starves the rest.

Gate blocking work behind a `tokio::sync::Semaphore` sized to what the downstream resource can actually take, and set `max_blocking_threads` explicitly rather than inheriting 512. For parallel CPU work prefer a `rayon` pool with its own bounded queue — `spawn_blocking` is for blocking *I/O*, not for fan-out compute.

### 2. Holding a guard across `.await` — `Send` error on one runtime, silent deadlock on another

`std::sync::MutexGuard` is `!Send`. Holding it across an `.await` makes the enclosing future `!Send`.

**The compiler only catches this where a `Send` bound is actually required.** Do not rely on it as a safety net:

| Where the future runs | Held guard across `.await` | Result |
|---|---|---|
| `tokio::spawn` on multi-thread runtime | rejected | compile error — `Send` is required |
| Axum handler (multi-thread Tokio) | rejected | compile error — handlers must be `Send` |
| `tokio::task::spawn_local` / `LocalSet` | **accepted** | compiles; deadlocks if the same task re-locks |
| Actix-web handler (single-threaded `actix-rt` worker) | **accepted** | compiles; deadlocks and stalls the whole worker |
| `current_thread` runtime, `block_on` | **accepted** | compiles; deadlocks |

So on Actix and any `LocalSet` code the error surfaces as a hung worker in production, not a build failure. Review for it by hand. AI often "fixes" the compile error by switching to `Arc<tokio::sync::Mutex<T>>` everywhere, which is overkill and slower.

The right rule:

- **Drop the guard before awaiting.** Scope the lock into a block; do the await after the block closes.
- Use `tokio::sync::Mutex` **only** when the guard genuinely needs to be held across an `.await` (e.g. you're performing an async operation atomically while holding the lock). It is slower than `std::sync::Mutex` — default to `std` and scope tightly.
- `std::sync::RwLock` has the same `!Send` guard constraint.

```rust
// WRONG — guard held across .await
async fn increment(state: Arc<std::sync::Mutex<u64>>) {
    let mut guard = state.lock().unwrap();
    some_async_work().await;   // compiler error: future is !Send
    *guard += 1;
}

// RIGHT — release before awaiting
async fn increment(state: Arc<std::sync::Mutex<u64>>) {
    some_async_work().await;   // await first, or...
    let mut guard = state.lock().unwrap();
    *guard += 1;
    // guard drops here, before any subsequent await
}

// ALSO RIGHT — scope the lock to prevent guard crossing .await
async fn read_then_work(state: Arc<std::sync::Mutex<Data>>) {
    let snapshot = {
        let guard = state.lock().unwrap();
        guard.clone() // copy out what you need
    }; // guard dropped here
    do_async_work(snapshot).await;
}
```

### 3. Cancellation safety — futures are dropped at any `.await`

A Tokio future can be dropped (cancelled) at **any `.await` point** by:

- `tokio::time::timeout` expiring
- A `tokio::select!` branch losing the race
- A client disconnecting mid-request
- Task being aborted via `JoinHandle::abort`

This means **"mutate A; `.await`; mutate B"** is NOT atomic — if the future is dropped after mutating A but before mutating B, you get partial state.

```rust
// DANGEROUS — partial mutation if dropped at the .await
async fn transfer(db: &Db, from: u64, to: u64, amount: i64) {
    db.debit(from, amount).await;  // dropped here? 'from' debited, 'to' never credited
    db.credit(to, amount).await;
}

// RIGHT — wrap in a DB transaction so partial mutations roll back
async fn transfer(db: &Db, from: u64, to: u64, amount: i64) {
    db.transaction(|tx| async move {
        tx.debit(from, amount).await?;
        tx.credit(to, amount).await?;
        Ok(())
    }).await.unwrap();
}
```

For `select!`: the branches that lose are **dropped immediately** — any state they were accumulating is lost. If a branch does I/O you care about, use cancellation-safe primitives (`recv` on a channel is cancellation-safe; `read` on an `AsyncRead` may not be). Consult the Tokio docs' cancellation-safety table for each primitive.

`tokio::select!` also drops the other branch futures on every iteration — this is not a bug, but AI often writes stateful code inside select branches assuming they survive across iterations.

### 4. Panics, poisoned Mutexes, and task boundaries

- A panic inside a `tokio::spawn`'d task **unwinds only that task**. The `JoinHandle` returns `Err(JoinError)` — you must check it to know the task panicked. Ignoring `JoinHandle`s means silent failures.
- **Dropping a `JoinHandle` detaches the task, it does not cancel it.** `tokio::spawn(...)` with the result unused compiles and keeps running — through the response, through shutdown, until the runtime is dropped mid-write. Cancelling requires `handle.abort()` or a `CancellationToken`. Own your tasks with a `JoinSet` or `tokio_util::task::TaskTracker` so shutdown can wait for them within a bounded grace period.
- A panic while **holding a `std::sync::Mutex`** poisons the mutex. Every subsequent `lock()` returns `Err(PoisonError)`. Either call `.unwrap_or_else(|e| e.into_inner())` (accepting the possibly-corrupt state) or treat poison as a fatal error.
- Decide on a panic strategy at task boundaries: either catch panics via `JoinHandle`, use `catch_unwind`, or treat a task panic as a process-fatal error and let a supervisor restart. Don't silently swallow `JoinHandle` results.

```rust
let handle = tokio::spawn(async move { risky_work().await });
match handle.await {
    Ok(result) => process(result),
    Err(e) if e.is_panic() => tracing::error!("task panicked: {:?}", e),
    Err(e) => tracing::error!("task cancelled: {:?}", e),
}
```

### 5. Shared state — `Arc` and `'static` bounds on spawned tasks

`tokio::spawn` requires the future to be `'static` — it cannot borrow from the caller's stack. AI frequently tries to pass `&self` or local references into a `spawn`'d closure and hits a lifetime error, then "fixes" it with `unsafe` or unnecessary cloning.

The correct pattern:

- Share data via `Arc<T>` (cheap clone, reference-counted heap allocation).
- Mutable shared data via `Arc<Mutex<T>>` or `Arc<RwLock<T>>`.
- Move the `Arc` clone into the task — not a reference to it.

```rust
let state = Arc::new(Mutex::new(vec![]));

let state_clone = Arc::clone(&state);
tokio::spawn(async move {
    state_clone.lock().unwrap().push(42); // owns the clone, 'static ok
});
```

For read-heavy workloads, prefer `Arc<tokio::sync::RwLock<T>>` or `Arc<std::sync::RwLock<T>>` (drop before awaiting) over a `Mutex` to allow concurrent readers.

### 6. Bounded channels for backpressure — unbounded channels can OOM

`tokio::sync::mpsc::unbounded_channel` never exerts backpressure — a slow consumer and fast producer will buffer messages in memory until the process OOM-kills. **Default to bounded channels** (`tokio::sync::mpsc::channel(capacity)`) so that the sender blocks (awaits) when the buffer is full, propagating backpressure upstream.

```rust
// RISKY — unbounded, can grow without limit
let (tx, rx) = tokio::sync::mpsc::unbounded_channel::<Work>();

// RIGHT — bounded; sender .await's when full, applies backpressure
let (tx, rx) = tokio::sync::mpsc::channel::<Work>(256);
```

Choose capacity based on acceptable latency buffering and memory budget. A capacity of 1 maximizes backpressure responsiveness; very large capacities approach unbounded behavior.

### 7. Timeouts on every outbound call — connection pool sizing

Every call that crosses a process boundary (HTTP, DB, Redis, gRPC, DNS) must have a timeout. Without one, a slow dependency holds a Tokio task (and possibly a connection from the pool) open indefinitely, leading to exhausted pools and cascading failures.

```rust
use tokio::time::{timeout, Duration};

let result = timeout(
    Duration::from_secs(5),
    http_client.get(url).send()
).await
.map_err(|_| AppError::Timeout)?  // timeout elapsed
??;                                // reqwest error
```

Connection pool sizing (`deadpool`, `sqlx::Pool`, `bb8`): `max_connections` should reflect the actual DB/service concurrency limit, not be set to a huge number "just to be safe." Too many pool connections starve the DB. Too few starve the app under load. Set `connection_timeout` on pool acquisition — a task that can't get a connection should fail fast, not wait forever.

### 8. `async fn` in traits — stable since 1.75, but the `Send` bound problem is still not solved

AI writes `#[async_trait]` out of habit, or writes a bare `async fn` in a trait and is surprised when `tokio::spawn` rejects it. Both mistakes come from the same gap. Status as of Rust 1.97:

| Feature | Stable? | What it means for you |
|---|---|---|
| `async fn` in trait (AFIT) / RPITIT | **Yes**, since 1.75 | Write `async fn` in traits directly. No macro needed for static dispatch |
| `dyn Trait` with an `async fn` method | **No** | `error[E0038]: the trait is not dyn compatible`. You cannot put it in a `Box<dyn …>` or `Vec<Box<dyn …>>` |
| Return-type notation — `T: Trait<method(..): Send>` | **No** | The stabilization PR (rust-lang/rust#138424) was **closed unmerged**; tracking issue #109417 is still open. Nightly only |
| `async_fn_in_dyn_trait` | **No** | Nightly feature, tracking issue #133119 |

Because RTN is not stable, you **cannot** write a generic bound saying "the future this trait method returns is `Send`." That is exactly what you need to `tokio::spawn` work behind a trait. Pick deliberately:

| You need | Use | Cost |
|---|---|---|
| Static dispatch, no spawning | plain `async fn` in trait | none — this is the default, prefer it |
| Static dispatch + `Send` futures for `tokio::spawn` | `#[trait_variant::make(Send)]` | generates a `Send` variant; **static dispatch only** |
| `dyn Trait` (plugin registry, `Vec<Box<dyn Repo>>`, mocking) | `#[async_trait]` — still current and correct | one `Box::pin` allocation per call |
| `dyn` without a macro | hand-write `-> Pin<Box<dyn Future<Output = T> + Send + '_>>` | verbose, but no proc-macro dependency |

`#[async_trait]` is **not** deprecated or obsolete. It is the right answer whenever you genuinely need `dyn`. What changed is that it is no longer the *default* — reaching for it on a trait you only ever use generically adds a heap allocation per call for nothing.

Note the framework split: **axum removed `#[async_trait]` from `FromRequest`/`FromRequestParts` in axum-core 0.5** (see `mir-backend-rust-axum`), so custom extractors written for 0.7 will not compile until you delete the attribute.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** Confirm the async runtime is Tokio. State the concurrency model (async I/O, `spawn_blocking` for CPU/blocking, rayon for parallel CPU). A blocking call on a Tokio thread is a runtime-level defect — flag it before any framework-specific review.
- **Gate 6 (implementation):** no blocking calls inside `async fn`; guards dropped before awaits; `Arc` for shared state; bounded channels; timeout on every outbound call; `#[async_trait]` only where `dyn` is genuinely needed.
- **Gate 7 (review):** the reliability-reviewer checks items 1–8 here for any Tokio-based Rust service; the security-reviewer checks the Security section below; framework mechanics are in the framework module. Run `cargo audit` (or `cargo deny check advisories`) as part of this gate — it is the only step that catches the advisories listed below.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability) → **up** to `mir-backend`.
- A specific library's mechanics (Axum extractors/state, Actix workers/`web::Data`, tower middleware) → **down** to the framework module (`mir-backend-rust-<framework>`).
- **Here:** only what every async Rust backend shares because of Tokio — blocking the runtime, guard-across-await, cancellation safety, panic/poison, `Arc`+'static, async traits, bounded channels, timeout discipline.
- A different runtime (Node, Go, Python…) → its own `mir-backend-<runtime>` tier. Never widen this one.

## Security

Runtime-level and shared-crate security footguns. Framework-specific ones (CORS presets, extractor limits, static-file serving) are in `mir-backend-rust-axum` / `mir-backend-rust-actix`.

Rust's borrow checker prevents memory-safety bugs in *your* safe code. It does not prevent authorization bugs, injection, SSRF, or the `unsafe` blocks inside your dependency tree. Every item below has bitten a Rust web service.

### Advisories on the default web stack — check these first

Verified against the RustSec advisory database on 13 Aug 2026. `bytes` and `slab` arrive under both Axum and Actix-web whether or not you named them in `Cargo.toml`; the rest apply only once you pick the crate (a database driver, `rustls`, `tracing-subscriber`). Confirm what you actually have with `cargo tree -i <crate>`.

| Advisory | Crate | What breaks | Fixed in |
|---|---|---|---|
| **RUSTSEC-2026-0007** / CVE-2026-25541 | `bytes` | Integer overflow in `BytesMut::reserve` sets `cap` beyond the real allocation; `spare_capacity_mut()` then builds out-of-bounds slices. Wraps silently in release builds, panics in debug. `bytes` sits under `hyper`, `axum`, `actix-web`, `tokio-util` | **>= 1.11.1** |
| **RUSTSEC-2025-0055** / CVE-2025-58160 | `tracing-subscriber` | ANSI escape sequences in logged user input reach the terminal — log poisoning, and a delivery path for terminal-emulator exploits | **>= 0.3.20** |
| **RUSTSEC-2025-0023** | `tokio` | `broadcast` clones values in parallel while only requiring `T: Send`. Unsound for `Send`-but-`!Sync` payloads | >= 1.44.2 (backports: 1.38.2, 1.42.1, 1.43.1) |
| **RUSTSEC-2025-0047** / CVE-2025-55159 | `slab` | `get_disjoint_mut` bounds-checks capacity instead of length — reads uninitialized memory. Only 0.4.10 is affected | **>= 0.4.11** |
| **RUSTSEC-2024-0363** | `sqlx` | Protocol-level SQL injection. A value over 4 GiB overflows the length prefix and the server reads the remainder as protocol commands — parameterized queries do not save you | **>= 0.8.1** |
| **RUSTSEC-2026-0178** | `tokio-postgres` | `Row::get` / `try_get` panic on a `DataRow` with fewer fields than declared columns, aborting the task. A malicious or compromised server is enough | **>= 0.7.18** |
| **RUSTSEC-2026-0104** | `rustls-webpki` | Reachable panic parsing a CRL with an empty `BIT STRING` in `onlySomeReasons`, **before** the CRL signature is verified. Only affects apps that use CRLs | **>= 0.103.13** |
| RUSTSEC-2026-0098 / -0099 | `rustls-webpki` | Name-constraint bypasses: URI name constraints silently ignored; permitted-subtree DNS constraints wrongly accepted for wildcard certs | >= 0.103.12 |
| RUSTSEC-2026-0097 | `rand` | `rand::rng()` is unsound with a custom `log` logger when the `log` + `thread_rng` features are both on | >= 0.10.1 / >= 0.9.3 |

Run `cargo audit` in CI on every build, not just on dependency bumps — an advisory can be filed against a version you already shipped. `cargo deny check advisories` does the same and also enforces license and duplicate-version policy.

### Secret and PII leakage through `Debug` and error types

This is the most common Rust-specific leak, and it is mechanical.

- **`#[derive(Debug)]` on a config or credential struct prints the secret.** Any `tracing::error!("{config:?}")`, any `.unwrap()` panic message, any `anyhow` chain that captures it. Wrap secrets in a newtype with a hand-written `Debug` that prints `[redacted]`, or use `secrecy::SecretString`, whose `Debug`/`Display` are redacted by construction and which zeroizes on drop.
- **`sqlx::Error`, `tokio_postgres::Error` and `reqwest::Error` have informative `Display` impls.** `sqlx::Error::Database` carries the server message, which can include column values from a constraint violation. `reqwest::Error` can carry the full URL including a query-string token. Never write `(StatusCode::INTERNAL_SERVER_ERROR, e.to_string())`. Log the detail server-side with a correlation ID; return the ID.
- **`anyhow::Error` has three formats and only one is narrow.** `{}` / `to_string()` prints just the outermost context, `{:#}` prints the whole cause chain, and `{:?}` adds the backtrace. A blanket `impl IntoResponse for anyhow::Error` that formats any of them into the body ships internals to the client — and the innocuous-looking `{}` still leaks whatever string the last `.context(...)` call put there.
- **Panic messages go to stderr and often to the log aggregator.** `.expect("failed to connect to postgres://user:pass@host/db")` writes the password to your logs forever.
- `RUST_BACKTRACE=1` in a production container turns every error into a stack trace. Set it deliberately, and never let backtraces reach an HTTP response body.

### Injection

- **SQL:** use `sqlx::query!` / `query_as!` (compile-time checked, always parameterized) or runtime `query()` with `.bind()`. `format!` into a SQL string is injection, and the compile-time macros cannot check a string you built at runtime. Identifiers (table and column names) **cannot** be parameterized by any driver — if one has to be dynamic, match it against a hard-coded allow-list of `&'static str`, never interpolate user input.
- Cap request body size *before* parsing (see the framework module). RUSTSEC-2024-0363 is the reason: an unbounded body plus a driver that truncates a length cast is protocol smuggling even with bound parameters.
- **Command:** `std::process::Command::new("sh").arg("-c").arg(user_input)` is shell injection. `Command` without a shell passes args directly and does not glob or word-split — pass the program and each argument separately and never route through `sh -c`.
- **Log injection:** upgrade `tracing-subscriber` to >= 0.3.20 (RUSTSEC-2025-0055 above), and log user-controlled values as structured fields (`tracing::info!(user_email = %email, "login")`), not interpolated into the message. Structured fields keep the value in its own key where a log pipeline can escape it.
- **Template:** `askama` and `minijinja` HTML-escape by default. `askama`'s `{{ value|safe }}` and `minijinja`'s `|safe` filter turn escaping off — treat every `|safe` as a review item. `tera` escapes only for files with HTML-ish extensions; rendering a template from a string or a `.txt` file does **not** escape.

### SSRF from user-supplied URLs

`reqwest` follows up to 10 redirects by default and resolves whatever hostname it is given. If a URL comes from a request body — webhook targets, avatar-import, "fetch my RSS feed" — it can reach your cloud metadata endpoint (`169.254.169.254`, and `metadata.google.internal`), your Kubernetes API, or anything on the pod network.

Mechanically:

1. `reqwest::redirect::Policy::none()`, or a custom policy that re-validates every hop. Validating only the first URL is bypassed by a redirect — this is the single most common SSRF mistake.
2. Parse with `url::Url` and require the scheme to be `https` (reject `file:`, `gopher:`, `data:`).
3. Resolve the host yourself and reject the resulting `IpAddr` if `is_loopback()`, `is_private()`, `is_link_local()`, `is_unspecified()`, or is in `100.64.0.0/10`. Check the resolved address, not the string — `http://[::ffff:169.254.169.254]` and decimal-encoded IPs both parse to blocked addresses but do not look like them.
4. Prefer an egress allow-list or a dedicated proxy over in-process filtering. DNS rebinding beats step 3 because the name resolves twice — once when you check, once when `reqwest` connects.
5. Always set `.timeout()` on the client. An SSRF probe against a black-holed internal IP otherwise pins a task for the OS TCP timeout.

### Deserialization of untrusted input

- `serde` itself is safe, but `#[serde(deny_unknown_fields)]` is **not** the default. Without it, unknown keys are silently dropped — which is what makes mass assignment possible (see the framework module for the request-body side).
- `#[serde(flatten)]` disables `deny_unknown_fields` on the containing struct. Combining them does not error; the check just stops applying.
- Untagged and internally-tagged enums buffer the entire input to try each variant. Deeply nested untrusted JSON against an untagged enum is a CPU denial-of-service.
- `serde_json` **does** enforce a nesting limit — 128 by default — and returns `ErrorCode::RecursionLimitExceeded` rather than overflowing the stack. You lose that only by enabling the `unbounded_depth` feature and calling `disable_recursion_limit()`; if a crate in your tree does that, it needs `serde_stacker` and an iterative drop, because dropping a deeply nested `Value` recurses too and no `catch_unwind` recovers a stack overflow.
- Never deserialize untrusted bytes into a type carrying `usize` field offsets, indices, or capacities that later index a slice. That is how a serialization bug becomes an out-of-bounds read.

### Concurrency defects that are security defects

- **Cancellation (item 3 above) is an authorization bug when the check and the effect are separated by an `.await`.** Verify permission, `.await`, then mutate — the client disconnects between them and you have a partially applied change with no audit record. Do the check and the effect in one database transaction.
- **A poisoned `Mutex` handled with `.unwrap_or_else(|e| e.into_inner())` continues on state a panic left half-updated.** If that state is a rate-limit counter, a session table, or a permission cache, you are now reading values that no code path ever intended to produce. For anything security-relevant, treat poison as fatal.
- Rate limiters and login-attempt counters kept in an `Arc<Mutex<HashMap<..>>>` are per-process. Run three replicas and the effective limit is 3x. Enforce in a shared store (Redis) or accept and document the multiplier.

### Supply chain

- **Commit `Cargo.lock` for binaries** — services, not libraries. Build and audit with `--locked` so CI fails on a lockfile that does not match `Cargo.toml` instead of silently resolving a new version.
- Note the sqlx 0.9 change: sqlx **removed `Cargo.lock` from git**, so `cargo install --locked sqlx-cli` no longer works. Use `cargo install sqlx-cli` (which always ignored the lockfile), or vendor your own lockfile if you need reproducible tool builds.
- Rust has no npm-style install scripts, but `build.rs` **runs arbitrary code at build time**, on your machine and in CI, before any test. Proc-macro crates execute at compile time too. A new dependency with a `build.rs` deserves the same scrutiny as a new binary.
- `cargo deny check bans` catches duplicate major versions of the same crate. Two versions of `bytes` or `rustls` in one tree means patching one leaves the other vulnerable — this is how an advisory stays live after you "upgraded."
- `unsafe` in a dependency is where memory-safety CVEs come from (three of the advisories above are exactly this). `cargo geiger` counts `unsafe` per crate; `#![forbid(unsafe_code)]` at the top of your own crates makes your side of the line auditable.
- Pin the toolchain with a `rust-toolchain.toml` so CI and developers compile with the same compiler.
