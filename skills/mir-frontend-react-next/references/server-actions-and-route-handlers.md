# Server Actions and Route Handlers — depth

Read at Gate 6 before writing any `'use server'` file or `route.ts`.
Verified against Next.js 16.3 docs, 13 Aug 2026.

## 1. What a Server Action actually is

Every exported function in a `'use server'` file becomes a POST endpoint on the deployed app.
It is reachable by a direct HTTP request. It does not require the page it lives on to have been
rendered, and it does not inherit that page's auth check.

What Next.js gives you, and what each thing is not:

| Built-in | What it does | What it is not |
|---|---|---|
| Encrypted, non-deterministic action IDs, regenerated per build (cached max 14 days) | Makes the endpoint hard to guess | Not authorization. CVE-2026-64643 disclosed these IDs to unauthenticated callers |
| Dead-code elimination of unreferenced actions | An unused export is not compiled into a public endpoint | Not authorization. Any action you actually use is live |
| POST-only | Removes the GET-side-effect class of CSRF | Not authorization |
| `Origin` compared to `Host` / `X-Forwarded-Host` | Rejects cross-site invocation | Not authorization. Same-origin XSS still calls it |
| Closure variables encrypted with a per-build key | Keeps a captured value out of the client payload | "We don't recommend relying on encryption alone." `.bind()` args are **not** encrypted |

## 2. The template

Every action, every call, in this order.

```ts
'use server'
import { verifySession } from '@/lib/dal'      // server-only module
import { updateTag } from 'next/cache'
import { z } from 'zod'

const Input = z.object({
  postId: z.string().uuid(),
  title: z.string().min(1).max(200),
})                                              // no `authorId`, no `status`, no `isPublished`

export async function renamePost(raw: unknown) {
  // 1. Authenticate. Not the page — here.
  const session = await verifySession()
  if (!session) throw new Error('Unauthorized')

  // 2. Validate. TypeScript annotations do not exist at runtime.
  const { postId, title } = Input.parse(raw)

  // 3+4. Authorize the object IN the write, and mutate only schema-allowed fields.
  //      One statement: no window between the check and the update.
  const { count } = await db.post.updateMany({
    where: { id: postId, authorId: session.userId },
    data: { title },
  })
  if (count !== 1) throw new Error('Not found')   // 404, not 403 — do not confirm the row exists

  // 5. Invalidate at write granularity.
  updateTag(`posts:${session.userId}`)

  // 6. Return the minimum. The return value is serialized to the client.
  return { ok: true }
}
```

Notes on each step:

- **Step 1** must run in the action even if the page redirected unauthenticated users. The docs
  are explicit: a page-level authentication check does not extend to the Server Actions defined
  within it. `redirect('/login')` in the page controls which UI renders. It does not close the
  endpoint.
- **Step 2**: `FormData` values are strings or `File`s and every key is attacker-controlled.
  Never `Object.fromEntries(formData)` straight into an ORM `data` object — that is mass
  assignment and it hands the client `role`, `tenantId`, `price`, `isAdmin`.
- **Step 3+4** is the one AI skips. "Is logged in" is authentication. "Owns this row" is
  authorization. Put the owner/tenant predicate in the `WHERE` clause of the write itself and
  assert the affected-row count — a separate `findUnique` → `if` → `update({where:{id}})` has a
  window between the check and the write, and the write is then scoped only by an
  attacker-supplied id. Same rule for list queries: filter by `userId`/`tenantId` in `WHERE`,
  never fetch everything and filter in JavaScript.
- **Step 6**: returning `db.post.update(...)` returns the whole row, internal columns included.
- **`redirect()` and `notFound()` throw.** Inside a `try`/`catch` in the action body, your `catch`
  swallows `NEXT_REDIRECT` and the navigation silently never happens. Call them after the
  `try`/`catch`.
- **This template is the authorization half only.** Idempotency keys for retryable writes,
  transaction boundaries across multiple tables, and firing external side effects only after
  commit are `mir-backend`'s material — a Server Action that charges a card or sends mail needs
  them, and nothing here provides them.

### Push it into a Data Access Layer

Keep the `'use server'` file thin and put auth + authz + the query in a `server-only` module,
so every caller gets the check whether it came from an action, a Route Handler, or a Server
Component.

