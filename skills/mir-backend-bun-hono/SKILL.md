---
name: mir-backend-bun-hono
description: "Make It Right (Hono module). Hono 4 reliability augmentation for backends built on the Web-standard Request/Response model — Bun, Cloudflare Workers, Deno, and Node via @hono/node-server. Carries the mechanical footguns the runtime-agnostic tiers omit: the request body is a stream read once, so c.req.raw after a validator throws and you need cloneRawRequest; Context is request-scoped and must not outlive the response; `await next()` never throws, so try/catch cleanup middleware sees nothing and the error lands in c.error instead; validator() parses but does not authorize, and yields an empty object when the Content-Type header is missing; RPC/hc types come from route chaining and drift from what onError and middleware actually return; the hono/bun vs hono/cloudflare-workers adapter split, where Node built-ins, the filesystem, and long-lived state work on Bun and fail on Workers; streaming responses escape onError once the first byte is written; and module-scope Maps used for rate limiting or caching, which are wrong on edge isolates. Also carries the 2026 Hono advisory set: CORS credentials-with-wildcard origin reflection (CVE-2026-54290, HIGH), JWT accepting any Authorization scheme, bodyLimit chunked bypass, cache-middleware cross-user leakage, and hono/jsx cross-request context disclosure. TRIGGER only when the web framework is Hono — building, reviewing, or debugging a Hono route, middleware, validator, RPC client, adapter, or streaming handler, on any runtime it targets. Loads TOGETHER WITH mir-backend (the gates) plus the runtime tier for the real deploy target: mir-backend-bun for Bun, mir-backend-node when served through @hono/node-server. SKIP for Elysia and raw Bun.serve handlers, SKIP for Express, Fastify, NestJS, Koa, and Hapi (each has its own mir-backend-node-<framework> module), SKIP for a Cloudflare Worker with no Hono in it, and SKIP for Bun runtime mechanics themselves — the bundler, test runner, Bun-native APIs, and install supply chain live in mir-backend-bun."
trigger: /mir-backend-bun-hono
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-bun-hono · Make It Right (Hono)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-bun` (Bun runtime model) → **this** (Hono library mechanics). Run the gates first; load the runtime tier for the process, scheduler, and install-time concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (Bun's event loop and worker model, Bun-native APIs, the test runner and bundler, install scripts and lockfile trust) live in `mir-backend-bun` — not here.** If the deploy target is Node through `@hono/node-server`, load `mir-backend-node` as the runtime tier instead.

**Stack assumed (npm registry + GitHub advisory DB, 13 Aug 2026):** `hono@4.13.1` is `latest`, published 2026-08-07. Hono 4 is the only current major; there is no v5. The package has **zero runtime dependencies** and declares `engines.node >= 16.9.0`, but target the runtime tier's floor, not Hono's. Bun `1.3.14`. Companions: `@hono/node-server@2.1.0`, `@hono/zod-validator@0.9.0` (peer `hono >= 4.11.2`, `zod ^3.25 || ^4`), `@hono/standard-validator@0.4.0`, `@hono/zod-openapi@1.5.2`. Rate limiting is **not** built in — `hono-rate-limiter@0.5.3` is third-party.

**Minimum patched version is 4.12.34**, not just "4.x" — see the advisory table in Security. 4.13.0 also reworked the core request path for speed and added HTTP `QUERY` method support; treat a 4.12→4.13 bump as a real upgrade and re-run route tests.

## The Hono footguns AI walks into most

### 1. The request body is a stream, read once — `c.req` caches, `c.req.raw` does not

Hono hands you a Web-standard `Request`, not Node's `IncomingMessage`. There is no `req.body` already-parsed object, and the body stream is consumed on first read.

`HonoRequest` shields you from most of that: `c.req.json()`, `.text()`, `.arrayBuffer()`, `.parseBody()` all go through an internal `bodyCache` and cross-convert, so calling them repeatedly — or after a validator already ran — is safe. `c.req.raw` is the underlying `Request` and has no such cache:

```ts
// WRONG — the validator already drained raw; this throws "ReadableStream is locked"
app.post('/forward', zValidator('json', Schema), async (c) => {
  return fetch(UPSTREAM, { method: 'POST', body: c.req.raw.body }) // raw.bodyUsed === true
})

