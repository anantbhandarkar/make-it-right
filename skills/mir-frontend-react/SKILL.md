---
name: mir-frontend-react
description: "Make It Right (React reactivity tier). React 19 + React Compiler reactivity footguns shared across EVERY React meta-framework (Next.js, React Router 7/Remix, TanStack Start, Vite SPA) — distinct from the generic frontend gates and from any one framework's mechanics. Covers: the Rules of Hooks, effect-dependency discipline (derive in render, effects are for external sync only), stale closures, list-key correctness, controlled vs uncontrolled inputs, granular Suspense + Error Boundary placement, useTransition/useDeferredValue for INP, React Compiler 1.0 interop (blind useMemo/useCallback is now a liability; the 'use no memo' opt-out), concurrent-rendering/StrictMode double-invoke, and the server-state-vs-client-state boundary (TanStack Query, not useState mirrors). TRIGGER when the frontend reactivity library is React — sits between mir-frontend (generic) and the framework module (e.g. mir-frontend-react-next). SKIP for Vue/Angular/Svelte (each gets its own mir-frontend-<lib> tier), and for meta-framework-library mechanics (RSC boundary, Server Actions, file routing, framework caching — those live in the framework module)."
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

**Runtime assumed:** React 19.2 + React Compiler 1.0 GA available. These footguns apply equally to Next.js 16, React Router 7 (Framework Mode), TanStack Start 1.0, and a Vite SPA. Framework-specific wiring (RSC boundary, Server Actions, hydration, file-based routing, framework caching) lives in the framework module — not here.

## The React reactivity footguns AI walks into (framework-agnostic)

### 1. Rules of Hooks — call order is the identity
React ties hook state to **call-site position** in the render sequence. Violating this corrupts state silently or throws at runtime.
- Never call hooks **inside conditions, loops, or nested functions** — they must fire in the same order every render.
- Never call hooks from a plain (non-component, non-custom-hook) function.
- Custom hooks **must start with `use`** — React's linter and the Compiler both treat this as the hook boundary signal.

```tsx
// BAD — conditional hook; breaks call order
if (isAdmin) { const [x, setX] = useState(0) }

// GOOD — always call, branch on result
const [x, setX] = useState(0)
const visible = isAdmin ? x : null
```

### 2. Derived state in useState — the #1 React footgun
Storing **derived data** in a separate `useState` and keeping it in sync via `useEffect` is the single most common React mistake. It causes stale windows, double renders, and effect loops.

**Rule:** store the *source of truth* once; derive everything else **during render**.

```tsx
// BAD — mirror + sync (classic AI output)
const [filtered, setFiltered] = useState([])
useEffect(() => { setFiltered(items.filter(fn)) }, [items, fn])

// GOOD — derive in render; Compiler memoizes automatically
const filtered = items.filter(fn)

// GOOD — manual useMemo only when Compiler is opted out (see footgun 10)
const filtered = useMemo(() => items.filter(fn), [items, fn])
```

### 3. Effect-dependency discipline — effects are for external system sync only
`useEffect` is for synchronizing with **non-React external systems** (subscriptions, DOM mutations, third-party widgets, analytics events). It is **not** for data transformation, for triggering derived state, or for "reacting to prop changes."

- **Missing deps → stale closures.** The effect sees the values from the render it was born in.
- **Object/array literal deps → infinite loops.** `{}` and `[]` are new references every render.
- **Lint with `eslint-plugin-react-hooks` v6** (the `eslint-plugin-react-compiler` rules are merged in, under the `react-hooks/*` namespace). `rules-of-hooks` is always mandatory. `exhaustive-deps` stays mandatory when you write effects by hand; with the Compiler's linter on, its `set-state-in-effect`/`purity` rules subsume most exhaustive-deps cases, so the team allows relaxing that one rule specifically — but only with the Compiler enforcing the replacements.

