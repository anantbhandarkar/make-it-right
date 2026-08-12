---
name: mir-backend-node-express
description: "Make It Right (Express module). Express 5 (now the npm default) + Express 4 maintenance-line reliability augmentation. Use alongside mir-backend and mir-backend-node when the target stack is Express — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: what Express 5 does and does not auto-catch for async handlers, the path-to-regexp route-syntax break that makes `app.get('*')` throw at boot, req.body being undefined rather than {}, the simple-vs-extended query parser change, middleware ordering as a hard contract, error-handler arity, the absence of built-in validation and what fills the gap, CORS/helmet/rate-limit being off by default, trust-proxy spoofing, and object-level authorization gaps that structural frameworks catch but Express doesn't. TRIGGER only when the Node backend stack is Express used directly — building, reviewing, or debugging an Express route, middleware, or error handler. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-node (V8 event-loop / process-model concerns: blocking, worker_threads, unhandled rejections, backpressure, timeouts, npm supply chain); this module only adds Express library mechanics. SKIP for Fastify, Hapi, or Koa (each has its own mir-backend-node-<framework> module), SKIP for NestJS even though NestJS runs on Express by default — that stack loads mir-backend-node-nestjs instead — and SKIP for non-Node runtimes."
trigger: /mir-backend-node-express
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-node-express · Make It Right (Express)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-node` (V8/Node event-loop model) → **this** (Express library mechanics). Run the gates first; load the Node runtime tier for event-loop and process-model concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (blocking the event loop, worker_threads, unhandled rejections, stream backpressure, timeouts, heap limits, npm supply chain) live in `mir-backend-node` — not here.**

**Stack assumed (npm registry, 13 Aug 2026):** `express@5.2.1` is the `latest` tag; the 4.x maintenance line is `latest-4` = `4.22.2`. Express 5 requires Node ≥ 18 by its own `engines` field, but target Node 22/24 per the runtime tier — Node 20 is EOL. Do not pin `5.2.0`: it shipped an unintended breaking change to extended query parsing and was superseded by `5.2.1` the same day.

**If you are writing new Express code, write Express 5.** `npm install express` has resolved to 5.x since 5.1.0 took the `latest` tag on 2025-03-31. Most training data — and most AI output — is still Express 4, which is where the next four items come from.

## The Express footguns AI walks into most

### 1. Express 5 auto-forwards rejected promises — Express 4 does not, and neither catches callbacks

**Express 5** awaits the promise returned by a route handler **or by any middleware**, and forwards a rejection to the error-handling middleware. That "or any middleware" matters: async auth and validation middleware get the same treatment, which is the reason the `asyncHandler` wrapper and the `express-async-errors` shim are both unnecessary on 5:

```js
// CORRECT in Express 5 — a throw reaches the (err, req, res, next) handler
app.get('/users/:id', async (req, res) => {
  const user = await db.findUser(req.params.id);
  res.json(user);
});
```

**Express 4** does not await the return value. The same code produces an unhandled rejection (which kills the process, see the Node tier) and the request hangs until the client times out. On 4.x, wrap or catch explicitly:

```js
// Express 4 only — forward rejections to next
const asyncHandler = fn => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
app.get('/users/:id', asyncHandler(async (req, res) => { /* ... */ }));
```

**The part Express 5 does NOT fix**, and the reason "we're on 5, we're fine" is wrong in review: only the *returned* promise is awaited. Everything else escapes the handler and lands at the process level — as two different failures that need two different handlers:

| What escapes | Where it lands |
|---|---|
| a promise you started but did not `return` or `await` | `unhandledRejection` → process exits (Node default) |
| a throw inside a `setTimeout`/`setInterval` or a plain callback | `uncaughtException` |
| an `EventEmitter` `'error'` event with no listener | throws → `uncaughtException` |
| a stream error with no `'error'` handler and no `pipeline()` | same |

Wiring only `process.on('unhandledRejection')` and calling it done leaves the bottom three unhandled. See the Node tier §3 and §7.

```js
// STILL BROKEN on Express 5 — nothing awaits this, the error escapes the handler
app.post('/orders', async (req, res) => {
  auditLog.write(req.body);          // returns a promise, not returned or awaited
  stream.on('data', () => JSON.parse(chunk)); // throws inside a callback → process-level
  res.json({ ok: true });
});
```

State the Express major in the design. If it is 4, every async handler needs a wrapper and the plan needs a migration note.

### 2. Express 5 changed the route syntax — `app.get('*')` throws at startup

Express 5 upgraded to `path-to-regexp` 8, which removed sub-expression regex patterns and requires wildcards to be named. AI writes Express 4 route strings constantly, and the failure is a boot-time `TypeError` about a missing parameter name, or a silent 404 — not a helpful message:

| Express 4 | Express 5 | Notes |
|---|---|---|
| `app.get('*', h)` | `app.get('/*splat', h)` | wildcard must be named |
| `'/files/*'` matching `/files` too | `'/files/{*splat}'` | braces make the group optional |
| `'/:file.:ext?'` | `'/:file{.:ext}'` | `?` optional marker removed |
| `'/:id(\\d+)'` | validate `req.params.id` in the handler | inline regex removed for ReDoS reasons |
| `req.params[0]` for a wildcard | `req.params.splat` — an **array** of segments | shape change, not just a name change |

This applies everywhere a path string is accepted: `app.use`, `router.use`, and any path-based middleware mount.

### 3. Express 5 also changed three defaults that produce silent wrong behavior

| Change | Express 4 | Express 5 | The failure |
|---|---|---|---|
| `req.body` with no body parser | `{}` | **`undefined`** | `req.body.email` throws `TypeError: Cannot read properties of undefined` instead of being falsy |
| default `query parser` | `extended` | **`simple`** | `?filter[status]=open` yields the string key `'filter[status]'`, not a nested object; filters silently stop working |
| `res.status(n)` | anything accepted | must be an integer 100–999 | `res.status(err.code)` with a driver error code now **throws** |

Also removed in 5: `app.del()` (use `app.delete()`), `req.param(name)`, `res.sendfile()`, `res.send(body, status)` / `res.json(obj, status)` (use `res.status(s).json(o)`), `res.redirect(url, status)` (argument order is now `(status, url)`), and `res.redirect('back')`. `npx codemod@latest @expressjs/v5-migration-recipe` applies the mechanical ones.

### 4. Middleware order is the contract — get it wrong and nothing else matters

Express resolves middleware strictly in registration order. The order is not a style choice; it is the execution contract:

```
body parser
  └── request-id / correlation-id logger
        └── rate limiter
              └── authentication
                    └── authorization (object-level, per-router)
                          └── route handlers
                                └── 404 handler
                                      └── error handler  ← MUST be last, MUST have 4 args
