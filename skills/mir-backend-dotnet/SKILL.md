---
name: mir-backend-dotnet
description: "Make It Right (.NET runtime tier). CLR/CoreCLR runtime reliability footguns shared across EVERY .NET backend framework (ASP.NET Core, Minimal APIs, gRPC, Blazor Server, SignalR, Orleans, Worker Service) — distinct from the generic backend gates and from any one framework's mechanics. Covers: runtime version currency (.NET 10 is the current LTS; .NET 8 and .NET 9 both leave support 10 Nov 2026), sync-over-async deadlock and thread-pool starvation (.Result/.Wait()/.GetAwaiter().GetResult()), ConfigureAwait(false) and ConfigureAwaitOptions in library code, ValueTask misuse, thread-pool saturation under load, IDisposable/IAsyncDisposable discipline and HttpClient socket/DNS exhaustion, DbContext thread-safety and lifetime, DI captive dependency (Scoped/Transient injected into Singleton) and the fact that ValidateScopes only runs in Development, CancellationToken propagation, BackgroundService host-kill semantics, the DATAS server-GC default, large-object-heap pressure, trimming/Native AOT reflection breakage, and CLR-level security (BinaryFormatter removal, ProcessStartInfo.ArgumentList, Path.Combine traversal, SSRF via HttpClient redirect following, NuGet lockfile / package source mapping / NuGetAudit). TRIGGER when the backend runtime is .NET / CLR — any C# or F# service. SKIP for Node/JVM/Go/Rust/Python/Ruby/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and SKIP for ASP.NET Core and EF Core library mechanics — middleware order, model binding and overposting, antiforgery, EF Core queries, migrations, Options pattern all live in mir-backend-dotnet-aspnetcore, not here."
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

**Runtime assumed (verified 13 Aug 2026):** **.NET 10** — the current LTS, shipped Nov 2025, supported to Nov 2028. Latest patch is **10.0.11 (11 Aug 2026)**; the corresponding .NET 9 / .NET 8 patches are **9.0.19** and **8.0.30**.

**.NET 8 (LTS) and .NET 9 (STS) both reach end of support on 10 Nov 2026** — the same day, because STS moved to a 24-month window. After that date neither gets security fixes. A new service targeting `net8.0` or `net9.0` today is a migration item with under three months of runway, not a default; say so at Gate 0 rather than accepting the TFM silently. .NET 11 is the next STS (expected Nov 2026) and is in preview as of this writing — do not target it in production.

Load order: `mir-backend` → `mir-backend-dotnet` → `<framework module>`.

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

`ConfigureAwait(false)` is the same as `ConfigureAwait(ConfigureAwaitOptions.None)`. The enum overload adds two flags worth knowing:

| Option | Use it for |
|---|---|
| `ConfigureAwaitOptions.None` | identical to `ConfigureAwait(false)` — library default |
| `ConfigureAwaitOptions.ContinueOnCapturedContext` | identical to `ConfigureAwait(true)` |
| `ConfigureAwaitOptions.SuppressThrowing` | await a task for completion but do not rethrow its exception — replaces `try { await t; } catch { }`, which also swallows cancellation |
| `ConfigureAwaitOptions.ForceYielding` | force an async continuation even when the task already completed |

`SuppressThrowing` on a `Task<T>` throws `ArgumentOutOfRangeException` — it is only valid on non-generic `Task`, because there is no result to hand back.

### 3. ValueTask — await at most once, never cache or await twice

`ValueTask` / `ValueTask<T>` exists for hot-path allocation savings on synchronous-fast paths. Its constraints depend on the backing representation, which the caller usually cannot see, and the compiler enforces none of them:

- **Await it at most once.** A `ValueTask` wrapping a result or a `Task` tolerates repeated awaits, but an `IValueTaskSource`-backed one generally supports a single consumption — awaiting twice (or `.Result` after an await) then reads a recycled object. Since you cannot tell which you got, treat one await as the rule.
- **Don't store it and await later.** The source may have been recycled by the time you come back.
- **Convert to `Task` before sharing:** call `AsTask()` once and share the resulting `Task`.

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

The CLR thread pool grows using hill-climbing plus starvation detection, not a fixed injection interval — the old "one thread per 500 ms" figure is obsolete, but the shape of the failure is unchanged: blocking calls (sync I/O, `Thread.Sleep`, heavy CPU on pool threads, lock contention) consume workers faster than the pool reacts. Symptoms: all endpoints slow simultaneously, `ThreadPool.GetAvailableThreads` drops to near zero, and the queue climbs while CPU sits idle.

