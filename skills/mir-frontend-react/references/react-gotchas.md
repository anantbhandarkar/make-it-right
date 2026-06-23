# React 19 reactivity gotchas — right vs wrong

Code-level companion to SKILL.md. Stack: **React 19.2 · React Compiler 1.0 · eslint-plugin-react-hooks v6 · TanStack Query**. Strictly reactivity-level — RSC, Server Actions, file routing, and framework caching live in the meta-framework module (`mir-frontend-react-<framework>`). Each section is the executable form of a SKILL.md footgun.

---

## 1. Derive in render — never mirror into state

```tsx
// WRONG — mirror + sync; stale window, double render, effect loop
const [filtered, setFiltered] = useState<Item[]>([])
useEffect(() => { setFiltered(items.filter(fn)) }, [items, fn])

// RIGHT — derive during render; the Compiler memoizes it automatically
const filtered = items.filter(fn)

// RIGHT — manual useMemo ONLY when the Compiler is opted out (see §13)
const filtered = useMemo(() => items.filter(fn), [items, fn])
```

Store the source of truth once; compute everything else in render. Under the Compiler the bare `items.filter(fn)` is already memoized — a hand-written `useMemo` there is the redundant memoization §13 warns about.

## 2. Effects sync external systems — and always clean up

```tsx
// WRONG — fetch in an effect with no cleanup: out-of-order responses + setState-after-unmount
useEffect(() => { fetch(`/api/u/${id}`).then(r => r.json()).then(setUser) }, [id])

// RIGHT — abort on cleanup; ignore the AbortError; (better: use TanStack Query instead)
useEffect(() => {
  const ac = new AbortController()
  fetch(`/api/u/${id}`, { signal: ac.signal })
    .then(r => r.json()).then(setUser)
    .catch(e => { if (e.name !== 'AbortError') throw e })
  return () => ac.abort()
}, [id])

// RIGHT — external subscription, cleanup required
useEffect(() => {
  const sub = store.subscribe(handler)
  return () => sub.unsubscribe()
}, [store, handler])
```

Effects are for syncing with **non-React systems** (subscriptions, DOM, widgets, analytics) — not data transformation. The unaborted fetch is the canonical race: a slow earlier response overwrites a fast later one. Object/array literal deps (`{}`, `[]`) are new references every render → infinite loop.

## 3. Stale closures — functional updates; setState is stable

```tsx
// WRONG — closes over count===0 forever; missing dep
useEffect(() => {
  const t = setInterval(() => setCount(count + 1), 1000)
  return () => clearInterval(t)
}, [])

// RIGHT — functional update reads the live value
useEffect(() => {
  const t = setInterval(() => setCount(c => c + 1), 1000)
  return () => clearInterval(t)
}, [])
```

`setCount`/`dispatch` from `useState`/`useReducer` are **guaranteed stable** — never list them as effect deps and never wrap them in `useCallback`. For long-lived listeners that need the latest prop, keep it in a ref updated in a `useLayoutEffect`.

## 4. List keys = data identity; key-change to reset state

```tsx
// WRONG — index key: reordering/filtering attaches row state to the wrong row
{items.map((item, i) => <Row key={i} item={item} />)}

// RIGHT — stable identity from the data
{items.map(item => <Row key={item.id} item={item} />)}

// WRONG — resetting child state on prop change via an effect (derived-state anti-pattern)
useEffect(() => setDraft(initial), [userId])

// RIGHT — change the key to remount and reset declaratively
<EditForm key={userId} initial={initial} />
```

Index keys are fine only for static, append-only, never-reordered lists. To fully reset a subtree's internal state when an identity prop changes, change its `key` — don't sync state in an effect.

## 5. Controlled vs uncontrolled — never switch; default to `""`

```tsx
// WRONG — undefined → string flips uncontrolled→controlled, triggers a warning + display corruption
<input value={user?.name} onChange={onChange} />

// RIGHT — always a defined string
<input value={user?.name ?? ''} onChange={onChange} />
```

A controlled input owns its value in React state (`value` + `onChange`); uncontrolled owns it in the DOM (`defaultValue` + `ref`). Pick one for the field's lifetime. Never initialize a controlled input with `undefined`.

## 6. `useId` for SSR-stable IDs

