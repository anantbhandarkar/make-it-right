---
name: mir-backend-bun
description: "Make It Right (Bun runtime tier). Bun 1.3 runtime reliability footguns shared across every Bun backend (Bun.serve, Hono, Elysia, or an Express app run under bun) — Bun is a separate runtime on JavaScriptCore, not a faster Node, and this tier is where that difference bites. Covers: Node API gaps that present as silent stubs rather than errors (module.register exists and does nothing) and how to detect them in CI instead of production; native addons that require() cleanly then abort the process on first real use; the single-thread model, Bun.spawn, and worker_threads options that are silently ignored; Bun.serve defaults that differ from node:http (10s idleTimeout that kills SSE, development:true leaking source in 500 pages, binding every interface); Bun.file/bun:sqlite/Bun.password/Bun.$ behaviour differences; bun:test running every file in ONE process with leaking globals; the text bun.lock; the blocked-install-scripts default and its 367-package built-in allowlist; bun build --compile; unhandled rejections and backpressure. TRIGGER when the service runs on Bun in production, or when a Node-deployed project uses bun install / bun test in CI (then only the lockfile, install-script, and test sections apply). SKIP when the production runtime is Node.js with npm/pnpm/yarn — that is mir-backend-node, and this file would give wrong advice on lockfiles, install scripts, and the test runner. SKIP for Deno, and for Python/JVM/Go/Rust/.NET/Ruby/PHP/BEAM (each has its own mir-backend-<runtime> tier). SKIP for framework-library mechanics (Hono/Elysia routing, middleware order, validators) — those belong in a mir-backend-bun-<framework> module."
trigger: /mir-backend-bun
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-bun · Make It Right (Bun runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). A framework module knows the **library's mechanics**. This tier owns what is true for **every Bun backend because it runs on JavaScriptCore with Bun's own I/O layer and package manager** — not on V8, and not on Node's standard library.

Bun is a distinct runtime that implements a large part of Node's API. It is not a Node flavour. Everything below is about the places that distinction costs you.

Load order: `mir-backend` → `mir-backend-bun` → `<framework module, if any>`.

## Runtime state (verified 13 Aug 2026)

| Release | Status | Notes |
|---|---|---|
| Bun **1.3.14** (2026-05-13) | current stable | `latest` on npm and the newest GitHub release. No newer release in the three months to 13 Aug 2026 |
| Bun 1.3.13 | previous patch | the build every behaviour in this file was tested against (macOS arm64) |
| Bun 1.3.0 (2025-10-10) | the 1.3 line | isolated installs default for workspaces, `Bun.SQL`, built-in Redis client, Security Scanner API, `test.concurrent` |
| Bun 1.2 | superseded | switched the lockfile from binary `bun.lockb` to text `bun.lock` |

**The support model is "upgrade to newest".** There is no published LTS line, no end-of-life calendar, and no scheduled security-release train the way Node has. Pin an exact version in CI and your image, and own the upgrade cadence yourself.

**Advisories:** the GitHub Advisory Database lists one real Bun advisory, CVE-2024-21548 (medium, prototype pollution in the `Bun.Glob` native API, `> 0.0.12, < 1.1.30`) — not applicable to 1.3.x. CVE-2025-8022 was withdrawn on 2025-08-11. No open advisory affects the current line. Treat that as weaker evidence than it would be for Node: Bun does not publish a security-release feed, so "no advisory" partly reflects reporting, not just code.

**Node compatibility is measured against Node v23**, per Bun's own compatibility page. Bun's stated position is that a package working in Node but not in Bun is a Bun bug. That is the right policy, and it is not a plan you can ship on: the gap still exists until someone fixes it, and it will not announce itself (footgun 1).

## Fitness — is Bun right for this workload?

Gate 0 uses this the same way `mir-backend/references/runtime-map.md` does. A mismatch is not an automatic blocker, but it must be a recorded choice, not a silent default.