- Keep every I/O call async so threads return to the pool during the wait.
- CPU-bound work: `Task.Run(...)` to explicitly schedule on a pool thread and `await` it, or use a dedicated `BackgroundService` with bounded parallelism.
- Never call `Thread.Sleep` on a pool thread — `await Task.Delay(...)` instead.
- `SemaphoreSlim.Wait()` blocks; `await SemaphoreSlim.WaitAsync(ct)` does not. Same for `lock` around an `await` — you cannot `await` inside `lock`, and AI works around it with `.Result`. Use `SemaphoreSlim(1,1)` instead.
- Monitor with `dotnet-counters monitor --counters System.Runtime` and watch `ThreadPool Queue Length` and `ThreadPool Thread Count`. A queue length that climbs while CPU is idle means blocked threads, not slow code.
- `ThreadPool.SetMinThreads(n, n)` buys time by pre-growing the pool. It is a mitigation for a blocking call you have not removed yet, not a fix.

### 5. IDisposable / using scopes — not optional

Every `IDisposable` (streams, `HttpClient` (if raw), `SqlConnection`, `DbContext`, `SemaphoreSlim`, `CancellationTokenSource`) must be disposed. Failure patterns:

- Forgetting `using` on a `DbContext` created manually → connection leak.
- Creating `HttpClient` instances per-request (common AI pattern) → socket exhaustion (TIME_WAIT). Use `IHttpClientFactory` or a long-lived shared client; never `new HttpClient()` per request.
- `CancellationTokenSource` not disposed → timer leak if a delay was scheduled.
- Using `using` on a type that implements `IAsyncDisposable` (`DbContext`, `SqlConnection`, `FileStream` opened async, `ServiceProvider`) runs the synchronous disposal path or none at all. Write `await using`.

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

**The opposite failure — one static `HttpClient` forever:** DNS is resolved when a *new connection* is opened, so a client that keeps reusing a pooled connection indefinitely never re-resolves and keeps talking to a decommissioned IP after a failover or blue/green cutover. `IHttpClientFactory` rotates handlers (default handler lifetime 2 minutes) and avoids this. If you must hold your own client, bound the connection lifetime explicitly:

```csharp
var handler = new SocketsHttpHandler {
    PooledConnectionLifetime = TimeSpan.FromMinutes(2)   // forces periodic DNS re-resolution
};
var client = new HttpClient(handler);   // safe to keep for the process lifetime
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

The .NET DI container does not stop a **Singleton** from capturing a **Scoped** service. The Singleton is created once and holds that dependency for the whole app lifetime. For `DbContext` that means one shared context across all requests: cross-request data bleed and threading corruption. Capturing a **Transient** is legal, but the instance then lives as long as the Singleton — a problem when it holds request state or owns something disposable.

```csharp
// WRONG
services.AddSingleton<MyService>();   // captures Scoped DbContext in ctor
services.AddDbContext<AppDbContext>(); // Scoped by default

// RIGHT — validate at build time, in EVERY environment
builder.Host.UseDefaultServiceProvider((_, o) => {
    o.ValidateScopes  = true;   // Scoped resolved from root/Singleton throws — this is the captive-dep check
    o.ValidateOnBuild = true;   // walks registrations at Build() and throws if one cannot be constructed
});
```

**The trap that makes this bite in production specifically:** the default host turns `ValidateScopes` and `ValidateOnBuild` on **only when the environment is Development**. In Staging or Production they are off. So the captive dependency does not throw — it constructs, and one `DbContext` quietly serves every request. Pass the options unconditionally as above (the two-argument overload gives you the `HostBuilderContext` if you need to branch, but do not branch on environment here). `ValidateOnBuild` costs a few milliseconds at boot; it is not a perf trade worth making.

The two do different jobs, and neither is a complete captive-dependency analyzer: `ValidateScopes` is the one that catches Scoped-in-Singleton, `ValidateOnBuild` only proves constructibility. Neither resolves open generics or factory registrations (`AddSingleton<T>(sp => ...)`), so a captive dependency hidden inside a lambda still slips through. Read new `AddSingleton` registrations by hand.

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

**The exception — do not cancel a commit.** `RequestAborted` fires the moment the browser tab closes. If you thread it into the write path, a client disconnect can abort `SaveChangesAsync` or the payment call *after* the external side effect happened, leaving state split between systems. Read paths take `RequestAborted`; the commit path takes either `CancellationToken.None` or its own timeout token:

```csharp
// Read: honour the client going away
var items = await db.Items.AsNoTracking().ToListAsync(ct);