```tsx
// WRONG — server and client generate different ids → hydration mismatch
const id = `field-${Math.random()}`        // or a module counter, or crypto.randomUUID() in render

// RIGHT — stable across server render and client hydration
const id = useId()
return <><label htmlFor={id}>Email</label><input id={id} /></>
```

Any id used for `htmlFor` / `aria-describedby` / `aria-labelledby` must match across SSR and CSR. `useId` guarantees that; random or counter-based ids in render do not.

## 7. Browser APIs & effects under SSR

```tsx
// WRONG — window/localStorage read in render: undefined on the server → hydration mismatch / crash
const [theme] = useState(localStorage.getItem('theme') ?? 'light')

// RIGHT — render a server-safe default; read the browser-only value in an effect
const [theme, setTheme] = useState('light')          // matches the server
useEffect(() => { setTheme(localStorage.getItem('theme') ?? 'light') }, [])

// BEST for external stores — useSyncExternalStore with a server snapshot
const width = useSyncExternalStore(subscribe, () => window.innerWidth, () => 0 /* server snapshot */)
```

Effects do **not** run during server rendering. Reading `window`/`localStorage`/`matchMedia` directly in render produces a server value (undefined) that differs from the client → hydration mismatch. This is true for every SSR React framework, so it lives here.

## 8. `ref` as a prop + ref cleanup — no `forwardRef`

```tsx
// WRONG (React 19) — forwardRef is no longer needed; AI emits it reflexively from old training data
const Input = forwardRef<HTMLInputElement, Props>((props, ref) => <input ref={ref} {...props} />)

// RIGHT — ref is a normal prop you can destructure
function Input({ ref, ...props }: Props & { ref?: Ref<HTMLInputElement> }) {
  return <input ref={ref} {...props} />
}

// WRONG — implicit-arrow-return ref callback: the return is now read as a cleanup fn (TS error)
<div ref={n => (instance = n)} />

// RIGHT — block body; ref callbacks MAY return a cleanup function (called on unmount)
<div ref={n => { instance = n; return () => { instance = null } }} />
```

In React 19 `ref` is an ordinary prop — drop `forwardRef`. Ref callbacks may return a cleanup function (run on unmount instead of being called with `null`), so an implicit arrow return is now interpreted as that cleanup — always use a block body.

## 8b. `use()` — stable promises, conditional reads

```tsx
// WRONG — a new promise every render → Suspense never resolves (infinite loop)
function Profile({ id }: { id: string }) {
  const user = use(fetch(`/api/u/${id}`).then(r => r.json()))   // fresh promise each render
  return <h1>{user.name}</h1>
}

// RIGHT — a STABLE promise from a cache/loader, read under a Suspense boundary above
const user = use(userCache.get(id))        // same promise identity across renders

// RIGHT — use() may be called conditionally (unlike other hooks) for context
const theme = condition ? use(ThemeContext) : defaultTheme
```

`use()` relaxes the call-order rule (it may run in branches/loops), but the promise must be stable — created by a framework loader or a cache, never inline in render. The promise form needs a `<Suspense>` boundary above it.

## 9. Form actions — `useActionState` + `useFormStatus` placement

```tsx
// WRONG — manual loading flag reintroduces the derived-state footgun
const [pending, setPending] = useState(false)
async function onSubmit(e) { setPending(true); await save(data); setPending(false) }

// RIGHT — useActionState owns pending/result/error for the action
function ContactForm() {
  const [state, formAction, isPending] = useActionState(submit, { error: null })
  return (
    <form action={formAction}>
      <input name="email" />
      <SubmitButton />                     {/* useFormStatus must live INSIDE the form subtree */}
      {state.error && <p role="alert">{state.error}</p>}
    </form>
  )
}
function SubmitButton() {
  const { pending } = useFormStatus()      // reads the PARENT <form> — only works rendered inside it
  return <button disabled={pending}>Save</button>
}
```

These are client-side reactivity primitives (not Server Actions — those are framework tier). The classic miss: calling `useFormStatus()` in the component that *renders* the form instead of a child *inside* the form — it reads the nearest ancestor `<form>` and returns `pending: false` from the wrong place.

## 10. `useOptimistic` — it auto-rolls back

