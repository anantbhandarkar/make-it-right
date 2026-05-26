---
name: mir-backend-go-fiber
description: "Make It Right (Fiber module). Fiber web framework reliability augmentation for Go backends. Use alongside mir-backend and mir-backend-go when the target stack is Fiber — adds the mechanical footguns the runtime-agnostic tiers deliberately omit: fiber.Ctx and all its values (Body, Params, Headers) are pooled and reused after the handler returns (retaining them causes data corruption across requests), fasthttp incompatibility with net/http middleware and ecosystem, the Immutable setting that controls whether returned strings reference reused buffers, and graceful shutdown via app.ShutdownWithContext. TRIGGER only when the Go backend uses the Fiber framework — building, reviewing, or debugging a Fiber handler, middleware, or route. Always loads TOGETHER WITH mir-backend (generic gates) and mir-backend-go (Go runtime concerns: goroutine leaks, context propagation, data races, channel rules, typed-nil, defer-in-loop, slice aliasing, error discipline, WaitGroup); this module only adds Fiber library mechanics. SKIP for Gin, Echo, chi, stdlib net/http, or any non-Fiber stack, and for non-Go runtimes."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-go` (Go runtime model) → **this** (Fiber library mechanics). Run the gates first; load the Go runtime tier for goroutine lifecycle, context propagation, and race discipline; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (goroutine leaks, data races, context propagation, typed-nil, slice aliasing) live in `mir-backend-go` — not here.**

**Stack assumed:** `github.com/gofiber/fiber/v2`. Fiber is built on `fasthttp` — not on `net/http`. This distinction is the root of nearly every Fiber-specific footgun.

## The Fiber footguns AI walks into most

### 1. `fiber.Ctx` and all its values are pooled — retaining them is data corruption

This is Fiber's defining hazard and the one AI consistently misses. Fiber (via fasthttp) reuses the request context object from a `sync.Pool` after the handler returns. **Every string, byte slice, or struct value obtained from `fiber.Ctx` — `c.Body()`, `c.Params("id")`, `c.Get("X-Header")`, `c.Query("page")`, `c.BaseURL()` — references the reused buffer.** When the pool recycles the slot for the next request, those references now point at the next request's data. The result is silent cross-request data corruption that is almost impossible to reproduce deterministically.

- **You MUST copy any value you retain past the end of the handler or pass to a goroutine.** Use `utils.CopyString(s)` for strings, `c.Request().Body()` returns a byte slice that also requires a copy (copy the slice before the handler returns), or use `fiber`'s BodyParser to decode into a struct (struct fields are safe — they are copies):
  ```go
  // WRONG: id and body reference pooled buffers; the goroutine may read another request's data
  func handler(c *fiber.Ctx) error {
      id := c.Params("id")
      body := c.Body()
      go func() {
          process(id, body) // data corruption — buffers are reused after handler returns
      }()
      return c.SendStatus(fiber.StatusAccepted)
  }

  // RIGHT: copy strings; copy body bytes before the handler returns
  func handler(c *fiber.Ctx) error {
      id := utils.CopyString(c.Params("id"))
      body := make([]byte, len(c.Body()))
      copy(body, c.Body())
      go func() {
          process(id, body) // safe — copies survive pool recycle
      }()
      return c.SendStatus(fiber.StatusAccepted)
  }
  ```
- Alternatively, parse the body into a struct with `c.BodyParser(&req)` before spawning the goroutine; struct fields are allocated on the heap and are safe.
- This applies to **every** value extracted from `c`: params, query strings, headers, body, locals. When in doubt, copy.
- The bug is not a race in the Go memory-model sense (the pool recycles the slot *after* the handler returns) — it is a logical data corruption. The race detector will **not** catch it.

### 2. fasthttp ≠ net/http — standard middleware and libraries may not work

Fiber's underlying engine is `fasthttp`, which provides its own `RequestCtx` and does not implement `http.Handler` or `http.ResponseWriter`. Any Go library that expects `net/http` types — standard middleware, `net/http`-based auth libraries, OpenTelemetry instrumentation designed for `net/http`, etc. — **cannot be used directly with Fiber**.

- Do not import `net/http` middleware and expect it to compose with Fiber handlers. Look for Fiber-native adapters in the `gofiber/contrib` repository, or write a thin adapter.
- Some popular packages (e.g. certain JWT libraries, OpenTelemetry HTTP contrib) ship both a `net/http` and a `fasthttp`/Fiber variant. Check before assuming compatibility.
- If a library only has a `net/http` interface and no Fiber adapter exists, wrap the Fiber handler into a `net/http` adapter using `fasthttpadaptor.NewFastHTTPHandler` / `fasthttpadaptor.NewFastHTTPHandlerFunc` — but be aware these adapters do *not* solve the buffer-pooling problem for values you extract via `fiber.Ctx`.
- AI routinely adds `net/http` middleware to a Fiber app without checking compatibility. Always verify the ecosystem tier: does this lib export a Fiber middleware, a fasthttp handler, or only a `net/http.Handler`?

### 3. `Immutable` setting — returned strings reference reused buffers by default

By default, strings returned by Fiber's API (params, query, headers) reference the underlying fasthttp buffer directly for zero-allocation performance. These strings become invalid after the handler returns (same root cause as footgun 1). Enabling `Immutable: true` on the app config makes Fiber copy all strings before returning them — safe to hold across handler boundaries, but at the cost of allocations.

- **Default (`Immutable: false`):** fast, but every string you retain or pass to a goroutine must be explicitly copied via `utils.CopyString`.
- **`Immutable: true`:** strings are safe to retain; simpler code, more allocations. Prefer this setting for services where correctness is worth the allocation overhead — most CRUD APIs.
  ```go
  app := fiber.New(fiber.Config{
      Immutable: true,
  })
  ```
- **Document the choice in the project.** A codebase where some files were written assuming `Immutable: true` and others were written assuming `false` will have latent corruption bugs. Pick one and enforce it.
- Even with `Immutable: true`, the raw `c.Body()` byte slice is NOT copied — body bytes still require an explicit copy or `BodyParser` before being retained.

### 4. Graceful shutdown via `app.ShutdownWithContext`

Unlike `net/http`'s `http.Server.Shutdown`, Fiber exposes its own shutdown method. Using `os.Exit` directly or ignoring SIGTERM drops in-flight requests and leaks resources (DB connections, locks).

- **Wire `app.ShutdownWithContext(ctx)` to SIGTERM:**
  ```go
  app := fiber.New()

  go func() {
      if err := app.Listen(":8080"); err != nil && !errors.Is(err, fiber.ErrServiceUnavailable) {
          log.Fatalf("listen: %v", err)
      }
  }()

  quit := make(chan os.Signal, 1)
  signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
  <-quit

  ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
  defer cancel()
  if err := app.ShutdownWithContext(ctx); err != nil {
      log.Fatalf("forced shutdown: %v", err)
  }
  ```
- `app.Shutdown()` (without context) blocks indefinitely if requests do not complete — always use `ShutdownWithContext` with a bounded deadline.
- Close DB pools and other resources after `ShutdownWithContext` returns.

### 5. `Recover` middleware and per-goroutine panic recovery

Fiber's `recover` middleware (`gofiber/fiber/v2/middleware/recover`) catches panics in the handler goroutine and converts them to 500 responses. Register it as the first middleware.

- Register early:
  ```go
  app.Use(recover.New())
  ```
- This only covers panics on the handler goroutine. Goroutines you spawn inside a handler (e.g. background processing) need their own `recover` — see `mir-backend-go` footgun 5.
- The default `recover` handler logs via `fmt.Fprintf(os.Stderr, ...)`. In production, customize it to emit structured logs and optionally propagate a trace ID from the (copied) context.

## How this slots into the core pipeline

- **Gate 5 (Design):** identify which values are extracted from `c` and used outside the handler boundary (goroutines, queues, stored locals). Plan explicit copies or `BodyParser`-into-struct for each. Decide on `Immutable` mode and document it.
- **Gate 6 (Implementation):** every value passed to a goroutine is explicitly copied (`utils.CopyString`, byte `copy`, or struct via `BodyParser`); `Immutable` mode is consistent across the codebase; `app.ShutdownWithContext` wired to SIGTERM; `recover` middleware registered first.
- **Gate 7 (Review):** reliability-reviewer checks items 1–5 above; confirm no `net/http` middleware is used without a Fiber/fasthttp adapter; confirm `Immutable` setting is documented; verify graceful shutdown present in `main`.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Fiber library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Node too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Go backend (goroutine leaks, data races, context propagation, typed-nil, defer-in-loop)? → **runtime tier** (`mir-backend-go`).
- A mechanical footgun of *Fiber/fasthttp* (pooled `Ctx` buffers, `Immutable` setting, `net/http` incompatibility, `ShutdownWithContext`, `recover` middleware)? → **here**.
- A *different* Go framework (Gin, Echo) → its own `mir-backend-go-<framework>` module. Never widen this one.
