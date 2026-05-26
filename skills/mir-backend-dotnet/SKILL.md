---
name: mir-backend-dotnet
description: "Make It Right (.NET runtime tier). CLR/CoreCLR runtime reliability footguns shared across EVERY .NET backend framework (ASP.NET Core, Minimal APIs, gRPC, Blazor Server, SignalR, background services) — distinct from the generic backend gates and from any one framework's mechanics. Covers: sync-over-async deadlock and thread-pool starvation (.Result/.Wait()/.GetAwaiter().GetResult()), ConfigureAwait(false) in library code, ValueTask misuse, thread-pool saturation under load, IDisposable/using discipline, DbContext thread-safety and lifetime, DI captive dependency (Scoped/Transient injected into Singleton), CancellationToken propagation, and large-object heap pressure. TRIGGER when the backend runtime is .NET / CLR — any C# or F# service. SKIP for Node/JVM/Go/Rust/Python/Ruby/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics (those live in mir-backend-dotnet-<framework>)."
trigger: /mir-backend-dotnet
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-dotnet · Make It Right (.NET runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-dotnet-aspnetcore`) knows the **library's mechanics**. This tier owns what's true for **all .NET backends because they run on the CLR** — the async model, thread pool, DI container, and object lifetime rules that ASP.NET Core, gRPC services, background workers, and SignalR hubs all inherit.

**Runtime assumed:** .NET 8+ (LTS). Notes apply to .NET 6/7 unless stated. Load order: `mir-backend` → `mir-backend-dotnet` → `<framework module>`.

## The CLR footguns AI walks into (framework-agnostic)

### 1. Sync-over-async — the classic deadlock and thread-pool starvation trap

Calling `.Result`, `.Wait()`, or `.GetAwaiter().GetResult()` on a `Task` or `ValueTask` from synchronous code is the #1 async bug on .NET. In runtimes that have a `SynchronizationContext` (old ASP.NET, WinForms, WPF, Blazor Server), the awaited continuation is posted back to the same context — but the calling thread is blocking that context, so you deadlock. In ASP.NET Core (no sync context) you dodge the deadlock but still **consume a thread-pool thread for the full duration of the I/O wait**, which starves the pool under concurrent load and produces the latency cliff that looks identical to "the server slows down after 30 concurrent users."

```csharp
// WRONG — blocks a thread-pool thread; deadlocks in sync-context runtimes
var result = GetDataAsync().Result;
var result = GetDataAsync().GetAwaiter().GetResult();

// RIGHT — async all the way
var result = await GetDataAsync();
```

Fix: **async all the way from the top.** Every caller must be async; there is no safe place to block. If you genuinely need a blocking entry point (console bootstrap, legacy sync API), use `Task.Run(() => AsyncWork()).GetAwaiter().GetResult()` only at the outermost boundary — never inside a library or middleware.

### 2. ConfigureAwait(false) in library code

In apps with a `SynchronizationContext` the continuation after `await` captures the current context and marshals back to it — necessary for UI/Blazor but adds overhead and causes deadlock risk when called by sync-over-async callers. **Library code must `ConfigureAwait(false)` on every `await`** to avoid capturing the caller's context:

```csharp
// Library method — WRONG (may deadlock callers with sync context)
public async Task<T> ReadAsync() {
    var data = await _store.GetAsync();   // captures context
    return Transform(data);
}

// RIGHT
public async Task<T> ReadAsync() {
    var data = await _store.GetAsync().ConfigureAwait(false);
    return Transform(data);
}
```

ASP.NET Core itself has no `SynchronizationContext`, so omitting `ConfigureAwait(false)` in an ASP.NET Core-only project is usually safe — but a library you ship may be consumed by Blazor Server or WPF; always add it in library/NuGet code. AI routinely omits it everywhere or adds it nowhere.

### 3. ValueTask — await at most once, never cache or await twice

`ValueTask` / `ValueTask<T>` exists for hot-path allocations savings on synchronous-fast paths. It has strict constraints the compiler does not enforce:

- **Await it at most once.** Awaiting a `ValueTask` twice (or calling `.Result` after awaiting) is undefined behavior and corrupts the underlying `IValueTaskSource` pool.
- **Don't store it and await later.** A `ValueTask` may already be recycled by the time you access it after doing other work.
- **Convert to `Task` before sharing:** `valueTask.AsTask()` if you need to await in multiple places.

```csharp
// WRONG — awaited twice
var vt = GetValueAsync();
var a = await vt;
var b = await vt;   // undefined behavior

// WRONG — cached, then awaited later
_cachedVt = GetValueAsync();
// ... later ...
var result = await _cachedVt;   // may be recycled

// RIGHT — await immediately, convert if you need to share
var result = await GetValueAsync();
// or
var task = GetValueAsync().AsTask();
```

Use `ValueTask` only when profiling proves the allocation matters (hot-loop network reads, high-frequency polling). Default to `Task`.

### 4. Thread-pool starvation — blocking calls under load

The CLR thread pool grows lazily (one new thread per ~500ms by default). Under concurrent load, blocking calls (sync I/O, `Thread.Sleep`, heavy CPU on pool threads, lock contention) exhaust available threads faster than the pool can grow. Symptoms: all endpoints slow simultaneously, `ThreadPool.GetAvailableThreads` drops to near zero, new requests queue behind the pool.

- Keep every I/O call async so threads return to the pool during the wait.
- CPU-bound work: `Task.Run(...)` to explicitly schedule on a pool thread and `await` it, or use dedicated `IHostedService` with bounded parallelism.
- Never call `Thread.Sleep` on a pool thread — `await Task.Delay(...)` instead.
- Monitor with `ThreadPool.GetAvailableThreads` / the `dotnet-counters` `ThreadPool Queue Length` metric.

### 5. IDisposable / using scopes — not optional

Every `IDisposable` (streams, `HttpClient` (if raw), `SqlConnection`, `DbContext`, `SemaphoreSlim`, `CancellationTokenSource`) must be disposed. Failure patterns:

- Forgetting `using` on a `DbContext` created manually → connection leak.
- Creating `HttpClient` instances per-request (common AI pattern) → socket exhaustion (TIME_WAIT). Use `IHttpClientFactory` or a long-lived shared client; never `new HttpClient()` per request.
- `CancellationTokenSource` not disposed → timer leak if a delay was scheduled.

```csharp
// WRONG — new HttpClient per request → socket exhaustion
async Task<string> CallApi() {
    using var client = new HttpClient();   // still wrong — see below
    return await client.GetStringAsync(url);
}

// RIGHT — factory or singleton
public class MyService(IHttpClientFactory factory) {
    async Task<string> CallApi() {
        using var client = factory.CreateClient("myApi");
        return await client.GetStringAsync(url);
    }
}
```

### 6. DbContext is NOT thread-safe — never share across threads or store in a singleton

`DbContext` (EF Core) tracks entity state in a non-thread-safe identity map. AI commonly produces two bugs:

**Bug A — singleton or static DbContext:** the context lives for the app lifetime, its change tracker grows unboundedly, and concurrent requests corrupt each other's entity state.

**Bug B — sharing across `Task.WhenAll` / `Parallel.ForEach`:** concurrent operations on one context produce non-deterministic exceptions (`InvalidOperationException: A second operation was started on this context`).

```csharp
// WRONG — DbContext injected into a singleton service
public class MySingletonService {
    private readonly AppDbContext _db;   // Scoped captured in Singleton = captive dependency
    public MySingletonService(AppDbContext db) => _db = db;
}

// RIGHT — use IDbContextFactory<T> in long-lived services
public class MySingletonService(IDbContextFactory<AppDbContext> factory) {
    public async Task DoWork() {
        await using var db = await factory.CreateDbContextAsync();
        // short-lived context, disposed after the unit of work
    }
}
```

### 7. DI lifetime — the captive dependency bug

The .NET DI container does not protect you from registering a **Scoped** (or Transient) service inside a **Singleton**. The Singleton is created once; it captures the Scoped dependency at construction time, which then lives for the entire app lifetime. For `DbContext` this means: one shared context across all requests = cross-request data bleed and threading corruption.

```csharp
// WRONG
services.AddSingleton<MyService>();   // captures Scoped DbContext in ctor
services.AddDbContext<AppDbContext>(); // Scoped by default

// RIGHT — validate on startup (throws at boot, not in production)
services.AddSingleton<MyService>();
services.AddDbContext<AppDbContext>();
// In Program.cs / appsettings or host builder:
builder.Host.UseDefaultServiceProvider(o => o.ValidateScopes = true);
// Or explicitly:
builder.Host.UseDefaultServiceProvider(o => {
    o.ValidateScopes = true;
    o.ValidateOnBuild = true;   // throws at build time if any captive dep detected
});
```

Enable `ValidateScopes = true` and `ValidateOnBuild = true` in development unconditionally. AI omits these; add them to every new service registration review.

### 8. CancellationToken — propagate through the full call chain

ASP.NET Core injects a `CancellationToken` tied to the HTTP request lifecycle (`HttpContext.RequestAborted`). If the client disconnects, the token is cancelled. Failing to propagate it means work continues needlessly, wasting resources:

```csharp
// WRONG — no token propagation; work continues after client disconnects
app.MapGet("/data", async () => {
    var result = await _db.Items.ToListAsync();
    return result;
});

// RIGHT
app.MapGet("/data", async (CancellationToken ct) => {
    var result = await _db.Items.ToListAsync(ct);
    return result;
});
```

Propagate `CancellationToken` to: EF Core queries (`.ToListAsync(ct)`, `.FirstOrDefaultAsync(ct)`), `HttpClient` calls (`.GetAsync(url, ct)`), any `Task.Delay`, and all downstream service calls. Never discard the token at an intermediate layer.

### 9. Nullable reference types and memory — don't ignore the compiler

- Enable `<Nullable>enable</Nullable>` in the project file. Treat warnings as errors in new code (`<WarningsAsErrors>Nullable</WarningsAsErrors>`). AI generates `!` (null-forgiving) suppressions as a shortcut — each one is a suppressed NullReferenceException.
- **Large Object Heap (LOH):** objects ≥ 85 KB are allocated on the LOH, which is not compacted by default. Repeated large-buffer allocations (e.g. `new byte[1_000_000]` per request) cause heap fragmentation and Gen 2 GC pressure. Use `ArrayPool<byte>.Shared.Rent(size)` and return it; or use `System.IO.Pipelines` for streaming.
- **Struct vs class:** prefer `struct` for small, frequently allocated value objects to reduce GC pressure, but avoid large structs copied by value on every method call (rule of thumb: ≤ 16 bytes, immutable, no ref fields).

## How this slots into the pipeline

- **Gate 0 (stack fitness):** confirm the workload is a good fit for .NET (enterprise web, Azure-integrated services). Flag if the task is a microsecond-HFT path or a GC-latency-sensitive hard-real-time system where the CLR's GC pauses are a genuine risk (consider .NET NativeAOT or Rust).
- **Gate 5 (design):** state the async model (async all the way), DI lifetime for each service, CancellationToken threading strategy, and whether `IDbContextFactory` or request-scoped `DbContext` is used.
- **Gate 6 (implementation):** code against the 9 footguns above. Every new async method: no `.Result`/`.Wait()`, token propagated, disposables wrapped. Every new DI registration: lifetime validated, no captive deps.
- **Gate 7 (review):** reliability-reviewer additionally checks items 1–9 here for any .NET service. `ValidateOnBuild = true` is the first thing to check — it catches captive deps at boot.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability, risk register) → **up** to `mir-backend`.
- A specific library's mechanics (ASP.NET Core middleware order, EF Core `Include`/projection, Minimal API model binding, antiforgery, `IOptions<T>`) → **down** to `mir-backend-dotnet-aspnetcore`.
- **Here:** only what every .NET backend shares because of the CLR — async/await model, thread pool, `ValueTask`, `IDisposable`, DI container lifetime rules, `DbContext` threading, `CancellationToken` propagation, LOH/GC.
- A different runtime (Node, Go, Python, JVM…) → its own `mir-backend-<runtime>` tier. Never widen this one.