// RIGHT — rebuild a fresh Request from the cached body
import { cloneRawRequest } from 'hono/request'
app.post('/forward', zValidator('json', Schema), async (c) => {
  return fetch(UPSTREAM, await cloneRawRequest(c.req))
})
```

`cloneRawRequest` clones directly when `raw.bodyUsed` is false, otherwise rebuilds from `bodyCache`. If you bypassed Hono and read `c.req.raw.json()` yourself, there is no cache and it throws `HTTPException(500)` telling you so. Rule: **read bodies through `c.req.*`, never through `c.req.raw.*`.** Same rule outbound — a `Response` body is read-once, so `res.clone()` before you both log it and return it.

### 2. `Context` is request-scoped and dies with the response

One `Context` per request, alive until the response is returned. `c.set()` / `c.get()` values exist only inside that request.

- Never store `c` in a module-level variable, a class field, or a closure that outlives the handler. On Workers you get `Cannot perform I/O on behalf of a different request`; on Bun you get silent cross-request bleed.
- Fire-and-forget work must not hold `c`. Extract the values first, then hand those to the background task — and on Workers wrap it in `c.executionCtx.waitUntil(p)` or the isolate is frozen the moment you return. Reading `c.executionCtx` on any other runtime throws `This context has no ExecutionContext`; see `references/runtime-portability.md` for the branch that actually works.
- Type variables with a generic on the app (`new Hono<{ Variables: { user: User } }>()`), not with the global `ContextVariableMap`. The docs are explicit that `ContextVariableMap` "adds types **globally** to all contexts, regardless of whether the middleware that sets the variable has actually run" — so a route missing its auth middleware still type-checks and `c.get('user')` is `undefined` at runtime.

### 3. `await next()` never throws — your try/catch middleware is dead code

The onion order is registration order: everything before `await next()` runs outward-in, everything after runs inward-out. The trap is that **Hono catches handler and downstream-middleware errors itself**, so `next()` resolves normally and puts the error on `c.error`:

```ts
// WRONG — catch never fires; the transaction commits on a failed handler
app.use(async (c, next) => {
  const tx = await db.begin()
  try { await next(); await tx.commit() }
  catch { await tx.rollback() }        // unreachable
})

