# Checklists — Gate 6 (codegen) & Gate 7 (production-readiness)

These are **strict, checked-against** lists. For each item: either it is satisfied (point to the code), it is consciously N/A (say why), or it is a finding. "Looks fine" is not an allowed state.

---

## Gate 6 — Codegen checklist (while writing)

### State & Data

- [ ] No derived value stored in `useState` — computed values live in render (or `useMemo` only where Compiler is confirmed off and profiling justifies it)
- [ ] All server state managed via TanStack Query (v5); no hand-rolled fetch-in-`useEffect` for remote data
- [ ] On SSR paths, `QueryClient` is instantiated **per request** — never at module scope / never shared across users
- [ ] Client state is minimal: only values that have no server representation and must survive re-renders (UI toggles, form draft, selection)
- [ ] Every mutation is followed by explicit cache invalidation or optimistic update + rollback — no "fire and forget"
- [ ] Cache keys include all dimensions that change the result (tenant, user, filter params) — no cross-user key collisions possible

### Async

- [ ] Every in-flight fetch can be cancelled: `AbortController` in `useEffect` cleanup, or TanStack Query's built-in cancellation
- [ ] Stale-response guard in place: component does not apply a response that arrived after a newer request resolved
- [ ] Optimistic update defined: what the UI shows immediately, and what rollback looks like on failure
- [ ] Retries are bounded (TanStack Query default: 3; override for non-idempotent mutations to `retry: 0`)
- [ ] Every async surface has timeout or `staleTime` / `gcTime` configured; no indefinitely-pending states

### Rendering

- [ ] `<Suspense>` + `<ErrorBoundary>` placed **per data subtree** — not only at the root
- [ ] `useSuspenseQuery` used only where the query is prefetched (Server Component / route loader) — no client-side suspense waterfalls
- [ ] No hydration-unsafe values in render (`Date.now()`, `Math.random()`, `window`, `navigator`, locale) — gated behind `useEffect` or `use client`
- [ ] Heavy or non-urgent state updates wrapped in `startTransition`; deferred values via `useDeferredValue` where appropriate
- [ ] List keys are stable, unique identifiers — never array index on a dynamic/reorderable list
- [ ] No `useEffect` used to derive or sync state that can be computed in render (failure-mode #3)

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

- [ ] INP ≤ 200 ms: heavy work deferred (`startTransition`, Web Worker, `scheduler.yield()`); event handlers do not block the main thread
- [ ] LCP ≤ 2.5 s: primary above-fold image uses `fetchpriority="high"` (or `next/image` with `priority`); no render-blocking resources
- [ ] CLS ≤ 0.1: image, video, and ad slots have explicit width/height or `aspect-ratio` reserved; no late-injected content above the fold
- [ ] No unnecessary `useMemo` / `useCallback` in Compiler-enabled projects; Compiler interop verified (failure-mode #14)
- [ ] Images served in modern format (WebP/AVIF), sized for the display context, lazy-loaded below the fold
- [ ] Fonts use `font-display: swap` or `optional`; subset where possible; preloaded if critical
- [ ] First-load JS bundle within project budget (default starting point: ≤ 100 KB compressed); code-split at route boundaries

### Security

- [ ] Any user-controlled HTML rendered via the raw-HTML prop is sanitized first (DOMPurify or equivalent) — never raw user input
- [ ] Markdown rendered through a safe renderer that does not output unsanitized HTML
- [ ] No secrets in `NEXT_PUBLIC_*` variables (or framework-equivalent public-bundle env vars) — secrets belong in server-only env vars
- [ ] Client-side authorization checks (hidden routes, disabled buttons) are UX hints only; the same rule is enforced server-side
- [ ] CSP headers configured; Trusted Types policy enabled where the framework supports it
- [ ] Server Action inputs are revalidated on the server (Zod or equivalent) — client-supplied values are untrusted
- [ ] SSR dehydration uses a safe serializer (not bare `JSON.stringify` into a `<script>` block) — failure-mode #7

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

### Security-reviewer focus (frontend surfaces)

- Raw-HTML prop usage: every instance audited; user-controlled content always goes through a sanitizer first
- `NEXT_PUBLIC_*` (or equivalent public env) audit: no API keys, signing secrets, or internal identifiers exposed
- Client-side auth checks identified and confirmed as hints only; corresponding server-side enforcement located in code
- CSP policy present and does not use `unsafe-inline` / `unsafe-eval` without documented justification
- Server Actions (or equivalent RPC): each one validates and re-authorizes inputs server-side; CSRF protection via framework default or explicit token
- npm supply-chain: package list cross-checked against npm registry (no hallucinated packages); lock file present; `npm audit` clean at High+

### a11y-reviewer focus (WCAG 2.2 AA)

- Automated axe-core run (in CI or Storybook addon) with zero Critical / Serious violations
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
- React Compiler interop: if Compiler is enabled, no unnecessary manual `useMemo`/`useCallback`; `eslint-plugin-react-hooks` v6 clean; `"use no memo"` used only where documented
- Image/font: `next/image` (or equivalent) used for all content images; `next/font` (or equivalent) for web fonts; no layout shift from font swap

---

## Testing gate

Layered strategy — each layer catches a distinct class of AI-generated errors:

| Layer | Tool (May 2026) | What it catches |
|---|---|---|
| **Unit** | Vitest | Logic errors in hooks, utilities, state machines, reducers; stale-closure bugs in isolated callbacks |
| **Component** | Storybook + Vitest addon *or* Playwright Component Testing | Per-state rendering (empty, error, loading, stale); a11y (axe-core in Storybook a11y addon); visual snapshot baseline |
| **E2E** | Playwright | Full interaction flows; keyboard navigation; focus management; form validation; race conditions under network throttling |
| **Visual regression** | Playwright screenshots *or* Chromatic | FOWT; layout shift; theme switching; responsive breakpoints |
| **A11y automation** | axe-core in CI (via `@axe-core/playwright` or Storybook addon) | WCAG 2.2 AA violations: missing labels, low contrast, missing landmarks, focus indicators |

**Important:** INP is a runtime metric measured only in RUM (e.g. web-vitals.js → analytics). It cannot be reliably reproduced in CI. Treat a missing RUM setup as a High finding at Gate 7.

**Which layer catches which AI error class:**
- Derived state in `useState` → unit (hook test with multiple renders)
- Stale response / race → E2E with simulated slow network (Playwright `routeFulfill` with delay)
- useEffect infinite loop → unit (render count assertion)
- Stale closure → unit (simulate timer / subscription callback after state change)
- Hydration mismatch → E2E (compare SSR HTML to hydrated DOM; Next dev mode throws on mismatch)
- Cross-user queryClient bleed → integration (two concurrent SSR renders in the same process)
- INP-blocking handler → RUM only
- Missing UX states → component (story-per-state + axe) + E2E (simulate each network condition)
- Missing ErrorBoundary → E2E (inject an error into the data subtree; assert partial-page, not blank)
- XSS via raw-HTML prop → unit (render with a payload string; assert it is escaped in the DOM)
- FOWT → visual regression (screenshot at page-load before hydration)
- Supply-chain → `npm audit` in CI

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
