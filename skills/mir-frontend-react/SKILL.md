---
name: mir-frontend-react
description: "Make It Right (React reactivity tier). React 19 + React Compiler reactivity footguns shared across EVERY React meta-framework (Next.js, React Router 7/Remix, TanStack Start, Vite SPA) — distinct from the generic frontend gates and from any one framework's mechanics. Covers the Rules of Hooks, effect-dependency discipline (derive in render; effects are for external sync only), stale closures, list-key correctness, use() and promise identity, granular Suspense + Error Boundary placement, useTransition/useDeferredValue for INP, React Compiler 1.0 interop (blind useMemo/useCallback is now a liability; the 'use no memo' opt-out), the server-state-vs-client-state boundary (TanStack Query, not useState mirrors), and React-layer security (raw-HTML props, LLM-output rendering, secrets in the bundle). Chains: mir-frontend → this → mir-frontend-react-next. TRIGGER when the reactivity library is React, including React Server Components — render purity, promise identity and Suspense placement apply on the server too. SKIP for Vue (mir-frontend-vue), Angular, Svelte, and plain-DOM work (mir-frontend-vanilla). SKIP for Next.js mechanics — RSC boundary wiring, Server Actions, file routing, framework caching and middleware belong to mir-frontend-react-next. SKIP for backend code (mir-backend)."
trigger: /mir-frontend-react
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-frontend-react · Make It Right (React reactivity tier)

Middle tier. `mir-frontend` decides **what is correct** (any reactive UI); this owns **React's reactivity model**, shared by all React meta-frameworks; the framework module knows the library's mechanics. Load order: `mir-frontend` → `mir-frontend-react` → `<framework module>`.

These footguns apply equally to Next.js, React Router 7 (Framework Mode), TanStack Start, and a Vite SPA. Framework-specific wiring (RSC boundary, Server Actions, hydration, file-based routing, framework caching) lives in the framework module — not here.

## Version floor (checked against the npm registry and react.dev, 13 Aug 2026)

| Package | Current stable | Notes |
|---|---|---|
| `react` / `react-dom` | **19.2.8** (21 Jul 2026) | React 19 is still the current major. 19.3 exists only as a canary; there is no React 20. |
| `react-server-dom-webpack` / `-parcel` / `-turbopack` | 19.2.8 · 19.1.9 · 19.0.8 | Security floor, not a preference — see Security. |
| `babel-plugin-react-compiler` | **1.0.0** (7 Oct 2025) | Still the only stable release. No 1.1 line exists. |
| `react-compiler-runtime` | 1.0.0 | Only needed when targeting React 17/18. On React 19 the compiler imports `react/compiler-runtime`. |
| `eslint-plugin-react-hooks` | **7.1.1** (17 Apr 2026) | Requires Node 18+; supports ESLint 9 and 10. |
| `eslint-plugin-react-compiler` | never left RC (`19.1.0-rc.2`) | Dead package, folded into `eslint-plugin-react-hooks`. Uninstall it. |

**Two things earlier versions of this skill got wrong.**

1. "Lint with `eslint-plugin-react-hooks` v6" — v7 is the current line. v7 also changed what the preset does: the React Compiler rules (`set-state-in-effect`, `set-state-in-render`, `purity`, `immutability`, `refs`, `preserve-manual-memoization`, `static-components`, `error-boundaries`, `use-memo`, `globals`, `gating`, `config`, `unsupported-syntax`, `incompatible-library`) are **on in `recommended`**, not opt-in. `recommended-latest` adds the experimental compiler rules on top. Upgrading v6 → v7 turns CI red on code that passed before; budget for that as a task, not a surprise.
2. The Compiler does **not** delete your `useMemo`/`useCallback`. It preserves them. See footgun 12 — the failure mode is different from what the old text said.

**Behaviour that changed since this skill was written:**

| Changed in | What changed | What it breaks |
|---|---|---|
| React 19.2 | `useId` prefix is now `_r_` (was `«r»` in 19.1, `:r:` in 19.0) | Snapshot tests, and any CSS or test selector written against a generated id |
| React 19.2 | Server-rendered Suspense boundaries batch their reveals | A staircase of boundaries no longer appears one at a time — see footgun 8 |
| `eslint-plugin-react-hooks` 7.0 | Compiler rules enabled in `recommended` | Existing code that never ran the compiler now fails lint |

## The React reactivity footguns AI walks into (framework-agnostic)