// Write: run to completion on its own deadline, not the client's
using var commitCts = new CancellationTokenSource(TimeSpan.FromSeconds(30));
await db.SaveChangesAsync(commitCts.Token);
```

### 9. Nullable reference types and memory — don't ignore the compiler

- Enable `<Nullable>enable</Nullable>` in the project file. Treat warnings as errors in new code (`<WarningsAsErrors>Nullable</WarningsAsErrors>`). AI generates `!` (null-forgiving) suppressions as a shortcut — each one is a suppressed NullReferenceException.
- **Large Object Heap (LOH):** objects ≥ 85 KB are allocated on the LOH, which is not compacted by default. Repeated large-buffer allocations (e.g. `new byte[1_000_000]` per request) cause heap fragmentation and Gen 2 GC pressure. Use `ArrayPool<byte>.Shared.Rent(size)` and return it; or use `System.IO.Pipelines` for streaming.
- **Struct vs class:** prefer `struct` for small, frequently allocated value objects to reduce GC pressure, but avoid large structs copied by value on every method call (rule of thumb: ≤ 16 bytes, immutable, no ref fields).

### 10. DATAS changed Server GC behaviour — an upgrade to .NET 10 is a perf change, not just a version bump

DATAS (Dynamic Adaptation To Application Sizes) was opt-in in .NET 8 and became **enabled by default for Server GC in .NET 9**. Most teams meet it for the first time jumping .NET 8 → .NET 10, because .NET 9 was skipped as an STS release. It trades a few percent of throughput for a large working-set reduction. Two behaviours to expect after the upgrade:

- **Higher Gen0/Gen1 GC counts** and drastically smaller working set. That is DATAS working, not a leak.
- **Slower startup ramp.** DATAS begins at one heap and grows, so a cold instance is slower than .NET 8's Server GC was. This matters for scale-to-zero and per-request-billed hosting.

If you measure a regression against a real SLO, tune before you disable — `GCDTargetTCP` (target throughput cost percentage) first. To turn it off: `<GarbageCollectionAdaptationMode>0</GarbageCollectionAdaptationMode>` in the `.csproj`, `{ "configProperties": { "System.GC.DynamicAdaptationMode": 0 } }` in `runtimeconfig.json`, or `DOTNET_GCDynamicAdaptationMode=0`. Setting `GCHeapCount` also disables it, because pinning the heap count removes the mechanism DATAS uses. Benchmark both ways against your own p99 — do not accept either default on faith.

### 11. BackgroundService — an unhandled exception now stops the whole host

`HostOptions.BackgroundServiceExceptionBehavior` defaults to `StopHost`. An unhandled exception in `ExecuteAsync` therefore takes down the entire application, not just that worker. AI writes `ExecuteAsync` bodies with no `try`/`catch` around the work loop, so one transient database blip on a nightly job kills the web server sharing the process.

```csharp
// WRONG — one throw ends the process
protected override async Task ExecuteAsync(CancellationToken stoppingToken) {
    while (!stoppingToken.IsCancellationRequested) {
        await DoWorkAsync(stoppingToken);
        await Task.Delay(TimeSpan.FromMinutes(1), stoppingToken);
    }
}

