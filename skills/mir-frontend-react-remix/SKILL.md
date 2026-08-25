---
name: mir-frontend-react-remix
description: "Make It Right (React Router module). React Router v8 Framework Mode mechanics — the meta-framework formerly shipped as Remix, hence the directory name. Carries loader/action authorization (re-authenticate every call; ownership predicate in the WHERE clause), clientLoader/clientAction and what runs where, the middleware chain that became default in v8 and why centralising auth there hides a hole; single-fetch serialization boundaries, useFetcher race semantics, headers/shouldRevalidate, redirect()/data() throw-vs-return, .server/.client bundle leaks, SPA vs SSR vs prerender, and the v7→v8 migration: ESM-only, Node 22.22+, React 19.2.7+, a coupled Vite major, and react-router-dom removed. Chains: mir-frontend → mir-frontend-react → this. TRIGGER only when the React meta-framework is React Router v7/v8 Framework Mode or Remix v2 — @react-router/dev in package.json, routes.ts, react-router.config.ts, a route module exporting loader/action/clientLoader/middleware, or any React Router data or revalidation question. SKIP for Next.js (mir-frontend-react-next), TanStack Start, Astro, a plain Vite React SPA, Nuxt and SvelteKit; for Remix 3, a separate non-React framework on a forked Preact — not covered here or by the React tier; for React-general reactivity — Rules of Hooks, derived state, stale closures, Suspense placement and Compiler interop live in mir-frontend-react; and for the generic UI gates in mir-frontend."
trigger: /mir-frontend-react-remix
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-frontend-react-remix · Make It Right (React Router, Framework Mode)

Bottom tier of the chain: `mir-frontend` (generic gates) → `mir-frontend-react` (React reactivity) → **this** (React Router library mechanics). Run the gates first. Reach for this at Gate 0 (render-model fitness), Gate 5 (rendering ownership), Gate 6 (implementation), and Gate 7 (review). **React-level rules — Rules of Hooks, derived state, stale closures, list keys, Suspense placement, Compiler interop — live in `mir-frontend-react`, not here.**

**Why the directory says "remix".** The product is **React Router in Framework Mode**. Remix v1/v2 and React Router merged into one package in Dec 2024, and v8 marked Remix v2 End of Life. The slug is kept because the repo's planned-not-written list, `README.md`, `EXTENDING.md` and two `mir-frontend` files already name it, and renaming buys nothing: **the description is the router, not the slug**, and this skill's description leads with "React Router". "Remix 3" is a *different product* and is deliberately out of scope — see SKIP and footgun 12.

**Stack assumed. Versions verified against the npm registry, reactrouter.com and OSV on 25 Aug 2026.** Framework Mode only; Declarative Mode (`<BrowserRouter>`) and Data Mode (`createBrowserRouter`) get the pillar and the React tier, not this module's loader/action material.

| Release | Status | Notes |
|---|---|---|
| React Router **8.3.0** (22 Jul 2026) | current stable · **minimum patch floor** | ESM-only (`"type": "module"`) · Node ≥ 22.22.0 · react/react-dom ≥ 19.2.7 · Vite 7 or 8 · TypeScript ≥ 5.1 |
| React Router **7.18.2** (28 Jul 2026) | maintained — security updates only | The floor if you are still on v7. Do not sit below it |
| React Router 6.x · Remix v2 (`@remix-run/*` 2.17.5) | **End of Life** | v8's release post: v6 and Remix v2 "will no longer be receiving security updates" |
| `remix@3.0.0-beta.10` (17 Aug 2026) | beta, **different product** | Zero-dependency, Node ≥ 24.3, no `react` dependency at all. Not this module, not the React tier |

**Removed or changed in v8** — `react-router-dom` (no 8.x exists; import from `react-router`, DOM-only APIs from `react-router/dom`); `cloudflareDevProxy()` (use `@cloudflare/vite-plugin`); `meta`/`useMatches()` argument `data` → `loaderData`; `@react-router/architect` now reads `event.requestContext.domainName` instead of `X-Forwarded-Host`. Future flags that became mandatory: `v8_middleware`, `v8_viteEnvironmentApi`, `v8_passThroughRequests`, `v8_trailingSlashAwareDataRequests`; `v8_splitRouteModules` became the top-level `splitRouteModules` option. The team moved to a yearly major cadence — check the current major before quoting this table.

## The React Router footguns AI walks into most

