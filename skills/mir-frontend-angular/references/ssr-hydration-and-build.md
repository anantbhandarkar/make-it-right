# SSR, hydration, and the build

Read at Gate 0 (render-model fitness), Gate 5 (rendering ownership) and Gate 7 (review). Angular has no meta-framework layer, so server rendering, hydration and the toolchain are this tier's material.

Version basis: `@angular/ssr`, `@angular/cli`, `@angular/build` 22.1.5, verified 25 Aug 2026.

---

## 1. What `@angular/ssr` actually gives you

`ng add @angular/ssr` (or `ng new --ssr`) wires the `application` builder to emit a client bundle, a Node server entry, and build-time prerendered routes. The three modes are per-route, not per-app: **prerender (SSG)**, **server (SSR)**, and **client** — declared in a server routes config. Getting this table wrong is the most common Gate 0 mistake: a personalised dashboard prerendered at build time serves one user's shell to everyone, and a public marketing page rendered per request pays for nothing.

Decide at Gate 0 which routes are which, and write it in the design. "SSR is on" is not a rendering-ownership statement.

## 2. Cross-request state — the server bug with no client analogue

A Node server process is shared by every request. Anything Angular does not scope per request is shared by every user.

- **Module-scope mutable state is a cross-user leak.** A `let cache = {}` at the top of a service file, a singleton client instance holding an auth header, a module-level array — one request writes, the next reads. This is the same failure `mir-frontend` Gate 0 names for any SSR path; on Angular it is easy to reach because `providedIn: 'root'` reads like "per app" and on the server "app" means "process."
- **`window`, `document`, `localStorage`, `navigator` do not exist.** Touching them at construction time crashes the render. Guard with `afterNextRender()` (browser only, runs once after the first render) or `isPlatformBrowser(inject(PLATFORM_ID))`. Do not guard with `typeof window !== 'undefined'` scattered through a component — that is how half a component ends up server-safe.
- Angular has shipped a real advisory in exactly this class: **CVE-2025-59052 / GHSA-68x2-mx4q-78m7**, a global platform-injector race in `@angular/platform-server` and `@angular/ssr` leading to cross-request data leakage. Patched in 20.3.0 / 19.2.16 / 18.2.21. A concurrency bug in the framework's own injector is the strongest possible argument for not adding your own.

```ts
// BAD — runs on the server, crashes the render
theme = localStorage.getItem('theme')

// GOOD
constructor() { afterNextRender(() => this.theme.set(localStorage.getItem('theme'))) }
```

## 3. Hydration and its mismatch classes

`provideClientHydration()` in the app config makes the client reuse the server-rendered DOM instead of destroying and re-creating it. Without it you get a visible flash and a wasted render. With it, the server and client trees must agree.

Mismatch causes, in the order they actually occur:

| Cause | What you see | Fix |
|---|---|---|
| Invalid HTML nesting (`<div>` inside `<p>`, a `<tr>` outside `<tbody>`) | the browser's parser repairs the markup, so the DOM no longer matches what Angular emitted | fix the markup; this is the #1 cause |
| Direct DOM manipulation in the component (a raw markup setter, a jQuery plugin, a chart lib writing during construction) | nodes Angular does not know about | move it into `afterNextRender()` |
| Non-deterministic render values — `Date.now()`, `Math.random()`, `crypto.randomUUID()`, locale/timezone-dependent formatting | text differs between server and client | compute once on the server and pass it down, or render it after hydration |
| Browser-only branches (`isPlatformBrowser` used *in the template*) | different tree on each side | render the same tree; change it after hydration |

`ngSkipHydration` on an element opts its subtree out entirely — the client destroys and re-renders it. It is the correct answer for a genuinely uncontrolled third-party widget and the wrong answer for "the warning went away." Every use gets a comment naming the widget.

**Incremental hydration is on by default in v22** when `provideClientHydration()` is present; `withNoIncrementalHydration()` turns it off. It lets a `@defer` block stay dehydrated until a trigger fires:

```html
@defer (hydrate on viewport) { <heavy-chart [data]="data()" /> } @placeholder { <chart-skeleton /> }
```

Triggers: `hydrate on idle | viewport | interaction | hover | timer(ms) | immediate`, `hydrate when <expr>`, and `hydrate never`. Constraints worth knowing before you design around it:
- It **depends on and automatically enables event replay** — clicks made before hydration are queued and replayed. That changes what "the button did nothing" means during debugging.
- **A parent `@defer` must hydrate before its children.** A `hydrate never` parent freezes everything below it.
- `@placeholder` is still required, because after a client-side navigation the block renders as a normal `@defer` with no server HTML to reuse.

