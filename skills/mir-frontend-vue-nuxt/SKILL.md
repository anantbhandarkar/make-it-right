---
name: mir-frontend-vue-nuxt
description: "Make It Right (Nuxt module). Nuxt 4.5 universal-rendering mechanics layered on the Vue tier — the failures that exist only because the same component code runs once in Nitro and again in the browser: bare $fetch in setup causing a double fetch, useAsyncData vs useFetch and the key/payload deduplication rules, module-scope state as a CROSS-REQUEST USER-DATA LEAK on the server (and CVE-2026-71316, where cached-route payload extraction served one user's SSR data to the next visitor), server-only vs client-only values and the hydration mismatch they produce, payload bloat from over-fetching in asyncData (pick/transform), Nitro server routes and server middleware, runtimeConfig public vs private and exactly what ships in the client payload, auto-imports and their debugging cost, route middleware for auth and why client-side route middleware is never a security control (CVE-2026-53721 route-rule case bypass), and Nuxt plugin/module ordering. TRIGGER only when the Vue stack is Nuxt — building, reviewing, or debugging a Nuxt page, layout, composable, server/api route, server middleware, route middleware, plugin, Nuxt module, or nuxt.config. Always loads TOGETHER WITH mir-frontend (the gates) and mir-frontend-vue (Vue reactivity: ref vs reactive, watch vs computed, provide/inject); this module only adds Nuxt/Nitro library mechanics. SKIP for a plain Vite+Vue SPA, Vue with vue-router alone, Vuetify/Quasar/PrimeVue apps that are not on Nuxt, VitePress/Astro sites, and every non-Vue framework — React and Next.js go to mir-frontend-react and its modules."
trigger: /mir-frontend-vue-nuxt
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-frontend-vue-nuxt · Make It Right (Nuxt)

Bottom of the chain: `mir-frontend` (generic gates) → `mir-frontend-vue` (Vue reactivity) → **this** (Nuxt + Nitro mechanics). Run the gates first, load the Vue tier for reactivity, reach for this at Gate 5 (design), Gate 6 (implementation), and Gate 7 (review). **Vue-level concerns — `ref` vs `reactive`, `watch` vs `computed`, `provide`/`inject`, template refs — live in `mir-frontend-vue`, not here.**

**Stack assumed**, versions verified 13 Aug 2026: Nuxt **4.5.2** (npm `latest`) · Vue 3.5.x · vue-router 5 (since Nuxt 4.4) · Nitro via `@nuxt/nitro-server` 4.5.2 over `nitropack` 2.13.x + h3 1.15.x · Vite 8 (Rolldown) or Rspack 2 · unhead v3. Node `^22.19.0 || ^24.11.0 || >=26.0.0`.

- **Nuxt 3 reached end of life 31 July 2026** (3.21.11 is the last maintenance release). A Nuxt 3 app is an unpatched app — put that in the Gate 4 risk register, not in a footnote. **Version floor is 4.5.1**; anything below carries the July 2026 advisory set, including a cross-user data disclosure (see Security).
- **Nuxt 5 is not released.** `future.compatibilityVersion: 5` opts into its breaking changes early. Do not put that flag in a Gate 5 design without labelling it a preview.

## The Nuxt footguns AI walks into

### 1. Universal rendering — know what runs twice

Component `setup()` runs on the server to produce HTML, then runs **again** in the browser to hydrate. Anything non-deterministic between the two runs produces a hydration mismatch: Vue discards the server HTML for that subtree and re-renders, which costs a visible flash and, on a page with many mismatches, real INP.

| Code | Server | Client (hydration) | Client (later navigation) |
|---|---|---|---|
| `<script setup>` body | runs | runs | runs |
| `onMounted` | never | runs | runs |
| `plugins/x.ts` | runs | runs | — |
| `plugins/x.client.ts` | never | runs | — |
| `plugins/x.server.ts` | runs | never | — |
| `middleware/x.global.ts` | runs on first load | runs again on first load | runs |
| `server/**` | runs | never | never (HTTP call instead) |

Guard with `import.meta.server` / `import.meta.client`, not `typeof window`. Wrap genuinely browser-only UI in `<ClientOnly>` with a `#fallback` slot that reserves the same box, or the swap causes layout shift. Note the documented cost: CSS used only inside `<ClientOnly>` may not be inlined in the initial HTML.