### 1. Rules of Hooks — call order is the identity
React ties hook state to **call-site position** in the render sequence. Violating this corrupts state silently or throws at runtime.
- Never call hooks **inside conditions, loops, nested functions, or `try`/`catch`/`finally` blocks**. The try/catch clause is explicit in the current docs — it is not folklore.
- Never call hooks after a conditional `return`, from an event handler, or from a plain (non-component, non-custom-hook) function.
- Custom hooks **must start with `use`** — the linter and the Compiler both treat this as the hook boundary signal.
- **`use()` is the one documented exception**: it may be called inside conditions and loops. It still may not be called inside `try`/`catch` (`eslint-plugin-react-hooks` has enforced that since 6.1.0). See footgun 7.
- **`useEffectEvent()` has its own rule**: the returned function must not go in a dependency array, must not be passed to another component or hook, and may only be called from Effects declared in the same component or custom hook.

```tsx
if (isAdmin) { const [x, setX] = useState(0) }                       // BAD — breaks call order
const [x, setX] = useState(0); const visible = isAdmin ? x : null    // GOOD — call, then branch
```

### 2. Derived state in useState — the #1 React footgun
Storing **derived data** in a separate `useState` and keeping it in sync via `useEffect` is the single most common React mistake. It causes stale windows, double renders, and effect loops. `react-hooks/set-state-in-effect` reports it as an error under the v7 `recommended` preset.

**Rule:** store the *source of truth* once; derive everything else **during render**.

```tsx
const [filtered, setFiltered] = useState([])                      // BAD — mirror + sync
useEffect(() => { setFiltered(items.filter(fn)) }, [items, fn])   //       (classic AI output)

const filtered = items.filter(fn)                                 // GOOD — derive in render
```

The documented exception is measurement: `setState` inside a `useLayoutEffect` whose value comes from a ref (`ref.current.getBoundingClientRect()`) is legitimate — there is no other way to read layout.

### 3. Effect-dependency discipline — effects are for external system sync only
`useEffect` is for synchronizing with **non-React external systems** (subscriptions, DOM mutations, third-party widgets, analytics). It is **not** for data transformation, derived state, or "reacting to prop changes."

- **Missing deps → stale closures.** The effect sees the values from the render it was born in.
- **Object/array literal deps → infinite loops.** `{}` and `[]` are new references every render.
- **Exhaustive-deps lint is mandatory.** `eslint-plugin-react-hooks` 7.1.1, `recommended` preset.

```tsx
// BAD — effect for derived state; causes an extra render
useEffect(() => { setTotal(cart.reduce((a, i) => a + i.price, 0)) }, [cart])

// GOOD — effect for an external subscription (cleanup required)
useEffect(() => {
  const sub = store.subscribe(handler)
  return () => sub.unsubscribe()
}, [store, handler])
```

### 4. Stale closures — the closure captures, not the live value
Event handlers, `setTimeout`/`setInterval` callbacks, and subscription listeners **close over** the values present when they were created. If those values change before the callback fires, the callback sees stale data.

```tsx
// BAD — stale closure; `count` is always 0 inside the interval
useEffect(() => { const id = setInterval(() => setCount(count + 1), 1000); return () => clearInterval(id) }, [])

// GOOD — functional update; no stale closure
useEffect(() => { const id = setInterval(() => setCount(c => c + 1), 1000); return () => clearInterval(id) }, [])

// GOOD (React 19.2+) — useEffectEvent for the "read latest, don't re-subscribe" case
const onMessage = useEffectEvent((msg) => log(msg, theme))   // theme is always current
useEffect(() => {
  const c = connect(roomId); c.on("message", onMessage); return () => c.close()
}, [roomId])                                                  // onMessage is NOT a dep
```

`useEffectEvent` replaces the hand-rolled `handlerRef` + `useLayoutEffect` latest-value pattern. It is stable as of React 19.2 — do not reach for the ref version on 19.2+.

### 5. List keys — array index is wrong for dynamic lists
React uses `key` to match VDOM nodes across renders. An index key means **row N always gets the state that was on row N before** — when items are reordered, filtered, or prepended, state (focus, input values, animations) silently attaches to the wrong row.

```tsx
{items.map((item, i) => <Row key={i} item={item} />)}      // BAD — reorder corrupts row state
{items.map(item => <Row key={item.id} item={item} />)}     // GOOD — identity from the data
```