| Use Bun when… | Do NOT use Bun when… |
|---|---|
| HTTP/JSON services, I/O-bound APIs, WebSocket and SSE servers | You depend on native addons you have not exercised end-to-end under Bun (footgun 3) |
| TypeScript-first services you want to run with no build step | The work is CPU-bound: compute, ML inference, large data transforms — same single-thread model as Node, and worker `resourceLimits` are ignored so you cannot cap a worker's heap |
| CLIs and internal tools shipped as one binary (`--compile`) | You need `cluster` handle passing, or HTTP load balancing across processes on anything other than Linux |
| Monorepo installs and CI where `bun install` speed is the point | Your APM/tracing agent needs ESM loader hooks — `module.register` is a no-op (footgun 1) |
| Greenfield services where you control every dependency | Compliance requires a documented per-major support window and a vendor advisory feed |

## The Bun footguns AI walks into (framework-agnostic)

### 1. Compatibility gaps are silent stubs, not errors

This is the defining Bun failure and the reason "we tested the import, it's fine" is not a test. `node:module.register` exists in Bun 1.3.13, is a `function`, accepts the call, and **never runs the hook**:

```js
import { register } from "node:module";
register("./hooks.mjs", import.meta.url);
await import("virtual:answer");
// Node: hook resolves it, value 42
// Bun 1.3.13: ResolveMessage — Cannot find package 'virtual:answer'. No warning that the hook was ignored.
```

`typeof module.register === "function"` is `true` on both runtimes. **A presence check gives you a false green.** The same shape applies to `Worker` options (footgun 4) and to `node:v8`, where `getHeapStatistics()` returns a real object with a completely different `heap_size_limit` (~248 MB on Bun vs ~4.3 GB on Node for the same machine) — so any heap-headroom logic keyed off it silently changes behaviour.

**Detect gaps before production, three layers:**

1. **Run the real test suite under Bun in CI**, on the same command your service runs. Not a smoke test — the suite that exercises every dependency's real code path.
2. **Exercise, don't import.** For every native or loader-hook dependency, write a startup assertion that does the actual work (open the DB, sign the token, emit a span) and fails the build if it does not produce the expected value.
3. **During a migration, run both runtimes in CI** on the same job matrix until Bun has been green for a full release cycle. Divergence shows up as a diff, not as a 2am page.

Read `references/node-compat-gaps.md` for the per-module gap table and a copy-paste startup probe.

### 2. The Node API gaps that change backend design

| Module | Gap | What it costs you |
|---|---|---|
| `node:module` | `module.register` does not run hooks; `_cache`/`_extensions`/`_pathCache` are no-ops | ESM loader hooks silently do nothing — OpenTelemetry auto-instrumentation, `import-in-the-middle`, loader-based mocking |
| `node:worker_threads` | `Worker` ignores `stdin`/`stdout`/`stderr`/`resourceLimits`/`trackedUnmanagedFds` | No per-worker heap cap, no captured worker output (footgun 4) |
| `node:cluster` | Handles and file descriptors cannot pass between workers; HTTP balancing only on Linux via `SO_REUSEPORT` | Multi-process scaling is Linux-only and cannot hand off sockets |
| `node:inspector` | Only the `Profiler` API | Most Node profiling and debugging tooling does not attach |
| `node:https` | `Agent` is not always used | Pooling, keep-alive, and proxy settings set on an Agent may not apply |
| `node:crypto` | Missing `secureHeapUsed`, `setEngine`, `setFips` | FIPS-mode requirements cannot be met |

Fully implemented and safe to rely on: `node:fs`, `node:http`, `node:net`, `node:stream`, `node:zlib`, `node:dns`, `node:dgram`, `node:diagnostics_channel`, `node:events`, `node:buffer`. The full per-module table is in `references/node-compat-gaps.md`.

**`node:sqlite` does not exist in Bun.** Verified on 1.3.13: `require("node:sqlite")` throws `No such built-in module: node:sqlite`. Node 22+ ships it, so ported code that uses `DatabaseSync` fails at import. Use `bun:sqlite` instead — different API, same engine.

### 3. Native addons: `require()` succeeding proves nothing

Bun implements Node-API, and most N-API addons load. The failure mode when one does not is worse than an exception. Verified on Bun 1.3.13 with `better-sqlite3@13.0.3` on macOS arm64:

```
require("better-sqlite3")        → succeeds, exit 0
new Database(":memory:")         → process aborts, exit 133, Bun crash-report URL
```

Node with the identical `node_modules` runs both fine. Two consequences:

- **It is not catchable.** `try/catch` around the constructor does not help; the process is gone. You cannot write a graceful fallback, only a pre-flight check.
- **An import-only smoke test passes.** The crash needs first real use, which in a web service means the first request that touches the module — in production, under load.

Addons that link the V8 C++ API directly (NAN-era, or anything not built against Node-API) are the highest-risk class. Before Gate 5, list every transitive dependency with a `.node` file (`find node_modules -name '*.node'`) and write an exercise-it assertion for each. If one cannot be exercised, that dependency is a blocker, not a risk.

### 4. One process, one thread — and workers that ignore your limits

Bun's model is Node's: a single-threaded event loop, no CPU parallelism in-process. The blocking rules carry over unchanged — synchronous filesystem calls, `JSON.parse` on a large payload, sorting a big array, and a catastrophically backtracking regex on untrusted input each stall every concurrent request for their full duration. **If it does not do I/O and takes more than a few milliseconds, it must not run on the event loop thread.**

Bun-specific differences:

- **`bun:sqlite` is fully synchronous.** Every query blocks the event loop for its duration. That is fine for a 50 µs indexed lookup and a latency incident for an unindexed scan over a large table. Index the queries or move SQLite off the request path.
- **`Bun.password.hashSync` / `verifySync` block.** The async forms exist; use them. Argon2id at Bun's defaults measured ~66 ms per hash — 15 logins per second per process if you make it synchronous.
- **`Worker` silently ignores `resourceLimits` and `stdout`/`stderr`.** Verified: with `{ stdout: true, resourceLimits: { maxOldGenerationSizeMb: 8 } }`, worker output went to the parent's stdout instead of `worker.stdout` (which yielded nothing), and `worker.resourceLimits` was `undefined` (Node returns an object). You cannot cap a worker's heap. A runaway worker takes the container's memory, not its own budget.
- **`Bun.spawn` is the escape hatch for CPU work** and for anything you need isolated. It takes an argument array and does not use a shell:
  ```js
  const proc = Bun.spawn(["./resize", inputPath], { stdout: "pipe", timeout: 30_000 });
  const out = await new Response(proc.stdout).text();
  ```
  `stdout` defaults to `"pipe"`, `stderr` to `"inherit"`. Pass `timeout` or an `AbortSignal`, or a wedged child holds a slot forever. `proc.unref()` if the parent must be allowed to exit.
- **Horizontal scaling is the production answer**, same as Node. `cluster` on Bun cannot pass handles and only balances on Linux.

### 5. `Bun.serve` defaults that differ from `node:http`

Three defaults will change your service's behaviour without appearing in any diff.

| Setting | Bun.serve default | Consequence |
|---|---|---|
| `idleTimeout` | **10 seconds** | An in-flight response that sends nothing for 10 s is killed. Node's equivalent socket-inactivity setting, `server.timeout`, defaults to **`0` — disabled**, so code that ran indefinitely on Node now dies at 10 s. (Node's `requestTimeout` is 300 s but measures something else: time to *receive* the whole request, not idle time while responding) |
| `development` | **`true` unless `NODE_ENV === "production"`** | The 500 page embeds your exception message, source code, and absolute file paths |
| bind address | all interfaces | `server.hostname` reports `"localhost"`, but the socket is `*:port` |

**The 10-second timeout kills streams.** Verified: a streaming response that pauses 12 s is cut at 10 s with `[Bun.serve]: request timed out after 10 seconds` and the client gets a socket close. This hits SSE, long polling, large downloads on slow links, and any handler waiting on a slow query.

```js
Bun.serve({
  idleTimeout: 30,                 // 0–255 seconds; 0 disables
  fetch(req, server) {
    server.timeout(req, 0);        // opt this one request out — do this for SSE
    return new Response(stream);
  },
});
```

**The development error page is an information leak.** Verified on 1.3.13: with `NODE_ENV` unset, a thrown error produced a ~67 KB HTML response whose embedded base64 payload contained the exception message, the source of the failing file, and `/private/tmp/...` absolute paths. Grepping the body for the error string finds nothing, because it is base64 — a naive "we checked, it doesn't leak" test passes. Set `development: false` explicitly rather than relying on `NODE_ENV` being right in every environment.

