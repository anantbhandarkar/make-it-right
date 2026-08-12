---
name: mir-backend-node-fastify
description: "Make It Right (Fastify module). Fastify 5 + Node.js specific reliability augmentation. Use alongside mir-backend and mir-backend-node when the target stack is Fastify — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: schema-first validation and response serialization (and the data-leak risk of skipping the response schema), the fact that additionalProperties:false STRIPS rather than rejects under Fastify's default Ajv settings, the v5 full-JSON-schema requirement, server defaults that ship wide open (requestTimeout 0, connectionTimeout 0, maxParamLength 100, trustProxy false), the reply lifecycle and double-send traps, plugin encapsulation and decorator scoping, hook ordering for authentication, and the Content-Type validation-bypass CVE chain. TRIGGER only when the Node backend stack is Fastify used directly — building, reviewing, or debugging a Fastify route, plugin, hook, schema, or error handler. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-node (V8 event-loop / process-model concerns: blocking, worker_threads, unhandled rejections, backpressure, timeouts, npm supply chain); this module only adds Fastify library mechanics. SKIP for Express, Hapi, or Koa (each has its own mir-backend-node-<framework> module), SKIP for NestJS even when it runs on the Fastify adapter — that stack loads mir-backend-node-nestjs instead — and SKIP for non-Node runtimes."
trigger: /mir-backend-node-fastify
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-node-fastify · Make It Right (Fastify)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-node` (V8/Node event-loop model) → **this** (Fastify library mechanics). Run the gates first; load the Node runtime tier for event-loop and process-model concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (blocking the event loop, worker_threads, unhandled rejections, stream backpressure, timeouts, heap limits, npm supply chain) live in `mir-backend-node` — not here.**

**Stack assumed (npm registry, 13 Aug 2026):** `fastify@5.11.3` is `latest`. Fastify 4 reached end of life on 2025-06-30 and gets no security fixes — the `four` tag sits at `4.29.1`. A `6.0.0-alpha.0` exists on the `next` tag (published 2026-08-11); it is a pre-release, do not target it. Fastify 5 requires Node ≥ 20, but target Node 22/24 per the runtime tier — Node 20 is EOL.

**Minimum patched version is 5.8.5**, not just "5.x" — see the CVE chain in Security.

## The Fastify footguns AI walks into most

### 1. Skipping the response schema leaks internal fields

Fastify's JSON serialization is driven by `fast-json-stringify` using the JSON Schema on the route. The response schema does two things: it makes serialization faster than `JSON.stringify`, **and it strips any field not declared in the schema**. With no response schema, Fastify applies no serialization filter at all and the full internal object goes to the client:

```js
// WRONG — no response schema; the entire DB row (passwordHash, internalFlags) goes out
fastify.get('/users/:id', async (request, reply) => {
  return db.findUser(request.params.id);
});

// RIGHT — the response schema is the serialization and the data-exposure control
fastify.get('/users/:id', {
  schema: {
    params: {
      type: 'object',
      properties: { id: { type: 'string', format: 'uuid' } },
      required: ['id'],
    },
    response: {
      200: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          email: { type: 'string' },
          name: { type: 'string' },
          // passwordHash, internalFlags are NOT here — fast-json-stringify drops them
        },
        required: ['id', 'email'],
        additionalProperties: false,
      },
    },
  },
}, async (request, reply) => {
  return db.findUser(request.params.id);
});
```

Define a response schema for every route, including error responses — a `500` shape without a schema will happily serialize whatever your error object carries.

### 2. `additionalProperties: false` STRIPS extra fields — it does not reject them

This is the correction most Fastify code needs. Fastify's default Ajv options are `coerceTypes: 'array'`, `useDefaults: true`, **`removeAdditional: true`**, `allErrors: false`. With `removeAdditional: true`, a schema that sets `additionalProperties: false` causes Ajv to **delete** the unexpected keys and continue — the request succeeds with a 200, and nothing tells you the client sent `isInternal: true`.

That is safe against mass assignment (the field never reaches your handler) but it hides a misbehaving or hostile client, and it means "we return 400 on unknown fields" is false unless you changed the Ajv options:

```js
// Reject instead of strip — 400 on any undeclared property
const fastify = Fastify({
  ajv: { customOptions: { removeAdditional: false } },
});
```

Validation itself is also opt-in per section: Fastify validates `body`, `params`, `querystring`, and `headers` **only if** you declare a schema for that section. No body schema means `request.body` is the raw parsed payload — no required-field enforcement, no type coercion, nothing.

```js
// WRONG — body is unchecked; any field reaches the handler
fastify.post('/orders', async (request, reply) => {
  await db.createOrder(request.body); // userId, discount, isInternal could be spoofed
});