### 2. Bare `$fetch` in setup fetches twice

`$fetch` in `<script setup>` runs on the server during render **and again** on the client during hydration. The Nuxt docs are explicit that it "will not provide network calls de-duplication and navigation prevention." Two requests, two spinners, and a mismatch if the second response differs.

```vue
<!-- BAD — one request during SSR, a second on hydration -->
<script setup>const user = await $fetch('/api/me')</script>

<!-- GOOD — fetched once on the server, transferred in the payload -->
<script setup>const { data: user } = await useFetch('/api/me')</script>
```

`$fetch` is correct in event handlers, in `server/` code, and inside a `useAsyncData` handler. It is wrong as the only fetch in setup.

### 3. `useFetch` vs `useAsyncData` — and the reactive-URL trap

`useFetch(url)` is documented as "nearly equivalent to `useAsyncData(url, () => event.$fetch(url))`". Use `useFetch` for a plain URL; use `useAsyncData` when the data comes from an SDK, a CMS client, or more than one call.

- **A string URL is captured once.** ``useFetch(`/api/item/${id.value}`)`` never refetches when `id` changes. Pass a getter — ``useFetch(() => `/api/item/${id.value}`)`` — or list `watch: [id]`.
- **`enabled`** (Nuxt 4.5) gates a request on a reactive condition instead of the old `if` + `refresh` dance.
- `server: false` skips the server run: the SSR HTML has no content for SEO or LCP. `lazy: true` keeps SSR but stops blocking navigation — you then render the `status` states yourself.
- Read `references/nuxt-data-fetching.md` for the right/wrong code and for cookie forwarding during SSR (`$fetch` sends none).

### 4. The `key` argument is the deduplication identity

Nuxt caches results under a key in `nuxtApp.payload.data` and reuses them on hydration. The two composables derive that key differently, and conflating them is how people both miss real collisions and invent fake ones:

| Call | Key comes from |
|---|---|
| `useAsyncData(key, handler)` | your string, exactly |
| `useAsyncData(handler)` — no key | **call site only** (file + line). Adding a line above it changes the key and drops the payload reuse, so the client refetches on hydration and the page flashes |
| `useFetch(url, opts)` | the resolved **URL + fetch options + call site** |

- **Two calls sharing a key share one cache entry.** The second call returns the first one's data. The realistic path to this is a keyless `useAsyncData` in a composable called from two places, or two `useAsyncData` calls you gave the same string.
- **Wrapping `useAsyncData` in your own composable and not passing a key** gives every caller the same call-site key — take an explicit key in that wrapper. A `useFetch` wrapper is safer, because different resolved URLs already produce different keys; give it an explicit key when you want callers to *share* one entry, or when identity depends on something not in the URL.
- `dedupe: 'cancel'` (default) aborts the in-flight request when a new one starts; `'defer'` keeps the pending one. `'cancel'` is what you want for a search box.
- `getCachedData` overrides the reuse rule. Its default reads `nuxtApp.payload.data` or `nuxtApp.static.data` during hydration; a custom one that ignores the user identity is how you build your own version of the leak in footgun 6.

### 5. Payload size — everything you fetch is inlined in the HTML

Whatever `useAsyncData` returns is serialized into the payload that ships inside the server-rendered document. Fetching a 400 KB list to render five fields adds 400 KB to **every** SSR response and to the parse cost before the page is interactive.

```ts
// BAD — full objects land in the HTML payload
const { data } = await useFetch('/api/users')

// GOOD — only the fields the template reads are serialized
const { data } = await useFetch('/api/users', { pick: ['id', 'name'] })
const { data } = await useFetch('/api/users', { transform: r => r.map(u => ({ id: u.id, name: u.name })) })
```

`pick` and `transform` do not stop the fetch — they stop the bytes reaching the client. They are also a containment step: an API returning `email`, `stripeCustomerId`, or `passwordResetToken` alongside the fields you wanted has published them to anyone who views source. Trim in the server route first; `pick`/`transform` is the second line.

### 6. Module-scope state on the server is a cross-request user-data leak

**This is the loudest rule in this file.** `mir-frontend-vue` states the general Vue-SSR form; this is the Nuxt form and the fix Nuxt actually gives you. Nitro is a long-lived process serving every user. A `ref` declared at module scope is created once per process, not once per request — so user B renders with user A's data.

