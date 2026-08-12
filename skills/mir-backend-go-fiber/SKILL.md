---
name: mir-backend-go-fiber
description: "Make It Right (Fiber module). Fiber web framework reliability augmentation for Go backends, covering both Fiber v3 (current) and v2 (still patched). Use alongside mir-backend and mir-backend-go when the target stack is Fiber — adds the mechanical footguns the runtime-agnostic tiers deliberately omit: fiber.Ctx and every value read from it (Body, Params, Query, Headers) are pooled and reused after the handler returns, so retaining them corrupts or discloses another request's data; the v2→v3 API rewrite (fiber.Ctx is now an interface, BodyParser became c.Bind().Body(), c.Context() now returns a context.Context, TrustedProxies became TrustProxyConfig) that AI mixes up; c.Bind() silently skips validation when fiber.Config.StructValidator is nil; the Immutable setting; fasthttp's incompatibility with net/http middleware; and graceful shutdown via ListenConfig.GracefulContext or app.ShutdownWithContext, which hangs on keep-alive connections when ReadTimeout is 0. TRIGGER only when the Go backend uses the Fiber framework — building, reviewing, or debugging a Fiber handler, middleware, or route. Always loads TOGETHER WITH mir-backend (generic gates) and mir-backend-go (Go runtime concerns: goroutine leaks, context propagation, data races, channel rules, typed-nil, defer-in-loop, slice aliasing, error discipline, WaitGroup, os.Root, module checksums); this module only adds Fiber library mechanics. SKIP for Gin (mir-backend-go-gin), Echo (mir-backend-go-echo), chi, stdlib net/http, or any non-Fiber stack, and for non-Go runtimes."
trigger: /mir-backend-go-fiber
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-go-fiber · Make It Right (Fiber)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-go` (Go runtime model) → **this** (Fiber library mechanics). Run the gates first; load the Go runtime tier for goroutine lifecycle, context propagation, and race discipline; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (goroutine leaks, data races, context propagation, typed-nil, slice aliasing, `os.Root`, module checksums) live in `mir-backend-go` — not here.**

**Stack state, verified 13 Aug 2026.**

| Line | Current | Notes |
|---|---|---|
| **v3** (`github.com/gofiber/fiber/v3`) | v3.4.0 (2026-07-02); v3.0.0 GA 2026-02-02 | Current major. `go.mod` requires Go 1.25. `fiber migrate --to v3` assists the port |
| **v2** (`github.com/gofiber/fiber/v2`) | v2.52.15 (2026-08-12) | Still receiving security patches. Different handler signature and binding API |

**Establish which major the project is on before writing a line.** The two have different handler signatures, a different binding API, and different config field names — code written for one does not compile against the other, and AI mixes them constantly (footgun 2).

Fiber is built on `fasthttp`, not on `net/http`. That is the root of most Fiber-specific footguns.

## The Fiber footguns AI walks into most

### 1. `fiber.Ctx` and every value read from it are pooled — retaining them is data corruption

This is Fiber's defining hazard, and it is unchanged in v3. Fiber reuses the request context from a `sync.Pool` after the handler returns. **Every string and byte slice obtained from `fiber.Ctx` — `c.Body()`, `c.Params("id")`, `c.Get("X-Header")`, `c.Query("page")`, `c.Locals(...)` — points into the reused buffer.** When the pool recycles the slot for the next request, those references now read the next request's data.

```go
// WRONG: id and body point at pooled buffers; the goroutine reads another request's data
func handler(c fiber.Ctx) error {
    id := c.Params("id")
    body := c.Body()
    go func() { process(id, body) }()
    return c.SendStatus(fiber.StatusAccepted)
}

