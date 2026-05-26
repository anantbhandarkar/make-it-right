# Constraint Catalog — the frontend question bank

The constraint-interrogator sweeps these dimensions, then returns only the 2–4 questions whose answers most change the implementation. **This file is the source pool, not the question list** — never dump all of it at the user. Rank by leverage: a question is high-leverage if two reasonable answers would produce materially different code, architecture, or UX.

For every question the interrogator surfaces, it must attach 2–4 concrete options, mark one `[DEFAULT — Recommended]`, and give a one-line reason an expert would choose it.

> **Rank ruthlessly, ≤ 4 questions per round. Mark one `[DEFAULT — Recommended]` per question.**
> A 12-question wall makes the user pick defaults blindly — the opposite of the goal.

---

## Dimension 1 — UX & Interaction

The questions AI skips because the answers live in a designer's head or a PRD no one linked.

- **Save semantics**: when does the user's work get persisted?
  - Autosave on every keystroke (debounced write) — for long-form content where losing work is catastrophic
  - Autosave on blur (field exit) — balanced; standard for form-heavy apps
  - Explicit save button only `[DEFAULT — Recommended]` — clearest mental model; safest for destructive-capable forms
  - Hybrid: autosave draft locally, explicit save to server

- **Search / filter behavior**: how do results update as the user types?
  - Debounce 300 ms + cancel stale requests (AbortController) `[DEFAULT — Recommended]` — avoids flicker and wasted round-trips
  - Search on Enter only — appropriate for expensive server-side full-text search with no autocomplete
  - Instant (no debounce) — only valid for local in-memory filtering (< ~500 rows, no network call)
  - Minimum length threshold (e.g. 3 chars) before first query

- **Pagination vs. infinite-scroll vs. virtualization**:
  - Paginated with explicit page controls `[DEFAULT — Recommended]` — best for SEO, back-button correctness, filter/sort retention; required for bookmark-able state
  - Infinite scroll (append on scroll) — good for social/content feeds; breaks back-button; requires intersection observer + cleanup
  - Virtualized list (render only visible rows) — mandatory for > ~500 rows in DOM; adds complexity; requires known item heights or dynamic measurement

- **Optimistic updates + rollback**: after a mutation, does the UI update before server confirmation?
  - No optimistic update — wait for server response before updating UI `[DEFAULT — Recommended]` — simplest; avoids rollback complexity; appropriate when latency is acceptable
  - Optimistic update with rollback — update UI immediately; revert on error; requires explicit rollback state in UI State Machine
  - Optimistic update without rollback — only for idempotent, low-stakes actions (e.g. toggle a like)

- **Error UX**: how are errors communicated to the user?
  - Inline error within the form/component `[DEFAULT — Recommended]` — closest to the error source; best for form validation
  - Toast/snackbar (auto-dismiss) — good for transient, non-blocking notifications; bad for errors that need action
  - Error dialog (requires dismissal) — reserved for destructive or blocking errors
  - Silent retry (no visible error until N failures) — only for background auto-save; never for user-initiated actions

- **Destructive actions**: does any action delete, publish, or irreversibly change data?
  - Confirmation dialog before action `[DEFAULT — Recommended]` — mandatory for delete; best practice for publish/archive
  - Undo within a time window — superior UX but requires server-side soft-delete or event sourcing
  - No confirmation needed — only for trivially reversible actions (e.g. removing a local filter)

---

## Dimension 2 — State & Data

- **Server state vs. client state**: what data is fetched from the server vs. purely local?
  - All async/fetched data lives in server-state library (TanStack Query / SWR) `[DEFAULT — Recommended]` — cache, deduplication, background refetch, stale-while-revalidate handled automatically
  - Mixed: server state in query library, UI state (modals, selections, filters) in local `useState` — correct split; most common pattern
  - Everything in a global store (Zustand/RTK) — anti-pattern for server data; creates stale-data bugs and duplicated cache

- **Offline support**: does the app need to function without a network connection?
  - No offline support required `[DEFAULT — Recommended]` — simplest; appropriate for most dashboard/app-behind-auth contexts
  - Read-only offline (show stale cached data) — requires explicit stale indicators and OFFLINE state in UI State Machine
  - Full offline-first with sync — requires service worker, IndexedDB, conflict resolution; major architectural commitment

- **Real-time updates**: does data need to update automatically without user action?
  - No real-time; user manually refreshes or navigates `[DEFAULT — Recommended]`
  - Polling (refetch every N seconds) — simple; appropriate for low-stakes dashboards
  - WebSocket / SSE — required for chat, live collaboration, stock tickers; adds connection lifecycle complexity