// RIGHT — catch per iteration, let cancellation through, log and continue
protected override async Task ExecuteAsync(CancellationToken stoppingToken) {
    while (!stoppingToken.IsCancellationRequested) {
        try { await DoWorkAsync(stoppingToken); }
        catch (OperationCanceledException) when (stoppingToken.IsCancellationRequested) { break; }
        catch (Exception ex) { _logger.LogError(ex, "Work iteration failed; continuing"); }
        try { await Task.Delay(TimeSpan.FromMinutes(1), stoppingToken); }
        catch (OperationCanceledException) { break; }
    }
}
```

Two more lifecycle rules:

- **.NET 10 runs all of `ExecuteAsync` on a background thread.** Before .NET 10 the synchronous portion (up to the first *incomplete* await) ran on the startup path and blocked other services from starting, and the workaround was `await Task.Yield();` first. That is now stale — drop the `Task.Yield()`. The inverse is the new trap: if work genuinely **must** finish before startup proceeds, `ExecuteAsync` will no longer do it. Put it in the constructor, override `StartAsync` and work before `base.StartAsync`, or implement `IHostedLifecycleService`.
- **`stoppingToken` is the shutdown signal, not a cancellation of pending work.** Shutdown waits only `HostOptions.ShutdownTimeout` (default 30 seconds); anything still running is abandoned. Long units of work need their own checkpoint/resume, not a longer timeout.

### 12. Trimming and Native AOT break reflection — silently, unless you make warnings errors

`PublishTrimmed` and `PublishAot` remove code the static analyzer cannot see is reachable. Reflection-based patterns — `Activator.CreateInstance(Type.GetType(name))`, `MakeGenericType`, `Assembly.Load`, reflection-based JSON serialization, DI registration by assembly scanning, expression-tree compilation — fail at runtime with `MissingMethodException`, `TypeLoadException`, or an empty serialized object. The compiler *does* warn (IL2xxx for trimming, IL3xxx for AOT); AI ignores them.

```xml
<PropertyGroup>
  <PublishAot>true</PublishAot>
  <TreatWarningsAsErrors>true</TreatWarningsAsErrors>
  <!-- Do NOT suppress IL2026/IL2075/IL3050 to make the build pass -->
</PropertyGroup>
```

Rules that make AOT survivable: use the `System.Text.Json` source generator (`JsonSerializerContext`) rather than reflection-based `JsonSerializer.Serialize<T>`; register DI explicitly instead of scanning assemblies; avoid `dynamic` entirely (it needs the runtime binder). **A zero-warning publish is the contract** — Microsoft's guidance is that an app publishing without AOT warnings behaves the same as the JIT build, and an app that emits them may not work. Which ASP.NET Core features are AOT-compatible at all is a framework question — see `mir-backend-dotnet-aspnetcore`.

## How this slots into the pipeline

- **Gate 0 (stack fitness):** confirm the workload is a good fit for .NET (enterprise web, Azure-integrated services). Flag if the task is a microsecond-latency path or a GC-pause-sensitive hard-real-time system (consider Native AOT or Rust). Also check the target framework moniker here: `net8.0`/`net9.0` are out of support on 10 Nov 2026, so a new service on either starts with a scheduled migration.
- **Gate 5 (design):** state the async model (async all the way), DI lifetime for each service, CancellationToken threading strategy (including which calls deliberately do *not* take `RequestAborted`), whether `IDbContextFactory` or request-scoped `DbContext` is used, and whether the deployment target requires Native AOT.
- **Gate 6 (implementation):** code against the 12 footguns above. Every new async method: no `.Result`/`.Wait()`, token propagated, disposables wrapped (`await using` for `IAsyncDisposable`). Every new DI registration: lifetime validated, no captive deps.
- **Gate 7 (review):** reliability-reviewer additionally checks items 1–12 here for any .NET service. `ValidateOnBuild = true` set unconditionally is the first thing to check — it catches captive deps at boot instead of in production. security-reviewer runs the Security list below.

## Security

CLR-level security footguns. Framework-level ones (middleware order, antiforgery, CORS, IDOR, EF Core injection, file serving) live in `mir-backend-dotnet-aspnetcore`.

### Patch floor and advisory currency

.NET security fixes ship on Patch Tuesday and are cumulative. **Verified floors as of 13 Aug 2026: 10.0.11 · 9.0.19 · 8.0.30** (11 Aug 2026 release). Recent named advisories on the default path:

| CVE | What | Affected | Fixed in |
|---|---|---|---|
| CVE-2025-55315 | Kestrel HTTP request smuggling — auth bypass, SSRF, CSRF bypass. CVSS 9.9, the highest ever assigned to ASP.NET Core | ASP.NET Core ≤ 8.0.20, ≤ 9.0.9, ≤ 6.0.36 (EOL, unfixed), `Microsoft.AspNetCore.Server.Kestrel.Core` ≤ 2.3.0 | 8.0.21, 9.0.10, Kestrel.Core 2.3.6 |
| CVE-2026-40372 | `Microsoft.AspNetCore.DataProtection` padding oracle — forged auth cookies, decryption of protected payloads. CVSS 9.1, **non-Windows only** | 10.0.0 – 10.0.6 | 10.0.7 (**and rotate the key ring** — tokens forged before patching stay valid) |
| CVE-2026-47303 | `Microsoft.AspNetCore.Authentication.Negotiate` parsing — elevation of privilege, LDAP injection. CVSS 8.8 | ≤ 8.0.28, ≤ 9.0.17, ≤ 10.0.9 | 8.0.29, 9.0.18, 10.0.10 |
| CVE-2026-45591 | SignalR/Blazor Server MessagePack hub protocol — stack overflow from deeply nested arrays (DoS) | 8.0, 9.0, 10.0 | June 2026 patch |
| CVE-2026-32175 | .NET Core tampering — crafted files let an attacker write arbitrary files/directories | 8.0, 9.0, 10.0 | May 2026 patch |

**Patching the machine is not enough.** Self-contained and single-file publishes bundle the runtime, so they must be *rebuilt and redeployed*. Container images must be rebased. A host that is fully current on OS updates can still run a vulnerable runtime. Track `github.com/dotnet/announcements` — every advisory lands there as an issue.

### Deserialization of untrusted input

- **`BinaryFormatter` is removed.** Since .NET 9 the in-box implementation always throws, regardless of project type, and the old opt-in compatibility switches were deleted. If you hit this, **do not** add the `System.Runtime.Serialization.Formatters` NuGet package — it restores the unsafe implementation and Microsoft ships it explicitly unsupported. Migrate to `System.Text.Json`, protobuf, or MessagePack with a fixed contract.
- **`Newtonsoft.Json` with `TypeNameHandling` set to anything but `None` is remote code execution.** The `$type` field in attacker-controlled JSON selects the type to construct, and public gadget chains exist. If you need polymorphism, use `System.Text.Json` `[JsonDerivedType]` with an explicit discriminator allow-list, or a `SerializationBinder` that allow-lists types by name.
- `System.Text.Json` is safe by default. You reopen the hole with a custom `IJsonTypeInfoResolver` that resolves types from the payload, or with `[JsonExtensionData]`, which collects every unmapped member into a dictionary and hands mass assignment straight back.

### Command injection

`ProcessStartInfo.Arguments` is a single command-line string whose quoting and argument boundaries are platform-dependent; `ArgumentList` is a list escaped per element. Note what the risk actually is: with `UseShellExecute = false` (the .NET default) the executable is started directly, so `&` and `|` do **not** spawn anything — the bug is that a filename containing spaces or quotes silently becomes two arguments, or an argument the callee interprets as a flag. Metacharacter injection only happens if you explicitly launch `cmd /c`, `powershell -Command`, or `sh -c`; never put untrusted text in one of those.

```csharp
// WRONG — argument boundaries decided by whitespace in attacker-controlled input
Process.Start(new ProcessStartInfo("convert", $"{userFile} out.png"));

