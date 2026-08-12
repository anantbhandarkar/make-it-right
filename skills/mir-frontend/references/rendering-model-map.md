# Rendering-model selection map

Used at **Gate 0** for two decisions: the **rendering model** (how HTML gets to the browser) and the **reactivity tier** (what, if anything, owns rendering in the browser). They are independent choices, and both belong in the Assumption Ledger.

## How to use this at Gate 0

1. Identify the rendering model (chosen or implied) from the task or existing code.
2. Check the workload against **"Do NOT use when…"** below. If the chosen model lands in its own anti-pattern column (e.g. CSR for a public SEO-critical page, RSC for a heavy offline-first PWA), **stop and flag it** — that's a mismatch no amount of correct component code fixes.
3. Record the mismatch as a **conscious, ledgered choice** — never a silent default. Example: "CSR is chosen for a public marketing page — SEO will suffer and LCP will be slow; consider SSG or SSR. Proceed on CSR anyway, or reconsider?"
4. Pick the reactivity tier from the second table and load it, plus the framework module if one exists, at Gate 5.

A mismatch isn't an automatic blocker — the team may have valid reasons (existing infra, team familiarity, deployment constraints). But it must be a **conscious, stated** choice, not an oversight discovered in production.

---

## Table 1 — rendering-model fitness

| Rendering model | Slug | Typical meta-frameworks | Use when… | Do NOT use when… |
|---|---|---|---|---|
| **CSR SPA** (Client-Side Rendering) | `csr` | Vite + React Router, Vite + TanStack Router, Vite + Vue Router | App-behind-auth (dashboards, admin panels, internal tools); no meaningful SEO need; rich interactivity with minimal server involvement; team wants simple deployment (static hosting) | Public SEO-critical pages (crawlers see an empty shell); slow first paint is unacceptable (LCP suffers on slow devices without SSR); content must be indexable immediately |
| **SSR** (Server-Side Rendering, per-request) | `ssr` | Next.js App Router, Nuxt, React Router (framework mode), Angular SSR | Personalized pages that need SEO and fresh server data on every request; auth-gated content that must also rank; pages where stale cached HTML would mislead users | Purely static content that never changes per user (wasteful — use SSG); no server budget or serverless-cold-start sensitivity; team is unfamiliar with the server/client split and hydration constraints |
| **RSC + streaming** (React Server Components with Suspense streaming) | `rsc` | Next.js App Router, TanStack Start | Large apps with heavy server-data requirements; want minimal client JS; data-fetching colocated with rendering; HTML and the RSC payload stream in with only Client Components hydrating (Server Components never hydrate); server-side secrets never leave the server | Highly interactive offline-first apps (Server Components cannot use client hooks); team unfamiliar with the server/client boundary (footguns: client state in a server component, double-fetch waterfall across the boundary, a module-scope cache or query client shared across SSR requests) |
| **SSG / ISR** (Static Site Generation / Incremental Static Regeneration) | `ssg` | Next.js (`generateStaticParams`), Astro, Nuxt `generate` | Marketing sites, documentation, blogs, content that changes rarely or on a build schedule; maximum CDN cache efficiency; lowest TTFB; SEO-first | Per-user personalized data (every user sees the same cached HTML); real-time data that must be fresh on every view; pages with unbounded dynamic routes (ISR mitigates but doesn't eliminate) |
| **Edge** (Edge Runtime / Edge Middleware) | `edge` | Next.js + Vercel Edge, Cloudflare Workers + Pages, Deno Deploy | Geo-aware low-latency responses (A/B testing, localization redirects, auth-adjacent redirects); lightweight compute that must run close to the user; sub-10 ms middleware decisions | Heavy CPU-bound work (strict CPU time limits); large dependencies (Node built-ins are unavailable); tasks needing Node-only APIs (file system, native modules). Also: never as the only authorization check — the middleware-bypass advisory class is real, see `mir-frontend-react-next` |

---

## Table 2 — reactivity-tier fitness

Independent of the rendering model above. This picks which `mir-frontend-<lib>` tier loads at Gate 5. Versions checked against the npm registry on 13 Aug 2026.

| Tier | Current stable | Use when… | Do NOT use when… |
|---|---|---|---|
| **React** — `mir-frontend-react` | react 19.2.8; babel-plugin-react-compiler 1.0.0 | Greenfield with no existing Vue/Angular investment; you need RSC, Actions, or the widest meta-framework choice (Next.js 16.3.0, react-router 8.3.0, @tanstack/react-start 1.168.44, Vite SPA); the team is already hiring against it | The page is mostly static content with one interactive widget — you are shipping a runtime to do a script's job; the team has no React experience and the app is small |
| **Vue 3** — `mir-frontend-vue` | vue 3.5.41; nuxt 4.5.2 | The team knows Vue; you want reactivity adopted gradually into an existing server-rendered HTML codebase; SFCs and the template compiler fit the work | You need React-only ecosystem pieces (RSC, a React-only component library) with no Vue equivalent; the app is one widget (go vanilla) |
| **No framework** — `mir-frontend-vanilla` | platform only — see that skill's Baseline table | A widget embedded in someone else's page (their globals, their CSP, their prototypes); a design-system primitive or custom element meant to work under every framework; a browser-extension content script; a static site with one script; a bundle budget a framework runtime cannot fit inside | **Any app with more than a handful of interdependent states.** There is no render loop and no automatic teardown: every listener, observer, timer, and DOM write is yours to undo, and every state change is yours to reflect by hand. Also do not go framework-free for: forms with cross-field validation, routed multi-screen apps, anything server-rendered and hydrated, or lists that reorder and filter. What gets built instead is an in-house reactivity system with no docs, no tests, and one person who understands it |
| **Angular** — no tier written | @angular/core 22.1.1 | Large enterprise codebases that benefit from one opinionated stack (CLI, DI, forms, router, SSR in one) | You want tier-level coverage from this kit — there is none for Angular, so you get the pillar gates and nothing about signals, DI, or the Angular compiler |
| **Svelte / Solid** — no tier written | svelte 5.56.9 | Team preference and small bundle targets | Same gap: no tier exists, so runes, stores, and compiler behaviour are uncovered here |

For Angular, Svelte, and Solid there is **no reactivity tier yet**. Run the `mir-frontend` gates alone, say so in the design, and treat every tier-level rule as unwritten rather than assumed.

---

## Naming reminder

- Reactivity tier: `mir-frontend-<lib>` — **written:** `mir-frontend-react`, `mir-frontend-vue`, `mir-frontend-vanilla`.
- Framework module: `mir-frontend-<lib>-<framework>` — **written:** `mir-frontend-react-next`, `mir-frontend-vue-nuxt`.
- **Planned, not written, not loadable:** `mir-frontend-react-remix` (React Router / Remix) and `mir-frontend-angular`. Both are on the repo's planned-not-written list; `validate.py` downgrades a reference to them from an error to a warning so the gap stays visible.
- No module exists yet for TanStack Start, a Vite React SPA, Quasar, or a Vite Vue SPA. Those stacks get the pillar plus the tier.
- Add more via the recipe in `EXTENDING.md`.
