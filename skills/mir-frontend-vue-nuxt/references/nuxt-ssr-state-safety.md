# Cross-request state safety in Nuxt SSR

Companion to `mir-frontend-vue-nuxt` footgun 6. This is the audit for the bug that shows one user another user's data.

## The mechanism

`nuxt dev` and `node .output/server/index.mjs` are **one long-lived process** handling every request. Module-level code runs once, when the module is first imported — not once per request.

- Per **request**: the Vue app instance, `useState` entries, `useAsyncData` payload entries, `event.context`, Pinia stores created through the Nuxt plugin.
- Per **process**: anything at module scope in `app/composables/`, `app/utils/`, `shared/`, `server/utils/`, and the top level of any imported file.

If per-user data lands in a per-process object, the next request reads it. It will not reproduce in local development with one browser tab.

## Grep list

Run these against `app/`, `shared/`, and `server/utils/`. Every hit is a question, not automatically a bug.

```bash
rg -n '^\s*(export\s+)?(const|let|var)\s+\w+\s*=\s*(ref|reactive|shallowRef|computed)\(' app shared
rg -n '^\s*(export\s+)?let\s+' app shared server/utils
rg -n '^\s*(export\s+)?const\s+\w+\s*=\s*new (Map|Set|WeakMap)\(' app shared server/utils
rg -n 'globalThis\.' app shared server
rg -n 'createClient|new [A-Z]\w*Client|axios\.create' app shared server/utils
```

## Verdict table

| Pattern | Verdict |
|---|---|
| `export const user = ref(null)` | **Leak.** Replace with `export const useUser = () => useState('user', () => null)`. |
| `let currentTenant` set from a request | **Leak.** Put it on `event.context` (server) or in `useState` (app). |
| `const cache = new Map()` keyed by URL only | **Leak** the moment a response varies by user. Include the user or tenant in the key, add a TTL, or move it to a real cache with an explicit key policy. |
| `getCachedData` reading a module-scope map | **Leak.** This is the hand-rolled form of CVE-2026-71316. |
| `const api = createClient({ token })` where `token` came from a request | **Leak.** Build the client per request inside the handler or plugin. |
| `const db = createPool(...)` at module scope | **Fine and correct.** Connection pools are meant to be per process. The rule is about *per-user* data, not about all shared objects. |
| `const CONFIG = { pageSize: 20 }` | Fine — immutable, not user-derived. |
| `const logger = createLogger()` | Fine, unless you attach a request id to it once and reuse it. |
| A Pinia store from `defineStore` used via the Nuxt module | Fine — Pinia creates one instance per Nuxt app, i.e. per request under SSR. A hand-rolled `export const store = reactive({})` is not. |

## `useState` rules

```ts
// app/composables/useUser.ts
export const useUser = () => useState<User | null>('user', () => null)
```

- The key is global to the app. Two `useState('user')` calls in different files are the same value — that is the point, and also the collision risk. Namespace keys the way you namespace files: `'auth:user'`, `'cart:items'`.
- The initializer runs on the server; the value is serialized into `payload.state` and picked up on the client.
- The value must be JSON-serializable — no class instances, functions, symbols, or `undefined` in place of `null`. A `Date` survives only because Nuxt has a reducer for it; your own class does not unless you register one.
- **Anything you put in `useState` is in the page HTML.** Do not put a token, an internal ID you did not intend to publish, or a full user record with an email in it.

## Server-side per-request storage

```ts
// server/middleware/auth.ts — runs on every matching request
export default defineEventHandler(async (event) => {
  const token = getCookie(event, 'session')
  if (token) event.context.auth = await verify(token)
  // no return value — see footgun 8
})

// server/api/orders/[id].get.ts
export default defineEventHandler(async (event) => {
  const auth = event.context.auth
  if (!auth) throw createError({ statusCode: 401 })
  const order = await db.order.findUnique({ where: { id: getRouterParam(event, 'id') } })
  if (!order || order.userId !== auth.userId) throw createError({ statusCode: 404 })
  return order
})
```

`event.context` is created per request and discarded with it. It is the only correct place for per-request server state.

## Reproducing the leak

One browser will never show it. The minimum reproduction:

1. Build and run the production server — `nuxt build && node .output/server/index.mjs`. One process, no HMR.
2. Browser A (or `curl` with a cookie jar): log in as user A, load the page.
3. Browser B in a private window with no cookies: load the same page.
4. Look for A's name, email, tenant, or list data in B's HTML — check the `__NUXT_DATA__` payload, not just the rendered text.
5. Repeat with the order reversed, and with a page under `routeRules` `swr`/`isr`/`cache`.

Make this a test. Two concurrent requests with different session cookies against the built server, asserting that neither response body contains the other user's identifier, catches the whole class — including the version Nuxt itself shipped in 4.4.0–4.5.0.

## Related failure: `getCachedData` and cached route rules

`routeRules: { '/dashboard': { swr: 3600 } }` caches the rendered output **and**, in affected versions, the extracted payload, keyed on path. A route whose output varies by user must not be under `cache`, `swr`, or `isr`. If a page has a shared shell and a per-user panel, cache the shell and load the panel from a `server/api` route that authorizes on every call.
