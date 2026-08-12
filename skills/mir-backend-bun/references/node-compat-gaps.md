# Bun Node-API compatibility — the gap list and how to detect gaps in CI

Referenced from `mir-backend-bun` footgun 1. Read this when a service is moving to Bun, or when a Node-ecosystem dependency behaves differently under Bun than it does under Node.

**Verified 13 Aug 2026** against Bun's compatibility documentation (which measures against **Node v23**) and against local runs on **Bun 1.3.13, macOS arm64**. Re-check the table when you upgrade; these gaps close release by release.

## Why presence checks fail

Bun's compatibility gaps come in three shapes, and only one of them is loud:

| Shape | Example | How it presents |
|---|---|---|
| **Missing** | `tls.createSecurePair` | `TypeError: not a function`. Loud, easy to find |
| **Silent stub** | `module.register` | The function exists, accepts the call, returns normally, and does nothing |
| **Different behaviour** | `v8.getHeapStatistics().heap_size_limit` | Returns a real value that means something else |

Feature detection (`typeof x === "function"`, `"register" in module`) only catches the first shape. The other two are what reach production.

```js
// Both Node and Bun 1.3.13 print "function". Only Node actually runs the hook.
console.log(typeof require("node:module").register);
```

The rule: **assert on behaviour, never on presence.**

## Per-module gap table

Sourced from Bun's own compatibility page, filtered to backend-relevant entries.

### Partially implemented

| Module | Documented gap |
|---|---|
| `node:module` | `module.register` does not run hooks. `module._cache`, `module._extensions`, `module._pathCache` are no-ops. Missing `syncBuiltinESMExports` behaviour, `Module#load()` |
| `node:worker_threads` | `Worker` ignores options `stdin`, `stdout`, `stderr`, `trackedUnmanagedFds`, `resourceLimits`. Missing `markAsUntransferable`, `moveMessagePortToContext` |
| `node:cluster` | Handles and file descriptors cannot be passed between workers. HTTP load balancing works only on Linux, via `SO_REUSEPORT` |
| `node:async_hooks` | `AsyncLocalStorage` and `AsyncResource` are implemented. V8 promise hooks are never called. Bun's docs discourage use of the rest |
| `node:child_process` | Missing `proc.gid` and `proc.uid`. IPC cannot send socket handles |
| `node:crypto` | Missing `secureHeapUsed`, `setEngine`, `setFips` |
| `node:http2` | Client and server implemented; ~95% of the gRPC test suite passes |
| `node:https` | Implemented, but `Agent` is not always used — Agent-level pooling/proxy settings may not apply |
| `node:inspector` | Only the `Profiler` API (`enable`, `disable`, `start`, `stop`, `setSamplingInterval`) |
| `node:perf_hooks` | Implemented, but Node's test suite for this module does not pass |
| `node:v8` | Only some methods. Bun points at `bun:jsc` for profiling |
| `node:vm` | `vm.Script`, contexts, and ES modules implemented. Still not a security boundary (same as Node) |
| `node:tls` | Missing `tls.createSecurePair` |
| `node:repl` | Result previews, some tab-completion, and V8-specific error wording differ |
| `node:domain` | Missing `Domain.active` |
| `node:process` | `process.binding` (internal, some packages depend on it) only partially implemented |
| `node:test` | Missing `run()`, snapshot testing, `mock.module()`, coverage. Use `bun:test` instead |

### Fully implemented (safe to rely on)

`node:assert` · `node:buffer` · `node:console` · `node:dgram` · `node:diagnostics_channel` · `node:dns` · `node:events` · `node:fs` · `node:http` · `node:net` · `node:os` · `node:path` · `node:querystring` · `node:readline` · `node:stream` · `node:string_decoder` · `node:trace_events` · `node:tty` · `node:url` · `node:zlib`

### Absent entirely

| Module | Symptom | Use instead |
|---|---|---|
| `node:sqlite` | `require("node:sqlite")` throws `No such built-in module: node:sqlite` (verified 1.3.13). Node 22+ has it, so ported code breaks at import | `bun:sqlite` — different API, same engine |

### Native addons (Node-API)

`.node` files load through `require()` and `process.dlopen()`, and most Node-API addons work. Two things the table does not tell you:

1. Addons built against the **V8 C++ API** rather than Node-API (NAN-era addons, and anything linking V8 symbols) are the high-risk class.
2. **Loading is not using.** Verified on Bun 1.3.13 with `better-sqlite3@13.0.3`: `require()` returned normally with exit 0, and `new Database(":memory:")` aborted the process with **exit 133** and a crash-report URL. The same `node_modules` worked under Node. An abort is not catchable — `try/catch` does not help.