// RIGHT — schema gates the body; identity comes from auth, never from the payload
fastify.post('/orders', {
  schema: {
    body: {
      type: 'object',
      properties: {
        productId: { type: 'string', format: 'uuid' },
        quantity: { type: 'integer', minimum: 1, maximum: 100 },
      },
      required: ['productId', 'quantity'],
      additionalProperties: false,
    },
  },
}, async (request, reply) => {
  await db.createOrder({ ...request.body, userId: request.user.id });
});
```

Two more Ajv defaults worth knowing because they change values silently: `coerceTypes: 'array'` turns `"5"` into `5` and wraps a scalar into a single-element array when the schema says `array`; `useDefaults: true` injects schema `default` values into `request.body`, so a field you never sent can arrive populated.

### 3. Fastify 5 requires full JSON schemas — shorthand was removed

The `jsonShorthand` option is gone. Every schema section needs a complete JSON Schema including `type`:

```js
// Fastify 4 shorthand — throws at route registration in Fastify 5
schema: { querystring: { name: { type: 'string' } } }

// Fastify 5
schema: {
  querystring: {
    type: 'object',
    properties: { name: { type: 'string' } },
    required: ['name'],
    additionalProperties: false,
  },
}
```

The failure is at boot, not at request time, so it is cheap to catch — but AI generates the shorthand form constantly because Fastify 4 examples dominate. The same change makes it easier to swap the validator compiler for Zod or TypeBox if you prefer TypeScript-first schemas.

### 4. The double-send trap — return the payload OR call `reply.send`, never both

Fastify routes can either `return` the payload (Fastify sends it) or call `reply.send(payload)`. Doing both in an async handler sends the response twice and Fastify raises `FST_ERR_REP_ALREADY_SENT`:

```js
// WRONG — sends twice; FST_ERR_REP_ALREADY_SENT
fastify.get('/ping', async (request, reply) => {
  reply.send({ pong: true });
  return { pong: true };
});

// RIGHT (return style — preferred for async handlers)
fastify.get('/ping', async (request, reply) => {
  return { pong: true };
});

// RIGHT (reply.send style — for callbacks, streams, conditional flows)
fastify.get('/ping', (request, reply) => {
  reply.send({ pong: true });
});
```

The subtle version: in an async handler that calls `reply.send()` inside a branch and then falls through, the implicit `return undefined` is treated as a payload. When you send manually from an async handler, `return reply` to tell Fastify you have taken over.

### 5. Plugin encapsulation — decorators and hooks are scoped, not global

Without `fastify-plugin` (`fp`), decorators, hooks, and `request`/`reply` extensions registered in a child plugin are **not visible** to sibling plugins or to the parent. AI code that decorates in one plugin and uses it in another without `fp` fails at runtime with `FST_ERR_DEC_MISSING_DEPENDENCY`:

```js
// WRONG — the db decorator is encapsulated; siblings can't see it
fastify.register(async function dbPlugin(fastify) {
  fastify.decorate('db', createDbClient());
});

// RIGHT — fastify-plugin breaks encapsulation for shared infrastructure
import fp from 'fastify-plugin';
const dbPlugin = fp(async function (fastify) {
  fastify.decorate('db', createDbClient());
});
fastify.register(dbPlugin);
fastify.register(routePlugin); // can now use fastify.db
```

Rule: **shared infrastructure (DB, Redis, config, auth decorators) → wrap with `fp`.** Route groups that should be isolated (own prefix, own hooks) → do NOT use `fp`. Encapsulation is also the reason a `preHandler` hook added inside one plugin does not protect routes registered in another — a security-relevant scoping mistake, not just an ergonomic one.

### 6. Hook ordering — `onRequest` for auth, `setErrorHandler` for errors

Hooks run in this order per request:

```
onRequest → preParsing → preValidation → preHandler → handler → preSerialization → onSend → onResponse
```

- **Authentication**: `onRequest` (before any body parsing — fail fast and cheap).
- **Webhook HMAC signatures are not a `preHandler` job.** Stripe, GitHub, Slack and friends sign the **exact bytes** they sent. `request.body` is a parsed object; re-serialising it changes key order, whitespace, unicode escaping, and duplicate-key handling, so the digest will not match — and if you make it match by normalising, you are authenticating bytes the provider never signed. Capture the raw buffer with `addContentTypeParser('application/json', { parseAs: 'buffer' }, …)` (or `@fastify/raw-body` on that route), compare with `crypto.timingSafeEqual`, and parse only after the signature verifies.
- **Route-level hooks** go in the route options (`preHandler: [...]`). Do not register a global `preHandler` when you only want auth on some routes.
- **Errors**: `setErrorHandler` is the single place for consistent mapping. Express-style `(err, req, res, next)` does not exist. Handlers registered inside an encapsulated plugin apply only to that scope.

```js
fastify.addHook('onRequest', async (request, reply) => {
  try {
    await request.jwtVerify();
  } catch (err) {
    return reply.code(401).send({ error: 'Unauthorized' }); // return, or the handler still runs
  }
});

