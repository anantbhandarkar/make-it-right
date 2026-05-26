---
name: mir-backend-dotnet-aspnetcore
description: "Make It Right (ASP.NET Core module). ASP.NET Core + Minimal APIs + EF Core + Entity Framework migrations specific reliability augmentation. Use alongside mir-backend and mir-backend-dotnet when the target stack is ASP.NET Core or Minimal APIs — it carries the mechanical footguns the runtime-agnostic tiers deliberately omit: DI lifetime errors in practice (AddDbContext Scoped vs singleton capture, IHttpContextAccessor caveats), middleware pipeline ORDER (UseRouting → UseAuthentication → UseAuthorization → endpoints; wrong order silently disables auth), model binding overposting / mass assignment onto EF entities, response DTO discipline to prevent field leakage, EF Core N+1 (lazy loading raises in async contexts, use Include/projection/AsNoTracking), async-all-the-way in endpoints with CancellationToken from the request, object-level authorization (IDOR via valid token without resource check), antiforgery for cookie auth, and the Options pattern for config. TRIGGER only when the .NET backend stack uses ASP.NET Core or Minimal APIs — building, reviewing, or debugging a controller, endpoint, middleware, EF Core query, or migration. Always loads TOGETHER WITH mir-backend (gates) and mir-backend-dotnet (CLR runtime concerns: sync-over-async, thread-pool starvation, ValueTask, IDisposable, DI captive dependency, CancellationToken); this module adds only ASP.NET Core / EF Core library mechanics. SKIP for other frameworks or runtimes (WCF, Nancy, non-.NET stacks)."
trigger: /mir-backend-dotnet-aspnetcore
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-dotnet-aspnetcore · Make It Right (ASP.NET Core)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-dotnet` (CLR runtime model) → **this** (ASP.NET Core / EF Core / Minimal API library mechanics). Run the gates first; load the .NET runtime tier for async model, DI lifetime theory, and thread-pool concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (sync-over-async deadlock, ConfigureAwait(false), ValueTask, IDisposable, CancellationToken propagation, captive dependency theory) live in `mir-backend-dotnet` — not here.**

**Stack assumed:** ASP.NET Core 8+ (controllers or Minimal APIs) · EF Core 8 · SQL Server / PostgreSQL · `Microsoft.EntityFrameworkCore.Design` migrations. If the project uses Dapper or a different ORM, note the divergence before applying EF-specific guidance.

## The ASP.NET Core footguns AI walks into most

### 1. DI lifetimes in practice — AddDbContext is Scoped; singletons must not capture it

`AddDbContext<T>()` registers `DbContext` as **Scoped** (one instance per HTTP request). The captive dependency problem (runtime tier) surfaces concretely here:

```csharp
// WRONG — MyWorker is Singleton; captures a Scoped DbContext at construction
services.AddSingleton<MyWorker>();
services.AddDbContext<AppDbContext>();

public class MyWorker(AppDbContext db) { ... }   // db lives forever = cross-request bleed
```

```csharp
// RIGHT — use IDbContextFactory<T> in long-lived services
services.AddSingleton<MyWorker>();
services.AddDbContextFactory<AppDbContext>();

public class MyWorker(IDbContextFactory<AppDbContext> factory) {
    public async Task RunAsync(CancellationToken ct) {
        await using var db = await factory.CreateDbContextAsync(ct);
        // short-lived, disposed after unit of work
    }
}
```

**`IHttpContextAccessor` caveat:** it's Singleton but accesses Scoped `HttpContext`. Safe in middleware/request-scoped code, but using it inside a background service or event handler (outside an active request) returns `null`. Don't use it as a general-purpose context bag outside the request pipeline.

### 2. Middleware ORDER is the pipeline contract

ASP.NET Core middleware runs in registration order. Auth especially is order-sensitive — **getting it wrong silently disables security with no error**:

```csharp
// WRONG — UseAuthorization before UseAuthentication; auth policies never fire correctly
app.UseRouting();
app.UseAuthorization();       // wrong position
app.UseAuthentication();
app.MapControllers();