Also: `server.hostname` returning `"localhost"` does not mean it is bound to loopback. Pass `hostname: "127.0.0.1"` when you mean loopback, and confirm with `lsof -nP -iTCP:<port> -sTCP:LISTEN`.

Graceful shutdown: `server.stop()` waits for in-flight requests; `server.stop(true)` cuts them. Wire `SIGTERM` → `stop()` → drain pools → exit, same ordering rule as Node.

### 6. Bun's own APIs behave differently from the Node equivalents

| API | Difference from the Node/npm equivalent | What to write |
|---|---|---|
| `Bun.file(path)` | Lazy. A missing file does not throw here: `.size` is `0`, `.type` is the default MIME, `.exists()` is `false`. The error arrives at `.text()`/`.json()` | `if (!(await f.exists())) return 404;` — do not infer existence from `.size` |
| `bun:sqlite` | WAL is **not** on by default | `db.run("PRAGMA journal_mode = WAL;")` at startup, or concurrent readers block on every write |
| `bun:sqlite` | `db.query()` caches the prepared statement (20 max); `db.prepare()` does not | Use `db.query()` on hot paths; `db.prepare()` for one-shot DDL |
| `bun:sqlite` | `db.run` / `db.exec` execute **multiple statements** from one string | Never build that string from user input — one `;` is a second statement |
| `bun:sqlite` | Integers above 2^53 silently lose precision unless `safeIntegers: true` | Set `safeIntegers: true` for money, IDs, and counters, and handle `bigint` |
| `Bun.password` | Default is argon2id (`m=65536` KiB, `t=2`), ~66 ms measured | Fine as-is. Do not lower it to "speed up login" |
| `Bun.password` | `algorithm: "bcrypt"` emitted `$2b$10$` on 1.3.13 (~70 ms). Bun's docs table states a different default | Pass `cost` explicitly. Never rely on the default |
| `Bun.password` | Passwords over 72 bytes are SHA-512 pre-hashed, not truncated | Hashes are **not** interchangeable with other bcrypt libraries for long passwords. Check this before a migration in either direction |
| `Bun.hash` | Not cryptographic — wyhash, xxHash, CRC32 | Tokens, session IDs, signatures use `crypto.randomUUID()`, `crypto.getRandomValues()`, or `Bun.CryptoHasher` |
| `Bun.$` (Bun Shell) | Escapes interpolated values by default — against **shell metacharacters only** | `` await $`convert ${userFile} out.png` `` cannot inject a second command, but a `userFile` of `-write` is still parsed by `convert` as an *option* (see argument injection below). `{ raw: userInput }` and `` $`bash -c ${cmd}` `` discard the escaping entirely |

### 7. `bun install` — the lockfile and what runs during it

**The lockfile is text.** Bun 1.2 replaced binary `bun.lockb` with `bun.lock`, a JSONC file with per-package `sha512-` integrity hashes. It reviews like any other lockfile: read the diff. If a repo still has `bun.lockb`, migrate it so reviewers can actually see dependency changes:

```bash
bun install --save-text-lockfile --frozen-lockfile --lockfile-only && rm bun.lockb
```

Commit `bun.lock`. In review, the lines that matter are new packages, changed resolved versions, and changed integrity hashes on unchanged versions.

**`bun install` does not run *arbitrary* dependency lifecycle scripts by default.** Verified: a dependency with a `postinstall` was blocked (`Blocked 1 postinstall. Run 'bun pm untrusted' for details.`) and its side effect never happened. This is a real difference from npm's historical default and it is the right default. The word "arbitrary" is load-bearing: scripts still run unprompted for Bun's 367-package built-in trust list and for anything in `trustedDependencies`. See Security.

**CI is not frozen by default.** Verified on 1.3.13: with `package.json` and `bun.lock` disagreeing, `CI=true bun install` **rewrote the lockfile and exited 0**. Setting `CI` buys you nothing here — you have to ask for frozen explicitly.

Two commands do that, and they are equivalent: `bun ci` is an alias for `bun install --frozen-lockfile`. Both error with `lockfile had changes, but lockfile is frozen`, exit 1, and leave `bun.lock` untouched. `bun ci` is not listed in `bun --help`, which is why it is easy to conclude it does not exist.

```yaml
- run: bun ci   # or: bun install --frozen-lockfile. Not optional.
```

