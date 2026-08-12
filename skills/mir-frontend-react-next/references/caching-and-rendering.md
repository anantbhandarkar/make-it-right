# Next.js caching, revalidation, and rendering — depth

Read at Gate 5 when you write "rendering ownership" and "state ownership", and at Gate 6.
Verified against Next.js 16.3 docs, 13 Aug 2026.

## 1. Which model are you in

One flag decides everything. Check `next.config.ts` before you write a line.

```ts
const nextConfig = { cacheComponents: true }   // Cache Components on
```

`experimental.dynamicIO` was renamed to `cacheComponents` in 16. `experimental.ppr` and
`export const experimental_ppr` were removed — PPR is now the default behaviour when
Cache Components is on, not a separate flag.

## 2. The layers, `cacheComponents: false` (default)

| Layer | Lives | Caches | Default | Invalidate with |
|---|---|---|---|---|
| Request memoization | Server, one render pass | `fetch` with the same URL+options; anything wrapped in React `cache()` | On, automatic | Nothing — it dies with the request |
| Data Cache | Server, across requests and deploys | `fetch(url, {cache:'force-cache'})`, `unstable_cache(fn, keys, {tags, revalidate})` | **Off** — `fetch` is not cached by default since Next 15 | `revalidateTag`, `revalidatePath`, `next.revalidate` |
| Full Route Cache | Server, build output | Prerendered HTML + RSC payload for static routes | On for routes with no request-time input | `revalidatePath`, redeploy, `revalidate` |
| Client Router Cache | Browser | RSC payloads for prefetched and visited routes | On | Navigation, `router.refresh()`, and a Server Action calling `revalidateTag`, `revalidatePath`, `updateTag`, or `refresh` |

Segment config that overrides the above: `export const dynamic`, `export const revalidate`,
`export const fetchCache`. `fetchCache` is an escape hatch — if you need it, the design is
probably fighting the default.

## 3. The layers, `cacheComponents: true`

Everything runs at request time unless you cache it. You opt in per function or component.

```ts
import { cacheLife, cacheTag } from 'next/cache'

async function getNotesByUserId(userId: string) {
  'use cache'
  cacheTag(`notes:${userId}`)
  cacheLife('minutes')
  return db.query.notes.findMany({ where: eq(notes.userId, userId) })
}
```

Three directives:

| Directive | Stored | Can read `cookies()` / `headers()` / `searchParams` | Use for |
|---|---|---|---|
| `'use cache'` | Per-instance in-memory server cache, best effort, evicted under pressure, not shared on serverless | **No — throws** | Shared data keyed by an argument |
| `'use cache: remote'` | Durable cache handler shared across instances | No | Data that must survive instance churn; a network hop, worth it only at a high hit rate |
| `'use cache: private'` | The browser only, never the server | Yes (not `connection()`) | Session-derived UI you want in the per-link prefetch |

Rules that bite:

- **Arguments and closed-over values are the cache key.** Change an argument, get a new entry.
- **Cache keys and `cacheTag` values are plain text** in both the default and remote handlers.
  Key and tag on a stable id. No tokens, no raw emails, no passwords.
- Every cache entry's key includes the build id, so a deploy starts cold — even `remote` ones.
- Pair every `'use cache'` with a `cacheLife()`. Without one you get the implicit `default`
  profile (five-minute `stale`), which is rarely what you meant.
- If you tune `cacheLife` and drop `stale` below 30 seconds, the scope drops out of prefetching.
- A `redirect()` or `notFound()` thrown inside a cached function is not cached; only a resolved
  return value is.

### Caching something derived from the session

Wrong: cache the whole thing. The session read throws inside `'use cache'`, and if you work
around it by dropping the user id from the arguments you have built a cross-user data leak.

Right: resolve the user in an exported wrapper, pass only the id into an **unexported** cached
function, so a caller cannot request someone else's data by passing a different id.

```ts
export async function getNotes() {
  const user = await getCurrentUser()   // reads cookies, not cached (or 'use cache: private')
  return getNotesByUserId(user.id)      // unexported, plain 'use cache', keyed by id
}
```

## 4. Revalidation decision table

| You need | API | Where it runs | Guarantee |
|---|---|---|---|
| Invalidate a named dataset with stale-while-revalidate | `revalidateTag(tag, 'max')` | Server Action or Route Handler | Eventual. Users may see the old value once more |
| The acting user must see their own write on the next render | `updateTag(tag)` | Server Actions only | Read-your-writes |
| Re-run uncached reads elsewhere on the page after a mutation | `refresh()` | Server Actions only | Does not touch any cache |
| Nuke a route's cached output | `revalidatePath('/dashboard')` | Server Action or Route Handler | Coarse. On a personalised route this discards every user's entry |

`revalidateTag(tag)` with one argument is deprecated in 16 — the second argument is a
`cacheLife` profile name (`'max'`, `'hours'`, `'days'`) or an inline `{ expire: seconds }`.

