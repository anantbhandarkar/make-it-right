---
name: mir-backend-rust
description: "Make It Right (Rust runtime tier). Async Rust on Tokio runtime reliability footguns that are shared across EVERY Rust backend framework (Axum, Actix-web, Warp, Poem) — distinct from the generic backend gates and from any one framework's mechanics. Covers: blocking the async runtime (std::thread::sleep / blocking I/O inside async tasks starves Tokio worker threads), holding a std::sync::MutexGuard across an .await point (compile error or deadlock), cancellation safety (futures dropped at any .await under timeout/select!/disconnect leaving partial state), panic-poisoned Mutexes, Arc-based shared state with 'static bounds on spawned tasks, bounded vs unbounded channels for backpressure, and timeout discipline on every outbound call. TRIGGER when the backend runtime is Rust — sits between mir-backend (generic) and the framework module (e.g. mir-backend-rust-axum). SKIP for Python/Node/JVM/Go/.NET/Ruby/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics (those live in the framework module)."
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

### 2. Holding a guard across `.await` — compile error or deadlock

`std::sync::MutexGuard` is `!Send`. Holding it across an `.await` makes the enclosing future `!Send`, which means it **cannot be spawned** on a multi-threaded Tokio runtime (`tokio::spawn` requires `Send`). The compiler catches this — but AI often "fixes" it by switching to `Arc<tokio::sync::Mutex<T>>` everywhere, which is overkill.

The right rule:

- **Drop the guard before awaiting.** Scope the lock into a block; do the await after the block closes.
- Use `tokio::sync::Mutex` **only** when the guard genuinely needs to be held across an `.await` (e.g. you're performing an async operation atomically while holding the lock). It is slower than `std::sync::Mutex` — default to `std` and scope tightly.
- `std::sync::RwLock` has the same `!Send` guard constraint.

```rust
// WRONG — guard held across .await, won't compile on multi-thread runtime
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

## How this slots into the pipeline

- **Gate 0/5 (model choice):** Confirm the async runtime is Tokio. State the concurrency model (async I/O, `spawn_blocking` for CPU/blocking, rayon for parallel CPU). A blocking call on a Tokio thread is a runtime-level defect — flag it before any framework-specific review.
- **Gate 6 (implementation):** no blocking calls inside `async fn`; guards dropped before awaits; `Arc` for shared state; bounded channels; timeout on every outbound call.
- **Gate 7 (review):** the reliability-reviewer checks items 1–7 here for any Tokio-based Rust service; framework mechanics are in the framework module.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability) → **up** to `mir-backend`.
- A specific library's mechanics (Axum extractors/state, Actix workers/`web::Data`, tower middleware) → **down** to the framework module (`mir-backend-rust-<framework>`).
- **Here:** only what every async Rust backend shares because of Tokio — blocking the runtime, guard-across-await, cancellation safety, panic/poison, `Arc`+'static, bounded channels, timeout discipline.
- A different runtime (Node, Go, Python…) → its own `mir-backend-<runtime>` tier. Never widen this one.