### 8. `bun:test` is not Jest and not Vitest

The API is Jest-shaped, so AI writes Jest tests and they mostly run. The execution model is different, and that is where tests lie to you.

**Every test file runs in the same process, and globals leak between files.** Verified: two test files reported the same PID, and a counter on `globalThis` read `1` in the first file and `2` in the second. Jest gives each file its own worker; Vitest isolates per file by default. In Bun:

- A module-level singleton (a DB pool, a cached config, a `mock.module` replacement) set by one file is visible to the next.
- Tests that pass alone and fail in a suite are the normal symptom. So are tests that pass in a suite because an earlier file set something up.
- File order is not alphabetical and is not guaranteed. `--randomize` makes the ordering dependency fail loudly instead of intermittently — run it in CI.
- Reset shared state in `beforeEach`, not by relying on a fresh module registry.

**`.only` throws under CI.** Verified: `test.only(...)` passed locally and produced `1 fail, 1 error` with `CI=true` on 1.3.13. That is deliberate (a 1.3 change) and good — but it means a committed `.only` is green on the developer's machine and red in CI. New snapshots behave the same way.

Other differences worth knowing: `test.concurrent` / `describe.concurrent` / `test.serial` exist (1.3); coverage is configured in `bunfig.toml` under `[test]` (`coverage`, `coverageThreshold`, `coverageReporter`); `[test] preload` is the setup-file mechanism; `expectTypeOf()` is built in. The `node:test` shim is missing `run()`, snapshots, `mock.module()`, and coverage — target `bun:test` directly.

### 9. `bun build --compile` — what it does and does not bundle

`--compile` produces one executable containing your bundled code plus a full copy of the Bun runtime. Measured on 1.3.13: a one-line `console.log` compiled to a **60 MB** binary, and `--minify --bytecode` did not change that — the floor is the runtime, so size optimisation buys almost nothing at small sizes.

| Included | Not included / caveat |
|---|---|
| Your source and every imported npm package | `tsconfig.json` and `package.json` are **not** read at runtime |
| The Bun runtime and its Node API implementation | `.env` and `bunfig.toml` **are** read at runtime from the working directory — a surprise if you assumed the binary was sealed. `--no-compile-autoload-dotenv` and `--no-compile-autoload-bunfig` turn that off; the four `--compile-autoload-*` flags default to on for dotenv and bunfig, off for tsconfig and package.json |
| Assets via `with { type: "file" }` import attributes — there is **no `--asset` flag** | `.node` addons only if required directly. `@mapbox/node-pre-gyp`-style indirect resolution does not bundle |
| Worker entrypoints | only if listed as separate entrypoints — dynamic worker paths are not detected |
| Embedded SQLite via `with { type: "sqlite", embed: "true" }` | in a compiled binary it is read-write **in memory**; writes are lost on exit |

**`bun build` silently ignores flags it does not recognise.** Verified: `bun build ./e.ts --compile --totallyfakeflag ./a.txt` exits 0 and builds. A misremembered flag does not fail the build — it does nothing, and a path argument following it is swallowed as an extra entrypoint. Diff the output, do not trust exit 0.

Unsupported with `--compile`: `--outdir`, `--public-path`, `--target=node`, `--no-bundle`. Cross-compile targets cover Linux/macOS/Windows on x64 and arm64; the default `-modern` build needs AVX2, so an older x64 host gets `Illegal instruction` — use a `-baseline` target if you do not control the CPU. macOS binaries need `codesign` with `com.apple.security.cs.allow-jit` or Gatekeeper blocks them.

### 10. Unhandled rejections, streams, backpressure

**Promise semantics match Node**, verified on both runtimes — which removes the "Bun is different" excuse. An unhandled rejection terminates the process with exit 1; a `.catch()` attached on a later tick does not save it; `try/catch` does not cover a promise you did not `await`; an `EventEmitter` `'error'` with no listener still takes the process down. `process.on("unhandledRejection", fn)` suppresses the exit on both — use it to log and exit deliberately, never to keep running.

**Streams:** `node:stream/promises` `pipeline` works and is still the right way to wire streams — it propagates errors and destroys the chain on failure.

```js
import { pipeline } from "node:stream/promises";
await pipeline(readable, transform, writable, { signal: req.signal });
```