// RIGHT
var psi = new ProcessStartInfo("convert") { UseShellExecute = false };
psi.ArgumentList.Add(userFile);      // escaped as one argument
psi.ArgumentList.Add("out.png");
Process.Start(psi);
```

`UseShellExecute = true` hands the string to the OS shell/handler (the graphical shell — `ShellExecute` on Windows, `open`/`xdg-open` elsewhere) and makes any escaping you did irrelevant, including turning a `.url`/`.desktop`/document path into a launch. Keep it `false` for anything touching user input. `Process.Start(string)` with a user-supplied value is the same bug in shorter form.

### Path traversal

`Path.Combine` **discards everything before a rooted segment**: `Path.Combine("/srv/uploads", "/etc/passwd")` returns `/etc/passwd`. `..` segments do the rest. Canonicalize, then compare against the root:

```csharp
var root = Path.GetFullPath("/srv/uploads") + Path.DirectorySeparatorChar;
var full = Path.GetFullPath(Path.Combine(root, userPath));
if (!full.StartsWith(root, StringComparison.Ordinal)) throw new UnauthorizedAccessException();
```

Never derive a filesystem path from a client-supplied name — store a generated identifier and keep the original name as metadata. On archive extraction, apply the same check to every entry's `FullName` before writing (zip slip); do not assume the extraction API validates for you.

### SSRF from user-supplied URLs

Any `HttpClient` call whose URL comes from a request body, webhook registration, or "import from URL" field is an SSRF primitive. Two things AI gets wrong:

- **Validating the URL then following redirects.** `SocketsHttpHandler.AllowAutoRedirect` defaults to `true`, so an allow-listed host can 302 to `http://169.254.169.254/`. Set `AllowAutoRedirect = false` and re-validate every hop yourself.
- **Checking the hostname instead of the resolved address.** DNS can return a private address (rebinding). Resolve with `Dns.GetHostAddressesAsync`, reject loopback, link-local (`169.254.0.0/16`, `fe80::/10`), private RFC1918 ranges, and IPv6 unique-local — then connect to the resolved address.

