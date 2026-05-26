# Failure-Mode Catalog — Frontend

The recurring ways AI-generated frontend code is *locally correct but broken in production*. Each entry: the trap, why AI defaults to it, and the defuse. Used in Gate 3 (UI state machine + failure modes) and Gate 4 (risk register).

The meta-pattern: **AI models the happy visual path; production UIs are state machines under async, failure, and assistive-tech.** Most of these are async-timeline bugs, missing states, or category errors about where trust lives.

---

## 1. Derived state stored in useState

Computed values (filtered lists, totals, formatted strings, boolean flags derived from props) stored in a separate `useState` and synced via `useEffect`. **Why AI defaults here:** it cargo-cults the "state = data" mental model and writes an explicit sync because "that's how you keep things up to date." **Defuse:** if a value can be computed purely from existing state or props during render, compute it inline — no `useState`, no `useEffect`. With React Compiler 1.0 GA the compiler handles memoization; only reach for `useMemo` if profiling shows a genuine perf cost and Compiler interop is confirmed off for that component. The Effect is always the wrong tool for derived values.

## 2. Async race / stale response

A search or fetch fires on each keystroke or dep change; the last fired request wins but may return first if a slower earlier request resolves late — silently displaying stale data. No cleanup on unmount or dep change. **Why AI defaults here:** it writes `fetch(…).then(setState)` in a single shot and never models overlapping calls. **Defuse:** cancel in-flight requests via `AbortController` in the effect cleanup; alternatively (and preferably for most data-fetching) use TanStack Query, which handles request deduplication, cancellation, and stale-response guarding by default. Never trust that the last-initiated request finishes last.

## 3. useEffect overuse, effect-driven derived state, and infinite loops

Effects used to compute derived values, to synchronize two pieces of state, or written with unstable object/array/function literals in the dependency array — producing infinite render loops. **Why AI defaults here:** `useEffect` looks like "do something when X changes," which matches every conditional-update intuition. **Defuse:** apply the single rule — *if a value is derivable in render, there is no effect.* Effects are for synchronizing with external systems (timers, subscriptions, DOM APIs, analytics) only. Unstable deps: stable identity via `useCallback`/`useMemo` (or Compiler) before putting a function or object in a dep array; primitives derived in render are stable by construction.

## 4. Stale closures in callbacks, timers, and subscriptions

An event handler, `setTimeout`, or subscription callback captures state or props at creation time. By the time it fires, the values are stale — the closure never sees updates. **Why AI defaults here:** JavaScript closures are transparent; AI writes callbacks that look correct at the call site but freeze a snapshot of the world. **Defuse:** use the `useRef`-as-latest-value pattern for values that must be read inside long-lived callbacks without triggering re-subscription; or restructure so the callback is re-created when deps change (with proper cleanup). In React 19 the `use` hook and Actions model reduces the surface area for these patterns in async mutations.

## 5. Hydration mismatch

SSR-rendered HTML diverges from the client's first render because the component reads `Date.now()`, `Math.random()`, `window`, `navigator`, locale, or any value that differs between server and client environments. **Why AI defaults here:** it writes a component that "works in the browser" without modeling that the same function runs twice — first on the server, then re-executed on the client. **Defuse:** keep render functions pure and environment-agnostic; gate browser-only reads behind `useEffect` (which runs client-side only) or the `use client` boundary. `suppressHydrationWarning` is a narrow escape hatch for genuinely unavoidable differences (e.g. timestamp formatted for the user's locale) — it is not a general silence switch.

## 6. Cross-user data bleed via singleton queryClient on SSR

A `QueryClient` instance created at module scope (outside the component tree) is shared across all concurrent SSR requests. One user's cached data leaks into another user's response. **Why AI defaults here:** the TanStack Query docs show a `new QueryClient()` at the top of a file; AI copies the pattern without seeing the server-rendering warning. **Defuse:** create a `new QueryClient()` per request inside the server render path (Next App Router: in the layout/page Server Component; TanStack Start: in the loader). Never share a `queryClient` across requests on the server.

## 7. Unsafe SSR dehydration (XSS via inline script)

