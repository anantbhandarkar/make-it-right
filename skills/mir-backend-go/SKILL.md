---
name: mir-backend-go
description: "Make It Right (Go runtime tier). Go 1.25/1.26 runtime reliability footguns shared across every Go backend framework (Gin, Fiber, Echo, chi, stdlib net/http) — distinct from the generic backend gates and from any one framework's mechanics. Covers: goroutine leaks (the #1 Go reliability bug) and the runtime goroutineleak profile, context propagation and cancellation, data races and `go test -race`, channel ownership rules, goroutine-level panic recovery, the nil-interface/nil-pointer trap, defer-in-loop resource buildup, slice aliasing, error wrapping with errors.Is/As/AsType, sync.WaitGroup.Go, the Go 1.22 per-iteration loop-variable change and its go.mod gating, deterministic concurrency tests with testing/synctest, container-aware GOMAXPROCS, log/slog structured logging, and Go-level security mechanics (http.Server timeouts, net/http CrossOriginProtection, os.Root path containment, SSRF dialer control, module checksum verification, govulncheck). TRIGGER when the backend runtime is Go — sits between mir-backend (generic gates) and the framework module. SKIP for Python/Node/JVM/Rust/.NET/Ruby/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics: Gin's c.Copy() and SetTrustedProxies go to mir-backend-go-gin, Fiber's pooled Ctx and fasthttp constraints to mir-backend-go-fiber, Echo's Bind/Validate and IPExtractor to mir-backend-go-echo."
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

**Runtime state, verified 13 Aug 2026.** Load order: `mir-backend` → `mir-backend-go` → `<framework module>`.

| Release | Status | Notes |
|---|---|---|
| Go 1.26 (1.26.5, 2026-07-07) | current stable | Green Tea GC on by default; `goroutineleak` profile behind `GOEXPERIMENT=goroutineleakprofile`; `errors.AsType`; `slog.NewMultiHandler` |
| Go 1.25 (1.25.12, 2026-07-07) | supported | `sync.WaitGroup.Go`; `testing/synctest` stable; container-aware `GOMAXPROCS`; `net/http.CrossOriginProtection` |
| Go 1.24 and older | **end of life** | No security patches. Go supports each major only until two newer majors exist |
| Go 1.27 | RC2 as of 2026-07-07, expected Aug 2026 | Final release not confirmed at time of writing — verify before relying on it |

**Set the floor at Go 1.25.** Anything below it is an unpatched toolchain. Note the two version knobs are different things: the *toolchain* you build with decides which security fixes you have; the `go` line in `go.mod` decides which *language and GODEBUG defaults* apply (see footgun 11).

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
- Long-running worker loops must `select` on `ctx.Done()` in every iteration.
- **Two detection tools, use both.** `uber-go/goleak` in tests fails a package when goroutines outlive it. The runtime's own `goroutineleak` profile finds leaks in a running process: it reports goroutines blocked on a concurrency primitive that is unreachable from any runnable goroutine, so it cannot become unblocked. Enable it on Go 1.26 with `GOEXPERIMENT=goroutineleakprofile` at build time and read `/debug/pprof/goroutineleak`. It is a regular profile in Go 1.27 (unreleased at time of writing).
- The profile misses leaks where the channel is still reachable through a global or through a live goroutine's locals. Keep `goleak` in tests; do not treat a clean profile as proof.

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
- Use `context.WithTimeoutCause` / `context.WithCancelCause` when you need to tell "client hung up" apart from "we hit our own deadline" in logs — `context.Cause(ctx)` returns the specific error, `ctx.Err()` only ever returns `DeadlineExceeded` or `Canceled`.
- Never store a context in a struct field for later use — pass it at call time. Stored contexts outlive their validity.
- `context.Background()` is only for top-level entry points (main, test setup). Everywhere else, derive from the incoming ctx.
- **Work that must outlive the request needs a detached context** — `context.WithoutCancel(ctx)` keeps the values (trace IDs, tenant) while dropping the cancellation. Apply your own timeout after detaching, because the derived context now has none. Detaching only survives the *handler*, not the *process*: if losing the work is unacceptable (payment capture, outbound email, webhook delivery), it belongs in a durable queue or transactional outbox, not a goroutine — the next deploy kills it after you already returned 202.

