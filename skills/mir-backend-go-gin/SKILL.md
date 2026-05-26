---
name: mir-backend-go-gin
description: "Make It Right (Gin module). Gin web framework reliability augmentation for Go backends. Use alongside mir-backend and mir-backend-go when the target stack is Gin — adds the mechanical footguns the runtime-agnostic tiers deliberately omit: *gin.Context is request-scoped and must be copied before passing to a spawned goroutine, binding and validation discipline (ShouldBindJSON + struct tags, separate request vs DB model), middleware ordering (Recovery before your handlers), missing graceful shutdown wiring (http.Server.Shutdown on SIGTERM), and route group auth guard placement. TRIGGER only when the Go backend uses the Gin framework — building, reviewing, or debugging a Gin handler, middleware, or router. Always loads TOGETHER WITH mir-backend (generic gates) and mir-backend-go (Go runtime concerns: goroutine leaks, context propagation, data races, channel rules, typed-nil, defer-in-loop, slice aliasing, error discipline, WaitGroup); this module only adds Gin library mechanics. SKIP for Fiber, Echo, chi, stdlib net/http, or any non-Gin stack, and for non-Go runtimes."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-go` (Go runtime model) → **this** (Gin library mechanics). Run the gates first; load the Go runtime tier for goroutine lifecycle, context propagation, and race discipline; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (goroutine leaks, data races, context propagation, typed-nil, slice aliasing) live in `mir-backend-go` — not here.**

**Stack assumed:** `github.com/gin-gonic/gin` v1.9+. If the project uses `gin-contrib/*` middleware or GORM, note the interaction before applying these.

## The Gin footguns AI walks into most

### 1. `*gin.Context` is request-scoped — never retain it across the handler boundary

`*gin.Context` is reused from a sync pool after the handler returns. Passing a `*gin.Context` reference to a goroutine spawned inside the handler and then accessing it after the handler returns causes a data race on the pooled object — you get another request's data or a crash.

- **Call `c.Copy()` before passing `*gin.Context` to any goroutine.** The copy is safe to read from a different goroutine after the handler returns; it does not hold a reference to the pool slot.
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
- If you only need specific values, extract them into plain variables before spawning — that is even safer than `c.Copy()` and avoids carrying the context object at all.
- This applies to any async work: goroutines, `errgroup`, task queues.

### 2. Binding and validation — ShouldBindJSON + struct tags, never bind to the DB model

Gin's `ShouldBindJSON` deserializes JSON but performs **no validation** beyond checking required fields unless you use struct tags (`binding:"required"`, `binding:"min=1"`, `binding:"email"`, etc. via `go-playground/validator` which Gin embeds). AI routinely binds requests directly to the ORM/DB struct, which introduces mass assignment vulnerabilities.

- Use `ShouldBindJSON` (not `BindJSON`) so that binding failure returns an error rather than writing a 400 and aborting the handler — you control the error response:
  ```go
  var req CreateOrderRequest
  if err := c.ShouldBindJSON(&req); err != nil {
      c.JSON(http.StatusBadRequest, gin.H{"error": err.Error()})
      return
  }
  ```
- Define a **separate request struct** (`CreateOrderRequest`) with validation tags. Never bind into your DB model — the model may expose `ID`, `CreatedAt`, `IsAdmin`, `TenantID` fields that the client must not set.
- Annotate required fields: `binding:"required"`. Add domain constraints: `binding:"min=0,max=10000"`, `binding:"oneof=pending paid"`, `binding:"email"`.
- Validate **authorization** separately — a valid, well-formed request can still be a request to modify someone else's resource (IDOR). Binding validation is not authorization.

### 3. Middleware order — Recovery() must wrap your handlers, not the other way around

Gin processes middleware in registration order. If you register your own middleware before `gin.Recovery()`, a panic in your middleware is not caught and crashes the process.

- **Register `gin.Recovery()` first** (or early) on the router/group so it is the outermost wrapper:
  ```go
  r := gin.New()          // don't use gin.Default() in production — it logs with the default logger
  r.Use(gin.Recovery())   // catch panics from ALL subsequent middleware and handlers → 500
  r.Use(myLogger())
  r.Use(myAuth())
  ```
- `gin.Default()` includes `Logger` and `Recovery`, but in development-friendly formats. In production, inject a structured logger (Zap, slog) via a custom middleware before shipping.
- Recovery catches panics in handlers *on the same goroutine as the request*. Goroutines you spawn inside a handler need their own `recover` — see `mir-backend-go` footgun 5.

### 4. Missing graceful shutdown — in-flight requests are dropped on SIGTERM

Gin's `r.Run()` starts `net/http`'s `ListenAndServe`, which does **not** drain in-flight requests when the process receives a signal. Kubernetes and other orchestrators send SIGTERM before SIGKILL; if you call `r.Run()` directly, the process exits mid-request, causing 502s, partial writes, and corrupted state.

- **Wire `http.Server.Shutdown(ctx)` to SIGTERM explicitly:**
  ```go
  srv := &http.Server{
      Addr:    ":8080",
      Handler: r,
  }

  go func() {
      if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
          log.Fatalf("listen: %v", err)
      }
  }()

  quit := make(chan os.Signal, 1)
  signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
  <-quit

  ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
  defer cancel()
  if err := srv.Shutdown(ctx); err != nil {
      log.Fatalf("forced shutdown: %v", err)
  }
  ```
- The `Shutdown` timeout must be long enough to drain the slowest expected request. The context passed to `Shutdown` controls the drain window, not individual handler timeouts.
- Close DB connection pools and other resources after `Shutdown` returns (connections are idle by then).

### 5. Route group auth guard placement

Gin allows attaching middleware to route groups. AI often places auth middleware incorrectly — either too broadly (catching routes that should be public) or at the wrong group level (an inner group that excludes some routes).

- **Structure routes so auth middleware wraps exactly the protected group:**
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
- Do not add auth to the root router if you have public routes — add it to the group.
- The auth middleware should set the authenticated identity in the Gin context (`c.Set("user", user)`) so downstream handlers can retrieve it without re-querying.
- Object-level authorization (does this user own this order?) is NOT the auth middleware's job — it must be checked in the handler or a dedicated middleware on that specific route. Middleware that only checks "is the user logged in?" does not prevent IDOR.

## How this slots into the core pipeline

- **Gate 5 (Design):** state handler boundaries, which routes are protected, and how `*gin.Context` values flow into any async work. Identify any binding struct that doubles as a DB model — separate them.
- **Gate 6 (Implementation):** every goroutine spawned inside a handler uses `c.Copy()` or copies specific values; `ShouldBindJSON` + validation tags used everywhere; `http.Server.Shutdown` wired to SIGTERM.
- **Gate 7 (Review):** reliability-reviewer checks items 1–5 above; confirm `gin.Recovery()` is registered, binding structs are separate from DB models, and graceful shutdown is present in `main`.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Gin library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Node too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Go backend (goroutine leaks, data races, context propagation, typed-nil, defer-in-loop)? → **runtime tier** (`mir-backend-go`).
- A mechanical footgun of *Gin* (`c.Copy()`, `ShouldBindJSON` + validation tags, `Recovery()` order, `http.Server.Shutdown`, route group auth)? → **here**.
- A *different* Go framework (Fiber, Echo) → its own `mir-backend-go-<framework>` module. Never widen this one.