Inventory before you commit to Bun:

```bash
find node_modules -name '*.node' -not -path '*/prebuilds/*' | sed 's|node_modules/||' | cut -d/ -f1 | sort -u
```

Every name that comes back needs an exercise-it assertion below.

## The startup probe

Run this as a CI step and as the first thing your service does on boot. It fails the process on a gap instead of waiting for the request that finds it. Adapt the `checks` list to your dependencies — the point is the shape, not the specific entries.

```ts
// scripts/bun-compat-probe.ts — run with: bun run scripts/bun-compat-probe.ts
type Check = { name: string; run: () => unknown | Promise<unknown>; expect: (v: unknown) => boolean };

const checks: Check[] = [
  // Behaviour, not presence: does the ESM loader hook actually resolve?
  {
    name: "module.register hooks run",
    run: async () => {
      const { register } = await import("node:module");
      register("./compat-hook.mjs", import.meta.url);
      return (await import("virtual:probe")).default;
    },
    expect: (v) => v === 42,
  },
  // Native addon: construct and query, do not just require.
  {
    name: "better-sqlite3 opens and queries",
    run: () => {
      const Database = require("better-sqlite3");
      return new Database(":memory:").prepare("select 1 as x").get().x;
    },
    expect: (v) => v === 1,
  },
  // Worker limits are silently ignored on Bun — assert rather than assume.
  {
    name: "worker resourceLimits are honoured",
    run: async () => {
      const { Worker } = await import("node:worker_threads");
      const w = new Worker("", { eval: true, resourceLimits: { maxOldGenerationSizeMb: 64 } });
      const limits = w.resourceLimits;
      await w.terminate();
      return limits;
    },
    expect: (v) => v != null,
  },
];

let failed = 0;
for (const c of checks) {
  try {
    const value = await c.run();
    if (c.expect(value)) console.log(`ok   ${c.name}`);
    else { console.error(`FAIL ${c.name} — got ${JSON.stringify(value)}`); failed++; }
  } catch (err) {
    console.error(`FAIL ${c.name} — threw ${(err as Error).message}`);
    failed++;
  }
}
process.exit(failed ? 1 : 0);
```

```js
// scripts/compat-hook.mjs
export async function resolve(spec, ctx, next) {
  if (spec === "virtual:probe") return { url: "data:text/javascript,export default 42", shortCircuit: true };
  return next(spec, ctx);
}
```

**A check that aborts the process (exit 133, or a Bun crash-report URL) is itself the finding.** It will not print `FAIL`, because the probe is gone. Run each risky native-addon check as its own `bun run` invocation and inspect the exit code, so an abort is attributable.

Two rules the obvious version of this loop breaks:

- **`require()` is not a probe.** `better-sqlite3@13.0.3` on Bun 1.3.13 returns from `require()` with exit 0 and then aborts with exit 133 on `new Database(":memory:")`. A load-only loop reports green on the exact dependency that takes production down. Each probe script must do the real work — open and query the database, decode and transform an image, render the canvas.
- **A bare `|| echo` loop exits 0.** The loop's status is the status of the last `echo`, so CI marks the step passed and prints the failure into logs nobody reads. Accumulate and exit non-zero.

```bash
# scripts/probe-native.sh — one probe file per addon, each doing real work
failed=0
for probe in scripts/probe-better-sqlite3.ts scripts/probe-sharp.ts scripts/probe-canvas.ts; do
  if bun run "$probe"; then
    echo "ok   $probe"
  else
    echo "FAIL $probe (exit $?)"   # 133 == abort, the addon is a blocker
    failed=1
  fi
done
exit $failed
```

## Migration checklist

1. **Inventory native addons** with the `find` command above. Each one gets an exercise-it check.
2. **Run the full existing test suite under Bun**, not a subset. Expect to fix test isolation first — `bun:test` shares one process across files, so suites that relied on Jest's per-file workers will surface leaked state (see the main skill, footgun 8).
3. **Run both runtimes in CI** on the same matrix until Bun has been green for a full release cycle.
4. **Confirm the observability agent works.** ESM loader hooks are a no-op, which is how several tracing agents auto-instrument. Assert that a span actually reaches your collector from a Bun process — do not accept "the SDK initialised without error".
5. **Re-verify after every Bun upgrade.** These gaps move in both directions: a stub becomes real, and occasionally a behaviour changes. The probe is a permanent CI step, not a one-off migration task.
6. **Pin the exact Bun version** in CI and in the container image. There is no LTS line to fall back on.