### 3. Data races on shared maps, slices, and structs

Go's runtime panics on a concurrent map write (`fatal error: concurrent map read and map write`). Races on other types produce silent data corruption that surfaces intermittently in production but may be invisible in tests.

- **Any state read or written from more than one goroutine must be protected** with `sync.Mutex`, `sync.RWMutex`, `sync/atomic`, or a dedicated owner goroutine (the channel-passing pattern).
- Never pass a `map` or `[]T` to a goroutine and also read/write it from the caller without synchronization.
- **Test every concurrent package with `go test -race`.** The race detector catches most write-write and write-read races with very low false-positive rate. Add it to CI:
  ```
  go test -race ./...
  ```
- The detector only reports races it actually observes. A single-shot test rarely interleaves — run contended tests with `-race -count=10` or drive them with `testing/synctest` (footgun 12).
- `sync.Map` is appropriate only when writes are rare and reads dominate, or when keys are disjoint across goroutines. For general use, a mutex-guarded map is clearer. Prefer the typed `atomic.Int64` / `atomic.Pointer[T]` types over the old `atomic.AddInt64(&x, 1)` function calls — the typed values cannot be read non-atomically by mistake.

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
              slog.Error("recovered panic", "panic", r, "stack", string(debug.Stack()))
          }
      }()
      doWork(ctx)
  }()
  ```
- Web frameworks (Gin `Recovery()`, Echo `middleware.Recover()`, Fiber `recover.New()`) install this at the HTTP layer — but goroutines *you* spawn inside a handler are not covered by the framework middleware and need their own recover.
- `sync.WaitGroup.Go` does **not** recover. A panic inside it still kills the process; it only guarantees `Done` is called on the way out.

### 6. Nil interface vs. nil pointer — the typed-nil trap

The most common source of surprising `if err != nil` failures in Go. An interface value is nil only when **both** its type and value are nil. A non-nil interface holding a nil pointer is non-nil.

```go
var e *MyError            // nil pointer
return e                  // WRONG: error interface holds (*MyError, nil) — err != nil is TRUE
return nil                // RIGHT: bare untyped nil
```

- A function whose *declared return type* is `error` must return bare `nil`, never a nil-valued concrete pointer. Same trap for a struct field or a channel of interface type.
- When checking: `errors.Is` / `errors.As` are safe. `== nil` on an interface is the trap.

### 7. `defer` inside a loop — resource buildup until function return

`defer` statements execute when the **enclosing function** returns, not when the block or loop iteration exits. Deferring `rows.Close()` or `f.Close()` inside a loop keeps every handle open until the whole function returns.

```go
for _, p := range paths {
    f, err := os.Open(p)
    if err != nil { return err }
    defer f.Close()          // WRONG: fires at end of the function, so every file stays open
}