### 1. Every `loader` and `action` is a public HTTP endpoint
A route with a `loader` is fetchable as `GET /that/route.data`. A route with an `action` accepts a POST. Neither the component that renders above it nor the `<Link>` you did not draw is a gate. **The route module is the security boundary.**

```ts
export async function action({ request, params }: Route.ActionArgs) {
  const user = await requireUser(request)                    // 1. authenticate, every call
  const form = await request.formData()
  const title = form.get("title")
  if (typeof title !== "string" || !title) {                 // 2. TS types are erased at runtime
    throw data({ error: "Bad input" }, { status: 400 })
  }
  const { count } = await db.post.updateMany({               // 3. ownership IN the write,
    where: { id: params.postId, authorId: user.id },         //    not a separate read first
    data: { title },
  })
  if (count !== 1) throw data(null, { status: 404 })
  return { ok: true }                                        // 4. not the DB row
}
```

**Put the ownership predicate in the `WHERE` clause of the write, not in an `if` above it.** `findUnique` → check → `update({where:{id}})` leaves a window where ownership changes between the two statements, and it leaves a write scoped only by an attacker-supplied param if anyone later deletes the check. Read first only when you need the row's contents; then still scope the write.

### 2. Middleware is default in v8 — and centralising auth there can hide the hole
`v8_middleware` is no longer a flag. `export const middleware` gives you a parent→child chain around loaders and actions, with `next()` in the middle and typed `context.set()` / `context.get()`.

**Server middleware only runs for document requests and `.data` requests.** A client-side navigation to a route with **no loader** makes no `.data` request, so its server middleware never runs. The documented fix is a loader that exists purely to force the round trip:

```ts
export async function loader() { return null }   // makes the server middleware actually run
```

Two more edges. Server `context` is **request-scoped**: an SPA form submission is a POST and then a separate GET, so nothing set during the POST is visible in the revalidation (`clientMiddleware` is per-navigation and does not have this problem). And `next()` **never throws** — a handler error comes back as a 500 `Response`, so a middleware that assumes success will happily post-process an error page.

**Centralisation becomes obscurity.** A `requireUser()` call inside each loader is three characters longer and impossible to skip by accident. A root middleware that authenticates looks like it covers every route, and the exceptions above are exactly where reviewers stop looking. Use middleware for cross-cutting *effects* — logging, timing, response headers, seeding a request-scoped DB handle — and keep the authorization call in the handler that reads the data. If you do centralise, write down in the Gate 5 design which routes have no loader.

### 3. `clientLoader` / `clientAction` — know what runs where
| Export | Runs | Serialized? |
|---|---|---|
| `loader` / `action` | server only | yes — see footgun 4 |
| `clientLoader` / `clientAction` | browser only | no |
| `middleware` | server only | — |
| `clientMiddleware` | browser only | — |
| `headers` / `links` / `meta` | server (document render) | — |
| default component / `ErrorBoundary` | both | — |
| `HydrateFallback` | browser, initial load only | — |

On the **initial** SSR load a `clientLoader` does not run at all unless you set `clientLoader.hydrate = true as const` — and once you do, the route needs a `HydrateFallback` or it renders nothing while the client loader runs. `serverLoader()` inside a `clientLoader` is a real network call to the `.data` endpoint, not a local function.

**Never let an authorization decision live only in a `clientLoader` or `clientAction`.** They are client code; the `.data` endpoint behind them is still open. This is the React tier's "client authz is a hint" rule with a React Router-shaped trapdoor.

### 4. Single fetch: one `.data` request, one serialization boundary
Framework Mode batches every loader on the page into a single `.data` request serialized with React Router's vendored turbo-stream. `Promise`, `Date`, `Map`, `Set`, `BigInt`, `RegExp`, `URL`, `Error` and `undefined` survive; class instances, functions and getters do not — they arrive as plain objects or vanish.

Returning a promise (rather than awaiting it) streams that value in later; the component reads it with `<Await>` or React's `use()`. That is the deferred-data path, and it is the fix for one slow query holding the whole route.

**Everything any loader on the route returns is in that payload and readable in the browser** — including fields the component never renders. Return a narrow DTO. `db.user.findUnique()` returns `passwordHash` unless you tell it not to.

### 5. `redirect()` and `data()` — return versus throw is a real decision
`redirect()` builds a `Response`. **Returning it works only from the loader/action itself.** Inside a helper, `return redirect(...)` hands a Response back to the caller, which keeps running — the classic "my auth guard doesn't guard" bug. Throw it.

