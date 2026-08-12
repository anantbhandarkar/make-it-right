---
name: mir-backend-rust-axum
description: "Make It Right (Axum module). Axum 0.8 + Tower + async Rust specific reliability augmentation. Use alongside mir-backend and mir-backend-rust when the target stack is Axum — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: the 0.8 route syntax change (/:id now panics, use /{id}), extractor ordering (body-consuming extractors must be last), custom extractors after the #[async_trait] removal in axum-core 0.5, typed State<T> vs Extension<T> and the FromRef sub-state pattern, implementing IntoResponse for error types without leaking internals, DefaultBodyLimit, and Tower middleware layer ordering (outermost wraps first). TRIGGER only when the Rust backend framework is Axum — building, reviewing, or debugging an Axum handler, router, extractor, or Tower middleware. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-rust (Tokio runtime concerns: blocking, guard-across-await, cancellation safety, async traits, Arc/'static, backpressure, timeouts); this module only adds Axum/Tower library mechanics. SKIP for Actix-web — the App-factory worker trap, web::Data, web::block and ResponseError belong to mir-backend-rust-actix, not here. SKIP for Warp, Poem, or any non-Axum Rust stack, and for non-Rust runtimes."
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

**Stack assumed:** Axum · Tower middleware · async Rust on Tokio.

**Versions verified 13 Aug 2026:**

| Crate | Current | Note |
|---|---|---|
| `axum` | **0.8.9** (2026-04-14) | MSRV 1.80. 0.8.0 shipped 2025-01-01 |
| `axum-core` | 0.5.6 | where the `#[async_trait]` removal landed |
| `tower` | 0.5.3 | |
| `tower-http` | **0.7.0** (2026-06-15) | breaking changes from 0.6 — see §5 |

**Axum 0.9 is not released.** Work is on `main` with breaking changes queued, and the maintainers have stated there is no timeline. Target 0.8.x. If you are reading a tutorial that shows `Router::new().route("/users/:id", ...)`, it predates 0.8 and will panic — see §1.

## The Axum footguns AI walks into most

### 1. Route syntax changed in 0.8 — `/:id` panics, write `/{id}`

The single most common failure when AI writes Axum today. Axum 0.8 upgraded `matchit` to 0.8, which changed path parameter syntax. The old form was **deliberately made a panic** rather than silently changing behaviour, so this fails at startup — usually inside a test or on first boot, not at compile time.

| Axum 0.7 and earlier | Axum 0.8+ |
|---|---|
| `/users/:id` | `/users/{id}` |
| `/assets/*path` | `/assets/{*path}` |

```rust
// WRONG on 0.8 — panics when the Router is built
let app = Router::new().route("/users/:id", get(get_user));

// RIGHT
let app = Router::new().route("/users/{id}", get(get_user));
```

The syntax now matches OpenAPI and `format!()`. Migration is a mechanical find-and-replace across every `.route(...)` and `.nest(...)` call. To escape a literal brace, double it: `{{`.

Because it panics rather than misroutes, the failure is loud — but a route registered only under a feature flag or in a rarely-built module will panic in production instead of in CI. Grep the whole tree for `"/:` and `*` route patterns when upgrading.

### 2. Extractor order — body-consuming extractors MUST be last

This is the #1 Axum compile/runtime trap. HTTP request bodies are streams: **reading the body consumes it**. Axum extractors that consume the body (`Json<T>`, `String`, `Bytes`, `Form<T>`, `Multipart`) must be the **last parameter** in the handler signature. Only **one** body-consuming extractor per handler is allowed — the request has one body.

Extractors that do NOT consume the body (they read headers, URI, path, query, or app state) can appear in any position: `Path<T>`, `Query<T>`, `State<T>`, `Extension<T>`, `TypedHeader<T>`, `ConnectInfo<T>`.

The rule is enforced by the type system, not at runtime: body-consuming extractors implement `FromRequest`, the rest implement `FromRequestParts`, and a handler may have at most one `FromRequest` in the final position. Getting it wrong is a **compile error**, but a famously unhelpful one — it surfaces as "the trait bound `fn(...): Handler<_, _>` is not satisfied", pointing at `.route()` rather than at the handler. Axum 0.8 improved these diagnostics; when they are still opaque, put `#[debug_handler]` on the handler and the error names the offending argument directly.