```tsx
// BAD — effect for derived state; causes extra render
useEffect(() => { setTotal(cart.reduce((a, i) => a + i.price, 0)) }, [cart])

// GOOD — derive during render
const total = cart.reduce((a, i) => a + i.price, 0)

// GOOD — effect for an external subscription (cleanup required)
useEffect(() => {
  const sub = store.subscribe(handler)
  return () => sub.unsubscribe()
}, [store, handler])
```

### 4. Stale closures — the closure captures, not the live value
Event handlers, `setTimeout`/`setInterval` callbacks, and subscription listeners all **close over** the values present at the time they were created. If those values change before the callback fires, the callback sees stale data.

```tsx
// BAD — stale closure; count is always 0 inside the interval
useEffect(() => {
  const id = setInterval(() => setCount(count + 1), 1000)  // missing dep
  return () => clearInterval(id)
}, [])

// GOOD — functional update; no stale closure
useEffect(() => {
  const id = setInterval(() => setCount(c => c + 1), 1000)
  return () => clearInterval(id)
}, [])

// GOOD — latest-value ref for long-lived listeners
const handlerRef = useRef(handler)
useLayoutEffect(() => { handlerRef.current = handler })
```

### 5. List keys — array index is wrong for dynamic lists
React uses `key` to match VDOM nodes across renders. An index key means **row N always gets the state that was on row N before** — when items are reordered, filtered, or prepended, state (focus, input values, animations) silently attaches to the wrong row.

```tsx
// BAD — index key; re-ordering corrupts input state
{items.map((item, i) => <Row key={i} item={item} />)}

// GOOD — stable, unique identity from the data
{items.map(item => <Row key={item.id} item={item} />)}
```

Index keys are acceptable only for **static, append-only, never-reordered** lists.

### 6. Controlled vs uncontrolled — pick one, never switch
A controlled input owns its value via React state (`value` + `onChange`). An uncontrolled input owns its value in the DOM (`defaultValue` + optional `ref`). Switching between them at runtime (passing `undefined` then a string, or the reverse) triggers a React warning and can corrupt the field's display state.

- Large forms: prefer uncontrolled (React Hook Form / TanStack Form with refs) — avoids per-keystroke re-renders.
- Live validation / format-on-input: controlled.
- Never initialize a controlled input with `undefined` — use `""` as the default.

```tsx
// BAD — undefined → string triggers "uncontrolled to controlled" warning
<input value={user?.name} onChange={...} />

// GOOD — always a defined string
<input value={user?.name ?? ""} onChange={...} />
```

### 7. Suspense + Error Boundary placement — granular, not one top-level
One `<Suspense>` at the root hides the **entire page** behind a spinner while any subtree loads. One `<ErrorBoundary>` at the root catches errors but gives users no recovery path in the affected region.

- Wrap each data-owning subtree in its own `<Suspense fallback={<Skeleton />}>` — reserve layout space to prevent CLS.
- Pair each Suspense with a co-located `<ErrorBoundary>` with a scoped reset action.
- `useSuspenseQuery` without server prefetch → server-then-client double-fetch waterfall. Prefetch/dehydrate wiring lives in the framework module.

```tsx
// BAD — one root Suspense; any suspension hides Header too
<Suspense fallback={<Spinner />}>
  <Header />
  <UserProfile />
  <Feed />
</Suspense>

// GOOD — granular boundaries; Header stays mounted
<Header />
<Suspense fallback={<ProfileSkeleton />}>
  <ErrorBoundary fallback={<ProfileError onReset={reset} />}>
    <UserProfile />
  </ErrorBoundary>
</Suspense>
<Suspense fallback={<FeedSkeleton />}><Feed /></Suspense>
```

### 8. Concurrent rendering / StrictMode — render must be pure
React 19's concurrent renderer may **invoke render functions more than once** for the same committed output (scheduling, prerendering, offscreen work). StrictMode double-invokes render and effects in development specifically to surface impurity.