// RIGHT: give defer its own function scope
for _, p := range paths {
    if err := func() error {
        f, err := os.Open(p)
        if err != nil { return err }
        defer f.Close()      // fires at end of this iteration
        return process(f)
    }(); err != nil {
        return err
    }
}
```

The same shape applies to `rows.Close()`, `mu.Unlock()`, and `tx.Rollback()` in a loop. On a long input list this is a file-descriptor or connection-pool exhaustion, not a leak you notice in tests.

### 8. Slice aliasing and backing-array mutation

Slices in Go are a view over a backing array. `append` may or may not allocate a new array depending on capacity, and sub-slices share the same backing memory. Passing a slice to a goroutine while still appending to it in the caller is a data race (see footgun 3) and causes silent corruption.

- If you need an independent copy, use `slices.Clone(src)` (or `make` + `copy` for pre-Go-1.21 code).
- When passing a slice to a goroutine, either pass a copy, or ensure the goroutine only reads after the caller is done writing.
- `append(s, x)` may silently reuse capacity from a parent slice; if a parent slice was sliced then appended to, the parent's backing array may be mutated. Use `s[lo:hi:hi]` (three-index slice) to cap capacity and force an allocation on the first append.

### 9. Error discipline — always check, always wrap

Go has no exceptions; ignored errors are discarded results. Nothing in the compiler stops you — a call used as a statement (`f.Write(b)`) throws away every return value including the error. The compiler only rejects an unused *local variable*, which is why `_ =` is the idiom that silences it.

- **Never discard an error** with `_` unless you have a documented reason. The silent `_ = f.Write(...)` pattern hides I/O failures.
- **Wrap errors with context** using `fmt.Errorf("context: %w", err)` so callers and logs show the full chain without losing the sentinel for `errors.Is` / `errors.As`.
- On Go 1.26+, `errors.AsType[*MyError](err)` replaces the two-line declare-then-`errors.As` dance and cannot be called with a non-pointer target by mistake.
- Returning a bare `err` from a deep call loses all context about where it happened — add a short annotation at each layer boundary.
- `log.Fatal` / `os.Exit` inside a library are forbidden — they bypass `defer` cleanup and callers cannot handle them. Return errors; let the entry point decide whether to exit.

### 10. `sync.WaitGroup` — use `wg.Go`, not `Add`/`Done` by hand

Go 1.25 added `WaitGroup.Go`, which does the `Add(1)` before the goroutine starts and the `defer Done()` inside it. Hand-rolled `Add`/`Done` is now the error-prone path — `Add` called inside the goroutine races with `Wait`, and a `Done` that is not deferred is skipped by a panic, hanging `Wait` forever.

```go
go func() { wg.Add(1); defer wg.Done(); process(item) }()  // WRONG: Add races with Wait()

