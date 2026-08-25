---
name: mir-frontend-react-next
description: "Make It Right (Next.js module). Next.js 16 App Router mechanics — the footguns that exist only in this meta-framework, not in React generally. Carries the Server/Client Component boundary and how one 'use client' pulls its entire import graph into the browser bundle; Server Actions as public POST endpoints that must re-authenticate and re-authorize on every call (hiding the button is not access control); proxy.ts as an optimistic redirect and never the sole auth gate — this framework has a repeating middleware-bypass advisory class (CVE-2025-29927, CVE-2026-45109); the opt-in caching layers after Next 16 ('use cache', cacheComponents, cacheTag, revalidateTag vs updateTag vs revalidatePath); request waterfalls from sequential awaits in nested layouts; and NEXT_PUBLIC_ vars inlined at build time. Chains: mir-frontend → mir-frontend-react → this. TRIGGER only when the React meta-framework is Next.js — work in app/, page.tsx, layout.tsx, route.ts, proxy.ts or middleware.ts, any 'use server' file, next.config.ts, or any Next.js caching, rendering or revalidation question. SKIP for React Router 7/8 Framework Mode and Remix v2 (mir-frontend-react-remix), TanStack Start, Astro, plain Vite SPAs, Nuxt and SvelteKit, and for React-general reactivity rules — Rules of Hooks, stale closures, derived state and Compiler interop live in mir-frontend-react."
trigger: /mir-frontend-react-next
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-frontend-react-next · Make It Right (Next.js App Router)

Bottom tier of the chain: `mir-frontend` (generic gates) → `mir-frontend-react` (React reactivity) → **this** (Next.js library mechanics). Run the gates first. Reach for this at Gate 5 (rendering ownership), Gate 6 (implementation), and Gate 7 (review). **React-level rules — Rules of Hooks, derived state, stale closures, list keys, Compiler interop — live in `mir-frontend-react`, not here.**

**Stack assumed, versions verified 13 Aug 2026.** App Router only; Pages Router (`getServerSideProps`, `pages/api`) is legacy and out of scope.

| Release | Status | Notes |
|---|---|---|
| Next.js 16.3 (3 Aug 2026) | current stable | `partialPrefetching`, root params (`next/root-params`), `catchError`, TypeScript 7 type-checking, experimental `useOffline` |
| Next.js 16.2.11 | **Active LTS — minimum patch floor** | Carries the July 2026 security release |
| Next.js 15.5.21 | Maintenance LTS | Same fixes backported; 15.x gets security only |
| Next.js 16.0 (Oct 2025) | baseline for everything below | Turbopack default · Cache Components · `middleware.ts` → `proxy.ts` · Node.js ≥ 20.9 · TypeScript ≥ 5.1 · async `params`/`searchParams`/`cookies()`/`headers()` |

Removed in 16: `experimental.ppr`, `experimental.dynamicIO` (renamed `cacheComponents`), `serverRuntimeConfig`/`publicRuntimeConfig`, `next lint`, AMP. Deprecated: `middleware.ts`, `images.domains`, single-argument `revalidateTag()`. A Next.js 17 was not released at the time of writing — verify before assuming it.

## The Next.js footguns AI walks into most

### 1. `"use client"` cascades through the import graph
Everything under `app/` is a Server Component by default. `"use client"` marks a boundary, and **every module that file imports and every component it directly renders goes into the client bundle** — including the charting library four imports down. Putting the directive at the top of a layout ships the whole subtree to the browser.

What does *not* cascade: Server Components passed in as `children` or other props. They render on the server and arrive as already-rendered output.

```tsx
// BAD — directive on the layout; heavy deps and all children become client code
'use client'
import Chart from 'heavy-charting-lib'
export default function Dashboard({ children }) { return <div>{children}</div> }

// GOOD — server layout, client leaf, server children slot
import Panel from './panel'            // 'use client' lives in panel.tsx
export default function Dashboard({ children }) { return <Panel>{children}</Panel> }
```

Crossing the boundary costs: props must be React-serializable (functions and classes are rejected), and **every prop you pass is written into the RSC payload**, which is readable in the browser whether or not the component renders it.

### 2. A Server Action is a public HTTP endpoint
`'use server'` compiles each exported function into a POST endpoint that anyone holding the action ID can call with any arguments. Next.js encrypts action IDs, regenerates them per build (cached max 14 days), and dead-code-eliminates unused actions — none of that is authorization, and CVE-2026-64643 disclosed those IDs to unauthenticated callers.

