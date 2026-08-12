---
name: mir-backend-rust-actix
description: "Make It Right (Actix-web module). Actix-web 4.x + async Rust specific reliability augmentation. Use alongside mir-backend and mir-backend-rust when the target stack is Actix-web — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: the multi-worker app data trap (state constructed inside the App factory closure yields N independent copies, not one shared instance), web::Data<T> Arc semantics versus web::ThinData, the removed .data() method, worker-local single-threaded actix-rt execution (!Send types allowed, guard-across-await compiles and deadlocks, blocking starves the whole worker), web::block for blocking work, the real default body limits behind JsonConfig and PayloadConfig, Route::wrap ordering that used to silently drop route middleware, and error handling via the ResponseError trait. TRIGGER only when the Rust backend framework is Actix-web — building, reviewing, or debugging an Actix-web handler, middleware, extractor, or App factory. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-rust (Tokio runtime concerns: blocking, cancellation safety, async traits, Arc/'static, backpressure, timeouts); this module only adds Actix-web library mechanics. SKIP for Axum — route syntax, extractor ordering, State/FromRef, IntoResponse and Tower layers belong to mir-backend-rust-axum, not here. SKIP for Warp, Poem, or any non-Actix Rust stack, and for non-Rust runtimes."
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

**Stack assumed:** Actix-web 4.x · actix-rt (Tokio-backed single-threaded workers) · async Rust.

**Versions verified 13 Aug 2026:**

| Crate | Current | Note |
|---|---|---|
| `actix-web` | **4.14.1** (2026-08-09) | **MSRV 1.88** since 4.13.0. Still 4.x — there is no Actix-web 5 |
| `actix-files` | **0.6.10** (2026-02-06) | 0.6.10 was a security release — see the Security section |
| `actix-cors` | 0.7.1 | ships an unsafe preset — see the Security section |
| `actix-session` | 0.11.0 | |

Actix-web 4 has been stable for years and the 4.x line is where all work lands. If you see `HttpServer::new` returning `App` with `.data(...)`, that is 3.x-era code and will not compile.

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

**`.data()` was removed in Actix-web 4.** Use `.app_data(web::Data::new(...))`. It only appears in 3.x codebases and in training data from that era; code using it does not compile against 4.x at all.

**Use `web::ThinData<T>` (added in 4.9.0) for state that is already cheap to clone.** `web::Data<T>` wraps `T` in an `Arc`. When `T` is a `PgPool`, a `redis::Client`, or anything that is itself an `Arc` internally, `web::Data` gives you `Arc<Arc<..>>` — a second pointer chase on every extraction for no benefit. `ThinData` stores the value directly:

```rust
// PgPool is already internally reference-counted — ThinData avoids the extra Arc
.app_data(web::ThinData(pool.clone()))

async fn get_user(web::ThinData(pool): web::ThinData<PgPool>) -> impl Responder { ... }
```

Keep `web::Data` for large or non-`Clone` values, and for anything you need to reach through `HttpRequest::app_data`. The construct-outside-the-closure rule in §1 applies to both.

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

**The absent `Send` bound removes a compiler safety net.** On Axum, holding a `std::sync::MutexGuard` across an `.await` is a compile error because handler futures must be `Send`. On Actix it **compiles**, because they need not be. The same code that fails to build on Axum builds here and deadlocks the worker at runtime — and since one worker serves a share of all traffic, that is a partial outage, not one bad request. `mir-backend-rust` §2 has the full table. Review Actix handlers for guard-across-await by hand; nothing will flag it for you.

Two more consequences of the single-threaded worker model:

- **Worker count is fixed at startup** (`HttpServer::workers(n)`, default one per CPU core) and connections are assigned to workers as they arrive. One worker stuck on blocking work does not shed its already-accepted connections to the others.
- **`web::block` submits to a shared blocking pool**, not to the worker. It is bounded — unbounded fan-out through it queues silently. The `mir-backend-rust` note on `spawn_blocking` thread-pool exhaustion applies to `web::block` too.

### 4. Extractor configuration — know the real defaults, and register the config in the right place

Actix-web extractors (`Json<T>`, `Form<T>`, `Query<T>`, `Path<T>`, `Payload`) are configured via typed config objects registered with `.app_data()`. **There are defaults, and they are not "unlimited"** — verified in the 4.x source:

| Extractor | Config type | Default limit |
|---|---|---|
| `web::Json<T>` | `JsonConfig` | **2 MB** (`2_097_152`) |
| `web::Form<T>` | `FormConfig` | 16 KB |
| `web::Bytes` / `String` | `PayloadConfig` | **256 KB** (`262_144`) |
| `web::Payload` (raw stream) | none | **Unlimited.** `impl FromRequest for Payload` just hands you the stream — `PayloadConfig` is never consulted. Use `Payload::to_bytes_limited(n)` or count bytes yourself |
| `Multipart` | `MultipartFormConfig` (from `actix-multipart`) | set it yourself — the total and per-field limits are what matter |

So for `Json`/`Form`/`Bytes`/`String` the risk is not "AI forgot the limit and the body is unbounded." The two real failures are:

1. **The default is wrong for the route.** 2 MB of JSON is a lot to parse per request on an endpoint that expects 2 KB. Lower it per scope. Conversely, an upload endpoint that legitimately needs 20 MB fails at 2 MB with a confusing error until someone raises it globally — raising it globally then applies to every JSON route in the app.
2. **The config is registered where it does not apply.** `.app_data(JsonConfig...)` is resolved by type at extraction time from the nearest matching scope. Registered on the wrong `App` or `Scope`, it is silently ignored and the default applies. There is no warning.

```rust
// Tighten per scope, not globally
let strict_json = web::JsonConfig::default()
    .limit(16_384)                       // 16 KB — this endpoint takes a small object
    .content_type_required(true)
    .error_handler(|err, _req| {
        // default rejections echo parse detail; return a fixed message instead
        let response = HttpResponse::BadRequest().json(json!({ "error": "invalid JSON body" }));
        actix_web::error::InternalError::from_response(err, response).into()
    });

App::new()
    .service(
        web::scope("/api/v1/notes")
            .app_data(strict_json.clone())   // applies to this scope only
            .route("", web::post().to(create_note)),
    )
```

`QueryConfig`, `PathConfig` and `FormConfig` take the same `.error_handler`. Use it: the default rejection body is Actix's plain-text parse error, which names your fields and types.

### 5. `Route::wrap()` must come **after** `.to()` — it used to fail silently

Route-level middleware is attached with `Route::wrap()`. `.to()` and `.service()` **replace** the route's service, so calling either one *after* `.wrap()` throws the wrapped service away. On Actix-web ≤ 4.13 that was silent: the middleware was **dropped with no error**. When that middleware is an auth guard, the route ships unauthenticated and nothing tells you.

```rust
// WRONG — .to() after .wrap() replaces the wrapped service.
// <= 4.13: auth wrapper silently discarded, route is public. 4.14+: panics at startup.
web::resource("/admin").route(web::get().wrap(RequireAdmin).to(admin_handler));

// RIGHT — handler first, then wrap
web::resource("/admin").route(web::get().to(admin_handler).wrap(RequireAdmin));
```

**Actix-web 4.14.0 turned this into a panic** so the mistake is now loud at startup; the panic text spells out the fix (`web::get().to(handler).wrap(mw)`). That is a strong reason to be on 4.14.1: if you are on 4.13 or earlier, this bug is silent, and an audit of every `.wrap()` call site is the only way to find it. Write an integration test that asserts a protected route returns 401 without credentials — a test is the only thing that catches middleware that is present in the source but absent at runtime.

For new middleware prefer `middleware::from_fn()` (added in 4.9.0) over hand-implementing `Transform` + `Service`; the trait pair is where most custom Actix middleware bugs live.

### 6. Error handling — implement `ResponseError`, not `unwrap`

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

**Watch what `error_response` puts in the body.** In the example above the `#[error("database error")]` attribute is what keeps `self.to_string()` safe for the `DbError` arm. Change it to `#[error("{0}")]` — which is what `thiserror` examples usually show — and the same `error_response` starts returning the raw `sqlx::Error` message to the client, including constraint names and sometimes column values. Log the source error, return a fixed string:

```rust
fn error_response(&self) -> HttpResponse {
    if let AppError::DbError(e) = self {
        tracing::error!(error = ?e, "db failure");        // detail stays server-side
    }
    HttpResponse::build(self.status_code())
        .json(json!({ "error": self.public_message() }))  // curated per variant
}
```

Never `.unwrap()` inside a handler. Actix-web catches per-request panics and returns a 500, so the process survives — which is exactly why this gets missed. The panic message goes to the log with whatever it was formatting, and the client gets a 500 with no correlation ID.

## How this slots into the core pipeline

