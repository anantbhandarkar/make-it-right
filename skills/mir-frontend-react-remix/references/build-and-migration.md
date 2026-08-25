# Build, bundling, migration, and deploy

Read at **Gate 0** (render-model fitness) and **Gate 6** (implementation), and before any version bump. Versions verified against the npm registry, reactrouter.com and OSV on **25 Aug 2026**.

---

## 1. Where you are, and where the floors sit

| Line | Current | Status |
|---|---|---|
| `react-router` 8.x | **8.3.0** (22 Jul 2026) | current stable; also the **security floor** for 8.x |
| `react-router` 7.x | **7.18.2** (28 Jul 2026) | maintained, security fixes only; the floor if you stay on v7 |
| `react-router` 6.x | 6.30.x | **End of Life** — v8's release announcement ended security updates |
| `@remix-run/*` v2 | 2.17.5 (1 Jun 2026) | **End of Life** — same announcement |
| `remix` 3.x | `3.0.0-beta.10` (17 Aug 2026) | a different framework. Not React. Out of scope here |

A `react-router@6.30.6` was published on 18 Aug 2026, after the EOL statement. Treat the announced policy as the plan: **do not budget on further v6 patches.** Migration off v6 and off Remix v2 is a security task with a deadline that has already passed, not a refactor.

**v8 requirements:** Node **≥ 22.22.0**, react and react-dom **≥ 19.2.7** (`react-dom` is an optional peer), Vite **7 or 8**, TypeScript **≥ 5.1** (`@react-router/dev` 8.3.0 accepts `^5.1 || ^6 || ^7`). `react-router` 8.x is `"type": "module"` — **ESM only**.

All `@react-router/*` packages ship in lockstep at 8.3.0: `dev`, `node`, `serve`, `express`, `architect`, `cloudflare`, `fs-routes`, `remix-routes-option-adapter`. They must all resolve to the same version — `npm ls react-router @react-router/dev`.

---

## 2. v7 → v8 migration, in order

The upgrade is designed to be boring *if* you land the flags first. Do these in separate commits; each one is independently revertable.

1. **Get to the latest v7 (7.18.2) and green.** This also clears every v7-line advisory in one step.
2. **Turn on each `future` flag one at a time, on v7**, shipping between each: `v8_middleware`, `v8_viteEnvironmentApi`, `v8_splitRouteModules`, `v8_passThroughRequests`, `v8_trailingSlashAwareDataRequests`. Doing all five with the major bump means a broken app with five candidate causes.
   - `v8_passThroughRequests` — the raw HTTP request reaches your handlers; `.data` URL normalization is gone. Anything that parsed `request.url` for a `.data` suffix breaks here.
   - `v8_trailingSlashAwareDataRequests` — the `.data` URL format changes to `/_.data`. Breaks CDN rules, WAF rules, and log dashboards keyed on the old shape.
   - `v8_middleware` — middleware becomes available; see the loaders reference for the gaps it does not cover.
3. **Bump Node to ≥ 22.22.0** in CI, the Dockerfile, `engines`, `.nvmrc`, and the deploy target. Do this before the package bump so a Node failure is unambiguous.
4. **Bump Vite** to 7 or 8 — see §3. This is the real friction, not React Router.
5. **Bump `react-router` and every `@react-router/*` to 8.3.0** and remove `react-router-dom`.
6. **Rewrite the imports** (§4).
7. **Fix the non-flag breaks:** `meta` and `useMatches()` now receive `loaderData`, not `data`. `@react-router/architect` reads `event.requestContext.domainName` instead of `X-Forwarded-Host` — if you were relying on the forwarded host behind a proxy, this changes the origin your CSRF and redirect checks see. `cloudflareDevProxy()` is gone; use `@cloudflare/vite-plugin`'s `cloudflare()`.
8. **`react-router typegen`, then `react-router typecheck`,** and commit the resulting type fixes.

`splitRouteModules` moved from `future` to a top-level config option; it is on by default.

---

## 3. The coupled Vite major — the real cost of this upgrade

