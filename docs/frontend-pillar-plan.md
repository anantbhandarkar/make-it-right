# Make It Right — Frontend Pillar Plan

> Status: **PLAN (not yet built)** · Target reality: **May 2026** · Seeded from a ChatGPT "Frontend Reliability Skill" draft + a verified currency/gap analysis.
> This document is the executable spec for adding the `mir-frontend` pillar to Make It Right. It follows the same 3-tier, lazy-loaded, gate-driven contract as `mir-backend`.

---

## 0. Architecture Decision (ADR)

**Decision: the frontend middle tier is the reactivity library, not the rendering model or the meta-framework.**

```
mir-frontend                          generic pillar — reactive-UI reliability, any library
mir-frontend-react                    reactivity "runtime" tier — React's model (shared by every React meta-framework)
mir-frontend-react-next               framework module — Next.js App Router
mir-frontend-react-remix              framework module — React Router 7 (Framework Mode, ex-Remix)
mir-frontend-react-tanstack-start     framework module — TanStack Start
mir-frontend-react-spa                framework module — Vite + React Router/TanStack Query (CSR SPA)
mir-frontend-vue · mir-frontend-vue-nuxt        (later) Vue sibling tier + module
mir-frontend-angular                            (later) Angular sibling tier + module
```

**Why reactivity = the "runtime":** the backend middle tier is the *execution substrate* (CPython, V8). The frontend execution substrate is the **reactivity engine** — React's hooks rules, render/commit model, concurrent rendering, effect semantics. Those footguns are identical across Next.js, Remix/RR7, TanStack Start, and a Vite SPA, so they belong in one shared tier — exactly as the GIL lives once in `mir-backend-python`, not duplicated in FastAPI/Django/Flask. The meta-framework (Next.js) is then the *framework module* on top of React, isomorphic to FastAPI-on-CPython.

**Rejected alternatives** (both considered, both flawed):
- *Rendering-model tier (csr/ssr/ssg):* SSR vs CSR is now a **per-route** choice inside App Router / TanStack Start — a hard tier creates false binaries, and React would be duplicated under every model. → demoted to a **Gate-0 fitness decision** (`rendering-model-map.md`), not a tier.
- *Meta-framework tier:* couples the UI library into the tier and **duplicates React-level concerns** (hooks, effects, Suspense) across every framework module.

**Consequence:** rendering model is selected at Gate 0 via a fitness map (like backend runtime fitness), and SSR/hydration *primitives* live in the React tier while *framework wiring* of SSR lives in the meta-framework module.

---

## 1. The frontend gate pipeline (adapted from mir-backend)

Same hard-gated pipeline; the hard rule is rephrased for UI:

> **You are FORBIDDEN from writing component code until Gate 5 passes.** (Override only with `--advisory`.) If the UX is unspecified, **stop and ask** — when UX is incomplete, AI improvises pagination, save, retry, debounce, and sort semantics.

| Gate | Backend | Frontend adaptation |
|---|---|---|
| 0 Intent & Triage | risk surface + runtime-map | **UX intent + render-model fitness** (consult `rendering-model-map.md`); classify the frontend risk surface (below) |
| 1 Constraint Interrogation `[USER]` | constraint-interrogator | same agent, frontend constraint-catalog — the **must-ask UX questions** (save/search/pagination/optimistic/error-UX/freshness/multi-tab) |
| 2 Assumption Ledger `[USER]` | assumptions | UX + interaction-contract ledger (e.g. "search debounced 300ms; sorting client-side; drafts persist local") |
| 3 Invariants & Failure | invariants, state machine | **UI state machine** (IDLE/LOADING/SUCCESS/EMPTY/ERROR/STALE/RETRYING/OPTIMISTIC/ROLLING_BACK/OFFLINE) + a11y invariants |
| 4 Risk Register | Risk\|Sev\|Mitigation | + a11y, INP/CWV, bundle, hydration, XSS risks |
| 5 Design Review `[USER]` | tx/consistency/observability | **state ownership (server vs client), rendering ownership, interaction contracts, a11y plan, perf budget, telemetry plan** → sign-off |
| 6 Implementation | codegen checklist | frontend codegen checklist |
| 7 Production-Readiness | reliability/security/migration reviewers | reliability + security(+FE) + **a11y** + **performance** reviewers; end with a **UI State Matrix** + test plan |