Index keys are acceptable only for **static, append-only, never-reordered** lists.

### 6. Controlled vs uncontrolled — pick one, never switch
A controlled input owns its value via React state (`value` + `onChange`). An uncontrolled input owns its value in the DOM (`defaultValue` + optional `ref`). Switching at runtime (passing `undefined` then a string, or the reverse) triggers a React warning and can corrupt the field's display state.

- Large forms: prefer uncontrolled (React Hook Form / TanStack Form with refs) — avoids per-keystroke re-renders.
- Live validation / format-on-input: controlled.
- Never initialize a controlled input with `undefined` — use `""`.

```tsx
<input value={user?.name} onChange={...} />          // BAD — undefined → string flips the mode
<input value={user?.name ?? ""} onChange={...} />    // GOOD — always a defined string
```

### 7. `use()` — promise identity is the whole contract
`use(promise)` suspends until the promise settles. It reads the promise by **identity**, so a promise created during render is a new promise every render: React suspends, re-renders, creates another promise, suspends again. The fallback never goes away.

```tsx
const data = use(fetch("/api/albums").then(r => r.json()))               // BAD — new promise
                                                                         // every render; never settles
function Albums({ albumsPromise }) { const albums = use(albumsPromise) } // GOOD — created outside
```

- Create the promise in a Server Component, a cached function, a route loader, or a data library — never in the client component's render body.
- Never read `promise.status` / `promise.value` yourself. Those are React-internal fields.
- `use()` may be called conditionally and in loops, but **not inside `try`/`catch`** — catch failures with an Error Boundary instead.
- `use(SomeContext)` is not supported in Server Components.

### 8. Suspense + Error Boundary placement — granular, and know what actually suspends
One `<Suspense>` at the root hides the **entire page** behind a spinner while any subtree loads. One `<ErrorBoundary>` at the root gives users no recovery path in the affected region.

**An Error Boundary only catches errors thrown while rendering** — during render, in lifecycle methods, and in the constructors of the tree below it. It does **not** catch errors in event handlers, in `setTimeout`/`Promise` callbacks, or in server rendering. Those need a `try`/`catch` at the call site that puts the failure into state. The exception: an error thrown inside `startTransition` (or an Action) *does* reach the nearest boundary, which is one reason to route mutations through Actions.

**Suspense only activates for:** `lazy()`, reading a promise with `use()`, `<link rel="stylesheet">` with `precedence`, streaming server rendering, and fonts/images inside `<ViewTransition>`. **A `fetch` inside `useEffect` never triggers a Suspense boundary** — a `<Suspense>` wrapped around a `useEffect` fetcher is dead code, and AI writes that pairing constantly.

Three behaviours that change how you place boundaries:
- React reveals suspended content **at most once every 300 ms**, measured from the last reveal. Boundaries that become ready inside that window reveal together. Splitting one boundary into six does not buy six separate paint steps.
- A component that suspends **before it first mounts loses all state** from that attempt — React re-renders the tree from scratch. Do not initialise expensive state above a boundary that can suspend on first load.
- Re-suspending an already-visible tree shows the fallback again **unless** the update was wrapped in `startTransition` or came from `useDeferredValue`, and React cleans up and re-runs layout effects in that tree.

```tsx
// BAD — one root Suspense; any suspension hides Header too
<Suspense fallback={<Spinner />}><Header /><UserProfile /><Feed /></Suspense>

// GOOD — granular boundaries; Header stays mounted
<Header />
<Suspense fallback={<ProfileSkeleton />}>
  <ErrorBoundary fallback={<ProfileError onReset={reset} />}><UserProfile /></ErrorBoundary>
</Suspense>
<Suspense fallback={<FeedSkeleton />}><Feed /></Suspense>
```

`useSuspenseQuery` without server prefetch → server-then-client double-fetch waterfall. Prefetch/dehydrate wiring lives in the framework module.

### 9. Actions, `useActionState`, `useOptimistic` — the React 19 mutation path
React 19 made async functions passed to `startTransition` or to `<form action>` first-class ("Actions"). Pending state, error routing to the nearest Error Boundary, and optimistic reverts hang off that. Hand-rolled `useState` pending flags reimplement it worse.

```tsx
const [saving, setSaving] = useState(false)   // BAD — hand-rolled pending, no rollback

// GOOD — pending, error and result all come from the Action
const [state, submit, isPending] = useActionState(async (prev, formData) => {
  const res = await saveName(formData.get("name"))
  return res.error ? { error: res.error } : { ok: true }
}, { ok: false })
<form action={submit}>…</form>
```

