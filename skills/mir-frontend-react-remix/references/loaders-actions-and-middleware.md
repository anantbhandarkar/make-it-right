# Loaders, actions, middleware, and the data layer

Read at **Gate 5** (rendering and data ownership) and **Gate 6** (implementation). React Router **Framework Mode**, v8.3.0. Facts verified against reactrouter.com and the npm registry on 25 Aug 2026.

Declarative Mode (`<BrowserRouter>`) has none of this. Data Mode (`createBrowserRouter`) has `loader`/`action`/`shouldRevalidate`/fetchers and `clientMiddleware`, but no server, so the authorization material below does not apply to it — it applies to whatever backend it calls (`mir-backend`).

---

## 1. The authorization template

Every `loader` and `action` is an HTTP endpoint. `GET /route/path.data` reaches the loader; a POST reaches the action. Nothing in the component tree gates either one.

```ts
import { data, redirect } from "react-router"
import type { Route } from "./+types/post"

// app/auth.server.ts — .server suffix keeps it out of the client bundle
export async function requireUser(request: Request) {
  const session = await getSession(request.headers.get("Cookie"))
  const userId = session.get("userId")
  if (!userId) throw redirect("/login")      // THROW. `return` here would not stop the caller.
  const user = await db.user.findUnique({ where: { id: userId } })
  if (!user) throw redirect("/login")
  return user
}

export async function action({ request, params }: Route.ActionArgs) {
  const user = await requireUser(request)                    // 1. authenticate — every call

  const form = await request.formData()
  const title = form.get("title")
  const published = form.get("published")
  if (typeof title !== "string" || title.length === 0 || title.length > 200) {
    throw data({ error: "Title must be 1–200 characters" }, { status: 400 })
  }                                                          // 2. validate at runtime

  const { count } = await db.post.updateMany({               // 3. ownership in the WHERE clause
    where: { id: params.postId, authorId: user.id },
    data: { title, published: published === "on" },          // 4. named fields only
  })
  if (count !== 1) throw data(null, { status: 404 })         // 5. 404, not 403 — no existence oracle

  return { ok: true }                                        // 6. not the DB row
}
```

Each numbered line is a failure this catches:

1. **Authenticate every call.** A page-level check does not extend to the actions and loaders defined on that page, and a `<Link>` you did not render is not a restriction.
2. **TypeScript is erased.** `Route.ActionArgs` types the *shape you expect*, not the bytes that arrive. `formData.get()` returns `FormDataEntryValue | null`. Validate with a schema library or explicit narrowing; a cast is not validation.
3. **The ownership predicate belongs in the `WHERE` clause of the write.** `findUnique` → `if (post.authorId !== user.id)` → `update({ where: { id } })` has a window between check and write, and the write is scoped only by an attacker-supplied param if anyone later deletes the check. Read first only when you need the row's contents — then still scope the write.
4. **No mass assignment.** `data: Object.fromEntries(formData)` hands the client `authorId`, `tenantId`, `role`. Name the client-settable fields; take server-owned ones from the session.
5. **404 over 403** on objects the caller may not see, so the endpoint is not an existence oracle. Use 403 only where the resource's existence is already public.
6. **Return a narrow result.** The return value is serialized into the `.data` payload and is readable in the browser — see §4.

Same six rules for a `loader`, minus the body parsing. A loader that returns `db.user.findUnique({ where: { id: params.id } })` publishes `passwordHash`, `email`, `stripeCustomerId` and everything else on that row. `select` the fields the UI needs.

**`params` is attacker input.** `params.postId` comes from the URL. So do `request.url` search params, every `formData` field, every header, and any hidden input. Re-derive tenant, user and role from the session on the server — never from anything the client sent.

---

## 2. Middleware: the chain, the context, and the gaps

`v8_middleware` stopped being a flag in v8. A route module exports an ordered array; the chain wraps parent→child and unwinds child→parent.

```ts
import { createContext, redirect } from "react-router"
export const userContext = createContext<User | null>(null)

export const middleware: Route.MiddlewareFunction[] = [
  async ({ request, context }, next) => {
    const user = await getUser(request)
    if (!user) throw redirect("/login")
    context.set(userContext, user)          // typed; no string-key collisions
    const response = await next()           // runs the rest of the chain + the handlers
    response.headers.set("X-Frame-Options", "DENY")
    return response                         // server middleware MUST return the Response
  },
]
```

Rules that bite:

- **`next()` may be called at most once.** A second call throws. Omitting it entirely is allowed and auto-calls the rest of the chain — but then you cannot post-process.
- **`next()` never throws.** A handler error comes back as a `Response` the ErrorBoundary already rendered, often with status 500. Middleware that assumes success will decorate an error page.
- **Throwing before `next()`** bubbles to the highest route with a `loader`, and that boundary has no `loaderData`. Throwing after `next()` bubbles from the throwing route. Design the ErrorBoundary for the no-data case.
- **`clientMiddleware` returns nothing** — there is no HTTP `Response` on the client — and it runs on every client navigation whether or not any loader runs.
- **`getLoadContext(req, res)`** (custom servers) seeds a `RouterContextProvider` before the chain starts; Data Mode's equivalent is the `getContext` router option. This is where a request-scoped DB handle or trace id belongs.