### Frontend risk surface (Gate 0 tick-boxes — each forces mandatory dimensions)
- Manages **shared/global client state** → state ownership, cross-user bleed (esp. SSR `queryClient` singleton)
- **Fetches data** → loading/empty/error/stale states, race/cancel (AbortController), cache invalidation
- **Server-rendered** (SSR/RSC) → hydration mismatch, server/client boundary, double-fetch waterfall
- **Forms / user input** → validation timing, dirty/draft, autosave races, server-side revalidation
- Renders **user / third-party content** → XSS, sanitization, CSP/Trusted Types
- **Auth-gated UI** → client authz is a hint only (never a gate); expired-auth UX
- **Heavy work on interaction** → INP budget, `startTransition`/`scheduler.yield()`
- **Animations** → `prefers-reduced-motion`, layout thrash
- **Long-lived navigation** → scroll restoration, multi-tab sync, undo, retained filters
- **Localized / RTL** → i18n keys, CSS logical properties
- **Public / SEO-critical** → SSR/SSG + metadata

---

## 2. Content allocation — the placement test

> **"True for Vue/Angular too (any reactive UI)?"** → **`mir-frontend`** (generic).
> **"True for every React meta-framework (Next + Remix + TanStack Start + Vite-SPA)?"** → **`mir-frontend-react`** (reactivity tier).
> **"Only this meta-framework?"** → the **module** (`mir-frontend-react-next`, …).
> New reactivity lib (Vue/Angular/Svelte) → new tier. New React meta-framework → new module under `mir-frontend-react-`.

| Topic (from draft + gap analysis) | Tier |
|---|---|
| State ownership / server-vs-client split; UX state matrix; interaction contracts; async correctness *principles*; accessibility invariants; client-security principles; telemetry/RUM expectation; cognitive load; design-intent preservation | **pillar** `mir-frontend` |
| Hooks rules; effect-dependency discipline ("derive in render"); stale closures; `key` correctness; controlled/uncontrolled; Suspense + Error Boundary placement; `useTransition`/`useDeferredValue`; **React Compiler interop + `"use no memo"`**; concurrent rendering | **React tier** `mir-frontend-react` |
| RSC vs client-component boundary; **Server Actions security/CSRF + progressive enhancement**; framework caching/revalidation; file-based routing data waterfalls; hydration *wiring*; `generateMetadata`/SEO; `next/image` + `next/font`; Turbopack | **Next module** `mir-frontend-react-next` |
| Loader/action error handling; nested-route data waterfalls; progressive enhancement | **Remix/RR7 module** |
| `queryClient`-per-request, dehydrate/hydrate, RSC-as-cacheable-data | **TanStack Start module** |
| Server-state lib (TanStack Query) vs client-state (Zustand/Jotai/RTK); cache invalidation after mutation; **unsafe `JSON.stringify` dehydration (XSS)** | React tier (principles) + module (wiring) |
| Forms: RHF vs TanStack Form; Zod; async/dependent validation; `isTouched` gating; autosave/draft | React tier + pillar (UX contract) |
| **CWV/INP budgets + RUM**; bundle-size budget; image/font; **supply-chain / `NEXT_PUBLIC_` leakage**; CSP/Trusted Types; **testing-as-a-gate**; i18n/RTL; design tokens/dark-mode FOWT; feature flags | pillar `checklists.md` + reviewers (perf/security/a11y) |

---

## 3. Files to create

