---
name: mir-backend-rust-actix
description: "Make It Right (Actix-web module). Actix-web + async Rust specific reliability augmentation. Use alongside mir-backend and mir-backend-rust when the target stack is Actix-web — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: the multi-worker app data trap (state constructed inside the App factory closure yields N independent copies, not one shared instance), web::Data<T> Arc semantics and the .app_data vs .data deprecation, worker-local single-threaded actix-rt execution (!Send types allowed but blocking still starves the worker), extractor configuration via JsonConfig/QueryConfig, and error handling via the ResponseError trait. TRIGGER only when the Rust backend framework is Actix-web — building, reviewing, or debugging an Actix-web handler, middleware, extractor, or app factory. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-rust (Tokio runtime concerns: blocking, cancellation safety, Arc/'static, backpressure, timeouts); this module only adds Actix-web library mechanics. SKIP for Axum, Warp, Poem, or any non-Actix Rust stack (those get their own mir-backend-rust-<framework> module), and for non-Rust runtimes."
trigger: /mir-backend-rust-actix
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-rust-actix · Make It Right (Actix-web)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-rust` (Tokio runtime model) → **this** (Actix-web library mechanics). Run the gates first; load the Rust runtime tier for blocking/cancellation/Arc concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (blocking the async runtime, cancellation safety, bounded channels, timeouts) live in `mir-backend-rust` — not here.**

**Stack assumed:** Actix-web (4.x) · actix-rt (Tokio-backed single-threaded workers) · async Rust.

## The Actix-web footguns AI walks into most

### 1. The multi-worker App Data trap — the defining Actix footgun

`HttpServer::new` accepts a **closure** (the App factory) and calls it **once per worker thread** (default: one worker per CPU core). Any state you **construct inside** the closure is created N times — yielding N completely independent copies. This is silent and compiles perfectly: there's no error, just N separate caches, N separate in-memory counters, or N separate connection pools that never coordinate.

```rust
// WRONG — a fresh Vec is created for each of the N workers
// Every worker has its own isolated list; requests are distributed
// across workers, so the list appears empty or inconsistent
HttpServer::new(|| {
    let state = web::Data::new(Mutex::new(Vec::<String>::new())); // N copies!
    App::new()
        .app_data(state)
        .route("/items", web::get().to(list_items))
})

// RIGHT — construct state OUTSIDE the closure, clone the Arc in
let state = web::Data::new(Mutex::new(Vec::<String>::new())); // one Arc

HttpServer::new(move || {
    App::new()
        .app_data(state.clone()) // clone the Arc, not the data
        .route("/items", web::get().to(list_items))
})
.workers(4)
.bind("0.0.0.0:8080")?
.run()
.await
```

The same rule applies to DB pools, Redis connections, caches, and any other shared resource. Build them once, outside `HttpServer::new`, wrap in `web::Data::new` (which is `Arc`), and `.clone()` the `Data` handle into each App factory invocation.

AI almost always builds state inside the closure because that's where all the App configuration lives — it looks natural. The bug only surfaces at runtime when you notice your in-memory state is silently partitioned.

### 2. `web::Data<T>` is an Arc — clone the handle, not the data

`web::Data<T>` is a newtype wrapper around `Arc<T>`. Cloning a `web::Data<T>` clones the Arc (cheap reference count bump) — the underlying `T` is shared. This is correct and expected:

```rust
// In the factory:
.app_data(pool.clone()) // clones the Arc wrapper, not the pool