// RIGHT — decide from c.error and the response status
app.use(async (c, next) => {
  const tx = await db.begin()
  await next()
  if (c.error || c.res.status >= 500) { await tx.rollback() } else { await tx.commit() }
})
```

Two more ordering rules:
- **Not awaiting `next()` silently skips the rest of the chain's post-phase** and lets the handler race your middleware. Always `await` it.
- Middleware either `await next()` and returns nothing, or returns a `Response` to short-circuit. A middleware that calls `next()` *and* returns a `Response` discards the handler's work. To replace a response after the fact, assign `c.res` — set it to `undefined` first, which is the documented way to drop the existing one.
- Auth middleware must be registered **before** the routes it guards, on the same instance. `app.use('/admin/*', auth)` after `app.route('/admin', adminApp)` does not protect those routes.

### 4. `validator()` is a parser, not a guard

`zValidator('json', Schema)` gives you `c.req.valid('json')`. Three separate failures:

- **Validation is not authorization.** A schema-valid `{ "orderId": "..." }` is still a request to touch someone else's order. The ownership predicate belongs in the query — see Security.
- **A missing `Content-Type` yields `{}`.** The docs: when you validate `json` or `form`, "the request _must_ contain a matching `content-type` header," otherwise the body is not parsed. If every field in your schema is optional, `{}` passes validation and the handler runs on nothing. Make at least one field required, or check the header.
- **Header targets must be lowercase keys** — validate `value['idempotency-key']`, not `Idempotency-Key`. `c.req.header()` with no argument also returns all-lowercase keys.

Mass assignment is your job: define the input schema with exactly the client-settable fields, and take `userId` / `tenantId` / `role` / `price` from the authenticated context, never from `c.req.valid()`.

### 5. Typed routing and the `hc` client drift from runtime reality

`AppType` inference only works if you **chain** route definitions (`app.get(...).post(...)`) and chain again at each `.route()` merge. Break the chain and the client silently degrades to `any` — no error, just no safety.

What the types do *not* know:
- Responses produced by `app.onError` and by middleware are not in the inferred union unless you merge them with `ApplyGlobalResponse`. The client's exhaustive status handling is therefore incomplete.
- `c.notFound()` blocks correct 404 inference — return `c.json({...}, 404)` instead.
- The client is a compile-time artifact. A deployed server on an older build serves a different shape than the types say. Version the API or the type is a lie.

For large apps, export a pre-computed `Client` type from a wrapper module so `tsc` instantiates the generics once — otherwise `tsserver` recomputes them per keystroke and the editor stalls in proportion to route count.

### 6. The adapter split: it runs on Bun, it fails on Workers

Hono's core is runtime-neutral; everything platform-specific is an adapter import. Portable code on Bun is not automatically portable to Workers. Read `references/runtime-portability.md` before committing to a multi-runtime deploy.

| What | Bun | Cloudflare Workers |
|---|---|---|
| Static files | `serveStatic` from `hono/bun` | `hono/cloudflare-workers` (assets binding) — no disk |
| Node built-ins | work | `nodejs_compat` required, and the subset depends on your `compatibility_date`. `fs` is a **virtual** filesystem (`/tmp` writable but per-request); `child_process` and `worker_threads` are importable stubs that do nothing |
| Config | `process.env` via `env(c)` | `c.env.BINDING` — `process.env` is absent by default |
| Work after the response | just await it | **must** be `c.executionCtx.waitUntil(p)` — and `c.executionCtx` is a **throwing getter** off Workers, so `c.executionCtx?.waitUntil?.(p)` does not degrade gracefully, it 500s. Branch on `getRuntimeKey() === 'workerd'` |
| Request body cap | Bun's `maxRequestBodySize`, default **128 MiB** | platform limit |
| WebSockets | `createBunWebSocket()` from `hono/bun` | `upgradeWebSocket` from `hono/cloudflare-workers` |

Use `getRuntimeKey()` (`bun` · `workerd` · `node` · `deno` · `edge-light` · `fastly` · `other`) when a branch is genuinely unavoidable, and `env(c)` to read config uniformly. Preset choice is a real decision: `hono` (SmartRouter) is the documented default for long-lived Bun/Node servers and is fine on Workers because isolates persist; `hono/quick` is for environments re-initialized per request; `hono/tiny` for size-constrained ones.

### 7. Streaming: once the first byte is written, `onError` is gone

`stream()` / `streamText()` / `streamSSE()` return immediately and the callback runs against an already-committed response. The docs state it plainly: if the streaming callback throws, "the `onError` event of Hono will not be triggered." Status and headers are already on the wire, so there is nothing left to turn into a 500 — the client sees a truncated 200.

```ts
app.get('/events', (c) => streamSSE(c,
  async (stream) => {
    stream.onAbort(() => cleanup())                 // client disconnect
    while (!stream.aborted) {
      await stream.writeSSE({ data: JSON.stringify(await nextEvent()) })
    }
  },
  async (err, stream) => { log.error({ err }); await stream.close() }  // the 3rd arg is the only handler
))
```

Do every failable thing — auth, the first DB read, permission checks — **before** returning the stream, so a real failure is still an ordinary status code. Always pass the third `onError` argument; always handle `stream.aborted` / `onAbort`, or a disconnected client leaves the producer running and billing.

### 8. `onError` and `notFound` — one shape, one place, no internals

`app.onError((err, c) => Response)` catches everything the handlers and middleware throw (not streaming callbacks, see §7). Route-level handlers take priority over a parent app's. `app.notFound()` only fires from the **top-level** app — setting it on a sub-router does nothing.

```ts
import { HTTPException } from 'hono/http-exception'

