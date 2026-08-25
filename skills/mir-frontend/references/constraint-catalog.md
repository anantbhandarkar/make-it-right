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
  - All async/fetched data lives in a server-state layer (TanStack Query / SWR, or the framework's route loader / `useAsyncData`) `[DEFAULT — Recommended]` — cache, deduplication, background refetch, stale-while-revalidate handled for you
  - Mixed: server state in the query layer, UI state (modals, selections, filters) in local component state — correct split; most common pattern
  - Everything in a global store (Zustand / RTK / Pinia) — wrong tool for server data; creates stale-data bugs and a duplicate cache

- **Where per-request state lives on a server-rendered path**: which objects are created per request rather than at module scope?
  - Every cache, store, and API client constructed inside the request/render path `[DEFAULT — Recommended]` — the only safe answer; module scope is one object shared by every visitor the process serves
  - A module-scope singleton — only for stateless, user-independent things (a connection pool, a compiled schema). Never for anything holding a user's data or token

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
- **Reactivity tier**: React, Vue, Angular, or no framework at all? A written tier exists for each of those four; Svelte and Solid have none yet. Going framework-free means no render loop and no automatic teardown — a deliberate choice for a widget, a design-system primitive, or an embedded script, and a bad one for an app with interdependent state. Table 2 in `rendering-model-map.md` has the "Do NOT use when…" list
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
  - WCAG 2.2 AA `[DEFAULT — Recommended]` — W3C Recommendation, updated 12 Dec 2024; the level most procurement and accessibility regulations point at. Confirm the specific standard your jurisdiction or contract names rather than assuming
  - WCAG 2.2 AAA — only commit to this if explicitly required; some criteria are impossible to satisfy for all content
  - Internal tooling — still target AA; keyboard parity and focus management are non-negotiable regardless

- **Reduced-motion**: must animations respect `prefers-reduced-motion`?
  - Yes — all animations wrapped with `@media (prefers-reduced-motion: reduce)` or the JS equivalent `[DEFAULT — Recommended]` — users with vestibular disorders are physically affected by this, which is why it is a default here rather than a preference. On the standards: SC 2.3.3 *Animation from Interactions* is **Level AAA**, so honouring the media query is above the AA bar; SC 2.2.2 *Pause, Stop, Hide* is the one that covers motion the page starts on its own
  - No animations used — N/A

---

## Dimension 5 — Performance

- **INP / LCP / CLS budgets** (the three Core Web Vitals; FID was retired in favour of INP):
  - INP ≤ 200 ms (Interaction to Next Paint) — every event handler returns within budget; heavy work deferred off the interaction or moved to a Web Worker
  - LCP ≤ 2.5 s (Largest Contentful Paint) — the one above-fold hero image is priority-loaded; no render-blocking resources
  - CLS ≤ 0.1 (Cumulative Layout Shift) — explicit width/height on images/embeds; no late-injected layout above the fold

- **Virtualization**: is the list large enough to require rendering only visible rows?
  - Not needed (< ~200 rows) `[DEFAULT — Recommended]`
  - Virtualize — required for > ~500 rows; adds item-height measurement complexity. The package is `@tanstack/react-virtual` (3.14.9). `@tanstack/virtual` returns 404 on the registry: check the name, do not autocomplete it (failure-mode #15)

- **Bundle budget**: what is the first-load JS budget for this route?
  - No explicit budget set — flag as a risk `[DEFAULT — Recommended for new projects]`
  - ≤ 100 KB first-load JS (gzipped) — good baseline for content/marketing routes
  - ≤ 250 KB — appropriate for interactive app routes; enforce via bundler size plugin in CI

- **Target devices**: are there known low-memory, low-CPU, or older browser constraints?
  - Modern evergreen browsers only `[DEFAULT — Recommended]`
  - iOS Safari from a named version — every browser on iOS runs WebKit, so "works in Chrome" proves nothing. Check each API you depend on against its Baseline status and test on a real device
  - Low-memory Android — avoid heavy libraries; virtualize aggressively; minimize re-renders

---

## Dimension 6 — Design System

- **Existing component library**: is there a design system or component library already in use?
  - Yes — use existing library (shadcn/ui, MUI, Radix UI, etc.) `[DEFAULT — Recommended]` — avoids style drift; inherit a11y behaviors
  - No — build primitives; establish token/variant convention early; document the system

- **Theme / dark mode**:
  - CSS custom properties (plain CSS variables, or `@theme` in Tailwind — 4.3.3 as of 13 Aug 2026) `[DEFAULT — Recommended]` — no FOUC; framework-agnostic
  - No dark mode required — explicitly ledger this; easier to add early than retrofit

- **Design tokens**: are spacing, color, and typography values tokenized?
  - Yes — consume tokens from design system; do not hardcode values `[DEFAULT — Recommended]`
  - No tokens yet — establish a minimal token set (color, spacing, radius) before Gate 6; prevents ad-hoc magic numbers

---

## Dimension 7 — Security

- **Renders user, third-party, or model-generated content**: does any component render markdown, HTML, or external strings?
  - No — plain text only `[DEFAULT — Recommended]`
  - Yes — render structured elements; if it must stay HTML, sanitize with DOMPurify (≥ 3.4.13, pinned) before it reaches any raw-HTML sink. LLM output counts as user content

- **Client-side secrets risk**: are any sensitive values (API keys, tokens) reachable from client code?
  - No — all secrets server-side only `[DEFAULT — Recommended]`
  - A build-time public env var (`NEXT_PUBLIC_`, `VITE_`, `PUBLIC_`, `NUXT_PUBLIC_`, `REACT_APP_`) — these are inlined into the client bundle at build time and are public. Only non-sensitive config belongs there, and rotation needs a rebuild

- **Auth model**: how does the UI know what to show vs. hide for the current user?
  - Server-side enforcement only; client rendering is a hint `[DEFAULT — Recommended]` — a client-side `if (user.isAdmin)` is a UX courtesy, never a security gate; every sensitive operation is authorized server-side, per object

- **CSP and Trusted Types**: what does the response send today?
  - A CSP with a per-response nonce, `object-src 'none'`, `base-uri 'none'`, and `frame-ancestors` set `[DEFAULT — Recommended]` — the `frame-ancestors` directive is the clickjacking control
  - Trusted Types on top (`require-trusted-types-for 'script'`) — turns a missed sanitizer into a `TypeError` at the sink instead of an execution. Roll out in report-only mode first; one unconverted sink in a dependency breaks the page
  - None, or `'unsafe-inline'` — say so out loud and put it in the risk register; it is what turns one missed sanitizer call into a working XSS

- **CSRF**: how is auth carried on state-changing requests?
  - Cookie or session auth — needs `SameSite` plus a server-side origin check or a double-submit token on every mutating endpoint, Server Actions and RPC included `[DEFAULT — Recommended when cookies are used]`
  - `Authorization: Bearer` header — not CSRF-relevant, but the token then lives somewhere an XSS can read; say where

- **Third-party scripts and tag managers**: what other JavaScript runs on this origin?
  - None `[DEFAULT — Recommended]`
  - Named, pinned scripts with `integrity` + `crossorigin`, preferably self-hosted — list them and name who can change them
  - A tag manager — anyone with container access can inject arbitrary JavaScript into production with no diff and no review. Ledger that as an accepted risk with a named owner, or do not ship it

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
  - Yes — the `web-vitals` library (6.1.0) reporting into analytics, or a platform RUM product `[DEFAULT — Recommended for production]`
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
| Raw HTML injection | No untrusted value reaches a raw-HTML sink — React's raw-HTML prop, `<iframe srcDoc>`, Vue `v-html`, Angular `[innerHTML]`, plain `innerHTML`/`insertAdjacentHTML`. Render structured elements, or sanitize with a pinned DOMPurify first |
| Client authz | `if (user.isAdmin)` in a component is a UX hint — the server enforces, per object; the component never gates security |
| Teardown | Every listener, observer, timer, and subscription a component creates is removed when it goes away — the library's unmount hook, or one `AbortController` per instance with no framework |
