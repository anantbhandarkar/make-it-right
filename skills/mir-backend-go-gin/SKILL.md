---
name: mir-backend-go-gin
description: "Make It Right (Gin module). Gin web framework reliability augmentation for Go backends. Chains: mir-backend (generic gates) -> mir-backend-go (Go runtime) -> this (Gin library mechanics). Adds the mechanical footguns the runtime-agnostic tiers omit: *gin.Context is request-scoped and pooled, so it must be copied with c.Copy() before any spawned goroutine touches it; passing *gin.Context as a context.Context silently drops cancellation unless engine.ContextWithFallback is set; binding and validation discipline (ShouldBindJSON, binding tags, a separate request struct, EnableDecoderDisallowUnknownFields); middleware ordering and graceful shutdown wiring (http.Server.Shutdown on SIGTERM); and Gin's insecure defaults - SetTrustedProxies defaults to 0.0.0.0/0 so c.ClientIP() is attacker-controlled, and gin-contrib/cors will emit credentials with a reflected origin. TRIGGER only when the Go backend uses the Gin framework - building, reviewing, or debugging a Gin handler, middleware, or router. SKIP for Fiber (mir-backend-go-fiber), Echo (mir-backend-go-echo), chi, stdlib net/http, or any non-Gin stack, and for every non-Go runtime."
trigger: /mir-backend-go-gin
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-go-gin · Make It Right (Gin)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-go` (Go runtime model) → **this** (Gin library mechanics). Run the gates first; load the Go runtime tier for goroutine lifecycle, context propagation, and race discipline; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (goroutine leaks, data races, context propagation, typed-nil, slice aliasing, `http.Server` timeouts) live in `mir-backend-go` — not here.**

**Stack state, verified 13 Aug 2026.** `github.com/gin-gonic/gin` **v1.12.0** (2026-02-28) is current; v1.11.0 (2025-09-20) added HTTP/3 via quic-go and `BindPlain`. v1.12.0's `go.mod` requires **Go 1.25** — an older toolchain will not build it. Gin is a `net/http` framework: `*gin.Engine` implements `http.Handler`, so standard `net/http` middleware composes with it. If the project uses `gin-contrib/*` middleware or GORM, note the interaction before applying these.

## The Gin footguns AI walks into most

### 1. `*gin.Context` is request-scoped — never retain it across the handler boundary

`*gin.Context` is reused from a `sync.Pool` after the handler returns. Passing a `*gin.Context` reference to a goroutine spawned inside the handler and then accessing it after the handler returns causes a data race on the pooled object — you get another request's data or a crash.

- **Call `c.Copy()` before passing `*gin.Context` to any goroutine.**
  ```go
  // WRONG: c is recycled after the handler returns; the goroutine reads stale/corrupt data
  func handler(c *gin.Context) {
      go func() {
          sendEmail(c.Param("id"), c.GetHeader("X-Trace-Id"))
      }()
      c.JSON(http.StatusAccepted, gin.H{"status": "queued"})
  }

  // RIGHT: copy before spawning
  func handler(c *gin.Context) {
      cCopy := c.Copy()
      go func() {
          sendEmail(cCopy.Param("id"), cCopy.GetHeader("X-Trace-Id"))
      }()
      c.JSON(http.StatusAccepted, gin.H{"status": "queued"})
  }
  ```
- **`c.Copy()` does not solve cancellation.** `Copy()` clones `Keys` and `Params` but copies `Request` **by pointer**, so `cCopy.Request.Context()` is still the live request context — and `net/http` cancels that the instant the handler returns. A background goroutine doing `db.QueryContext(cCopy.Request.Context(), ...)` fails immediately with `context canceled`. Derive a detached context for the async work:
  ```go
  cCopy := c.Copy()
  bg := context.WithoutCancel(cCopy.Request.Context()) // keeps trace/tenant values, drops cancellation
  bg, cancel := context.WithTimeout(bg, 30*time.Second)
  go func() { defer cancel(); sendEmail(bg, cCopy.Param("id")) }()
  ```
- **The copy cannot write a response.** `Copy()` nils out the underlying `ResponseWriter` and sets the handler index past the end. `cCopy.JSON(...)` from a goroutine does not reach the client. Async work reports through logs, metrics, or a queue — not through the copied context.
- If you only need specific values, extract them into plain variables before spawning — simpler than `c.Copy()` and it makes the lifetime obvious.

### 2. `*gin.Context` used as a `context.Context` silently drops cancellation

`*gin.Context` implements `context.Context`, so `db.QueryContext(c, ...)` compiles. By default it is a dead context: `engine.ContextWithFallback` is **false**, which makes `c.Deadline()` return zero, `c.Done()` return nil, `c.Err()` return nil, and `c.Value(k)` look only at Gin's own `Keys` map. Client disconnects and request deadlines are ignored, and any value stashed by `net/http` middleware (trace IDs, `otel` spans) is invisible.

```go
// WRONG unless ContextWithFallback is on: query keeps running after the client hangs up
rows, err := db.QueryContext(c, q, args...)

// RIGHT either way
rows, err := db.QueryContext(c.Request.Context(), q, args...)
```

- Set `r.ContextWithFallback = true` at startup so `c` delegates to `c.Request.Context()`, **and** still prefer `c.Request.Context()` explicitly at call sites — it is unambiguous to the next reader.
- This is also why `c.Value(...)` does not see values set by `net/http` middleware registered outside Gin.

### 3. Binding and validation — `ShouldBindJSON` + tags, never bind to the DB model

Gin's binding deserializes JSON and runs `go-playground/validator/v10` tags, but it does **nothing** if you leave the tags off. AI routinely binds requests directly to the ORM/DB struct, which is mass assignment.

- Use `ShouldBindJSON` (not `BindJSON`) so binding failure returns an error rather than writing a 400 and aborting — you control the error response:
  ```go
  var req CreateOrderRequest
  if err := c.ShouldBindJSON(&req); err != nil {
      c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request"})
      return
  }
  ```
- Define a **separate request struct** with validation tags. Never bind into your DB model — it exposes `ID`, `CreatedAt`, `IsAdmin`, `TenantID` fields the client must not set.
- Annotate: `binding:"required"`, `binding:"min=0,max=10000"`, `binding:"oneof=pending paid"`, `binding:"email"`. A field with no tag accepts anything.
- `binding:"required"` rejects the **zero value**, not just a missing key. `Quantity int \`binding:"required"\`` rejects a legitimate `0`. Use a pointer (`*int`) plus `binding:"required"` when zero is valid, and `omitempty` semantics when the field is optional.
- Extra keys in the body are silently discarded by default. Set `binding.EnableDecoderDisallowUnknownFields = true` once at startup to make them a 400. It is a **global** flag and applies to the JSON binder only — `ShouldBindQuery`, `ShouldBindUri`, and form binding still ignore unknown keys, so the separate request struct is still the real defence.
- Validate **authorization** separately — a valid, well-formed request can still be a request to modify someone else's resource. Binding validation is not authorization.

### 4. Middleware order — `Recovery()` must wrap your handlers, not the other way around

Gin processes middleware in registration order. If you register your own middleware before `gin.Recovery()`, a panic in your middleware is not caught and crashes the process.

- **Register `gin.Recovery()` first** (or early) on the router/group so it is the outermost wrapper:
  ```go
  gin.SetMode(gin.ReleaseMode) // do this before gin.New()
  r := gin.New()               // not gin.Default() in production
  r.Use(gin.Recovery())        // catch panics from ALL subsequent middleware and handlers → 500
  r.Use(slogMiddleware())
  r.Use(authMiddleware())
  ```
- `gin.Default()` installs `Logger` + `Recovery` with a human-readable, colorized log format that no log aggregator can parse. In production, use `gin.New()` and inject a `log/slog` middleware.
- `gin.SetMode(gin.ReleaseMode)` (or `GIN_MODE=release`) stops Gin printing every route and handler name at startup. Do it *before* building the engine — debug mode is also where Gin prints the trusted-proxy warning, so read the startup log at least once in debug before switching.
- Recovery catches panics in handlers *on the same goroutine as the request*. Goroutines you spawn inside a handler need their own `recover` — see `mir-backend-go` footgun 5.

### 5. Missing graceful shutdown — in-flight requests are dropped on SIGTERM

`r.Run()` starts `net/http`'s `ListenAndServe` with a zero-value `http.Server`: no drain on signal, and **no timeouts at all** (see `mir-backend-go` → Security). Kubernetes sends SIGTERM before SIGKILL; `r.Run()` exits mid-request, causing 502s, partial writes, and corrupted state.

```go
srv := &http.Server{
    Addr:              ":8080",
    Handler:           r,
    ReadHeaderTimeout: 5 * time.Second,
    ReadTimeout:       15 * time.Second,
    WriteTimeout:      30 * time.Second,
    IdleTimeout:       60 * time.Second,
}

go func() {
    if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
        slog.Error("listen failed", "err", err)
        os.Exit(1)
    }
}()

ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
defer stop()
<-ctx.Done()

shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
defer cancel()
if err := srv.Shutdown(shutdownCtx); err != nil {
    slog.Error("forced shutdown", "err", err)
}
```

- The `Shutdown` timeout must exceed the slowest expected request. It bounds the drain window, not individual handlers.
- `Shutdown` does not wait for goroutines you spawned inside handlers. Track them with an `errgroup` or a `sync.WaitGroup` and wait on that too, or the background work is killed mid-write.
- Close DB connection pools and other resources after `Shutdown` returns.

### 6. Route group auth guard placement — and what it does not protect

Gin attaches middleware to route groups. AI often places auth incorrectly — either too broadly (catching routes that should be public) or at a group level that misses routes.

```go
public := r.Group("/")
{
    public.POST("/login", loginHandler)
    public.GET("/health", healthHandler)
}

protected := r.Group("/api/v1")
protected.Use(authMiddleware())
{
    protected.GET("/orders", listOrders)
    protected.POST("/orders", createOrder)
}
```

- `Use` on a group applies to routes registered on that group **after** the `Use` call. A route registered before it runs unauthenticated. Register middleware first, routes second, every time.
- Do not add auth to the root router if you have public routes — add it to the group.
- The auth middleware should set the authenticated identity with `c.Set("user", user)` so downstream handlers retrieve it without re-querying. Read it back with the two-value form (`v, ok := c.Get("user")`) — `c.MustGet` panics if a route was wired without the middleware.
- **Object-level authorization is not the auth middleware's job.** "Is the user logged in?" does not answer "does this user own order 4711?" See Security below.

## Security

Gin-specific mechanics. Runtime-level items (`http.Server` timeouts, SSRF dialer control, `os.Root`, `govulncheck`, module checksums) are in `mir-backend-go`.

### `c.ClientIP()` is attacker-controlled by default — the one to fix first

`gin.New()` sets `trustedProxies = ["0.0.0.0/0", "::/0"]`, `ForwardedByClientIP = true`, and `RemoteIPHeaders = ["X-Forwarded-For", "X-Real-IP"]`. Every client is treated as a trusted proxy, so anyone can send `X-Forwarded-For: 1.2.3.4` and choose what `c.ClientIP()` returns. This is **CVE-2020-28483**, and it is a configuration default, not a code bug — upgrading Gin does not fix it.

```go
r.SetTrustedProxies([]string{"10.0.0.0/8"}) // exactly your load balancer's CIDRs
// or, if the process is directly internet-facing:
r.SetTrustedProxies(nil)                    // ignore forwarding headers entirely
```

Until that call exists, do not key rate limits, allow-lists, audit logs, or fraud signals on `c.ClientIP()`. `TrustedPlatform` (`gin.PlatformCloudflare`, `gin.PlatformGoogleAppEngine`) reads one header and skips all other logic — correct only if the platform provably strips that header from client requests at its edge.

### Object-level authorization (IDOR / BOLA)

A valid token proves *who*, never *which row*. `authMiddleware()` passing is not permission to touch `/api/v1/orders/:id`.

```go
// WRONG: any authenticated user reads any order
order, err := repo.GetOrder(ctx, c.Param("id"))

// RIGHT: ownership is part of the query, not a later if-statement
uid := c.MustGet("userID").(string)
order, err := repo.GetOrderForUser(ctx, c.Param("id"), uid) // ... WHERE id=$1 AND user_id=$2
```

Scope by owner or tenant **inside the query**. A post-fetch `if order.UserID != uid` check is better than nothing but leaks existence through timing and through any log line written before the check. Never derive the tenant from a request field (`?tenant_id=`, a JSON body key) — derive it from the token.

### Mass assignment

Gin binds whatever fields exist on the target struct. Binding into a GORM model lets a client send `{"is_admin": true, "tenant_id": "other"}`.

1. One request struct per endpoint, containing only client-settable fields.
2. `binding.EnableDecoderDisallowUnknownFields = true` at startup so surprises become 400s (JSON binder only).
3. `json:"-"` on any field of a shared struct that must never come from the wire.
4. Map request struct → model field by field. Never `copier.Copy` a whole request onto a model.

### CORS: `gin-contrib/cors` will happily emit a reflected origin with credentials

Gin has no built-in CORS. `gin-contrib/cors`' `Config.Validate()` does **not** reject `AllowAllOrigins: true` combined with `AllowCredentials: true` — it emits `Access-Control-Allow-Origin: *` *and* `Access-Control-Allow-Credentials: true`. Browsers refuse that pair, so it looks broken, and the usual "fix" is worse:

```go
// WRONG: reflected origin + credentials — any site reads authenticated responses
cors.New(cors.Config{
    AllowOriginFunc:  func(origin string) bool { return true },
    AllowCredentials: true,
})

// RIGHT: explicit allow-list
cors.New(cors.Config{
    AllowOrigins:     []string{"https://app.example.com"},
    AllowCredentials: true,
    AllowMethods:     []string{"GET", "POST"},
    AllowHeaders:     []string{"Authorization", "Content-Type"},
})
```

`cors.Default()` sets `AllowAllOrigins: true` — fine for a public read-only API, wrong the moment cookies or `Authorization` are involved. With a multi-entry allow-list the middleware sets `Vary: Origin`; with `AllowAllOrigins` it does not, which is correct but means a shared cache in front of you must not be configured to strip `Vary`.

### CSRF

Gin ships no CSRF middleware. If sessions live in cookies, wrap the engine — `*gin.Engine` is an `http.Handler`, so the standard-library protection composes directly:

```go
p := http.NewCrossOriginProtection() // Go 1.25+
p.AddTrustedOrigin("https://admin.example.com")
srv := &http.Server{Addr: ":8080", Handler: p.Handler(r), ReadHeaderTimeout: 5 * time.Second}
```

Set session cookies with `c.SetSameSite(http.SameSiteLaxMode)` **before** `c.SetCookie(...)` — `SetSameSite` configures the context, so calling it afterwards has no effect on the cookie already written. Bearer-token APIs do not need CSRF protection; the browser never attaches an `Authorization` header cross-site.

### File serving and uploads

- `c.SaveUploadedFile(file, dst)` performs **no validation of `dst`**: it `MkdirAll`s the parent and creates the file. `filepath.Join(dir, file.Filename)` with `file.Filename = "../../etc/cron.d/pwn"` writes outside your upload directory. Generate the stored name yourself (UUID + a validated extension) and write through an `*os.Root` opened on the upload directory. Note it also `Chmod`s the parent directory to `0750` (or your `perm` argument), so it can change permissions on a directory that already existed.
- `c.File(userPath)` and `c.FileFromFS` do not contain the path — same traversal. `r.Static` / `r.StaticFS` go through `http.Dir`, which rejects `..`, but still follow symlinks out of the root and serve dotfiles.
- `c.FileAttachment(path, filename)` escapes quotes in the filename as of **v1.9.1** (CVE-2023-29401, unsanitized `Content-Disposition`). If a project is pinned below v1.9.1, that is the fix.
- Cap uploads: `r.MaxMultipartMemory` (default 32 MiB) only bounds what is buffered in memory, not the request size. Wrap the body with `http.MaxBytesReader` in middleware.

### Error responses and logs

- `c.JSON(500, gin.H{"error": err.Error()})` leaks driver text, table names, and sometimes parameter values. Log the wrapped error with a correlation ID; return the ID and a generic message.
- `c.AbortWithError(code, err)` attaches the error to `c.Errors` and still requires you to render a body — it does not sanitize anything.
- Gin's default `Logger()` logs the path including the query string. A token in a query parameter is then in your log store forever.
- `gin.Recovery()` writes the stack trace to the logger, not to the client — keep it that way; do not swap in a handler that echoes `debug.Stack()` in the response.

### Supply chain

Gin v1.12.0 requires Go 1.25 and pulls a wide dependency graph (`bytedance/sonic` for JSON, `quic-go` for HTTP/3, `go-playground/validator`). Run `govulncheck ./...` in CI; the reachability analysis matters here because most of that graph is unreachable in a typical service. Everything else — `go.sum`, `GOPRIVATE` scope, `GOFLAGS`, toolchain pinning — is in `mir-backend-go` → Security.

## How this slots into the core pipeline

- **Gate 5 (Design):** state handler boundaries, which routes are protected, and how `*gin.Context` values flow into any async work (copy + detached context). Name the trusted-proxy CIDRs. Identify any binding struct that doubles as a DB model — separate them. Decide the CORS allow-list and whether the auth scheme needs CSRF at all.
- **Gate 6 (Implementation):** `r.SetTrustedProxies(...)` called; `gin.Recovery()` registered first; `gin.ReleaseMode` set; every goroutine spawned inside a handler uses `c.Copy()` plus `context.WithoutCancel`; `ShouldBindJSON` + tags + separate request struct; every resource read/write scoped by owner or tenant in the query; `http.Server` with timeouts and `Shutdown` wired to SIGTERM.
- **Gate 7 (Review):** reliability-reviewer checks items 1–6; security-reviewer works the Security section. The three most commonly missed: `SetTrustedProxies` never called, ownership checked in an `if` (or not at all) instead of in the query, and `ContextWithFallback` left false while handlers pass `c` to the database.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Gin library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Node too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Go backend (goroutine leaks, data races, context propagation, typed-nil, defer-in-loop, `http.Server` timeouts, `os.Root`, module checksums)? → **runtime tier** (`mir-backend-go`).
- A mechanical footgun of *Gin* (`c.Copy()`, `ContextWithFallback`, `ShouldBindJSON` + binding tags, `Recovery()` order, `SetTrustedProxies`, `SaveUploadedFile`, `gin-contrib/cors`)? → **here**.
- A *different* Go framework → `mir-backend-go-fiber` or `mir-backend-go-echo`. Never widen this one.
