# Native AOT support and in-box OpenAPI (ASP.NET Core 10)

Read at Gate 5 **only when** the deployment target requires Native AOT, or when the API publishes an OpenAPI document. Neither changes what you write for an ordinary endpoint, which is why they are not in `SKILL.md`.

Verified 13 Aug 2026.

---

## Native AOT — check the feature matrix before promising it

`PublishAot` is not a flag you add to an existing app. Microsoft publishes a per-feature compatibility table; the parts that decide most projects:

| Fully supported | Partially supported | **Not supported** |
|---|---|---|
| gRPC, JWT bearer auth, CORS, HealthChecks, RateLimiting, OutputCaching, ResponseCompression, StaticFiles, WebSockets, HttpLogging, Localization, Rewrite | Minimal APIs, SignalR | **MVC and Razor Pages**, **Blazor Server**, Session, SPA services, authentication schemes other than JWT bearer |

Practical consequences:

- **An MVC app cannot be made AOT-compatible by setting `PublishAot=true`.** Razor view compilation, TagHelpers, and dynamic model binding rely on runtime code generation. You get build warnings, then missing-method or type-load failures at runtime, or features that throw "not supported."
- The `webapiaot` template uses Minimal APIs with `WebApplication.CreateSlimBuilder()` — a reduced default feature set, HTTP only, no IIS profile. `CreateSlimBuilder` is not a drop-in for `CreateBuilder`; things you assumed were registered are not.
- The usual real-world shape is a split: AOT-published Minimal API or gRPC services alongside a JIT-published MVC app, not one binary.
- Trimming/AOT warning handling is a runtime-tier concern — see `mir-backend-dotnet` footgun 12. The rule there applies here: zero AOT warnings at publish, no suppressions.
- For the Options pattern under AOT, use the source-generated validator (`[OptionsValidator]` on a partial class implementing `IValidateOptions<T>`); `ValidateDataAnnotations()` is reflection-based and produces trim warnings.

## OpenAPI moved in-box — Swashbuckle is no longer in the templates

Since .NET 9 the API templates ship `Microsoft.AspNetCore.OpenApi` instead of Swashbuckle, and **there is no UI in the box** — add `Swashbuckle.AspNetCore.SwaggerUI` or `Scalar.AspNetCore` and point it at the document endpoint.

```csharp
builder.Services.AddOpenApi();
// ...
if (app.Environment.IsDevelopment()) app.MapOpenApi();   // serves /openapi/v1.json — document only
```

- **ASP.NET Core 10 emits OpenAPI 3.1 by default** (JSON Schema draft 2020-12), up from 3.0 in .NET 9. Client generators and gateways pinned to 3.0 will reject the document. Check the consumer before upgrading.
- Swashbuckle was removed from the templates, not deprecated. Swashbuckle 10.x supports .NET 10 but still emits 3.0 unless you set `options.OpenApiVersion = OpenApiSpecVersion.OpenApi3_1`. Do not mix `AddOpenApi()` with `AddSwaggerGen()`.
- Migration break: `Microsoft.AspNetCore.OpenApi` v10+ depends on `Microsoft.OpenApi` v2+, whose types moved out of the `Microsoft.OpenApi.Models` namespace. Remove any explicit `Microsoft.OpenApi` `PackageReference`.
- **`MapOpenApi()` is unauthenticated.** Gate it behind `IsDevelopment()` or `.RequireAuthorization()`.
