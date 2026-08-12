# Checklists — Gate 6 (codegen) & Gate 7 (production-readiness)

These are **strict, checked-against** lists. For each item: either it is satisfied (point to the code), it is consciously N/A (say why), or it is a finding. "Looks fine" is not an allowed state.

Written library-neutrally, with React named where a concrete example helps. The tier skill has the exact API for your stack: `mir-frontend-react`, `mir-frontend-vue`, `mir-frontend-vanilla`. Versions here were checked against the npm registry on 13 Aug 2026.

---

## Gate 6 — Codegen checklist (while writing)

### State & Data

- [ ] No derived value stored in its own state variable — computed values are derived during render (or in a `computed`); no effect or watcher syncs a copy back
- [ ] All server state managed by a data-fetching layer (`@tanstack/react-query` 5.101.4, SWR, or the framework's route loader / `useAsyncData`); no hand-rolled fetch-in-effect for remote data
- [ ] On SSR paths, every cache, store, and API client is created **per request** — never at module scope, never shared across users. This is the cross-user data leak: a `QueryClient`, a `createPinia()` instance, a hand-rolled reactive singleton, a `Map` cache, or a request-token-configured SDK client at module scope is one process-wide object serving every visitor. A module-scope `defineStore()` *declaration* used through the Nuxt Pinia module is fine — that creates one store instance per Nuxt app, i.e. per request
- [ ] Client state is minimal: only values that have no server representation and must survive re-renders (UI toggles, form draft, selection)
- [ ] Every mutation is followed by explicit cache invalidation or optimistic update + rollback — no "fire and forget"
- [ ] Cache keys include all dimensions that change the result (tenant, user, filter params) — no cross-user key collisions possible

### Async

- [ ] Every in-flight fetch can be cancelled: `AbortController` wired to the effect/watcher cleanup, or the data library's built-in cancellation
- [ ] Stale-response guard in place: the component does not apply a response that arrived after a newer request resolved
- [ ] `fetch` result checked with `res.ok` — `fetch` rejects only on network failure, so a 500 with an HTML error page resolves and renders as data
- [ ] Optimistic update defined: what the UI shows immediately, and what rollback looks like on failure
- [ ] Retries are bounded (TanStack Query default: 3; override for non-idempotent mutations to `retry: 0`)
- [ ] Every remote request has a real deadline — `AbortSignal.timeout(ms)`, an `AbortController`, or the client's own timeout option. `staleTime` and `gcTime` are cache-lifetime settings and cancel nothing; a request with `staleTime` set can still hang forever
- [ ] Every listener, observer, timer, and subscription created by this code has a matching teardown, written in the same edit — the library's unmount hook, or one `AbortController` per instance with no framework (failure-mode #16)

### Rendering

- [ ] Loading and error boundaries placed **per data subtree**, not only at the root (React `<Suspense>` + `<ErrorBoundary>`; Vue `<Suspense>` is still experimental — check `mir-frontend-vue` before depending on it; plain DOM renders its own states)
- [ ] Suspense-reading queries are prefetched on the server or in the route loader — no client-side suspense waterfall on first load
- [ ] No hydration-unsafe values in render (`Date.now()`, `Math.random()`, `crypto.randomUUID()`, `window`, `navigator`, locale, timezone) — computed once on the server and carried in the payload, or read after mount
- [ ] Heavy or non-urgent state updates are deferred off the interaction (React `startTransition` / `useDeferredValue`)
- [ ] List keys are stable, unique identifiers from the data — never an array index on a list that reorders, filters, or prepends
- [ ] No effect or watcher used to derive or sync state that can be computed in render (failure-mode #3)

### Accessibility

- [ ] Semantic HTML used throughout: `<button>` for actions, `<a>` for navigation, `<nav>`, `<main>`, `<header>`, `<section>`, heading hierarchy correct
- [ ] Every interactive element is keyboard-reachable and operable with Enter/Space as appropriate
- [ ] Focus is managed explicitly on route change, dialog open/close, and dynamic content insertion (no focus traps outside of modals; modals do trap)
- [ ] Visible focus indicator meets WCAG 2.2 Focus Appearance: ≥ 3:1 contrast ratio against adjacent colors
- [ ] Interactive target size ≥ 24 × 24 CSS px (WCAG 2.2 AA; ≥ 44 × 44 for touch preferred)
- [ ] `prefers-reduced-motion` respected: CSS `@media (prefers-reduced-motion: reduce)` disables or slows animations
- [ ] All images have meaningful `alt` text; decorative images use `alt=""`
- [ ] Form inputs have associated `<label>` or `aria-label`; error messages linked via `aria-describedby`
- [ ] Dynamic content updates announced via `aria-live` regions where appropriate (toasts, status, validation)

### Performance

- [ ] INP ≤ 200 ms: heavy work deferred (React `startTransition`, a Web Worker, or `scheduler.yield()` where supported — it is not Baseline everywhere, so feature-detect); event handlers do not block the main thread
- [ ] LCP ≤ 2.5 s: primary above-fold image uses `fetchpriority="high"` (or the framework's `priority` flag) and nothing else does; no render-blocking resources
- [ ] CLS ≤ 0.1: image, video, and ad slots have explicit width/height or `aspect-ratio` reserved; no late-injected content above the fold
- [ ] Geometry reads and style writes are not interleaved in a loop — batch all reads, then all writes (failure-mode #8)
- [ ] Manual memoization is deliberate, not reflexive: React Compiler 1.0.0 **preserves** whatever `useMemo`/`useCallback` you wrote, and an incomplete dependency array then stops it optimizing that code at all (failure-mode #14)
- [ ] Images served in modern format (WebP/AVIF), sized for the display context, lazy-loaded below the fold
- [ ] Fonts use `font-display: swap` or `optional`; subset where possible; preloaded if critical
- [ ] First-load JS bundle within project budget (default starting point: ≤ 100 KB compressed); code-split at route boundaries

### Security

- [ ] Any user-controlled HTML reaching a raw-HTML sink is sanitized first — DOMPurify **≥ 3.4.13**, pinned exactly, never `IN_PLACE: true`
- [ ] Markdown rendered with raw-HTML passthrough off; if it must be on, an explicit sanitization schema is configured. LLM output is treated as untrusted input on the same path
- [ ] No secrets in build-time public env vars (`NEXT_PUBLIC_*`, `VITE_*`, `PUBLIC_*`, `NUXT_PUBLIC_*`, `REACT_APP_*`) or in any module a client component imports — verified by grepping the **built output** in CI, not the source
- [ ] Client-side authorization checks (hidden routes, disabled buttons, lazy admin chunks) are UX hints only; the same rule is enforced server-side, per object
- [ ] User-supplied URLs are validated by parsed protocol against an allow-list before they reach `href`/`src`/`action` — not by string prefix match
- [ ] CSP configured with no `'unsafe-inline'` / `'unsafe-eval'`, plus `object-src 'none'`, `base-uri 'none'`, and `frame-ancestors` set for clickjacking; Trusted Types enabled where the stack supports it
- [ ] Every third-party `<script src>` is pinned to an exact version with `integrity` + `crossorigin="anonymous"`, or self-hosted; tag-manager access is named and limited
- [ ] Server Action / RPC inputs are revalidated and re-authorized on the server (Zod or equivalent) — client-supplied values and TypeScript types are not enforcement
- [ ] State-changing endpoints have a CSRF defence when auth is cookie-based: `SameSite` plus an origin check or a double-submit token
- [ ] SSR dehydration uses a safe serializer (not bare `JSON.stringify` into a `<script>` block) — failure-mode #7
- [ ] Lockfile committed; CI installs with `npm ci`; install scripts disabled; `npm audit` clean at High+; every package name confirmed to exist on the registry (failure-mode #15)

### i18n & UX completeness

- [ ] No hardcoded user-visible copy — all strings use i18n keys
- [ ] Layout uses CSS logical properties (`margin-inline-start`, `padding-inline-end`, `inset-inline`) for RTL compatibility
- [ ] Every async surface models all required states: loading, empty, error, stale, retrying, offline, optimistic, rollback, permission-denied, expired-auth (failure-mode #9)
- [ ] Dark-mode theme applied without flash of wrong theme (FOWT): blocking inline script in `<head>` or `prefers-color-scheme` CSS default (failure-mode #12b)

---

## Gate 7 — Production-readiness review (after writing)

Run by the reviewer sub-agents. Each finding: **severity (Critical / High / Med / Low) + file:line + the fix**. Reviewers do not edit code; they report. The orchestrator fixes Critical / High before declaring done.

### Reliability-reviewer focus

- Every UI state from the Gate 3 state machine is reachable and has a defined, intentional render (no blank/undefined states)
- Optimistic updates have a rollback path: what the UI shows on mutation failure and how the cache is restored
- Race conditions covered: AbortController or TanStack Query cancellation present on all searches, pagination, and dep-change fetches
- Stale closures: callbacks used in timers, subscriptions, or long-lived event listeners reference current state via ref or are re-created with cleanup
- Error boundaries are granular: a failure in a secondary data subtree does not blank the primary page content
- Suspense boundaries have prefetched data on SSR paths; no client-only waterfalls on initial load
- Teardown: every listener, observer, timer, and subscription has a removal path that actually runs — including the deactivated-not-destroyed case for cached/kept-alive components (failure-mode #16)
- Module-scope state: on any server-rendered path, no cache, store, or API client is constructed at module scope (failure-mode #6). Grep before concluding it is clean

### Security-reviewer focus (the browser side)

- Raw-HTML sinks: grep for every one by name (the tier skill lists them) and audit each instance; user-controlled content always goes through a sanitizer first, and `dompurify` is pinned at ≥ 3.4.13
- Public env audit: no API keys, signing secrets, or internal hostnames in any build-time public variable or client-imported module — checked against the built output, not the source. Source maps not served from a public origin
- Client-side auth checks identified and confirmed as hints only; the corresponding server-side, per-object enforcement located in code
- CSP present, without `unsafe-inline` / `unsafe-eval`, with `frame-ancestors` set; Trusted Types enabled or its absence justified
- Server Actions (or equivalent RPC): each one authenticates, authorizes the specific object, validates arguments, and returns filtered data — not the database row. CSRF protection via framework default or explicit token
- Third-party scripts and tag managers enumerated, pinned, `integrity`-checked; who can change them is written down
- npm supply chain: every package name cross-checked against the registry (no hallucinated or typosquatted names); lockfile present; install scripts off; `npm audit` clean at High+

### a11y-reviewer focus (WCAG 2.2 AA)

- Automated axe-core run (4.13.0, in CI or the Storybook addon) with zero Critical / Serious violations. Automation cannot see focus order, meaningful alt text, or whether a flow is completable — a clean axe run is the floor, not the review
- Keyboard-only navigation: every flow completable without a mouse; no keyboard traps outside modals; modals trap and release correctly
- Focus management: visible focus indicator present on all interactive elements; meets 3:1 Focus Appearance contrast
- Target size: all touch/click targets ≥ 24 × 24 CSS px; primary actions ≥ 44 × 44 px preferred
- `prefers-reduced-motion`: animations stop or slow when the media query fires; no vestibular-triggering motion at full speed
- Semantic structure: heading hierarchy (`h1` → `h2` → …) correct; landmarks present; lists use `<ul>`/`<ol>`
- Dynamic content: toasts, alerts, and form validation errors announced via `aria-live`; region roles correct

### Frontend-perf-reviewer focus (CWV / INP / bundle / waterfalls / Compiler)

- INP ≤ 200 ms: event handlers free of synchronous blocking work; `startTransition` present on non-urgent state updates; RUM instrumentation in place (INP is not measurable in CI — RUM is required)
- LCP ≤ 2.5 s: above-fold image is priority-loaded; no render-blocking stylesheets or synchronous scripts in `<head>`
- CLS ≤ 0.1: all media and dynamic-content containers have reserved space; no unsized images
- Fetch waterfalls: no sequential data dependencies in child components; sibling fetches are parallel; route loaders or Server Components hoist data to the top
- Bundle budget: first-load JS ≤ project-defined limit (default baseline ≤ 100 KB compressed); route splitting present; no accidental whole-library imports (e.g. `import _ from 'lodash'`)
- React Compiler interop: `eslint-plugin-react-hooks` **7.1.1** clean. Note v7 turned the compiler rules on inside `recommended` — upgrading from v6 turns CI red on code that passed before, so treat that as a scheduled task. `preserve-manual-memoization` findings are the ones that matter; `"use no memo"` only where documented
- Image/font: the framework's image and font primitives used for all content images and web fonts; explicit dimensions everywhere; a metric-matched fallback so the font swap does not shift layout

---

## Testing gate

Layered strategy — each layer catches a distinct class of AI-generated errors:

| Layer | Tool | What it catches |
|---|---|---|
| **Unit** | Vitest | Logic errors in hooks, composables, utilities, state machines, reducers; stale-closure bugs in isolated callbacks |
| **Component** | Storybook + the Vitest addon *or* Playwright Component Testing | Per-state rendering (empty, error, loading, stale); a11y via the axe addon; visual snapshot baseline |
| **E2E** | Playwright | Full interaction flows; keyboard navigation; focus management; form validation; race conditions under network throttling |
| **Visual regression** | Playwright screenshots *or* Chromatic | FOWT; layout shift; theme switching; responsive breakpoints |
| **A11y automation** | axe-core 4.13.0 in CI (via `@axe-core/playwright` or the Storybook addon) | WCAG 2.2 AA violations: missing labels, low contrast, missing landmarks, focus indicators |

**Important:** INP is a runtime metric measured only in RUM (`web-vitals` 6.1.0 → analytics, or a platform RUM product). It cannot be reliably reproduced in CI. Treat a missing RUM setup as a High finding at Gate 7.

**Which layer catches which AI error class:**
- Derived state kept in a second state variable → unit (test across multiple renders)
- Stale response / race → E2E with a simulated slow network (Playwright route interception with a delay)
- Effect or watcher loop → unit (render-count assertion)
- Stale closure → unit (fire the timer / subscription callback after a state change)
- Hydration mismatch → E2E (compare the server HTML to the hydrated DOM; most dev servers throw on mismatch)
- Cross-user bleed from module-scope state → integration (two concurrent SSR renders in one process, two identities)
- Missing teardown / leak → integration (mount and unmount N times, then a heap snapshot filtered to detached nodes)
- INP-blocking handler → RUM only
- Missing UX states → component (story-per-state + axe) + E2E (simulate each network condition)
- Missing error boundary → E2E (inject an error into the data subtree; assert a partial page, not a blank one)
- XSS via a raw-HTML sink → unit (render with a payload string; assert it is escaped in the DOM)
- FOWT → visual regression (screenshot at page load, before hydration)
- Supply chain → `npm audit` and a lockfile diff in CI

---

## Final hand-off — the UI State Matrix

The orchestrator rule: never finish without a UI State Matrix and a short test plan. "It renders" is not done.

### UI State Matrix template

| Scenario | Expected UI |
|---|---|
| Initial load / slow network | Skeleton / loading placeholder — no layout shift when content arrives |
| Empty result set | Empty-state illustration + contextual call-to-action (e.g. "No results — try adjusting filters") |
| Partial load (some data available, rest loading) | Progressive disclosure — render available content; skeletons for pending regions |
| Mutation in flight (optimistic) | Immediate UI update (button disabled, item shows as pending state) |
| Mutation failure | Optimistic update rolled back; toast / inline error with retry action |
| Network offline | Offline banner; mutation queue preserved; cached data still visible (stale label if shown) |
| Auth expired (session timeout / 401) | Redirect to login; current page URL preserved as `?next=` param for post-login return |
| Permission denied (403) | Dedicated permission-denied state (not generic error); no blank page |
| Server error (5xx) | Friendly error with retry; error boundary prevents full-page blank |

### Short test plan

1. **Golden path** — one E2E test that walks the primary user flow end-to-end: load page → interact → see confirmed result.
2. **One test per UI state** — each row in the UI State Matrix above has a corresponding test (Playwright network intercept or mock) that asserts the correct UI is shown.
3. **Double-click / rapid-click safety** — the primary submit/action is debounced or disabled after first click; a rapid-click test asserts exactly one mutation fires.
4. **Mobile keyboard** — form submission tested with the soft keyboard visible (viewport height reduced); no input obscured; submit reachable.
5. **Reduced-motion** — E2E run with `prefers-reduced-motion: reduce` emulated (Playwright `page.emulateMedia`); assert no transitions fire.
6. **What was deferred** — list any Med / Low finding or `pending` risk not addressed in this iteration, so the user makes an explicit deferral decision.