wg.Go(func() { process(item) })                            // RIGHT (Go 1.25+)
```

- `wg.Go` still does not recover panics (footgun 5) and gives you no error channel. For fan-out that needs first-error capture and cancellation, use `errgroup.Group` (`golang.org/x/sync/errgroup`) and always bound it with `g.SetLimit(n)` — an unbounded fan-out over a user-controlled list is a self-inflicted denial of service.
- `errgroup.WithContext(ctx)` cancels the derived context on the first error; every worker must actually honour that context or the cancellation does nothing.

### 11. The `go` line in `go.mod`, not the toolchain, picks the language semantics

The toolchain you build with decides which security fixes you have. The `go` line decides which language rules and GODEBUG defaults apply. Building a `go 1.21` module with a 1.26 toolchain gives you 1.21's semantics.

- **Do not write `item := item` or `go func(i Item){...}(item)`.** Loop variables have been per-iteration since Go 1.22; that workaround is dead code. If you find it in existing code, it is a signal the module may still declare an old `go` line — check, do not copy the pattern.
- Same gating for GODEBUG defaults: a module on `go 1.24` does not get Go 1.25's container-aware `GOMAXPROCS` even on a 1.26 toolchain (footgun 13).
- **Fix once:** raise the `go` directive to your real floor, run `go fix ./...` (Go 1.26 made `go fix` the home of the modernizers), then `go vet`.

### 12. Concurrency tests that sleep are flaky — use `testing/synctest`

`time.Sleep(100 * time.Millisecond)` in a test is a guess about scheduling. It is slow when it passes and flaky when the CI box is loaded. `testing/synctest` (stable since Go 1.25) runs a test in a bubble with a fake clock: time advances instantly once every goroutine in the bubble is blocked.

```go
func TestExpiry(t *testing.T) {
    synctest.Test(t, func(t *testing.T) {
        c := NewCache(5 * time.Minute)
        c.Set("k", "v")
        time.Sleep(6 * time.Minute) // returns immediately; the bubble clock jumps
        synctest.Wait()             // let all bubble goroutines settle
        if _, ok := c.Get("k"); ok {
            t.Fatal("expected expiry")
        }
    })
}
```

- The Go 1.24 experiment API (`GOEXPERIMENT=synctest` with `synctest.Run`) was **removed in Go 1.26**. Code calling `synctest.Run` no longer compiles — migrate to `synctest.Test(t, f)`.
- Real network and disk I/O inside a bubble will not block the fake clock correctly. Bubble the logic; keep integration tests outside.

### 13. `GOMAXPROCS`, cgroup limits, and the memory limit

Since Go 1.25, `GOMAXPROCS` defaults to the cgroup CPU bandwidth limit on Linux when that is lower than the CPU count, and the runtime re-reads it periodically. This is the "Kubernetes CPU limit" case that previously required `go.uber.org/automaxprocs`.

- **Setting the `GOMAXPROCS` env var or calling `runtime.GOMAXPROCS(n)` pins the value and disables the periodic update.** If you still import `automaxprocs`, it does exactly that — drop it once the module's `go` line is 1.25 or later.
- Behaviour is off if the `go` line is below 1.25 (footgun 11) or if `GODEBUG=containermaxprocs=0` / `updatemaxprocs=0` is set.
- `GOMAXPROCS` does not bound memory. In a container, also set `GOMEMLIMIT` to roughly 80–90% of the memory limit — otherwise the GC only reacts to `GOGC` and the kernel OOM-kills the process before the GC decides it is under pressure.

### 14. Logging: `log/slog`, not `log.Printf` and not a third-party logger

`log/slog` has been in the standard library since Go 1.21. There is no reason to add Zap or Logrus for structured logging in new code.

```go
h := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo})
slog.SetDefault(slog.New(h))
slog.InfoContext(ctx, "order created", "order_id", id, "tenant_id", tid)
```

- Use the `...Context` variants (`InfoContext`, `ErrorContext`) everywhere. They pass the context to the handler, which is how a handler injects the trace/correlation ID without every call site repeating it.
- Prefer the typed attribute constructors (`slog.String`, `slog.Int64`) over loose key/value pairs in hot paths — a stray odd argument becomes a `!BADKEY` entry rather than a compile error.
- Go 1.26 added `slog.NewMultiHandler` for fan-out to more than one sink; you no longer need a third-party tee handler.
- Redaction is a type responsibility, not a call-site one — see Security below.

## Security

Go-runtime-level mechanics. Object-level authorization, mass assignment, and request binding are framework concerns — they live in `mir-backend-go-gin` / `-fiber` / `-echo`.

### `http.Server` ships with no timeouts

`http.ListenAndServe(addr, h)` and a bare `&http.Server{Addr: a, Handler: h}` leave `ReadTimeout`, `ReadHeaderTimeout`, `WriteTimeout`, and `IdleTimeout` all at zero, which means **no limit**. One slow client holding a connection open with a trickle of header bytes costs you a goroutine and a file descriptor indefinitely (Slowloris). Every framework built on `net/http` — Gin, Echo, chi — inherits this.

```go
srv := &http.Server{
    Addr:              ":8080",
    Handler:           h,
    ReadHeaderTimeout: 5 * time.Second,   // the Slowloris-specific one
    ReadTimeout:       15 * time.Second,
    WriteTimeout:      30 * time.Second,
    IdleTimeout:       60 * time.Second,
    MaxHeaderBytes:    1 << 20,           // this one does default to 1 MiB
}
```

Bodies are separate: `http.MaxBytesReader(w, r.Body, n)` (or the framework's body-limit middleware) caps a single request; without it, `io.ReadAll(r.Body)` or `json.Decode` will allocate whatever the client sends. Go 1.27 adds `Server.MaxHeaderValueCount` for repeated-header floods — unreleased at time of writing.

### CSRF: `net/http.CrossOriginProtection` (Go 1.25)

If your auth is a cookie or session, the browser attaches it to cross-site requests and you need CSRF protection. If your auth is an `Authorization: Bearer` header, you do not — the browser will not add that header for an attacker.

```go
p := http.NewCrossOriginProtection()
p.AddTrustedOrigin("https://admin.example.com")
srv.Handler = p.Handler(mux)
```

It rejects non-safe cross-origin requests using `Sec-Fetch-Site`, falling back to comparing `Origin` against `Host`. No token, no cookie. Two things to know: GET/HEAD/OPTIONS are always allowed, so a state-changing GET is still unprotected; and requests with neither header are allowed, which is what keeps non-browser clients working. `AddInsecureBypassPattern` is named that way for a reason — every pattern you add is an unprotected route.

### CORS: nothing in the standard library, and reflection is the bug

There is no `net/http` CORS implementation, so this comes from middleware. Two failure shapes:

| Config | What actually happens |
|---|---|
| `Access-Control-Allow-Origin: *` + `Access-Control-Allow-Credentials: true` | Browsers reject the combination, so it looks broken, and the usual "fix" is the next row |
| Echoing the request's `Origin` back + credentials | Any origin now reads authenticated responses. This is the real vulnerability |

Match `Origin` against an explicit allow-list. When the allow-list has more than one entry, set `Vary: Origin` or a shared cache will serve one tenant's `Access-Control-Allow-Origin` to another.

### Injection

| Vector | Wrong | Right |
|---|---|---|
| SQL | `db.Query(fmt.Sprintf("... WHERE id=%s", id))` | `db.QueryContext(ctx, "... WHERE id=$1", id)` — placeholders |
| SQL identifiers | placeholders (they do not work for table/column names) | map the user string through an allow-list before interpolating |
| Command | `exec.Command("sh", "-c", "convert "+name)` | `exec.CommandContext(ctx, "convert", name)` — separate args, no shell |
| HTML | `text/template`, or `template.HTML(userInput)` | `html/template` with plain `string` values so contextual escaping runs |

`html/template` escapes per context, which is why the escaping bugs are subtle: **CVE-2026-32289** was a mis-tracked JavaScript template-literal context producing XSS, fixed in Go 1.26.2 / 1.25.9. Patching the toolchain is part of the XSS defence.

### Path traversal: use `os.Root`, and patch it

`filepath.Join(base, userPath)` does not contain anything — `..` segments and symlinks both escape. `os.OpenRoot` (Go 1.24) returns an `*os.Root` whose operations cannot leave the directory, including through symlinks.

```go
root, err := os.OpenRoot("/srv/uploads")
defer root.Close()
f, err := root.Open(name) // name may be attacker-controlled
```

This primitive has had two escapes of its own, both fixed only in recent patch releases:

| CVE | Problem | Fixed in |
|---|---|---|
| CVE-2026-39822 | Trailing `/` on a final symlink component escaped the root on Unix | 1.26.5 / 1.25.12 |
| CVE-2026-32282 | `Root.Chmod` followed symlinks out of the root on Linux | 1.26.2 / 1.25.9 |

For archives, the same rule: never `filepath.Join(dest, header.Name)` from a tar or zip (zip-slip). Extract through an `*os.Root`, reject absolute names and `..`, and cap the output with `io.LimitReader` — **CVE-2026-32288** was unbounded allocation parsing a GNU sparse map in `archive/tar` (fixed 1.26.2 / 1.25.9).

### SSRF from user-supplied URLs

`http.Get(userURL)` will happily fetch `http://169.254.169.254/latest/meta-data/` and hand you cloud credentials. Validating the hostname before the request does not help — DNS can resolve to a private address, and can resolve differently on the second lookup (rebinding). **Check the IP the dialer is about to connect to**, using `Dialer.Control`, which runs after resolution and before connect:

```go
d := &net.Dialer{
    Control: func(network, address string, _ syscall.RawConn) error {
        host, _, _ := net.SplitHostPort(address)
        ip := net.ParseIP(host)
        if ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsUnspecified() {
            return fmt.Errorf("blocked address %s", ip)
        }
        return nil
    },
}
client := &http.Client{
    Transport: &http.Transport{DialContext: d.DialContext},
    Timeout:   10 * time.Second,
    CheckRedirect: func(r *http.Request, via []*http.Request) error {
        if len(via) >= 3 { return errors.New("too many redirects") }
        return nil // Control re-runs on each hop, so redirects are covered
    },
}
```

Always set `Client.Timeout` — the zero value is no timeout. And on **every** outbound call, not just this one: `defer resp.Body.Close()` after the error check, and read through `io.LimitReader(resp.Body, n)`. An unclosed body leaks the connection and its file descriptor; an unbounded `io.ReadAll` over a hostile response is remote memory exhaustion.

### Secret and PII leakage

- `%v` and `%+v` print every field. A struct holding an API key logs the API key. Give the type a `String() string` that redacts, or implement `slog.LogValuer` so `slog` substitutes a masked value wherever the type appears.
- Never return `err.Error()` from a DB driver or an internal call to a client — driver errors carry table names, column names, and sometimes parameter values. Log the wrapped error with a correlation ID; return a generic message and that ID.
- **`import _ "net/http/pprof"` registers `/debug/pprof/*` on `http.DefaultServeMux` in its `init()`.** Any `http.ListenAndServe(addr, nil)` in the same process then serves heap and goroutine dumps — which contain live request data — to the internet. Serve pprof on a separate listener bound to localhost or an internal port, with its own `ServeMux`.
- Access logs commonly record the full request URI. A token in a query string is then permanently in your log store. Log the path, not `RawQuery`.