```ts
async function requireUser(request: Request) {
  const user = await getUser(request)
  if (!user) throw redirect("/login")     // GOOD — non-local exit; caller cannot continue
  return user
}
```

`throw data(obj, { status })` sends the ErrorBoundary a typed error you can narrow with `isRouteErrorResponse()`; `return data(obj, { status, headers })` sends normal loader data with a status and headers attached. `redirectDocument()` forces a full document load — use it when the destination is outside the router, and after anything that changes the session or the deployed build.

### 6. `shouldRevalidate` — the default is revalidate-everything
After any action, **every loader on the page re-runs**. That default is correct and expensive. Narrowing it is a correctness decision, not an optimization:

```ts
export function shouldRevalidate({ formAction, defaultShouldRevalidate }: Route.ShouldRevalidateArgs) {
  if (formAction === "/prefs/theme") return false       // this mutation cannot affect this route
  return defaultShouldRevalidate                        // ALWAYS fall through to the default
}
```

`return false` at the top is how a mutation silently stops appearing. Anything that ignores `defaultShouldRevalidate` also opts out of `useRevalidator()` and of the revalidation after an error — both of which you wanted.

### 7. `useFetcher` — concurrency that is nearly, but not entirely, handled
React Router mirrors browser behaviour: a new navigation or submission cancels the in-flight one. Fetchers are not singletons, so **fetchers do not interrupt each other, but a fetcher interrupts itself** — `fetcher.submit()` cancels that fetcher's pending request, which is what makes the type-ahead pattern correct without an AbortController. Among concurrent revalidations, React Router commits fresh responses and drops ones that started earlier than an already-committed one.

Two things it does **not** do. Cancellation is client-side only: **the server already ran the action.** Double-submit, retry and offline-replay therefore need an idempotency key on the server (`mir-backend` owns that). And `useFetcher()` with no `key` is component-scoped, so a fetcher inside a list row is per-row — pass an explicit `key` when two components must observe the same submission, and expect stale `fetcher.data` to persist after the row unmounts and remounts.

### 8. `headers` — the route that caches is the route that leaks
`headers` runs server-side on the document response. In a nested route the deepest matching route's `headers` wins unless you merge `parentHeaders` yourself, so a `Cache-Control` set on a layout can be dropped by a leaf that also exports `headers`, and vice versa.

A `Cache-Control: public, max-age=…` on a personalised route caches one user's HTML at the CDN and serves it to the next. Personalised routes get `private, no-store`; add `Vary` for anything that varies on a header. `.data` responses are separately cacheable — reason about both.

### 9. `.server` / `.client` and the route-module bundle leak
`app/db.server.ts` is stripped from the client bundle; `*.client.ts` is stripped from the server bundle. The suffix must be on the **filename**.

The leak is not usually a `.server` mistake — it is a route module. React Router tree-shakes `loader`, `action`, `headers` and `middleware` out of the client build (`splitRouteModules` is now on by default), but that only reaches the exports. A top-level `import { db } from "~/db"` in a route module, where `db.ts` has no `.server` suffix, pulls the driver and its config into the browser bundle. Rename it to `db.server.ts` and let the build fail loudly instead.

**Verify by grepping the built client output, not the source.** `grep -RIn "postgres://\|BEGIN PRIVATE KEY\|sk_live" build/client/` in CI. `splitRouteModules` is a bundling optimization, not a security control.

### 10. `ssr`, `prerender`, SPA mode — pick before you write loaders
`react-router.config.ts` decides the whole shape:

| Config | What you get | What breaks if you switch later |
|---|---|---|
| `ssr: true` (default) | server render + `.data` requests | — |
| `ssr: false` (SPA mode) | one prerendered `index.html`, **no server at runtime** | Every `loader` and `action` stops existing. `shouldRevalidate` starts behaving like Data Mode |
| `prerender: [...]` | HTML built at build time for those paths | Loaders run at build time — no request, no cookies, no per-user data |

SPA mode is not "SSR with a flag off". Choosing it after the app has loaders is a rewrite of the data layer. Decide at Gate 0 against `mir-frontend/references/rendering-model-map.md`, and put the answer in the Assumption Ledger.

### 11. Nested routes load in parallel — until you serialize them by hand
Every matched route's `loader` runs **in parallel**, which is the framework's main structural advantage over a nested-layout `await` chain. Three habits throw it away:

