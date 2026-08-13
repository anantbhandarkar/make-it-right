---
name: mir-backend-go-echo
description: "Make It Right (Echo module). Echo web framework reliability augmentation for Go backends, covering Echo v5 (current) and v4 (maintained). Chains: mir-backend (generic gates) -> mir-backend-go (Go runtime) -> this (Echo library mechanics). Adds the mechanical footguns the runtime-agnostic tiers omit: echo.Context comes from a sync.Pool and must never be retained past the handler or handed to a goroutine; the v4->v5 rewrite that AI mixes up (Context became a *echo.Context struct, Logger became *slog.Logger, HTTPErrorHandler's arguments swapped, e.Shutdown and e.Close removed in favour of StartConfig); Bind never validates, so a missing c.Validate ships unchecked input, and Bind merges path and query values into one struct, a mass-assignment path; middleware ordering and graceful shutdown; and Echo's insecure defaults - c.RealIP() trusts X-Forwarded-For unless IPExtractor is set, and middleware.Secure sends no HSTS or CSP. TRIGGER only when the Go backend uses the Echo framework - building, reviewing, or debugging an Echo handler, middleware, or router. SKIP for Gin (mir-backend-go-gin), Fiber (mir-backend-go-fiber), chi, stdlib net/http, or any non-Echo stack, and for every non-Go runtime."
trigger: /mir-backend-go-echo
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-go-echo · Make It Right (Echo)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-go` (Go runtime model) → **this** (Echo library mechanics). Run the gates first; load the Go runtime tier for goroutine lifecycle, context propagation, and race discipline; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (goroutine leaks, data races, context propagation, typed-nil, slice aliasing, `os.Root`, module checksums) live in `mir-backend-go` — not here.**

**Stack state, verified 13 Aug 2026.**

| Line | Current | Notes |
|---|---|---|
| **v5** (`github.com/labstack/echo/v5`) | v5.3.1 (2026-07-21); v5.0.0 GA 2026-01-18 | Current major. `go.mod` requires Go 1.25 |
| **v4** (`github.com/labstack/echo/v4`) | v4.15.4 (2026-06-15) | Still receiving security and bug fixes |

**Establish the major before writing a line.** Handler signatures, the logger, the error handler, and the whole server-startup API changed in v5 (footgun 2). Note also that `echo-contrib` ships separate release lines for v4 and v5 — importing the wrong one gives type errors that look like Echo's fault. Whichever major you are on, this module's earlier claim that "v5 is pre-release" is obsolete: v5 has been GA since January 2026.

## The Echo footguns AI walks into most

### 1. `echo.Context` is pooled — do not retain it across the handler boundary

Echo keeps contexts in a `sync.Pool` and reuses them. Storing the context in a struct, or handing it to a goroutine that outlives the handler, means you are reading a context that now belongs to a different request.

```go
// WRONG: c is recycled after the handler returns
func handler(c *echo.Context) error {
    go func() {
        sendNotification(c.Param("id"), c.Request().Header.Get("X-Trace-Id"))
    }()
    return c.NoContent(http.StatusAccepted)
}