```

Common ordering defects AI introduces:
- **Body parser after routes** — `req.body` is `undefined` in the handler (and in Express 5 that is a `TypeError`, not an empty object).
- **Auth middleware missing from a router** — a route added later slips in unauthenticated.
- **Error handler not registered last** — it never runs.
- **Error handler with 3 args** (`(req, res, next)`) — Express identifies error handlers by arity; 3-arg handlers are treated as regular middleware and skipped for errors.

```js
// WRONG — error handler silently skipped (3 args)
app.use((err, res, next) => { res.status(500).json({ error: err.message }); });

// RIGHT — must have exactly 4 args
app.use((err, req, res, next) => { res.status(500).json({ error: 'Internal error' }); });
```

### 5. No built-in validation — without it, mass assignment and injection slip in

Express puts raw, unvalidated input on `req.body` / `req.query` / `req.params`. AI code that passes these straight to a database or service layer is vulnerable to:
- **Mass assignment / overposting** — a client sends `{ "isAdmin": true }` alongside a legitimate payload; if you spread `req.body` into an ORM object, you have just promoted them.
- **Type coercion surprises** — query strings are always strings; passing `req.query.limit` to a DB call that expects a number without parsing is a latent bug. Express 5's `simple` query parser makes this worse, because nested keys arrive as flat strings.

Validate at the route boundary with `zod` (latest `4.4.3`) or `joi`. Define a schema, parse, and use only the parsed output:

```js
import { z } from 'zod';

// z.strictObject REJECTS unknown keys. Plain z.object() STRIPS them silently —
// safe against mass assignment, but you get no signal that a client is sending
// isAdmin. Prefer strictObject so overposting shows up as a 400.
const CreateUserSchema = z.strictObject({
  email: z.email(),                    // Zod 4 top-level format; z.string().email() is legacy
  name: z.string().min(1).max(100),
  // id, isAdmin, role are NOT here — they're not accepted from the client
});

app.post('/users', async (req, res) => {
  const body = CreateUserSchema.parse(req.body); // throws ZodError on invalid input
  const user = await db.createUser(body);        // only safe fields reach the DB
  res.status(201).json(user);
});
```

Catch `ZodError` in the error handler and return a 400 with field-level detail. Never pass `req.body` to an ORM model constructor without schema-gating it first. Express has no response-side equivalent of Fastify's response schema, so also choose the fields you send back explicitly — do not `res.json(userRow)`.

### 6. Object-level authorization is easy to miss — no structure enforces it

Express has no built-in guard or controller structure. Authentication (who are you?) is easy to add as a single middleware. **Authorization (may *this user* act on *this specific resource*?)** is easy to forget because there is no framework hook that forces it at the route level:

```js
// WRONG — checks auth but not ownership; any authenticated user reads any user's data
app.get('/accounts/:id', requireAuth, async (req, res) => {
  const account = await db.findAccount(req.params.id);
  res.json(account); // IDOR: account might belong to a different user
});

