# ASP.NET Core security operations: advisories, leakage, files, key ring, rate limiting

Read by the security-reviewer at Gate 7, and at Gate 5 when the design touches file serving, cookie auth, or auth-adjacent endpoints. The per-endpoint controls an agent needs *while writing code* (IDOR, injection, CORS, CSRF, the insecure-defaults table) stay in `SKILL.md`.

Verified 13 Aug 2026. CLR-level items (serializer choice, `Process`, `Path`, `HttpClient`/SSRF, cryptography, NuGet supply chain) are in `mir-backend-dotnet`.

---

## Advisories on the default path

Every one of these affects code you did not write. Patch floor as of 13 Aug 2026: **10.0.11 · 9.0.19 · 8.0.30**. The runtime tier (`mir-backend-dotnet`) carries the same table plus the non-ASP.NET entries; this copy is the ASP.NET-relevant read.

| CVE | Component | Why it matters here |
|---|---|---|
| CVE-2025-55315 | Kestrel HTTP parsing | Request smuggling, CVSS 9.9 — the highest ever assigned to ASP.NET Core. A smuggled request can log in as another user, bypass CSRF checks, or reach internal endpoints. Fixed in 8.0.21 / 9.0.10 / Kestrel.Core 2.3.6. .NET 6 is EOL and never got a fix. |
| CVE-2026-40372 | `Microsoft.AspNetCore.DataProtection` 10.0.0–10.0.6 | Padding oracle → forged authentication cookies and decryption of protected payloads. CVSS 9.1, **non-Windows hosts only**. Fixed in 10.0.7 — **and you must rotate the key ring**, because tokens forged before the patch stay valid. |
| CVE-2026-47303 | `Microsoft.AspNetCore.Authentication.Negotiate` | Elevation of privilege via parsing, including LDAP injection. CVSS 8.8. Fixed in 8.0.29 / 9.0.18 / 10.0.10. |
| CVE-2026-45591 | SignalR / Blazor Server MessagePack hub protocol | Deeply nested arrays cause a stack overflow — an unauthenticated DoS on any hub using MessagePack. |

Do not hand-parse `Content-Length` or `Transfer-Encoding` in custom middleware. Disagreeing with Kestrel about request boundaries is how smuggling bugs get reintroduced above the patched layer.

## Secret, PII, and stack-trace leakage

| Leak | Fix |
|---|---|
| `UseDeveloperExceptionPage()` reachable in production, or `ASPNETCORE_ENVIRONMENT=Development` set on a prod host — returns the stack trace, the routing table, and loaded configuration | Gate on `app.Environment.IsDevelopment()`; verify the deployed env var, not the code |
| `UseExceptionHandler` handler that writes `ex.ToString()` or `ex.Message` into the response | Return `Results.Problem()` / `ProblemDetails` with a correlation id; log the detail server-side only |
| `AddProblemDetails()` with a customizer that copies exception detail into `Extensions` | Add the correlation id, nothing from the exception |
| **`optionsBuilder.EnableSensitiveDataLogging()`** left on — logs every command parameter value: passwords, tokens, PII | Development only. For production query visibility use a `DbCommandInterceptor` that logs SQL text, duration, and parameter *names and types*, never values. Keep `Microsoft.EntityFrameworkCore.Database.Command` at Warning |
| Returning EF entities from endpoints (footgun 3) | Response DTOs |
| Kestrel advertises itself in every response | `builder.WebHost.ConfigureKestrel(o => o.AddServerHeader = false)` |
| `MapOpenApi()` publishing the full route and schema inventory unauthenticated | `if (app.Environment.IsDevelopment())` or `.RequireAuthorization()` |

## Path traversal in file serving and uploads

- **`IFormFile.FileName` is attacker-controlled** and may contain `..`, a rooted path, or a null byte. Never use it to build a save path. Generate the stored name yourself and keep the original as metadata only. `Path.GetFileName()` strips directory components but does not make an arbitrary name safe.
- Any endpoint taking a filename or path from the request and calling `Results.File` / `PhysicalFile` / `File.OpenRead` needs the canonicalize-then-prefix-check from `mir-backend-dotnet`.
- `UseStaticFiles` with a `PhysicalFileProvider` rooted above `wwwroot` serves whatever is under that root — including `appsettings.Production.json` if you root it at the content path.
- Set upload limits deliberately: `[RequestSizeLimit]` per endpoint, `FormOptions.MultipartBodyLengthLimit`, and Kestrel's `MaxRequestBodySize`. Unbounded multipart is a memory DoS.
- Validate content by inspecting the bytes, not by trusting `ContentType` or the extension.

## Data Protection key ring

Antiforgery tokens, authentication cookies, and anything through `IDataProtector` are keyed by the Data Protection key ring. **The default persistence is local** — a directory in the user profile or the container filesystem. Two failures follow:

- **Container restart** discards the keys, invalidating every issued auth cookie and antiforgery token. It reads as random logouts and unexplained 400s.
- **Multiple instances behind a load balancer each generate their own ring**, so a request handled by a different replica cannot decrypt the cookie. Same symptom, but intermittent, which is worse.

```csharp
builder.Services.AddDataProtection()
    .PersistKeysToAzureBlobStorage(blobUri, credential)   // or ToFileSystem on a shared volume, ToDbContext, ToStackExchangeRedis
    .ProtectKeysWithAzureKeyVault(keyIdUri, credential)   // keys at rest are otherwise unencrypted on non-Windows
    .SetApplicationName("orders-api");                    // required for sharing across apps; changing it invalidates everything
```

Rotate the ring after any exposure of the key store, and after CVE-2026-40372 specifically.

## Rate limiting and brute force

`AddRateLimiter` / `UseRateLimiter` ship in the box and are not enabled by default. Login, password-reset, token, and OTP endpoints need a partitioned limiter keyed on client identity, applied with `.RequireRateLimiting("auth")`. `UseRateLimiter()` must come after `UseRouting()` when any policy is attached per-endpoint. Note that ASP.NET Core Identity's lockout is per-account, so it does not stop password spraying across many accounts from one source.