```ts
// composables/useUser.ts
// CATASTROPHIC — one ref shared by every request the server ever handles
export const user = ref(null)

// GOOD — per-request state, keyed, serialized into that request's payload
export const useUser = () => useState('user', () => null)
```

The Nuxt docs say it directly: "Never define `const state = ref()` outside of `<script setup>` or `setup()` function. For example, doing `export myState = ref({})` would result in state shared across requests on the server and can lead to memory leaks."

Same defect, other shapes — all one process-wide object:

- `let currentTenant` / `const cache = new Map()` at the top of a composable or util.
- A store created at module scope instead of per request (a Pinia instance is per-Nuxt-app; a hand-rolled singleton is not).
- An SDK client configured with the caller's token at module scope, reused for the next caller.
- A `getCachedData` or response cache keyed by URL alone, with no user in the key.

A connection pool at module scope is fine — the rule is about *per-user* data, not about all shared objects.

**How you catch it:** it never reproduces in `nuxt dev` with one browser. Two users, two browsers, one production server process, reload. Grep for module-scope `= ref(`, `= reactive(`, `= new Map(`, and `let ` in `app/composables`, `app/utils`, and `shared/`. `references/nuxt-ssr-state-safety.md` has the full audit list and a test that catches the class.

Nuxt shipped this bug itself: **CVE-2026-71316** cached extracted payloads under the request path with no account of cookies or auth, so an authenticated visitor's SSR data went to the next anonymous one. Fixed in 4.5.1.

### 7. Server-only and client-only values produce hydration mismatches

Any value that differs between the server render and the first client render breaks hydration.

| Source | Why it diverges | Fix |
|---|---|---|
| `new Date()`, `Math.random()`, `crypto.randomUUID()` | different value per run | compute once on the server, carry in `useState` |
| `localStorage`, `sessionStorage`, `window.matchMedia` | absent on the server | read in `onMounted`, or `<ClientOnly>` |
| `useCookie` on an `httpOnly` cookie | server can read it, client cannot | read it in `server/` with `getCookie(event, …)` |
| user-agent or viewport branching | server sees headers, client sees the real device | pick one source; do not switch after mount |
| `$fetch` in setup (footgun 2) | second response differs from the first | `useAsyncData` / `useFetch` |

`useState(key, init)` is the transfer mechanism: the initializer runs on the server, the value is serialized into `payload.state`, and the client picks it up instead of recomputing. It must be JSON-serializable — no class instances, functions, or symbols.

### 8. Nitro server routes and server middleware

- `server/api/**` is mounted under `/api`; `server/routes/**` at the root. `server/middleware/**` runs on **every** matching request, before route handlers, in alphabetical filename order.
- **Server middleware must not return a value.** The docs: "Middleware handlers should not return anything (nor close or respond to the request)." A middleware that returns `true` silently turns every route into `true`. Set `event.context.*`, or `throw createError(...)`.
- Middleware runs for asset and payload requests too, so a DB lookup or JWT verify in it runs on requests you did not intend. Match the path explicitly.
- Method suffixes are part of the filename: `server/api/order.post.ts`. `readBody` in a GET handler throws `405 Method Not Allowed`.
- **Do not import Vue app code into `server/`** (or server-only code into `app/`). The docs call this out; it is also how a server secret gets pulled into the client bundle.
- Call `useRuntimeConfig(event)` **with the event** in server routes — the docs recommend it so runtime env-var overrides actually apply.

### 9. `runtimeConfig` — `public` means published

Everything under `runtimeConfig.public` is added to **each page payload**. Everything at the top level is server-only and is not shipped.

```ts
export default defineNuxtConfig({
  runtimeConfig: {
    stripeSecretKey: '',              // server only — override with NUXT_STRIPE_SECRET_KEY
    public: { apiBase: '/api' }       // in every page's HTML — NUXT_PUBLIC_API_BASE
  }
})
```

- Env override names are `NUXT_` + the key path uppercased with `_` at each key boundary and case change. A key you forgot to declare in `runtimeConfig` is **not** overridable at runtime — the env var is silently ignored and you ship the build-time value.
- The docs' own warning: "Be careful not to expose runtime config keys to the client-side by either rendering them or passing them to `useState`." A server-only key read inside a component's setup is read on the client too, where it is `undefined` — and if you route around that by moving it to `public`, you have published it.
- `process.env.MY_SECRET` referenced in `app/` code is inlined into the client bundle by the bundler. Secrets are read in `server/` only.