fastify.setErrorHandler(async (error, request, reply) => {
  if (error.validation) {
    return reply.code(400).send({ error: 'Validation failed', details: error.validation });
  }
  request.log.error({ err: error }, 'unhandled');
  const status = error.statusCode ?? 500;
  // Do NOT echo error.message on 5xx — driver errors carry SQL and file paths
  return reply.code(status).send({
    error: status < 500 ? error.message : 'Internal error',
    requestId: request.id,
  });
});
```

The missing `return` in a hook is a real defect: `reply.send()` without returning lets the lifecycle continue to the handler in some flows, which is how "the 401 fired but the row was still updated" happens.

### 7. Server defaults that are wider than you expect

Verified against the Fastify server reference:

| Option | Default | What it means in production |
|---|---|---|
| `requestTimeout` | `0` | **no request timeout** — a slow client can hold a connection indefinitely |
| `connectionTimeout` | `0` | no socket timeout |
| `keepAliveTimeout` | `72000` ms | must exceed the idle timeout of any load balancer in front, or you get random 502s |
| `bodyLimit` | `1048576` (1 MiB) | applies per route unless overridden; raising it is a memory decision |
| `maxParamLength` | `100` | a route param longer than 100 chars **404s** — bites when a token or a long slug is in the path |
| `trustProxy` | `false` | `request.ip` is the proxy until you set this |
| `caseSensitive` | `true` | `/Users` and `/users` are different routes |
| `ignoreTrailingSlash` | `false` | `/users` and `/users/` are different routes |
| `onProtoPoisoning` / `onConstructorPoisoning` | `'error'` | good default — but only for the built-in JSON parser (see Security) |

Also changed in Fastify 5, and each is a startup or runtime failure rather than a warning: the `logger` option no longer accepts a logger instance (use `loggerInstance`); semicolon query delimiters are off (`useSemicolonDelimiter: true` to restore); variadic `listen(port, host)` was removed in favour of `listen({ port, host })`; `request.connection` was removed (use `request.socket`); a `DELETE` with `Content-Type: application/json` and an empty body is now rejected; `hasRoute()` matches only the exact registered string.

## Security

Fastify-specific mechanics. Runtime-level items (SSRF, command injection, prototype pollution in your own merges, path traversal helpers, npm supply chain, secrets in logs) live in `mir-backend-node`.

### Current advisory class: Content-Type validation bypass, three CVEs in one chain

If a route declares per-content-type validation via `schema.body.content`, a crafted `Content-Type` header makes Fastify parse the body normally while skipping validation entirely. Same root cause each time — the parser and the validator normalise the header differently:

| CVE | Affected | Fixed in | Bypass |
|---|---|---|---|
| CVE-2025-32442 | 5.0.0–5.3.0, 4.29.0 | 5.3.2 (5.3.1 was incomplete), 4.29.1 | altered casing or whitespace before `;` |
| CVE-2026-25223 | < 5.7.2 | 5.7.2 | trailing tab (`\t`) plus arbitrary content |
| CVE-2026-33806 (HIGH, CVSS 7.5) | 5.3.2–5.8.4 | **5.8.5** | a single leading space (`\x20`) before the media type |

Actions: pin `fastify` ≥ **5.8.5**. Prefer one `schema.body` over a `schema.body.content` map — the bypass only exists on the per-content-type path, so avoiding it is also the documented workaround. If a route genuinely needs different shapes per content type, **reject** anything that is not an exact expected media type rather than repairing it in an `onRequest` hook; hand-rolled normalisation is the same class of parser/validator disagreement that produced all three CVEs, written by you instead of by the framework.

Fastify's security team assigns CVEs through the OpenJS CNA; watch the `fastify/fastify` advisory feed rather than waiting for a dependency scanner.

### Plugin advisories to check

- `@fastify/multipart` CVE-2025-24033 (HIGH): `saveRequestFiles` left temporary files on disk when the client cancelled the request — unbounded disk growth. Affects `< 8.3.1` and `9.0.0–9.0.2`; fixed in 8.3.1 / 9.0.3.
- Multipart is off by default. When you add it, set `limits: { fileSize, files, fields, parts }` explicitly; the plugin's historical DoS advisories were all "no limit on part count".

### The custom content-type parser silently removes the prototype-poisoning guard

Fastify's built-in JSON parser uses `secure-json-parse` with `onProtoPoisoning: 'error'`. A hand-rolled parser drops that protection:

```js
// WRONG — plain JSON.parse; __proto__ and constructor keys now survive
fastify.addContentTypeParser('application/vnd.api+json', { parseAs: 'string' },
  (req, body, done) => done(null, JSON.parse(body)));

