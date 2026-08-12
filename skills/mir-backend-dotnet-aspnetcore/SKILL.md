---
name: mir-backend-dotnet-aspnetcore
description: "Make It Right (ASP.NET Core module). ASP.NET Core 10 + Minimal APIs + EF Core 10 + EF migrations specific reliability augmentation. Use alongside mir-backend and mir-backend-dotnet when the target stack is ASP.NET Core or Minimal APIs — it carries the mechanical footguns the runtime-agnostic tiers deliberately omit: DI lifetime errors in practice (AddDbContext Scoped vs singleton capture, IHttpContextAccessor caveats), middleware pipeline ORDER (UseRouting → UseAuthentication → UseAuthorization → UseAntiforgery → endpoints; wrong order silently disables auth), model binding overposting / mass assignment onto EF entities and why [Bind] does NOT work on JSON bodies, response DTO discipline to prevent field leakage, EF Core N+1 and the EF Core 10 parameterized-collection translation change, ExecuteUpdate/ExecuteDelete bypassing the change tracker, async-all-the-way in endpoints with CancellationToken from the request, object-level authorization (IDOR via valid token without resource check), the .NET 10 built-in Minimal API validation (AddValidation), current antiforgery defaults including the non-short-circuiting middleware and the insecure antiforgery cookie SecurePolicy, CORS credentials misconfiguration, Data Protection key-ring persistence, Native AOT feature support, built-in OpenAPI, and the Options pattern for config. TRIGGER only when the .NET backend stack uses ASP.NET Core or Minimal APIs — building, reviewing, or debugging a controller, endpoint, middleware, EF Core query, or migration. Always loads TOGETHER WITH mir-backend (gates) and mir-backend-dotnet (CLR runtime concerns: runtime version currency, sync-over-async, thread-pool starvation, ValueTask, IDisposable, DI captive dependency, CancellationToken, GC/DATAS, trimming, NuGet supply chain); this module adds only ASP.NET Core / EF Core library mechanics. SKIP for non-.NET runtimes (each has its own mir-backend-<runtime> tier and framework modules), SKIP for CLR-level concerns that belong in mir-backend-dotnet, and SKIP for other .NET frameworks — Blazor WebAssembly, standalone gRPC services, Orleans, WCF, and Azure Functions each get their own mir-backend-dotnet-<framework> module rather than this one."
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

**Stack assumed (verified 13 Aug 2026):** **ASP.NET Core 10** (controllers or Minimal APIs) · **EF Core 10** · SQL Server / PostgreSQL · `Microsoft.EntityFrameworkCore.Design` migrations. Current patch is 10.0.11 (11 Aug 2026).

Version notes that change what you should write:

- **EF Core 10 requires .NET 10.** There is no EF Core 10 on `net8.0`. A project stuck on `net8.0`/`net9.0` is stuck on EF Core 8/9, and both of those runtimes leave support on **10 Nov 2026** (see `mir-backend-dotnet`).
- **EF Core 10 changed how parameterized collections translate** — see footgun 4. This is the one upgrade change most likely to show up as a production latency regression rather than a compile error.
- **`ExecuteUpdateAsync` now takes a plain lambda instead of an expression tree** in EF Core 10. Code that composed `Expression<Func<SetPropertyCalls<T>, SetPropertyCalls<T>>>` by hand no longer compiles; rewrite it as ordinary `if`/loop statements inside the lambda. Each `SetProperty` selector is still expression-based and must translate to SQL.

If the project uses Dapper or a different ORM, note the divergence before applying EF-specific guidance.

## The ASP.NET Core footguns AI walks into most

### 1. DI lifetimes in practice — AddDbContext is Scoped; singletons must not capture it

`AddDbContext<T>()` registers `DbContext` as **Scoped** (one instance per HTTP request). Inject it into a Singleton — a hosted service, a cache, an event handler — and the captive-dependency bug from the runtime tier becomes one shared context serving every request: cross-request data bleed plus `InvalidOperationException: A second operation was started on this context`.