## 4. `TransferState`, `HttpTransferCache`, and the double fetch

The default failure without transfer state: the server fetches `/api/x` to render, the client hydrates and fetches `/api/x` again. The user pays for two round trips and sees a flash when the second one differs.

`provideClientHydration(withHttpTransferCache(...))` serialises server-side `HttpClient` **GET/HEAD** responses into the HTML and replays them from memory on the client, once each. `TransferState` (`makeStateKey` / `set` / `get`) is the manual form for anything not going through `HttpClient`.

Everything in the transfer cache is **embedded in the HTML document, in plain text, readable by anyone who receives that page.** Three consequences, all of which Angular has issued advisories for:

- **Credentialed requests were cached by default** — `CVE-2026-50170` / `GHSA-q6f4-qqrg-jv6x` (HIGH), fixed in 22.0.0-rc.2 / 21.2.15 / 20.3.22 / 19.2.23. One authenticated user's response could be embedded and served to another. Set `includeRequestsWithAuthHeaders` deliberately, and only when the page is per-user and uncacheable.
- **Cache keys were weak.** `CVE-2026-54266` / `GHSA-39pv-4j6c-2g6v` (HIGH) — a 32-bit hash producing collisions and cross-request data leakage; fixed in 22.0.1 / 21.2.17 / 20.3.25. `CVE-2026-68945` / `GHSA-jhpw-976m-542j` (HIGH) — key ambiguity giving cross-request response reuse and state poisoning; fixed in 22.0.2 / 21.2.19 / 20.3.27.
- **`CVE-2026-54267` / `GHSA-rgjc-h3x7-9mwg` (HIGH)** — client hydration DOM clobbering and response-cache poisoning; fixed in 22.0.1 / 21.2.17 / 20.3.25.

Rule for Gate 5: name every response that goes into the transfer cache, and confirm none of them is per-user unless the whole page is `Cache-Control: private, no-store`. If a CDN can cache the HTML, the transfer cache is CDN-cached too.

## 5. The SSR request pipeline is a server, and it has server bugs

`@angular/ssr` parses incoming request headers to build the URL it renders. Every advisory in the table below is a request-header parsing bug — treat the Angular server the way `mir-backend` treats any HTTP edge.

| Advisory | Class | Fixed in |
|---|---|---|
| `CVE-2026-27739` / `GHSA-x288-3778-4hhx` (**CRITICAL**) | SSRF + header injection via the request-handling pipeline | 21.1.5 / 20.3.17 / 19.2.21 |
| `CVE-2026-46417` / `GHSA-rfh7-fxqc-q52v` (HIGH) | SSRF via hostname hijacking (`platform-server`) | 22.0.0-next.12 / 21.2.13 / 20.3.21 / 19.2.22 |
| `CVE-2026-50168` / `GHSA-xrxm-cp7j-8xf6` (HIGH) | URL-parser differential → SSRF allow-list bypass | 22.0.0-rc.2 / 21.2.15 / 20.3.22 / 19.2.23 |
| `CVE-2026-41423` / `GHSA-45q2-gjvg-7973` (HIGH) | SSRF via protocol-relative and backslash URLs | 22.0.0-next.8 / 21.2.9 / 20.3.19 / 19.2.21 |
| `CVE-2025-62427` / `GHSA-q63q-pgmf-mxhr` (HIGH) | SSRF | 21.0.0-next.8 / 20.3.6 / 19.2.18 |
| `CVE-2026-44437` / `GHSA-69xr-m8h6-h664` | open redirect / request steering via encoded `X-Forwarded-Prefix` | 22.0.0-next.7 / 21.2.9 / 20.3.25 / 19.2.25 |
| `CVE-2026-27738` / `GHSA-xh43-g2fq-wjrj` | open redirect via `X-Forwarded-Prefix` | 21.1.5 / 20.3.17 / 19.2.21 |
| `CVE-2026-33397` / `GHSA-vfx2-hv2g-xj5f` | protocol-relative URL injection via a single-backslash bypass | 22.0.0-next.2 / 21.2.3 / 20.3.21 |
| `CVE-2026-50555` / `GHSA-hqr9-c56f-3x7f` (HIGH) | XSS — improper neutralisation during page generation | 22.0.0-rc.2 / 21.2.16 / 20.3.24 / 19.2.25 |
| `CVE-2026-50556` / `GHSA-gxx4-3xcv-f8qx` (HIGH) | XSS — missing `<noscript>` raw-text serialisation escaping | 22.0.0-rc.2 / 21.2.16 / 20.3.24 / 19.2.25 |
| `CVE-2026-69149` / `GHSA-vpx6-8pjr-4g3v` (HIGH) | XSS — missing fallback raw-content serialisation escaping | 22.0.7 / 21.2.19 / 20.3.27 |

