# Node.js / V8 runtime gotchas — right vs wrong

Code-level companion to SKILL.md. Stack: **Node 20+ LTS · V8 · single-threaded event loop**. Strictly runtime-level — Express/Fastify/Nest mechanics live in the framework module. Each section is the executable form of a SKILL.md footgun.

---

## 1. Don't block the event loop

```js
// WRONG — sync I/O / crypto / big parse on the one thread stalls EVERY request
const cfg = JSON.parse(fs.readFileSync('big.json'));   // sync read + sync parse
const hash = crypto.pbkdf2Sync(pw, salt, 100000, 64, 'sha512');

// RIGHT — async I/O, async crypto, stream/limit large payloads
const cfg = JSON.parse(await fs.promises.readFile('big.json', 'utf8'));  // I/O async; parse still sync — cap body size
const hash = await promisify(crypto.pbkdf2)(pw, salt, 100000, 64, 'sha512');
```

`JSON.parse`/`JSON.stringify` are synchronous on the V8 thread — a 5 MB parse at 100 RPS is a sustained block. Cap request body size at the transport layer; stream-parse (`stream-json`) when payloads are genuinely large. **Detect** blocks, don't just avoid them: `require('perf_hooks').monitorEventLoopDelay()` and `--cpu-prof`.

Rule: *if it does no I/O and takes more than a few ms, it must not run on the event loop thread.*

## 2. CPU work off the main thread

```js
// WRONG — heavy compute in the handler blocks all concurrent requests
app.get('/report', (req, res) => res.json(crunch(hugeDataset)));   // 200 ms of CPU = 200 ms stall for everyone

// RIGHT — offload to a managed worker pool
import Piscina from 'piscina';
const pool = new Piscina({ filename: new URL('./crunch-worker.js', import.meta.url).href });
app.get('/report', async (req, res) => res.json(await pool.run(params)));
```

`worker_threads`/`piscina` for compute that returns a result; `cluster` (or N container replicas) to scale request handling across cores. `postMessage` **structured-clones** the payload — you cannot pass functions, classes, or closures; only `ArrayBuffer`/`MessagePort` move zero-copy via the transfer list. Never spawn unbounded workers — use a pool.

## 3. Promise hygiene & unhandled rejection

```js
// WRONG — fire-and-forget; a rejection here crashes the process (Node 15+: exit 1)
doWork();

// RIGHT — every promise is awaited or .catch()'d
doWork().catch(err => logger.error({ err }, 'doWork failed'));

// The global net REPLACES the default crash — so it MUST exit, never just log:
process.on('unhandledRejection', (reason) => {
  logger.fatal({ reason }, 'unhandledRejection — exiting');
  process.exit(1);                 // log-and-return here = zombie process on corrupt state
});
```

`try/catch` guards only the `await` point. It does **not** catch a throw inside a non-awaited promise or a plain callback:

```js
try { fireAndForget(); } catch { /* never runs — the throw is async */ }
```

## 4. Error-first callbacks — never swallow `err`

```js
// WRONG — ignores the error arg; and throwing here is NOT caught by any try/catch
fs.readFile(p, (err, data) => { use(data); });        // err dropped
fs.readFile(p, (err, data) => { if (err) throw err; }); // throw escapes to uncaughtException

// RIGHT — handle the error, or use the promise API
const data = await fs.promises.readFile(p);            // errors become awaitable rejections
// or, wrapping a callback API:
const stat = await promisify(fs.stat)(p);
```

Prefer `fs/promises` / `util.promisify` over raw callbacks. A `throw` inside a callback does not unwind to a surrounding `try/catch` — it becomes an `uncaughtException`.

## 5. Bounded concurrency — `Promise.all`, but capped

```js
// WRONG — serial: each item waits for the previous (N sequential round-trips)
for (const id of ids) results.push(await fetchById(id));

// WRONG — unbounded: 10k concurrent calls exhaust the DB pool / hit rate limits
const results = await Promise.all(ids.map(fetchById));

// RIGHT — concurrent but bounded
import pLimit from 'p-limit';
const limit = pLimit(10);
const results = await Promise.all(ids.map(id => limit(() => fetchById(id))));
```