```
skills/
  mir-frontend/
    SKILL.md                          # gates (adapted), FE risk surface, persona, edit map
    references/
      constraint-catalog.md           # FE dimensions: UX/State/Data/Render/A11y/Security/Perf/Ops + must-ask UX Qs
      failure-mode-catalog.md         # hydration mismatch, race/stale response, derived-state-in-useState,
                                       #   effect loops, XSS, cross-user cache bleed, INP-blocking handlers,
                                       #   FOWT, waterfall fetch, UX-state-explosion, hallucinated UX
      checklists.md                   # Gate 6 codegen + Gate 7 readiness (incl. CWV/INP, bundle, test gate,
                                       #   i18n/RTL, CSP/NEXT_PUBLIC_, design tokens) + the UI State Matrix template
      rendering-model-map.md          # FITNESS MAP (Gate 0): CSR/SSR/RSC-streaming/SSG-ISR/Edge × use-when / do-NOT
  mir-frontend-react/
    SKILL.md                          # React reactivity tier (shared by all React meta-frameworks)
  mir-frontend-react-next/
    SKILL.md
    references/ (next-gotchas.md, rsc-and-server-actions.md, next-caching.md)
  mir-frontend-react-remix/ SKILL.md
  mir-frontend-react-tanstack-start/ SKILL.md
  mir-frontend-react-spa/ SKILL.md     # Vite + React Router/TanStack Query CSR
agents/
  a11y-reviewer.md                    # NEW — WCAG 2.2 AA, axe-core, focus/keyboard/target-size/reduced-motion
  frontend-perf-reviewer.md           # NEW — CWV/INP budgets, bundle, waterfalls, Compiler interop, image/font
  # reuse: constraint-interrogator (+ FE catalog), reliability-reviewer (async/state correctness)
  # extend: security-reviewer description to add FE surfaces (XSS, NEXT_PUBLIC_ leakage, CSP/Trusted Types,
  #         client-side authz, Server-Action CSRF, npm supply-chain)
```

UX / design-system / architecture review concerns ride as **Gate-7 checklist sections** (not separate agents) to avoid agent sprawl; promote to agents only if they earn it.

---

## 4. Reviewer agents (frontend)

| Agent | Status | Focus |
|---|---|---|
| `constraint-interrogator` | reuse | proposes the 2–4 must-ask UX questions w/ `[DEFAULT — Recommended]`, from FE constraint-catalog |
| `reliability-reviewer` | reuse | async correctness, race/cancel, optimistic rollback, state-machine completeness, error/empty/loading coverage |
| `security-reviewer` | extend | + XSS / raw-HTML injection props, `NEXT_PUBLIC_` secret leakage, CSP/Trusted Types, client-authz-as-hint, Server-Action CSRF & server-side revalidation, npm supply-chain hygiene |
| `a11y-reviewer` | **new** | WCAG 2.2 AA; axe-core findings; keyboard reachability; focus management/traps; Focus Appearance 3:1; target size ≥24px; reduced-motion; semantic HTML |
| `frontend-perf-reviewer` | **new** | INP ≤200ms / LCP ≤2.5s / CLS ≤0.1; bundle ≤~100KB first-load; fetch waterfalls; Compiler interop (no blind `useMemo`); `next/image`+`next/font`; RUM presence |

All new agents follow the existing contract: `tools: Read, Grep, Glob, Bash` · `model: sonnet` · read-only, severity-tagged findings table + one-line verdict, "Do not edit code. Report only."

---

## 5. Rendering-model fitness map (Gate 0 reference)

`skills/mir-frontend/references/rendering-model-map.md` — mirrors `runtime-map.md`:

| Model | Slug | Typical meta-frameworks | Use when… | Do NOT use when… |
|---|---|---|---|---|
| CSR SPA | `csr` | Vite + React Router/TanStack | App-behind-auth, dashboards, no SEO need | Public SEO-critical pages; slow first paint unacceptable |
| SSR (per-request) | `ssr` | Next App Router, Nuxt, Angular SSR | Personalized + SEO + fresh data | Purely static content (wasteful); no server budget |
| RSC + streaming | `rsc` | Next App Router, TanStack Start | Large apps, server-data-heavy, want minimal client JS | Highly interactive offline-first; team unfamiliar w/ boundary model |
| SSG / ISR | `ssg` | Next, Astro, Nuxt generate | Marketing, docs, content that changes rarely | Per-user or real-time-on-every-view data |
| Edge | `edge` | Next/Vercel edge, Cloudflare | Geo-low-latency, light compute | Heavy CPU, large deps, Node-only APIs |