**A page-level auth check does not extend to the actions defined on that page.** Hiding the button, `return null` in a layout, and a `proxy.ts` redirect all leave the endpoint reachable.

```ts
'use server'
export async function deletePost(postId: string) {
  const session = await auth()                                   // 1. authenticate, every call
  if (!session?.user) throw new Error('Unauthorized')
  if (typeof postId !== 'string') throw new Error('Bad input')   // 2. TS types are erased at runtime
  const { count } = await db.post.deleteMany({                   // 3. ownership IN the write,
    where: { id: postId, authorId: session.user.id },            //    not a separate read first
  })
  if (count !== 1) throw new Error('Not found')
  updateTag(`posts:${session.user.id}`)
  return { ok: true }                                            // 4. not the DB row
}
```

**Put the ownership predicate in the `WHERE` clause of the write, not in an `if` above it.** `findUnique` → check → `update({where:{id}})` leaves a window where ownership changes between the two statements, and it leaves a write scoped only by an attacker-supplied id if anyone later deletes the check. Read first only when you need the row's contents; then still scope the write.

Closed-over variables are encrypted before going to the client; `.bind(null, x)` arguments are **not**. Do not put a secret in either.

**`redirect()` throws.** It signals by throwing `NEXT_REDIRECT`, so a `try`/`catch` around your action body swallows it and the user stays on the page with no error and no navigation — the standard way a post-login or post-delete redirect silently stops working. Call `redirect()` after the `try`/`catch`, never inside it. Same for `notFound()`.

### 3. `proxy.ts` is an optimistic redirect, not an auth gate
Next 16 renamed `middleware.ts` to `proxy.ts` (export `proxy`, Node.js runtime). `middleware.ts` still works for Edge but is deprecated. It runs on every route including prefetches, so read the session cookie only — no database call.

It is for UX redirects. Enforce authorization in the data path. Three separate advisories have bypassed this layer (see Security). The GHSA for the most recent one says it directly: enforce authorization in the page's server-side data path instead of relying solely on middleware.

**Layouts are not a gate either.** They do not re-render on client navigation, and a layout does not control whether sibling segments and parallel-route slots render or appear in the RSC payload. Put the check in a `server-only` Data Access Layer that every read calls.

### 4. Two caching models — know which one you are in
`fetch()` has not been cached by default since Next 15. Beyond that, the behaviour depends on one flag.

| | `cacheComponents: false` (default) | `cacheComponents: true` |
|---|---|---|
| Default | Route is prerendered until something request-time appears | Everything runs at request time unless you cache it |
| Opt in | `fetch(url, {cache:'force-cache'})`, `unstable_cache` | `'use cache'` + `cacheLife()` + `cacheTag()` |
| Reading `cookies()` uncovered | Silently makes the whole route dynamic | Build error — you must wrap in `<Suspense>` or cache |
| PPR | not available (`experimental.ppr` removed) | on; the shell is prerendered, the rest streams |

`'use cache'` cannot call `cookies()`, `headers()` or `searchParams` — it throws. Extract the value outside and pass it in as an argument, or use `'use cache: private'` (result stays in the browser, never on the server). **Arguments and closed-over values become the cache key, and `cacheTag` values are stored as written — both in plain text.** Key on a user id; never on a token or an email.

### 5. Revalidation: pick the one that matches the guarantee
| Need | Use | Notes |
|---|---|---|
| Invalidate a named dataset, tolerate brief staleness | `revalidateTag(tag, 'max')` | The `cacheLife` profile is the **second argument** since 16; the single-argument form is deprecated |
| The user must see their own write immediately | `updateTag(tag)` | Server Actions only; read-your-writes |
| Refresh uncached data shown elsewhere on the page | `refresh()` | Server Actions only; does not touch the cache |
| Drop everything under a route | `revalidatePath('/x')` | Blunt. On a personalised route it discards every user's entry |

Time (`cacheLife`, `next.revalidate`) is a staleness ceiling, not a correctness mechanism. If a write must be visible, invalidate explicitly. Tag at write granularity — `cacheTag("notes:" + userId)`, not `cacheTag("notes")`.