`Bun.file(path).writer()` returns a `FileSink` that buffers to `highWaterMark` — call `.flush()` and check its return value rather than writing in a loop and hoping. For `Bun.serve` responses, return a `ReadableStream` and let the runtime apply backpressure instead of building the body in memory. A slow producer hits the 10-second `idleTimeout` (footgun 5) long before it hits any memory limit.

## Security

Runtime-level mechanics. Framework items (route-level authorization, validation, CORS middleware config) belong in a framework module.

### Supply chain — the default is good, the allowlist is the part to check

Bun blocks dependency lifecycle scripts by default, which closes the mechanism behind the 2025–2026 npm worm incidents (`postinstall`/`preinstall` credential harvesters). But **"default-secure" is not "no scripts"**:

- `bun pm default-trusted` on 1.3.13 lists **367 packages** whose scripts run with no approval from you. Verified: `bun add esbuild` ran its postinstall silently. A compromise of any package on that list executes on your machine at install time.
- **`trustedDependencies` in `package.json` replaces the default list, it does not extend it.** Adding one entry turns the other 367 off. That is the safe direction, and it is the opposite of what most people assume.
- `git:`, `file:`, and `github:` dependencies are never auto-trusted regardless of name.
- Workflow: `bun pm untrusted` to list what was blocked, `bun pm trust <pkg>` to approve. Review the actual script text that `bun pm untrusted` prints before approving — that output is the audit.

| Control | What to set |
|---|---|
| Frozen install in CI | `bun install --frozen-lockfile`. Plain `bun install` rewrites the lockfile even with `CI=true` |
| Cooling-off window | `bunfig.toml` → `[install] minimumReleaseAge` in **seconds**. npm's `min-release-age` is days, pnpm/Yarn are minutes — copying a number across tools is the common misconfiguration. `minimumReleaseAgeExcludes` for your own scoped packages |
| Kill all scripts | `[install] ignoreScripts = true`, or `--ignore-scripts`. Strictest option; breaks packages that genuinely need a build |
| Vulnerability check | `bun audit` (`--audit-level=high`, `--ignore <CVE>`, `--json`). It queries npm's advisory data — it does not know about Bun's own runtime |
| Pre-install scanning | `[install.security] scanner = "<pkg>"`. `fatal` stops the install non-zero; `warn` prompts interactively but **exits immediately in CI** |
| Registry auth | `[install.scopes]` — keep tokens in env vars, never literal in a committed `bunfig.toml` |

### `Bun.serve` settings that ship insecure

- `development` is `true` unless `NODE_ENV === "production"`. Set `development: false` explicitly in the server config. Do not let one missing env var turn your 500 page into a source-code disclosure (footgun 5).
- The default bind is every interface. On a host with a private network, that is a service reachable from it. Set `hostname` explicitly.
- There is no built-in CSRF protection. If auth is a cookie, check `Origin` against an allow-list yourself and set `SameSite=Lax` or `Strict` on the session cookie. Bearer-token auth does not need this.
- There is no built-in CORS. Reflecting the request's `Origin` back with `Access-Control-Allow-Credentials: true` lets any site read authenticated responses — match against an explicit allow-list and set `Vary: Origin`.
- `maxRequestBodySize` defaults high — verified that a 2 MB body is accepted with no config, and that setting it to `1024` correctly returns 413. Set it to your real maximum, or `await req.json()` allocates whatever the client sends.

### Injection

| Vector | Wrong | Right |
|---|---|---|
| SQL (`bun:sqlite`) | `db.query("SELECT * FROM t WHERE id = " + id)`, or the same string built with a template literal | `db.query("SELECT * FROM t WHERE id = ?1").get(id)` |
| SQL, multi-statement | `db.run(userString)` — `run`/`exec` execute every `;`-separated statement | Never pass user input to `run`/`exec`. Use a prepared `query` |
| SQL identifiers | placeholders (they do not bind table/column names) | map the user value through an allow-list |
| Command | `` await $`bash -c ${cmd}` `` — spawning a shell discards Bun Shell's escaping | `Bun.spawn(["convert", name])` — argument array, no shell |
| Argument injection | passing a user string that starts with `-` as a positional argument | validate against an allow-list, or use `--` before user arguments |
| Prototype pollution | recursive merge of a parsed body | build with `Object.create(null)`, reject `__proto__`/`constructor` keys. CVE-2024-21548 was exactly this class in Bun's own `Glob` API |