- **Never mutate external state or produce side effects during render.** Reading is fine; writing (incrementing a counter, pushing to an array, scheduling a timeout) is not.
- **StrictMode double-invoke is a detector, not a bug.** The correct fix is to make the component pure, not to remove `<StrictMode>`.

```tsx
// BAD — mutation during render; double-invoke corrupts the counter
let renderCount = 0
function MyComponent() { renderCount++ }  // side effect in render

// GOOD — use a ref; mutations outside render are fine
const renderCount = useRef(0)
useEffect(() => { renderCount.current++ })
```

### 9. useTransition / useDeferredValue — protect INP on heavy updates
INP target is ≤ 200 ms. A heavy re-render triggered on every keystroke blocks the main thread. Mark non-urgent updates so React keeps the input responsive.

```tsx
// BAD — every keystroke blocks on full list re-render
const results = expensiveFilter(items, query)  // blocks on each key

// GOOD — input stays responsive; heavy compute is deferred
const deferredQuery = useDeferredValue(query)
const results = useMemo(() => expensiveFilter(items, deferredQuery), [items, deferredQuery])

// GOOD — explicit user actions (tab switch, page nav) use useTransition
const [isPending, startTransition] = useTransition()
startTransition(() => setActiveTab(next))
```

`useDeferredValue`: expensive read from existing state. `useTransition`: explicit state-changing user action.

### 10. React Compiler 1.0 interop — blind memoization is now a liability
React Compiler 1.0 (GA Oct 2025) auto-memoizes pure components and derivations. Hand-written `useMemo`/`useCallback` on code the Compiler already handles locks in a shape it cannot rewrite, produces cache-key mismatches, and defeats its optimizations.

- Write idiomatic, pure components. Do not pre-emptively wrap with memo hooks.
- Lint with `eslint-plugin-react-hooks` v6 — `eslint-plugin-react-compiler` is merged in as of v6; it reports Compiler-incompatible patterns.
- Opt out with `"use no memo"` only for known-incompatible code (intentionally mutable objects, referential-instability dependencies, legacy integrations). Treat it as a **temporary, tracked escape hatch** — add it with a TODO, file the incompatibility, and remove it once fixed. It is *not* a permanent boundary like `"use client"`; long-lived `"use no memo"` is debt, not a design.

```tsx
// BAD under Compiler — redundant; Compiler already memoizes this
const value = useMemo(() => items.map(transform), [items])

// GOOD — idiomatic; Compiler handles it
const value = items.map(transform)

// GOOD — explicit opt-out for code that cannot be compiled
function LegacyWidget({ data }) {
  "use no memo"
  // ... intentionally mutable logic the Compiler cannot analyse
}
```

### 11. Server state vs client state — TanStack Query, not useState mirrors
Server data has its own lifecycle: caching, background revalidation, deduplication, mutation invalidation. Mirroring it into `useState`/Context creates a second source of truth that goes stale, duplicates fetches, and makes invalidation manual.

```tsx
// BAD — server state mirrored into useState
const [user, setUser] = useState(null)
useEffect(() => { fetch("/api/user").then(r => r.json()).then(setUser) }, [])

// GOOD — server state owned by TanStack Query
const { data: user } = useQuery({ queryKey: ["user"], queryFn: fetchUser })
```

**Decision rule:**
- Server data (anything fetched from an API, shared across components, needs caching/invalidation) → **TanStack Query** (`useQuery` / `useSuspenseQuery` / `useMutation`).
- Client-only UI state (open/closed, selected tab, form draft, undo stack) → **`useState`**, Zustand, or Jotai.

SSR `queryClient`-per-request, `dehydrate`/`hydrate` wiring, and RSC-as-cacheable-data belong in the framework module — point there.

### 12. Context performance — unstable provider value re-renders all consumers
`React.createContext` uses referential equality. A provider passing `{...}` or `[...]` as `value` creates a new reference on every render, re-rendering every consumer regardless of whether the data changed.