Gate 0 flags a mismatch (e.g. "CSR chosen for a public marketing site → SEO will suffer; consider SSG/SSR") as a conscious, ledgered choice.

---

## 6. May-2026 currency baseline (pin these; correct the draft's staleness)

| Area | Current (May 2026) | Draft was stale on |
|---|---|---|
| React | 19.2.x stable; RSC + Actions stable | "RSC experimental" |
| React Compiler | **1.0 GA (Oct 2025)** — blind `useMemo`/`useCallback` now a *liability*; lint via `eslint-plugin-react-hooks` v6 | implied manual memoization advice |
| Next.js | **16.x** (App Router canonical; encrypted Server-Action closures) | "Next 15" |
| Remix / RR7 | Remix→**React Router 7** merge **done** (Dec 2024); "Remix 3" beta is a separate non-React project | merge framed as pending |
| TanStack | Query v5, Router v1, **Start 1.0 GA**; `useSuspenseQuery` first-class | n/a (add) |
| Build | **Vite 8 + Rolldown** default; Turbopack for Next dev | "Vite" (pre-Rolldown) |
| CSS | **Tailwind v4.3** CSS-first `@theme` | v3 `tailwind.config.js` model |
| Core Web Vitals | **LCP / INP / CLS** (FID removed Sep 2024) | referenced FID |
| A11y | **WCAG 2.2 AA** (ISO/IEC 40500:2025); WCAG 3.0 = trajectory only (CR ~2027) | treated 3.0 as near-term |
| Supply chain | TanStack npm compromise (May 2026, CVE-2026-45321) → pin + audit + provenance-isn't-enough | absent |

---

## 7. Build roadmap (orchestrated, same pattern as backend)

Parallel `model: sonnet` sub-agents, each seeded with specific footguns + the existing skills as the style template. Waves:

- **Wave 0 — Foundation:** `mir-frontend` SKILL + 4 references (constraint-catalog, failure-mode-catalog, checklists, rendering-model-map); create `a11y-reviewer` + `frontend-perf-reviewer`; extend `security-reviewer` description. *(Do first — everything else references these.)*
- **Wave 1 — Reactivity tier:** `mir-frontend-react` (hooks/effects/closures/keys/Suspense+ErrorBoundary/transitions/Compiler interop/controlled-uncontrolled).
- **Wave 2 — First meta-framework:** `mir-frontend-react-next` + references (RSC boundary, Server Actions security, caching, metadata, image/font). *(Next first: largest install base, most complex reliability surface.)*
- **Wave 3 — More React modules (parallel):** `mir-frontend-react-remix`, `mir-frontend-react-tanstack-start`, `mir-frontend-react-spa`.
- **Wave 4 — Other reactivity tiers (later):** `mir-frontend-vue` + `mir-frontend-vue-nuxt`; `mir-frontend-angular`.

Each skill must carry `TRIGGER`/`SKIP` routing (so a Next task loads `mir-frontend` + `mir-frontend-react` + `mir-frontend-react-next` only) and an Edit-boundary section.

---

## 8. Cross-cutting notes

- **AGENTS.md:** keep thin. A frontend repo gets its own project-level `AGENTS.md` (FE persona + hard rule + gate names) rather than concatenating into the backend baseline. The global `AGENTS.md` stays pillar-agnostic where possible.
- **Installer:** no changes — `install.sh` globs `skills/*/` and `agents/*.md`; new skills auto-register on the next `./install.sh`.
- **README/EXTENDING:** flip the `mir-frontend` row from 🔜 to shipping per wave; add Vue/Angular as planned.
- **Relationship to CodeRiskKit:** frontend *review* overlaps CodeRiskKit's React/TS scope — scope `mir-frontend` to *build-time/plan-time* reliability and let the reviewers (or CodeRiskKit) own pre-merge review; cross-link to avoid two divergent checklists.

## Provenance
Seeded from a ChatGPT "AI Frontend Reliability Skill (May 2026)" draft (16 weakness areas + 2026-realities + phase workflow), reconciled with a verified currency/gap analysis (16 gaps, 9 staleness corrections, tiering options) and mapped onto the Make It Right 3-tier contract. Middle-tier decision (reactivity library) made 2026-05-26.
