---
name: mir-backend-go-echo
description: "Make It Right (Echo module). Echo web framework reliability augmentation for Go backends. Use alongside mir-backend and mir-backend-go when the target stack is Echo — adds the mechanical footguns the runtime-agnostic tiers deliberately omit: echo.Context is request-scoped and must not be retained across the handler boundary or passed to a goroutine without copying values, Bind + Validator separation (Bind alone does not validate — you must install a custom Validator and call Validate explicitly), middleware ordering (Recover() before your middleware), HTTPErrorHandler for consistent error shape, and graceful shutdown via e.Shutdown(ctx). TRIGGER only when the Go backend uses the Echo framework — building, reviewing, or debugging an Echo handler, middleware, or router. Always loads TOGETHER WITH mir-backend (generic gates) and mir-backend-go (Go runtime concerns: goroutine leaks, context propagation, data races, channel rules, typed-nil, defer-in-loop, slice aliasing, error discipline, WaitGroup); this module only adds Echo library mechanics. SKIP for Gin, Fiber, chi, stdlib net/http, or any non-Echo stack, and for non-Go runtimes."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-go` (Go runtime model) → **this** (Echo library mechanics). Run the gates first; load the Go runtime tier for goroutine lifecycle, context propagation, and race discipline; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (goroutine leaks, data races, context propagation, typed-nil, slice aliasing) live in `mir-backend-go` — not here.**

**Stack assumed:** `github.com/labstack/echo/v4`. Notes apply to Echo v4; v5 (pre-release) has a different context model.

## The Echo footguns AI walks into most

### 1. `echo.Context` is request-scoped — do not retain it across the handler boundary

Echo creates one `echo.Context` per request. The underlying `net/http` request and response objects are replaced per request, and some Echo versions pool the context. Storing `echo.Context` in a struct or passing it to a goroutine and reading it after the handler returns results in a data race — you read the next request's data or a zeroed struct.

- Extract specific values before spawning any goroutine and pass those values (plain strings, ints, typed structs) instead of the context:
  ```go
  // WRONG: c is reused/zeroed after the handler returns
  func handler(c echo.Context) error {
      go func() {
          sendNotification(c.Param("id"), c.Request().Header.Get("X-Trace-Id"))
      }()
      return c.NoContent(http.StatusAccepted)
  }

  // RIGHT: capture before spawning
  func handler(c echo.Context) error {
      id := c.Param("id")
      traceID := c.Request().Header.Get("X-Trace-Id")
      go func() {
          sendNotification(id, traceID)
      }()
      return c.NoContent(http.StatusAccepted)
  }
  ```
- For longer async work that needs the full request context (auth identity, tenant, locale), copy the values you need into a plain struct before spawning.
- Context values stored via `c.Set(key, value)` are also in the per-request map — extract them before the handler returns if a goroutine needs them.

### 2. `Bind` does not validate — you must install a Validator and call `Validate` explicitly

This is Echo's most commonly misunderstood design decision. `c.Bind(&req)` deserializes the request (JSON, form, query params) into the struct, but it **does not run validation**. Without an explicit call to `c.Validate(&req)`, invalid or missing fields silently pass through to your business logic.

- Install a validator once at startup. The canonical choice is `go-playground/validator/v10`:
  ```go
  type customValidator struct {
      validator *validator.Validate
  }
  func (cv *customValidator) Validate(i interface{}) error {
      return cv.validator.Struct(i)
  }

  e := echo.New()
  e.Validator = &customValidator{validator: validator.New()}
  ```
- In every handler, call `Bind` **and then** `Validate`:
  ```go
  var req CreateOrderRequest
  if err := c.Bind(&req); err != nil {
      return echo.NewHTTPError(http.StatusBadRequest, err.Error())
  }
  if err := c.Validate(&req); err != nil {
      return echo.NewHTTPError(http.StatusUnprocessableEntity, err.Error())
  }
  ```
- Annotate request structs with `validate:"required"`, `validate:"min=1"`, `validate:"email"` etc. from `go-playground/validator`. These tags are ignored unless a Validator is installed.
- AI routinely ships code that calls `Bind` but never sets `e.Validator` and never calls `Validate` — the handler processes garbage input.
- **Never bind to your DB/ORM model** — use a dedicated request struct. Same mass-assignment hazard as other frameworks: DB model fields like `ID`, `IsAdmin`, `TenantID` must not be settable from request JSON.

### 3. Middleware order — `Recover()` must be registered first

Echo processes middleware in registration order. If your own middleware (auth, logging, rate-limiting) runs before `middleware.Recover()`, a panic in one of those middleware functions crashes the process.

- **Register `middleware.Recover()` as the very first `Use` call:**
  ```go
  e := echo.New()
  e.Use(middleware.Recover())   // catch panics from ALL subsequent middleware and handlers
  e.Use(middleware.Logger())
  e.Use(authMiddleware())
  ```
- Echo's `Recover` middleware converts panics to HTTP 500 responses and logs the stack trace. Customize the config (`middleware.RecoverConfig`) to emit structured logs.
- Recover only covers panics on the handler goroutine. Goroutines spawned inside a handler need their own `recover` — see `mir-backend-go` footgun 5.

### 4. `HTTPErrorHandler` — consistent error shape across the whole API

By default, Echo converts `echo.HTTPError` values to JSON responses and logs other errors with a generic 500. AI often returns raw Go errors (e.g. `return err`) or constructs inconsistent JSON shapes in each handler, producing a non-uniform error contract for API consumers.

- **Register a custom `HTTPErrorHandler` once** to enforce a single error response shape:
  ```go
  e.HTTPErrorHandler = func(err error, c echo.Context) {
      var he *echo.HTTPError
      if errors.As(err, &he) {
          _ = c.JSON(he.Code, map[string]interface{}{
              "error": he.Message,
          })
          return
      }
      // log unexpected errors; don't leak internals to the client
      e.Logger.Error(err)
      _ = c.JSON(http.StatusInternalServerError, map[string]interface{}{
          "error": "internal server error",
      })
  }
  ```
- Return `echo.NewHTTPError(statusCode, message)` from handlers and middleware; the error handler shapes the response.
- Never return raw DB errors, internal struct fields, or stack traces in the error message body — those are information-disclosure vulnerabilities.

### 5. Graceful shutdown via `e.Shutdown(ctx)`

Echo wraps `net/http` and exposes `e.Shutdown(ctx)` which drains in-flight requests before exiting. Using `e.Close()` (immediate) or ignoring SIGTERM drops in-flight requests and leaves DB connections stranded.

- **Wire `e.Shutdown(ctx)` to SIGTERM:**
  ```go
  e := echo.New()
  e.HideBanner = true

  go func() {
      if err := e.Start(":8080"); err != nil && !errors.Is(err, http.ErrServerClosed) {
          e.Logger.Fatal("shutting down the server")
      }
  }()

  quit := make(chan os.Signal, 1)
  signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
  <-quit

  ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
  defer cancel()
  if err := e.Shutdown(ctx); err != nil {
      e.Logger.Fatal(err)
  }
  ```
- The context deadline passed to `Shutdown` bounds the drain window. Set it to the maximum expected request duration plus a small buffer.
- Close DB pools and other resources after `e.Shutdown(ctx)` returns; at that point no handler is executing.

## How this slots into the core pipeline

- **Gate 5 (Design):** identify values extracted from `echo.Context` that cross the handler boundary into goroutines. Confirm `e.Validator` is wired and `Validate` is called. Confirm a custom `HTTPErrorHandler` is planned.
- **Gate 6 (Implementation):** every goroutine spawned inside a handler uses extracted values, not `echo.Context`; every handler calls `Bind` + `Validate`; `middleware.Recover()` registered first; `e.Shutdown(ctx)` wired to SIGTERM; custom `HTTPErrorHandler` installed.
- **Gate 7 (Review):** reliability-reviewer checks items 1–5 above; confirm no handler returns raw errors; confirm request structs are separate from DB models; verify graceful shutdown in `main`.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Echo library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Node too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Go backend (goroutine leaks, data races, context propagation, typed-nil, defer-in-loop)? → **runtime tier** (`mir-backend-go`).
- A mechanical footgun of *Echo* (request-scoped `echo.Context`, `Bind`+`Validate` separation, `Recover()` order, `HTTPErrorHandler`, `e.Shutdown(ctx)`)? → **here**.
- A *different* Go framework (Gin, Fiber) → its own `mir-backend-go-<framework>` module. Never widen this one.