```csharp
// WRONG — MyWorker is Singleton; captures a Scoped DbContext at construction
services.AddSingleton<MyWorker>();
services.AddDbContext<AppDbContext>();
public class MyWorker(AppDbContext db) { ... }          // db lives forever

// RIGHT — register the factory instead and open a context per unit of work
services.AddDbContextFactory<AppDbContext>();
public class MyWorker(IDbContextFactory<AppDbContext> factory) {
    public async Task RunAsync(CancellationToken ct) {
        await using var db = await factory.CreateDbContextAsync(ct);
    }
}
```

**`IHttpContextAccessor` caveat:** it is Singleton but reads Scoped `HttpContext`. Safe in middleware and request-scoped code; inside a background service or event handler it returns `null`. It is not a general-purpose context bag.

### 2. Middleware ORDER is the pipeline contract

ASP.NET Core middleware runs in registration order, and nothing warns you about a wrong order. Be precise about the failure: `UseAuthorization()` before `UseAuthentication()` makes authorization evaluate an **anonymous** principal, so legitimate callers get 401/403 — it is broken, not a bypass. The orders that genuinely weaken security are `UseCors()` in the wrong place and `UseAntiforgery()` before auth.

```csharp
// WRONG — UseAuthorization before UseAuthentication; auth policies never fire correctly
app.UseRouting();
app.UseAuthorization();       // wrong position
app.UseAuthentication();
app.MapControllers();

// RIGHT — canonical order
app.UseExceptionHandler();    // 0. catches everything below it
app.UseHsts();
app.UseHttpsRedirection();
app.UseRouting();
app.UseCors();                // 1. before auth, so preflight gets headers
app.UseAuthentication();      // 2. identify the caller
app.UseAuthorization();       // 3. decide if they're allowed
app.UseAntiforgery();         // 4. AFTER auth — see footgun 7
app.MapStaticAssets();        // 5. endpoints
app.MapControllers();
```

Other ordering traps:
- `UseCors()` must come **before** `UseAuthentication`/`UseAuthorization` and the endpoint mappings to emit CORS headers on preflight.
- **`UseAntiforgery()` must come after `UseAuthentication` and `UseAuthorization`**, so form data is never read for an unauthenticated caller. `WebApplicationBuilder` adds it automatically in the right place; if you call it explicitly, you own the position.
- `UseExceptionHandler()` / `UseHsts()` / `UseHttpsRedirection()` belong at the very top (before any business middleware) so they catch all exceptions and redirect correctly.
- Custom middleware that reads `HttpContext.User` must come after `UseAuthentication`.
- **`MapStaticAssets()` (ASP.NET Core 9+) is an endpoint, not middleware.** It participates in routing, so it goes where the other `Map*` calls go — not where `UseStaticFiles()` used to sit above `UseRouting()`. It only serves assets known at build/publish time (with build-time compression and content-hash fingerprinting). Files written to disk after deployment, or served from embedded resources, still need `UseStaticFiles()`.
- `UseRateLimiter()` must come after `UseRouting()` if any policy is attached per-endpoint, otherwise the endpoint metadata is not resolved yet and the policy silently does not apply.

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

**`[Bind]` / `[BindNever]` do NOT protect a JSON API.** This is the most common wrong fix, and AI emits it confidently. Microsoft's docs state `[Bind]` "does not affect input formatters" — those attributes are read by the MVC *model binding* system, which handles form and query values. A `[FromBody]` parameter is deserialized by an input formatter (`System.Text.Json`), and binding-source attributes on its properties are ignored. `[Bind("Name,Email")]` on a JSON DTO is a no-op that reads like a control.

| Mechanism | Where it applies | Notes |
|---|---|---|
| A separate request record with only the writable fields | everywhere | The real fix. If the type has no `IsAdmin` property, nothing can set it. |
| `JsonSerializerOptions.UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow` | JSON bodies | Rejects payloads containing undeclared members (400 instead of silent ignore). Also per-type via `[JsonUnmappedMemberHandling]`. |
| `[JsonIgnore]` on the sensitive property | JSON bodies and responses | Serializer-level. Blunt — removes the property from output too. |
| `[Bind]` / `[BindNever]` / `[BindRequired]` | form and query binding **only** | Fine for Razor Pages/MVC form posts. Useless for `[FromBody]`. |

