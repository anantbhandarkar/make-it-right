# Rendering-model selection map

Used at **Gate 0** to catch a **render-model/workload mismatch** before any code — and to know which `mir-frontend-<lib>` reactivity tier and framework module to load.

## How to use this at Gate 0

1. Identify the rendering model (chosen or implied) from the task or existing code.
2. Check the workload against **"Do NOT use when…"** below. If the chosen model lands in its own anti-pattern column (e.g. CSR for a public SEO-critical page, RSC for a heavy offline-first PWA), **stop and flag it** — that's a render-model mismatch that no amount of correct component code fixes.
3. Surface the mismatch as a **conscious, ledgered choice** recorded in the Assumption Ledger — never let it be a silent default. Example: "CSR is chosen for a public marketing page — SEO will suffer and LCP will be slow; consider SSG or SSR. Proceed on CSR anyway, or reconsider?"
4. Load the matching reactivity tier (`mir-frontend-react` / `mir-frontend-vue` / `mir-frontend-angular`) and the framework module at Gate 5.

A mismatch isn't an automatic blocker — the team may have valid reasons (existing infra, team familiarity, deployment constraints). But it must be a **conscious, surfaced** choice, not an oversight discovered in production.

---

## The fitness map

| Rendering model | Slug | Typical meta-frameworks | Use when… | Do NOT use when… |
|---|---|---|---|---|
| **CSR SPA** (Client-Side Rendering) | `csr` | Vite + React Router, Vite + TanStack Router, Create React App (legacy) | App-behind-auth (dashboards, admin panels, internal tools); no meaningful SEO need; rich interactivity with minimal server involvement; team wants simple deployment (static hosting) | Public SEO-critical pages (crawlers see an empty shell); slow first paint is unacceptable (LCP suffers on slow devices without SSR); content must be indexable immediately |
| **SSR** (Server-Side Rendering, per-request) | `ssr` | Next.js App Router (server components), Nuxt 3, Angular SSR, Remix / React Router 7 (loader pattern) | Personalized pages that need SEO and fresh server data on every request; auth-gated content that must also rank; pages where stale cached HTML would mislead users | Purely static content that never changes per user (wasteful — use SSG); no server budget or serverless-cold-start sensitivity; team is unfamiliar with server/client split and hydration constraints |
| **RSC + streaming** (React Server Components with Suspense streaming) | `rsc` | Next.js 16 App Router, TanStack Start 1.0 | Large apps with heavy server-data requirements; want minimal client JS bundle; data-fetching colocated with rendering; progressive hydration via Suspense streaming; server-side secrets never leave the server | Highly interactive offline-first apps (RSC components cannot use client hooks); team unfamiliar with the server/client boundary model (footguns: accidental client-side state in server components, double-fetch waterfall across boundaries, `queryClient` singleton bleed in SSR) |
| **SSG / ISR** (Static Site Generation / Incremental Static Regeneration) | `ssg` | Next.js (`generateStaticParams`), Astro, Nuxt generate, Gatsby | Marketing sites, documentation, blogs, content that changes rarely or on a build schedule; maximum CDN cache efficiency; lowest TTFB; SEO-first | Per-user personalized data (every user sees the same cached HTML); real-time data that must be fresh on every view; pages with unbounded dynamic routes (ISR mitigates but doesn't eliminate) |
| **Edge** (Edge Runtime / Edge Middleware) | `edge` | Next.js + Vercel Edge, Cloudflare Workers + Pages, Deno Deploy | Geo-aware low-latency responses (A/B testing, localization redirects, auth edge middleware); lightweight compute that must run close to the user; sub-10 ms middleware decisions | Heavy CPU-bound work (edge runtime has strict CPU time limits); large dependencies (Node.js built-ins not available in edge runtime); tasks that require Node-only APIs (file system, native modules) |

---

## UI library note

The rendering model above is independent of the reactive UI library, but library ecosystem constrains framework options:

| Library | Ecosystem state (May 2026) | When to choose |
|---|---|---|
| **React 19.2.x** | Dominant; RSC + Actions stable; Compiler 1.0 GA; widest meta-framework choice (Next, RR7, TanStack Start, Vite SPA) | Default choice for new greenfield projects without existing Vue/Angular investment; large talent pool |
| **Vue 3.x** | Mature, stable Composition API; Nuxt 3 for SSR/SSG; strong in Asia-Pacific and European markets; excellent progressive-enhancement story | Teams with Vue expertise; projects that need gradual adoption of reactivity into an existing HTML codebase |
| **Angular 17+** | Signals-based reactivity GA; strong for large enterprise teams that benefit from opinions and integrated tooling (CLI, DI, forms, router all in one); Angular SSR (Universal) for SSR/SSG | Enterprise teams; large codebases requiring strong conventions and long-term stability guarantees |

---

## Naming reminder

- Reactivity tier skill: `mir-frontend-<lib>` (e.g. `mir-frontend-react`, `mir-frontend-vue`, `mir-frontend-angular`).
- Framework module: `mir-frontend-<lib>-<framework>` (e.g. `mir-frontend-react-next`, `mir-frontend-vue-nuxt`).
- Currently planned — **Wave 1 (shipping):** React tier + React modules:
  - `mir-frontend-react` → `-next`, `-remix`, `-tanstack-start`, `-spa`
- **Wave 4 (later):**
  - `mir-frontend-vue` → `-nuxt`
  - `mir-frontend-angular`
- Add more frameworks/libraries via the recipe in `EXTENDING.md`.