// In a handler:
async fn get_user(pool: web::Data<PgPool>, path: web::Path<u64>) -> impl Responder {
    // pool.get_ref() gives &PgPool, or use Deref coercion
    let row = sqlx::query!("SELECT * FROM users WHERE id = $1", *path)
        .fetch_one(pool.get_ref())
        .await;
    ...
}
```

**`.data()` is deprecated** (removed in Actix-web 4). Use `.app_data(web::Data::new(...))`. The old `.data()` method exists in 3.x codebases — if you see it, update to `.app_data`.

### 3. Workers are single-threaded actix-rt — !Send types allowed, but blocking still starves the worker

Each Actix-web worker runs its own single-threaded `actix-rt` executor (a single-threaded Tokio runtime). Because the executor is single-threaded, handler futures do **not** need to be `Send` — you can use `Rc<T>`, `Cell<T>`, and other `!Send` types freely inside handlers. This is a deliberate design difference from Axum (which requires `Send` futures for multi-thread Tokio).

However: **a single-threaded executor means blocking work blocks the entire worker thread**, stalling all requests routed to that worker. The `mir-backend-rust` rule (don't block async tasks) applies here too, with one Actix-specific escape hatch:

- For blocking/CPU-heavy work inside an Actix handler, use **`web::block`** — it runs the closure on a separate blocking threadpool and returns a `Future`:

```rust
async fn compress_data(body: web::Bytes) -> actix_web::Result<web::Bytes> {
    let compressed = web::block(move || {
        // runs on blocking pool — won't stall the actix worker
        compress_sync(&body)
    })
    .await
    .map_err(|e| actix_web::error::ErrorInternalServerError(e))??;
    Ok(compressed)
}
```

Do not use `tokio::task::spawn_blocking` in Actix handlers — it works, but `web::block` is the idiomatic Actix-web API and integrates with the error model. `std::thread::sleep` and sync I/O inside handlers are still blocked.

### 4. Extractors and their configuration — `JsonConfig` limits and error shaping

Actix-web extractors (`Json<T>`, `Form<T>`, `Query<T>`, `Path<T>`) are configured via typed config objects registered with `.app_data()`. AI frequently forgets to set `JsonConfig` limits, accepting arbitrarily large bodies:

```rust
// Set body size limit and custom error handler for JSON extractor
let json_cfg = web::JsonConfig::default()
    .limit(1_048_576) // 1 MB max body
    .error_handler(|err, req| {
        let response = HttpResponse::BadRequest().json(json!({ "error": err.to_string() }));
        actix_web::error::InternalError::from_response(err, response).into()
    });

HttpServer::new(move || {
    App::new()
        .app_data(json_cfg.clone())
        .app_data(pool.clone())
        // ...
})
```

Similarly, `QueryConfig` and `PathConfig` accept `.error_handler` to return clean JSON errors instead of the default plain-text Actix error responses.

### 5. Error handling — implement `ResponseError`, not `unwrap`

Actix-web's error trait is `ResponseError` (not `IntoResponse` as in Axum). Implement it on your error type to produce consistent HTTP responses:

```rust
#[derive(Debug, thiserror::Error)]
enum AppError {
    #[error("not found: {0}")]
    NotFound(String),
    #[error("database error")]
    DbError(#[from] sqlx::Error),
    #[error("unauthorized")]
    Unauthorized,
}

impl ResponseError for AppError {
    fn status_code(&self) -> StatusCode {
        match self {
            AppError::NotFound(_)  => StatusCode::NOT_FOUND,
            AppError::DbError(_)   => StatusCode::INTERNAL_SERVER_ERROR,
            AppError::Unauthorized => StatusCode::UNAUTHORIZED,
        }
    }

    fn error_response(&self) -> HttpResponse {
        HttpResponse::build(self.status_code())
            .json(json!({ "error": self.to_string() }))
    }
}

async fn get_user(
    pool: web::Data<PgPool>,
    path: web::Path<u64>,
) -> Result<web::Json<User>, AppError> {
    let user = sqlx::query_as!(User, "SELECT * FROM users WHERE id = $1", *path as i64)
        .fetch_optional(pool.get_ref())
        .await
        .map_err(AppError::DbError)?
        .ok_or_else(|| AppError::NotFound(format!("user {} not found", *path)))?;
    Ok(web::Json(user))
}
```

Never `.unwrap()` inside a handler — panics in Actix-web handlers propagate as 500 responses (Actix catches panics per request by default) but suppress the actual error from structured logging and clients.

## How this slots into the core pipeline

- **Gate 5 (Design):** state construction outside `HttpServer::new` as the mandatory pattern; define `AppState`-equivalent as `web::Data`-wrapped values built before the factory; declare the error type implementing `ResponseError`; note `JsonConfig` limits.
- **Gate 6 (Implementation):** shared state built outside closure; `.app_data(data.clone())` inside factory; `web::block` for blocking/CPU work; `?` propagation through `AppError: ResponseError`; `JsonConfig` with explicit size limit and error handler.
- **Gate 7 (Review):** verify no state construction inside `HttpServer::new`; no `unwrap` in handlers; `JsonConfig` limits present; no `.data()` (deprecated) usage; `web::block` used for any blocking work.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Actix-web library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Node too (idempotency, invariants, gates, observability)? → **generic core** (`mir-backend`).
- True for every async Rust backend on Tokio (blocking, guard-across-await, cancellation, Arc/'static, channels, timeouts)? → **runtime tier** (`mir-backend-rust`).
- A mechanical footgun of *this library* (multi-worker App Data trap, `web::Data` Arc semantics, `web::block`, `JsonConfig` limits, `ResponseError`)? → **here**.
- A *different* Rust framework (Axum, Warp) → its own `mir-backend-rust-<framework>` module. Never widen this one.