`[JsonExtensionData]` on a DTO collects every unmapped member into a dictionary and reintroduces the whole problem.

**Never return the EF entity directly** — use a response DTO. Otherwise you leak password hashes and internal flags, and the response shape changes with whichever navigation properties an earlier query happened to load.

### 4. EF Core N+1 — lazy loading raises in async, doesn't just degrade performance

Lazy-loading proxies issue one **synchronous** query per navigation access — they do not become async just because the surrounding method is. Inside a loop that is N+1; after the `DbContext` is disposed the navigation cannot load at all and you get `InvalidOperationException`. Both forms are defects:

```csharp
// WRONG — N+1: one query for orders, then one per order for customer name
var orders = await db.Orders.ToListAsync(ct);
foreach (var o in orders) {
    Console.WriteLine(o.Customer.Name);   // sync lazy load, or InvalidOperationException if disposed
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
- **`Include()` on two or more collection navigations produces a cartesian product** — rows multiply, and a "slow query" turns into gigabytes of duplicated data over the wire. Use `AsSplitQuery()` (separate SELECT per collection, no cross-collection consistency inside the query) or project to a DTO.

#### EF Core 10 changed collection parameter translation — an upgrade can regress query plans

`Where(b => ids.Contains(b.Id))` has been translated three different ways in three releases. EF Core 8 and 9 sent the whole collection as one JSON parameter (`OPENJSON` on SQL Server). **EF Core 10 defaults to multiple scalar parameters** — `WHERE [b].[Id] IN (@ids1, @ids2, @ids3)` — padded up to a bucket size so the plan cache does not fill with one entry per collection length. Better for small collections (the optimizer gets real cardinality), worse for large ones (wire overhead and a hard provider parameter limit). The upgrade does not break the build; it changes the SQL, so it shows up as latency.

```csharp
// Global override, per provider — back to the JSON-parameter form
optionsBuilder.UseSqlServer(conn,
    o => o.UseParameterizedCollectionMode(ParameterTranslationMode.Parameter));

// Per query
await db.Blogs.Where(b => EF.Parameter(bigList).Contains(b.Id)).ToListAsync(ct);   // one JSON parameter
await db.Blogs.Where(b => EF.Constant(fixedList).Contains(b.Id)).ToListAsync(ct);  // inline literals, no plan reuse
```

`TranslateParameterizedCollectionsToConstants` is obsolete — `UseParameterizedCollectionMode` replaces it.

#### `ExecuteUpdate` / `ExecuteDelete` skip everything the change tracker does

They issue one `UPDATE`/`DELETE` directly. That is why they are fast, and why they are dangerous in code that relies on `SaveChanges` behaviour:

- **No `SaveChangesAsync` interceptors fire** — audit columns, save-time soft-delete, outbox writes, and domain events all silently do not run.
- **No optimistic concurrency check.** A `[Timestamp]`/`rowversion` property is not compared, so a lost update is not detected.
- **Loaded entities go stale.** The tracked copy still holds the old values; a later `SaveChangesAsync` can write them back.
- **They execute immediately, outside the current `DbContext` transaction** unless you opened one with `db.Database.BeginTransactionAsync(ct)`.

Use them for genuine bulk operations, and state that decision at Gate 5.

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

### 7. Antiforgery — the middleware does not block anything, and the cookie is not Secure by default

If your app authenticates via cookies (not bearer tokens), mutating endpoints are exposed to CSRF. Three specific things about how ASP.NET Core's antiforgery actually behaves are routinely got wrong, including by AI-generated code.

**There is no `WithAntiforgery()` or `RequireAntiforgery()` extension method.** Validation is on by default for the endpoints that qualify; the opt-*out* is `DisableAntiforgery()`. To force validation on an endpoint that would not otherwise qualify, the shipped mechanism is the `[RequireAntiforgeryToken]` attribute (`Microsoft.AspNetCore.Antiforgery.RequireAntiforgeryTokenAttribute`, which implements `IAntiforgeryMetadata`) — apply it to the handler, or `.WithMetadata(new RequireAntiforgeryTokenAttribute())`. It still only covers POST/PUT/PATCH; for other methods resolve `IAntiforgery` and call `ValidateRequestAsync` yourself.

```csharp
builder.Services.AddAntiforgery();   // WebApplicationBuilder then adds UseAntiforgery() for you