### 10. Route middleware is not a security control

`app/middleware/auth.global.ts` runs on the server for the first page load, and then **in the browser** for every client-side navigation after that. The user owns the browser. A middleware that calls `navigateTo('/login')` is a UX redirect, not authorization.

- Enforce on the server: in the `server/api` handler that returns the data, or in `server/middleware` that gates a path prefix. If the data is protected there, a bypassed client redirect exposes an empty shell.
- Documented ordering: global middleware first (alphabetical by filename), then page middleware in the order declared in `definePageMeta`. Numeric prefixes are **sorted as strings** — `10.auth.global.ts` runs before `2.tenant.global.ts`. Zero-pad them.
- The initial page's middleware runs twice (server, then client). Anything with a side effect in it fires twice; guard with `import.meta.server`.
- Nuxt's own routing has been the bypass: **CVE-2026-53721** — vue-router matches case-insensitively but the compiled `routeRules` matcher was case-sensitive, so `/Admin/dashboard` rendered the page while `routeRules.appMiddleware` never applied. Fixed in 4.4.7 / 3.21.7, with a related mixed-case route-rule drop fixed in 4.5.1.

### 11. Auto-imports cost you at debug time

Components, composables, and utils under `app/` are auto-imported, plus Vue and Nuxt APIs. Nothing is wrong with that until something breaks.

- There is **no import line to grep**. When two files export `useCart`, one shadows the other and the only signal is behaviour. Check `.nuxt/imports.d.ts` and `.nuxt/components.d.ts` for what actually resolved.
- **Nuxt composables must be called synchronously** in a setup function, plugin, or route middleware. Awaiting first loses the Nuxt context and the next composable throws. Hoist every `useX()` above the first `await`, or wrap with `nuxtApp.runWithContext(...)`.
- `#imports` gives an explicit import when you want one. `imports.autoImport: false` disables the mechanism; `imports.scan: false` keeps framework composables auto-imported and requires explicit imports for yours — reasonable on a large codebase.
- "This composable doesn't exist" in a fresh checkout is almost always a stale `.nuxt/` — run `nuxt prepare`.

### 12. Plugin and module ordering

- `app/plugins/**` top-level files register automatically in **string-sorted filename order** — `10.x.ts` before `2.x.ts`. Zero-pad.
- `.client` / `.server` suffixes restrict a plugin to one side. A helper provided only in `.client.ts` leaves `nuxtApp.$helper` undefined during SSR; every consumer needs a guard, or the plugin must be universal.
- Object syntax gives real ordering: `enforce: 'pre' | 'post'`, `dependsOn: ['plugin-name']`, `parallel: true`. Plugins run **sequentially** by default; `parallel` lets the next start before this one finishes — never on a plugin something else `dependsOn`.
- Module-installed plugins are injected by the module, not by your filenames. Order against them with `dependsOn` and the plugin's declared `name`; a numeric prefix cannot.

## How this slots into the pipeline

- **Gate 0 (render model):** state the rendering mode per route — universal SSR, `ssr: false` (SPA), or `routeRules` with `prerender`/`swr`/`isr`. `ssr: false` moves every fetch and every secret decision to the client; ledger it, do not default into it.
- **Gate 3 (UI state machine):** add the states that only exist here — SERVER_RENDERED, HYDRATING, CLIENT_NAVIGATED. Most Nuxt bugs are a transition between two of them.
- **Gate 5 (design):** name each piece of state and its owner — server-only (`server/`), per-request (`useState`, Pinia), or client-only (`onMounted`). Name the `useAsyncData` keys. State the payload budget. **No module-scope mutable state** is a design constraint, not a review finding.
- **Gate 6 (implementation):** code against footguns 1–12 and `references/nuxt-data-fetching.md`.
- **Gate 7 (review):** reliability-reviewer runs footguns 1–8 and 12; security-reviewer runs the Security section, starting with the grep in `references/nuxt-ssr-state-safety.md`; frontend-perf-reviewer checks payload size (footgun 5).

## References

- `references/nuxt-data-fetching.md` — right/wrong code for `useFetch` / `useAsyncData` / `$fetch`, keys and wrappers, reactive URLs, `getCachedData`, cookie forwarding during SSR, payload trimming.
- `references/nuxt-ssr-state-safety.md` — the cross-request leak audit: what to grep for, per-request vs process-wide objects, Pinia and SDK clients under SSR, and the two-browser reproduction.

