---
name: mir-backend-node-fastify
description: "Make It Right (Fastify module). Fastify 4/5 + Node.js specific reliability augmentation. Use alongside mir-backend and mir-backend-node when the target stack is Fastify — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: schema-first validation and response serialization (and the data-leak risk of skipping it), the reply lifecycle and double-send traps, plugin encapsulation and decorator scoping, and hook-ordering for authentication. TRIGGER only when the Node backend stack is Fastify — building, reviewing, or debugging a Fastify route, plugin, hook, or schema. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-node (V8 event-loop / process-model concerns: blocking, worker_threads, unhandled rejections, backpressure, timeouts); this module only adds Fastify library mechanics. SKIP for Express, NestJS, Hapi, Koa, or any non-Fastify stack (those get their own mir-backend-node-<framework> module), and for non-Node runtimes."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-node` (V8/Node event-loop model) → **this** (Fastify library mechanics). Run the gates first; load the Node runtime tier for event-loop and process-model concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (blocking the event loop, worker_threads, unhandled rejections, stream backpressure, AbortController timeouts, heap limits) live in `mir-backend-node` — not here.**

**Stack assumed:** Fastify 4.x (or 5.x; breaking changes called out) on Node 20+ LTS.

## The Fastify footguns AI walks into most

### 1. Skipping the response schema leaks internal fields

Fastify's JSON serialization is powered by `fast-json-stringify`, driven by the JSON Schema on the route. The response schema does two things: it makes serialization 2–5× faster than `JSON.stringify`, **and it strips any field not declared in the schema**. Omitting it hands the full internal object to the client:

```js
// WRONG — no response schema; entire DB row (including passwordHash, internalFlags) goes to client
fastify.get('/users/:id', async (request, reply) => {
  return db.findUser(request.params.id);
});