- **Gate 5 (Design):** state construction outside `HttpServer::new` as the mandatory pattern; choose `web::Data` vs `web::ThinData` per value; declare the error type implementing `ResponseError` and what its body does *not* contain; state the per-scope body limits; list route middleware and where it is wrapped.
- **Gate 6 (Implementation):** shared state built outside closure; `.app_data(data.clone())` inside factory; `web::block` for blocking/CPU work; no guard held across `.await`; `?` propagation through `AppError: ResponseError`; extractor configs registered on the scope they are meant to govern; `.wrap()` after `.to()`.
- **Gate 7 (Review):** verify no state construction inside `HttpServer::new`; no `unwrap` in handlers; extractor limits set per scope and actually reachable; no `.data()` usage; `web::block` used for any blocking work; a test asserts each protected route 401s without credentials; walk the Security section below.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Actix-web library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Node too (idempotency, invariants, gates, observability)? → **generic core** (`mir-backend`).
- True for every async Rust backend on Tokio (blocking, guard-across-await, cancellation, Arc/'static, async traits, channels, timeouts)? → **runtime tier** (`mir-backend-rust`).
- A mechanical footgun of *this library* (multi-worker App Data trap, `web::Data` vs `web::ThinData`, `web::block`, extractor config limits, `Route::wrap` ordering, `ResponseError`)? → **here**.
- A *different* Rust framework (Axum, Warp) → its own `mir-backend-rust-<framework>` module. Never widen this one.

## Security

Actix-web layer only. The advisories on shared crates (`bytes`, `tracing-subscriber`, `sqlx`, `rustls-webpki`) and the generic Rust rules — secret leakage via `Debug`, SSRF, injection, deserialization, supply chain — are in `mir-backend-rust`.

### What is insecure by default, named exactly

| Setting | Default | Why it bites | Fix |
|---|---|---|---|
| `actix_cors::Cors::permissive()` | all origins, all methods, all headers, **and `supports_credentials: true`** | Its own doc says "*All* origins, methods, request headers and exposed headers allowed. Credentials supported." With `send_wildcard` off it **reflects the request `Origin`** and sends `Access-Control-Allow-Credentials: true`. Any site can read authenticated responses. It is the snippet everyone copies for local dev and forgets to remove | Build explicitly: `Cors::default().allowed_origin("https://app.example.com").allowed_methods(vec!["GET","POST"])` |
| `Cors::allow_any_origin()` + `supports_credentials()` | **accepted, no error** | actix-cors only refuses `send_wildcard()` combined with `supports_credentials()` — the literal `*`. Reflecting every origin is the same vulnerability and passes the check | Enumerate origins, or use `allowed_origin_fn` with a strict match. Never a `contains()` substring test |
| `actix-files` `Files::use_hidden_files()` | off (good) — but people enable it | Turns on serving dotfiles: `.env`, `.git/config`, `.ssh/`. Leave it off | Leave it off. If you need one dotfile, route it explicitly |
| `actix-files` `Files::show_files_listing()` | off (good) | Enabling it exposes a full directory index | Leave it off in production |
| `Files::new` pointing at `.` or the repo root | serves whatever is there | Historically an invalid `Files::new` input served the process **CWD** | Serve a build-output directory only. Be on `actix-files` 0.6.10+ (below) |
| No security headers | none set | No `X-Content-Type-Options`, `Strict-Transport-Security`, `Content-Security-Policy` | `middleware::DefaultHeaders::new().add(...)`, or `actix-web-lab`'s header middleware |
| Server timeouts | `client_request_timeout` defaults to 5s; keep-alive 5s | Reasonable, but `HttpServer` has no total request duration cap. A slow body transfer inside the payload limit still holds a worker slot | Set `client_request_timeout`, `client_disconnect_timeout` and `keep_alive` deliberately; terminate at a proxy for total-duration limits |
| `actix-session` cookie flags | you choose them | A session store with `SessionMiddleware::builder` requires you to set `cookie_secure`, `cookie_http_only` and `cookie_same_site` — the defaults are for development | `.cookie_secure(true).cookie_http_only(true).cookie_same_site(SameSite::Lax)` |

**`actix-files` 0.6.10 (2026-02-06) was a security release.** It fixed two issues: a panic on an **empty `Range` header** (remote denial of service — any client can send one), and **serving the current working directory on invalid `Files::new` inputs** (arbitrary file disclosure). These were published in the crate's release notes as a "Security Notice"; no RUSTSEC identifier was assigned, so `cargo audit` will **not** flag an older version. Check the pinned version by hand: `cargo tree -i actix-files`. 0.6.10 is current.

### Object-level authorization (IDOR / BOLA)

Actix extractors give you identity, never authority. `web::Path<u64>` is attacker-controlled input, and a middleware that validated a JWT has not checked whether this caller may touch this row.

```rust
// WRONG — valid session, arbitrary invoice
async fn get_invoice(
    _user: AuthUser,
    pool: web::ThinData<PgPool>,
    path: web::Path<i64>,
) -> Result<web::Json<Invoice>, AppError> {
    let inv = sqlx::query_as!(Invoice, "SELECT * FROM invoices WHERE id = $1", *path)
        .fetch_optional(&pool.0).await?.ok_or(AppError::NotFound)?;
    Ok(web::Json(inv))
}

// RIGHT — the tenant scope is in the query
let inv = sqlx::query_as!(
    Invoice,
    "SELECT * FROM invoices WHERE id = $1 AND tenant_id = $2",
    *path, user.tenant_id
).fetch_optional(&pool.0).await?.ok_or(AppError::NotFound)?;
```

Scope in the `WHERE` clause, not in an `if` after the fetch. Return `NotFound` rather than `Forbidden` so the response does not confirm the id exists. Middleware cannot cover this — at `wrap` time the path has not been bound to a row.

### Mass assignment

`serde` drops unknown fields silently, so deserializing straight into the persisted type makes every field on it client-writable.

```rust
// WRONG — {"name":"x","is_admin":true} sets is_admin
#[derive(Deserialize)] struct User { name: String, is_admin: bool }
async fn update(user: web::Json<User>) { save(user.into_inner()).await }

// RIGHT — a dedicated input type is the allow-list
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UpdateUserInput { name: String }
```

The allow-list mechanism Actix gives you is **a separate input struct holding only writable fields**. `#[serde(deny_unknown_fields)]` turns a probe into a 400 instead of a silent drop. `#[serde(flatten)]` anywhere in the struct switches that check off with no error.

### CSRF and cookies

Actix-web has **no built-in CSRF middleware**. If any state-changing route authenticates by cookie — `actix-session`, or a JWT stored in a cookie — you must add protection yourself: a synchronizer token, or an `Origin`/`Sec-Fetch-Site` check in `middleware::from_fn()`.

| Auth scheme | Browser sends it automatically? | Needs CSRF protection? |
|---|---|---|
| `actix-session` cookie | yes | **Yes** |
| `Authorization: Bearer` from JS memory | no | No |
| JWT in a cookie | yes | **Yes** |

Set `SameSite=Lax` at minimum (`Strict` for admin routes) plus `HttpOnly` and `Secure`. `SameSite` alone is not enough — `Lax` still allows top-level `GET`, so no `GET` route may change state.

Cookie parsing notes from recent releases: 4.13.0 **ignores unparsable cookies** in the `Cookie` header rather than failing the request, and 4.14.0 added `HttpRequest::cookie_raw` / `cookies_raw` to read values **without percent-decoding**. If you compare a cookie value against a signed token, decide explicitly which form you are comparing — decoding differences between your check and your issuer are how token comparisons get bypassed.

### Path traversal in uploads and file serving

- `actix-files` resolves `..` safely, but **you** must sanitize anything you build a path from. Never `PathBuf::from(user_input)` or `format!("./uploads/{}", filename)`. A multipart `filename` is attacker-controlled and may contain `../`, an absolute path, a null byte, or a Windows drive prefix.
- Store uploads under a generated name (a UUID) and keep the client's filename as metadata in the database, used only for the `Content-Disposition` header on download — where it must be quoted and escaped.
- After joining any user-derived component, canonicalize and verify the result is still inside the base directory before opening it.
- `Multipart` has no default total or per-field limit. Configure `MultipartFormConfig` with `total_limit` and `memory_limit`, and cap the field count — an unbounded number of tiny fields exhausts memory without exceeding any byte limit.

### Errors, logs, and panics

- A panicking handler is caught per request and returns a 500, so the service stays up and the bug stays invisible. Alert on the 500 rate; do not rely on the process dying to tell you.
- `middleware::Logger` writes the full request line including the query string. Tokens or emails in query parameters land in your logs. 4.11.0 improved its handling of non-UTF-8 header values, so be on 4.11+ if you log headers at all.
- The default extractor rejections return Actix's plain-text parse errors, naming your fields and types. Set `.error_handler` on every extractor config that faces the public (see §4).
- `ResponseError::error_response` is the single place your error text becomes an HTTP body. Audit it whenever you add a variant — see §6.

### Multi-worker state and security

The §1 App Data trap has a security form worth calling out. **Rate limiters, login-attempt counters, nonce caches and CSRF token stores built inside the `HttpServer::new` closure are per-worker.** With 8 workers, a "5 failed logins then lock" rule permits up to 40, because each worker counts independently and connections are distributed across them. The code looks correct and the tests pass with one worker. Build these outside the closure, or keep them in Redis — which is also what makes the limit hold across replicas.