- **Data freshness / stale time**: how long is fetched data considered fresh?
  - Default stale time (0 ms — always stale, refetch on window focus) `[DEFAULT — Recommended]` — safe default; ensures fresh data on re-navigation
  - Explicit stale time (e.g. 5 min) — appropriate for data that changes infrequently; reduces server load
  - Infinite stale time (never auto-refetch) — only for truly immutable data (e.g. static config, reference lists)

- **Multi-tab sync**: if the user has the app open in multiple tabs, does state need to stay in sync?
  - No multi-tab sync required `[DEFAULT — Recommended]`
  - BroadcastChannel / storage events for cross-tab invalidation — needed when a mutation in one tab must reflect in another
  - Full sync via service worker — only for offline-first apps

- **Draft persistence**: where do in-progress form values live if the user navigates away?
  - No draft persistence — form resets on navigation `[DEFAULT — Recommended]` — simplest; appropriate for short forms
  - LocalStorage (survives refresh, same origin only) — good for long-form content; add explicit "clear draft" path
  - SessionStorage (survives soft navigation, cleared on tab close)
  - Server-side draft (auto-saved to DB) — highest durability; requires a draft API endpoint and conflict handling

---

## Dimension 3 — Rendering

- **Rendering model**: CSR, SSR, RSC+streaming, SSG/ISR, or Edge? (consult `rendering-model-map.md` at Gate 0)
- **Streaming / Suspense boundaries**: which parts of the page stream progressively vs. block render?
  - Stream non-critical panels behind Suspense; block only above-the-fold critical content `[DEFAULT — Recommended]`
  - Block entire page until all data is ready — simpler; avoid for pages with slow-loading sub-sections
- **Hydration constraints**: are there known date/random/locale mismatches between server and client render?
- **SEO / metadata needs**: does this route need `<title>`, `<meta description>`, Open Graph? (forces SSR/SSG + `generateMetadata` or equivalent)
- **Double-fetch waterfall risk**: does the component fetch data that its parent also fetches? (flag and colocate)

---

## Dimension 4 — Accessibility

- **Keyboard parity**: must every interactive element be reachable and operable via keyboard alone?
  - Yes — full keyboard parity required `[DEFAULT — Recommended]` — WCAG 2.2 AA mandate; non-negotiable for public-facing products
  - Partial — keyboard support for primary flows only — explicitly ledger which flows are excluded and why

- **Screen-reader support level**:
  - ARIA roles and live regions for all dynamic content `[DEFAULT — Recommended]` — correct baseline
  - Full screen-reader testing with NVDA/JAWS/VoiceOver — required for regulated or high-traffic public interfaces
  - ARIA on primary actions only — explicitly ledger exceptions

- **WCAG target**:
  - WCAG 2.2 AA `[DEFAULT — Recommended]` — current ISO/IEC 40500:2025 standard; legal baseline in most jurisdictions
  - WCAG 2.2 AAA — only commit to this if explicitly required; some criteria are impossible to satisfy for all content
  - Internal tooling — still target AA; keyboard parity and focus management are non-negotiable regardless

- **Reduced-motion**: must animations respect `prefers-reduced-motion`?
  - Yes — all animations wrapped with `@media (prefers-reduced-motion: reduce)` or JS equivalent `[DEFAULT — Recommended]` — WCAG 2.3.3 (AA); users with vestibular disorders require this
  - No animations used — N/A

---

## Dimension 5 — Performance

- **INP / LCP / CLS budgets** (Core Web Vitals, FID removed Sep 2024):
  - INP ≤ 200 ms (Interaction to Next Paint) — all event handlers must return within budget; heavy work in `startTransition` or `scheduler.yield()`
  - LCP ≤ 2.5 s (Largest Contentful Paint) — hero images preloaded; `next/image` with priority; no render-blocking resources
  - CLS ≤ 0.1 (Cumulative Layout Shift) — explicit width/height on images/embeds; no late-injected layout above the fold

- **Virtualization**: is the list large enough to require rendering only visible rows?
  - Not needed (< ~200 rows) `[DEFAULT — Recommended]`
  - Virtualize with `@tanstack/virtual` or `react-window` — required for > ~500 rows; adds item-height measurement complexity

- **Bundle budget**: what is the first-load JS budget for this route?
  - No explicit budget set — flag as a risk `[DEFAULT — Recommended for new projects]`
  - ≤ 100 KB first-load JS (gzipped) — good baseline for content/marketing routes
  - ≤ 250 KB — appropriate for interactive app routes; enforce via bundler size plugin in CI