// RIGHT — canonical order
app.UseRouting();
app.UseAuthentication();      // 1. identify the caller
app.UseAuthorization();       // 2. decide if they're allowed
app.MapControllers();         // 3. dispatch
```

Other ordering traps:
- `UseCors()` must come **before** `UseAuthentication`/`UseAuthorization` and `MapControllers` to emit CORS headers on preflight.
- `UseStaticFiles()` before `UseRouting()` short-circuits static asset requests efficiently — fine, but don't put auth middleware before it if static files should be public.
- `UseExceptionHandler()` / `UseHsts()` / `UseHttpsRedirection()` belong at the very top (before any business middleware) so they catch all exceptions and redirect correctly.
- Custom middleware that reads `HttpContext.User` must come after `UseAuthentication`.

### 3. Model binding overposting / mass assignment — never bind straight to EF entities

Binding a request body directly to an EF entity lets a client set any property the model exposes, including `IsAdmin`, `Role`, `TenantId`, `OwnerId`, or audit timestamps:

```csharp
// WRONG — client can POST { "name": "Alice", "isAdmin": true }
app.MapPost("/users", async (User user, AppDbContext db) => {
    db.Users.Add(user);
    await db.SaveChangesAsync();
    return Results.Created($"/users/{user.Id}", user);   // also leaks entity
});
```

```csharp
// RIGHT — separate input and output DTOs
app.MapPost("/users", async (CreateUserRequest req, AppDbContext db, CancellationToken ct) => {
    var user = new User { Name = req.Name, Email = req.Email };   // only allowed fields
    db.Users.Add(user);
    await db.SaveChangesAsync(ct);
    return Results.Created($"/users/{user.Id}", new UserResponse(user.Id, user.Name));
});

public record CreateUserRequest(string Name, string Email);   // no Id, IsAdmin, TenantId
public record UserResponse(Guid Id, string Name);             // no PasswordHash, internal flags
```

For controllers using `[ApiController]`, mark the input DTO class with `[Bind("Name,Email")]` allowlist, or simply use separate request/response records. **Never return the EF entity directly** — use a response DTO to avoid leaking password hashes, internal flags, or navigation-loaded child objects.

### 4. EF Core N+1 — lazy loading raises in async, doesn't just degrade performance

Lazy loading (proxy-based) silently issues one query per navigation property access. In async EF Core, accessing an unloaded navigation property outside a live `DbContext` throws `InvalidOperationException` (detached instance). Both forms are defects:

```csharp
// WRONG — N+1: one query for orders, then one per order for customer name
var orders = await db.Orders.ToListAsync(ct);
foreach (var o in orders) {
    Console.WriteLine(o.Customer.Name);   // lazy load or DetachedInstanceError
}

// RIGHT — eager load with Include
var orders = await db.Orders
    .Include(o => o.Customer)
    .ToListAsync(ct);

// BETTER for read-only endpoints — project to DTO, no tracking overhead
var dtos = await db.Orders
    .Select(o => new OrderDto(o.Id, o.Customer.Name, o.Total))
    .AsNoTracking()
    .ToListAsync(ct);
```

Rules:
- **`Include()`** for full entity graphs you need to mutate.
- **`Select()` to DTO** for read-only endpoints — eliminates N+1 at the query level and avoids loading unnecessary columns.
- **`AsNoTracking()`** on read-only queries to skip the identity-map overhead (significant under load).
- Avoid `ToList()` + LINQ-to-objects filtering — this pulls the entire table into memory. Filter in the query (`Where`, `Take`, `Skip`).

### 5. Async all the way in endpoints — CancellationToken from the request

ASP.NET Core Minimal APIs auto-bind a `CancellationToken` parameter to the request-abort token. Controllers expose it via `HttpContext.RequestAborted`. Failing to pass it through means work continues after the client disconnects:

```csharp
// WRONG — no cancellation; holds DB connection after client disconnects
app.MapGet("/items", async (AppDbContext db) =>
    await db.Items.ToListAsync());

// RIGHT
app.MapGet("/items", async (AppDbContext db, CancellationToken ct) =>
    await db.Items.AsNoTracking().ToListAsync(ct));
```

Pass `ct` to every EF Core method (`.ToListAsync(ct)`, `.SaveChangesAsync(ct)`), every `HttpClient` call, and every downstream service call.

### 6. Object-level authorization — a valid token is not authorization to a specific resource

AI implements route-level `[Authorize]` or `RequireAuthorization()` and considers the job done. That guards the endpoint but does not check whether **this user owns this specific record** — any authenticated user can read/mutate any resource (IDOR):

```csharp
// WRONG — authenticated user can read any order by id
app.MapGet("/orders/{id}", async (Guid id, AppDbContext db, ClaimsPrincipal user, CancellationToken ct) => {
    var order = await db.Orders.FindAsync([id], ct);
    return order is null ? Results.NotFound() : Results.Ok(order);
}).RequireAuthorization();