### Deserialization of untrusted input

- **`encoding/gob` is not safe on untrusted input.** It instantiates arbitrary registered types and can be made to allocate without bound. Use JSON for anything crossing a trust boundary.
- Always decode into a purpose-built struct, never `map[string]any` that you then index — unknown-shape data becomes unchecked type assertions, which panic.
- `json.Decoder.DisallowUnknownFields()` turns silently-ignored extra keys into an error. This is the standard-library building block behind the framework-specific mass-assignment guards.
- Go 1.27 makes `encoding/json/v2` available with stricter defaults (duplicate object names and invalid UTF-8 are rejected) and reimplements `encoding/json` on top of it, with `GOEXPERIMENT=nojsonv2` as the escape hatch. Re-run your decoding tests when you upgrade; Go 1.27 was not final at time of writing.

### Randomness and comparisons

- `math/rand` and `math/rand/v2` are **not** cryptographic. Session IDs, CSRF tokens, password-reset links, and idempotency keys come from `crypto/rand`.
- Since Go 1.24 `crypto/rand.Read` never returns an error — it panics if the OS source fails. Old code shaped like `if _, err := rand.Read(b); err != nil { fallbackToMathRand() }` now has a dead branch that used to produce predictable tokens. Fiber's **CVE-2025-66630** is exactly this bug: a UUID helper returned the all-zero UUID when `crypto/rand` failed, and session and CSRF tokens were derived from it.
- Compare secrets with `subtle.ConstantTimeCompare(a, b) == 1`, not `==` or `bytes.Equal`. It still leaks length, so compare fixed-length values (hash the input first if it is variable-length).

### TLS

`tls.Config{InsecureSkipVerify: true}` disables certificate verification entirely; it is never correct outside a test. Go 1.25 stopped accepting SHA-1 signatures in TLS 1.2, and Go 1.26 enables hybrid post-quantum key exchange by default — both are reasons an old client that "worked before" fails after a toolchain upgrade. Fix the peer, do not set `InsecureSkipVerify`.

