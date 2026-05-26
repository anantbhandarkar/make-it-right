---
name: mir-backend-rust-axum
description: "Make It Right (Axum module). Axum + Tower + async Rust specific reliability augmentation. Use alongside mir-backend and mir-backend-rust when the target stack is Axum — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: extractor ordering (body-consuming extractors must be last), typed State<T> vs Extension<T> and the FromRef sub-state pattern, implementing IntoResponse for error types, and Tower middleware layer ordering (outermost wraps first). TRIGGER only when the Rust backend framework is Axum — building, reviewing, or debugging an Axum handler, router, extractor, or middleware. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-rust (Tokio runtime concerns: blocking, guard-across-await, cancellation safety, Arc/'static, backpressure, timeouts); this module only adds Axum/Tower library mechanics. SKIP for Actix-web, Warp, Poem, or any non-Axum Rust stack (those get their own mir-backend-rust-<framework> module), and for non-Rust runtimes."
trigger: /mir-backend-rust-axum
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-rust-axum · Make It Right (Axum)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-rust` (Tokio runtime model) → **this** (Axum/Tower library mechanics). Run the gates first; load the Rust runtime tier for blocking/cancellation/Arc concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (blocking the Tokio runtime, guard-across-await, cancellation safety, bounded channels, timeouts) live in `mir-backend-rust` — not here.**

**Stack assumed:** Axum (current stable, 0.7+) · Tower middleware · async Rust on Tokio. Notes apply to `axum` with `tokio` and `tower`/`tower-http`.

## The Axum footguns AI walks into most

### 1. Extractor order — body-consuming extractors MUST be last

This is the #1 Axum compile/runtime trap. HTTP request bodies are streams: **reading the body consumes it**. Axum extractors that consume the body (`Json<T>`, `String`, `Bytes`, `Form<T>`, `Multipart`) must be the **last parameter** in the handler signature. Only **one** body-consuming extractor per handler is allowed — the request has one body.

Extractors that do NOT consume the body (they read headers, URI, path, query, or app state) can appear in any position: `Path<T>`, `Query<T>`, `State<T>`, `Extension<T>`, `TypedHeader<T>`, `ConnectInfo<T>`.

```rust
// WRONG — Json is not last; compilation may succeed but the extractor
// that follows Json will see an empty/already-consumed body
async fn create_user(
    Json(payload): Json<CreateUser>,  // consumes the body
    State(db): State<Db>,             // body already gone if Json is first
) -> impl IntoResponse { ... }

// RIGHT — body-consuming extractor is last
async fn create_user(
    State(db): State<Db>,             // reads app state, no body access
    Json(payload): Json<CreateUser>,  // LAST — consumes the body here
) -> impl IntoResponse { ... }

// RIGHT — path + query before body
async fn update_item(
    Path(id): Path<u64>,
    Query(params): Query<UpdateParams>,
    State(db): State<Db>,
    Json(body): Json<UpdateItem>,     // LAST
) -> impl IntoResponse { ... }
```

AI routinely puts `State` last (habit from other frameworks) and bumps `Json` to first or second — flag any handler where a body extractor is not the final argument.

### 2. `State<T>` vs `Extension<T>` — prefer `State`, use `FromRef` for sub-states

Axum provides two mechanisms for injecting app-wide data into handlers:

- **`State<T>`** (via `.with_state(value)` on the router): type-checked at compile time, zero-cost extraction, the idiomatic modern approach. Use this.
- **`Extension<T>`** (via `.layer(Extension(value))`): stored as a type-erased map in the request extensions, extracted at runtime — a missing extension panics at runtime, not compile time. Use only for middleware-injected data (e.g. authenticated user injected by an auth layer downstream handlers expect).

For large apps with multiple shared resources (DB pool, config, cache), define a single `AppState` struct and implement `FromRef<AppState>` for each sub-state so individual handlers can extract only what they need:

```rust
#[derive(Clone)]
struct AppState {
    db: PgPool,
    cache: RedisPool,
}

// Allow handlers to extract State<PgPool> directly from AppState
impl FromRef<AppState> for PgPool {
    fn from_ref(state: &AppState) -> Self {
        state.db.clone()
    }
}

// Handler only declares what it needs
async fn get_user(
    State(db): State<PgPool>,     // extracted via FromRef
    Path(id): Path<u64>,
) -> impl IntoResponse { ... }