- **`await serverLoader()` inside a `clientLoader`** turns one batched `.data` request into a second round trip, after the first one finished.
- **A parent loader that returns an id the child loader then fetches with.** The child is now serial behind the parent. Read the param in both, or move the join into the parent.
- **`await parentData` / `useRouteLoaderData` used as a fetch input.** Same shape, less visible.

Route-level `<Suspense>` is not the tool here — return the slow promise from the loader instead of awaiting it (footgun 4) and render it with `<Await>` or `use()`, so the shell streams while the slow part resolves.

`<Link prefetch="intent">` starts the loader on hover or focus and usually removes the perceived navigation delay outright; `prefetch="render"` prefetches every rendered link, which on a long list is a self-inflicted load test on your own loaders. Prefetch fires the **real** loader — a prefetched route with a side-effecting loader runs that side effect on hover.

### 12. `ErrorBoundary` is per route module, and a missing one bubbles to root
Any route module export that throws — `loader`, `action`, `middleware`, the component — renders the nearest route `ErrorBoundary`. With none in between, that is the root, and the entire page becomes the error page: this is the React tier's granular-boundary rule expressed through file placement rather than JSX.

```tsx
export function ErrorBoundary({ error }: Route.ErrorBoundaryProps) {
  if (isRouteErrorResponse(error)) return <NotFound status={error.status} />  // thrown data()/Response
  return <GenericError />                                                     // real exception
}
```

Two consequences worth designing for: a route that owns a risky read should own an `ErrorBoundary` so its siblings survive, and a leaf `ErrorBoundary` still renders **inside** its parent layout, so the layout's own loader data must be valid for the error page to render at all.

### 13. `routes.ts` and generated types — the types are a build artifact
Routes are declared in `app/routes.ts` (`route()`, `index()`, `layout()`, `prefix()`), or file-based via `@react-router/fs-routes`. `react-router typegen` writes `.react-router/types/**`, which is what `Route.LoaderArgs`, `Route.ComponentProps` and friends resolve to.

Those types are **generated**, so they go stale: add a route or change a param and the old types keep type-checking until typegen re-runs. `react-router typecheck` runs typegen first — use it in CI rather than bare `tsc`. `.react-router/` is generated output; it belongs in `.gitignore`, and `tsconfig.json` needs the `rootDirs` entry pointing at it or every `Route.*` import fails to resolve on a clean clone.

### 14. RSC in React Router is **unstable** — do not design around it
This is the surface everyone asks about, so state it plainly rather than omitting it. As of 8.3.0 every React Router RSC API is `unstable_`-prefixed (`unstable_reactRouterRSC`, `unstable_matchRSCServerRequest`, `unstable_RSCHydratedRouter`, …), it requires `@vitejs/plugin-rsc`, which `@react-router/dev` pins as a peer at `~0.5.26` and which has not reached 1.0, and the official docs say support "is experimental and subject to breaking changes in minor/patch releases."

Two 2026 advisories are reachable **only** through those unstable paths (see Security). If a design needs stable RSC today, that is a reason to choose a different meta-framework — record it at Gate 5 as a decision, not a discovery. If you adopt it anyway, ledger it as an accepted risk with a named owner watching every patch release.

### 15. Remix 3 is not this, and it is not React
`remix@3.x` (beta) is a separate framework: no `react` dependency, a forked Preact renderer, its own `@remix-run/*` server primitives, Node ≥ 24.3. It is not a React meta-framework, so it belongs under neither this module nor `mir-frontend-react`. If the repo has `remix@3`, say so and run `mir-frontend` alone. The detection signal for *this* module is **`@react-router/dev`** in `package.json` — not `react-router`, which is a transitive dependency of a great many React apps.

## How this slots into the pipeline

- **Gate 0 (render-model fitness):** state `ssr`, `prerender` and whether SPA mode is on, and whether the app is Framework, Data or Declarative Mode. Half of this file only applies to Framework Mode.
- **Gate 5 (rendering ownership):** name every route's `loader` / `clientLoader` split and what runs where; name where authorization is enforced for each route (handler or middleware — and if middleware, which routes have no loader); name the revalidation behaviour of each mutation. Read `references/loaders-actions-and-middleware.md`.
- **Gate 6 (implementation):** code against footguns 1–15. Every `loader` and `action` follows the template in `references/loaders-actions-and-middleware.md`. Upgrades and deploy-target changes follow `references/build-and-migration.md`.
- **Gate 7 (review):** the security-reviewer works the Security section below — loader/action authorization and the single-fetch payload are the two most commonly missed. The reliability-reviewer checks 5, 6, 7 and 12; the frontend-perf-reviewer checks 4 (deferred data), 6 and 11.