### Why centralising authorization here is a trap

**Server middleware runs only for document requests and `.data` requests.** A client-side navigation to a route with **no loader** issues no `.data` request, so that route's server middleware never runs. The documented workaround is a loader whose only job is to force the round trip:

```ts
export async function loader() { return null }
```

**Server context is request-scoped.** An SPA form submission is a POST followed by a separate GET for revalidation; nothing `context.set()` during the POST survives into the GET. `clientMiddleware` context spans a navigation and does not have this problem — which is exactly why it is the wrong place for an authorization decision.

The failure mode is not that middleware is broken. It is that a root middleware *looks* like it covers every route, so nobody re-checks the leaves. **Centralisation becomes obscurity.** The safe split:

| Put in middleware | Keep in the handler |
|---|---|
| Request logging and timing | `requireUser()` / `requireRole()` |
| Response headers (CSP, `X-Frame-Options`, `Vary`) | Object-level ownership checks |
| Seeding a request-scoped DB handle or trace id | Argument validation |
| Origin/CSRF checks on state-changing methods | Anything whose absence is silently exploitable |

If you do authenticate in middleware, list in the Gate 5 design every route that has no `loader`, and say what protects it.

---

## 3. `clientLoader` / `clientAction` — placement, not preference

| | Initial SSR load | Client navigation |
|---|---|---|
| `loader` | runs (server) | runs (server, via `.data`) |
| `clientLoader` | **does not run** unless `clientLoader.hydrate = true as const` | runs instead of the loader |
| `clientAction` | n/a | runs instead of the action |

- With `clientLoader.hydrate = true`, the route renders nothing on first paint until the client loader resolves — so it **requires** a `HydrateFallback` export.
- `serverLoader()` / `serverAction()` inside a client handler are network calls to the `.data` endpoint. Awaiting one after the document already loaded is a second round trip (see the waterfall footgun).
- `clientLoader` return values are **not** serialized — they can be class instances, functions, anything.

Legitimate uses: reading `localStorage`/`IndexedDB`, an in-memory client cache in front of `serverLoader()`, a client-only third-party SDK call, invalidating a client cache in `clientAction` before delegating to `serverAction()`.

**Not a legitimate use: an authorization check.** The `.data` endpoint stays open regardless. In **SPA mode** (`ssr: false`) `loader` and `action` do not exist at runtime at all, so `clientLoader`/`clientAction` are the only handlers — and the authorization must live in whatever API they call.

---

## 4. Single fetch and the serialization boundary

Framework Mode batches every loader for the matched routes into one `.data` request, serialized with React Router's vendored turbo-stream.

**Survives the boundary:** `Promise`, `Date`, `Map`, `Set`, `BigInt`, `RegExp`, `URL`, `Error`, `undefined`, `NaN`, `Infinity`, plus plain objects and arrays.
**Does not:** class instances (arrive as plain objects, methods gone), functions, getters, `Symbol`, circular structures you did not intend.

Returning a promise instead of awaiting it streams that value after the initial payload:

```ts
export async function loader({ params }: Route.LoaderArgs) {
  const post = await db.post.findUnique({ where: { id: params.id }, select: { id: true, title: true, body: true } })
  const comments = db.comment.findMany({ where: { postId: params.id } })   // NOT awaited
  return { post, comments }
}

<Suspense fallback={<CommentsSkeleton />}>
  <Await resolve={loaderData.comments}>{(c) => <Comments items={c} />}</Await>
</Suspense>
```

An un-awaited promise that rejects with no `<Await>`/`errorElement` around it becomes an unhandled rejection on the client. Attach the boundary in the same change.

**Security property of this boundary:** every field every loader on the route returns is in the payload and readable in DevTools, whether or not a component renders it. Filter server-side, `select` narrowly, and return a DTO. In production React Router replaces thrown server error messages with a generic string before serialization — do not defeat that by catching an error in the loader and returning `err.message`.

---

## 5. `redirect()`, `data()`, and throw-versus-return

| Call | Returns | Return it | Throw it |
|---|---|---|---|
| `redirect(url, init?)` | a `Response` | works **only** directly inside the loader/action | the correct form inside any helper — it is a non-local exit |
| `redirectDocument(url)` | a `Response` | same | forces a full document load, not a client navigation |
| `replace(url)` | a `Response` | same | redirect that replaces the history entry |
| `data(value, init?)` | a wrapper | normal loader data with a status/headers attached | goes to the nearest `ErrorBoundary`; narrow it with `isRouteErrorResponse()` |

The bug this prevents: a `requireUser()` helper that **returns** `redirect("/login")` hands a `Response` back to a caller that ignores it and keeps executing — the guard renders as a no-op and the unauthenticated path continues into the database. Throw.

Use `redirectDocument()` when the destination is outside the router, after a login/logout that changes the session cookie, and after a deploy-version mismatch — anywhere the client-side bundle's assumptions must be discarded.

---

## 6. `headers` and `shouldRevalidate`