// Automatic: a Minimal API endpoint that binds a form parameter is validated
app.MapPost("/orders", async ([FromForm] OrderForm form, ...) => { ... });

// Explicit opt-out (a webhook with its own signature check, for example)
app.MapPost("/stripe/webhook", async (...) => { ... }).DisableAntiforgery();
```

**Validation only fires when all three hold:** the endpoint carries `IAntiforgeryMetadata` with `RequiresValidation = true`, the method is POST/PUT/PATCH, and the request resolved to a valid endpoint. In practice that means Minimal API endpoints binding a form parameter, MVC actions with `[ValidateAntiForgeryToken]` or `[AutoValidateAntiforgeryToken]`, and Blazor SSR endpoints. A cookie-authenticated JSON `POST` that binds `[FromBody]` gets **no** antiforgery validation. Add `[RequireAntiforgeryToken]` to those endpoints, apply `[AutoValidateAntiforgeryToken]` as a global filter on MVC, or move them to bearer tokens.

**The middleware does not short-circuit the pipeline.** It records a verdict in `IAntiforgeryValidationFeature` and calls the next middleware. The *consumers* — MVC antiforgery filters, form-binding Minimal API endpoints, Blazor SSR, and any code reading `Request.Form` — enforce that verdict and return `400` before your handler body runs. The gap is an endpoint that carries antiforgery metadata but uses none of those consumers: a handler that reads the raw request body itself sees no rejection, so check the verdict yourself:

```csharp
var af = context.Features.Get<IAntiforgeryValidationFeature>();
if (af is { IsValid: false }) return Results.BadRequest();   // af.Error has the detail
```

**Insecure default to fix explicitly:** ASP.NET Core relaxes the antiforgery cookie's `SecurePolicy` to `CookieSecurePolicy.None`, so the cookie is sent over plain HTTP. Set it in every non-development environment:

```csharp
if (!builder.Environment.IsDevelopment()) {
    builder.Services.AddAntiforgery(o => o.Cookie.SecurePolicy = CookieSecurePolicy.Always);
}
```

Bearer-token-only APIs (no cookies, no browser auto-attach) do not need antiforgery. Mixed apps (cookie auth for the web UI, bearer for the API) must apply it to the cookie-authenticated endpoints only.

A header-based automatic CSRF middleware — validating `Sec-Fetch-Site` with an `Origin` fallback, on by default, with a global disable switch — is documented against ASP.NET Core 11. **.NET 11 has not shipped as of this writing (expected Nov 2026).** Do not design around it on .NET 10; keep the token-based defence, and re-check the docs before assuming it is available.

### 8. Options pattern for config — never read IConfiguration inline

`IConfiguration["Stripe:SecretKey"]` injected into a service returns `null` for a missing key and fails at first use, in production, on the request that needed it. Bind a typed Options class and validate at boot instead:

```csharp
public sealed class StripeOptions {
    [Required] public string SecretKey     { get; init; } = "";
    [Required] public string WebhookSecret { get; init; } = "";
}

services.AddOptions<StripeOptions>()
    .BindConfiguration("Stripe")
    .ValidateDataAnnotations()
    .ValidateOnStart();          // throws at boot if missing/invalid, not at first use