## References

- `references/loaders-actions-and-middleware.md` — the loader/action authorization template, runtime argument validation, the middleware chain and its gaps, `clientLoader`/`clientAction` placement, single-fetch serialization, `headers`/`shouldRevalidate`, `useFetcher` concurrency, `redirect()`/`data()` semantics, sessions and CSRF.
- `references/build-and-migration.md` — the v7→v8 migration order, the ESM-only fallout, the import map, the coupled Vite major, `.server`/`.client` bundle rules, SPA vs SSR vs prerender, and the deploy adapters.

## Security

React Router library mechanics. React-level rules (raw-HTML props, LLM output, secrets in the bundle) are in `mir-frontend-react`; framework-agnostic browser security (CSP, clickjacking, third-party scripts) is in `mir-frontend`. **The `react-server-dom-*` advisory cluster is owned by `mir-frontend-react`** — read it there; it applies here whenever the unstable RSC path is enabled.

**Patch floor: `react-router` 8.3.0, or 7.18.2 on the v7 line. `@react-router/node` ≥ 7.9.4. `vite` ≥ 8.0.16 / 7.3.5 / 6.4.3.** Anything below is unpatched.

**React Router 6.x and Remix v2 are End of Life and receive no security fixes.** That is a security fact, not a roadmap note: a v6 or `@remix-run/*` app inherits every future advisory permanently. (A `react-router@6.30.6` was published 18 Aug 2026, after the EOL statement — treat the announced policy as the plan and do not budget on further v6 patches.)

| Advisory | Affected | Fixed in | What it does |
|---|---|---|---|
| **CVE-2026-42211** (`GHSA-49rj-9fvp-4h2h`, 8.1 HIGH) | 7.0.0 – 7.14.1, Framework Mode | 7.14.2 | Vendored turbo-stream v2 deserialization invokes arbitrary constructors → unauthenticated RCE. Needs a prototype-pollution primitive in your app to land |
| **GHSA-qwww-vcr4-c8h2** (HIGH) | 7.12.0 – 7.18.1, **8.0.0 – 8.2.x** | 7.18.2 / **8.3.0** | CSRF bypass in the **unstable RSC** path — the action executes before the 400 is returned. The only advisory so far to hit the 8.x line |
| **CVE-2026-53663** (`GHSA-84g9-w2xq-vcv6`) | 7.12.0 – 7.15.0; `@remix-run/server-runtime` 2.17.3 – 2.17.4 | 7.15.1 / 2.17.5 | CSRF via `PUT`/`PATCH`/`DELETE` document requests |
| **CVE-2026-22030** (`GHSA-h5cw-625j-3rxh`) | 7.0.0 – 7.11.x; `@remix-run/server-runtime` < 2.17.3 | 7.12.0 / 2.17.3 | CSRF in action / Server Action request processing. CVE-2026-53663 and GHSA-qwww are its follow-ups — treat this as a **class** |
| **CVE-2025-61686** (`GHSA-9583-h5hc-x8cw`, **CRITICAL**) | `@react-router/node` < 7.9.4; `@remix-run/node`/`-deno` < 2.17.2 | 7.9.4 / 2.17.2 | Path traversal in `createFileSessionStorage()` — only with an **unsigned** cookie |
| **CVE-2026-55685** · **CVE-2026-42342** · **CVE-2026-34077** | 7.x below 7.18.0 / 7.15.0 / 7.14.0 | see each | Unauthenticated DoS: inefficient route matching and unbounded path expansion on the `__manifest` endpoint (Framework Mode only), and reflected input in single-fetch |
| **CVE-2026-53669** · **CVE-2026-53668** · **CVE-2026-40181** · **CVE-2025-68470** | 6.0.0 – 7.17.x across the set | 7.18.0 / 7.13.0 / 7.14.1 / 7.9.6 | **Open-redirect → XSS class.** Backslash in `<Link>`/`useNavigate`, `//`-prefixed protocol-relative paths, untrusted redirect targets. Four bypasses of each other |
| **CVE-2026-53667** · **CVE-2026-33245** · **CVE-2026-33244** · **CVE-2026-53666** | 7.5.1 – 7.17.x | 7.18.0 / 7.13.2 | XSS: missing protocol validation in the RSC error handler, `javascript:` targets in unstable RSC redirects, an unescaped `Location` header in prerendered redirect HTML, and constructor injection in `deserializeErrors()` during SSR hydration |
| **CVE-2026-21884** · **CVE-2025-59057** | `@remix-run/react` < 2.17.3 / < 2.17.1; 7.x below 7.12.0 / 7.9.0 | 7.12.0 / 2.17.3 | SSR XSS in `ScrollRestoration`, and a second XSS. **No fix exists for Remix v2 past 2.17.5** |