```rust
// WRONG — Json is not last. Compile error at the .route() call site
async fn create_user(
    Json(payload): Json<CreateUser>,  // consumes the body
    State(db): State<Db>,             // cannot follow a body extractor
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

**Two 0.8 changes that break extractor code written for 0.7:**

`#[async_trait]` was removed from `FromRequest` and `FromRequestParts` in axum-core 0.5 — Rust's native `async fn` in traits made it unnecessary. Custom extractors carrying the attribute no longer compile. Delete the attribute and the `use axum::async_trait;` import; leave the `async fn` signature alone.

```rust
// axum 0.7 — no longer compiles
#[async_trait]
impl<S> FromRequestParts<S> for AuthUser where S: Send + Sync {
    type Rejection = AppError;
    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> { ... }
}

// axum 0.8 — identical body, attribute deleted
impl<S> FromRequestParts<S> for AuthUser where S: Send + Sync {
    type Rejection = AppError;
    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> { ... }
}
```

`Option<T>` as an extractor changed meaning. In 0.7 any rejection from `T` was swallowed into `None` — including a malformed token or an unparseable body, which is how "optional auth" quietly became "no auth". In 0.8, `Option<T>` requires `T: OptionalFromRequestParts` (or `OptionalFromRequest`), so the extractor decides whether a failure means "absent" or "reject". If `Option<T>` stops compiling on upgrade, that is the point — the old code was discarding an error. Note `Query` does **not** have an `OptionalFromRequestParts` impl; use `Query<SomeStructWithOptionFields>` instead of `Option<Query<T>>`.

### 3. `State<T>` vs `Extension<T>` — prefer `State`, use `FromRef` for sub-states

Axum provides two mechanisms for injecting app-wide data into handlers:

- **`State<T>`** (via `.with_state(value)` on the router): type-checked at compile time, zero-cost extraction, the idiomatic modern approach. Use this.
- **`Extension<T>`** (via `.layer(Extension(value))`): stored as a type-erased map in the request extensions and looked up at runtime. A missing extension does **not** panic and is **not** a compile error — it produces an `ExtensionRejection::MissingExtension`, which is a plain **500 with the body "Missing request extension"**. That is quiet: it looks like an ordinary server error in your dashboards, so a layer you removed or reordered can break a route without anything naming the cause. Use `Extension` only for middleware-injected per-request data, never for app-wide state.

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
    .route("/users/{id}", get(get_user))   // 0.8 syntax — `/:id` panics
    .with_state(AppState { db, cache });