// RIGHT: capture values and detach the context before spawning
func handler(c *echo.Context) error {
    id := c.Param("id")
    traceID := c.Request().Header.Get("X-Trace-Id")
    ctx := context.WithoutCancel(c.Request().Context()) // keeps values, drops cancellation
    ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
    go func() { defer cancel(); sendNotification(ctx, id, traceID) }()
    return c.NoContent(http.StatusAccepted)
}
```

- `c.Request().Context()` is canceled the instant the handler returns. Passing it to background work makes every downstream call fail with `context canceled` — this is the more common bug than the pooling itself, because it fails loudly on the first request rather than silently under load.
- Values stashed with `c.Set(key, value)` live in the per-request store. Read them out before the handler returns if a goroutine needs them.
- Copy the values you need into a plain struct. Do not build a helper that "clones the context" — there is no supported clone, and the pooled fields will still be reused.

### 2. v4 and v5 are different APIs — do not mix them

| Concern | v4 | v5 |
|---|---|---|
| Handler signature | `func(c echo.Context) error` (interface) | `func(c *echo.Context) error` (struct pointer) |
| Logger | custom `echo.Logger` interface; `e.Logger.Error(...)` | `*slog.Logger`; `c.Logger()` returns `*slog.Logger` |
| Error handler | `func(err error, c echo.Context)` | `func(c *echo.Context, err error)` — **arguments swapped** |
| Default error handler | `e.DefaultHTTPErrorHandler` | `DefaultHTTPErrorHandler(exposeError bool)` factory |
| Binder interface | `Bind(i any, c Context) error` | `Bind(c *Context, target any) error` |
| Standalone binders | — | `BindBody`, `BindQueryParams`, `BindPathValues` (was `BindPathParams`), `BindHeaders` |
| Server startup | `e.Start` / `e.StartTLS` / `e.Shutdown` / `e.Close`; `e.Server` is an exported `*http.Server` | `StartConfig{...}.Start(ctx, e)`; **`e.Shutdown` and `e.Close` are gone**; the `http.Server` is internal, reachable only via `StartConfig.BeforeServeFunc` |
| `c.RealIP()` default | trusts `X-Forwarded-For` / `X-Real-IP` | trusts them in v5.0.0; **returns the socket peer address from v5.1.0** |
| Removed | — | `GetPath`, the `MethodNotAllowedHandler` / `NotFoundHandler` package variables |

The swapped `HTTPErrorHandler` arguments are the nastiest: both parameters are single-word types, so a v4 handler pasted into a v5 project can compile in some shapes and log the wrong thing.

### 3. `Bind` does not validate — and `Bind` reads more sources than you think

`c.Bind(&req)` deserializes; it never runs validation tags. Two separate mistakes follow.

**Forgetting `Validate` is the silent one.** Registering no validator is loud — `c.Validate(x)` returns `ErrValidatorNotRegistered`. Never calling `Validate` at all produces no error anywhere, and your `validate:"required,email"` tags are decoration.

```go
type customValidator struct{ v *validator.Validate }
func (cv *customValidator) Validate(i any) error { return cv.v.Struct(i) }

e := echo.New()
e.Validator = &customValidator{v: validator.New()}
```

```go
var req CreateOrderRequest
if err := c.Bind(&req); err != nil {
    return echo.NewHTTPError(http.StatusBadRequest, "invalid request")
}
if err := c.Validate(&req); err != nil {          // this line is the one that gets dropped
    return echo.NewHTTPError(http.StatusUnprocessableEntity, "validation failed")
}
```

**`Bind` merges sources.** Echo's default binder fills the struct from path parameters and, for methods without a body (GET, DELETE, HEAD), from query parameters — as well as from the body. A field an attacker cannot reach through JSON may be settable through the URL. When you want exactly one source, use the explicit binders (`echo.BindBody(c, &req)`, `echo.BindQueryParams(c, &req)` in v5; `(&echo.DefaultBinder{}).BindBody(c, &req)` in v4) instead of the catch-all `Bind`.

- **Never bind to your DB/ORM model.** Use a dedicated request struct per endpoint. See Security, mass assignment.
- `validate:"required"` rejects the zero value, not just an absent key — use a pointer field when `0` or `""` is legitimate.

### 4. Middleware order — `Recover()` first, and know `Pre` from `Use`

Echo runs middleware in registration order. Your middleware registered before `middleware.Recover()` is not covered by it, and a panic there kills the process.

```go
e := echo.New()
e.Pre(middleware.RemoveTrailingSlash()) // runs BEFORE routing — can rewrite the path
e.Use(middleware.Recover())             // runs after routing; register it first among Use
e.Use(middleware.RequestID())
e.Use(authMiddleware())
```

- `e.Pre(...)` runs **before** the router matches, so it is the only place a middleware can change the path that routing sees. `e.Use(...)` runs after the match.
- Anything in `Pre` that rewrites or unescapes the path can desynchronise routing from later path handling — that is exactly the shape of CVE-2026-55677 (see Security). Add path-rewriting middleware deliberately, never by copy-paste.
- Group middleware applies to routes registered on that group **after** the `Use` call. Register middleware first, routes second.
- `Recover()` covers panics on the request goroutine only. Goroutines you spawn need their own `recover` — `mir-backend-go` footgun 5.

### 5. `HTTPErrorHandler` — one error shape, and do not expose the error

Returning raw Go errors from handlers produces a different response shape per endpoint and leaks internals. Echo's default (`DefaultHTTPErrorHandler(false)`) returns `{"message": "<status text>"}`; the factory's `exposeError` argument adds `{"error": "<err.Error()>"}` to the body — that is direct internal-error disclosure, and it must stay `false` outside development.

```go
// v5 signature: (c, err)
e.HTTPErrorHandler = func(c *echo.Context, err error) {
    var he *echo.HTTPError
    if errors.As(err, &he) {
        _ = c.JSON(he.Code, map[string]any{"error": he.Message})
        return
    }
    c.Logger().Error("unhandled error", "err", err, "path", c.Path()) // log detail
    _ = c.JSON(http.StatusInternalServerError, map[string]any{"error": "internal server error"})
}
```

- Return `echo.NewHTTPError(code, message)` from handlers and middleware; let the error handler shape the response.
- The error handler **does nothing if the response is already committed**. A handler that writes a 200 and then returns an error sends a 200 with a success body — check errors before you write.
- The default handler does not log. If you replace it, logging is now your job; if you keep it, add a logging middleware or errors vanish.

### 6. Graceful shutdown — different in each major

**v5:** `e.Start(":8080")` already installs a SIGINT/SIGTERM handler and drains with a default `GracefulTimeout` of 10 seconds. `e.Shutdown` and `e.Close` no longer exist. To tune it, drive `StartConfig` yourself:

```go
ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
defer stop()

