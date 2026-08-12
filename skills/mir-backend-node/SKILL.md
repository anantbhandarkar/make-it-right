---
name: mir-backend-node
description: "Make It Right (Node.js runtime tier). V8/Node 22–26 runtime reliability footguns that are shared across EVERY Node backend framework (Express, Fastify, NestJS, Hapi, Koa) — distinct from the generic backend gates and from any one framework's mechanics. Covers: the single-threaded event loop and what blocks it (sync I/O, huge JSON, synchronous crypto/zlib, long CPU loops, pathological regex), the absence of CPU parallelism on one process and how to get it (worker_threads / cluster), unhandled promise rejection crashes, serializing awaits in a loop vs. bounded Promise.all concurrency, stream backpressure, AbortSignal.timeout on every outbound call, uncaughtException semantics, heap limits under container memory, graceful shutdown with keep-alive sockets, async-context loss across callbacks and timers, require(esm) and native TypeScript type stripping, and npm supply-chain defaults after the 2025–2026 registry compromises. TRIGGER when the backend runtime is Node.js / V8 — sits between mir-backend (generic gates) and the framework module (e.g. mir-backend-node-express). SKIP for Python/JVM/Go/Rust/.NET/Ruby/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics — Express middleware/routing goes to mir-backend-node-express, Fastify schemas/hooks to mir-backend-node-fastify, NestJS DI/guards/pipes to mir-backend-node-nestjs."
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

Load order: `mir-backend` → `mir-backend-node` → `<framework module>`.

## Runtime floor (checked against nodejs.org, 13 Aug 2026)

| Line | Status | Notes |
|---|---|---|
| Node 26 | Current | latest 26.7.0; enters LTS Oct 2026. Not the production default yet. |
| Node 24 "Krypton" | **Active LTS** | latest 24.19.0. The default target for new services. |
| Node 22 "Jod" | Maintenance LTS | latest 22.23.2; EOL 2027-04-30. Acceptable floor for existing services. |
| Node 20 "Iron" | **End of life** | EOL 2026-04-30. Gets no security patches. Do not target it. |
| Node 18 | End of life | EOL 2025-04-30. |

**Older versions of this skill said "Node 20+ LTS". That is now wrong** — Node 20 stopped receiving security releases on 2026-04-30 and was explicitly excluded from the July 2026 security release. State **Node 22 minimum, Node 24 recommended** in `engines.node` and in the Dockerfile base image.

The Node project has announced that the odd/even release model ends with Node 27 (every line will enter LTS after its Current phase). Do not plan an upgrade path around "wait for the next even number".

**Security patch floor:** the 29 July 2026 release fixed 11 CVEs including two HIGH HTTP/2 issues (CVE-2026-56846 memory exhaustion, CVE-2026-56848 heap use-after-free) and a HIGH Permission Model over-grant (CVE-2026-58043). Minimum patched versions: **22.23.2 / 24.18.1 / 26.5.1**.

## The Node/V8 footguns AI walks into (framework-agnostic)

### 1. Blocking the event loop — the #1 Node reliability defect

Node is single-threaded. One synchronous operation that runs long stalls **every** concurrent request — latency spikes to the length of the blocking call multiplied by the backlog. AI routinely introduces blocks in hot paths:

- **Synchronous filesystem calls** (`fs.readFileSync`, `fs.writeFileSync`, `fs.existsSync`) in request handlers → use the `node:fs/promises` async equivalents.
- **`JSON.parse` / `JSON.stringify` on large payloads** — these run synchronously on the V8 thread. A 5 MB JSON parse at 100 RPS is a sustained event-loop block. Stream-parse large bodies (e.g. `stream-json`) or reject oversized payloads at the transport layer.
- **Synchronous crypto** (`crypto.pbkdf2Sync`, `crypto.scryptSync`) — use the async form (`crypto.pbkdf2`, `crypto.scrypt`) or a `worker_threads` pool. `bcrypt.hashSync` in a route handler is the common instance of this.
- **Synchronous zlib** (`zlib.deflateSync`, `zlib.gzipSync`) — use stream-based zlib or the async variants.
- **Long CPU loops** — sorting/aggregating a large in-memory dataset, deep recursive computation. Any loop that takes > 5 ms is a latency problem at scale. Offload to `worker_threads` / `piscina`.
- **Pathological regex (ReDoS)** — a catastrophically backtracking regex run on untrusted input blocks the loop indefinitely. Avoid nested quantifiers on unbounded user input; check with `safe-regex`. This includes regexes you did not write: two 2026 CVEs in `path-to-regexp` (see the Express module) are exactly this.