### Secrets, PII, and SSRF

- `console.log(req)` or logging a whole request object writes the `Authorization` header and every cookie. Redact by field name at the logger, not by hoping.
- Never return a caught error's `message` or stack from a 5xx. `bun:sqlite` errors carry SQL text; Node-API addon errors carry paths.
- `fetch` follows redirects, so validating a user-supplied URL proves nothing about where the request lands. Allow-list destination hosts when you can. When you cannot, resolve **every** A and AAAA record, normalise the address, and reject every non-global range — `0/8`, `10/8`, `100.64/10` (CGNAT), `127/8`, `169.254/16`, `172.16/12`, `192.168/16`, `198.18/15`, `::1`, `fe80::/10`, `fc00::/7`, and **`::ffff:0:0/96`** (IPv4-mapped IPv6, the standard way to smuggle `169.254.169.254` past an IPv4-only deny-list). Allow only `https:`, use `redirect: "manual"`, and re-validate every hop. `169.254.169.254` returns cloud instance credentials to anything that can reach it.
- **A deny-list alone does not stop DNS rebinding.** Resolving the host, approving the address, and then calling `fetch(hostname)` re-resolves — and the second answer can be internal. Connect to the address you validated (preserving the original `Host`/SNI), or route the call through an egress proxy that enforces the policy.
- Path traversal: `Bun.file(join(UPLOAD_DIR, userName))` escapes on `../`. Resolve, then assert the result still starts with `UPLOAD_DIR + "/"` — `startsWith(UPLOAD_DIR)` alone lets `/data/uploads-evil` through. Generate stored filenames yourself.
- `--compile`d binaries read `.env` and `bunfig.toml` from the working directory at runtime. Do not treat the binary as a sealed secret boundary, and do not bake secrets in at build time.
- If this service calls an LLM: model output is untrusted input, exactly like a request body. Never put it inside `{ raw: ... }` in a `Bun.$` command, never pass it to `db.run`/`db.exec`, and never let it become a file path. Tool calls the model chooses still need the same authorization check as a direct request from that user.

## How this slots into the pipeline

- **Gate 0 (stack fitness):** check the workload against the "Do NOT use Bun when…" column. Name the exact Bun version. If any dependency has a `.node` file, that is a Gate 0 finding, not a Gate 7 one.
- **Gate 5 (design):** state the concurrency model (single event loop, `Bun.spawn` for CPU work, N containers for scale), the `idleTimeout` value and which routes opt out, and the plan for detecting Node-API gaps in CI (footgun 1). If tracing or profiling is in the observability plan, confirm the agent works under Bun before signing off — do not assume it does.
- **Gate 6 (implementation):** `development: false` and explicit `hostname` on `Bun.serve`; WAL on if `bun:sqlite` is used; async `Bun.password`; no `hashSync` on a request path; `--frozen-lockfile` in CI; `bun test --randomize` in CI.
- **Gate 7 (review):** the reliability-reviewer works items 1–10, weighting footgun 1 (silent stubs), 3 (native addons), and 8 (shared test process — an assertion that passes because a prior file leaked state is not a passing assertion). The security-reviewer works the Security section; the three most commonly missed are the 367-package default trust list, non-frozen CI installs, and `development` defaulting to `true`.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability principles) → **up** to `mir-backend`.
- A specific library's mechanics (Hono routing and middleware order, Elysia's type system and lifecycle hooks, Drizzle/Prisma query behaviour) → **down** to a `mir-backend-bun-<framework>` module.
- **Here:** only what every Bun backend shares because of JavaScriptCore, Bun's I/O layer, Bun's Node-API implementation, `bun install`, `bun:test`, and `bun build` — compatibility detection, native addons, the process/concurrency model, `Bun.serve` defaults, Bun's own APIs, lockfile and install-script policy, the test runner, compilation, promise semantics, and backpressure.
- **Node.js is a different runtime tier.** Do not copy rules between `mir-backend-node` and this file without re-verifying them on Bun — the lockfile, install scripts, test runner, and HTTP-server defaults all differ, and the compatibility gaps are the whole point of this tier existing.