**`headers`** runs server-side on the document response. In a nested match the deepest route exporting `headers` wins; merge `parentHeaders` explicitly if a layout's cache policy has to survive.

```ts
export function headers({ parentHeaders }: Route.HeadersArgs) {
  parentHeaders.set("Cache-Control", "private, no-store")
  return parentHeaders
}
```

A `Cache-Control: public, max-age=…` on a personalised route caches one user's HTML at the CDN and serves it to the next. Personalised routes: `private, no-store`. Add `Vary` for anything that varies on a request header. `.data` responses are cached independently of the document — reason about both.

**`shouldRevalidate`** narrows the default, which is that *every* loader on the page re-runs after *any* action.

```ts
export function shouldRevalidate({
  currentUrl, nextUrl, formAction, actionResult, defaultShouldRevalidate,
}: Route.ShouldRevalidateArgs) {
  if (formAction === "/prefs/theme") return false      // this mutation cannot affect this route
  if (currentUrl.pathname === nextUrl.pathname) return false   // only search params changed
  return defaultShouldRevalidate                        // ALWAYS fall through
}
```

- A bare `return false` is how a mutation silently stops appearing in the UI. It also opts the route out of `useRevalidator()` and of revalidation after an error.
- It runs on **both** server and client.
- In SPA mode it behaves like Data Mode: no automatic revalidation on navigation, only after actions.
- Cost lives in the loaders. If revalidation is expensive, the fix is usually a cheaper loader or `select`ing fewer columns, not switching it off.

---

## 7. `useFetcher` concurrency and races

What React Router handles:

- A new navigation or submission **cancels** the in-flight one, matching browser behaviour.
- Fetchers do **not** interrupt each other — many can be in flight — but **a fetcher interrupts itself**: `fetcher.submit()` or `fetcher.load()` cancels that fetcher's pending request. This is why the type-ahead pattern is correct with no AbortController.
- Among concurrent revalidations it commits fresh responses and drops any that started earlier than one already committed, so a slow earlier response cannot overwrite a fast later one.

What it does not handle, and you must:

- **Cancellation is client-side only. The server already ran the action.** Double submits, retries, and offline replay need a server-side idempotency key. `mir-backend` owns that mechanism; this module owns knowing that you need it.
- **`useFetcher()` with no `key` is component-scoped.** In a list, each row gets its own fetcher — usually what you want. Pass an explicit `key` when two components must observe the same submission, and remember a keyed fetcher's `data` persists after unmount/remount.
- **`fetcher.data` is sticky.** It keeps the previous result while `state === "submitting"`, which is what makes optimistic UI easy and what makes "why is the old error still showing" a recurring bug. Use `fetcher.reset()` or key on `fetcher.state`.
- **Optimistic UI:** read `fetcher.formData` to render the pending value. Pair it with the React tier's `useOptimistic` rules — the rollback path is a Gate 3 state, not an afterthought.

---

## 8. Sessions, cookies, and CSRF

- `createCookieSessionStorage` stores the session **in the cookie**. It is **signed, not encrypted** — the client can read every byte. Store an id; re-read roles and permissions server-side. `createFileSessionStorage` / `createMemorySessionStorage` / a database store keep the payload server-side.
- Always pass `secrets: [...]`. Rotate by prepending the new secret; older entries still verify existing cookies. An **unsigned** cookie is what turned the `createFileSessionStorage` path traversal (CVE-2025-61686) from a bug into a critical.
- Cookie options: `httpOnly: true`, `secure: true`, `sameSite: "lax"`, explicit `path`, explicit `maxAge`. Rotate the session id on privilege change (login, role change, impersonation).
- **React Router ships no CSRF protection.** `SameSite=Lax` covers most cross-site POSTs, but three advisories in this module's Security table are CSRF — add an explicit defence on every state-changing route: an `Origin`-against-`Host` check in middleware, or a double-submit token in the form and the session. Then re-authorize in the action anyway; CSRF defence is not authorization.
- **Never pass a user-supplied string to `redirect()` or `<Link to>`.** Four 2026 open-redirect advisories were bypasses of each other's fixes (backslashes, `//`-prefixed protocol-relative paths, untrusted `returnTo` params). Validate with `new URL(value, origin)`, require the resolved origin to equal yours, and reject a pathname beginning `//` or `\`. An allow-list of known internal paths is stronger than any sanitizer.

---

## 9. Gate 5 checklist for the data layer

For each route in the change, state:

1. Mode — Framework, Data or Declarative; `ssr` and `prerender` from `react-router.config.ts`.
2. `loader` / `clientLoader` split, and whether `clientLoader.hydrate` is set (and its `HydrateFallback`).
3. Where authorization is enforced for that route. If middleware: does the route have a `loader`?
4. Exactly which fields leave the server, and why each one is safe in the `.data` payload.
5. Which mutations revalidate what, and every `shouldRevalidate` that returns `false`.
6. Every fetcher, its `key`, and whether its action is idempotent server-side.
7. The `Cache-Control` on each route and whether the route is personalised.
8. Where each `ErrorBoundary` sits, and what its parent layout still needs in order to render.