- **`useOptimistic`'s setter must be called inside an Action or `startTransition`.** Called outside one, React warns and the optimistic value renders for a frame and then reverts — which looks exactly like a flaky backend.
- The optimistic value reverts automatically when the Action settles; there is no separate "clear" render. If the Action throws, the UI falls back to the real `value`. Do not write manual rollback state on top of it.
- `useFormStatus` only tracks a **parent** `<form>`. Called in the component that renders the `<form>` itself, it still returns an object — but a permanently inert one (`pending: false`, `data`/`method`/`action` `null`), so the submit button never disables and nothing errors. It must be called from a child.
- `<form action={fn}>` resets the uncontrolled form on success. If you need the values kept, control them or return them from the Action.

### 10. Concurrent rendering / StrictMode — render must be pure
React 19's concurrent renderer may **invoke render functions more than once** for the same committed output (scheduling, prerendering, offscreen work). StrictMode double-invokes render and effects in development specifically to surface impurity.

- **Never mutate external state or produce side effects during render.** Reading is fine; writing (incrementing a counter, pushing to an array, scheduling a timeout) is not. `react-hooks/purity` and `react-hooks/set-state-in-render` catch the common shapes.
- **StrictMode double-invoke is a detector, not a bug.** The fix is to make the component pure, not to remove `<StrictMode>`.

```tsx
let renderCount = 0
function MyComponent() { renderCount++ }                     // BAD — mutation during render

const renderCount = useRef(0)
useEffect(() => { renderCount.current++ })                   // GOOD — mutate after commit
```

### 11. useTransition / useDeferredValue — protect INP on heavy updates
INP target is ≤ 200 ms. A heavy re-render triggered on every keystroke blocks the main thread. Mark non-urgent updates so React keeps the input responsive.

```tsx
const results = expensiveFilter(items, query)           // BAD — every keystroke blocks

const deferredQuery = useDeferredValue(query)           // GOOD — input stays responsive
const results = expensiveFilter(items, deferredQuery)   //        (Compiler memoizes this)

const [isPending, startTransition] = useTransition()    // GOOD — explicit user actions
startTransition(() => setActiveTab(next))
```

`useDeferredValue`: expensive read from existing state. `useTransition`: explicit state-changing user action. Both also stop a re-suspension from flashing the Suspense fallback (footgun 8).

### 12. React Compiler 1.0 interop — it keeps your manual memoization, wrong deps and all
React Compiler 1.0 auto-memoizes pure components and derivations. What it does with existing manual memoization is the part people get wrong: **it preserves your `useMemo`, `useCallback`, and `React.memo` — it does not remove them.** It assumes you had a reason.

The cost lands elsewhere. If a manual `useMemo`'s dependency array is incomplete, the compiler can no longer follow the data flow through it, so it stops applying further optimizations to that code. You end up with hand-written memoization that is wrong *and* no automatic memoization to compensate. `react-hooks/preserve-manual-memoization` is the rule that reports it.

- Write idiomatic, pure components. Do not pre-emptively wrap with memo hooks.
- In existing code, **leave a correct `useMemo` in place** or test carefully before removing it — react.dev: "removing it can change compilation output." Leaving an *incorrect* one is the expensive case. In new code, write plain and let the Compiler do it.
- Adopt incrementally with `compilationMode: 'annotation'` (only functions marked `"use memo"` compile) or the `gating` option (runtime feature flag), rather than switching a large codebase on at once.
- `"use no memo"` is a **temporary debugging escape hatch**, not a permanent marker — react.dev: "intended as a temporary debugging tool, not a permanent solution." Every use gets a comment naming the underlying Rules-of-React violation or library incompatibility and the condition for removing it.

```tsx
const value = useMemo(() => items.map(t => t[key]), [items])  // BAD — `key` missing from deps;
                                                              //       compiler stops optimizing here
const value = items.map(t => t[key])                          // GOOD — Compiler handles it

function LegacyWidget({ data }) { "use no memo" }             // GOOD — explicit opt-out
```

### 13. Server state vs client state — TanStack Query, not useState mirrors
Server data has its own lifecycle: caching, background revalidation, deduplication, mutation invalidation. Mirroring it into `useState`/Context creates a second source of truth that goes stale, duplicates fetches, and makes invalidation manual.