// shorter: services.AddOptionsWithValidateOnStart<StripeOptions>()
```

**The `required` keyword does not validate configuration.** C# `required` is a compile-time initialization rule enforced at object-creation sites. The configuration binder is not one of those sites, and `ValidateDataAnnotations()` only inspects `System.ComponentModel.DataAnnotations` attributes. A `required string SecretKey` with no `[Required]` attribute binds to `null` from a missing config key and passes validation. Use the attribute, and keep `required` only if you also construct the type in code.

Use `IOptionsSnapshot<T>` (Scoped, reloads per request with `reloadOnChange`) when you need live config updates. Use `IOptionsMonitor<T>` (Singleton-safe, fires `OnChange`) in long-lived services or hosted services. Never inject `IOptionsSnapshot<T>` into a Singleton — that is the captive dependency from `mir-backend-dotnet` footgun 7 in its most-shipped form.

### 9. EF Core migrations — AI writes them as if the table is empty

The table has millions of rows and the previous deploy is still running during a rolling update. Both assumptions AI makes are false. The two that lose data outright: **a rename generates `DROP` + `ADD`** unless you edit the migration to call `migrationBuilder.RenameColumn(...)`, and **`Database.Migrate()` in `Program.cs` races across replicas**. Read the generated SQL (`dotnet ef migrations script`) before any change that touches existing rows. Full recipes — NOT NULL columns, drops, type changes, `CREATE INDEX CONCURRENTLY`, `EnsureCreated()` — in `references/efcore-migration-safety.md`, read by the migration-reviewer at Gate 7.

### 10. Minimal API validation is built in as of .NET 10 — stop hand-rolling it

ASP.NET Core 10 added first-class validation to Minimal APIs using the same `System.ComponentModel.DataAnnotations` attributes MVC has always honoured. Before this, the options were a hand-written `IEndpointFilter`, a FluentValidation adapter, or `if (string.IsNullOrWhiteSpace(...)) return Results.BadRequest()` inside the handler. AI still writes the last one.

```csharp
using Microsoft.Extensions.Validation;   // NOT Microsoft.AspNetCore.Http.Validation — renamed before RTM
builder.Services.AddValidation();        // opt-in; nothing validates without this. That is the whole setup.
```

Two preview-era snippets to delete on sight: the `Microsoft.AspNetCore.Http.Validation` namespace (renamed to `Microsoft.Extensions.Validation` before RTM), and an `<InterceptorsNamespaces>` property in the `.csproj` — the .NET 10 Web SDK wires the generator itself. Call `AddValidation()` from **each assembly that defines endpoints**; the generator only sees the assembly it is invoked in. It is not supported in MVC.

```csharp
public record RegisterUserRequest(
    [Required, EmailAddress] string Email,
    [Required, StringLength(100, MinimumLength = 12)] string Password);