// RIGHT — add the ownership check in the query
app.MapGet("/orders/{id}", async (Guid id, AppDbContext db, ClaimsPrincipal user, CancellationToken ct) => {
    var userId = Guid.Parse(user.FindFirstValue(ClaimTypes.NameIdentifier)!);
    var order = await db.Orders
        .AsNoTracking()
        .Where(o => o.Id == id && o.OwnerId == userId)   // ownership enforced at DB level
        .Select(o => new OrderDto(o.Id, o.Total))
        .FirstOrDefaultAsync(ct);
    return order is null ? Results.NotFound() : Results.Ok(order);
}).RequireAuthorization();
```

For complex policies, use ASP.NET Core **resource-based authorization handlers** (`IAuthorizationHandler<TRequirement, TResource>`) — they receive the loaded resource and apply policy cleanly. Don't embed ownership logic in every endpoint; put it in a reusable handler.

### 7. Antiforgery for cookie auth — required when mixing cookie + JSON APIs

If your app authenticates via cookies (not bearer tokens), `POST`/`PUT`/`DELETE` endpoints are vulnerable to CSRF. ASP.NET Core 8 has built-in antiforgery middleware:

```csharp
// Register
builder.Services.AddAntiforgery();

// Validate on mutating endpoints (Minimal APIs)
app.MapPost("/orders", async (...) => { ... })
   .WithAntiforgery();

// Or globally for controllers
app.UseAntiforgery();
```

Bearer-token-only APIs (no cookies) do not need antiforgery — CSRF requires a browser to automatically attach credentials. Mixed apps (cookie auth for the web UI, bearer for the API) must apply antiforgery only to the cookie-authenticated surface.

### 8. Options pattern for config — never read IConfiguration inline

Reading `IConfiguration["key"]` directly in services makes configuration hard to validate, mock, and refactor. AI routinely injects `IConfiguration` everywhere:

```csharp
// WRONG — raw IConfiguration in a service
public class PaymentService(IConfiguration config) {
    private readonly string _key = config["Stripe:SecretKey"]!;   // null at runtime if key missing
}

// RIGHT — typed Options + validation at startup
public record StripeOptions {
    public required string SecretKey { get; init; }
    public required string WebhookSecret { get; init; }
}

// Registration with eager validation
services.AddOptions<StripeOptions>()
    .BindConfiguration("Stripe")
    .ValidateDataAnnotations()
    .ValidateOnStart();          // throws at boot if missing/invalid, not at first use

public class PaymentService(IOptions<StripeOptions> opts) {
    private readonly StripeOptions _cfg = opts.Value;
}
```

Use `IOptionsSnapshot<T>` (Scoped, reloads per request with `reloadOnChange`) when you need live config updates. Use `IOptionsMonitor<T>` (Singleton-safe, fires `OnChange`) in long-lived services or hosted services.

### 9. EF Core migrations — AI writes them as if the table is empty

Running `ALTER TABLE` / dropping a column / adding a non-nullable column without a default on a live populated table causes downtime or data loss:

- **Adding a NOT NULL column:** always add as nullable first (`ALTER TABLE ... ADD col TYPE NULL`), backfill in a separate step, then add the constraint — or supply a default value.
- **Dropping a column:** remove from code first (deploy), then drop in a subsequent migration — avoids errors during rolling deploy where old pods still reference the column.
- **Renaming a column or table:** EF Core generates `DROP` + `ADD` unless you add explicit `.RenameColumn()` in the migration — silently destroys data.
- **Adding an index on a large table:** use `migrationBuilder.Sql("CREATE INDEX CONCURRENTLY ...")` (Postgres) instead of the default `CREATE INDEX` which locks the table.

Always review the generated migration SQL (`dotnet ef migrations script`) before applying to production, especially on any schema change that touches existing rows.

## How this slots into the core pipeline

- **Gate 5 (Design):** state the middleware order, DI lifetimes (especially DbContext scope vs factory), DTO boundaries, auth strategy (route-level + resource-level), and antiforgery applicability.
- **Gate 6 (Implementation):** code against footguns 1–9 above. Every new endpoint: DTO in/out, ct propagated, ownership check present. Every new EF query: Include or Select, AsNoTracking on reads.
- **Gate 7 (Review):** reliability-reviewer checks items 1–9; additionally runs `ValidateOnBuild = true` at startup for DI lifetime violations. Migration-reviewer reads the generated SQL before approval.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY ASP.NET Core / Minimal APIs / EF Core mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability)? → **generic core** (`mir-backend`).
- True for every .NET framework on the CLR (sync-over-async, ConfigureAwait, ValueTask, IDisposable, DI lifetime theory, CancellationToken, LOH)? → **runtime tier** (`mir-backend-dotnet`).
- A mechanical footgun of *this library* (middleware order, `AddDbContext` scope, model binding overposting, EF Core `Include`/projection, resource-based authz, antiforgery, `IOptions<T>`, migration safety)? → **here**.
- A different .NET framework (WCF, gRPC-only service, Blazor Server specifics) → new `mir-backend-dotnet-<framework>` module. A different runtime → its own tier. Never widen this one.