// RIGHT: copy strings, copy body bytes, detach the context — all before returning
func handler(c fiber.Ctx) error {
    id := strings.Clone(c.Params("id"))       // stdlib; no Fiber import needed
    body := bytes.Clone(c.Body())
    ctx := context.WithoutCancel(c.Context()) // survives the handler; keeps values, drops cancellation
    go func() { process(ctx, id, body) }()
    return c.SendStatus(fiber.StatusAccepted)
}
```

- Applies to **every** value read from `c`: params, query, headers, body, locals. When in doubt, copy.
- **Use `strings.Clone` / `bytes.Clone`, not `utils.CopyString`.** The import path differs by major and AI gets it wrong: v2 has `github.com/gofiber/fiber/v2/utils`, but **v3 has no `utils` subpackage** — `CopyString` moved to the separate module `github.com/gofiber/utils/v2`. The stdlib functions work in both.
- **Binding is not an escape hatch, except for JSON.** `c.Bind().Body(&req)` on a JSON body goes through `encoding/json` and yields heap-allocated fields you can retain. The form, multipart, query, header, and cookie binders hand the decoder `utils.UnsafeString(val)` over the fasthttp buffer, so those struct fields **still alias the pooled memory**. Retaining a struct filled by `.Query()`, `.Form()`, `.Header()`, or `.Cookie()` has the same corruption as retaining `c.Query(...)` directly.
- With `Immutable` on (footgun 5), use `app.GetString(s)` / `app.GetBytes(b)` to get a value that is safe to keep; they return the argument unchanged when `Immutable` is off, so they are not a substitute for an explicit copy.
- **The race detector will not catch this.** The pool recycles the slot *after* the handler returns, so there is no concurrent access in the Go memory-model sense — it is a logical corruption. Symptom to recognise: a panic or garbled data in a package that never mentions Fiber in the stack trace.
- This is a confidentiality bug as well as a correctness bug — see Security.

### 2. v2 and v3 are different APIs — do not mix them

The single most common source of code that does not compile, or that compiles and means something else.

| Concern | v2 | v3 |
|---|---|---|
| Handler signature | `func(c *fiber.Ctx) error` (struct pointer) | `func(c fiber.Ctx) error` (`Ctx` is an **interface**) |
| Standard Go context | `c.UserContext()` | `c.Context()` / `c.SetContext(ctx)` |
| fasthttp context | `c.Context()` | `c.RequestCtx()` |
| Body binding | `c.BodyParser(&req)` | `c.Bind().Body(&req)` |
| Query / params / cookie binding | `c.QueryParser`, `c.ParamsParser`, `c.CookieParser` | `c.Bind().Query()`, `.URI()`, `.Cookie()` (the `params` struct tag became `uri`) |
| Template data binding | `c.Bind(...)` | `c.ViewBind(...)` |
| Trusted proxies | `EnableTrustedProxyCheck` + `TrustedProxies` | `TrustProxy` + `TrustProxyConfig.Proxies` |
| CORS list options | comma-separated strings | slices |
| CSRF expiry | `Expiration` (1 h) | `IdleTimeout` (30 min) |

`c.Context()` flipping meaning between majors is the dangerous one: in v2 it returns `*fasthttp.RequestCtx`, in v3 it returns a `context.Context`. Code ported without care compiles in neither, or compiles and passes the wrong thing to a database call.

### 3. `c.Bind()` silently skips validation when no `StructValidator` is configured

Fiber v3 runs your validator inside every `Bind` call — but only if one exists. `fiber.Config.StructValidator` defaults to nil, and the binder then returns success without checking a single tag. Structs decorated with `validate:"required,email"` pass straight through to your business logic.

```go
type structValidator struct{ v *validator.Validate }
func (s *structValidator) Validate(out any) error { return s.v.Struct(out) }