### 6. Request waterfalls from sequential awaits
Every `await` in a layout blocks its children, and nested layouts stack. This is the most common Next.js performance bug and it is invisible in dev.

```tsx
// BAD — serial: 3 round trips, and {children} waits for all of them
const user = await getUser(); const org = await getOrg(); const plan = await getPlan()

// GOOD — parallel when independent
const [user, org, plan] = await Promise.all([getUser(), getOrg(), getPlan()])
```

A top-level `await params` / `await cookies()` / DAL call in a layout holds `{children}` behind it. Pass the promise down and await it inside a `<Suspense>` boundary instead. Wrap non-`fetch` reads in React's `cache()` so a layout and its page do not query twice (`fetch` is memoized per render pass already).

### 7. Suspense placement decides what is prerendered
This is the Next-specific half of the React tier's granular-boundary rule: **what sits outside a boundary goes into the static shell and is served instantly; what sits inside streams at request time.** A single root `loading.tsx` puts the entire page behind one fallback and removes the shell.

Push the async read down, not the boundary up. Next 16.3 adds `catchError` from `next/error` for component-level error boundaries with a `retry()` that re-renders Server Components and does not swallow `notFound()`/`redirect()`; `error.js` stays route-level.

### 8. What silently opts a route into dynamic rendering
| Trigger | Where it usually sneaks in |
|---|---|
| `cookies()`, `headers()`, `draftMode()`, `connection()` | An analytics or theme read added to a shared header |
| `searchParams` in a page, or dynamic `params` not covered by `generateStaticParams` | A filter added to a listing page |
| `fetch(url, { cache: 'no-store' })` | Copied from a dashboard example |
| `export const dynamic = 'force-dynamic'` / `export const revalidate = 0` | Added to fix a stale-data bug, never removed |
| `Math.random()`, `Date.now()`, `crypto.randomUUID()` in render | A "unique key" or a timestamp in a footer |

With `cacheComponents: false` nothing warns — the whole route just stops being prerendered. Turning `cacheComponents: true` on converts these into build errors, which is the main reason to adopt it. Note that bots and crawlers skip the shell and get the full page rendered at request time, so anything your shell only has at build time will fail for a crawler.

### 9. Route Handlers vs Server Actions
| Use a Server Action | Use a Route Handler (`route.ts`) |
|---|---|
| A mutation triggered by your own UI | Third-party webhooks and OAuth callbacks |
| You want `<form action={fn}>` progressive enhancement | A public or versioned JSON API, other clients |
| You want POST-only plus the built-in `Origin`/`Host` check | Non-POST methods, streaming/SSE, file downloads |

Route Handlers get **no** CSRF protection, no origin check, and no automatic method restriction — you write all of it. A `GET` handler can set cookies, which is why they need extra review. Do not create an internal `/api` route and `fetch()` it from a Server Component: that is a network hop back into your own process. Call the function.

### 10. `NEXT_PUBLIC_` is inlined into the client bundle
Only `NEXT_PUBLIC_`-prefixed variables reach the browser. Non-prefixed variables read from client code are **replaced with an empty string**, so the failure is silent wrong behaviour, not an error.

The value is inlined at build time. Rotating a `NEXT_PUBLIC_` value requires a rebuild, and every previously shipped build still carries the old one. Treat anything with that prefix as published. Put `import 'server-only'` at the top of every module that touches a secret so an accidental client import fails the build; `client-only` for the reverse.

### 11. `next/image` correctness
`width` + `height` (or `fill` on a positioned parent) is what prevents CLS — it is not optional. A static `import` supplies both plus `blurDataURL` automatically. With `fill` or any responsive layout, set `sizes`; without it the browser assumes `100vw` and downloads the largest candidate. Put `priority` on the LCP image only — marking several defeats the point.

Use `remotePatterns` with an explicit `hostname` **and** `pathname`; `images.domains` is deprecated, and a broad pattern turns `/_next/image` into a free image proxy for the internet. Next 16 changed defaults you may be relying on: `qualities: [75]`, `minimumCacheTTL` 4 hours, `imageSizes` no longer includes `16`, `maximumRedirects: 3`, local IPs blocked unless `images.dangerouslyAllowLocalIP`, and a local `src` with a query string now needs `images.localPatterns`.

### 12. `next/font` correctness
The loader is a build-time transform, not a runtime function. **Call it at module scope with a literal object.** Calling it inside a component, or with a computed argument, fails the build.