Use `Promise.allSettled` when partial failure is acceptable and you want every result regardless.

## 6. Stream backpressure — `pipeline()`, not manual `write()`

```js
// WRONG — ignores write()===false; buffers unboundedly → OOM on large transfers
readable.on('data', chunk => writable.write(chunk));

// RIGHT — pipeline handles backpressure, error propagation, and cleanup
import { pipeline } from 'stream/promises';
await pipeline(readable, transform, writable);
```

`writable.write()` returns `false` when the internal buffer is full; honoring it is the whole point of streams. `pipeline` does it for you and tears everything down on error.

## 7. Outbound timeouts + AbortSignal propagation

```js
// WRONG — no deadline; a slow upstream accumulates open sockets until FD exhaustion
const res = await fetch('https://api.example.com/data');

// RIGHT — one-liner deadline, and THREAD the signal through the call chain
async function getThing(id, signal) {
  const res = await fetch(`${BASE}/thing/${id}`, {
    signal: AbortSignal.any([signal, AbortSignal.timeout(5000)]),   // client-cancel OR 5s deadline
  });
  return res.json();
}
// honor client disconnect at the edge: getThing(id, req.signal)
```

`AbortSignal.timeout(ms)` replaces the manual `setTimeout`/`clearTimeout` dance. The common miss: setting a deadline at the edge but **not forwarding the signal** into downstream DB/HTTP calls — so a timeout or disconnect cancels nothing and orphaned work keeps running. Combine deadlines with `AbortSignal.any([...])`.

## 8. Keep-alive / connection pooling on outbound calls

```js
// WRONG — no keep-alive: a fresh TCP+TLS handshake per call; ephemeral-port exhaustion under load
import axios from 'axios';
await axios.get(url);                         // default agent, keepAlive off

// RIGHT — pooled agent created ONCE at module scope, reused for every call
import { Agent } from 'undici';
import { setGlobalDispatcher } from 'undici';
setGlobalDispatcher(new Agent({ connections: 128, keepAliveTimeout: 10_000 }));  // native fetch now pools
// http/https/axios: const agent = new https.Agent({ keepAlive: true, maxSockets: 128 }); axios.create({ httpsAgent: agent })
```

Native `fetch`/undici pool by default — tune the global dispatcher once. With `http`/`https`/`axios`, set an explicit keep-alive `Agent` at module scope. Never create an agent per request.

## 9. `uncaughtException` — log and exit, never resume

```js
// RIGHT — heap is in an UNDEFINED state after this; the only safe move is to die
process.on('uncaughtException', (err) => {
  logger.fatal({ err }, 'uncaughtException — exiting');
  process.exit(1);                 // let PM2 / systemd / K8s restart a clean process
});

// To OBSERVE without overriding the default crash, use the monitor variant:
process.on('uncaughtExceptionMonitor', (err) => metrics.increment('uncaught', { type: err.name }));
```

Do not attempt to "recover" and continue. `uncaughtExceptionMonitor` fires for telemetry but does **not** suppress the default exit — use it when you want a metric but still want Node to crash.

## 10. Graceful shutdown — drain SIGTERM, don't drop in-flight work

```js
// WRONG — process dies instantly on deploy/scale-down; in-flight requests dropped, pools severed mid-query
const server = app.listen(3000);

// RIGHT — stop accepting, drain in-flight with a deadline, close resources, exit
const server = app.listen(3000);
const connections = new Set();
server.on('connection', c => { connections.add(c); c.on('close', () => connections.delete(c)); });

async function shutdown(signal) {
  logger.info({ signal }, 'draining');
  server.close(() => {});                                 // stop accepting NEW connections
  const deadline = setTimeout(() => {                     // force-close stragglers after grace period
    for (const c of connections) c.destroy();
  }, 10_000).unref();
  await drainInFlight();                                  // let active requests finish
  clearTimeout(deadline);
  await Promise.allSettled([db.end(), redis.quit()]);     // close pools AFTER draining
  process.exit(0);
}
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => shutdown(sig));
```