// RIGHT — load the resource, then assert ownership before returning
app.get('/accounts/:id', requireAuth, async (req, res) => {
  const account = await db.findAccount(req.params.id);
  if (!account || account.userId !== req.user.id) {
    return res.status(404).json({ error: 'Not found' });
  }
  res.json(account);
});
```

Return 404 (not 403) when the resource does not belong to the requester — 403 confirms the resource exists. Enforce object-level authorization at every endpoint that loads an entity by a client-supplied ID, including PATCH/DELETE, not just GET.

## Security

Express-specific mechanics. Runtime-level items (SSRF, command injection, prototype pollution, path traversal, npm supply chain, secrets in logs) live in `mir-backend-node`.

### Current advisory class: `path-to-regexp` ReDoS, and it reaches you transitively

Express 5 pulls `path-to-regexp` through `router@2.2.0`, declared as `^8.0.0`. Two ReDoS CVEs published 2026-03-27 affect **8.0.0 up to but not including 8.4.0**, fixed in **8.4.0** (registry latest is `8.4.2`):

| CVE | Trigger |
|---|---|
| CVE-2026-4923 | multiple wildcards plus at least one parameter, where the second wildcard is not at the end — e.g. `/*foo-*bar-:baz`, `/x/*a-:b/*c/y`. `/*foo-:bar` is safe. |
| CVE-2026-4926 | multiple sequential optional groups in brace syntax — e.g. `{a}{b}{c}:z`. Generated regex grows exponentially with group count. |

A caret range means a fresh install picks up the fix; a lockfile committed before March 2026 does not. Check with `npm ls path-to-regexp`, not by reading `package.json`. Then audit your own route strings for the two shapes above — the fix removes the pathological regex generation, but multi-wildcard routes are worth simplifying anyway. Express's own security-updates page still only documents 4.x entries, so do not treat "no Express advisory" as "no risk"; Express's exposure mostly arrives through `path-to-regexp`, `send`, `serve-static`, `qs`, `cookie`, and `body-parser`.

### Nothing secure is on by default

Express ships with no security headers, no CORS policy, and no rate limiting. Registered versions verified on the registry today: `helmet@8.3.0`, `cors@2.8.6`, `express-rate-limit@8.6.2`.

```js
import helmet from 'helmet';
import cors from 'cors';
import rateLimit from 'express-rate-limit';

app.use(helmet());                          // CSP, HSTS, X-Frame-Options, nosniff
app.use(cors({
  origin: ['https://app.example.com'],      // explicit allow-list
  credentials: true,
}));
app.use('/api/', rateLimit({ windowMs: 60_000, limit: 100 }));
```

### CORS: the two configurations that hand your API to any site

`cors()` with no options sets `Access-Control-Allow-Origin: *` and no credentials. That is permissive but not exploitable for authenticated requests, because browsers refuse `*` together with credentials. The two dangerous forms:

```js
// WRONG — reflects whatever Origin the caller sent, with cookies attached.
// evil.example can now make credentialed requests as the logged-in user.
app.use(cors({ origin: true, credentials: true }));

// WRONG — same defect written by hand
res.setHeader('Access-Control-Allow-Origin', req.headers.origin);
res.setHeader('Access-Control-Allow-Credentials', 'true');
```

Use a static array, or a function that matches against an allow-list and returns `false` otherwise. Never build the allowed origin from a substring check (`origin.endsWith('example.com')` matches `notexample.com`).

### `trust proxy` — the setting that silently disables rate limiting

Behind a load balancer, `req.ip` is the proxy unless you set `trust proxy`. But `app.set('trust proxy', true)` trusts the **entire** `X-Forwarded-For` chain and takes the leftmost entry, which the client writes. Every request can then claim a fresh IP and get a fresh rate-limit bucket. `express-rate-limit` treats this as a configuration error and raises `ERR_ERL_PERMISSIVE_TRUST_PROXY`; the mirror-image error when the setting is left at the default `false` behind a proxy is `ERR_ERL_UNEXPECTED_X_FORWARDED_FOR`.

```js
app.set('trust proxy', 1);              // number of proxies in front of you
// or restrict to the known load balancer subnet
app.set('trust proxy', '10.0.0.0/8');
```

Both obvious values (`true` and `false`) are wrong behind a proxy. The same spoofed IP flows into audit logs and any IP-based allow-list.

### Body limits and parser defaults

`body-parser@2.3.0` defaults, which Express re-exports as `express.json()` / `express.urlencoded()`:

| Option | Default | Consequence |
|---|---|---|
| `limit` | `'100kb'` | fine for JSON APIs; a route that accepts documents needs an explicit, bounded value — not `Infinity` |
| `strict` (json) | `true` | only objects and arrays accepted; keep it |
| `extended` (urlencoded) | `false` in 2.x | `qs` deep parsing is off by default now; turning it on re-enables deeply nested keys |
| `parameterLimit` | `1000` | raising it is a CPU-cost decision |

`express.json()` has **no prototype-poisoning guard** — unlike Fastify, which defaults to `onProtoPoisoning: 'error'`. A `__proto__` key survives `JSON.parse` as an own property and pollutes as soon as anything merges it. Run with `--disable-proto=throw` and never recursively merge `req.body` (see the Node tier).

### Error responses leak stack traces unless NODE_ENV is production

Express's built-in final error handler includes `err.stack` in the response body when `NODE_ENV` is not `'production'`. A staging box with the default env, or a container where the variable was never set, returns file paths and internal structure to anyone who can trigger a 500. Do not rely on the environment variable — register your own error handler that logs the full error and returns a fixed body:

```js
app.use((err, req, res, next) => {
  req.log.error({ err }, 'unhandled');           // full detail to logs only
  if (res.headersSent) return next(err);         // MUST delegate — a second send corrupts the response
  if (err instanceof ZodError) return res.status(400).json({ error: 'Validation failed', details: err.issues });
  res.status(err.status ?? 500).json({ error: 'Internal error', requestId: req.id });
});
```

The `res.headersSent` check is not optional. If the error came from a stream that already wrote a 200 and part of a body — file downloads, SSE, any `res.write()` — calling `res.status().json()` throws `ERR_HTTP_HEADERS_SENT` inside the error handler itself. Express's docs require delegating to the default handler in that case; it closes the connection.

Never put `err.message` in a 5xx body — driver errors carry SQL text and sometimes parameter values.

### Sessions, cookies, CSRF

- `express-session` sets `httpOnly: true` by default but **`secure: false`** and no `sameSite`. Set `cookie: { secure: true, httpOnly: true, sameSite: 'lax' }` and set `trust proxy` so the secure cookie is not dropped behind TLS termination. The default `MemoryStore` is explicitly not for production — it leaks memory and loses sessions on restart.
- CSRF applies to **cookie-based** auth, not to `Authorization: Bearer` headers. If the browser attaches the credential automatically, you need a token. `SameSite=Lax` blocks the classic cross-site form POST but not same-site subdomain attacks, so it is a mitigation, not the control.
- The `csurf` package has been deprecated and unmaintained since 2022. Use a maintained double-submit implementation, and verify the `Origin`/`Sec-Fetch-Site` headers on state-changing requests as a second check.

### Static files and uploads

- `res.sendFile(p)` throws unless `p` is absolute or you pass `{ root }`. Always pass `root` and let Express reject the traversal — do not build the path yourself with `path.join`.
- `express.static` serves whatever is under the directory you point at. Never point it at the repository root or at an uploads directory that also holds anything executable, and set `dotfiles: 'ignore'` explicitly rather than assuming the default.
- `multer@2.2.0` writes uploads with the client's filename if you use `diskStorage` naively. Generate the stored name yourself, cap `limits.fileSize` and `limits.files`, and validate the content type from the bytes, not the header.

### Object-level authorization is the item reviewers miss

Repeat of §6 because it is the most common shipped defect: a valid token proves *who*, never *what*. Every route that takes an ID from the client needs an ownership or tenancy predicate in the query itself (`WHERE id = $1 AND tenant_id = $2`), not a check bolted on after the row is loaded — a check-after-load is easy to forget on the update path even when it exists on the read path.

## How this slots into the core pipeline

- **Gate 5 (Design):** state Express version (4 vs 5), middleware order, validation library, CORS allow-list, and auth strategy. On 4.x, a missing `asyncHandler` wrapper is a design defect that surfaces as hung requests. On 5.x, list which handlers do fire-and-forget work — those are still unguarded.
- **Gate 6 (Implementation):** route strings use Express 5 syntax; validation schema defined before the handler body; helmet + CORS allow-list + rate limiter registered before routes; `trust proxy` set to a hop count or subnet; own error handler registered last with 4 args; object-level authorization at every entity-loading route.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–6; the security-reviewer checks the Security section and specifically runs `npm ls path-to-regexp` and greps for `origin: true` alongside `credentials: true`.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Express library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Java too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Node framework (blocking the event loop, unhandled rejections, backpressure, heap limits, timeouts, SSRF, command injection, npm supply chain)? → **runtime tier** (`mir-backend-node`).
- A mechanical footgun of Express itself (async error propagation, route syntax, middleware arity, parser defaults, `trust proxy`, CORS wiring, IDOR from missing authorization)? → **here**.
- A different Node framework (Fastify, NestJS) → its own `mir-backend-node-<framework>` module. A different runtime → its own tier. Never widen this one.