```tsx
// BAD — new object on every render; all consumers re-render always
<ThemeContext.Provider value={{ theme, setTheme }}>

// GOOD — memoize the value
const ctxValue = useMemo(() => ({ theme, setTheme }), [theme, setTheme])
<ThemeContext.Provider value={ctxValue}>

// GOOD — split fast-changing state into a separate context
<ThemeStateContext.Provider value={theme}>
<ThemeDispatchContext.Provider value={setTheme}>
```

Frequently changing state (scroll offset, real-time data) must not live in a wide context. Use Zustand/Jotai subscriptions or `useSyncExternalStore` instead.

### 13. Refs vs state — non-render data does not belong in state
`useState` re-renders on update. `useRef` persists across renders without triggering a re-render. Wrong tool in either direction: state for timer IDs wastes render cycles; ref for render-affecting data means the UI never updates.

```tsx
// BAD — timer ID in state causes unnecessary re-render on each setInterval
const [timerId, setTimerId] = useState(null)

// GOOD — timer ID in a ref; no re-render
const timerRef = useRef(null)

// BAD — ref for render-affecting data; UI never updates
const countRef = useRef(0)
countRef.current++          // no re-render → stale UI

// GOOD — counter that affects output goes in state
const [count, setCount] = useState(0)
```

Refs: DOM handles, interval/timeout IDs, the latest-value pattern (callback ref for long-lived listeners), previous-render values.

---

## How this slots into the pipeline

- **Gate 3 (UI state machine):** model the interaction in React terms — `useReducer` or XState for complex flows (IDLE / LOADING / SUCCESS / EMPTY / ERROR / STALE / RETRYING / OPTIMISTIC / ROLLING_BACK). Each footgun above maps to a state-machine edge that AI tends to miss (e.g. no STALE state → derived-state-in-useState; no ROLLING_BACK edge → optimistic update without rollback).
- **Gate 5 (state ownership):** declare which state is server-owned (TanStack Query) vs client-owned (useState / Zustand / Jotai) and which components own rendering. Footguns 11, 12, and 13 are Gate-5 design decisions.
- **Gate 6 (implementation):** code against footguns 1–13 above and the code-level companion `references/react-gotchas.md`. The Gate-6 codegen checklist in `mir-frontend` references this tier.
- **Gate 7 (review):** the reliability-reviewer checks async correctness (footguns 3, 4, 7, 11); the frontend-perf-reviewer checks footguns 9, 10; both check footgun 2. Items 1, 5, 6, 8, 12, and 13 are correctness issues any reviewer should flag.

---

## References

- `references/react-gotchas.md` — code-level right-vs-wrong TSX companion to the footguns above, plus the React 19 primitives AI most often misuses: `use()` (stable promises), `useActionState`/`useFormStatus` placement, `useOptimistic` auto-rollback, `ref`-as-prop + ref cleanup (no `forwardRef`), `AbortController` cleanup in fetch effects, `useId` for hydration-stable ids, the SSR browser-API mismatch, and key-based remount-to-reset.

---

## Edit boundary (what belongs here vs. above/below)

**The 3-question placement test:**

1. True for Vue, Angular, and Svelte too (any reactive UI library)? → **up** to `mir-frontend`.
2. True for every React meta-framework (Next.js, React Router 7, TanStack Start, Vite SPA) because they all run React's reactivity model? → **here**.
3. Specific to one meta-framework's mechanics (RSC boundary, Server Actions, file-based routing, framework caching, hydration wiring, `generateMetadata`)? → **down** to `mir-frontend-react-<framework>`.
4. Different reactivity library (Vue, Angular, Svelte)? → its own `mir-frontend-<lib>` tier. Never widen this one.

Cross-ref: full edit map is in `mir-frontend/SKILL.md` → "Where these instructions live."