This is the inbound twin of §7. Kubernetes/most orchestrators send `SIGTERM` then `SIGKILL` after a grace period — drain inside that window. `.unref()` the force-close timer so it can't itself keep the process alive.

## 11. Heap, cgroups & Buffers

```js
// WRONG — hard-pinning overrides Node 20+'s cgroup auto-detection;
//         the same image in a 2 GB container is now capped at 400 MB and GC-thrashes
//   node --max-old-space-size=400 server.js

// RIGHT — on Node 20+, set nothing; verify it read the container limit
node -e 'console.log(require("v8").getHeapStatistics().heap_size_limit / 1048576)'  // ≈ container limit, not host RAM
// Override ONLY to leave room for large off-heap/native allocs, as a fraction of the real limit (~75–80%).

// Buffers: default to zero-filled; allocUnsafe only when you immediately overwrite the whole buffer
const safe = Buffer.alloc(n);            // zero-filled
const fast = Buffer.allocUnsafe(n);      // uninitialized heap — fill it fully or you leak old memory
```

Node 20+ sizes the V8 old-space from the cgroup memory limit. A fixed `--max-old-space-size` *overrides* that and re-introduces the OOM/throttle bug it used to prevent. **Caveat:** on some cgroup-v2 hosts older libuv builds read host RAM instead of the container limit, so always *verify* `heap_size_limit` tracks the limit inside the actual container — if it reads host RAM, then (and only then) set an explicit fraction. Monitor `process.memoryUsage()` / `v8.getHeapStatistics()`.

## 12. Async context, startup config & money

```js
// AsyncLocalStorage — carry request/correlation context across async boundaries (Node's contextvars)
import { AsyncLocalStorage } from 'async_hooks';
export const ctx = new AsyncLocalStorage();
app.use((req, res, next) => ctx.run({ requestId: req.headers['x-request-id'] }, next));
const { requestId } = ctx.getStore() ?? {};      // available anywhere down the async chain

// WRONG — config read at import time: frozen before dotenv/secrets load, unvalidated, unmockable
export const API_KEY = process.env.API_KEY;      // top-level read

// RIGHT — read + VALIDATE once at startup into a frozen object
import { z } from 'zod';
const Env = z.object({ API_KEY: z.string().min(1), PORT: z.coerce.number().default(3000) });
export const config = Object.freeze(Env.parse(process.env));   // fail fast on missing/invalid

// Money: never IEEE-754 floats; 64-bit IDs/amounts overflow Number at 2^53
const total = priceMinor * qty;                  // integer minor units (cents), or a decimal lib
const id = BigInt(row.id);                        // JSON.parse silently truncates > MAX_SAFE_INTEGER — keep 64-bit IDs as strings/BigInt end-to-end
```

`0.1 + 0.2 !== 0.3`: store money as integer minor units or a decimal library, never as a `number` you multiply. `JSON.parse` truncates integers above `Number.MAX_SAFE_INTEGER` (2⁵³) — 64-bit IDs and amounts from other services must stay strings (or use a BigInt-aware parser) end-to-end.

---

### Also watch (lower-frequency, runtime-level)

- **Timer/handle leaks:** `setInterval` heartbeats and keep-alive sockets hold the loop open and hang shutdown. `.unref()` background timers; clear them in the shutdown path (§10).
- **EventEmitter listener leaks:** adding a listener per request without removing it trips `MaxListenersExceededWarning` — treat the warning as a real leak. Use `once` / an `AbortSignal` for per-request listeners; register long-lived ones a single time.
- **ESM/CJS interop:** pick one module system per package. In ESM, `__dirname`/`require` don't exist — use `import.meta.url` + `createRequire`. Node 22+ can `require()` synchronous ESM, but it's still a sharp edge; dual-published packages can load twice and break `instanceof`/singletons.
