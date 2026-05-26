---
name: mir-backend-node-express
description: "Make It Right (Express module). Express 4/5 + Node.js specific reliability augmentation. Use alongside mir-backend and mir-backend-node when the target stack is Express — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: async error propagation differences between Express 4 and 5, middleware ordering as a hard contract, the absence of built-in validation and what fills the gap, security headers and CORS off-by-default, and object-level authorization gaps that structural frameworks catch but Express doesn't. TRIGGER only when the Node backend stack is Express — building, reviewing, or debugging an Express route, middleware, or error handler. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-node (V8 event-loop / process-model concerns: blocking, worker_threads, unhandled rejections, backpressure, timeouts); this module only adds Express library mechanics. SKIP for Fastify, NestJS, Hapi, Koa, or any non-Express stack (those get their own mir-backend-node-<framework> module), and for non-Node runtimes."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-node` (V8/Node event-loop model) → **this** (Express library mechanics). Run the gates first; load the Node runtime tier for event-loop and process-model concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (blocking the event loop, worker_threads, unhandled rejections, stream backpressure, AbortController timeouts, heap limits) live in `mir-backend-node` — not here.**

**Stack assumed:** Express 4.x or 5.x on Node 20+ LTS. Version differences that change behavior are called out explicitly.

## The Express footguns AI walks into most

### 1. Async errors in Express 4 are silently swallowed — Express 5 changes this

This is the single most common Express reliability defect AI ships.

**Express 4** does not await route handler return values. If an `async` handler throws, the error is an unhandled promise rejection — it does **not** reach your `(err, req, res, next)` error handler, and the request hangs until the client times out:

```js
// WRONG in Express 4 — thrown error is an unhandled rejection, request hangs
app.get('/users/:id', async (req, res) => {
  const user = await db.findUser(req.params.id); // throws → disappears
  res.json(user);
});
```

Fix: wrap with a helper that forwards rejections to `next`, or explicitly catch and call `next(err)`:

```js
// Option A — asyncHandler wrapper (e.g. express-async-errors or a 3-line helper)
const asyncHandler = fn => (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);

app.get('/users/:id', asyncHandler(async (req, res) => {
  const user = await db.findUser(req.params.id);
  res.json(user);
}));

// Option B — explicit catch
app.get('/users/:id', async (req, res, next) => {
  try {
    const user = await db.findUser(req.params.id);
    res.json(user);
  } catch (err) {
    next(err);
  }
});
```

**Express 5** awaits returned promises from route handlers and automatically forwards rejections to `next` — no wrapper needed. State the version in use; the fix differs. Using `express-async-errors` is a zero-migration shim for Express 4 codebases.

### 2. Middleware order is the contract — get it wrong and nothing else matters

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
- **Body parser after routes** — `req.body` is `undefined` in the handler.
- **Auth middleware missing from a router** — a route added later slips in unauthenticated.
- **Error handler not registered last** — it never runs.
- **Error handler with 3 args** (`(req, res, next)`) — Express identifies error handlers by arity; 3-arg handlers are treated as regular middleware and skipped for errors.

```js
// WRONG — error handler silently skipped (3 args)
app.use((err, res, next) => { res.status(500).json({ error: err.message }); });

// RIGHT — must have exactly 4 args
app.use((err, req, res, next) => { res.status(500).json({ error: err.message }); });
```

### 3. No built-in validation — without it, mass assignment and injection slip in

Express puts raw, unvalidated input on `req.body` / `req.query` / `req.params`. AI code that passes these directly to a database or service layer is vulnerable to:
- **Mass assignment / overposting** — a client sends `{ "isAdmin": true }` alongside a legitimate payload; if you spread `req.body` into an ORM object, you've just self-promoted them.
- **Type coercion surprises** — query strings are always strings; passing `req.query.limit` to a DB call that expects a number without parsing is a latent bug.

Validate at the route boundary with `zod` or `joi`. Define a schema, parse, and use only the parsed output:

```js
import { z } from 'zod';

const CreateUserSchema = z.object({
  email: z.string().email(),
  name: z.string().min(1).max(100),
  // id, isAdmin, role are NOT here — they're not accepted from the client
});

app.post('/users', asyncHandler(async (req, res) => {
  const body = CreateUserSchema.parse(req.body); // throws ZodError on invalid input
  const user = await db.createUser(body);        // only safe fields reach the DB
  res.status(201).json(user);
}));
```

Catch `ZodError` in the error handler and return a 400 with field-level detail. Never pass `req.body` to an ORM model constructor without schema-gating it first.

### 4. Security defaults are off — add helmet, CORS allowlist, rate limiting

Express ships with no security headers, open CORS, and no rate limiting. The defaults are the attack surface:

```js
import helmet from 'helmet';
import cors from 'cors';
import rateLimit from 'express-rate-limit';

// Helmet sets CSP, HSTS, X-Frame-Options, etc.
app.use(helmet());

// CORS: explicit allowlist, not '*'
app.use(cors({
  origin: ['https://app.example.com'],
  methods: ['GET', 'POST', 'PUT', 'DELETE'],
  credentials: true,
}));

// Rate limiting: per-IP, before auth/DB work
app.use('/api/', rateLimit({ windowMs: 60_000, max: 100 }));
```

Do not trust `req.ip` behind a proxy without `app.set('trust proxy', 1)` — otherwise rate limiting and logging record the proxy IP, not the real client.

### 5. Object-level authorization is easy to miss — no structure enforces it

Express has no built-in guard or controller structure. Authentication (who are you?) is easy to add as a single middleware. **Authorization (may *this user* act on *this specific resource*?)** is easy to forget because there's no framework hook that forces it at the route level:

```js
// WRONG — checks auth but not ownership; any authenticated user can read any user's data
app.get('/accounts/:id', requireAuth, asyncHandler(async (req, res) => {
  const account = await db.findAccount(req.params.id);
  res.json(account); // IDOR: account might belong to a different user
}));

// RIGHT — load the resource, then assert ownership before returning
app.get('/accounts/:id', requireAuth, asyncHandler(async (req, res) => {
  const account = await db.findAccount(req.params.id);
  if (!account || account.userId !== req.user.id) {
    return res.status(404).json({ error: 'Not found' });
  }
  res.json(account);
}));
```

Return 404 (not 403) when the resource doesn't belong to the requester — 403 leaks the existence of the resource. Enforce object-level authz at every endpoint that loads an entity by a client-supplied ID.

## How this slots into the core pipeline

- **Gate 5 (Design):** state Express version (4 vs 5), middleware order, validation library, and auth strategy. A missing `asyncHandler` wrapper in Express 4 is a design defect that surfaces as hung requests in production.
- **Gate 6 (Implementation):** every route handler is wrapped (Express 4) or returns a promise (Express 5); validation schema defined before handler body; helmet + cors + rate-limiter registered before routes; object-level authz at every entity-loading route.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–5 here; specifically verifies error-handler arity and that no route accesses `req.body` before body-parser middleware.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Express library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Java too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Node framework (blocking the event loop, unhandled rejections, backpressure, heap limits, AbortController)? → **runtime tier** (`mir-backend-node`).
- A mechanical footgun of Express itself (async error propagation, middleware arity, no built-in validation, CORS/helmet defaults, IDOR from missing authz)? → **here**.
- A different Node framework (Fastify, NestJS) → its own `mir-backend-node-<framework>` module. A different runtime → its own tier. Never widen this one.
