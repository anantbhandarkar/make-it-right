# Hono runtime portability — Bun · Workers · Deno · Node

Read this when a Hono app targets more than one runtime, or when code that works locally on Bun fails after deploying to Cloudflare Workers. Verified 13 Aug 2026 against Hono 4.13.1 docs and the Cloudflare Workers docs.

Hono's router and `Context` are runtime-neutral. **Everything platform-specific is an adapter import**, and the compiler will not stop you importing the wrong one — Workers failures show up at deploy or at the first request, not at build.

## Adapter imports by runtime

| Concern | Bun | Cloudflare Workers | Node | Deno |
|---|---|---|---|---|
| Entry | `export default app` or `{ port, fetch: app.fetch }` | `export default app` (module worker) | `serve({ fetch: app.fetch })` from `@hono/node-server` | `Deno.serve(app.fetch)` |
| Static files | `serveStatic` from `hono/bun` | `hono/cloudflare-workers` + an assets binding | `serveStatic` from `@hono/node-server/serve-static` | `serveStatic` from `hono/deno` |
| WebSockets | `createBunWebSocket()` from `hono/bun` | `upgradeWebSocket` from `hono/cloudflare-workers` | `hono/node-ws` | `hono/deno` |
| Config | `process.env` / `Bun.env` | `c.env.BINDING` | `process.env` | `Deno.env` |
| Deferred work | await it, or fire and forget | `c.executionCtx.waitUntil(p)` | await it | await it |

Read config through `env(c)` from `hono/adapter` so one call site works everywhere. Branch on `getRuntimeKey()` only when the behaviour genuinely differs; possible values are `bun`, `workerd`, `deno`, `node`, `edge-light`, `fastly`, `other`.

## What works on Bun and fails on Workers

### Node built-ins and the filesystem

Bun implements most of Node's standard library. Workers implements a subset behind `nodejs_compat`, and the subset is keyed to your **compatibility date** — the same code on two Workers with different dates behaves differently. Checked against Cloudflare's Node.js compatibility docs, 13 Aug 2026:

| Module | Bun | Workers |
|---|---|---|
| `node:crypto`, `node:buffer`, `node:stream`, `node:path`, `node:util` | yes | yes with `nodejs_compat` |
| `node:fs` | real disk | **virtual filesystem, not disk.** `/bundle` is read-only (your bundled modules), `/tmp` is writable but **per-request** and discarded, `/dev` has the usual character devices. Available with `nodejs_compat` from compat date 2025-09-01, on by default from 2026-08-04 |
| `node:net` | yes | yes — outbound sockets |
| `node:tls` | yes | partial |
| `node:child_process`, `node:worker_threads` | yes | **non-functional stubs** since compat date 2026-03-17. `import` and `require` succeed; nothing works |

Two consequences worth stating separately:

- **`node:fs` no longer throws on Workers — it lies.** Writing to `/tmp` succeeds, and the file is gone on the next request. Code that caches a rendered template, accumulates an upload across chunks, or writes a lockfile to disk passes local tests and silently loses data in production. Anything that must outlive one request goes to R2, KV, or a Durable Object.
- **`child_process` and `worker_threads` are the silent-stub shape**, the same failure the Bun tier warns about in footgun 1: a presence check passes, the import passes, and the call does nothing. Anything shelling out (image conversion, PDF rendering, `git`) cannot run on Workers — move it to a queue consumer on a runtime with a process model.

Reading a certificate, a migration file, or a JSON fixture from disk at startup still works on Bun and does not on Workers unless it is inside your bundle at `/bundle`. Bundling those as imports remains the portable answer.

### Long-lived state and connections

Bun is one long-running process: a module-level Postgres pool is created once and reused. Workers has no equivalent — see `SKILL.md` §9. A connection pool in module scope on Workers means a pool per isolate, created and discarded unpredictably. Use Hyperdrive, an HTTP-protocol database driver, or a Durable Object as the connection owner.

### Timers, background work, and `waitUntil`

On Bun a promise you do not await usually finishes, because the server keeps the process alive — but that is a side effect, not a guarantee, and the work is still lost on deploy, crash, or shutdown. On Workers the isolate may be frozen the instant the response is returned, so unawaited work is dropped mid-flight. The classic symptom is analytics events and audit logs that appear in development and vanish in production.