```tsx
const [user, setUser] = useState(null)                                        // BAD — mirrored
useEffect(() => { fetch("/api/user").then(r => r.json()).then(setUser) }, [])

const { data: user } = useQuery({ queryKey: ["user"], queryFn: fetchUser })   // GOOD — owned
```

**Decision rule:**
- Server data (fetched from an API, shared across components, needs caching/invalidation) → **TanStack Query** (`useQuery` / `useSuspenseQuery` / `useMutation`).
- Client-only UI state (open/closed, selected tab, form draft, undo stack) → **`useState`**, Zustand, or Jotai.

SSR `queryClient`-per-request, `dehydrate`/`hydrate` wiring, and RSC-as-cacheable-data belong in the framework module — point there.

### 14. Context performance — unstable provider value re-renders all consumers
`React.createContext` uses referential equality. A provider passing `{...}` or `[...]` as `value` creates a new reference every render, re-rendering every consumer regardless of whether the data changed.

```tsx
// BAD — new object on every render; all consumers re-render always
<ThemeContext.Provider value={{ theme, setTheme }}>

// GOOD — split fast-changing state from the stable dispatch
<ThemeStateContext.Provider value={theme}>
<ThemeDispatchContext.Provider value={setTheme}>
```

Under the Compiler the object literal is memoized for you; the split-context fix still matters, because it is about *which* consumers re-render, not about reference stability. Frequently changing state (scroll offset, real-time feeds) must not live in a wide context — use Zustand/Jotai subscriptions or `useSyncExternalStore`.

### 15. Refs vs state — non-render data does not belong in state
`useState` re-renders on update. `useRef` persists across renders without triggering one. Wrong tool in either direction: state for timer IDs wastes render cycles; ref for render-affecting data means the UI never updates.

```tsx
const [timerId, setTimerId] = useState(null)   // BAD — timer ID in state re-renders for nothing
const timerRef = useRef(null)                  // GOOD

const countRef = useRef(0); countRef.current++ // BAD — render-affecting data in a ref; UI is stale
const [count, setCount] = useState(0)          // GOOD
```

Refs: DOM handles, interval/timeout IDs, previous-render values. Do not read or write `ref.current` during render — `react-hooks/refs` flags it.

### 16. `<Activity>` — hidden is not unmounted, and unmounted is not hidden
React 19.2 shipped `<Activity mode="visible" | "hidden">` as stable. Hidden children stay mounted with their state intact, but their Effects are **unmounted** and their updates are deprioritized. This is the correct tool for a tab or route you want to return to.

```tsx
{tab === "search" && <SearchPanel />}                                  // BAD — scroll position,
                                                                        // draft text, filters all die
<div style={{ display: tab === "search" ? "block" : "none" }}>…</div>  // BAD — timers and polling
                                                                        // keep running while invisible
<Activity mode={tab === "search" ? "visible" : "hidden"}><SearchPanel /></Activity>  // GOOD
```

Because Effects unmount when a subtree goes hidden, any cleanup you skipped shows up here as a leak. Write the cleanup.

---

## Security

React-layer mechanics. Server-side authorization, CORS, and rate limiting belong to `mir-backend`; RSC boundary rules, Server Action argument validation, and framework middleware belong to `mir-frontend-react-next`.

### The raw-HTML sinks React does not protect

React escapes text children. It does **not** escape these:

| Sink | React's behaviour | What to do |
|---|---|---|
| `dangerouslySetInnerHTML={{__html}}` | inserted verbatim | sanitize first, or render structured elements instead |
| `<iframe srcDoc={html}>` | inserted verbatim, **no sanitization at all** | the one people forget; treat it exactly like `__html` |
| `<div {...userObject}>` | every key becomes a prop | an attacker-controlled object can supply `dangerouslySetInnerHTML` |

Spreading an untrusted object into JSX is the component-layer version of mass assignment. Pick fields explicitly (`<Row title={o.title} href={o.href} />`); never spread a request body, a URL query object, or a CMS record into props.