Query/state snapshots serialized into HTML via `JSON.stringify` and injected into a `<script>` tag or a `data-` attribute — without escaping `</script>` sequences or HTML-encoding the content. A user-controlled string in the cache can escape the script block. **Why AI defaults here:** `JSON.stringify` "produces valid JSON" and AI treats validity as safety. It is not. **Defuse:** use a safe serializer that escapes `<`, `>`, and `&` in JSON strings before injecting into HTML (e.g. `devalue`, or the built-in sanitization in TanStack Query's `dehydrate`/`<HydrationBoundary>` pipeline). Never hand-roll `<script>window.__STATE__ = ${JSON.stringify(data)}</script>`.

## 8. INP-blocking interactions

Heavy synchronous work — large list renders, complex calculations, synchronous API calls, unthrottled scroll handlers — runs directly in an event handler or synchronous state update, blocking the main thread and pushing Interaction to Next Paint (INP) past the 200 ms budget. **Why AI defaults here:** it solves for functional correctness; visual latency is invisible in a test. **Defuse:** defer non-urgent rendering with `startTransition` (marks the update as interruptible) or `useDeferredValue` (defers a value until the urgent render finishes); for truly CPU-heavy work, move it off the main thread via a Web Worker or `scheduler.yield()`. INP ≤ 200 ms is a Core Web Vital since FID was retired (Sep 2024); it is only measurable in RUM, not in CI.

## 9. UX state explosion — missing states

Only three states modeled: loading / success / error. Production surfaces require: **empty** (no results yet, vs "search returned zero"), **stale** (showing cached data while refreshing), **retrying** (bounded retry in progress), **offline** (network gone), **optimistic** (mutation submitted, not confirmed), **rolling back** (optimistic update rejected), **permission-denied** (403 differs from generic error), **expired-auth** (redirect, not generic error). **Why AI defaults here:** loading/success/error is the minimal demo; the rest emerge only from real usage. **Defuse:** Gate 3 requires an explicit UI state machine with every reachable state named. Each state must have a defined UI: skeleton, empty-state illustration, inline error with retry, banner, disabled form, or redirect. States not modeled will be silently wrong.

## 10. ErrorBoundary and Suspense placement errors

A single `<ErrorBoundary>` at the root catches all errors but shows a full-page crash for a sidebar widget's data failure. `<Suspense>` wrapping with `useSuspenseQuery` without a prefetched query causes a server-then-client double waterfall: the server suspends, the client suspends again. **Why AI defaults here:** one boundary works; the ergonomics of granular placement require deliberate architecture. **Defuse:** place `<ErrorBoundary>` + `<Suspense>` around each independent data subtree so a failure in one region does not blank the whole page. Prefetch queries in Server Components or route loaders so `useSuspenseQuery` on the client finds data already in the cache and does not re-suspend.

## 11. Fetch waterfalls

Child component fetches only after parent renders; sequential `await fetch()` chains inside a single loader; data loaded inside components that render inside other data-loading components. **Why AI defaults here:** component-local fetching is the obvious, self-contained pattern — it composes cleanly in isolation. **Defuse:** hoist fetches to the route loader or Server Component so sibling fetches fire in parallel (`Promise.all`). TanStack Query's `useSuspenseQuery` in parallel (not nested) or prefetch-then-read avoids the sequential trap. The rule: data dependencies should be declared at the route/page level, not discovered lazily as the tree renders.

## 12. XSS via the raw-HTML prop, unsafe markdown, and client-side secret/auth leakage

Setting the raw-HTML prop on a DOM element with unescaped user content injects arbitrary scripts. Markdown renderers that output raw HTML without a sanitizer are the same risk. Separately: environment variables prefixed `NEXT_PUBLIC_` (or their equivalent) are embedded in the client bundle — any secret placed there is public. Client-side authorization checks (hide a button, skip a route) are a UX hint, not a security gate; the server must enforce the same rule. **Why AI defaults here:** the raw-HTML prop and markdown rendering "just work" for trusted content; prefix conventions for public vs. private env vars are easy to confuse; and visible UI suppression feels like access control. **Defuse:** sanitize any user-controlled HTML with DOMPurify (or a safe markdown renderer with sandboxed HTML) before using the raw-HTML prop; audit every `NEXT_PUBLIC_` variable for secrets; replicate every authorization decision server-side.

## 12b. Flash of wrong theme (FOWT) on SSR dark mode

Dark-mode preference read from `localStorage` after hydration causes the page to flash light → dark on load. **Why AI defaults here:** `localStorage` is browser-only, so it is naturally deferred to `useEffect` — which fires after paint. **Defuse:** inject a blocking inline `<script>` in `<head>` (before the CSS) that reads `localStorage` and applies the theme class synchronously, before the browser paints. Alternatively use a CSS media query (`prefers-color-scheme`) as the default so no JS is needed for the initial paint; layer the user-preference override on top. Never read theme from `localStorage` in a `useEffect` if avoiding flash matters.

## 13. Hallucinated UX

Pagination, autosave, debounced search, sortable columns, undo/redo, optimistic updates, and retry buttons appear in the implementation without ever being specified. AI invents plausible, production-looking defaults. **Why AI defaults here:** these patterns are common enough to feel like "obviously needed" — and they complete the feature ergonomically. **Defuse:** Gate 1 asks explicitly: "Does this list paginate? If so, cursor or offset? Debounce on search? Client-side or server-side sort? Autosave or explicit submit? Optimistic or pessimistic mutation?" Every answer not given by the user must be surfaced as an Assumption Ledger entry with a recommended default — never silently implemented.

## 14. React Compiler era: blind useMemo/useCallback as a liability

Manual `useMemo` and `useCallback` wrapped around every value and callback "for performance" — now that React Compiler 1.0 (GA Oct 2025) handles memoization automatically, these are redundant noise at best and a source of incorrect dependency arrays at worst. `eslint-plugin-react-hooks` v6 flags unnecessary manual memos in Compiler-enabled projects. **Why AI defaults here:** training data through 2024 consistently rewarded manual memoization; the Compiler's GA status post-dates most patterns the model learned. **Defuse:** confirm whether the project has opted into the Compiler (`babel-plugin-react-compiler` / Next 16 built-in). If yes: remove unnecessary `useMemo`/`useCallback`; use `"use no memo"` directive only for components the Compiler cannot safely transform (documented in the Compiler output). If Compiler is off: use memos judiciously with profiling evidence, not preemptively.

## 15. Supply-chain and dependency hygiene

AI generates `import` statements for npm packages that do not exist (hallucinated), pins nothing (unpinned `^` ranges drift into breaking or compromised versions), and skips `npm audit`. Bundle size receives no budget. **Why AI defaults here:** package names are plausible, semver ranges look conventional, and bundle size is invisible in a text diff. The TanStack npm compromise (May 2026, CVE-2026-45321) demonstrated that a widely-trusted package ecosystem is a live attack surface, not a safe assumption. **Defuse:** verify every package name against the actual npm registry before committing; pin exact versions or use a lock file with integrity checks; run `npm audit --audit-level=high` in CI; set a bundle-size budget (first-load JS ≤ ~100 KB compressed as a starting point) and enforce it with a tool like `bundlewatch` or Next's built-in size tracking. Provenance attestations reduce risk but do not eliminate it — pinning + audit remains mandatory.