**The open-redirect / XSS class is the one to design against.** Four advisories in 2026 were bypasses of each other's fixes. Never pass a user-supplied string to `redirect()` or `<Link to>`. Validate with `new URL(value, origin)` and require the resolved `origin` to equal yours *and* the pathname not to start with `//` or `\`. An allow-list of known internal paths beats any sanitizer here.

**The Vite dev server is now a hard prerequisite, so its advisories are yours.** `@react-router/dev` 7.x accepted Vite 5–8; 8.x accepts only 7 or 8, so most v7→v8 upgrades drag one or two Vite majors along with them (see `references/build-and-migration.md`). Vite's `server.fs.deny` has been bypassed at least a dozen times since 2023, four of them in 2026: `GHSA-fx2h-pf6j-xcff` (Windows alternate paths, HIGH, fixed 8.0.16 / 7.3.5 / 6.4.3), `GHSA-v2wj-q39q-566r` (bypass with queries, HIGH, 8.0.5 / 7.3.2), `GHSA-p9ff-h696-f583` (arbitrary file read over the dev WebSocket, HIGH, 8.0.5 / 7.3.2 / 6.4.2), `GHSA-4w7w-66w2-5vf9` (path traversal in optimized-deps `.map`). Assume another. **Never expose the dev server** — no `--host`, no `server.host: true` on a shared network, no tunnelling it for a demo. `server.fs.deny` is defence in depth, not a boundary.

**Sessions and cookies.** `createCookieSessionStorage` stores the session **in the cookie** — it is signed, not encrypted, and the client can read it. Never put a role, a permission set or PII there; store an id and re-read the record server-side. Always pass `secrets` (rotate by prepending; older entries still verify). Set `httpOnly`, `secure`, `sameSite: "lax"`, an explicit `path` and a `maxAge`. An unsigned cookie is what turned CVE-2025-61686 from a bug into a critical.

**CSRF.** React Router does not ship CSRF protection. `SameSite=Lax` blocks cross-site POST with cookies, which is most of it — but three advisories in this table are CSRF, so add an explicit defence on every state-changing route: an origin check in middleware (`Origin` against your own host), or a double-submit token in the form and the session. Re-authorize in the action regardless; CSRF defence is not authorization.

**Error and payload hygiene.** In production React Router replaces server error messages with a generic string before they reach the client — do not defeat that by returning `err.message` from a `try`/`catch` in a loader. Everything a loader returns is in the `.data` payload whether the component renders it or not.

**Supply chain.** Commit the lockfile, install with `npm ci`. `react-router` and every `@react-router/*` package must resolve to the same version — verify with `npm ls react-router @react-router/dev`. A transitive constraint can hold you below the floor with no warning. `react-router-dom` is not published for 8.x, so a stray `react-router-dom` in the tree after an upgrade means an unmigrated dependency pinning you to the v7 line.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY React Router Framework Mode mechanics.** Apply the placement test before adding anything:

1. True for Vue and Angular too (any reactive UI)? → **up** to `mir-frontend`.
2. True for every React meta-framework because they all run React's reactivity model (hook rules, derived state, stale closures, keys, Suspense placement, Compiler interop, the `react-server-dom-*` advisories)? → **up** to `mir-frontend-react`.
3. A mechanical footgun of *React Router* (route modules, loaders/actions, middleware, single fetch, `shouldRevalidate`, fetchers, `react-router.config.ts`, the adapters)? → **here**.
4. A *different* React meta-framework (Next.js, TanStack Start, a Vite React SPA)? → its own module. Next.js is `mir-frontend-react-next`; the others are unwritten. Never widen this one.
5. **Remix 3?** → not a React meta-framework at all. It gets its own top-level module if it ever earns one; it does not go here and it does not go under `mir-frontend-react`.

Full layered edit map: `mir-frontend/SKILL.md` → "Where these instructions live".