Fonts are downloaded at build time and self-hosted; no request goes to Google from the browser. Defaults: `display: 'swap'`, `preload: true`, and `adjustFontFallback` on (`true` for Google, `'Arial'` for local) — that last one generates the metric-matched fallback that keeps the swap from shifting layout, so turning it off reintroduces CLS. `subsets` must be set or you get a warning and no preload link. Every loader call is a separate hosted instance: declare fonts once in a `fonts.ts` and import them, or you ship the same font twice. Preload scope follows the call site — root layout preloads on all routes, a page preloads on that route only.

## How this slots into the pipeline

- **Gate 0 (render-model fitness):** state whether `cacheComponents` is on. It changes the default rendering of every route and what counts as an error.
- **Gate 5 (rendering ownership):** name each `"use client"` boundary and what it drags in; name the caching layer each piece of data lives in; name the revalidation API for each mutation. Read `references/caching-and-rendering.md`.
- **Gate 6 (implementation):** code against footguns 1–12. Every `'use server'` export follows the template in `references/server-actions-and-route-handlers.md`.
- **Gate 7 (review):** the security-reviewer works the Security section below — Server Action authorization and cache-key scoping are the two most commonly missed. The frontend-perf-reviewer checks 6, 7, 11, 12.

## References

- `references/caching-and-rendering.md` — the caching layers in both models, `use cache` / `cacheLife` / `cacheTag` rules, the full dynamic-opt-in list, waterfall and Suspense patterns, Cache Components migration order.
- `references/server-actions-and-route-handlers.md` — the Server Action authorization template, argument validation, CSRF and `allowedOrigins`, `NEXT_SERVER_ACTIONS_ENCRYPTION_KEY` across instances, Route Handler checklist.

## Security