sc := echo.StartConfig{
    Address:         ":8080",
    GracefulTimeout: 30 * time.Second,
    OnShutdownError: func(err error) { slog.Error("drain exceeded timeout", "err", err) },
    BeforeServeFunc: func(s *http.Server) error { // the only hook to the http.Server in v5
        s.ReadHeaderTimeout = 5 * time.Second
        s.IdleTimeout = 60 * time.Second
        return nil
    },
}
if err := sc.Start(ctx, e); err != nil && !errors.Is(err, http.ErrServerClosed) {
    slog.Error("server failed", "err", err)
}
```

A negative `GracefulTimeout` disables draining entirely — do not set it to skip a slow shutdown; fix the slow request instead.

**v4:** wire it manually, and set the timeouts, because `e.Server` is a zero-value `*http.Server` with none:

```go
e.Server.ReadHeaderTimeout = 5 * time.Second
e.Server.ReadTimeout = 15 * time.Second
e.Server.WriteTimeout = 30 * time.Second
go func() {
    if err := e.Start(":8080"); err != nil && !errors.Is(err, http.ErrServerClosed) { /* ... */ }
}()
ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
defer stop()
<-ctx.Done()
shutdownCtx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
defer cancel()
_ = e.Shutdown(shutdownCtx)
```

In both majors, `Shutdown` does not wait for goroutines you spawned inside handlers — track them yourself. Close DB pools after the drain returns.

## Security

Echo-specific mechanics. Runtime-level items (`os.Root`, SSRF dialer control, `govulncheck`, module checksums, secret redaction) are in `mir-backend-go`.

### CVE-2026-55677 — encoded `%2F` bypassed route-level middleware

The router matched on the **raw** encoded path while `StaticDirectoryHandler` unescaped `%2F` to `/` before resolving the file. A request could therefore be routed as if it were a public path while the file handler read a protected one, bypassing auth middleware attached to a route prefix. High severity, CVSS 7.5.

| Affected | Fixed |
|---|---|
| v4 ≤ 4.15.2 | **4.15.3** |
| v5 ≤ 5.1.1 | **5.2.0** |

The fix disables path unescaping in static file serving by default. That means:

- `middleware.StaticConfig.EnablePathUnescaping = true` re-opens the exact mismatch the fix closed. Turn it on only if you genuinely serve files whose names contain encoded characters, and only if no middleware makes authorization decisions from the path. `DisablePathUnescaping` is deprecated and now ignored.
- The router's `RouterConfig.UnescapePathParamValues` is the same class of decision for path *parameters*.
- The pattern to recognise: `e.StaticFS("/", os.DirFS("public"))` next to route-prefix auth middleware. Authorize on the resource, not on the string in the URL.

**GHSA-pgvm-wxw2-hrv9** (Feb 2026) is the sibling bug: Windows path traversal via backslash in `middleware.Static`'s default filesystem. Serve from an explicit `fs.FS` (`os.DirFS`, `embed.FS`) rather than the default root-path filesystem, and keep `Browse: false` so directories are not listed.

### `c.RealIP()` trusts client headers on v4 and on v5.0.0

With `e.IPExtractor` unset, `RealIP()` reads `X-Forwarded-For`, then `X-Real-IP`, and only then falls back to `RemoteAddr` — so any client can choose the value. This is the default in **all of v4 (through v4.15.4) and in v5.0.0**. From **v5.1.0** the fallback was removed and `RealIP()` returns the socket peer address unless an extractor is configured.

```go
// behind a load balancer you control
e.IPExtractor = echo.ExtractIPFromXFFHeader(
    echo.TrustLoopback(true),
    echo.TrustPrivateNet(true),
)
// directly internet-facing
e.IPExtractor = echo.ExtractIPDirect()
```

Set it explicitly on every major — it is the only way the behaviour is unambiguous to a reader, and on v4 it is the fix. Until it is set, do not key rate limits, allow-lists, audit logs, or fraud signals on `RealIP()`.

### Object-level authorization (IDOR / BOLA)

Auth middleware answers *who*, never *which row*. Put the ownership predicate in the query:

```go
uid := c.Get("userID").(string)
order, err := repo.GetOrderForUser(ctx, c.Param("id"), uid) // ... WHERE id=$1 AND user_id=$2
```

Derive the tenant from the token, never from a request field. `c.Get` returns `any` — use the comma-ok assertion, because a bare `.(string)` panics on any route wired without the auth middleware, which turns a routing mistake into a 500 storm.

### Mass assignment

`Bind` sets every matching field, from body **and** path **and** (for bodyless methods) query. Binding into a GORM model lets a client set `is_admin` or `tenant_id`, potentially from the URL.

1. One request struct per endpoint containing only client-settable fields; map to the model field by field.
2. Prefer the single-source binders over `Bind` when only the body should be trusted.
3. `json:"-"` on any field of a shared struct that must never come from the wire.
4. Echo has no `DisallowUnknownFields` switch — the request struct **is** the allow-list.

### CORS

`middleware.CORSWithConfig` rejects `AllowOrigins: []string{"*"}` together with `AllowCredentials: true` and panics at construction. The hole left open is the escape hatch, which is named honestly:

```go
// WRONG: reflected origin + credentials — any site reads authenticated responses
middleware.CORSWithConfig(middleware.CORSConfig{
    UnsafeAllowOriginFunc: func(origin string) bool { return true },
    AllowCredentials:      true,
})
```

Use an explicit `AllowOrigins` list. If you must compute the origin, the function still has to match against a stored allow-list — never return `true` unconditionally.

### CSRF and cookie flags

`middleware.CSRF()` protects cookie/session auth; a Bearer-token API does not need it. The defaults are not production-ready:

| Default | Value | Do |
|---|---|---|
| `CookieSecure` | `false` | set `true` — otherwise the token cookie goes over plain HTTP |
| `CookieHTTPOnly` | `false` | set `true` unless client JS must read the token |
| `CookieSameSite` | `SameSiteDefaultMode` (no attribute emitted) | set `http.SameSiteLaxMode` explicitly |
| `CookieMaxAge` | `86400` (24 h) | shorten for high-value sessions |
| `TokenLookup` | `header:X-CSRF-Token` | keep; add `form:_csrf` only for HTML form posts |

Only `CookieSameSite: SameSiteNoneMode` forces `CookieSecure` on automatically. Populate `TrustedOrigins` when the app is legitimately used cross-origin. Apply the same flags to your session cookie.

### `middleware.Secure()` sends neither HSTS nor CSP by default

`DefaultSecureConfig` sets `X-XSS-Protection: 1; mode=block`, `X-Content-Type-Options: nosniff`, and `X-Frame-Options: SAMEORIGIN`. `HSTSMaxAge` is **0**, so the `Strict-Transport-Security` header is never emitted, and `ContentSecurityPolicy` is empty. `X-XSS-Protection` is a header modern browsers ignore.

```go
e.Use(middleware.SecureWithConfig(middleware.SecureConfig{
    HSTSMaxAge:            31536000,
    HSTSPreloadEnabled:    true,
    ContentSecurityPolicy: "default-src 'self'; frame-ancestors 'none'",
    ContentTypeNosniff:    "nosniff",
    XFrameOptions:         "DENY",
}))
```

Only send HSTS if every hostname on that domain is HTTPS-only; `includeSubdomains` is on unless you set `HSTSExcludeSubdomains`.

### Logging and error leakage

- v5's logger is `*slog.Logger`, so structured logging is built in — `c.Logger().Error("...", "order_id", id)`. In v4 the custom logger interface is not structured; wire your own `slog` middleware.
- `middleware.Logger()`'s default format includes the full URI with its query string. A token in a query parameter is then permanently in your log store. Customise the format to log the path only.
- Never put a wrapped driver error into `echo.NewHTTPError`'s message — that message goes to the client. Log the detail with a correlation ID; return the ID.
- Keep `DefaultHTTPErrorHandler(false)`. `true` puts `err.Error()` in the response body.

### Request size

Echo applies no body limit by default. Add the middleware and tighten it per group where the expected payload is small — the signature differs by major: `middleware.BodyLimit(2 * 1024 * 1024)` takes an `int64` of bytes in v5, and `middleware.BodyLimit("2M")` takes a string in v4. On v5 the internal `http.Server` gets a 30-second `ReadTimeout` and `WriteTimeout` by default (Echo sets them explicitly against gosec rule G112); **v4 sets none**, so a v4 service without `e.Server.ReadHeaderTimeout` is open to Slowloris.

### Supply chain

Both majors require a current Go toolchain (v5's `go.mod` requires Go 1.25). Echo's advisories in 2026 have been in the static-file path, so an app that serves files needs the patch level as much as the config. `echo-contrib` and `echo-jwt` are separate modules on their own release lines — `govulncheck ./...` covers them; a manual check of the Echo version alone does not. Everything else — `go.sum`, `GOPRIVATE` scope, toolchain pinning — is in `mir-backend-go` → Security.

## How this slots into the core pipeline

- **Gate 5 (Design):** state the major version. List values extracted from `echo.Context` that cross into goroutines and how the background context is detached. Confirm `e.Validator` is wired and that every handler calls `Validate`. Name the `IPExtractor` strategy, the CORS allow-list, whether the auth scheme needs CSRF, and whether any middleware authorizes on a path string.
- **Gate 6 (Implementation):** `middleware.Recover()` first; `e.Validator` set and `Bind` + `Validate` in every handler; `IPExtractor` configured; a single `HTTPErrorHandler` with `exposeError` false; ownership predicates in queries; body limit and server timeouts set; shutdown wired (v5 `StartConfig`, v4 `e.Shutdown`).
- **Gate 7 (Review):** reliability-reviewer checks items 1–6; security-reviewer works the Security section. The four most commonly missed: `Validate` never called, `IPExtractor` never set, `EnablePathUnescaping` turned on to "fix" a 404, and a v4 service with no `e.Server` timeouts.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Echo library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Node too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Go backend (goroutine leaks, data races, context propagation, typed-nil, defer-in-loop, `os.Root`, module checksums)? → **runtime tier** (`mir-backend-go`).
- A mechanical footgun of *Echo* (pooled `echo.Context`, `Bind`+`Validate`, `Pre` vs `Use`, `Recover()` order, `HTTPErrorHandler`, `StartConfig`, `IPExtractor`, `EnablePathUnescaping`)? → **here**.
- A *different* Go framework → `mir-backend-go-gin` or `mir-backend-go-fiber`. Never widen this one.