**You are doing two majors at once.** `@react-router/dev` **7.x** accepted Vite `^5.1 || ^6 || ^7 || ^8` and Node ≥ 20; **8.x** accepts only `^7 || ^8` and Node ≥ 22.22.0. A v7 app sitting on Vite 5 or 6 — which was perfectly supported yesterday — must cross one or two Vite majors in the same window. That is where the time goes — Rollup 4→5 behaviour, plugin ecosystem lag, `resolve.conditions` and SSR externals changes, and CSS/asset handling — none of which React Router controls or documents.

Sequence it as: Vite bump alone → green → React Router bump. Never in the same commit. If a plugin has no Vite 8 build, the choice is made for you at that point, not after the router upgrade has also landed.

**Vite is now a security dependency of this stack, not just a dev tool.** `server.fs.deny` has been bypassed at least a dozen times since 2023 and four times in 2026 alone (Windows alternate paths, query-string bypass, dev-server WebSocket arbitrary file read, optimized-deps `.map` traversal). Floors: **vite ≥ 8.0.16 / 7.3.5 / 6.4.3**. Current stable is 8.2.2 (20 Aug 2026).

Assume a next bypass exists. **Never expose the dev server**: no `--host`, no `server.host: true` on a shared or café network, no tunnelling it for a demo or a screen share. `server.fs.deny` is defence in depth, not a boundary.

---

## 4. The import map

`react-router-dom` has no 8.x release — the line stops at 7.18.2. Its presence in an 8.x lockfile means some dependency is still pinning the v7 line.

| Was | Now |
|---|---|
| `import { Link, Form, useLoaderData, redirect, data } from "react-router-dom"` | `from "react-router"` |
| `import { RouterProvider, HydratedRouter } from "react-router-dom"` | `from "react-router/dom"` |
| `@remix-run/react` | `react-router` |
| `@remix-run/node`, `@remix-run/cloudflare` | `@react-router/node`, `@react-router/cloudflare` |
| `@remix-run/dev` | `@react-router/dev` |

Everything except the DOM-mounting entry points comes from `react-router`. `react-router/dom` holds the browser-only mounting APIs. A codemod handles the mechanical part; grep for the leftovers:

```bash
grep -rn "react-router-dom\|@remix-run/" app/ vite.config.* package.json
npm ls react-router-dom          # must be empty on 8.x
```

### ESM-only fallout

`react-router` 8.x is `"type": "module"` with no CJS build. What breaks:

- **`require("react-router")` from a CommonJS server, a Jest config, or a `.eslintrc.js`.** Move to `import`, or use a dynamic `await import()` in a context that allows it.
- **A CJS custom server** (`server.js` with `require("express")` and `require("@react-router/express")`). Convert to ESM: `"type": "module"` in `package.json`, or rename to `.mjs`, and replace `__dirname`/`require.resolve` with `import.meta.url` equivalents.
- **Jest without ESM support.** Vitest is the low-friction path here; it shares the Vite config you already maintain.
- **A bundler or test runner resolving the wrong `exports` condition.** `react-router` ships `development`, `module-sync` and `react-server` conditions. A tool with a stale `conditions` list can silently pick the production build in dev, or the `react-server` build in a client test.

TypeScript needs `"module": "ESNext"`-family settings with a bundler-aware `moduleResolution`; v8 moved its own config to ES2022 targets.

---

## 5. `.server` / `.client` and keeping secrets out of the bundle

- `*.server.ts` / `*.server.tsx` are stripped from the **client** bundle. `*.client.ts` from the **server** bundle. The suffix must be on the **filename**, not a directory.
- React Router removes `loader`, `action`, `headers` and `middleware` from the client build of a route module. That is an export-level transform: it does **not** remove a top-level `import { db } from "~/db"` whose module has no `.server` suffix. That import is the classic leak — the driver, its config, and often a connection string end up in the browser bundle.
- Rule: any module that touches a secret, a database, or a server-only SDK gets `.server` in its name, so a wrong import fails the build instead of shipping.
- Code needed on both sides goes in a plain module with no suffix and no secrets.
- `splitRouteModules` (now default) splits route exports into separate client chunks. It is a bundling optimization, **not** a security control.