Next.js library mechanics. React-level rules (untrusted HTML through React's raw-HTML prop, client checks are not authorization) are in `mir-frontend` and `mir-frontend-react`.

**Patch floor: 16.3.0, or 16.2.11 (Active LTS) / 15.5.21 (Maintenance LTS).** Anything older is unpatched.

| Advisory | Affected | Fixed in | What it does |
|---|---|---|---|
| **CVE-2026-64642** (`GHSA-6gpp-xcg3-4w24`, CVSS 8.3) | 16.0.0 – 16.2.10, App Router + Turbopack + exactly one entry in `config.i18n.locales` | 16.2.11 | **Proxy/middleware bypass.** Every auth check in `proxy.ts` is skipped, unauthenticated, no user interaction |
| **CVE-2026-45109** (`GHSA-26hh-7cqf-hhc6`, CVSS 7.5) | 15.2.0 – 15.5.17, 16.0.0 – 16.2.5 | 15.5.18 / 16.2.6 | Middleware bypass via segment-prefetch routes. Incomplete fix of CVE-2026-44575, which had missed `middleware.ts` under Turbopack |
| **CVE-2025-29927** | 12.x, 13.x, 14.x and 15.x below 15.2.3 — self-hosted `next start` or `output: 'standalone'` only | 15.2.3 / 14.2.25 / 13.5.9 / 12.3.5 | The `x-middleware-subrequest` header made the server skip middleware entirely. The origin of this advisory class |
| **CVE-2026-64643** | App Router with Server Actions or `use cache` | 16.2.11 / 15.5.21 | Server Function endpoint IDs disclosed to unauthenticated callers — reconnaissance for calling your actions directly |
| **CVE-2026-64649**, **CVE-2026-64645** | Server Actions on custom servers; `rewrites()`/`redirects()` whose destination hostname comes from request input | 16.2.11 / 15.5.21 | SSRF (open redirect for `redirects()`) |
| **CVE-2026-64641 / 64646 / 64644** | Server Actions (CPU), Edge Server Action payload (memory), `/_next/image` with SVG (CPU) | 16.2.11 / 15.5.21 | Denial of service |
| **CVE-2026-64647 / 64648** | Server-side `fetch` with a request body | 16.2.11 / 15.5.21 | Cache confusion — one request's response body returned for a different request |
| `CVE-2026-44581`, `CVE-2026-44580`, `CVE-2026-44582`, `CVE-2026-44572` | CSP-nonce handling, `beforeInteractive` `<Script>`, RSC cache-busting collisions, proxy redirects | see each advisory | XSS and cache poisoning; check your exact version against the GitHub Advisory Database |

**The middleware/proxy bypass class**
Three bypasses of this layer since 2025. Assume a fourth. `proxy.ts` decides what the user *sees*; the Data Access Layer decides what they *get*. Every read goes through a `server-only` module that calls `verifySession()` (memoized with React `cache()`) before it touches the database, and returns a narrow Data Transfer Object holding only the fields the caller may see. Prefer an allow-list matcher over a deny-list — a deny-list misses RSC payload requests, prefetches, and rewrites.

**Server Action authorization (the framework's most dangerous default)**
Authenticate and authorize inside every `'use server'` function, every call. Check ownership of the specific object, not just "is logged in" — that is the IDOR/BOLA check. Validate every argument at runtime; TypeScript annotations are erased. Return `{ ok: true }`-shaped data, not the database row, because the return value is serialized to the client.

**Mass assignment**
`db.user.update({ data: Object.fromEntries(formData) })` hands the client `role`, `tenantId`, and `isAdmin`. Parse the form with a schema that names only client-settable fields and assign server-owned fields from the session.

**Data reaching the client**
The RSC payload carries every prop passed to a Client Component, rendered or not. Filter in the Data Access Layer and return only the fields the UI needs. `import 'server-only'` turns an accidental client import into a build error. `experimental.taint` plus `experimental_taintObjectReference` / `experimental_taintUniqueValue` is a second layer, not a substitute — it does not block derived values. Functions and classes are already rejected at the boundary.

**Cache poisoning and per-user cache bleed**
A plain `'use cache'` entry lives in a **shared** server cache. If the user id is not in the arguments, one user's data is served to the next. `'use cache: private'` keeps the result in that browser only. Cache keys and `cacheTag` values are stored in plain text in both the default and remote cache handlers — keep tokens, passwords and raw emails out of arguments and tags. `revalidatePath` on a personalised route discards every user's entry.

**CSRF, cookies, CORS**
Server Actions are POST-only and compare `Origin` against `Host`/`X-Forwarded-Host`. Behind a reverse proxy or on a different production domain, set `serverActions.allowedOrigins` rather than disabling the check. Route Handlers have none of this. Session cookies: `httpOnly`, `secure`, `sameSite: 'lax'`, an explicit `path` and expiry. There is no built-in CORS — if you add headers in `next.config.ts` or a Route Handler, match `Origin` against an allow-list and never reflect it back with credentials.

**SSRF and the image optimizer**
`rewrites()`/`redirects()` destinations built from request input are the documented SSRF path (CVE-2026-64645). A wide `images.remotePatterns` entry makes `/_next/image` fetch arbitrary hosts on your behalf; pin `hostname` and `pathname`. Keep `images.dangerouslyAllowLocalIP` off and leave `images.maximumRedirects` at 3.

**Self-hosting**
Set `NEXT_SERVER_ACTIONS_ENCRYPTION_KEY` (base64, 32 bytes) and make it identical on every instance — otherwise closure decryption fails across instances and rotates unpredictably on each build. Run production builds only; development mode sends full server errors and stack traces to the client in plain text.

**Supply chain**
Commit the lockfile and install with `npm ci`. `next`'s own floor is not the security floor — a transitive constraint elsewhere in the tree can hold you on a vulnerable version with no warning, so put an explicit floor in your own `package.json` and verify the resolved version with `npm ls next`. Watch the `@next/swc-*` platform-native optional dependencies during audits.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY Next.js App Router mechanics.** Apply the placement test before adding anything:

1. True for Vue and Angular too (any reactive UI)? → **up** to `mir-frontend`.
2. True for every React meta-framework because they all run React's reactivity model (hook rules, derived state, stale closures, keys, Compiler interop)? → **up** to `mir-frontend-react`.
3. A mechanical footgun of *Next.js* (RSC boundary, Server Actions, `proxy.ts`, `use cache`, `revalidateTag`, file routing, `next/image`, `next/font`, `next.config.ts`)? → **here**.
4. A *different* React meta-framework? React Router 7/8 in Framework Mode — the product formerly called Remix — is `mir-frontend-react-remix`. TanStack Start and a plain Vite React SPA have no module yet; those stacks get `mir-frontend` plus `mir-frontend-react`. Never widen this one.

Full layered edit map: `mir-frontend/SKILL.md` → "Where these instructions live".