- **Target devices**: are there known low-memory, low-CPU, or older browser constraints?
  - Modern evergreen browsers only `[DEFAULT — Recommended]`
  - iOS Safari (specific version) — WebKit quirks; no SharedArrayBuffer; limited PWA support
  - Low-memory Android — avoid heavy libraries; virtualize aggressively; minimize re-renders

---

## Dimension 6 — Design System

- **Existing component library**: is there a design system or component library already in use?
  - Yes — use existing library (shadcn/ui, MUI, Radix UI, etc.) `[DEFAULT — Recommended]` — avoids style drift; inherit a11y behaviors
  - No — build primitives; establish token/variant convention early; document the system

- **Theme / dark mode**:
  - CSS custom properties (`@theme` in Tailwind v4.3 or CSS variables) `[DEFAULT — Recommended]` — no FOUC; framework-agnostic
  - No dark mode required — explicitly ledger this; easier to add early than retrofit

- **Design tokens**: are spacing, color, and typography values tokenized?
  - Yes — consume tokens from design system; do not hardcode values `[DEFAULT — Recommended]`
  - No tokens yet — establish a minimal token set (color, spacing, radius) before Gate 6; prevents ad-hoc magic numbers

---

## Dimension 7 — Security

- **Renders user or third-party content**: does any component render markdown, HTML, or external user-supplied strings?
  - No — plain text only `[DEFAULT — Recommended]`
  - Yes — sanitize with DOMPurify before any use of React's raw-HTML prop; add CSP `script-src` and `style-src` directives; prefer structured rendering over raw HTML injection

- **Client-side secrets risk**: are any sensitive values (API keys, tokens) exposed to client code?
  - No — all secrets server-side only `[DEFAULT — Recommended]`
  - `NEXT_PUBLIC_` / `VITE_` env vars — these are baked into the client bundle at build time; never put secrets here; only public, non-sensitive config

- **Auth model**: how does the UI know what to show vs. hide for the current user?
  - Server-side enforcement only; client rendering is a hint `[DEFAULT — Recommended]` — client-side `if (user.isAdmin)` is a UX courtesy, never a security gate; all sensitive operations must be server-authorized

---

## Dimension 8 — Ops

- **Telemetry / analytics events**: which user interactions must emit structured events?
  - Key conversion actions (form submit, CTA click, error shown) `[DEFAULT — Recommended]`
  - All interactions — only if product analytics requires full funnels; adds noise to high-frequency events

- **Error tracking**: where are JS errors and unhandled rejections captured?
  - Sentry (or equivalent) with ErrorBoundary reporting `[DEFAULT — Recommended]`
  - Console.error only — not acceptable for production; errors disappear when the user closes the tab

- **Feature flags**: are any UI elements behind a feature flag?
  - No `[DEFAULT — Recommended]` — simpler; use if A/B testing or gradual rollout is required
  - Yes — flag keys must be documented in the Assumption Ledger; server-side evaluation preferred over client-side to prevent flag-value leakage

- **RUM (Real User Monitoring)**: is real-user Core Web Vitals data being collected?
  - Yes — `web-vitals` library or platform RUM (Vercel Analytics, Datadog RUM) `[DEFAULT — Recommended for production]`
  - No — flag as a risk; without RUM, CWV regressions are invisible until users complain

---

## Interaction laws — non-negotiable defaults

These are not questions — they are constraints that apply unless explicitly overridden with a ledgered reason:

| Law | Rule |
|---|---|
| Spinner duration | No spinner longer than 400 ms without a skeleton placeholder — spinners without content shape cause layout jank on resolve |
| Destructive actions | All delete/publish/archive actions require confirmation or an undo window — no exceptions |
| Keyboard parity | Every interactive element reachable and operable by Tab/Enter/Space/Arrow keys — WCAG 2.2 AA non-negotiable |
| Empty state | Every data-fetching component ships with an empty state (not a blank rectangle) — design it at Gate 2, not post-launch |
| Error state | Every async operation ships with an error state and a recovery path — "try again" is the minimum |
| Reduced-motion | Every CSS animation or JS-driven transition wrapped in `prefers-reduced-motion: reduce` — vestibular users require it |
| Raw HTML injection | React's raw-HTML prop is never used on untrusted input — sanitize first with DOMPurify or prefer structured rendering |
| Client authz | `if (user.isAdmin)` in a component is a UX hint — the server enforces; the component never gates security |
