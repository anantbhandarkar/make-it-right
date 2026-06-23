---
name: mir-backend-node
description: "Make It Right (Node.js runtime tier). V8/Node 20+ LTS runtime reliability footguns that are shared across EVERY Node backend framework (Express, Fastify, NestJS, Hapi, Koa) — distinct from the generic backend gates and from any one framework's mechanics. Covers: the single-threaded event loop and what blocks it (sync I/O, huge JSON, synchronous crypto/zlib, long CPU loops, pathological regex), the absence of CPU parallelism on one process and how to get it (worker_threads / cluster), unhandled promise rejection crashes, serializing awaits in a loop vs. bounded Promise.all concurrency, stream backpressure, AbortController timeouts on every outbound call, uncaughtException semantics, heap limits under container memory, and async-context loss across callbacks and timers. TRIGGER when the backend runtime is Node.js / V8 — sits between mir-backend (generic gates) and the framework module (e.g. mir-backend-node-express). SKIP for Python/JVM/Go/Rust/.NET/Ruby/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics (those live in the framework module)."
trigger: /mir-backend-node
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-node · Make It Right (Node.js runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-node-express`) knows the **library's mechanics**. This tier owns what's true for **all Node backends because they run on V8 in a single-threaded event loop** — the concurrency model and process model that Express, Fastify, NestJS, and every other Node framework all inherit.

**Runtime assumed:** Node.js 20+ LTS (all notes apply to 18 LTS; async context and worker_threads have been stable since Node 12+). Load order: `mir-backend` → `mir-backend-node` → `<framework module>`.

## The Node/V8 footguns AI walks into (framework-agnostic)

### 1. Blocking the event loop — the #1 Node reliability defect

Node is single-threaded. One synchronous operation that runs long stalls **every** concurrent request — latency spikes to the length of the blocking call multiplied by the backlog. AI routinely introduces blocks in hot paths:

- **Synchronous filesystem calls** (`fs.readFileSync`, `fs.writeFileSync`, `fs.existsSync`) in request handlers → use the `fs/promises` async equivalents or `fs.readFile` with a callback.
- **`JSON.parse` / `JSON.stringify` on large payloads** — these run synchronously on the V8 thread. A 5 MB JSON parse at 100 RPS is a sustained event-loop block. Stream-parse large bodies (e.g. `stream-json`) or reject oversized payloads at the transport layer.
- **Synchronous crypto** (`crypto.pbkdf2Sync`, `crypto.scryptSync`) — use the async form (`crypto.pbkdf2`, `crypto.scrypt`) or run in a `worker_threads` pool. `bcrypt.hashSync` in a route handler is a common example of this.
- **Synchronous zlib** (`zlib.deflateSync`, `zlib.gzipSync`) — use stream-based zlib or async variants.
- **Long CPU loops** — sorting/aggregating a large in-memory dataset, deep recursive computation. Any loop that takes > 5 ms is a latency problem at scale. Offload to `worker_threads` / `piscina`.
- **Pathological regex** (ReDoS) — a catastrophically backtracking regex run on untrusted input blocks the loop indefinitely. Test regex with `safe-regex` or `vuln-regex-detector`; avoid regex with nested quantifiers on unbounded user input.

Rule of thumb: **if it doesn't do I/O and takes more than a few milliseconds, it must not run on the event loop thread.**

### 2. No CPU parallelism on one process

Node has no GIL, but it also has no multi-core CPU parallelism by default — `async/await` only concurrently waits for I/O; it does not run JavaScript in parallel. For CPU-bound work:

- **`worker_threads`** — V8 isolates that share memory via `SharedArrayBuffer` / `Atomics` or pass structured clones. Best for compute tasks that need to return a result to the main thread. Use `piscina` for a managed pool (don't spin unlimited workers).
- **`cluster` / multiple processes** — N processes each listen on the same port (via OS SO_REUSEPORT or the cluster module's IPC dispatch). Each is an independent Node instance with its own heap. Right for scaling request handling across cores.
- **Horizontal scaling** — multiple container instances behind a load balancer. The canonical production answer for stateless services.

This is the runtime-level reason the runtime-map says "Do NOT use Node for heavily CPU-bound compute, data science, or heavy ML workflows."

### 3. Unhandled promise rejection crashes the process

Since Node 15, an unhandled rejection terminates the process (exit code 1). AI code that fires-and-forgets a promise without `.catch()` or `await` causes silent crashes in production:

```js
// WRONG — if doWork() rejects, the process dies with no log
doWork();

// RIGHT — always handle or await
doWork().catch(err => logger.error({ err }, 'doWork failed'));
// or
try { await doWork(); } catch (err) { logger.error({ err }, 'doWork failed'); }
```

Wire a global safety net — it should log + gracefully exit, not swallow:

```js
process.on('unhandledRejection', (reason, promise) => {
  logger.fatal({ reason }, 'Unhandled rejection — exiting');
  process.exit(1);
});
```

Critical: **attaching this listener replaces Node's default crash** — Node hands control to your handler instead of throwing-and-exiting. So your handler MUST exit; if it only logs and returns, you have re-introduced the zombie-process bug (a service running on a corrupted, half-initialized state). The same applies to `uncaughtException` (item 7).

Also critical: **`try/catch` does NOT catch errors thrown in a non-awaited promise or in a plain callback.** Only the `await` point is guarded by a surrounding `try/catch`.

### 4. Serialized awaits in a loop — use bounded `Promise.all`

AI commonly writes:

```js
// WRONG — runs serially: each item waits for the previous to finish
for (const id of ids) {
  results.push(await fetchById(id)); // N round-trips, sequential
}
```

Independent work should be concurrent:

```js
// Better — all requests fire at once
const results = await Promise.all(ids.map(id => fetchById(id)));
```

But **unbounded `Promise.all` over a large array exhausts the DB connection pool or external API rate limit.** Use `p-limit` or manual batching:

```js
import pLimit from 'p-limit';
const limit = pLimit(10); // max 10 in-flight at once
const results = await Promise.all(ids.map(id => limit(() => fetchById(id))));
```

Use `Promise.allSettled` when partial failure is acceptable and you want all results regardless.

### 5. Stream backpressure — ignoring it causes OOM

`stream.Writable.write()` returns `false` when the internal buffer is full. Ignoring the return value and continuing to `write()` buffers data unboundedly in memory — the classic OOM pattern for large file uploads, proxies, and ETL pipelines:

```js
// WRONG — ignores backpressure, can OOM
readable.on('data', chunk => writable.write(chunk));

// RIGHT — use pipeline(); it handles backpressure, errors, and cleanup
import { pipeline } from 'stream/promises';
await pipeline(readable, transform, writable);
```

`stream.pipeline` (and its promisified form) propagates errors and handles drain events. Always use it for stream-to-stream wiring.

### 6. Timeouts on every outbound call — no timeout = hung request

Node's built-in `fetch` (18+) and `undici` have no default request timeout. Under load, a slow or unresponsive upstream will accumulate open sockets until the process runs out of file descriptors:

```js
// WRONG — no timeout; hangs indefinitely
const res = await fetch('https://api.example.com/data');

// RIGHT — AbortController with a deadline
const controller = new AbortController();
const timeout = setTimeout(() => controller.abort(), 5000);
try {
  const res = await fetch('https://api.example.com/data', {
    signal: controller.signal,
  });
} finally {
  clearTimeout(timeout);
}
```

Wire this at the HTTP client layer (e.g. `undici`'s `bodyTimeout` / `headersTimeout`, or an `axios` instance with `timeout`) rather than per-call. Always pair outbound calls with a timeout.

### 7. `uncaughtException` — log and exit, never resume

`process.on('uncaughtException', handler)` catches synchronous throws that escape all call frames. The handler is a trap: **after an uncaught exception the process heap is in an undefined state.** The only safe action is to log and exit:

```js
process.on('uncaughtException', (err) => {
  logger.fatal({ err }, 'Uncaught exception — exiting');
  process.exit(1); // let the process manager (PM2, K8s) restart
});
```

Do not attempt to "resume" normal operation from `uncaughtException`. Use a process supervisor (PM2, systemd, Kubernetes restart policy) to bring it back.

### 8. Heap limits under container memory constraints

**Node 20+ auto-detects the container's memory limit** (via libuv `uv_get_constrained_memory`, cgroup-v2-aware) and sizes the V8 old-space heap from *that*, not from the host's total RAM. The old "a 512 MB container inherits the host's 16 GB heuristic and gets OOM-killed" failure mode is largely fixed on current LTS — and the once-standard fix of hard-pinning a fixed value now *causes* the opposite bug:

```dockerfile
# RISKY — a fixed value OVERRIDES auto-detection. The same image in a 2 GB
# container is now capped at 400 MB and GC-thrashes / OOMs under load.
CMD ["node", "--max-old-space-size=400", "dist/server.js"]
```

Guidance on Node 20+:

- **Default: set nothing.** Let Node read the cgroup limit. Verify it picked it up: `node -e 'console.log(require("v8").getHeapStatistics().heap_size_limit / 1048576)'` inside the container should track the limit, not host RAM.
- **Override only to decouple heap from the container** — e.g. large off-heap/native allocations (sharp, parsers, `Buffer` pools) need the JS heap kept *below* the container limit to leave room. When you do, set it as a *fraction of the actual limit with headroom* (e.g. ~75–80%), not a magic constant, so it scales when the limit changes. Newer Node also exposes `--max-old-space-size-percentage` for exactly this.
- Watch `--max-semi-space-size` for allocation-heavy (short-lived-object) workloads. Monitor heap via `process.memoryUsage()` / `v8.getHeapStatistics()` and expose it as a metric.

### 9. Async context loss — `AsyncLocalStorage` for request/correlation context

Callbacks and `setTimeout`/`setInterval` fire outside the call stack where they were registered — any `try/catch` higher up does not cover them, and naive "thread-local" patterns break:

```js
// WRONG — requestId is undefined inside the callback
let requestId;
app.use((req, res, next) => { requestId = req.headers['x-request-id']; next(); });
setTimeout(() => logger.info(requestId), 100); // loses context
```

Use `AsyncLocalStorage` (stable since Node 16) to carry per-request context across async boundaries:

```js
import { AsyncLocalStorage } from 'async_hooks';
export const requestContext = new AsyncLocalStorage();

// In middleware: wrap the rest of the request in a store
app.use((req, res, next) => {
  requestContext.run({ requestId: req.headers['x-request-id'] }, next);
});

// Anywhere in the async chain:
const { requestId } = requestContext.getStore() ?? {};
```

This is the Node equivalent of Python's `contextvars` — use it for correlation IDs, tenant context, and structured-log fields.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** state the concurrency model (async I/O vs. worker_threads vs. cluster) and justify it against the workload. CPU-bound work on the event loop thread is a runtime-level design defect — flag it.
- **Gate 6 (implementation):** code against `references/node-gotchas.md` alongside the core codegen checklist — no sync calls in hot paths; bound all concurrent work; add `AbortSignal.timeout` + signal propagation on every outbound call; use `pipeline()` for streams; wire SIGTERM draining.
- **Gate 7 (review):** the reliability-reviewer checks items 1–9 here (plus graceful shutdown and outbound-pooling from the reference) for any Node service.

## References

- `references/node-gotchas.md` — code-level right-vs-wrong companion to the footguns above, plus graceful SIGTERM shutdown, `AbortSignal` propagation, keep-alive connection pooling, startup config validation, and money/BigInt safety.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability principles) → **up** to `mir-backend`.
- A specific library's mechanics (Express middleware order, Fastify schema, NestJS DI scopes) → **down** to the framework module (`mir-backend-node-<framework>`).
- **Here:** only what every Node backend shares because of the V8 event loop and Node process model (concurrency model, backpressure, heap limits, async context, promise hygiene).
- A different runtime (Python, Go, JVM…) → its own `mir-backend-<runtime>` tier. Never widen this one.