Cloud metadata endpoints are the highest-value target: `169.254.169.254` (AWS/Azure/GCP) and `[fd00:ec2::254]` (AWS IPv6). Block the range at the egress layer as well; do not rely on the app check alone.

### Transport and cryptography

| Do not write | Write instead |
|---|---|
| `ServerCertificateCustomValidationCallback = (_,_,_,_) => true` (or `DangerousAcceptAnyServerCertificateValidator`) | fix the trust store / pin with a real callback; never return `true` unconditionally |
| `Random`, `Random.Shared`, or `Guid.NewGuid()` for tokens, password resets, session ids | `RandomNumberGenerator.GetBytes(32)`, `RandomNumberGenerator.GetHexString(n)`, `RandomNumberGenerator.GetItems(...)` |
| `token == expected` on a secret | `CryptographicOperations.FixedTimeEquals(a, b)` — string `==` leaks length and prefix by timing |
| `MD5`/`SHA1`, or a bare `SHA256(password)` | `Rfc2898DeriveBytes` (PBKDF2-SHA256, high iteration count) or ASP.NET Core Identity's `PasswordHasher<TUser>` |
| Hard-coded IV or key material in source | `RandomNumberGenerator` per message; keys from a key vault, never from `appsettings.json` in the repo |

### Secret and PII leakage

- **Exception text carries secrets.** `SqlException`/`HttpRequestException` messages and `ex.ToString()` regularly contain connection strings, hostnames, and bearer tokens. Log the exception, but never return `ex.ToString()` in a response body.
- **Structured logging with `{@obj}` serializes the whole object** — `_logger.LogInformation("User {@User}", user)` writes the password hash, email, and every other property to the log sink. Log named scalars, or a projection type built for logging.
- **`dotnet user-secrets` is plaintext JSON in the user profile** and is only read in the Development environment. It is a convenience for local dev, not a secret store. Production secrets come from the platform (Key Vault, Secrets Manager, mounted files).
- **Crash dumps and `DOTNET_`/`ASPNETCORE_` environment variables** carry configuration verbatim. Restrict who can pull dumps from prod hosts.

### Supply chain (NuGet specifics)

| Control | Exactly what to set |
|---|---|
| Lock the graph | `<RestorePackagesWithLockFile>true</RestorePackagesWithLockFile>`, commit `packages.lock.json`, and build with `dotnet restore --locked-mode` |
| Stop dependency confusion | `<packageSourceMapping>` in `nuget.config` — internal prefixes to the private feed, `*` to nuget.org. Pass `--configfile ./nuget.config` in CI; pipeline tasks inject extra sources otherwise |
| Know you are vulnerable | `NuGetAuditMode` defaults to `all` **only when the project targets `net10.0`**; projects targeting `net9.0` or earlier still default to `direct` and miss transitive advisories. Set it explicitly. (The .NET 9 SDK briefly defaulted to `all` in 9.0.100 and reverted in 9.0.101 — do not rely on SDK version.) |
| Check on demand | `dotnet list package --vulnerable --include-transitive`; `dotnet nuget why <pkg>` to find the root package pulling a bad transitive in |

**A NuGet package runs code at build time.** `.props`/`.targets` files inside a package are imported into your build and execute as MSBuild. Restoring an untrusted package on a build agent is equivalent to running its code with the agent's credentials. Pin sources and review new dependencies before they reach CI.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability, risk register) → **up** to `mir-backend`.
- A specific library's mechanics (ASP.NET Core middleware order, EF Core `Include`/projection, Minimal API model binding and validation, antiforgery, CORS policy, `IOptions<T>`, migrations) → **down** to `mir-backend-dotnet-aspnetcore`.
- **Here:** only what every .NET backend shares because of the CLR — async/await model, thread pool, `ValueTask`, `IDisposable`/`IAsyncDisposable`, DI container lifetime rules, `DbContext` threading, `CancellationToken` propagation, GC/DATAS/LOH, host lifecycle, trimming and AOT, and CLR-level security (serializers, `Process`, `Path`, `HttpClient`, cryptography, NuGet).
- A different runtime (Node, Go, Python, JVM…) → its own `mir-backend-<runtime>` tier. Never widen this one.