### Supply chain

Go has no npm-style install scripts, but the build still executes code: `#cgo` directives, SWIG, and `//go:generate` all run programs from dependency source. **CVE-2026-27140** was a `cmd/go` trust-layer bypass using cgo and SWIG (fixed 1.26.2 / 1.25.9).

| Control | What to do |
|---|---|
| `go.sum` | Commit it. `go mod verify` checks the module cache against it in CI |
| Checksum database | On by default via `GOSUMDB=sum.golang.org`. `GOSUMDB=off` turns it off globally; grep for it in CI config and Dockerfiles. `-mod=mod` is a different failure — it lets a build rewrite `go.mod`/`go.sum` instead of failing, so a new entry appears without review |
| `GOPRIVATE` / `GONOSUMDB` | These are what actually exempt a path from the checksum database. Scope them to exact private prefixes (`GOPRIVATE=git.corp.example.com/*`); a broad glob like `github.com/*` exempts every public dependency under it. `GOPRIVATE` supplies the default for `GONOPROXY` and `GONOSUMDB` |
| `GOINSECURE` | Permits fetching matching paths over plain HTTP / an unverified certificate. It does **not** disable checksum-database validation (`go help environment` says so explicitly) — so it is not the hole people assume, but it is still the wrong fix for a self-signed internal registry: fix the trust store |
| `-mod=readonly` | The default since Go 1.16. Keep it so a build fails on a missing requirement instead of silently rewriting `go.mod` |
| Toolchain | Pin with the `toolchain` line in `go.mod` plus `GOTOOLCHAIN` in CI, so the build cannot drift onto an unexpected compiler |
| Vulnerability scan | `govulncheck ./...` in CI. It uses call-graph reachability, so it reports far less noise than a plain dependency-version diff — but it misses paths reached only by reflection |
| Tool dependencies | Use the `tool` directive in `go.mod` (`go get -tool ...`, Go 1.24+) instead of the old `tools.go` blank-import file, so tools are version-pinned in the same lockfile |

## How this slots into the pipeline

- **Gate 0/5 (model choice):** state the concurrency model (goroutine-per-request, worker pool, channel pipeline, single-owner goroutine for shared state) and justify it. State the Go version floor and the `go` line in `go.mod` — they are not the same thing and footgun 11 depends on both. A design that passes shared state without synchronization is a runtime-level defect; flag it here, not during code review.
- **Gate 6 (implementation):** every goroutine has a context exit path; every shared resource has explicit ownership; every fan-out is bounded (`errgroup.SetLimit`); `http.Server` timeouts are set; `go test -race ./...` and `govulncheck ./...` run in CI.
- **Gate 7 (review):** the reliability-reviewer checks all 14 items above. Pay particular attention to goroutine leaks (footgun 1) and data races (footgun 3) — they are silent in development and catastrophic in production. The security-reviewer works the Security section; the timeout, pprof-on-DefaultServeMux, and `GOPRIVATE` scope checks are the three most commonly missed.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability) → **up** to `mir-backend`.
- A specific library's mechanics (Gin `c.Copy()` and `SetTrustedProxies`, Fiber pooled `Ctx` and `StructValidator`, Echo `Bind`+`Validate` and `IPExtractor`, GORM session scope) → **down** to the framework module (`mir-backend-go-<framework>`).
- **Here:** only what every Go backend shares because of the Go runtime, standard library, and toolchain — goroutine lifecycle, context propagation, the race detector, channel semantics, typed-nil, defer scoping, slice aliasing, error discipline, WaitGroup, language-version gating, synctest, GOMAXPROCS, slog, and the `net/http`/`crypto`/`os`/module-toolchain security mechanics.
- A different runtime (Python, Node, JVM…) → its own `mir-backend-<runtime>` tier. Never widen this one.