Two patterns follow. **Do not trust forwarding headers** — `X-Forwarded-Host`, `X-Forwarded-Prefix`, `X-Forwarded-Proto` are attacker-controlled unless a proxy you own strips and rewrites them; strip them at the edge. And **an allow-list matched by string comparison is not an allow-list**: four of the entries above are URL-parser differentials, where Angular's parse and your check disagreed about the same string.

Note the fixed-version columns: the three most recent advisories have no v19 fix at all. Angular 19 left support on 2026-05-19 and Angular 2–19 are EOL. An app on v19 or below does not get these patches.

## 6. The build

**`@angular/build:application` is the builder for new projects** — esbuild for bundling, Vite for the dev server. The webpack-based `@angular-devkit/build-angular` builders emit deprecation warnings on v22 as part of Angular's webpack deprecation. Migrate with `ng update @angular/cli --name use-application-builder`; `@angular/build:browser-esbuild` is the near-drop-in intermediate step for a client-only app.

Things the migration breaks, in order of frequency: webpack-specific stylesheet syntax (`~` and `^` prefixes in `@import`/`url()`), any `@angular-builders/custom-webpack` configuration (it depends directly on the deprecated package and needs an equivalent, not a port), and CommonJS assumptions in SSR server code (`require`, `__dirname`, `__filename`).

**Budgets are the only bundle-size control the CLI gives you, and they belong in CI**, not in a code review checklist:

```jsonc
"budgets": [
  { "type": "initial",          "maximumWarning": "500kB", "maximumError": "1MB" },
  { "type": "anyComponentStyle", "maximumWarning": "4kB",  "maximumError": "8kB" }
]
```

A budget with only a `maximumWarning` does not fail a build. Set `maximumError` or you have written documentation, not a gate. Name the number in the Gate 5 performance budget.

Also check at Gate 5: `optimization` and `sourceMap` (do **not** publish source maps to a public origin — upload them to the error tracker), `outputHashing`, and whether `namedChunks` is leaking internal route names.

## 7. Tests

**Vitest is the default runner for new projects** via the `@angular/build:unit-test` builder; Karma remains supported for existing projects but is the legacy path. Migration is two separate tools: an `ng update` migration (`migrate-karma-to-vitest`) that rewrites `angular.json` targets, options and dependencies, and a `ng generate` schematic that refactors Jasmine syntax inside `*.spec.ts`. Neither is complete — the schematic explicitly does not handle complex or nested spies, does not delete `karma.conf.js` / `src/test.ts`, and does not install dependencies. You need `vitest` plus a DOM emulation library (`happy-dom` is preferred if present, otherwise `jsdom`).

Two correctness points that outrank the tooling:

- **Configure `TestBed` the way production runs.** A zoneless app tested with zone.js present is not testing the change-detection behaviour it ships. Add `provideZonelessChangeDetection()` to the `TestBed` providers and replace `fakeAsync`/`tick()` with `await fixture.whenStable()`.
- `ChangeDetectorRef.checkNoChanges()` was **removed in v22**; use `fixture.detectChanges()`.

## 8. `HttpClient` in v22

`FetchBackend` is now the **default** — `withFetch()` is deprecated and can be removed. If you relied on XHR upload progress, opt back in with `provideHttpClient(withXhr())`. `reportProgress` is deprecated in favour of `reportUploadProgress` / `reportDownloadProgress`. Interceptors are functions (`withInterceptors([fn])`); the class-based `HTTP_INTERCEPTORS` path still works and is the older shape.

One security note that belongs with the client rather than the server: **`CVE-2025-66035` / `GHSA-58c5-g7wp-6w37` (HIGH)** leaked the XSRF token to third parties via protocol-relative URLs — Angular's XSRF interceptor attached the token to a request it should have treated as cross-origin. Fixed in 21.0.1 / 20.3.14 / 19.2.16. Angular's XSRF support is a cookie-to-header echo and only defends same-origin requests; the server-side CSRF control is `mir-backend`'s.