// RIGHT — response schema is the serialization + security boundary
fastify.get('/users/:id', {
  schema: {
    params: { type: 'object', properties: { id: { type: 'string', format: 'uuid' } }, required: ['id'] },
    response: {
      200: {
        type: 'object',
        properties: {
          id: { type: 'string' },
          email: { type: 'string' },
          name: { type: 'string' },
          // passwordHash, internalFlags are NOT here — fast-json-stringify silently drops them
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

Define a response schema for every route. `additionalProperties: false` makes the stripping explicit and protects against schema drift.

### 2. Request body and params schemas — validation is also off by default without them

Fastify validates `body`, `params`, `querystring`, and `headers` if and only if you declare a schema for them. Without a body schema, `request.body` is the raw parsed payload — no type coercion, no required-field enforcement, no protection against mass assignment:

```js
// WRONG — body is unchecked; any field reaches the handler
fastify.post('/orders', async (request, reply) => {
  await db.createOrder(request.body); // userId, discount, isInternal could be spoofed
});

// RIGHT — schema rejects invalid bodies with a 400 before the handler runs
fastify.post('/orders', {
  schema: {
    body: {
      type: 'object',
      properties: {
        productId: { type: 'string', format: 'uuid' },
        quantity: { type: 'integer', minimum: 1, maximum: 100 },
      },
      required: ['productId', 'quantity'],
      additionalProperties: false,  // rejects userId, isInternal, etc.
    },
  },
}, async (request, reply) => {
  await db.createOrder({ ...request.body, userId: request.user.id }); // userId from auth, not body
});
```

### 3. The double-send trap — return the payload OR call `reply.send`, never both

Fastify routes can either `return` the payload (Fastify sends it) or call `reply.send(payload)` (manual send). Doing both in an async handler sends the response twice — Fastify throws `FST_ERR_REP_ALREADY_SENT`, crashing the request:

```js
// WRONG — sends twice; FST_ERR_REP_ALREADY_SENT
fastify.get('/ping', async (request, reply) => {
  reply.send({ pong: true }); // sends here
  return { pong: true };      // Fastify tries to send again
});

// RIGHT (return style — preferred for async handlers)
fastify.get('/ping', async (request, reply) => {
  return { pong: true };
});

// RIGHT (reply.send style — fine in callbacks or conditional flows)
fastify.get('/ping', (request, reply) => {
  reply.send({ pong: true });
});
```

In async handlers, **prefer returning the payload**. Reserve `reply.send` for callback-based code, streams (`reply.send(stream)`), and cases where you need to set headers immediately before the body.

### 4. Plugin encapsulation — decorators and hooks are scoped, not global

Fastify's plugin system uses `fastify-plugin` / `fp` to opt out of encapsulation. Without it, decorators, hooks, and `reply`/`request` extensions registered in a child plugin are **not visible** to sibling plugins or the parent. AI code that registers a decorator in one plugin and uses it in another without `fp` produces `FST_ERR_DEC_MISSING_DEPENDENCY` at runtime:

```js
// WRONG — db decorator is encapsulated inside this plugin; siblings can't see it
fastify.register(async function dbPlugin(fastify) {
  fastify.decorate('db', createDbClient());
});

fastify.register(async function routePlugin(fastify) {
  fastify.get('/users', async req => req.server.db.findAll()); // Error: db not found
});

// RIGHT — use fastify-plugin to break encapsulation for shared utilities
import fp from 'fastify-plugin';

const dbPlugin = fp(async function (fastify) {
  fastify.decorate('db', createDbClient());
});

fastify.register(dbPlugin);        // db is now visible to all plugins
fastify.register(routePlugin);     // can access fastify.db
```

Rule: **shared infrastructure (DB, Redis, config, auth decorators) → wrap with `fp`**. Route groups that should be isolated (different prefix, scoped hooks) → do NOT use `fp`.

### 5. Hook ordering — `onRequest` vs `preHandler` for auth, `setErrorHandler` for errors

Fastify has a well-defined lifecycle. Hooks run in this order per request:

```
onRequest → preParsing → preValidation → preHandler → handler → preSerialization → onSend → onResponse
```

Getting the hook wrong means auth fires too late (after validation parses a body that should have been rejected) or too early (before the request is fully parsed):

- **Authentication**: use `onRequest` (runs before any body parsing — fail fast) or `preHandler` (after parsing, if you need `request.body` for auth — e.g. HMAC verification). Most auth (JWT, session) belongs in `onRequest`.
- **Route-level pre-handler hooks** are registered per-route via the `preHandler` option — don't register a global `preHandler` hook when you only want auth on specific routes.
- **Error handling**: Fastify's `setErrorHandler` is the single place for consistent error mapping. Express-style `(err, req, res, next)` does not exist — register one `fastify.setErrorHandler(async (error, request, reply) => { ... })` at the root or per-plugin scope.

```js
// Global auth hook — every request goes through this before parsing
fastify.addHook('onRequest', async (request, reply) => {
  try {
    await request.jwtVerify();
  } catch (err) {
    reply.code(401).send({ error: 'Unauthorized' });
  }
});

// Global error handler — maps Fastify validation errors and app errors consistently
fastify.setErrorHandler(async (error, request, reply) => {
  if (error.validation) {
    return reply.code(400).send({ error: 'Validation failed', details: error.validation });
  }
  request.log.error(error);
  reply.code(error.statusCode ?? 500).send({ error: error.message ?? 'Internal error' });
});
```

## How this slots into the core pipeline

- **Gate 5 (Design):** define JSON Schema for every route (body, params, querystring, response) before writing handlers. Missing response schema = potential data leak — flag it.
- **Gate 6 (Implementation):** async handlers return the payload (no `reply.send`); shared infrastructure plugins wrapped with `fp`; auth in `onRequest` hook; error handling in `setErrorHandler`.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–5 here; specifically verifies that every route has a response schema and no handler uses both `return` and `reply.send`.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Fastify library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Java too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Node framework (blocking the event loop, unhandled rejections, backpressure, heap limits, AbortController)? → **runtime tier** (`mir-backend-node`).
- A mechanical footgun of Fastify itself (schema-driven serialization, response-schema data leaks, double-send, plugin encapsulation, hook lifecycle, `setErrorHandler`)? → **here**.
- A different Node framework (Express, NestJS) → its own `mir-backend-node-<framework>` module. A different runtime → its own tier. Never widen this one.