```ts
// WRONG on Workers — may never complete
app.post('/order', async (c) => {
  const order = await createOrder(...)
  void auditLog.write(order)          // dropped when the isolate freezes
  return c.json(order, 201)
})

// ALSO WRONG — this is the idiom everyone reaches for, and it fails on every runtime
c.executionCtx?.waitUntil?.(p) ?? await p
```

Two independent bugs in that one line, both verified on Bun 1.3.13 + Hono 4.13.1:

| Bug | What actually happens |
|---|---|
| `c.executionCtx` is a **throwing getter**, not a missing property | Off Workers it throws `Error: This context has no ExecutionContext`. Optional chaining does not guard a getter that throws — `?.` only short-circuits on `null`/`undefined`, and the throw happens during the property read. Every request 500s on Bun and Node |
| `waitUntil()` returns `undefined` | `??` falls through on `undefined`, so the right side **always** runs. On Workers you register the promise *and* then await it — the response blocks on the background work, which is the exact latency the pattern was meant to avoid |

Branch on the runtime instead:

```ts
import { getRuntimeKey } from 'hono/adapter'

app.post('/order', async (c) => {
  const order = await createOrder(...)
  const p = auditLog.write(order)
  if (getRuntimeKey() === 'workerd') { c.executionCtx.waitUntil(p) } else { await p }
  return c.json(order, 201)
})
```

`waitUntil` extends the lifetime; it is not durable. If the work must happen, it belongs in a queue with an idempotent consumer — the same rule the generic pillar states about at-least-once delivery.

`setInterval` for cron work is a Bun-only pattern. On Workers use a scheduled handler alongside the fetch handler:

```ts
export default { fetch: app.fetch, scheduled: async (event, env, ctx) => { ... } }
```

### CPU and wall-clock limits

Bun will happily run a 30-second CPU loop. Workers enforces a CPU-time limit per invocation (wall time spent waiting on I/O is not counted). Synchronous work that is merely slow on Bun — a large JSON parse, a bcrypt round count tuned for a server, an in-process image resize — becomes a hard failure on Workers. Check that any CPU-heavy path is either offloaded or sized for the limit before choosing a Workers deploy.

## Streaming differences

The streaming helpers are shared, but the transport is not. Two documented cases to know:

- On Cloudflare Workers under Wrangler, streaming can appear broken; the documented workaround is setting `Content-Encoding: Identity`.
- On Node via `@hono/node-server`, the Web `ReadableStream` is adapted to a Node stream. Backpressure and abort signals behave differently from Bun and Workers, so verify client-disconnect handling (`stream.aborted`, `stream.onAbort`) on the runtime you actually ship.

## Request and response limits are set outside Hono

Hono's `bodyLimit()` runs inside your app, so the platform limit always applies first:

| Runtime | Limit set where |
|---|---|
| Bun | `maxRequestBodySize` in the serve options, default 128 MiB. A larger Hono `maxSize` never fires its `onError` — Bun rejected the request already. |
| Workers | platform request-size limit, plan-dependent |
| Node | `@hono/node-server` options plus whatever reverse proxy is in front |

Set the limit in both places and keep them consistent, with the platform value as the outer bound.

## Portability checklist

Run this before promising an app runs on more than one runtime.

- [ ] No `node:child_process` or `node:worker_threads` on any path that ships to Workers — they import cleanly and do nothing.
- [ ] No `node:fs` **writes** treated as durable on Workers: `/tmp` is per-request and discarded.
- [ ] No file reads at startup — templates, certs, and fixtures are imports.
- [ ] Workers `compatibility_date` pinned and recorded; the Node subset available depends on it.
- [ ] No mutable module-scope state: no caches, no counters, no rate-limit maps, no connection pools that assume one process.
- [ ] Every promise not awaited before the response goes through `waitUntil`, or is enqueued.
- [ ] Config read through `env(c)`, never `process.env` directly in handler code.
- [ ] Adapter imports (`serveStatic`, WebSockets) are behind one module per runtime, not scattered through routes.
- [ ] CPU-heavy paths measured against the Workers CPU limit, or explicitly excluded from the Workers deploy.
- [ ] Preset chosen deliberately: `hono` (SmartRouter) for long-lived Bun/Node servers and for Workers; `hono/quick` only where the environment is re-initialized per request; `hono/tiny` for size-constrained builds.
- [ ] Integration tests run with `app.fetch()` on each target runtime's test runner, not only `bun test`.
- [ ] Same `hono` version in every deploy target's lockfile — a per-target lockfile drifting below 4.12.34 reintroduces the advisories in `SKILL.md`.