## Security

Nuxt/Nitro mechanics. Vue-level rendering security (`v-html`, dynamic `<component :is>` from user input, template compilation) is in `mir-frontend-vue`; the generic discipline is in `mir-frontend`.

**Advisory set — the floor is Nuxt 4.5.1 and `@nuxt/devtools` 3.3.1**

| Advisory | Affected | Fixed in | What it does |
|---|---|---|---|
| **CVE-2026-71316** (`GHSA-wm8w-6qjm-cv43`, High 7.5) | 4.4.0–4.5.0 | 4.5.1 | Runtime payload extraction cached `/<page>/_payload.json` keyed on path only, ignoring cookies and auth. One authenticated visitor warms a `cache`/`swr`/`isr` route; **the next user gets their SSR data**. Cannot upgrade: `experimental.payloadExtraction: false`, and purge cached payloads afterwards. |
| **CVE-2026-71319** (`GHSA-279x-mwfv-vcqv`, Critical 9.6) | `@nuxt/devtools` < 3.3.1 | 3.3.1 | Unauthenticated RPC over the Vite HMR WebSocket. `updateOptions('behavior', { openInEditor: '<cmd>' })` then `openInEditor()` runs arbitrary programs on the developer's machine — reachable from the LAN under `nuxi dev --host`, and **cross-origin from any page you visit while dev runs**. |
| **CVE-2026-71320** (`GHSA-9473-5f9j-94wq`, High 8.1) | 3.4.0–3.21.9 / 4.0.0–4.5.0 | 3.21.10 / 4.5.1 | With `vue.runtimeCompiler: true`, a `template` key in server-island props compiles and executes in the Nitro process, reached through polymorphic `as`/`asChild` props. Default config is unaffected — a reason to leave `runtimeCompiler` off. |
| **CVE-2026-71318** (`GHSA-48hr-524c-v5w3`, Moderate 4.8) | 3.1.0–<3.21.10 / 4.0.0–<4.5.1 | 3.21.10 / 4.5.1 | An `as` prop on a server island instantiates any globally registered component; no `runtimeCompiler` needed. The patch rejects a top-level `as` — explicit prop forwarding is still yours to allow-list. |
| **CVE-2026-53721** (`GHSA-mm7m-92g8-7m47`, High) | 4.0.0–4.4.6 / 3.11.0–3.21.6 | 4.4.7 / 3.21.7 | `routeRules` matched case-sensitively while vue-router matched case-insensitively, so `/Admin/x` rendered the page with **no `appMiddleware` applied**. |
| `GHSA-9pgf-384g-p7mv` · `GHSA-hxcr-hm88-mpq6` (High) | < 4.5.1 / < 3.21.10 | 4.5.1 / 3.21.10 | Unauthenticated CPU exhaustion parsing the island endpoint; out-of-memory crash via `v-for` expansion. |

Nuxt 3 went end-of-life 31 July 2026, so **the next advisory in this series will have no Nuxt 3 patch.**

**Object-level authorization (IDOR/BOLA)**
A session cookie says *who*. It never says *what they may read*. In `server/api/orders/[id].get.ts`, `getRouterParam(event, 'id')` is attacker-controlled: load the row **and** check ownership against `event.context.auth` in the same handler, and return 404 rather than 403 so the response does not confirm the row exists. Filter list endpoints by `tenantId` in the query, never in JS after fetching. Route middleware in `app/middleware/` does none of this (footgun 10).

**Mass assignment**
`readBody(event)` returns whatever the client sent. `await db.user.update({ where: { id }, data: await readBody(event) })` lets a client set `role`, `tenantId`, or `credits`. Use `readValidatedBody(event, schema.parse)` with a schema that lists only client-settable fields and rejects unknown keys, and assign server-owned fields from `event.context`.

**Injection**
- SQL in a server route is ordinary SQL injection — parameterize; never template a user string into a query, and allow-list any user-supplied column name for `ORDER BY`.
- Command: a server route calling `execSync` with a filename from the request is RCE. Use the argument-array form and no shell.
- Prompt injection: Nitro hosts a lot of LLM proxy routes. Retrieved documents, tool results, and webhook bodies are data, not instructions — never concatenate them into a system prompt, and apply the same ownership check to any tool the model can call.
- Template injection: keep `vue.runtimeCompiler` at its default `false` (CVE-2026-71320).

