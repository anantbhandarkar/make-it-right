---
name: mir-backend-go
description: "Make It Right (Go runtime tier). Go 1.22+ runtime reliability footguns shared across every Go backend framework (Gin, Fiber, Echo, chi, stdlib net/http) — distinct from the generic backend gates and from any one framework's mechanics. Covers: goroutine leaks (the #1 Go reliability bug), context propagation and cancellation, data races on shared state, channel ownership rules, goroutine-level panic recovery, the nil-interface/nil-pointer trap, defer-in-loop resource buildup, slice aliasing and backing-array mutation, error wrapping discipline, and sync.WaitGroup misuse. TRIGGER when the backend runtime is Go — sits between mir-backend (generic) and the framework module (e.g. mir-backend-go-gin). SKIP for Python/Node/JVM/Rust/.NET/Ruby/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics (those live in the framework module)."
trigger: /mir-backend-go
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-go · Make It Right (Go runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-go-gin`) knows the **library's mechanics**. This tier owns what is true for **all Go backends because they run on the Go runtime** — the goroutine scheduler, memory model, and process model that Gin, Fiber, Echo, chi, and raw `net/http` all inherit.

**Runtime assumed:** Go 1.22+. Load order: `mir-backend` → `mir-backend-go` → `<framework module>`.

## The Go footguns AI walks into (framework-agnostic)

### 1. Goroutine leaks — the #1 Go reliability bug

A goroutine blocked forever on a channel send or receive with no exit path leaks memory and any handles (DB connections, file descriptors) it holds. The runtime never garbage-collects a blocked goroutine. Under load, hundreds of leaked goroutines exhaust memory silently.

- **Always give every goroutine a way to exit.** The standard mechanism: accept a `context.Context` as the first argument and `select` on `ctx.Done()` alongside the blocking operation.
- Leak pattern — no exit:
  ```go
  // WRONG: goroutine blocked forever if nobody reads ch
  go func() { ch <- result }()
  ```
- Fixed — context exit:
  ```go
  go func() {
      select {
      case ch <- result:
      case <-ctx.Done():
      }
  }()
  ```
- Use `goleak` (uber-go/goleak) in tests to catch leaks automatically.
- Long-running worker loops must `select` on `ctx.Done()` in every iteration.

### 2. Context propagation and cancellation

`context.Context` is the Go runtime's cancellation, deadline, and value bus. Failing to propagate it means cancellation and timeouts are silently ignored — requests pile up even after the client disconnects.

- **Pass `ctx` as the first argument through the entire call chain** — every function that does I/O, calls an external service, or blocks must accept and forward the context.
- **Honor `ctx.Done()` on every blocking operation**: DB queries, HTTP calls, gRPC calls, queue publishes.
- **Derive per-operation timeouts with `context.WithTimeout`**; this bounds downstream latency and prevents cascading stalls:
  ```go
  ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
  defer cancel() // always call cancel to release resources
  row, err := db.QueryRowContext(ctx, query, args...)
  ```
- Never store a context in a struct field for later use — pass it at call time. Stored contexts outlive their validity.
- `context.Background()` is only for top-level entry points (main, test setup). Everywhere else, derive from the incoming ctx.

### 3. Data races on shared maps, slices, and structs

Go's runtime panics on a concurrent map write (`fatal error: concurrent map read and map write`). Races on other types produce silent data corruption that surfaces intermittently in production but may be invisible in tests.

- **Any state read or written from more than one goroutine must be protected** with `sync.Mutex`, `sync.RWMutex`, `sync/atomic`, or a dedicated owner goroutine (the channel-passing pattern).
- Never pass a `map` or `[]T` to a goroutine and also read/write it from the caller without synchronization.
- **Test every concurrent package with `go test -race`.** The race detector catches most write-write and write-read races with very low false-positive rate. Add it to CI:
  ```
  go test -race ./...
  ```
- `sync.Map` is appropriate only when writes are rare and reads dominate, or when keys are disjoint across goroutines. For general use, a mutex-guarded map is clearer.

### 4. Channel ownership and closing rules

Misusing channels is the second most common source of Go panics after nil dereferences.

- **Only the sender closes a channel.** A receiver must never close. Closing from two goroutines panics.
- **Sending on a closed channel panics** — never close a channel that another goroutine may still send to.
- **`range` over a channel reads until the channel is closed** — if you intend to drain a channel with `range`, the sender must close it when done; otherwise the loop hangs.
- Unbuffered send with no receiver blocks the sender goroutine forever — see footgun 1. Use buffered channels when the producer must not block, but beware that a full buffer just defers the block.
- Prefer `close(done)` as a broadcast signal to multiple goroutines over sending N values.

### 5. Panic in a goroutine crashes the whole process

An unrecovered panic in any goroutine terminates the process — unlike exceptions in most runtimes, there is no per-goroutine catch-all. This means a single malformed request can bring down the server.

- **`recover()` only works in the same goroutine and only in a deferred function.** A top-level `recover` in `main` does not catch panics in spawned goroutines.
- Every goroutine that handles per-request or per-task work must have its own `recover`:
  ```go
  go func() {
      defer func() {
          if r := recover(); r != nil {
              log.Printf("recovered panic: %v\n%s", r, debug.Stack())
          }
      }()
      doWork(ctx)
  }()
  ```
- Web frameworks (Gin Recovery(), Echo Recover(), Fiber Recover()) install this at the HTTP layer — but goroutines *you* spawn inside a handler are not covered by the framework middleware and need their own recover.

### 6. Nil interface vs. nil pointer — the typed-nil trap

The most common source of surprising `if err != nil` failures in Go. An interface value is nil only when **both** its type and value are nil. A non-nil interface holding a nil pointer is non-nil.

```go
// WRONG: returns a non-nil *MyError even though the pointer is nil
func getError() error {
    var e *MyError
    if noError {
        return e // interface holds (*MyError, nil) — not nil!
    }
    ...
}

// RIGHT
func getError() error {
    if noError {
        return nil // bare nil — both type and value are nil
    }
    ...
}
```

- When a function returns a concrete error type, return the bare untyped `nil`, not a typed nil pointer.
- When checking: `errors.Is` / `errors.As` are safe. `== nil` on an interface is the trap.

### 7. `defer` inside a loop — resource buildup until function return

`defer` statements execute when the **enclosing function** returns, not when the block or loop iteration exits. Deferring `rows.Close()` or `f.Close()` inside a loop keeps every handle open until the whole function returns.

```go
// WRONG: all files stay open until processAll() returns
func processAll(paths []string) error {
    for _, p := range paths {
        f, err := os.Open(p)
        if err != nil { return err }
        defer f.Close() // deferred to end of processAll, not end of iteration
        process(f)
    }
}

// RIGHT: wrap the body in an inline func so defer fires per iteration
func processAll(paths []string) error {
    for _, p := range paths {
        if err := func() error {
            f, err := os.Open(p)
            if err != nil { return err }
            defer f.Close()
            return process(f)
        }(); err != nil {
            return err
        }
    }
}
```

### 8. Slice aliasing and backing-array mutation

Slices in Go are a view over a backing array. `append` may or may not allocate a new array depending on capacity, and sub-slices share the same backing memory. Passing a slice to a goroutine while still appending to it in the caller is a data race (see footgun 3) and causes silent corruption.

- If you need an independent copy, use `copy`:
  ```go
  dst := make([]T, len(src))
  copy(dst, src)
  ```
- When passing a slice to a goroutine, either pass a copy, or ensure the goroutine only reads after the caller is done writing.
- `append(s, x)` may silently reuse capacity from a parent slice; if a parent slice was sliced then appended to, the parent's backing array may be mutated. Use `s[lo:hi:hi]` (three-index slice) to cap capacity and force an allocation on the first append.

### 9. Error discipline — always check, always wrap

Go has no exceptions; ignored errors are discarded results. The compiler enforces that errors must be assigned, but does not enforce that they are checked.

- **Never discard an error** with `_` unless you have a documented reason. The silent `_ = f.Write(...)` pattern hides I/O failures.
- **Wrap errors with context** using `fmt.Errorf("context: %w", err)` so callers and logs show the full chain without losing the sentinel for `errors.Is` / `errors.As`.
- Returning a bare `err` from a deep call loses all context about where it happened — add a short annotation at each layer boundary.
- `log.Fatal` / `os.Exit` inside a library are forbidden — they bypass `defer` cleanup and callers cannot handle them. Return errors; let the entry point decide whether to exit.

### 10. `sync.WaitGroup` misuse — early return or hang

`sync.WaitGroup` is correct only when `Add` is called **before** the goroutine starts and `Done` is **always** called exactly once (use `defer`).

```go
// WRONG: Add inside the goroutine — race between Add and Wait
var wg sync.WaitGroup
for _, item := range items {
    go func(i Item) {
        wg.Add(1)        // may not execute before wg.Wait()
        defer wg.Done()
        process(i)
    }(item)
}
wg.Wait()

// RIGHT: Add before the goroutine is spawned
var wg sync.WaitGroup
for _, item := range items {
    wg.Add(1)
    go func(i Item) {
        defer wg.Done()
        process(i)
    }(item)
}
wg.Wait()
```

- If `Done` is not in a `defer`, a panic in the goroutine body skips `Done` and `Wait` hangs forever.
- For complex fan-out-fan-in patterns with cancellation, prefer `errgroup.Group` (`golang.org/x/sync/errgroup`) — it combines WaitGroup + first-error capture + context cancellation in one.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** state the concurrency model (goroutine-per-request, worker pool, channel pipeline, single-owner goroutine for shared state) and justify it. A design that passes shared state without synchronization is a runtime-level defect — flag it here, not during code review.
- **Gate 6 (implementation):** every goroutine has a context exit path; every shared resource has explicit ownership; race detector runs in CI (`go test -race`).
- **Gate 7 (review):** the reliability-reviewer checks all 10 items above for any Go service. Pay particular attention to goroutine leaks (footgun 1) and data races (footgun 3) — they are silent in development and catastrophic in production.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability) → **up** to `mir-backend`.
- A specific library's mechanics (Gin `c.Copy()`, Fiber pooled `Ctx`, Echo `Bind`+`Validator`, GORM session scope) → **down** to the framework module (`mir-backend-go-<framework>`).
- **Here:** only what every Go backend shares because of the Go runtime and memory model — goroutine lifecycle, context propagation, the race detector, channel semantics, typed-nil, defer scoping, slice aliasing, error discipline, WaitGroup contract.
- A different runtime (Python, Node, JVM…) → its own `mir-backend-<runtime>` tier. Never widen this one.