Rule of thumb: **if it doesn't do I/O and takes more than a few milliseconds, it must not run on the event loop thread.**

### 2. No CPU parallelism on one process

Node has no GIL, but it also has no multi-core CPU parallelism by default — `async/await` only concurrently waits for I/O; it does not run JavaScript in parallel. For CPU-bound work:

- **`worker_threads`** — V8 isolates that share memory via `SharedArrayBuffer` / `Atomics` or pass structured clones. Best for compute that must return a result to the main thread. Use `piscina` for a managed pool (don't spin unlimited workers).
- **`cluster` / multiple processes** — N processes each listening on the same port. Each is an independent Node instance with its own heap. Right for scaling request handling across cores.
- **Horizontal scaling** — multiple container instances behind a load balancer. The canonical production answer for stateless services.

This is the runtime-level reason the runtime-map says "Do NOT use Node for heavily CPU-bound compute, data science, or heavy ML workflows."

**The other pool people forget: libuv's.** `fs.*` (async included), `dns.lookup`, async `zlib`, and async `crypto` do not use the event loop — they queue onto a shared libuv thread pool whose default size is **4**. Four concurrent `fs.readFile` calls or four `crypto.pbkdf2` calls starve everything else that uses it, and the symptom is identical to an event-loop block: rising latency with an idle CPU and an idle loop. Network sockets do not use this pool, so it is invisible in an HTTP-only benchmark. Raise `UV_THREADPOOL_SIZE` (env only, read once at startup) deliberately, and prefer `dns.resolve*` over `dns.lookup` on hot paths since only the latter is pool-bound.

### 3. Unhandled promise rejection crashes the process

Since Node 15 the default is `--unhandled-rejections=throw`: an unhandled rejection terminates the process. AI code that fires-and-forgets a promise without `.catch()` or `await` causes silent crashes in production:

```js
// WRONG — if doWork() rejects, the process dies with no log
doWork();

// RIGHT — always handle or await
doWork().catch(err => logger.error({ err }, 'doWork failed'));
// or
try { await doWork(); } catch (err) { logger.error({ err }, 'doWork failed'); }
```

Wire a global safety net — it should log + exit, not swallow:

```js
process.on('unhandledRejection', (reason) => {
  logger.fatal({ reason }, 'Unhandled rejection — exiting');
  process.exit(1);
});
```

Never set `--unhandled-rejections=warn` to "fix" a crash loop; it converts a loud failure into silent data loss.

Critical: **`try/catch` does NOT catch errors thrown in a non-awaited promise or in a plain callback.** Only the `await` point is guarded by a surrounding `try/catch`. The same holds for `EventEmitter`: an `'error'` event with no listener throws and, on a socket or stream, takes the process down.

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

But **unbounded `Promise.all` over a large array exhausts the DB connection pool or the external API rate limit.** Use `p-limit` or manual batching:

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
import { pipeline } from 'node:stream/promises';
await pipeline(readable, transform, writable);
```

`stream.pipeline` (and its promisified form) propagates errors and destroys every stream in the chain on failure. Always use it for stream-to-stream wiring. Pass an `AbortSignal` in the options so a disconnected client tears the pipeline down.

### 6. Timeouts on every outbound call — no timeout = hung request

Node's built-in `fetch` (backed by undici) has **no total-request timeout**. Under load, a slow or unresponsive upstream accumulates open sockets until the process runs out of file descriptors.

The current recommended pattern is `AbortSignal.timeout()` — not a hand-rolled `AbortController` + `setTimeout` pair:

```js
// WRONG — no timeout; hangs indefinitely
const res = await fetch('https://api.example.com/data');

// RIGHT — one line, cleaned up automatically
const res = await fetch('https://api.example.com/data', {
  signal: AbortSignal.timeout(5000),
});

// Combine a deadline with a caller-supplied cancellation signal:
const res2 = await fetch(url, {
  signal: AbortSignal.any([req.signal, AbortSignal.timeout(5000)]),
});
```

`AbortSignal.timeout` rejects with a `TimeoutError` `DOMException`; distinguish it from a caller `AbortError` when deciding whether to retry. Undici's own `headersTimeout` / `bodyTimeout` defaults are far longer than any realistic API SLA — if you configure an `undici.Agent` or an `axios` instance, set them explicitly rather than relying on defaults.

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

Current Node **does** read the cgroup limit — it sizes V8's defaults from `min(uv_get_total_memory(), uv_get_constrained_memory())`, so the old "Node thinks it has the whole host" story is stale on Node 22/24/26. What still kills containers is the gap between the V8 old-space budget and process RSS: Buffers, native addons, Wasm, and per-thread stacks live outside old space, and every `cluster` worker or `worker_thread` gets its own heap inside the same cgroup. The kernel OOM-kills on RSS, not on old space. Set the budget explicitly so N processes fit:

```dockerfile
# Set explicitly — leave headroom for the OS and native modules
CMD ["node", "--max-old-space-size=400", "dist/server.js"]
```

Also watch `--max-semi-space-size` for allocation-heavy workloads. Monitor heap via `process.memoryUsage()` and expose it as a metric. Note that `NODE_OPTIONS` is honoured for these flags, which makes it both a deployment tool and a security concern (see Security).

### 9. Async context loss — `AsyncLocalStorage` for request/correlation context

Callbacks and `setTimeout`/`setInterval` fire outside the call stack where they were registered — any `try/catch` higher up does not cover them, and naive "thread-local" patterns break:

```js
// WRONG — requestId is whatever the last request set; wrong under concurrency
let requestId;
app.use((req, res, next) => { requestId = req.headers['x-request-id']; next(); });
setTimeout(() => logger.info(requestId), 100); // wrong value
```

Use `AsyncLocalStorage` to carry per-request context across async boundaries:

```js
import { AsyncLocalStorage } from 'node:async_hooks';
export const requestContext = new AsyncLocalStorage();

// In middleware: wrap the rest of the request in a store
app.use((req, res, next) => {
  requestContext.run({ requestId: req.headers['x-request-id'] }, next);
});

// Anywhere in the async chain:
const { requestId } = requestContext.getStore() ?? {};
```

This is the Node equivalent of Python's `contextvars` — use it for correlation IDs, tenant context, and structured-log fields. Node 24 switched the default implementation to `AsyncContextFrame`, which removed most of the historical performance objection to running it on every request.

### 10. `require(esm)` is stable — but not for top-level `await`

Loading an ES module from CommonJS with `require()` is no longer flagged (unflagged on the 22 line, stable from 24 on). The remaining failure is specific and mechanical: **`require()` of an ES module that contains top-level `await` throws `ERR_REQUIRE_ASYNC_MODULE`.** So does requiring a module whose dependency graph contains one.

```js
// throws ERR_REQUIRE_ASYNC_MODULE if ./config.mjs has a top-level await
const config = require('./config.mjs');

// fix: dynamic import at the call site, and make the caller async
const config = await import('./config.mjs');
```

Symptom to recognise in review: a library upgrade adds a top-level `await` and a CommonJS consumer starts failing at load time, not at call time.

### 11. Native TypeScript type stripping — erasable syntax only

Node 24 runs `.ts` files directly, with the experimental warning removed as of 24.3.0. It **strips** types; it does not check them, so `tsc --noEmit` still belongs in CI.

The mechanical trap: only *erasable* syntax works. These fail at runtime, not at build time:

| Syntax | Works under `node file.ts`? |
|---|---|
| type annotations, `interface`, `type`, generics, `as` | yes |
| `enum`, `const enum` | no |
| `namespace` with runtime members | no |
| **parameter properties** (`constructor(private readonly db: Db) {}`) | no |
| `declare` fields with initialisers | no |

Parameter properties are the one that bites hardest — it is the idiomatic dependency-injection form in NestJS and in most TypeScript service classes. Node 26's notable-changes list records the removal of the `--experimental-transform-types` escape hatch, so "just add the flag" is no longer an answer on the newest line. If your code uses non-erasable syntax, keep a real build step (tsc, swc, esbuild) rather than pointing `node` at `.ts` sources.

### 12. Graceful shutdown — `server.close()` handles idle sockets, not active ones

Since Node 19, `server.close()` stops accepting new connections **and closes idle keep-alive connections before returning**, so `closeIdleConnections()` is redundant on every line you should be running. Older advice to call it alongside `close()` is stale — harmless, but it is not what saves you.

What `close()` will not do is cut off a connection that is mid-request. A client streaming a slow upload, or one deliberately trickling bytes, keeps the process alive until the orchestrator SIGKILLs it and the in-flight work is lost anyway. You need your own deadline:

```js
process.on('SIGTERM', async () => {
  server.close(() => { /* accepting stopped; idle + finished connections closed */ });
  setTimeout(() => server.closeAllConnections(), 10_000).unref(); // hard cut-off for active ones
  await drainQueuesAndPools();
});
```

Make the deadline shorter than the orchestrator's `terminationGracePeriodSeconds`, or the hard cut-off never runs. WebSocket and HTTP/2 sessions are not HTTP keep-alives and need their own teardown. Flush the logger and close DB pools before `process.exit`, or the last errors of the deploy are never written.

### 13. `node:test` is the built-in runner — coverage and watch are not stable

`node:test` has been Stability 2 (Stable) since Node 20, and from Node 24 the runner automatically waits for subtests, which removes a class of "test passed because nobody awaited it" false greens. Two parts are still **Stability 1 (Experimental)** in the Node 26 docs: code coverage (`--experimental-test-coverage`) and watch mode (`--watch`). Do not gate a CI merge on a coverage number produced by an experimental flag without pinning the Node minor.

## Security

Runtime-level security mechanics. Framework-specific items (CORS config, validation pipes, response schemas) live in the framework module.

### Supply chain — the defaults changed in 2026, and they changed for a reason

2025–2026 produced a run of registry compromises that all followed one shape: maintainer account takeover → automated republish across every package that account owned → code execution through an install lifecycle script.

| Incident | When | Mechanism |
|---|---|---|
| `chalk` / `debug` and ~18 related packages | Sept 2025 | maintainer phished via a lookalike npm domain; crypto-clipper payload |
| "Shai-Hulud" worm (`@ctrl/tinycolor`, ngx-bootstrap, ~500 packages) | Sept 2025 | `postinstall` script harvested npm/GitHub/cloud credentials, then self-replicated by republishing the victim's own packages |
| "Shai-Hulud 2.0" / "The Second Coming" (~796 packages) | Nov 2025 | moved to **`preinstall`**, exfiltrated to public GitHub repos, destructive fallback |
| `axios` (1.14.1, 0.30.4) | Mar 2026 | stolen publish token; phantom dependency ran a `postinstall` RAT |
| Mastra AI scopes (140+ packages) | Jun 2026 | maintainer account takeover; typosquatted transitive dependency |
| `keyv` and related | Aug 2026 | `preinstall` loader ran before application code |

npm's answer shipped in **npm v12.0.0 (8 July 2026)** and changed three defaults. Verified against the npm v12 config docs:

| Config | v12 default | What it does |
|---|---|---|
| `allowScripts` (package.json policy) | off | dependency `preinstall`/`install`/`postinstall` and implicit `node-gyp rebuild` are **skipped with a warning** |
| `strict-allow-scripts` | `false` | set `true` in CI to turn that skip into a hard install failure |
| `allow-git` | `none` | git-ref dependencies do not resolve |
| `allow-remote` | `none` | tarball-URL dependencies do not resolve |
| `min-release-age` | `null` | when set (in **days**), only versions published more than N days ago are installed |

What to actually write:

```jsonc
// package.json — commit the approvals so CI and every developer run the same policy
{ "allowScripts": { "sharp@0.34.4": true } }
```

```ini
# .npmrc — committed
min-release-age=7          # npm >= 11.10.0 (Feb 2026); silently ignored by older npm
strict-allow-scripts=true  # CI: fail, don't warn, when a new dep wants to run a script
```

- Review pending scripts with `npm install-scripts ls`; approve with `npm install-scripts approve <pkg>`; block with `npm install-scripts deny <pkg>`. Approvals are version-pinned by default (`allow-scripts-pin`), so a new version of an approved package must be re-approved.
- `min-release-age` and `before` may both be set; within one config source `before` wins, and across sources normal npm precedence applies. The age window will hold back `npm audit fix` when the only patched version is newer than the window — npm keeps the vulnerable version, warns, and exits non-zero. Document the escape hatch for that case (`npm i --min-release-age 0 pkg@version` for a specific hotfix).
- Other package managers use different keys **and different units**: pnpm `minimumReleaseAge` (minutes), Yarn `npmMinimalAgeGate` (minutes), Bun `minimumReleaseAge` (seconds). Copying a value across tools is the most common misconfiguration.
- **Install from the lockfile in CI**: `npm ci`, not `npm install`. `npm ci` fails when `package-lock.json` disagrees with `package.json` instead of silently rewriting it. Never set `package-lock=false`.
- Publish from CI with **trusted publishing (OIDC)** rather than a long-lived token — npm's classic tokens were permanently revoked on 9 Dec 2025 and granular publish tokens are being narrowed further. Requires `id-token: write`, npm CLI ≥ 11.5.1, Node ≥ 22.14.0.
- None of this stops a compromise that publishes through a legitimate pipeline with valid provenance. Pin exact versions for anything in the auth, crypto, or payment path, and check `npm ls <pkg>` after an advisory rather than trusting the top-level range.

### Runtime hardening flags

| Flag / env | Effect | When to use |
|---|---|---|
| `--permission` (+ `--allow-fs-read`, `--allow-fs-write`, `--allow-child-process`, `--allow-worker`, `--allow-net`, `--allow-addons`) | restricts what the process may touch | defence in depth only |
| `--disable-proto=throw` | makes `Object.prototype.__proto__` access throw | any service parsing untrusted JSON |
| `NODE_TLS_REJECT_UNAUTHORIZED=0` | **disables all TLS verification process-wide** | never in any deployed environment |
| `NODE_OPTIONS` | injects CLI flags into every child `node` | treat as executable input; never build it from user data |

The Permission Model is **not a sandbox, and Node's own docs say so** — it is a seat belt against unintentional access by trusted code, not a boundary against malicious code. It does not inherit into child processes or worker threads, already-open file descriptors bypass it, symlinks are followed outside granted paths, and `--env-file` is read before it initialises. It has also been bypassed repeatedly: CVE-2026-58043 (HIGH, path-prefix over-grant), CVE-2026-56847 and CVE-2026-58039 (writes outside the allowlist via trace events and process reports), all fixed 29 July 2026. Do not present `--permission` to a reviewer as the reason an untrusted-code path is safe.

### `node:vm` is not a sandbox

`vm.runInNewContext` / `vm.runInThisContext` share the same process and prototype chain; escapes are trivial, and Node's docs state the module is not a security mechanism. CVE-2025-54782 (`@nestjs/devtools-integration` ≤ 0.2.0, CVSS 8.8, fixed in 0.2.1) is the worked example: an HTTP endpoint fed attacker JSON into `vm.runInNewContext`, so any website a developer visited got code execution on the developer's machine. If you must evaluate untrusted code, use a separate process with the OS or the container as the boundary. Never `eval`, `new Function`, or `vm` on request data, LLM output, or config fetched at runtime.

### Command injection

| API | Spawns a shell? | Use with request data? |
|---|---|---|
| `child_process.exec`, `execSync` | always | no |
| `child_process.execFile`, `spawn` with `shell: true` | yes | no |
| `child_process.execFile`, `spawn` with `shell: false` (default) | no | yes, with an allow-listed argument array |

```js
// RIGHT — no shell; arguments are passed as an array, never concatenated
import { execFile } from 'node:child_process';
execFile('convert', [`${safeName}.png`, 'out.png'], { shell: false });
```

The failure mode: any string interpolated into a shell command lets `;`, `&&`, `$( )`, and backticks run arbitrary commands as the service user. Validate every value that reaches an argument array against an allow-list; quoting is not a fix.

### SSRF from user-supplied URLs

`fetch` follows redirects by default, so validating the URL the user gave you proves nothing about where the request lands.

- Resolve the hostname, reject private and link-local ranges (`10/8`, `172.16/12`, `192.168/16`, `127/8`, `169.254/16`, `::1`, `fc00::/7`), then re-check **after every redirect** — use `redirect: 'manual'` and drive the chain yourself, or an undici `Dispatcher` with `maxRedirections: 0`.
- The cloud metadata endpoint (`169.254.169.254`, and `metadata.google.internal`) returns instance credentials to any process that can reach it. An SSRF that reaches it is a credential leak, not a curiosity. Require IMDSv2 / hop-limit 1 where the platform offers it.
- DNS rebinding defeats check-then-fetch: resolve once, then connect to the resolved IP with an explicit `Host` header, or route through a proxy that enforces the allow-list.
- Allow only `https:`. Block `file:`, `ftp:`, `gopher:`, and `data:` explicitly.

### Prototype pollution and deserialization

`JSON.parse` itself is safe, but the next line usually is not: a recursive merge, `Object.assign` into an existing object, or an ORM that spreads the body will happily walk a `__proto__` or `constructor.prototype` key and mutate every object in the process.

- Run with `--disable-proto=throw`.
- Never recursively merge untrusted objects. If you must, use a parser that rejects those keys (`secure-json-parse`, which is what Fastify uses by default) or build the target with `Object.create(null)`.
- `js-yaml` v4's `load()` uses the safe schema; the full schema and the old `unsafeLoad` do not. Never deserialize untrusted input with `node-serialize` or anything that reconstructs functions.

### Path traversal

```js
// WRONG — '../../etc/passwd' and '%2e%2e%2f' both escape the root
fs.createReadStream(path.join(UPLOAD_DIR, req.params.file));

// RIGHT — resolve, then prove the result is still inside the root
const full = path.resolve(UPLOAD_DIR, req.params.file);
if (full !== UPLOAD_DIR && !full.startsWith(UPLOAD_DIR + path.sep)) {
  throw new ForbiddenError();
}
```

Comparing with `startsWith(UPLOAD_DIR)` alone is wrong — `/data/uploads-evil` passes it. Include the separator. For uploads, generate the stored filename yourself (UUID) and keep the client's name only as a display label; never let it reach the filesystem. `path.normalize` alone is not a check.

### Secrets and PII

- Structured loggers serialize whole objects. `logger.info({ req })` writes the `Authorization` header and every cookie into your log pipeline. Configure redaction explicitly (`pino`'s `redact: ['req.headers.authorization', 'req.headers.cookie', '*.password', '*.token']`).
- Error responses: never return `err.stack` or `err.message` from a 5xx to a client. Database drivers put the failing SQL, and sometimes parameter values, into `err.message`.
- `process.env` dumps in crash handlers, health endpoints, and "debug" routes leak everything at once.
- `--env-file` / `process.loadEnvFile()` is a developer convenience; in production inject secrets from the platform's secret store so they never sit in an image layer or a repo.
- Anything read at build time gets baked into the image. Check `docker history` before assuming a build arg was ephemeral.

### Prompt injection, if this service calls an LLM

Model output is untrusted input, exactly like a request body. Retrieved documents, tool results, and user messages can all carry instructions.

- Never pass model output to a shell command, `vm`, `eval`, a SQL string, or a file path.
- Tool/function calls returned by a model must go through the same authorization check as a direct HTTP request from that user — the model deciding to call `deleteAccount` is not authorization.
- Bound the loop: max tool-call iterations, a wall-clock deadline, and a token budget, or one crafted document turns into unbounded spend.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** state the concurrency model (async I/O vs. worker_threads vs. cluster) and the Node line you target, and justify both against the workload. CPU-bound work on the event loop thread is a runtime-level design defect — flag it.
- **Gate 6 (implementation):** no sync calls in hot paths; bound all concurrent work; `AbortSignal.timeout` on every outbound call; `pipeline()` for streams; graceful shutdown wired to SIGTERM.
- **Gate 7 (review):** the reliability-reviewer checks items 1–13; the security-reviewer checks the Security section, and specifically that the Node minor is at or above 22.23.2 / 24.18.1 / 26.5.1 and that CI runs `npm ci`, not `npm install`.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability principles) → **up** to `mir-backend`.
- A specific library's mechanics (Express middleware order, Fastify schema, NestJS DI scopes) → **down** to the framework module (`mir-backend-node-<framework>`).
- **Here:** only what every Node backend shares because of the V8 event loop, the Node process model, and the npm ecosystem (concurrency model, backpressure, heap limits, async context, promise hygiene, module loading, supply chain).
- A different runtime (Python, Go, JVM…) → its own `mir-backend-<runtime>` tier. Never widen this one.