Time-based revalidation (`cacheLife`, `next.revalidate`, `export const revalidate`) is a
ceiling on staleness. It is never the mechanism that makes a write visible. If correctness
depends on the user seeing their change, use `updateTag`.

Tag granularity should match write granularity. `cacheTag('posts')` means one user's edit
invalidates the cache for everyone.

## 5. What opts a route out of prerendering

Request-time APIs: `cookies()`, `headers()`, `draftMode()`, `connection()`, `searchParams`
in a page, dynamic `params` not covered by `generateStaticParams`.

Data opt-outs: `fetch(url, {cache:'no-store'})`, `export const dynamic = 'force-dynamic'`,
`export const revalidate = 0`, `export const fetchCache = 'force-no-store'`.

Non-deterministic values in render: `Math.random()`, `Date.now()`, `crypto.randomUUID()`.
Under Cache Components these produce named dev-overlay insights
(`blocking-prerender-random`, `blocking-prerender-current-time`, `blocking-prerender-crypto`).
The two fixes are `await connection()` + `<Suspense>` for a per-request value, or `'use cache'`
to share one value across users.

`generateMetadata` and `generateViewport` count. An uncached fetch in either one opts the route
out exactly as it would in the page body.

**Bots and crawlers skip the static shell** and get the whole page rendered at request time. If
part of your shell depends on something only available at build time, the page renders for a
person and fails for a crawler.

With `cacheComponents: false` none of this warns. With it on, each becomes a build error. That
is the reason to turn it on: the failure mode changes from silent to loud.

## 6. Waterfalls

Sources, in the order they show up in real code:

1. **Sequential awaits in one component.** Independent reads go in `Promise.all`.
2. **Nested layouts each awaiting.** Layout awaits block `{children}`. A three-level layout
   stack with one `await` each is three serial round trips before the page starts.
3. **Awaiting `params` / `searchParams` at the layout top.** Do not. Pass the promise down.

```tsx
// GOOD — layout is not async; the await happens inside the boundary
export default function Layout({ children, params }: LayoutProps<'/shop/[slug]'>) {
  return (
    <div>
      <Sidebar />
      <Suspense fallback={<h1>Loading…</h1>}>
        {params.then(({ slug }) => <SlugHeading slug={slug} />)}
      </Suspense>
      {children}
    </div>
  )
}
```

4. **Duplicate reads across layout and page.** `fetch` is memoized per render pass; an ORM call
   is not. Wrap it in React `cache()`.
5. **Preload pattern** when you must do something slow before the read:
   `preload(id)` (a void call to the cached getter) started before the blocking work.

## 7. Suspense placement

Outside a boundary → static shell, served instantly, included in prefetches.
Inside a boundary → streams at request time behind the fallback.

- One root `loading.tsx` hides the whole page and removes the shell. Prefer per-subtree
  `<Suspense>` with a skeleton sized to the final content, so it does not cause CLS.
- `<Suspense>` alone does not make a component dynamic. A component doing only synchronous
  work still completes during prerender, boundary or not.
- Reading `cookies()` outside a boundary is a build error under Cache Components.
- Error boundaries: `catchError` from `next/error` (16.3) for component-level, with a `retry()`
  that re-renders the Server Components inside it and does not intercept `notFound()` or
  `redirect()`. `error.js` remains the route-level convention.

## 8. Cache Components changes what happens on navigation

Turning `cacheComponents: true` on also changes unmount behaviour. Next keeps the route you
navigated away from mounted inside React `<Activity mode="hidden">` instead of unmounting it, so
`useState`, form input values, `useActionState` results, and scroll position **survive navigating
away and back**. Effects still clean up and re-run.

Anything that relied on unmount to reset now needs an explicit reset:

| Symptom after enabling Cache Components | Fix |
|---|---|
| A dropdown or popover is still open when the user navigates back | Close it in a `useLayoutEffect` cleanup |
| A dialog's "focus the first input" effect does not re-fire | Derive the dialog's open state from the URL, not from `useState` |
| A submitted form shows its old success/error banner on return | Reset in the submit handler, or in a cleanup effect |

## 9. Cache Components migration order

1. Turn `cacheComponents: true` on in a branch and run `next build`. Read the errors — each one
   is a route that was already dynamic and you did not know.
2. For each flagged route, pick one: wrap the request-time read in `<Suspense>`, wrap the data
   read in `'use cache'`, or set `export const instant = false` on the page or layout to defer it.
3. Convert `unstable_cache` calls to `'use cache'` + `cacheTag` + `cacheLife`.
4. Convert `revalidateTag(tag)` to `revalidateTag(tag, 'max')`, or to `updateTag(tag)` in Server
   Actions where the user must see their own write.
5. Only then enable `partialPrefetching` (16.3) and check the DevTools Instant Insights panel.

Docs: `/docs/app/guides/migrating-to-cache-components`,
`/docs/app/guides/caching-without-cache-components` (the pre-16 model),
`/docs/app/guides/authentication-with-cache-components` (session + cache).