**Sanitizer floor: `dompurify` ≥ 3.4.13 (3 Aug 2026).** DOMPurify shipped six patch releases between 3 June and 3 August 2026 for a cluster of bypasses, nearly all in `IN_PLACE` mode or in hook-based config mutation: CVE-2026-49458, CVE-2026-49459, CVE-2026-49978, CVE-2026-65898 through CVE-2026-65902, CVE-2026-66010, and GHSA-55q2-fjhq-7xh7 (detached-subtree XSS, affects every version before 3.4.13). Rules that follow from that set:
- Do not use `IN_PLACE: true`. Call `sanitize()` and render the returned string.
- Do not mutate `data.allowedTags` / `data.allowedAttributes` inside a hook — it permanently pollutes the module-level defaults for every later call.
- Pin the exact version and re-check after each advisory; a floating `^3.4.0` with a stale lockfile is how you stay on a bypassed build.

**Turn the sink into a crash instead of an XSS.** React ships with its Trusted Types integration enabled, so under a `Content-Security-Policy: require-trusted-types-for 'script'` header a plain string passed to `__html` is rejected at the DOM sink rather than executed. Most React starter templates ship a CSP with `'unsafe-inline'` or no CSP at all — that default is what turns one missed sanitizer call into a working XSS.

### URL props — what React blocks and what it does not

React's `sanitizeURL` replaces a `javascript:` URL with a stub that throws, on `href`, `src`, `action`, `formAction`, and `xlink:href`. That is the whole list.

- **Not blocked:** `data:`, `blob:`, and any other scheme. `srcDoc` and `__html` are not touched at all.
- Do the check yourself for user-supplied URLs: `const u = new URL(value, location.origin); if (u.protocol !== "https:" && u.protocol !== "http:") reject()`. Reject before render, not in a click handler.
- A user-controlled `src` or `href` sends the current URL in the `Referer` header to a third party. If your app ever puts a token or an id in the URL, that is a leak.

### Markdown, rich text, and LLM output

- `react-markdown` (10.1.0, current) is safe by default: raw HTML is escaped and `urlTransform` allow-lists protocols. Two changes break that — adding `rehype-raw` (re-enables raw HTML passthrough) and overriding `urlTransform`. If raw HTML is genuinely required, add `rehype-sanitize` with an explicit schema.
- `marked` / `markdown-it` output piped into a raw-HTML prop is XSS unless you sanitize between them. `marked` removed its `sanitize` option in v5 and has no built-in sanitizer. It also carries CVE-2026-41680 (HIGH): unbounded recursion in the tokenizer causes an out-of-memory crash, which matters if you render user markdown server-side.
- **Model output is untrusted input.** Streaming an LLM response straight into `__html`, or into `react-markdown` with `rehype-raw` on, is a direct injection path. Render it as text or through the default escaping pipeline. A model can also emit a `data:` URL, which React does not block.
- `components` overrides and every remark/rehype plugin are code you are trusting on that path. Review them like dependencies, because they are.

### Client-side authorization is a hint, never a gate

- `if (user.isAdmin)`, `<ProtectedRoute>`, and `React.lazy(() => import("./Admin"))` control what is *drawn*. None of them controls what is *fetched*. Lazy chunks are plain URLs; anyone can request them.
- Conditional rendering hides a DOM node, not the props behind it. If a record reached the TanStack Query cache, it is in memory and it was in the network response. **Filter on the server, per object.** A valid session token says who the caller is; it does not say the caller owns order 41. The server must check the row, on every request, including the ones only your UI knows how to make.
- Anything a client can send, a client can change: hidden inputs, `formData` fields, and query keys are all attacker-controlled. Re-derive tenant, user, and role server-side.

### Secrets in the bundle

- Every module a client component imports ends up in the bundle, including its module-scope constants. Bundlers additionally inline `process.env.*` and `import.meta.env.*` at build time — the value becomes a string literal in the output, not a lookup.
- No API key, signing key, service credential, or internal hostname belongs in client-reachable module scope. Verify it in CI by grepping the build output (`grep -RIn "sk_live\|BEGIN PRIVATE KEY\|SECRET" dist/`), not by reading source.
- Publishing source maps to a public origin re-exposes original identifiers, comments, and file paths. Upload them to the error tracker; do not serve them.
- Error reporting leaks props. `componentDidCatch` and the `createRoot(el, { onCaughtError, onUncaughtError, onRecoverableError })` callbacks receive the error plus a component stack; forwarding those to a third-party logger sends whatever was in scope. Redact before you send, and never render a server `error.message` into the UI.

### `react-server-dom-*` — eight advisories in eight months

If the app renders React Server Components or Server Functions, `react-server-dom-webpack`, `-parcel`, or `-turbopack` is in the tree (usually transitively, through the bundler integration). A client-only Vite SPA does not have them — check `npm ls react-server-dom-webpack` rather than assuming either way.