app := fiber.New(fiber.Config{
    StructValidator: &structValidator{v: validator.New()},
})
```

- With that wired, `c.Bind().Body(&req)` parses **and** validates in one call. Without it, `Bind` is a decoder and nothing more.
- Error handling is manual by default: `Bind` does not write a response unless you chain `.WithAutoHandling()` (which sets 400 and returns a `*fiber.Error`). Always check and return the error.
- `.SkipValidation(true)` disables validation for one chain — grep for it during review; it is an easy way to accidentally disable the only input check on an endpoint.
- v2 has no validator hook at all: `BodyParser` never validates. Call `validator.Struct(&req)` yourself after every parse.
- **Never bind into your DB/ORM model** in either major — see Security, mass assignment.

### 4. fasthttp ≠ net/http — standard middleware and libraries may not work

`fasthttp` provides its own `RequestCtx` and does not implement `http.Handler` or `http.ResponseWriter`. Anything expecting `net/http` types — standard middleware, `net/http`-based auth libraries, `net/http` OpenTelemetry instrumentation, `net/http.CrossOriginProtection` — **cannot be used directly with Fiber**.

- Look for a Fiber-native equivalent in `gofiber/contrib` or the built-in `middleware/` set first.
- To reuse a `net/http` handler, wrap it with `adaptor.HTTPHandler` / `adaptor.HTTPMiddleware` (v3 `middleware/adaptor`). This converts types; it does **not** solve the buffer-pooling problem for values you read via `fiber.Ctx`.
- fasthttp does not support HTTP/2. If a client, a gRPC-Web gateway, or a load-balancer health check requires h2, Fiber is the wrong choice — surface it at Gate 0 stack fitness, not after implementation.
- Always check the ecosystem tier before adding a dependency: does it export a Fiber middleware, a fasthttp handler, or only an `http.Handler`?

### 5. `Immutable` — pick one mode and enforce it project-wide

By default (`Immutable: false`), strings returned by Fiber reference the fasthttp buffer directly for zero-allocation performance, and become invalid after the handler returns (footgun 1). `Immutable: true` makes Fiber copy every returned string, so values are safe to retain — at the cost of an allocation per read.

```go
app := fiber.New(fiber.Config{Immutable: true})
```

- **`Immutable: true`** — simpler, safer code. The right default for CRUD APIs where correctness is worth the allocations. It covers `c.Body()` too: both majors route the body through the copying path (`app.GetBytes` in v3, `utils.CopyBytes` in v2).
- **`Immutable: false`** — every retained string needs `strings.Clone`, every retained body needs `bytes.Clone`.
- **Document the choice and enforce it.** A codebase where some files assume `true` and others assume `false` has latent corruption that only appears under concurrent load.

### 6. Graceful shutdown — and the `ReadTimeout: 0` trap

Ignoring SIGTERM drops in-flight requests. But wiring shutdown without setting `ReadTimeout` produces a shutdown that appears to hang: **`Shutdown` does not close idle keep-alive connections**, so with `ReadTimeout: 0` those connections never time out and the drain waits for the full deadline on every deploy.

```go
app := fiber.New(fiber.Config{
    ReadTimeout:  15 * time.Second, // required for Shutdown to complete promptly
    WriteTimeout: 30 * time.Second,
    IdleTimeout:  60 * time.Second, // falls back to ReadTimeout when zero
})