**Verify against the built output, not the source:**

```bash
npm run build
grep -RIn "postgres://\|mysql://\|BEGIN PRIVATE KEY\|sk_live\|SERVICE_ROLE" build/client/ && exit 1
```

Put that grep in CI. Environment variables are the other half: anything the client reads is inlined by Vite at build time under the `VITE_` prefix and is therefore published — rotating one requires a rebuild, and every build already shipped still carries the old value. Read secrets in `.server` modules from `process.env` at request time.

---

## 6. `ssr`, `prerender`, SPA mode — decide at Gate 0

`react-router.config.ts`:

```ts
import type { Config } from "@react-router/dev/config"
export default {
  ssr: true,                                   // false = SPA mode
  prerender: ["/", "/about", "/blog/hello"],   // or an async function returning paths
} satisfies Config
```

| Mode | What ships | Loaders | Use when | Do NOT use when |
|---|---|---|---|---|
| `ssr: true` (default) | a server + client bundle | run per request | personalised or SEO-critical content, fresh data, auth-gated pages that must also render fast | there is no server budget, or the team cannot own a runtime |
| `prerender: [...]` | static HTML for those paths, server for the rest | run at **build time** — no request, no cookies, no user | marketing, docs, blogs; maximum CDN cacheability | anything per-user; anything whose loader reads the request |
| `ssr: false` (SPA) | one prerendered `index.html`, static hosting | **do not exist at runtime** — `clientLoader` only | app entirely behind auth, no SEO need, static hosting is a hard requirement | public/SEO pages; first paint matters on slow devices |

Switching later is not a config flip. Going `ssr: true` → `ssr: false` deletes every `loader` and `action` and forces the data layer into `clientLoader`/`clientAction` against a separate API, and moves the authorization boundary out of the app entirely. `shouldRevalidate` also changes semantics in SPA mode (Data-Mode behaviour: no revalidation on navigation, only after actions).

Cross-check the choice against `mir-frontend/references/rendering-model-map.md` at Gate 0 and ledger it at Gate 2.

---

## 7. Deploy targets and adapters

| Target | Package | Notes |
|---|---|---|
| Built-in Node server | `@react-router/serve` | The default `react-router-serve`. Fine for simple deploys; no custom middleware, no non-router routes |
| Express (custom server) | `@react-router/express` | Take this when you need your own middleware, websockets, or extra endpoints. **Must be ESM on v8** |
| Node primitives | `@react-router/node` | Session storage, file uploads. `createFileSessionStorage()` had a CRITICAL path traversal below 7.9.4 — sign your cookies |
| AWS via Architect | `@react-router/architect` | v8 reads `event.requestContext.domainName`, **not** `X-Forwarded-Host` — re-check origin-dependent logic after upgrading |
| Cloudflare Workers | `@react-router/cloudflare` + `@cloudflare/vite-plugin` | `cloudflareDevProxy()` was removed in v8. Workers constraints (no Node built-ins by default, CPU limits) are `mir-cloud`'s material |
| Static hosting | none — SPA mode or `prerender` | No server, so no `loader`/`action` |

Whatever the target: run the production build (`react-router build`), not the dev server. The dev server serves source, has the `server.fs.deny` history above, and returns full server errors to the client.

---

## 8. Upgrade verification checklist

```bash
node -v                                        # >= 22.22.0
npm ls react-router @react-router/dev          # all 8.3.0, single copy
npm ls react-router-dom                        # empty
npm ls react react-dom                         # >= 19.2.7, identical to each other
npm ls vite                                    # >= 8.0.16 / 7.3.5
npm audit --audit-level=high
npx react-router typegen && npx react-router typecheck
npm run build && grep -RIn "BEGIN PRIVATE KEY\|sk_live\|postgres://" build/client/
```

Then, by hand: one document request, one client navigation, one `<Form>` submission, one `fetcher.submit()`, one thrown `redirect()` from a guard, and one deliberate 404 — the six paths where a migration usually breaks quietly rather than loudly.