// Router wired to the full AppState
let app = Router::new()
    .route("/users/:id", get(get_user))
    .with_state(AppState { db, cache });
```

`AppState` must implement `Clone` (cheaply — wrap expensive resources in `Arc` or use pool handles that are already `Clone`).

### 3. Error handling — implement `IntoResponse`, never unwrap in handlers

Handlers return `Result<impl IntoResponse, E>` where `E: IntoResponse`. **Do not `.unwrap()` or `.expect()` inside a handler** — a panic kills the task and returns a 500 with no body (or crashes the process depending on panic handler). Implement `IntoResponse` for your error type to produce consistent, controlled HTTP responses:

```rust
#[derive(Debug)]
enum AppError {
    NotFound(String),
    DbError(sqlx::Error),
    Unauthorized,
}

impl IntoResponse for AppError {
    fn into_response(self) -> Response {
        let (status, message) = match self {
            AppError::NotFound(msg)  => (StatusCode::NOT_FOUND, msg),
            AppError::DbError(e)     => {
                tracing::error!("db error: {e}");
                (StatusCode::INTERNAL_SERVER_ERROR, "internal error".into())
            }
            AppError::Unauthorized   => (StatusCode::UNAUTHORIZED, "unauthorized".into()),
        };
        (status, Json(json!({ "error": message }))).into_response()
    }
}

async fn get_user(
    State(db): State<PgPool>,
    Path(id): Path<u64>,
) -> Result<Json<User>, AppError> {
    let user = sqlx::query_as!(User, "SELECT * FROM users WHERE id = $1", id as i64)
        .fetch_optional(&db)
        .await
        .map_err(AppError::DbError)?
        .ok_or_else(|| AppError::NotFound(format!("user {id} not found")))?;
    Ok(Json(user))
}
```

Using `anyhow::Error` as the error type with a blanket `impl IntoResponse` is also valid for rapid development, but always ensure internal error details are not leaked to clients in production.

### 4. Tower middleware layer order — outermost wraps first, responses last

Tower middleware is composed as a **stack**: the layer added **outermost** (last in the builder chain or added first to `ServiceBuilder`) sees the **request first** and the **response last**. This is the reverse of what many frameworks call "middleware order."

```rust
let app = Router::new()
    .route("/", get(handler))
    .layer(
        ServiceBuilder::new()
            .layer(TraceLayer::new_for_http())     // outermost: sees req first, resp last
            .layer(TimeoutLayer::new(Duration::from_secs(10)))
            .layer(CompressionLayer::new())
            .layer(auth_layer)                     // innermost: sees req last, resp first
    );
```

Practical ordering rules:
- **Tracing/logging** — outermost so it captures the full round trip including all other middleware latency.
- **Timeout** — before auth/business logic so runaway requests are cut regardless of what's inside.
- **Auth/authorization** — inner, after trace so requests are logged even if they fail auth (useful for security auditing), before business handlers.
- **Compression** — innermost (wraps the response body), since it should apply to the final handler output.

AI frequently reverses the intuition ("last added = outermost") because it matches neither Express nor Django middleware mental models. When reviewing a `.layer()` chain, trace through request and response direction explicitly.

## How this slots into the core pipeline

- **Gate 5 (Design):** state handler signatures with correct extractor order; define `AppState` with `FromRef` sub-states; declare the error type implementing `IntoResponse`; sketch the Tower middleware stack with ordering rationale.
- **Gate 6 (Implementation):** body-consuming extractor last; use `State<T>` not `Extension<T>` for app state; `?` propagation through `AppError: IntoResponse`; check middleware layer ordering in the `ServiceBuilder` chain.
- **Gate 7 (Review):** verify extractor ordering in every handler; confirm no `unwrap` in handlers; confirm `State` vs `Extension` usage; trace middleware request/response direction.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Axum + Tower library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Node too (idempotency, invariants, gates, observability)? → **generic core** (`mir-backend`).
- True for every async Rust backend on Tokio (blocking, guard-across-await, cancellation, Arc/'static, channels, timeouts)? → **runtime tier** (`mir-backend-rust`).
- A mechanical footgun of *this library* (extractor order, `State` vs `Extension`, `FromRef`, `IntoResponse`, Tower layer ordering)? → **here**.
- A *different* Rust framework (Actix-web, Warp) → its own `mir-backend-rust-<framework>` module. Never widen this one.