// v3: declarative — Listen drains when ctx is canceled
ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
defer stop()
if err := app.Listen(":8080", fiber.ListenConfig{
    GracefulContext: ctx,
    ShutdownTimeout: 30 * time.Second,
}); err != nil {
    slog.Error("listen failed", "err", err)
}
```

- The manual form works in both majors: run `app.Listen` in a goroutine, wait on the signal, then `app.ShutdownWithContext(ctx)` with a bounded deadline. Never `app.Shutdown()` without a context — it blocks indefinitely if a request does not finish.
- `ReadTimeout`, `WriteTimeout`, and `IdleTimeout` all default to **zero, meaning no limit**. Set them explicitly; a slow client otherwise holds a connection forever.
- Register `OnPreShutdown` / `OnPostShutdown` hooks (v3; v2's single `OnShutdown` was split) to fail readiness probes before the drain starts, then close DB pools after it ends.
- Goroutines you spawned inside handlers are not tracked by `Shutdown`. Wait on your own `errgroup` too.

### 7. Prefork forks processes — in-memory state is silently partitioned

Prefork (`fiber.Config.Prefork` in v2, `fiber.ListenConfig.EnablePrefork` in v3) spawns one **child OS process** per core, all listening on the same port with `SO_REUSEPORT`. It is off by default and is a throughput knob people turn on from a blog post.

Every process gets its own copy of every global. Nothing in the process is shared, so anything counted in memory is counted N times:

| Built in memory | What actually happens with prefork on |
|---|---|
| `limiter` middleware with the default memory store | Each child enforces the limit independently. 8 children on a "10 req/min" rule permits 80 |
| `session` middleware with the default memory store | A user's session exists in one child; the next request lands on another and they are logged out |
| `csrf` middleware with the default storage | Same — the token is unknown to the child that receives the form post |
| `idempotency` middleware, in-process caches, dedupe maps | Per child. Duplicate charges, duplicate sends |
| A cron/ticker started in `main` | Runs once per child. N duplicate jobs |

- Move rate limits, sessions, CSRF token storage, and idempotency keys to a shared `Storage` (Redis) before enabling prefork, or leave it off. This is the same defect class as the Actix multi-worker App Data trap, but across processes, so a mutex cannot save you either.
- `fiber.IsChild()` reports whether the current process is a child. Guard "run exactly once" startup work (migrations, schedulers, warm-up) with `if !fiber.IsChild()`.
- Tests and local runs use one process, so all of this passes CI and fails in production.

### 8. `recover` middleware covers the handler goroutine only

Fiber's `recover` middleware (`middleware/recover`) turns a panic on the request goroutine into a 500. Register it first so it wraps every later middleware.

```go
app.Use(recover.New())
```

- Goroutines you spawn inside a handler are **not** covered and need their own `defer recover()` — see `mir-backend-go` footgun 5. An unrecovered panic there kills the process.
- The default handler prints to stderr. Configure `recover.Config{EnableStackTrace: true, StackTraceHandler: ...}` to emit a structured `slog` record with the correlation ID — copied out of `c` first, per footgun 1.
- A panic recovered here still leaves whatever partial state the handler created. Recovery is not a rollback.

## Security

Fiber-specific mechanics. Runtime-level items (`os.Root`, SSRF dialer control, `govulncheck`, module checksums, secret redaction) are in `mir-backend-go`.

### Known advisories affecting the default path

Fiber has a steady advisory stream, and several of these hit middleware you would reach for by default. Pin above the fixes and re-run `govulncheck ./...` after every bump.

| ID | What breaks | Affected → fixed |
|---|---|---|
| **CVE-2025-66630** (GHSA-68rr-p4fp-j59v) | `utils.UUIDv4()` returned the all-zero UUID when `crypto/rand` failed. Session IDs, CSRF tokens, request IDs, and rate-limit keys are derived from it — forgeable | v2 < 2.52.11 → **2.52.11**. Only reachable on Go ≤ 1.23; Go 1.24+ `crypto/rand` cannot fail |
| **CVE-2026-25891** (GHSA-m3c2-496v-cw3v) | `sanitizePath` in the static middleware checked for backslashes before URL-decoding, so `..\..\` traversal read arbitrary files on Windows | v3 ≤ 3.0.0 → **3.1.0** |
| **CVE-2024-25124** (GHSA-fmg4-x8pw-hjhg) | CORS middleware allowed `AllowOrigins: "*"` together with `AllowCredentials: true` | v2 < 2.52.1 → **2.52.1** |
| GHSA-c5qf-v26h-rp5r | Idempotency middleware's default `MemoryLock` never deleted map entries — one mutex leaked per unique `X-Idempotency-Key`, and the default validator only checks key *length*. Unauthenticated memory-exhaustion DoS | v2 ≤ 2.52.14 → **2.52.15** |
| GHSA-2mr3-m5q5-wgp6 | Unbounded allocation parsing the flash cookie (DoS) | fixed 2026-02-24 releases |
| GHSA-35hp-hqmv-8qg8 | Cache middleware's default key generator ignored the query string, so `/search?q=a` and `/search?q=b` shared a cached response | fixed 2026-04-25 releases |
| GHSA-g5vh-55hw-rxm8 | BasicAuth's default authorizer compared non-constant-time, leaking valid usernames through timing | fixed 2026-07-02 releases |
| GHSA-gv83-gqw6-9j2c | `helmet` never set the HSTS header because of an incorrect protocol check | fixed 2026-07-02 releases |

The pattern worth internalising: **Fiber's middleware defaults are tuned for throughput, and several have been the vulnerability.** Read the config struct of every middleware you enable rather than calling `New()` bare.

### Cross-request data disclosure from the pooled `Ctx`

Footgun 1 is not only a correctness bug. A retained header, body, or `Locals` value that is later written into a response, a log line, or a cache entry emits **another user's data** — auth headers and request bodies included. Treat "value read from `c` escapes the handler" as a confidentiality finding at review, not a style note.

### `c.IP()` and trusted proxies

Fiber is safe by default here: trust is off, so `c.IP()` returns the socket peer address. On **v2** you break it by setting `ProxyHeader` without also setting `EnableTrustedProxyCheck` — the trust check returns true for everyone when it is disabled. Configure both halves:

```go
app := fiber.New(fiber.Config{
    TrustProxy:       true,
    TrustProxyConfig: fiber.TrustProxyConfig{Proxies: []string{"10.0.0.0/8"}}, // your LB only
    ProxyHeader:      fiber.HeaderXForwardedFor,
})
```

`TrustProxyConfig.Loopback` / `.LinkLocal` / `.Private` are conveniences for whole ranges — they are still a trust decision, not a shortcut. Until this is right, do not key rate limits or allow-lists on `c.IP()`.

The two majors fail in opposite directions when the config is half-done, so check which one you are on:

| Situation | v2 | v3 |
|---|---|---|
| Trust flag off (`EnableTrustedProxyCheck` / `TrustProxy` false) | `IsProxyTrusted()` returns **true** — only an empty `ProxyHeader` is protecting you. Set `ProxyHeader` without the flag and anyone can spoof `c.IP()` | `IsProxyTrusted()` returns false; `c.IP()` is the socket peer |
| Trust flag on, no proxies/ranges/`Loopback`/`Private`/`LinkLocal` configured | trusts nobody | trusts nobody — `c.IP()` is the socket peer, and your `X-Forwarded-For` is silently ignored. A load balancer's real client IP goes missing here, which is the bug you will actually hit |

### Object-level authorization (IDOR / BOLA)

A valid token proves *who*, never *which row*. Put the ownership predicate in the query, not in an `if` after the fetch:

```go
uid := c.Locals("userID").(string)
order, err := repo.GetOrderForUser(ctx, c.Params("id"), uid) // ... WHERE id=$1 AND user_id=$2
```

Derive the tenant from the token, never from a request field. `c.Locals` is typed `any` — a bare `.(string)` assertion panics if a route was wired without the auth middleware; use the comma-ok form.

### Mass assignment

`c.Bind().Body(&model)` sets every matching field on the target. Binding straight into a GORM model lets a client send `{"is_admin": true, "tenant_id": "other"}`.

- One request struct per endpoint, containing only client-settable fields, then map to the model field by field.
- Fiber has no unknown-field rejection equivalent to `DisallowUnknownFields`, so the dedicated request struct **is** the allow-list. There is no config flag that saves you.
- `c.Bind().All(&req)` merges body, query, params, headers, and cookies into one struct. A field an attacker cannot set in the body may be settable from a header — enumerate the sources you actually want and bind only those.

### CORS

v3's CORS middleware **panics at construction** if `AllowCredentials` is true and `AllowOrigins` contains `*` — that is the fix for CVE-2024-25124, and the panic is the feature. The remaining hole is your own code:

```go
// WRONG: reflected origin + credentials — any site reads authenticated responses
cors.New(cors.Config{
    AllowOriginsFunc: func(origin string) bool { return true },
    AllowCredentials: true,
})
```

`AllowOriginsFunc` is not validated. Match against an explicit allow-list, and remember it is consulted only for origins not already in `AllowOrigins`.

### CSRF

The `csrf` middleware defends cookie/session auth; a Bearer-token API does not need it. In v3, `Expiration` became `IdleTimeout` (default 30 minutes) and the middleware validates `Sec-Fetch-Site` for unsafe methods. Two requirements that AI omits:

1. Set `CookieSecure: true` — it defaults to `false`, so the token cookie goes over plain HTTP. (`CookieSameSite` already defaults to `"Lax"` in both majors; setting it explicitly only documents intent.)
2. Use the session-backed configuration (`Session: sessionStore`). The cookie-only double-submit mode was the subject of two 2023 advisories (GHSA-mv73-f69x-444p, GHSA-94w9-97p3-p368) because tokens were not tied to a session and could be injected and reused.

### Static files and uploads

- `static.New(root, static.Config{...})`: `Browse: true` lists directory contents — leave it off in production. Serve from an `embed.FS` or `os.DirFS` via `Config.FS` rather than a live directory path when the content is fixed.
- `c.SaveFile(fileHeader, path)` does not validate `path`. `file.Filename` is attacker-controlled — never `filepath.Join(dir, file.Filename)`. Generate the stored name yourself and write through an `*os.Root` (see `mir-backend-go` → Security).
- `c.SendFile(userPath)` does not contain the path either.
- `BodyLimit` does default to 4 MiB, which is one of the few safe Fiber defaults — but it is **app-wide and has no per-route form**. It is applied once as `fasthttp.Server.MaxRequestBodySize`. The `limiter` middleware caps request *rate* and `timeout` caps handler *duration*; neither changes the accepted body size. Where one endpoint needs a smaller cap, reject on `c.Request().Header.ContentLength()` before binding and check the parsed length; where one endpoint needs a larger cap, run it on a separate app/listener rather than raising the global.

### Error responses

Return `fiber.NewError(code, msg)` and shape everything else in a single `Config.ErrorHandler`. The default handler returns the error's message text: a wrapped driver error returned from a handler therefore reaches the client with table and column names in it. Log the wrapped error with a correlation ID; return the ID and a generic message.

### Supply chain

v3 requires Go 1.25. v2 is still patched but is the line carrying most of the open advisories above — if a project is on v2, either keep it pinned to the newest 2.52.x or plan the v3 migration (`fiber migrate --to v3`) as an explicit item, not a background hope. Everything else — `go.sum`, `GOPRIVATE` scope, toolchain pinning, `govulncheck` in CI — is in `mir-backend-go` → Security.

## How this slots into the core pipeline

- **Gate 0 (stack fitness):** Fiber means fasthttp, which means no HTTP/2 and a smaller middleware ecosystem. If the design needs h2, gRPC-Web, or a specific `net/http` instrumentation library, raise it here.
- **Gate 5 (Design):** state the major version. List every value read from `c` that crosses the handler boundary and how each is copied. Decide `Immutable` and record it. Name the trusted-proxy CIDRs, the CORS allow-list, and whether the auth scheme needs CSRF.
- **Gate 6 (Implementation):** `StructValidator` wired; every escaping value copied (`utils.CopyString` / `bytes.Clone` / struct via `Bind`) and every async goroutine on a `context.WithoutCancel` context; `recover.New()` registered first; timeouts set and shutdown wired to SIGTERM; ownership predicates in queries.
- **Gate 7 (Review):** reliability-reviewer checks items 1–8; security-reviewer works the Security section. The four most commonly missed: a `Ctx` value escaping the handler, `StructValidator` never set, a middleware enabled with its bare defaults, and prefork on with in-memory limiter/session state.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Fiber library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Node too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Go backend (goroutine leaks, data races, context propagation, typed-nil, defer-in-loop, `os.Root`, module checksums)? → **runtime tier** (`mir-backend-go`).
- A mechanical footgun of *Fiber/fasthttp* (pooled `Ctx` buffers, `Immutable`, `StructValidator`, the v2/v3 API split, `net/http` incompatibility, `GracefulContext` / `ShutdownWithContext`, `TrustProxyConfig`, prefork)? → **here**.
- A *different* Go framework → `mir-backend-go-gin` or `mir-backend-go-echo`. Never widen this one.