**SSRF**
A server route that fetches a URL from the request body runs inside your network and can reach the cloud metadata endpoint. Do not accept a URL — accept an ID and build the URL yourself. If you must accept one, allow-list the host, resolve it and reject private, loopback, and link-local addresses, cap redirects (`$fetch` follows them by default), and set a timeout and a response size cap.

**Secret and PII leakage**
- `runtimeConfig.public` is in every page's HTML (footgun 9). So is anything in `useState` or returned from `useAsyncData` without `pick`/`transform` (footgun 5).
- Any module imported by `app/` code is bundled to the client, including the constants file someone put an API key in. Server secrets are read in `server/` from `useRuntimeConfig(event)`.
- Verify before shipping: build, then `grep -r` `.output/public/` for a known secret value, and read the `__NUXT_DATA__` block of a logged-in page.
- Do not pass a caught DB error's message through `createError`. `statusMessage` and `data` are both serialized to the client. Log the detail server-side with a correlation ID.

**Cookies, CSRF, SameSite**
Cookie or session auth means the browser attaches credentials to cross-site requests and every state-changing `server/api` route needs CSRF defence — an origin check in `server/middleware`, or a double-submit token. `Authorization: Bearer` in a header does not. Set session cookies with `httpOnly: true, secure: true, sameSite: 'lax'`. `useCookie` runs on both sides: during SSR it can read an `httpOnly` cookie the browser will not expose to client JS, so rendering that value publishes the session token. Read session cookies with `getCookie(event, …)` in `server/`.

**CORS**
The one-line Nitro switch — `routeRules: { '/api/**': { cors: true } }` — is a permissive allow-all (confirm the exact headers your Nitro version emits; the wildcard semantics are stable, the header set has changed across versions). It is fine for a public read-only API and wrong for anything cookie-authenticated. Enumerate origins in `server/middleware` and set `Vary: Origin` when the allow-list has more than one entry.

**Supply chain**
`npx nuxt module add x` adds a dependency **and** grants it build-time code execution: modules run in your build, add server routes, inject client plugins, and run again on every `nuxt prepare`/`postinstall`. Read what a module does before adding it. Commit the lockfile, pin module versions, and re-run `npm audit` after each `nuxt upgrade` — the transitive tree is large (Vite, Rolldown, nitropack, h3, ofetch, unhead) and advisories land there more often than in `nuxt` itself.

**Defaults that ship on**
| Setting | Default | Why it matters |
|---|---|---|
| `devtools.enabled` | on in dev | CVE-2026-71319. Never run `nuxi dev --host` on an untrusted network. |
| `ssr` | `true` | Setting `false` makes an SPA — every fetch moves client-side and the server-only reasoning above stops applying. |
| `vue.runtimeCompiler` | `false` | Leave it off; turning it on re-enables the island template-injection path. |
| `experimental.payloadExtraction` | on for prerendered/cached routes | The mechanism behind CVE-2026-71316. Never combine it with per-user data on a cached route. |
| `.env` | loaded in dev, **not** in production | A deploy that relies on `.env` gets build-time defaults. Set real env vars in the runtime environment. |

**Path traversal**
A server route that joins a request parameter onto a filesystem path is traversal — `..` and symlinks both escape a bare `join`. Resolve, then check containment against the base directory. Generate your own upload filenames; never trust the client's.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Nuxt and Nitro mechanics.** Apply the placement test before adding anything:

- True for React, Angular, and Svelte too (state machine, a11y invariants, gates, risk register, perf budget)? → **up** to `mir-frontend`.
- True for every Vue app whether or not it uses Nuxt (`ref` vs `reactive`, reactivity loss on destructuring, `watch` vs `computed`, `provide`/`inject`, `v-html`)? → **up** to `mir-frontend-vue`.
- A mechanical footgun of Nuxt or Nitro (universal rendering, `useAsyncData`/`useFetch`, keys and payload, `useState`, `server/` routes, `runtimeConfig`, auto-imports, route middleware, plugin order, `routeRules`)? → **here**.
- A different Vue meta-framework (Vitesse, Quasar's SSR mode, Astro with Vue islands)? → its own module under `mir-frontend-vue`. Never widen this one.

Full layered edit map: `mir-frontend/SKILL.md` → "Where these instructions live".
