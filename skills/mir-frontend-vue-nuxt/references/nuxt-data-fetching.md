# Nuxt data fetching — right vs wrong

Companion to `mir-frontend-vue-nuxt` footguns 2–5. Nuxt 4.5.2, verified 13 Aug 2026.

## Pick the right call

| Situation | Use |
|---|---|
| Plain URL, fetched during render | `useFetch(url)` |
| SDK / CMS client / more than one call / custom logic | `useAsyncData(key, handler)` |
| Inside an event handler (click, submit) | `$fetch` |
| Inside a `server/` route | `$fetch` (or a direct DB call — do not HTTP-call your own API from your own server) |
| Need the incoming request's cookies during SSR | `useFetch` on a relative URL, or `useRequestFetch()` |

## `$fetch` does not carry cookies during SSR

The docs state that during SSR, "due to security risks such as Server-Side Request Forgery (SSRF) or Authentication Misuse, the `$fetch` wouldn't include the user's browser cookies, nor pass on cookies from the fetch response."

The failure this produces: the server render gets a 401 and paints the logged-out UI, then the client re-fetches with cookies, gets a 200, and the page visibly changes after hydration.

```ts
// BAD — no cookies on the server run; 401 during SSR, 200 on the client
const { data } = await useAsyncData('me', () => $fetch('/api/me'))

// GOOD — relative URL: Nuxt proxies the request headers for you
const { data } = await useFetch('/api/me')

// GOOD — explicit, for an absolute URL or a custom handler
const request = useRequestFetch()
const { data } = await useAsyncData('me', () => request('/api/me'))
```

When you forward headers to a **third-party** API, forward a named subset. The docs list headers that must not be proxied: `host`, `accept`, `content-length`, `content-md5`, `content-type`, any `x-forwarded-*`, and any `cf-*`.

## Reactive sources

```ts
// BAD — the URL string is evaluated once; changing id does nothing
const { data } = await useFetch(`/api/item/${id.value}`)

// GOOD — getter is re-evaluated, request re-fires
const { data } = await useFetch(() => `/api/item/${id.value}`)

// GOOD — reactive query params, watched explicitly
const { data } = await useFetch('/api/items', { query: { page }, watch: [page] })

// GOOD (Nuxt 4.5+) — gate the request instead of branching around it
const { data } = await useFetch('/api/me', { enabled: () => isLoggedIn.value })
```

`immediate: false` + `execute()` is the manual variant when nothing should fire until a user action.

## Keys

```ts
// BAD — every caller of this wrapper shares one call-site key, whatever id they pass
export const useItem = (id: Ref<string>) =>
  useAsyncData(() => $fetch(`/api/item/${id.value}`))

// GOOD — the key carries the argument
export const useItem = (id: Ref<string>) =>
  useAsyncData(() => `item:${id.value}`, () => $fetch(`/api/item/${id.value}`))

// OK as written — useFetch keys on the resolved URL too, so different ids do not collide.
// Add an explicit key when callers should SHARE one entry, or when identity is not in the URL.
export const useItem = (id: Ref<string>) =>
  useFetch(() => `/api/item/${id.value}`, { key: () => `item:${id.value}` })
```

Rules:
- `useAsyncData` with no key generates one from **call site (file + line) only**. Adding a line above the call changes the key and drops the payload reuse — the client refetches on hydration and you get a flash. `useFetch` folds the resolved URL and fetch options into its key as well.
- Never reuse one key for two different shapes of data. `payload.data` is a flat map, and calls sharing a key also share one ref — so they must agree on `transform`, `pick`, `default`, `deep`, and `getCachedData`, or the result depends on which one ran first.
- Read a sibling's already-fetched data with `useNuxtData('item:42')` instead of fetching it again.
- `refreshNuxtData('item:42')` invalidates by key after a mutation.

## `dedupe`, `getCachedData`, and reuse

- `dedupe: 'cancel'` (default) aborts an in-flight request when a new one starts — correct for search-as-you-type.
- `dedupe: 'defer'` lets the pending request finish and skips the new one.
- `getCachedData(key, nuxtApp)` overrides when Nuxt reuses instead of refetching. The default implementation checks whether the app is hydrating and reads `nuxtApp.payload.data` or `nuxtApp.static.data`.

```ts
// DANGEROUS — a process-wide cache with no user in the key.
// This is the hand-rolled version of CVE-2026-71316.
const cache = new Map()
useAsyncData('profile', fetchProfile, {
  getCachedData: key => cache.get(key)
})
```

If a cache key can be hit by two different users, it must include the user or tenant identity — or it must not exist. See `nuxt-ssr-state-safety.md`.

## Payload trimming

```ts
// 400 KB of user objects inlined into every server-rendered document
const { data } = await useFetch('/api/users')

// Only the two fields the template reads are serialized
const { data } = await useFetch('/api/users', { pick: ['id', 'name'] })

// transform: reshape before serialization (also drops nested secrets)
const { data } = await useFetch('/api/users', {
  transform: rows => rows.map(u => ({ id: u.id, name: u.name }))
})
```

Neither option prevents the fetch — they prevent the bytes reaching the client. The server route returning fewer fields is still the better fix. Check the real cost: build, load a page, and look at the size of the `<script type="application/json" id="__NUXT_DATA__">` block.

## `server`, `lazy`, and the states you must render

| Options | Server render | Blocks navigation | You must handle |
|---|---|---|---|
| default | yes | yes | error |
| `lazy: true` | yes | no | pending, error |
| `server: false` | no | no | pending, error, and empty SSR HTML (no SEO/LCP content) |
| `server: false, lazy: true` | no | no | same as above |

`status` is `'idle' | 'pending' | 'success' | 'error'`. Render every one of them — the Gate 3 state machine in `mir-frontend` is where they were enumerated.

## Errors

`useFetch` returns `error` rather than throwing. Check it. In a `server/` route, fail with `createError({ statusCode: 404, statusMessage: 'Not found' })` — the `data` field of a `createError` is serialized to the client, so keep driver messages and stack detail out of it and log them with a correlation ID instead.