```tsx
// WRONG — manually reverting double-handles what the hook already does
const [list, setList] = useState(items)
async function add(item) {
  setList(l => [...l, item])
  try { await save(item) } catch { setList(l => l.filter(x => x !== item)) }  // manual rollback
}

// RIGHT — addOptimistic MUST run inside a transition/Action, or React warns and reverts instantly
const [optimistic, addOptimistic] = useOptimistic(items, (state, item: Item) => [...state, item])
const [, startTransition] = useTransition()
function add(item: Item) {
  startTransition(async () => {
    addOptimistic(item)      // persists for the duration of the transition
    await save(item)         // on settle/throw, React drops the optimistic layer automatically
  })
}
// (a <form action={...}> or useActionState wraps an Action for you — same effect, no manual transition)
```

`useOptimistic` derives a temporary view layered over the real state **for the duration of an Action**. The setter must be called inside a transition or a form action — call it in a bare async function and React warns ("optimistic state update outside a transition or action") and reverts before your `await` resolves. You do not store the value and you do not roll it back by hand — it disappears when the Action settles, revealing whatever the real state became.

## 11. INP — defer heavy work; don't reach for `flushSync`

```tsx
// WRONG — every keystroke blocks on the full list re-render
const results = expensiveFilter(items, query)

// RIGHT — keep the input responsive; deferred read (Compiler memoizes the derivation)
const deferredQuery = useDeferredValue(query)
const results = expensiveFilter(items, deferredQuery)

// RIGHT — explicit non-urgent user action (tab switch, navigation)
const [isPending, startTransition] = useTransition()
startTransition(() => setActiveTab(next))

// WRONG — flushSync to "make state update now"; kills batching, hurts INP
flushSync(() => setOpen(true)); measure()

// RIGHT — flushSync ONLY when you must read the DOM before paint in the same tick
flushSync(() => setExpanded(true))
rowRef.current!.scrollIntoView()
```

`useDeferredValue` = expensive read from existing state. `useTransition` = explicit state-changing action. `flushSync` forces a synchronous render+commit — use it only for a DOM measurement that must happen before paint, never as a general "update immediately" tool.

## 12. Context — stable value, split by change rate

```tsx
// WRONG — new object every render: all consumers re-render regardless of what changed
<ThemeContext.Provider value={{ theme, setTheme }}>

// RIGHT — memoize (still needed when the Compiler can't prove stability across the provider boundary)
const value = useMemo(() => ({ theme, setTheme }), [theme, setTheme])
<ThemeContext.Provider value={value}>

// RIGHT — split fast-changing from stable so a value change doesn't re-render dispatch consumers
<ThemeStateContext.Provider value={theme}>
  <ThemeDispatchContext.Provider value={setTheme}>
```

`createContext` uses referential equality. Frequently changing values (scroll offset, real-time data) must not ride a wide context — use Zustand/Jotai subscriptions or `useSyncExternalStore`.

## 13. Compiler interop — stop hand-memoizing; `"use no memo"` is temporary

```tsx
// WRONG under the Compiler — redundant; it already memoizes this and your useMemo can defeat its rewrite
const value = useMemo(() => items.map(transform), [items])
const onClick = useCallback(() => doThing(id), [id])

// RIGHT — idiomatic; the Compiler handles memoization
const value = items.map(transform)
const onClick = () => doThing(id)

// RIGHT — opt out ONLY for known-incompatible code, as tracked debt
function LegacyWidget({ data }: Props) {
  'use no memo'   // TODO(#1234): remove once the mutable-data integration is fixed
  // ...intentionally mutable logic the Compiler can't analyze
}
```

React Compiler 1.0 (GA Oct 2025) auto-memoizes pure components and derivations — pre-emptive `useMemo`/`useCallback` is now a liability, not an optimization. Lint with `eslint-plugin-react-hooks` v6 (compiler rules under the `react-hooks/*` namespace). `"use no memo"` is a temporary, tracked escape hatch — not a permanent boundary like `"use client"`.

---

### Refs vs state (quick rule)

`useState` re-renders on change; `useRef` persists without re-rendering. Timer/interval IDs, DOM handles, previous-render values, and the latest-value pattern → **ref**. Anything that affects what the UI renders → **state**. State for a timer ID wastes renders; a ref for render-affecting data means the UI never updates.