// RIGHT — keep the guard
import sjson from 'secure-json-parse';
fastify.addContentTypeParser('application/vnd.api+json', { parseAs: 'string' },
  (req, body, done) => {
    try { done(null, sjson.parse(body, { protoAction: 'error' })); }
    catch (err) { err.statusCode = 400; done(err); }
  });
```

### `attachValidation: true` turns off the automatic 400

With `attachValidation`, Fastify stops returning 400 on validation failure and instead sets `request.validationError`. If the handler does not check it, **unvalidated data reaches your business logic and the request succeeds**. Only use it when you need custom error shaping, and check the property on the first line of the handler.

### CORS, headers, and rate limiting are all plugins, all off by default

- `@fastify/cors` with no options allows **any** origin. As with Express, the exploitable configuration is reflecting the request origin together with `credentials: true`. Pass an explicit array, or a function that compares against an allow-list and calls back with `false` otherwise.
- `@fastify/helmet` for security headers — nothing is set without it.
- `@fastify/rate-limit` for rate limiting — nothing is limited without it. Register it as a root-level plugin (with `fp` semantics) or it only covers the encapsulated scope it was registered in.
- `trustProxy: false` means `request.ip` is your load balancer. Setting `trustProxy: true` trusts a client-written `X-Forwarded-For`, which lets anyone spoof the IP used for rate limiting and audit logs. Pass a hop count, a subnet, or a function.

### Timeouts are a DoS control, not a tuning knob

`requestTimeout: 0` and `connectionTimeout: 0` mean a slow-loris client can hold connections until file descriptors run out. Set both to real values (single-digit seconds for an API), keep `bodyLimit` at or near the 1 MiB default, and override it per route only where a large body is genuinely expected.

### Static files and path traversal

`@fastify/static` serves everything under `root`. Set `root` to a dedicated directory, leave `wildcard` at its default rather than hand-rolling a `:path` route, and never build a file path by concatenating `request.params`. If you must serve by name, resolve and verify the prefix as shown in the Node tier. Remember `maxParamLength: 100` will 404 long path params before your code sees them, which can mask a traversal attempt in testing.

### Object-level authorization

Fastify has no controller or guard structure, so nothing forces an ownership check. A `onRequest` JWT hook proves *who*; it never proves *what*. Every route that takes an ID from the client needs the tenancy predicate in the query itself (`WHERE id = $1 AND tenant_id = $2`), and should return 404 rather than 403 so the response does not confirm the resource exists.

### Serialization is the outbound control

Restating §1 in security terms: the response schema is what prevents `passwordHash`, `stripeCustomerId`, and internal flags from reaching a client. A route without one has no outbound filter. `additionalProperties: false` on the response schema makes the omission explicit and survives schema drift when someone adds a column.

## How this slots into the core pipeline

- **Gate 5 (Design):** define full JSON Schemas for every route (body, params, querystring, response, and error responses) before writing handlers. Decide whether unknown properties strip or reject, and say which. A missing response schema is a data-exposure defect — flag it.
- **Gate 6 (Implementation):** async handlers return the payload; hooks `return` their replies; shared infrastructure wrapped with `fp`; auth in `onRequest`; one `setErrorHandler` that does not echo 5xx messages; `requestTimeout`/`connectionTimeout` set; CORS allow-list, helmet, and rate-limit plugins registered at the root.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–7; the security-reviewer confirms `fastify` ≥ 5.8.5, that no route uses `schema.body.content` without a normalising hook, that every route has a response schema, and that no custom content-type parser calls bare `JSON.parse`.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Fastify library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Java too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Node framework (blocking the event loop, unhandled rejections, backpressure, heap limits, timeouts, SSRF, command injection, npm supply chain)? → **runtime tier** (`mir-backend-node`).
- A mechanical footgun of Fastify itself (schema-driven serialization, Ajv defaults, response-schema data leaks, double-send, plugin encapsulation, hook lifecycle, `setErrorHandler`, server-option defaults)? → **here**.
- A different Node framework (Express, NestJS) → its own `mir-backend-node-<framework>` module. A different runtime → its own tier. Never widen this one.