app.MapPost("/register", (RegisterUserRequest req) => { /* only runs if valid */ });
```

It is source-generated (discovered at compile time, no runtime reflection), runs before the handler, and returns `400` with `ProblemDetails` on failure. `IValidatableObject` covers cross-property rules; `DisableValidation()` opts an endpoint out and `[SkipValidation]` opts out one parameter, type, or property.

**The failure mode: a type the generator could not see at compile time is not validated, and nothing says so.** When the object graph is not statically determinable, annotate the type `[ValidatableType]` to force it in. There is also a known gap where nullable value types used directly as Minimal API parameters are not validated. Both look identical to working code in review — assert with a test that posts a known-invalid body and expects `400`.

### 11. Native AOT and in-box OpenAPI — read the reference before promising either

`PublishAot` is not a flag you add to an existing app: **MVC and Razor Pages are not AOT-supported at all**, and `CreateSlimBuilder()` is not a drop-in for `CreateBuilder()`. Separately, .NET 9 replaced Swashbuckle with `Microsoft.AspNetCore.OpenApi` in the templates (no UI in the box), and **ASP.NET Core 10 emits OpenAPI 3.1 by default**, which consumers pinned to 3.0 reject. Full feature matrix and migration breaks: `references/deployment-and-openapi.md`.

## How this slots into the core pipeline

- **Gate 5 (Design):** state the middleware order, DI lifetimes (especially DbContext scope vs factory), DTO boundaries and the overposting control you chose, auth strategy (route-level + resource-level), antiforgery applicability, and whether the deployment target requires Native AOT (footgun 11 constrains the whole design if so).
- **Gate 6 (Implementation):** code against footguns 1–11 above. Every new endpoint: DTO in/out, `ct` propagated, ownership check present, validation attributes on the request record and the record declared `public`. Every new EF query: `Include` or `Select`, `AsNoTracking` on reads.
- **Gate 7 (Review):** reliability-reviewer checks items 1–11; additionally confirms `ValidateOnBuild = true` is set unconditionally, not only in Development. security-reviewer runs the Security list below plus `references/security-operations.md`. Migration-reviewer reads the generated SQL against `references/efcore-migration-safety.md`.

| Reference | What it holds | Read at |
|---|---|---|
| `references/efcore-migration-safety.md` | Per-change recipes for populated tables: nullable-then-backfill, rename without data loss, `CREATE INDEX CONCURRENTLY`, expand/contract type changes, `Database.Migrate()` races, `EnsureCreated()` | Gate 7, migration-reviewer |
| `references/security-operations.md` | The four current advisories, the leakage table (`EnableSensitiveDataLogging`, developer exception page, server header), upload and file-serving traversal, Data Protection key-ring persistence, rate limiting on auth endpoints | Gate 7, security-reviewer |
| `references/deployment-and-openapi.md` | Native AOT per-feature support matrix and `CreateSlimBuilder` differences; in-box OpenAPI, the 3.1 default, and the `Microsoft.OpenApi` v2 migration break | Gate 5, only when AOT or a published OpenAPI document is in scope |

## Security

ASP.NET Core and EF Core library security. CLR-level items (serializer choice, `Process`, `Path`, `HttpClient`/SSRF, cryptography, NuGet supply chain, patch floor) are in `mir-backend-dotnet`.

### Advisories, leakage, files, key ring, rate limiting → `references/security-operations.md`

Patch floor as of 13 Aug 2026: **10.0.11 · 9.0.19 · 8.0.30**. Four advisories hit code you did not write — CVE-2025-55315 (Kestrel request smuggling, CVSS 9.9), CVE-2026-40372 (DataProtection padding oracle, non-Windows, **rotate the key ring after patching**), CVE-2026-47303 (Negotiate EoP), CVE-2026-45591 (MessagePack hub DoS). The reference also carries the leakage table (`EnableSensitiveDataLogging`, developer exception page, `AddServerHeader`), upload/file-serving traversal, the Data Protection key-ring persistence failure, and rate limiting on auth endpoints. Read it at Gate 7.

### Object-level authorization (IDOR / BOLA) and mass assignment

Footgun 6 is the IDOR mechanism, footgun 3 the overposting one. The per-endpoint review questions: *does the query filter by the caller's identity, or only by the route id?* and *can the client set a field the handler never intended?* `FindAsync(id)` after `RequireAuthorization()` is the default AI shape and it is broken. Enforce ownership in the `Where` clause, or use a resource-based handler (`IAuthorizationHandler<TRequirement, TResource>` via `IAuthorizationService.AuthorizeAsync(user, resource, policy)`), which sees the loaded entity.

For multi-tenancy, a global query filter (`modelBuilder.Entity<T>().HasQueryFilter(e => e.TenantId == _tenant.Id)`) is the systematic control. A composable `FromSql`/`FromSqlRaw` **is** still a LINQ query root, so the filter composes over it — the ways out are `IgnoreQueryFilters()` and anything executed outside the entity query pipeline (`ExecuteSqlRaw`, Dapper, a raw `DbCommand`). Enforce tenancy at the repository boundary and in database permissions too; the filter reduces the number of places you can get this wrong, it does not eliminate them.

### Injection

- **SQL.** `FromSqlRaw` and `ExecuteSqlRaw` take the SQL text as raw. `FromSql`, `FromSqlInterpolated`, and `ExecuteSql` take a `FormattableString` and extract its arguments into `@p0`, `@p1` — that holds whether the `FormattableString` is written inline or held in a variable. The trap is upstream of the call: a string you built with `string.Format`, `$"..."` into a `string`, or `+` has already lost the argument boundaries, so passing it to any of these is plain injection. EF Core 10 ships an analyzer that warns on concatenation inside a raw-SQL call — do not suppress it. Identifiers (column and table names) can never be parameters; those need an allow-list.
- **Sorting and paging.** `OrderBy(userField)` via `System.Linq.Dynamic.Core` or a raw SQL `ORDER BY` interpolation is injection with extra steps. Map the client's sort key through a dictionary of allowed columns.
- **Header injection / open redirect.** Never write a user value into a `Location` header. Use `Results.LocalRedirect(returnUrl)` (Minimal APIs) or `LocalRedirect(...)` / `Url.IsLocalUrl(returnUrl)` (MVC), which reject absolute URLs, instead of `Results.Redirect(returnUrl)`.
- **Prompt injection.** If the app calls an LLM through `Microsoft.Extensions.AI` or Semantic Kernel, remember that a registered function/tool executes with the application's identity and its own DI dependencies — including `DbContext`. Treat every model-requested tool call as untrusted input: authorize the *caller's* access to the resource inside the tool, never inside the prompt.

### CORS

```csharp
// WRONG — throws at runtime: the CORS protocol forbids wildcard origin with credentials
policy.AllowAnyOrigin().AllowCredentials();