```ts
// data/posts.ts
import 'server-only'
export async function deletePost(postId: string) { /* session, ownership, delete */ }

// app/actions.ts
'use server'
import { deletePost } from '@/data/posts'
export async function deletePostAction(id: string) { await deletePost(id); revalidatePath('/posts') }
```

`import 'server-only'` works in both files. `'use server'` modules resolve in a server-only
layer, so importing the action into a Client Component (for `useActionState`) still builds.

## 3. Closures, `.bind`, and the encryption key

```tsx
export default async function Page() {
  const publishVersion = await getLatestVersion()
  async function publish() {
    'use server'
    if (publishVersion !== (await getLatestVersion())) throw new Error('Stale')
  }
}
```

`publishVersion` travels to the client and back. Next.js encrypts it with a per-build key.
`deletePost.bind(null, post.id)` does **not** encrypt — that is a deliberate opt-out for
performance, and the bound value is visible in the payload. Either way, the argument list is
hostile input and must be re-validated on arrival.

Self-hosting across multiple instances: set `NEXT_SERVER_ACTIONS_ENCRYPTION_KEY` to the same
base64 value everywhere (decoded length 16, 24, or 32 bytes; `openssl rand -base64 32`).
Without it each instance generates its own key and decryption fails across instances. With it,
you own the rotation schedule.

## 4. CSRF and origins

Server Actions are POST-only and compare `Origin` to `Host` (or `X-Forwarded-Host`); a mismatch
aborts the request. Behind a reverse proxy, or when the server API domain differs from the
production domain, enumerate the safe origins rather than removing the check:

```js
// next.config.js
module.exports = {
  experimental: { serverActions: { allowedOrigins: ['my-proxy.com', '*.my-proxy.com'] } },
}
```

Server Actions do not use CSRF tokens, so an XSS on your own origin can call any action the
victim is authorized for. Sanitizing rendered HTML is part of the CSRF defence here, not a
separate concern.

## 5. Rate limiting

Nothing is rate limited by default. An action that sends email, resets a password, writes rows,
or calls a paid API needs a limiter keyed by user id and IP. This is also the mitigation you
control for the Server Action DoS class (CVE-2026-64641, CVE-2026-64646) beyond patching.

## 6. Route Handlers (`route.ts`)

Choose a Route Handler when the caller is not your own UI:

- Third-party webhooks (Stripe, GitHub) — verify the signature before doing anything else.
- OAuth callbacks.
- A public or versioned JSON API consumed by other clients.
- Non-POST methods, streaming/SSE, file downloads, `robots.txt`/`sitemap.xml` style outputs.

Checklist, because none of the Server Action protections apply:

- [ ] Auth check inside the handler — 401 when unauthenticated, 403 when unauthorized.
- [ ] CSRF: you write it. Bearer-header auth needs none; cookie auth does.
- [ ] Only export the methods you intend. There is no catch-all, but an exported `GET` you
      forgot about is a live endpoint.
- [ ] A `GET` handler that sets cookies or mutates is a design error — that is the CSRF hole
      the App Router otherwise avoids by never using GET for side effects.
- [ ] Validate `params` and query values. Bracket folders are user input.
- [ ] CORS headers, if any, from an allow-list. Never reflect `Origin` back with credentials.
- [ ] Bound the request body; nothing caps it for you.
- [ ] Return generic errors. A driver error string carries table and column names.
- [ ] Under Cache Components, `GET` Route Handlers follow the same prerendering model as pages.

**Do not** create an internal `/api/x` route and `fetch('/api/x')` from a Server Component. That
is an HTTP round trip into your own process, it loses the request memoization, and it duplicates
the auth check. Import the function.

## 7. Mutations during render

Next.js blocks setting cookies and triggering revalidation during render, on purpose. A mutation
driven by `searchParams` (`?logout=1`) is a side effect on a GET and reintroduces CSRF. Use an
action.

## 8. Audit list

From the framework's own guidance, worth running against a diff:

- `"use server"` files — is the user re-authorized inside the action? Is ownership of the
  resource checked, not just login? Are arguments validated? Are return values filtered? Is DB
  access delegated to a `server-only` module?
- `"use client"` files — do the prop types accept private data? Are they overly broad?
- `/[param]/` folders — bracket folders are user input; are params validated?
- `proxy.ts` and `route.ts` — the two files with the most power and the fewest guardrails.
- Data Access Layer — are database packages and `process.env` imported anywhere outside it?