```

`AppState` must implement `Clone` (cheaply — wrap expensive resources in `Arc` or use pool handles that are already `Clone`).

### 4. Error handling — implement `IntoResponse`, never unwrap in handlers

Handlers return `Result<impl IntoResponse, E>` where `E: IntoResponse`. **Do not `.unwrap()` or `.expect()` inside a handler** — a panic aborts the request task and, without `CatchPanicLayer`, the connection is dropped with **no HTTP response at all**, not a 500. The client sees a transport error and your 5xx dashboard sees nothing. Implement `IntoResponse` for your error type to produce consistent, controlled HTTP responses:

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

Note the shape of the `DbError` arm: the `sqlx::Error` is logged server-side and the client gets a fixed string. A blanket `impl IntoResponse for anyhow::Error` that writes any `anyhow` formatting into the body does the opposite. `{}` / `to_string()` emits whatever the last `.context(...)` said, `{:#}` emits the full cause chain including the database's own error text, and `{:?}` adds the backtrace. If you use `anyhow` for speed, make the `IntoResponse` impl log the error and return a correlation ID, never the message.

### 5. Tower middleware layer order — outermost wraps first, responses last

Tower middleware is composed as a **stack**: the layer added **outermost** (last in the builder chain or added first to `ServiceBuilder`) sees the **request first** and the **response last**. This is the reverse of what many frameworks call "middleware order."

```rust
use tower_http::{trace::TraceLayer, timeout::TimeoutLayer, compression::CompressionLayer};

let app = Router::new()
    .route("/", get(handler))
    .layer(
        ServiceBuilder::new()
            .layer(TraceLayer::new_for_http())     // outermost: sees req first, resp last
            .layer(TimeoutLayer::with_status_code(
                StatusCode::REQUEST_TIMEOUT, Duration::from_secs(10)))
            .layer(CompressionLayer::new())
            .layer(auth_layer)                     // innermost: sees req last, resp first
    );
```

Take the `TimeoutLayer` import seriously: **`tower::timeout::TimeoutLayer` will not compile here.** Its error type is `BoxError`, and a `Router` layer must be infallible — you would need `HandleErrorLayer` to convert it. `tower_http`'s version returns the status directly. Note also that `TimeoutLayer::new` has been deprecated since tower-http 0.6.7 in favour of `with_status_code`.

Practical ordering rules:
- **Tracing/logging** — outermost so it captures the full round trip including all other middleware latency.
- **Timeout** — before auth/business logic so runaway requests are cut regardless of what's inside.
- **Auth/authorization** — inner, after trace so requests are logged even if they fail auth (useful for security auditing), before business handlers.
- **Compression** — innermost (wraps the response body), since it should apply to the final handler output.

AI frequently reverses the intuition ("last added = outermost") because it matches neither Express nor Django middleware mental models. When reviewing a `.layer()` chain, trace through request and response direction explicitly.

One ordering trap worth naming: `Router::layer` wraps the whole router **including the fallback**, while `Router::route_layer` runs only when a route actually matches. Auth belongs in `route_layer` — put it in `layer` and unmatched paths also get a 401, which turns a 404 into an authentication prompt and tells an attacker nothing useful either way. Put it in `route_layer` and a request to an unknown path correctly 404s.

### 6. tower-http 0.7 — new security middleware, and breaking changes from 0.6

tower-http 0.7.0 (2026-06-15) is where several defences you previously hand-rolled now live. If the code you are reviewing is on 0.6, these are the reasons to move.

**New in 0.7:**

- **`tower_http::csrf::CsrfLayer`** — cross-origin request protection ported from Go 1.25's scheme. It rejects cross-origin state-changing requests using `Sec-Fetch-Site`, an `Origin` allow-list, and an `Origin`/`Host` fallback, with **no per-request token state**. Before 0.7 there was no first-party CSRF middleware and projects invented their own.
  ```rust
  let layer = CsrfLayer::new().add_trusted_origin("https://example.com")?;
  ```
- **`RequestBodyDeadlineLayer` / `ResponseBodyDeadlineLayer`** (backed by `DeadlineBody`) — cap the *total* time of a body transfer. This is the fix for slow-loris uploads: the older `TimeoutBody` resets its deadline on every frame, so a client trickling one byte at a time never trips it. `DeadlineBody` does not reset.
- `ServeDir` gained strong `ETag` support with `If-Match`/`If-None-Match`, a `Backend` trait for non-filesystem sources, and `html_as_default_extension`.

**Breaking, and easy to miss:**

| Change | What to do |
|---|---|
| The no-op `tokio` and `async-compression` features were removed | Delete `tower-http/tokio` and `tower-http/async-compression` from your feature lists. The real dependencies still arrive via `compression-gzip`, `fs`, `timeout` |
| `compression` now honours `*` and `identity;q=0` per RFC 9110 | Requests that previously fell back to identity now get **406 Not Acceptable**. Check any client that sends `identity;q=0` |
| `follow-redirect` now forwards request `Extensions` across redirects by default | If you keep credentials or tenant identity in `Extensions`, this can leak them to a redirect target. The `Standard` policy drops them cross-origin; force the old behaviour with `FollowRedirectLayer::preserve_extensions(false)` |
| `SizeAbove` compression predicate widened `u16` → `u64` | Only matters if you constructed it explicitly |
| `GrpcCode` / `GrpcFailureClass` are now `#[non_exhaustive]` | Add a wildcard arm to matches |

## How this slots into the core pipeline

- **Gate 5 (Design):** state handler signatures with correct extractor order; define `AppState` with `FromRef` sub-states; declare the error type implementing `IntoResponse` and what it does *not* put in the body; sketch the Tower middleware stack with ordering rationale, including `DefaultBodyLimit`, timeouts and CORS.
- **Gate 6 (Implementation):** `{param}` route syntax; body-consuming extractor last; no `#[async_trait]` on custom extractors; use `State<T>` not `Extension<T>` for app state; `?` propagation through `AppError: IntoResponse`; check middleware layer ordering in the `ServiceBuilder` chain.
- **Gate 7 (Review):** grep for `"/:` in route strings; verify extractor ordering in every handler; confirm no `unwrap` in handlers; confirm `State` vs `Extension` usage; trace middleware request/response direction; walk the Security section below.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Axum + Tower library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Node too (idempotency, invariants, gates, observability)? → **generic core** (`mir-backend`).
- True for every async Rust backend on Tokio (blocking, guard-across-await, cancellation, Arc/'static, async traits, channels, timeouts)? → **runtime tier** (`mir-backend-rust`).
- A mechanical footgun of *this library* (route syntax, extractor order, `State` vs `Extension`, `FromRef`, `IntoResponse`, Tower layer ordering)? → **here**.
- A *different* Rust framework (Actix-web, Warp) → its own `mir-backend-rust-<framework>` module. Never widen this one.

## Security

Axum and Tower ship almost no security middleware enabled by default. Axum is a routing library; every control below is opt-in. The advisories on shared crates (`bytes`, `tracing-subscriber`, `sqlx`, `rustls-webpki`) and the generic Rust rules — secret leakage via `Debug`, SSRF, injection, supply chain — are in `mir-backend-rust`. This section is the Axum/Tower layer only.

### What is insecure by default, named exactly

| Setting | Default | Why it bites | Fix |
|---|---|---|---|
| `CorsLayer::very_permissive()` | credentials allowed **and origin reflected** | Mirrors the request `Origin` back with `Access-Control-Allow-Credentials: true`. Any website can read authenticated responses. It passes tower-http's own config check because it uses mirroring, not `Any` | Name origins: `.allow_origin(["https://app.example.com".parse().unwrap()])` |
| No CORS layer at all | no `Access-Control-Allow-Origin` header | Safe — browsers block cross-origin reads. Do not add a permissive layer to "fix" a CORS error you have not diagnosed | Add the narrowest layer that fixes the actual request |
| `axum::serve` on 0.8.x | **no header read timeout** | A client that opens a connection and dribbles request headers holds a task indefinitely. Slow-loris. Applying hyper's `header_read_timeout` is queued for 0.9, so 0.8.x needs it handled | Terminate TLS/HTTP at a proxy with header timeouts, or build the server via `hyper_util` and set `header_read_timeout` |
| `TimeoutLayer` on bodies | resets per frame | Caps idle time, not total transfer time. A slow upload never trips it | tower-http 0.7 `RequestBodyDeadlineLayer` |
| `DefaultBodyLimit` | 2 MB | Applies to `Bytes`, `String`, `Json`, `Form`. **Does not apply** to extractors that read the body directly via `Body::poll_frame` — streaming handlers and hand-rolled extractors are unlimited | `DefaultBodyLimit::max(n)` per route; enforce your own cap in any streaming handler |
| `ServeDir` | serves dotfiles, follows symlinks | Rejects `..` (`Component::ParentDir` is refused), so classic traversal is handled. But nothing filters `.env`, `.git/config`, `.ssh/`, and a symlink inside the served directory pointing outside it **is followed** | Serve a directory that contains only build output. Never point `ServeDir` at the repo root or CWD |
| `Extension<T>` | runtime lookup | A missing extension is a **runtime rejection**, not a compile error. Auth middleware removed from one route silently makes `Extension<CurrentUser>` fail — or, worse, a handler that reads `Option<Extension<CurrentUser>>` treats it as anonymous | Use `State` for app data; make the auth type its own `FromRequestParts` extractor that fails closed |
| No security headers | none set | No `X-Content-Type-Options`, `Strict-Transport-Security`, `Content-Security-Policy` | `tower_http::set_header::SetResponseHeaderLayer`, or `ValidateRequestHeaderLayer` where you need request-side checks |
| `CsrfLayer` | not applied | New in tower-http 0.7 and opt-in | See below |

`CorsLayer::permissive()` is safer than it looks: `allow_credentials(true)` combined with any `Any` wildcard **panics at startup** via tower-http's `ensure_usable_cors_rules` (it asserts on wildcard `allow_origin`, `allow_headers`, `allow_methods`, `expose_headers`). Treat that panic as the library doing its job. The gap it does not cover is `very_permissive()` and hand-rolled `AllowOrigin::mirror_request()` — mirroring is not a wildcard, so no assert fires.

### Object-level authorization (IDOR / BOLA)

Axum has no per-object authorization. A `FromRequestParts` extractor that validates a JWT proves *who* the caller is and nothing about *what* they may touch. This is the most common serious bug in generated Axum code:

```rust
// WRONG — token is valid, so the handler trusts the path parameter
async fn get_invoice(
    _user: AuthUser,                    // authenticated, not authorized
    State(db): State<PgPool>,
    Path(id): Path<i64>,
) -> Result<Json<Invoice>, AppError> {
    let inv = sqlx::query_as!(Invoice, "SELECT * FROM invoices WHERE id = $1", id)
        .fetch_optional(&db).await?.ok_or(AppError::NotFound)?;
    Ok(Json(inv))                        // any user reads any invoice
}

// RIGHT — ownership is part of the query, not a separate check
async fn get_invoice(
    user: AuthUser,
    State(db): State<PgPool>,
    Path(id): Path<i64>,
) -> Result<Json<Invoice>, AppError> {
    let inv = sqlx::query_as!(
        Invoice,
        "SELECT * FROM invoices WHERE id = $1 AND tenant_id = $2",
        id, user.tenant_id
    ).fetch_optional(&db).await?.ok_or(AppError::NotFound)?;
    Ok(Json(inv))
}
```

Put the scope in the `WHERE` clause rather than in an `if` after the fetch — a separate check is one refactor away from being dropped, and returning `NotFound` rather than `Forbidden` avoids confirming that the id exists. Middleware cannot do this for you: at `layer` time the path parameter has not been matched to a row.

### Mass assignment

`serde` silently ignores unknown fields. Deserializing the client's JSON straight into the type you persist means any field on that type is client-writable.

```rust
// WRONG — client can send {"name":"x","is_admin":true,"tenant_id":9}
#[derive(Deserialize)]
struct User { name: String, is_admin: bool, tenant_id: i64 }
async fn update(Json(u): Json<User>) { save(u).await }

// RIGHT — a separate input type is the allow-list
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UpdateUserInput { name: String }
```

Rust gives you the allow-list mechanism for free — **a distinct input struct containing only the writable fields**. Add `#[serde(deny_unknown_fields)]` so an unexpected key is a 422 instead of a silent drop; that turns a probing attacker into a log line. Note `#[serde(flatten)]` anywhere in the struct disables `deny_unknown_fields` without an error.

### CSRF and cookies

Which scheme needs CSRF protection depends entirely on where the credential lives:

| Auth scheme | Browser attaches it automatically? | CSRF protection needed? |
|---|---|---|
| Session cookie | yes | **Yes** |
| `Authorization: Bearer` from JS memory | no | No |
| JWT stored in a cookie | yes | **Yes** — storing a JWT in a cookie reintroduces CSRF |

If any state-changing route authenticates by cookie, apply `tower_http::csrf::CsrfLayer` (tower-http 0.7+) with an explicit `add_trusted_origin` list. Set the cookie itself with `SameSite=Lax` at minimum (`Strict` for admin surfaces), plus `HttpOnly`, `Secure`, and a `Path`. `SameSite=None` requires `Secure` and removes the browser's own defence, so it needs `CsrfLayer` unconditionally. `SameSite` alone is not sufficient: `Lax` still permits top-level `GET` navigation, so a `GET` route must never change state.

### Errors, panics, and logs

- A panic in a handler aborts that task. Without `tower_http::catch_panic::CatchPanicLayer` the connection is dropped with no response, and hyper logs the panic — including any secret in the message. With it, you return a controlled 500. Add the layer, and still treat every `.unwrap()` in a handler as a defect.
- Rejections have readable bodies. `JsonRejection` reports the failing field path (axum 0.8 wires `serde_path_to_error` into `Query` and `Form` too). That is good for debugging and it tells an attacker your internal schema. Map rejections to your own error type for public APIs: `Json<T>` → custom extractor, or `WithRejection` from `axum-extra`.
- `TraceLayer` logs the full URI including the query string. Tokens and emails passed as query parameters end up in your log store. Keep credentials out of query strings, and log user-controlled values as structured fields, not interpolated text.

### Upgrade notes with security consequences

- **axum 0.8.5** fixed `Json<T>` accepting **trailing characters after the JSON document**. On earlier 0.8.x, `{"a":1}{"b":2}` parsed as `{"a":1}` — a parser-differential: your service and a proxy or another service in the chain can disagree about what the body was. Be on 0.8.5+; 0.8.9 is current.
- **axum 0.8.9** returns a specific error when the multipart body limit is exceeded. `Multipart` is bounded by `DefaultBodyLimit` for the request as a whole; there is no per-field cap, so a single field can consume the entire allowance. If you write uploaded filenames to disk, sanitize them yourself — a multipart `filename` is attacker-controlled and may contain `../` or a null byte.
- **tower-http 0.7** `follow-redirect` now forwards `Extensions` across redirects (see §6). If a credential rides in `Extensions`, restrict it with `FilterCredentials::allow_extension::<T>()` or turn the behaviour off.