// WRONG AND WORSE — this is the "fix" AI reaches for. It compiles, runs, and reflects
// whatever Origin the attacker sends back in Access-Control-Allow-Origin, with credentials.
policy.SetIsOriginAllowed(_ => true).AllowCredentials().AllowAnyHeader().AllowAnyMethod();

// RIGHT
policy.WithOrigins("https://app.example.com").AllowCredentials()
      .WithHeaders("content-type", "authorization").WithMethods("GET", "POST");
```

Reflected origin plus credentials removes the same-origin protection entirely. If you need subdomains, `SetIsOriginAllowedToAllowWildcardSubdomains()` with `WithOrigins("https://*.example.com")`; if you need a predicate, check the parsed host against a real list, never `=> true`. `UseCors()` must run before authentication and before the endpoints (footgun 2). `[DisableCors]` is not a CSRF control.

### CSRF and SameSite

- Cookie authentication needs antiforgery. Bearer tokens in an `Authorization` header do not — the browser does not attach them automatically. A mixed app needs it on the cookie-authenticated endpoints only.
- Set `SameSite` explicitly on auth cookies: `options.Cookie.SameSite = SameSiteMode.Lax` (or `Strict`), plus `options.Cookie.SecurePolicy = CookieSecurePolicy.Always` and `HttpOnly = true`. `SameSiteMode.None` requires `Secure` and reopens cross-site sending — only for a deliberate cross-site flow.
- The antiforgery cookie's `SecurePolicy` is `None` by default (footgun 7). Override it.
- Do not rely on `SameSite` alone as the CSRF defence — it is not enforced identically across browsers and does not cover same-site subdomain attackers.

### Settings that ship insecure or unenforced, by exact name

| Setting | Default | What to do |
|---|---|---|
| `AntiforgeryOptions.Cookie.SecurePolicy` | `CookieSecurePolicy.None` | `Always` outside Development |
| `ServiceProviderOptions.ValidateScopes` / `.ValidateOnBuild` | on in Development only | set unconditionally |
| `KestrelServerOptions.AddServerHeader` | `true` | `false` |
| `JwtBearerOptions.MapInboundClaims` | `true` — rewrites `sub` to `ClaimTypes.NameIdentifier`, so `User.FindFirst("sub")` returns null and ownership checks silently compare against nothing | `false`, plus explicit `TokenValidationParameters.NameClaimType` / `RoleClaimType` |
| `TokenValidationParameters.ValidateIssuer` / `ValidateAudience` / `ValidateLifetime` | `true` | never set to `false` to "make it work" in dev — a token from any issuer then authenticates |
| `MapOpenApi()` | unauthenticated when called | gate it |
| Rate limiting | not registered | register it on auth endpoints |

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY ASP.NET Core / Minimal APIs / EF Core mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability)? → **generic core** (`mir-backend`).
- True for every .NET framework on the CLR (runtime version currency, sync-over-async, ConfigureAwait, ValueTask, IDisposable, DI lifetime theory, CancellationToken, GC/DATAS/LOH, host lifecycle, trimming/AOT mechanics, NuGet supply chain, `Process`/`Path`/`HttpClient`/crypto security)? → **runtime tier** (`mir-backend-dotnet`).
- A mechanical footgun of *this library* (middleware order, `AddDbContext` scope, model binding overposting, EF Core translation and `Include`/projection, resource-based authz, antiforgery, `AddValidation`, `IOptions<T>`, Data Protection key ring, CORS policy, migration safety)? → **here**.
- A different .NET framework (Blazor WebAssembly, standalone gRPC, Orleans, Azure Functions, WCF) → new `mir-backend-dotnet-<framework>` module. A different runtime → its own tier. Never widen this one.