| Advisory | Class | Fixed in |
|---|---|---|
| CVE-2025-55182 (**CRITICAL**) | unauthenticated remote code execution | 19.0.1 / 19.1.2 / 19.2.1 |
| CVE-2025-55183 | source code exposure | 19.0.2 / 19.1.3 / 19.2.2 |
| CVE-2025-55184 (HIGH) | denial of service | 19.0.2 / 19.1.3 / 19.2.2 |
| CVE-2025-67779 (HIGH) | DoS — incomplete fix for the above | 19.0.3 / 19.1.4 / 19.2.3 |
| CVE-2026-23864 (HIGH) | multiple DoS | 19.0.4 / 19.1.5 / 19.2.4 |
| CVE-2026-23869 (HIGH) | DoS via crafted Server Function requests | 19.0.5 / 19.1.6 / 19.2.5 |
| CVE-2026-23870 (HIGH) | DoS — OOM / CPU exhaustion | 19.0.6 / 19.1.7 / 19.2.6 |
| CVE-2026-44907 (HIGH) | DoS in Server Functions | 19.0.8 / 19.1.9 / 19.2.8 |

**Floor: 19.2.8, or 19.1.9 / 19.0.8 on the backport lines.** Every one of these is reachable by an unauthenticated HTTP request to a Server Function endpoint — no login, no UI interaction. A `^19.2.0` range in `package.json` proves nothing; the lockfile decides. Pin it, and re-run `npm ls` after each React security release rather than trusting the range.

### Supply chain, React-specific

- `react` and `react-dom` must be the **exact same version**. Two copies of React in one tree produce "invalid hook call" errors and context that silently misses.
- Remove `eslint-plugin-react-compiler`. It never shipped a stable release and is now part of `eslint-plugin-react-hooks` v7.
- Install from the lockfile in CI (`npm ci`), and keep dependency install scripts off by default. The general npm hardening is the Node runtime tier's material and it applies to the frontend build unchanged — a compromised `postinstall` in a devDependency runs in the same job that produces your bundle.
- Auth material in the client: a token in `localStorage` survives any XSS on the page; a cookie-authenticated app needs a CSRF defence on every state-changing request; and a `fetch(url, { credentials: "include" })` to another origin is what makes a permissive server CORS policy exploitable. The server-side settings are `mir-backend`'s; the client-side flag is written here.

---

## How this slots into the pipeline

- **Gate 3 (UI state machine):** model the interaction in React terms — `useActionState` or `useReducer` for flows, XState for complex ones (IDLE / LOADING / SUCCESS / EMPTY / ERROR / STALE / RETRYING / OPTIMISTIC / ROLLING_BACK). Each footgun above maps to an edge AI tends to miss (no STALE state → derived-state-in-useState; no ROLLING_BACK edge → optimistic update without an Action).
- **Gate 5 (state ownership):** declare which state is server-owned (TanStack Query) vs client-owned (useState / Zustand / Jotai) and which components own rendering. Footguns 13, 14, and 15 are Gate-5 decisions. State the React patch version you target.
- **Gate 6 (implementation):** code against footguns 1–16. The Gate-6 codegen checklist in `mir-frontend` references this tier.
- **Gate 7 (review):** the reliability-reviewer checks async correctness (3, 4, 7, 8, 9, 13); the frontend-perf-reviewer checks 11, 12; the security-reviewer checks the Security section, and specifically that `react-server-dom-*` is at or above 19.2.8 / 19.1.9 / 19.0.8, that `dompurify` is ≥ 3.4.13, and that no authorization decision exists only in a component.

---

## Edit boundary (what belongs here vs. above/below)

**The 4-question placement test:**

1. True for Vue, Angular, and Svelte too (any reactive UI library)? → **up** to `mir-frontend`.
2. True for every React meta-framework (Next.js, React Router 7, TanStack Start, Vite SPA) because they all run React's reactivity model? → **here**.
3. Specific to one meta-framework's mechanics (RSC boundary, Server Actions, file-based routing, framework caching, hydration wiring, `generateMetadata`)? → **down** to `mir-frontend-react-next` or the equivalent module.
4. Different reactivity library (Vue, Angular, Svelte)? → its own `mir-frontend-<lib>` tier. Never widen this one.

Cross-ref: full edit map is in `mir-frontend/SKILL.md` → "Where these instructions live."