app.onError((err, c) => {
  if (err instanceof HTTPException) { return err.getResponse() }
  c.get('log').error({ err, requestId: c.get('requestId') }, 'unhandled')
  // never echo err.message on 5xx — driver errors carry SQL, table names, and paths
  return c.json({ error: 'Internal error', requestId: c.get('requestId') }, 500)
})
```

Throw `HTTPException(status, { message })` for expected failures; it carries the status through. A bare `throw new Error()` becomes a 500 with whatever your handler decides to print.

### 9. Module-scope state is wrong on edge isolates

```ts
const hits = new Map<string, number>()   // WRONG on Workers, unreliable on multi-process Bun
```

Cloudflare's own docs: "there is no guarantee that any two user requests will be routed to the same or a different instance of your Worker," and "do not use or mutate global state." Isolates are created and evicted freely. So a module-level `Map` is a rate limiter that counts a fraction of traffic, a cache with an unbounded per-isolate copy, and an idempotency ledger that forgets. The same code on Bun works until you run more than one process, then each has its own counter.

Put shared counters and caches in a store that is actually shared: Cloudflare KV or Durable Objects (or `caches.default` for HTTP caching) on Workers, Redis or Postgres on Bun. Reserve module scope for immutable config and clients you would rebuild identically anyway.

## Security

Hono library mechanics. Runtime-level items (install scripts, lockfile trust, secrets in the process environment, SSRF dialer control, path containment) live in `mir-backend-bun`.

### Current advisory set — the floor is 4.12.34

Hono ships security fixes inside patch releases; a `^4.12.0` range does not guarantee you have them, and a stale lockfile silently does not.

| CVE / GHSA | Severity | Fixed in | What it does |
|---|---|---|---|
| **CVE-2026-54290** (`GHSA-88fw-hqm2-52qc`) | **High** | 4.12.25 | `cors({ credentials: true })` with `origin` left at the default wildcard **reflects the request `Origin`** and sends `Access-Control-Allow-Credentials: true`. This used to fail closed (browsers reject `*` + credentials); now every origin, including `null`, can read your cookie-authenticated endpoints. |
| CVE-2026-69207 (`GHSA-8j4g-w8fx-2239`) | Medium | 4.12.34 | ReDoS parsing `Access-Control-Request-Headers` on preflight when `allowHeaders` is unset (**the default**). One request burns CPU. |
| CVE-2026-71850 (`GHSA-f23p-vx2j-j53r`) | Medium | 4.12.34 | `hono/jsx` `memo()` reused a retained SSR result across requests when props compared equal — another user's HTML, including CSRF tokens. |
| CVE-2026-59896 (`GHSA-hvrm-45r6-mjfj`) | Medium | 4.12.27 | `hono/jsx` stored SSR context process-wide, so `useContext()` after an `await` in an async component returned a concurrent request's value. |
| CVE-2026-59895 (`GHSA-w62v-xxxg-mg59`) | Medium | 4.12.27 | `hono/css` `cx()` marked its output as pre-escaped without escaping input — server-side XSS through a class name. |
| CVE-2026-47673 (`GHSA-f577-qrjj-4474`) | Medium | 4.12.21 | `jwt`/`jwk` middleware accepted **any** `Authorization` scheme, not just `Bearer` — bypasses WAF and gateway rules keyed on the scheme. |
| CVE-2026-47675 (`GHSA-3hrh-pfw6-9m5x`) | Medium | 4.12.21 | Cookie helper did not sanitize `sameSite`/`priority` → `Set-Cookie` injection. |
| CVE-2026-47674 (`GHSA-xrhx-7g5j-rcj5`) | Medium | 4.12.21 | IP Restriction middleware missed non-canonical IPv6, bypassing deny rules. |
| CVE-2026-44457 (`GHSA-p77w-8qqv-26rm`) | Medium | 4.12.18 | Cache middleware ignored `Vary: Authorization` / `Vary: Cookie`, serving one user's response to another. |
| CVE-2026-44459 (`GHSA-hm8q-7f3q-5f36`) | Low | 4.12.18 | `exp`/`nbf`/`iat` were skipped when non-finite or non-numeric — a token that never expires. |
| CVE-2026-44456 (`GHSA-9vqf-7f2p-gf9v`) | Medium | 4.12.16 | `bodyLimit()` did not enforce `maxSize` for chunked / unknown-length bodies; oversized requests reached handlers and returned 200. |
| CVE-2026-54286 (`GHSA-wwfh-h76j-fc44`) | Medium | 4.12.25 | `serve-static` path traversal on Windows via encoded backslash (`%5C`). |
| CVE-2026-54288 / -54289 / -54287 | Medium | 4.12.25 | AWS Lambda adapters: `bodyLimit` bypass via understated `Content-Length`, dropped repeated headers, merged `Set-Cookie`. |

This table is the set worth knowing by name, not the complete list — 4.12.34 also fixes a Language-middleware ReDoS (CVE-2026-71848) and a Proxy-helper hop-by-hop header leak (CVE-2026-71849), and there are ~40 advisories across the 4.11–4.12 line. Do not audit against these rows; audit against the version. Watch `github.com/honojs/hono/security/advisories` — Hono files these directly and they land in patch releases, not majors.

### CORS: enumerate the origins, always

`cors()` defaults are `origin: '*'` and `allowHeaders: []`. Never pair `credentials: true` with a wildcard or a reflecting function. Pass an explicit array, or a function that compares against an allow-list and returns `undefined` (not the input) on no match. Set `allowHeaders` explicitly — it closes CVE-2026-69207's default path and documents the contract. With more than one allowed origin, the response varies by request, so any shared cache in front needs `Vary: Origin`.

### JWT: what `jwt()` verifies, and what it does not

It verifies the signature and, by default, `exp` / `nbf` / `iat` **when those claims are present**. It does not verify:

- **`iss`** — the docs are explicit: "The `iss` claim will **not** be checked if this isn't set." Set `verification: { iss, aud }` or a token from any issuer sharing your key is accepted.
- **`aud`** — only checked if you configure it.
- **Revocation.** A stolen token stays valid until `exp`. If you need logout-now, check a deny-list.
- **Authorization.** `c.get('jwtPayload')` says *who*, never *what they may touch*.
- **`alg` confusion** — pin `alg` in the options. Never derive the algorithm from the token header.

With `cookie: '...'` the token now travels automatically on cross-site requests, so that refactor requires CSRF defence (below).

### Object-level authorization (IDOR/BOLA)

Hono has no controller or guard layer, so nothing forces an ownership check. Put the tenancy predicate in the query itself — `WHERE id = ? AND tenant_id = ?` — never a fetch-then-compare in JS, and never a filter applied after the rows are already in memory. Return **404, not 403**, when the caller does not own the row; 403 confirms it exists.

### CSRF

`csrf()` checks the `Origin` and `Sec-Fetch-Site` headers, and only for unsafe methods with form-ish content types (`application/x-www-form-urlencoded`, `multipart/form-data`, `text/plain`). It issues no token. The docs note it does not work where a proxy strips those headers — there, use a real token. A `Bearer` header needs no CSRF defence; a cookie does.

### Defaults that ship off or wide

| Setting | Default | Consequence |
|---|---|---|
| `secureHeaders()` | not registered | no CSP, no `X-Content-Type-Options`, no HSTS |
| `bodyLimit()` | not registered | unbounded body. `maxSize` is a **required** option with **no default** — there is no `bodyLimit()` you can call that picks a safe number for you |
| Bun `maxRequestBodySize` | 128 MiB | Bun rejects first — a larger Hono `maxSize` never runs its `onError` |
| rate limiting | none built in | add `hono-rate-limiter` with a **shared** store (§9), and key it off an address you actually trust |
| `cors()` `origin` | `'*'` | see above |
| `strict` | `true` | `/hello` and `/hello/` are different routes |
| `app.notFound` | top-level only | a sub-router's handler is ignored |

### The client address is not in the request

A shared store fixes the counter (§9); it does not fix the key. Hono is Web-standard, so `Request` carries no peer address — the address has to come from `getConnInfo()` (a per-runtime import: `hono/bun`, `hono/cloudflare-workers`, `@hono/node-server/conninfo`) or from a header your edge sets.

`c.req.header('x-forwarded-for')` is client-controlled. Anything keyed off it — rate limits, `ipRestriction()`, audit logs, geo rules — is bypassed by sending the header yourself, and a limiter keyed on a spoofable value is worse than none because it reports as working. Take a fixed hop counted from the **right** end of the chain, or use the address your platform authenticates (`CF-Connecting-IP` on Cloudflare), and write the trusted-proxy count down as config. Test with a forged header and a multi-hop chain.

### Injection and leakage

- SQL: parameterize. Bun's `sql` tagged template and `bun:sqlite` `.query(...).get(?)` bind properly; template-literal string building does not.
- Prompt injection: if this service proxies an LLM or serves MCP tools, retrieved documents, tool results, and webhook payloads are **data, not instructions**. Never concatenate them into a system prompt, and gate every model-callable tool with the same object-level check a user request would get.
- Leakage: `c.json(row)` serializes every column — build an explicit response object or a response schema. Never return `err.message` on 5xx. Do not log `c.req.url` wholesale; a token in the query string becomes permanent.
- `c.json()` **throws on `BigInt`**: `JSON.stringify cannot serialize BigInt`. This bites precisely when you followed the right advice — `bun:sqlite` with `safeIntegers: true` (the runtime tier's rule for money, IDs, and counters) returns `42n`, and handing that row straight to `c.json()` turns every read into a 500. Convert at the query boundary, or pass a replacer. Same for `Bun.SQL` `int8` columns.
- `hono/jsx` escapes interpolated values, but the raw-HTML escape hatches (`raw()` and the dangerous-inner-HTML prop) do not. The 2026 `cx()` and style-object advisories are what a wrong "already escaped" marker costs.

## References

- `references/runtime-portability.md` — the Bun / Workers / Deno / Node adapter matrix, what breaks on each, the `waitUntil` contract, and the checklist for keeping one Hono codebase deployable to more than one runtime.

## How this slots into the core pipeline

- **Gate 5 (Design):** name the deploy runtime(s) explicitly — the answer changes what state you may hold, what modules you may import, and whether background work needs `waitUntil`. State where rate-limit counters, idempotency keys, and caches actually live (never module scope). State the response shape `onError` produces and whether the RPC client is a coupled artifact.
- **Gate 6 (Implementation):** bodies read through `c.req.*`; every middleware `await`s `next()` and branches on `c.error` rather than try/catch; validators declared per route with at least one required field; auth registered before the routes it guards; `cors()` with an explicit origin list and `allowHeaders`; `secureHeaders()`, `bodyLimit()`, and a shared-store rate limiter registered; one `onError` that does not echo 5xx messages; streaming handlers pass the third `onError` argument and handle `onAbort`.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–9. The security-reviewer confirms `hono >= 4.12.34` in the lockfile (not just the range), that no `cors()` pairs credentials with a wildcard or reflection, that `jwt()` sets `iss`/`aud`/`alg`, that every ID-taking route has the tenancy predicate in its query, and that no rate limiter or cache lives in module scope.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Hono library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Java too (idempotency, invariants, gates, observability)? → **generic core** (`mir-backend`).
- True for every Bun backend regardless of framework (Bun's process and scheduler model, Bun-native APIs, `bun test`, the bundler, install scripts and lockfile trust)? → **runtime tier** (`mir-backend-bun`). Node-hosted specifics go to `mir-backend-node`.
- A mechanical footgun of Hono itself (`bodyCache` and `cloneRawRequest`, Context scope, the `next()` contract, `validator()`, `hc`/`AppType` inference, adapter imports, the streaming helpers, `onError`, the built-in middleware defaults)? → **here**.
- A different framework on Bun (Elysia) or on Node (Express, Fastify, NestJS) → its own module. Cloudflare platform primitives themselves (KV, Durable Objects, R2, Queues, `wrangler` config) → `mir-cloud`. Never widen this one.
