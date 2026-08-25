---
name: mir-frontend-angular
description: "Make It Right (Angular reactivity tier). Angular ships DI, Router, Forms, SSR and the CLI in one box with no meta-framework layer below, so this tier carries both layers. Covers the signal graph (computed purity, linkedSignal, effect vs afterRenderEffect, resource()) and the toSignal RxJS boundary; zoneless change detection (default for new apps since v21) and what changes between modes: OnPush is the v22 default, and a plain field mutated from an async callback silently stops rendering; injection contexts (inject() outside one throws NG0203), provider scope, DestroyRef/takeUntilDestroyed leaks; functional Router guards as UX gates, never authorization; Signal Forms vs Reactive vs Template-driven; @angular/ssr, incremental hydration, TransferState double-fetch, the esbuild/Vite builder and Vitest; plus DomSanitizer.bypassSecurityTrust* and Angular's repeating template/i18n/SSR sanitization-bypass CVEs. Chains: mir-frontend then this. TRIGGER when the frontend reactivity library is Angular (v20-v22) — components, templates, signals, change detection, DI, Router, forms, @angular/ssr and hydration, or the CLI and angular.json. SKIP for React (mir-frontend-react), Vue (mir-frontend-vue), Svelte and Solid, plain-DOM work (mir-frontend-vanilla), the generic UX/state/a11y gates (mir-frontend), AnalogJS or any future Angular meta-framework (its own mir-frontend-angular-<framework> module), and the API this UI calls (mir-backend)."
trigger: /mir-frontend-angular
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-frontend-angular · Make It Right (Angular reactivity tier)

Middle tier. `mir-frontend` decides **what is correct** for any browser UI and owns Gates 0–7; this owns **Angular**. Load order: `mir-frontend` → `mir-frontend-angular`. This is a content tier, not a gate-runner — run the pillar's pipeline and pull from here at Gates 0, 3, 5, 6 and 7.

**Angular is the one stack with no framework module below it.** React splits into a reactivity tier plus a meta-framework module; Angular ships DI, the Router, three form systems, SSR and the CLI in the same release train, so both layers land here. That makes this body a **router into three reference files** rather than a place to inline everything:

| Read | At | For |
|---|---|---|
| `references/signals-and-change-detection.md` | Gate 3, Gate 6 | signal graph, `computed` purity, `linkedSignal`, `effect()` vs `afterRenderEffect()`, `resource()`/`httpResource`, zoned vs zoneless dirty semantics, signal inputs/outputs/queries, `@for`/`@defer`, `toSignal`/`toObservable` |
| `references/di-router-forms.md` | Gate 5, Gate 6 | injection contexts and `runInInjectionContext`, provider scope and lifetime, `DestroyRef`/`takeUntilDestroyed`, standalone shape, functional guards, Signal Forms vs Reactive vs Template-driven, control-value-accessor identity |
| `references/ssr-hydration-and-build.md` | Gate 0, Gate 5, Gate 7 | `@angular/ssr` route modes, cross-request state, hydration mismatch classes, incremental hydration, `TransferState`/`HttpTransferCache`, SSR advisories, the esbuild/Vite builder, `angular.json` budgets, Karma → Vitest |

## Version floor (checked against the npm registry and angular.dev, 25 Aug 2026)

| Package / line | Current | Notes |
|---|---|---|
| `@angular/core`, `@angular/common`, `@angular/forms` | **22.1.3** (19 Aug 2026) | v22.0.0 released 3 Jun 2026. Active support until Jun 2027, LTS until Jun 2028 |
| `@angular/cli`, `@angular/build`, `@angular/ssr` | **22.1.5** (19 Aug 2026) | the CLI packages version ahead of the framework packages; that is normal |
| v21 line | 21.2.21 — **LTS** | active support ended 3 Jun 2026; LTS ends Jun 2027 |
| v20 line | 20.3.29 — **LTS** | LTS ends 28 Nov 2026 |
| v19 and below | **EOL** | Angular 2–19 are out of support. Recent advisories ship no v19 fix at all |
| `zone.js` | 0.16.2 | not installed in a new v21+ app |

**Angular moved to a 12-month major cycle with v22** — angular.dev states plainly that "until Angular v22, Angular had a 6-month major release cycle." Support is 12 months active + 12 months LTS. Do not carry the old 6-month assumption into a migration plan.

## What a model trained on older Angular emits — and why it still compiles

This is the largest failure surface on this stack. Three-majors-stale Angular is *valid* Angular: it type-checks, it builds, and it ships.

| Stale output | Current shape | Why it survives review |
|---|---|---|
| `@NgModule({declarations, imports})`, `SharedModule`, `forRoot()` | standalone components (`standalone: true` is the default since v19); `bootstrapApplication(App, appConfig)` | NgModules are still supported, so nothing errors |
| `*ngIf` / `*ngFor` / `*ngSwitch` + `CommonModule` imports | `@if` / `@for (… ; track item.id)` / `@switch` | the legacy directives still work |
| `zone.js` assumptions — mutate a field, expect a re-render | signals; see footgun 1 | works under `provideZoneChangeDetection()`, silently renders nothing without it |
| `constructor(private http: HttpClient)` | `private readonly http = inject(HttpClient)` | both work; the constructor form breaks on `inject()` in a mixed file |
| `@Input() x` + a setter that copies into a field | `x = input<T>()`, read as `x()` | the setter form is a derived-state bug that only shows under async updates |
| class-based `CanActivate` / `Resolve` implementations | `CanActivateFn` / `ResolveFn` functional guards, which run in an injection context | class guards still resolve |
| `platformBrowserDynamic().bootstrapModule(AppModule)` | `bootstrapApplication(App, appConfig)` with a providers array | the module bootstrap path still runs |
| `new FormGroup({…})` for everything | Signal Forms on v22; Reactive Forms are *not* deprecated | Reactive Forms remain correct — this one is a preference, not a defect |
| `ChangeDetectorRef.checkNoChanges()`, `ComponentFactoryResolver`, `provideRoutes()` | removed in v22 | these *do* break the build — the one honest failure in the list |

## Zoned or zoneless — establish this first, because the rules differ

**Ask which mode the app is in before writing a line:**

```bash
grep -rn "zone.js" angular.json package.json src/polyfills.ts 2>/dev/null
grep -rn "provideZoneChangeDetection\|provideZonelessChangeDetection" src/
```

No `zone.js` and no `provideZoneChangeDetection()` on v21+ means zoneless — the default. A v21+ app that still calls `provideZoneChangeDetection()` has opted *back into* zones, usually by accident during an upgrade. State which mode you found in the Gate 5 design.

| What marks a view dirty | Zoned (`provideZoneChangeDetection()`) | Zoneless (default v21+) |
|---|---|---|
| A signal read in the template changes | yes | yes |
| A template event binding fires — `(click)`, `(input)` | yes | yes |
| An `@Input()` / `input()` reference changes | yes | yes |
| `AsyncPipe` emits, or `markForCheck()` is called | yes | yes |
| A plain field mutated in `setTimeout` / `setInterval` | a global CD pass runs; an `Eager` ancestor gets checked and usually drags the view along, hiding the bug | **nothing happens** |
| A plain field mutated in a `.subscribe()` body | same | **nothing happens** |
| A third-party callback — WebSocket, `IntersectionObserver`, a chart library | same | **nothing happens** |
| `NgZone.onStable` used as "rendering finished" | fires | meaningless — use `afterNextRender()` |
| `fakeAsync` / `tick()` in tests | works, because zone.js patches timers | needs `zone.js` as a **test-only** dependency; prefer `await fixture.whenStable()` |

Three rows of the middle column are why zoned code that "works" is not evidence of correctness: zone.js was compensating. Migrating is not deleting a polyfill — it is finding every plain field mutated from outside Angular and making it a signal.

**Test in the mode you ship.** A zoneless app whose `TestBed` still runs zoned is not testing its own change-detection behaviour, and that is the most common way a zoneless regression reaches production. Add `provideZonelessChangeDetection()` to the `TestBed` providers.

## Reading an existing Angular codebase at Gate 0

Run these before you restate intent. Each one answers a question that changes the plan, and each takes a second:

```bash
npx ng version                                          # major, CLI, and the builder in use
grep -rn "provideZoneChangeDetection\|zone.js" src/ angular.json     # mode (footgun 1)
grep -rln "@NgModule" src/                              # how much of the app is pre-standalone
grep -rn "bypassSecurityTrust" src/                     # every deliberate sanitizer bypass
grep -rn "\[innerHTML\]" src/                           # every raw-HTML sink
grep -rn "detectChanges()\|ApplicationRef" src/         # state change detection cannot see
grep -rn "\.subscribe(" src/ | grep -v takeUntilDestroyed   # candidate leaks
grep -rn "track \$index" src/                           # row-state corruption (footgun 9)
grep -n "budgets" -A6 angular.json                      # is there an error threshold, or only a warning
```

The `@NgModule` count decides whether you are writing modern Angular in a modern app or modern Angular into a legacy one — a mixed file where a constructor-injected class sits beside `inject()` is where `NG0203` shows up. The `bypassSecurityTrust` and `[innerHTML]` hits are the Gate 5 security inventory; go through them one at a time and record which argument is a compile-time constant.

## The Angular footguns AI walks into

### 1. Zoneless: a plain field mutated from an async callback renders nothing
`provideZonelessChangeDetection()` is **stable since v20.2** and is the **default for new apps from v21** — `zone.js` is not in a new app's polyfills. Change detection now runs only when something marks a view dirty: a signal read in the template changes, a template event binding fires, an input reference changes, `AsyncPipe` emits, or `markForCheck()` is called.

Under zone.js, a `setTimeout`, an RxJS subscription, or a WebSocket callback that mutated a plain class field triggered a global pass and the UI updated anyway. Remove zone.js and that stops — no error, no warning, no console message.

```ts
socket.onmessage = e => { this.rows.push(JSON.parse(e.data)) }        // BAD — renders nothing zoneless
rows = signal<Row[]>([])
socket.onmessage = e => this.rows.update(r => [...r, JSON.parse(e.data)])  // GOOD
```

The fix is signals, not `markForCheck()` at every call site. Note `[...r, x]` — a signal holding an array compares by reference; `push` returns the same reference and notifies nobody.

### 2. `OnPush` is the v22 default, and `Default` is deprecated
A component with no explicit `changeDetection` is now `OnPush`. `ChangeDetectionStrategy.Default` is deprecated in favour of `ChangeDetectionStrategy.Eager`, which is what you write to get the old always-check behaviour. Upgrading to v22 makes every previously-`Default` component `OnPush`, which is exactly where the footgun-1 bugs surface.

### 3. Derived state in a signal, kept in sync by an effect
The `useState` + `useEffect` mirror has an Angular spelling, and it has the same two failure modes: a frame of the stale value, and a loop. `computed()` for read-only derivations; `linkedSignal()` (stable v20.0) when the derived value must also be writable — "a selection that resets when the list changes."

```ts
selected = signal<Item|null>(null)                                   // BAD — mirror + reset effect
constructor() { effect(() => { this.items(); this.selected.set(null) }) }

selected = linkedSignal({ source: this.items,                        // GOOD
  computation: (items, prev) => items.find(i => i.id === prev?.value?.id) ?? items[0] ?? null })
```

### 4. `computed()` must be pure — and may never run
A `computed` is memoized and pull-based: it recomputes only when something *reads* it after a dependency changed. A `fetch`, a log you rely on, or a signal write inside the getter happens at an unpredictable time or never.

```ts
readonly total = computed(() => { this.track(this.items()); return sum(this.items()) })  // BAD
readonly total = computed(() => sum(this.items()))                                       // GOOD
```

Never write a signal from a `computed`, a template expression, or a pipe's `transform` — that is the signal-era `NG0100`: change detection has to run again to converge, and in the pathological case it does not. Dependencies are also collected **per execution**, so a branch not taken registered nothing: `cond() ? a() : b()` depends on `a` or `b`, never both.

### 5. `effect()` is for external systems; DOM reads belong in `afterRenderEffect()`
Effects synchronize the graph with things outside it — a canvas, a third-party widget, `localStorage`, analytics. Anything derivable is a `computed`. Effects run during change detection, *before* rendering, so measuring the DOM in one reads the previous frame. `afterRenderEffect()` (stable) has ordered `earlyRead`/`write`/`mixedReadWrite`/`read` phases; `afterNextRender()` is the browser-only one-shot. An effect that writes a signal it also reads is a loop.

### 6. `inject()` outside an injection context throws `NG0203`
The context is open in constructors, **field initializers**, `useFactory`, functional guards/resolvers, `effect()`, `resource` loaders, and inside `runInInjectionContext()`. It is closed in `ngOnInit`, in a `.subscribe()` body, in a `then()`, in any callback — **and after the first `await` of an async function**, even if the function started inside one.

```ts
ngOnInit() { const http = inject(HttpClient) }             // BAD — NG0203 at runtime
private readonly http = inject(HttpClient)                 // GOOD — field initializer

async load() { await this.warm(); const s = inject(Store) } // BAD — context closed at the await
async load() { const s = inject(Store); await this.warm() } // GOOD — resolve before the first await
```

`runInInjectionContext(injector, fn)` is a real escape hatch, not a workaround to sprinkle. If you need it, capture the `Injector` once and say why in the design.

### 7. Subscriptions are not torn down for you
`takeUntilDestroyed()` (no argument, injection context) or `takeUntilDestroyed(destroyRef)` replaces the `ngOnDestroy` + `Subject` + `takeUntil` boilerplate; `inject(DestroyRef).onDestroy(fn)` covers timers, listeners and sockets.

```ts
constructor() { this.svc.stream$.pipe(takeUntilDestroyed()).subscribe(v => this.value.set(v)) }

private readonly destroyRef = inject(DestroyRef)           // outside an injection context
start() { this.poll$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(…) }
```

Put it **last** in the pipe — an operator after it can resubscribe. `toSignal()` already handles its own teardown; do not add it there. `HttpClient` requests complete on their own so they do not leak memory, but a late response writing component state after destroy is still a real bug. This class never appears in a unit test; it appears as a tab that gets slower over an afternoon.

### 8. `ChangeDetectorRef.detectChanges()` is a smell
Reaching for it means state lives somewhere change detection cannot see, which *is* the bug. Fix the state. `ApplicationRef.tick()` from app code is the same smell one level up. `detach()`/`reattach()` for a genuinely high-frequency subtree is legitimate and rare — say why in the design. `checkNoChanges()` was removed in v22; in tests use `fixture.detectChanges()`.

### 9. `@for` without a stable `track` corrupts row state
`@for` requires `track`. `track $index` on a reorderable, filterable, or prepend-able list attaches focus, input values and animations to the wrong row when the data moves — the same failure as a React index key. `track item.id`. Index tracking is fine only for static, append-only lists.

### 10. A Router guard is a UX gate, never authorization
A guard runs in the browser, which the user controls. `CanMatch` stops *the router* from requesting a lazy chunk; that chunk is a plain URL anyone can `fetch`, route configuration is not a secret, and every API the feature would call is still callable directly. **The server authorizes each object on each request** — write that into the Assumption Ledger at Gate 2 so nobody later reads the guard as a control.

```ts
export const authGuard: CanActivateFn = (route, state) => {
  const auth = inject(AuthService), router = inject(Router)
  return auth.isLoggedIn() || router.createUrlTree(['/login'], { queryParams: { r: state.url } })
}
```

Returning bare `false` with no `UrlTree` leaves the user on the current URL with nothing rendered and no explanation — a real, shipped UX bug. Two v22 Router defaults changed quietly on upgrade: `paramsInheritanceStrategy` now defaults to `'always'`, and `CanMatchFn`'s `currentSnapshot` parameter is required. `provideRoutes()` was removed; use `provideRouter(routes)`.

### 11. Server rendering has no client analogue for shared state
On the server the process is shared by every request. Module-scope mutable state, a singleton holding an auth header, or `providedIn: 'root'` state that is really per-user is one user's data served to the next. `window`/`document`/`localStorage` do not exist — guard with `afterNextRender()` or `isPlatformBrowser(inject(PLATFORM_ID))`, not with `typeof window` checks scattered through a component.

### 12. Three form systems, and picking one is a design decision
Signal Forms (`@angular/forms/signals`, `form()` **stable since v22.0**) uses your `WritableSignal` model as the source of truth and puts validators in a schema. Reactive Forms are **not deprecated** and coexist via `compatForm` / `SignalFormControl`. Never mix `[(ngModel)]` and `formControlName` on one control. For custom controls, `NG_VALUE_ACCESSOR` needs `useExisting: forwardRef(...)` and `multi: true` — `useClass` builds a second instance and the form writes to the one the DOM is not showing, silently. Do not implement both `ControlValueAccessor` and `FormValueControl` on the same component.

### 13. `toSignal` / `toObservable` — the RxJS boundary is where the leaks are
Angular did not delete RxJS: `HttpClient`, the Router and most of the ecosystem still emit Observables. `@angular/core/rxjs-interop` is the seam.

- `toSignal(obs$)` subscribes immediately and unsubscribes on destroy — it must be called in an injection context, or take `{injector}`.
- Without `initialValue` (and without `requireSync: true`) the signal's type includes `undefined`. Code that ignores that renders "undefined" or throws on `.length`.
- `toSignal` **rethrows a source error on read**. Handle it with `catchError` and turn the failure into a value, or your ERROR state never renders.
- `toObservable(sig)` emits from an effect, so it is not synchronous and **does not replay intermediate values** — signals are glitch-free, so skipped states are skipped for good. Never use it to build an audit trail of writes.
- Round-tripping `toObservable` → operators → `toSignal` for what is a pure derivation is a `computed` written expensively.

### 14. `resource()` is for reads, and its abort is the race fix
`resource({params, loader})` and `httpResource(url)` are **stable since v22.0**: a reactive params signal drives an async load exposing `value()`, `status()`, `error()`, `isLoading()`, `reload()`.

- **Reads only.** It aborts the in-flight request via `AbortSignal` when params change or the component is destroyed — which would abort a mutation halfway. Mutations stay on `HttpClient`.
- That abort *is* the stale-response-overwrites-fresh-response mitigation. Name it as such in the Gate 4 risk register instead of hand-rolling a sequence counter.
- Derive the UI from `status()`. A private `loading` boolean beside it is a second source of truth that will disagree.
- There is no cross-component cache and no invalidation protocol. If you need shared server-state caching, decide that at Gate 5 — do not grow one out of a service full of `BehaviorSubject`s.

### 15. Provider scope is lifetime, and lifetime is the bug
`providedIn: 'root'` is one instance per **application**, which on a server means one per **process**. A component's `providers: []` is one instance per component *instance* — render that component three times and you have three of them.

- Per-user or per-form state in a root service outlives every component; the next user of the tab sees the last one's data.
- A `providers: [CartService]` placed on a repeated component when the cart was meant to be shared gives each row its own cart, and nothing errors.
- Name the owner **and what destroys it** for every piece of state in the Gate 5 table. "Where is it provided" is the question; "how long does it live" is the answer that matters.

### 16. Hydration requires the server and client trees to agree
`provideClientHydration()` makes the client reuse server-rendered DOM. The mismatches, in the order they occur: invalid HTML nesting (the browser's parser repairs it, so the DOM no longer matches what Angular emitted — this is #1 by a distance); direct DOM manipulation during construction; non-deterministic render values (`Date.now()`, `Math.random()`, locale- or timezone-dependent formatting); and browser-only branches taken *in the template*.

`ngSkipHydration` opts a subtree out entirely. It is right for an uncontrolled third-party widget and wrong for "the warning went away" — every use gets a comment naming the widget. Incremental hydration is **on by default in v22** with `provideClientHydration()`; `@defer (hydrate on viewport)` and friends keep a block dehydrated until a trigger fires, it auto-enables event replay, and a parent must hydrate before its children.

### 17. Use the signal-based authoring APIs, or you re-introduce derived state
The decorator APIs still work, which is why a stale model keeps emitting them. The signal forms participate in the graph, so the `@Input()` setter that copies into a field — the classic Angular derived-state bug — is no longer needed and is wrong when written.

| Old | New | Note |
|---|---|---|
| `@Input() x` | `x = input<T>()`, `input.required<T>()` | read as `x()`; the `transform` option replaces the setter |
| `@Output() y = new EventEmitter()` | `y = output<T>()` | not an Observable — no `.pipe()` |
| `@ViewChild` / `@ContentChild` | `viewChild()` / `contentChild()` (+ `.required`) | resolve as signals, readable inside `computed`/`effect` |
| an `@Input`/`@Output` pair for two-way | `model<T>()` | writable input; `[(x)]` binds it |

`input()` is read-only inside the component. Writing to a signal derived from an input and expecting the parent to see it is the same mistake as mutating props.

### 18. `@defer` has its own state machine, and its own way to defer nothing
`@defer` lazily loads its dependencies on a trigger (`on idle | viewport | interaction | hover | timer(…)`, or `when <expr>`). Two traps:

- **If the deferred component is also eagerly imported anywhere in the same chunk graph, nothing is deferred.** The build succeeds and the bundle does not shrink. Check the built output, not the source.
- `@placeholder` / `@loading` / `@error` are the block's states. An unhandled `@error` is a blank region on a network blip — the same failure as a missing ERROR state at Gate 3, one level down.

### 19. Accessibility: Angular Aria, and what it does not give you
Angular Aria reached stable in v22 and supplies headless behaviour patterns (roving focus, typeahead, selection, expansion) for building accessible composite widgets. It is behaviour, not compliance: it does not supply names, contrast, reduced-motion handling, or a focus-management strategy after async actions. The pillar's a11y invariants at Gate 3 and the `a11y-reviewer` at Gate 7 still apply unchanged. Do not record "we use Angular Aria" as an a11y plan.

## Migrating a zoned app to zoneless — the order that works

Not a polyfill deletion. Do it in this order, and keep the app shippable at each step:

1. **Turn on `OnPush` everywhere first, while zone.js is still present.** On v22 that is already the default for new components; existing ones with explicit `ChangeDetectionStrategy.Default` are the ones to convert. Bugs surface here with zone.js still available as a safety net.
2. **Convert state to signals**, starting with anything written from a `.subscribe()`, a timer, or a third-party callback. Grep for `this.<field> =` and `this.<field>.push(` inside callbacks — those are the zoneless failures, pre-identified.
3. **Replace `NgZone.onStable` waits** with `afterNextRender()`, and `runOutsideAngular()` wrappers with plain code.
4. **Switch the tests** to `provideZonelessChangeDetection()` and replace `fakeAsync`/`tick()` with `await fixture.whenStable()`. Do this *before* step 5 — otherwise the suite cannot detect the regression the switch causes.
5. **Remove `provideZoneChangeDetection()`** and drop `zone.js` from polyfills. Angular ships schematics for the mechanical part of this; the schematic cannot find step 2 for you.

Anything still broken after step 5 is state that change detection cannot see. Fix the state.

## How this slots into the pipeline

- **Gate 0 (tier fitness):** name the Angular major, whether the app is zoned or zoneless, and which routes are prerendered / server-rendered / client-only. "Angular with SSR" is not a rendering-ownership statement. Read `references/ssr-hydration-and-build.md` §1.
- **Gate 3 (UI state machine in signals):** express the machine as **one** signal derived into the rest, not as a set of booleans. Three independent `loading` / `error` / `empty` flags admit eight combinations of which four are impossible, and AI-generated templates render all eight.

  ```ts
  readonly rows = httpResource<Row[]>(() => `/api/rows?q=${this.query()}`)
  readonly view = computed<'LOADING'|'ERROR'|'EMPTY'|'SUCCESS'|'STALE'>(() => {
    const s = this.rows.status()
    if (s === 'error')   return 'ERROR'
    if (s === 'loading') return this.rows.hasValue() ? 'STALE' : 'LOADING'
    return this.rows.value()?.length ? 'SUCCESS' : 'EMPTY'
  })
  ```
  ```html
  @switch (view()) { @case ('LOADING') {…} @case ('STALE') {…} @case ('EMPTY') {…}
                     @case ('ERROR') {…} @default {…} }
  ```

  `@switch` over a single derived state makes an unhandled state a visible gap rather than a blank screen. Add OPTIMISTIC / ROLLING_BACK / OFFLINE to the union when the flow has a mutation, and name the invalid transitions the way the pillar's Gate 3 asks.
- **Gate 5 (state and rendering ownership):** for each piece of data name the owner and its **lifetime** — root-provided service, component provider, route injector, or component-local signal — and say what destroys it. Name what is per-request on any SSR path. Name every response entering the transfer cache. Read `references/di-router-forms.md` §1.2 and `references/ssr-hydration-and-build.md` §4.
- **Gate 6 (codegen):** code against footguns 1–12. Signal-based authoring APIs (`input()`, `output()`, `model()`, `viewChild()`), built-in control flow, `inject()`, functional guards, standalone. Every subscription has a teardown; every `@defer` has `@placeholder` and `@error`.
- **Gate 7 (which reviewer checks which footgun):** the `reliability-reviewer` takes 1, 3, 5, 6, 7, 11 (change detection reachability, effect loops, injection-context errors, teardown, cross-request state); the `frontend-perf-reviewer` takes 2, 8, 9 plus `angular.json` budgets and `@defer`/hydration triggers; the `a11y-reviewer` works the pillar's checklist unchanged; the `security-reviewer` works the Security section below and specifically checks that `@angular/*` is at or above the floor, that no `bypassSecurityTrust*` call takes a runtime-variable argument, and that no authorization decision exists only in a guard or an `@if`.

## Security

Angular-layer mechanics. Framework-agnostic browser security (CSP, Trusted Types, clickjacking, npm supply chain) is `mir-frontend`'s; server-side authorization, CORS and rate limiting are `mir-backend`'s; pipeline controls are `mir-devsecops`'.

**Patch floor: `@angular/*` 22.1.3 (CLI packages 22.1.5), or 21.2.21 / 20.3.29 on the LTS lines.** Angular 19 and below are EOL and the three most recent advisories below have no v19 fix.

### The `DomSanitizer` bypass class

Angular's template compiler sanitizes bindings to security-sensitive contexts (HTML, style, URL, resource-URL) automatically. `bypassSecurityTrustHtml`, `-Style`, `-Url`, `-ResourceUrl` and `-Script` turn that off by name, and `[innerHTML]` fed a `SafeHtml` inserts markup verbatim. This is Angular's repeating real-world XSS pattern because the API reads like a fix for a warning rather than a decision to stop sanitizing.

- **`bypassSecurityTrust*` may only take a compile-time constant.** The moment its argument is a variable derived from a request, a CMS record, a URL parameter, or model output, you have written the vulnerability. `[src]` on an `<iframe>` is a *resource URL* context — the strictest one, and the usual reason people reach for `bypassSecurityTrustResourceUrl`; allow-list the host instead of trusting the string.
- **Prefer structured rendering.** If it must stay HTML, sanitize with DOMPurify — floor **3.4.13**, pinned exactly, never `IN_PLACE: true` (`mir-frontend` Security carries the full DOMPurify rule) — and only then hand the result to Angular.
- `DomSanitizer.sanitize(SecurityContext.HTML, v)` is the explicit, non-bypassing call. Use it when you need Angular's own sanitizer outside a template.
- **LLM output is untrusted input.** Streaming a model response into `[innerHTML]`, or through a markdown renderer with raw-HTML passthrough on, is a direct injection path.

### Template, attribute and i18n sanitization bypasses — a repeating class

Every row is a case where Angular's *own* sanitizer did not run on a codepath it should have. These are not developer mistakes; they are framework bugs, which is why the version floor is a control and not a preference.

| Advisory | What it does | Fixed in |
|---|---|---|
| `CVE-2026-32635` / `GHSA-g93w-mfhg-p222` (HIGH) | **i18n attribute bindings.** Adding `i18n-<attribute>` to a security-sensitive attribute — `href`, `src`, `action`, `formaction`, `background`, `cite`, `codebase`, `data`, `itemtype`, `longdesc`, `poster`, `xlink:href` — bypasses sanitization entirely. Marking a link for translation is what disables the check | 22.0.0-next.3 / 21.2.4 / 20.3.18 / 19.2.20 |
| `CVE-2026-69151` / `GHSA-jj27-h5hq-8x99` (HIGH) | i18n XSS via event-handler attributes | 22.0.1 / 21.2.19 / 20.3.27 — **no v19 fix** |
| `CVE-2026-27970` / `GHSA-prjf-86w9-mfqv` (HIGH) | i18n XSS | 21.2.0 / 21.1.6 / 20.3.17 / 19.2.19 |
| `CVE-2026-54265` / `GHSA-58w9-8g37-x9v5` | **Two-way property-binding sanitization bypass.** The compiler did not emit a sanitizer for the `TwoWayProperty` operation, so `[(innerHTML)]="v"` / `bindon-innerHTML` was unprotected while the one-way `[innerHTML]` was fine | 22.0.1 / 21.2.17 / 20.3.25 |
| `CVE-2026-50557` / `GHSA-f3m7-gqxr-g87x` | template and attribute **namespace** sanitization bypass | 22.0.0-rc.2 / 21.2.15 / 20.3.22 / 19.2.22 |
| `CVE-2026-52725` / `GHSA-692r-grfm-v8x7` | template and **dynamic component** namespace bypass | 22.0.0-rc.2 / 21.2.15 / 20.3.22 / 19.2.23 |
| `CVE-2026-22610` / `GHSA-jrmj-c5cx-3cw6` (HIGH) | unsanitized **SVG script attributes** | 21.1.0-rc.0 / 21.0.7 / 20.3.16 / 19.2.18 |
| `CVE-2025-66412` / `GHSA-v4hv-rgfq-gp49` (HIGH) | stored XSS via SVG animation, SVG URL and **MathML** attributes | 21.0.2 / 20.3.15 / 19.2.17 |

Read the pattern, not just the rows: **SVG, MathML, namespaced attributes, dynamic components, two-way bindings and i18n are the codepaths where Angular's sanitizer has repeatedly not run.** Assume a ninth. That is an argument for a CSP with `require-trusted-types-for 'script'` behind the framework's own defence, not instead of it.

### Data reaching the client, and hydration

The transfer cache is embedded in the HTML in plain text and is readable by anyone who receives the page — including a CDN. Three advisories in this area, all HIGH: `CVE-2026-50170` / `GHSA-q6f4-qqrg-jv6x` (credentialed requests cached by default; 22.0.0-rc.2 / 21.2.15 / 20.3.22 / 19.2.23), `CVE-2026-54266` / `GHSA-39pv-4j6c-2g6v` (32-bit cache-key hash → collisions and cross-request leakage; 22.0.1 / 21.2.17 / 20.3.25), and `CVE-2026-68945` / `GHSA-jhpw-976m-542j` (cache-key ambiguity → cross-request response reuse; 22.0.2 / 21.2.19 / 20.3.27). Hydration itself carries `CVE-2026-54267` / `GHSA-rgjc-h3x7-9mwg` (HIGH) — DOM clobbering plus response-cache poisoning; 22.0.1 / 21.2.17 / 20.3.25.

### The SSR server

`@angular/ssr` parses request headers to build the URL it renders, so it inherits every server-side URL bug. `CVE-2026-27739` / `GHSA-x288-3778-4hhx` is **CRITICAL** (SSRF plus header injection; 21.1.5 / 20.3.17 / 19.2.21), and `CVE-2025-59052` / `GHSA-68x2-mx4q-78m7` (HIGH) was a global platform-injector race causing cross-request data leakage (20.3.0 / 19.2.16 / 18.2.21). The full SSRF, open-redirect and serialisation-escaping table — eleven identifiers — is in `references/ssr-hydration-and-build.md` §5. Do not trust `X-Forwarded-*`; strip it at an edge you own.

### The rest

- **`CVE-2025-66035` / `GHSA-58c5-g7wp-6w37`** (HIGH) — XSRF token leaked to third parties via protocol-relative URLs in the HTTP client; 21.0.1 / 20.3.14 / 19.2.16. Angular's XSRF support is a cookie-to-header echo for same-origin requests only; the server-side CSRF control is `mir-backend`'s.
- **`CVE-2026-54268`** / `GHSA-48r7-hpm6-gfxm` and **`CVE-2026-50171`** / `GHSA-p3vc-36g9-x9gr` (both HIGH) — OOM denial of service via `formatDate` and via `digitsInfo` in number formatting. These matter specifically because SSR runs formatting on the server with attacker-influenced input. Fixed in 22.0.1 / 21.2.17 / 20.3.25 and 22.0.0-rc.2 / 21.2.15 / 20.3.22 / 19.2.23.
- **Client-side authorization is a hint.** A guard, an `@if` on a role, and a lazy chunk decide what is *drawn*, never what is *fetched*. Put that sentence in the Assumption Ledger.
- **Secrets.** Angular has no `NEXT_PUBLIC_`-style prefix: anything reachable from a client module is in the bundle, including `environment.ts` — which is a source file, not a secret store. Verify by grepping the **built output** in CI. Do not publish source maps to a public origin.
- **Supply chain.** All `@angular/*` packages must be on the same minor. Commit the lockfile, install with `npm ci`, and check the resolved version with `npm ls @angular/core` — a range in `package.json` proves nothing.

## Edit boundary (what belongs here vs. above)

1. True for React, Vue and plain DOM too (any browser UI — gates, state machine, a11y invariants, CSP)? → **up** to `mir-frontend`.
2. True for Angular generally — signals, change detection, DI, Router, Forms, `@angular/ssr`, the CLI? → **here**, and into whichever of the three reference files owns that surface.
3. A mechanic of one Angular meta-framework (AnalogJS file routing, its server routes, its Vite integration)? → a new `mir-frontend-angular-<framework>` module. None exists; do not widen this tier to absorb one.
4. A different reactivity library? → its own `mir-frontend-<lib>` tier.

Full layered edit map: `mir-frontend/SKILL.md` → "Where these instructions live."

## Provenance

Written 25 Aug 2026 against the live sources, not from memory — the failure this skill exists to prevent is a model emitting `NgModule` + `zone.js` + `*ngIf` code that still compiles three majors later.

**Verified at the source on 25 Aug 2026:** package versions and publish dates from the npm registry (`@angular/core` 22.1.3, `@angular/cli`/`@angular/build`/`@angular/ssr` 22.1.5, LTS lines 21.2.21 and 20.3.29, `zone.js` 0.16.2); the support table, the 12-month major cycle and the v2–v19 EOL statement from `angular.dev/reference/releases`; the v22 breaking changes (`OnPush` default, `ChangeDetectionStrategy.Eager`, `checkNoChanges()` / `ComponentFactoryResolver` / `provideRoutes()` removed, `FetchBackend` default, `paramsInheritanceStrategy: 'always'`) from the `angular/angular` v22.0.0 release notes; stability labels from the angular.dev API reference (`provideZonelessChangeDetection` stable v20.2 and default in v21+, `linkedSignal` v20.0, `afterRenderEffect` stable, `resource`/`httpResource` and `form()` stable v22.0); and every advisory identifier from OSV plus the GitHub Advisory Database entry itself.

**Not confirmed, do not assert:** **selectorless components** — still prototype/design work at the time of writing, with no angular.dev page or release note shipping it as an experimental API in v22. If a task involves selectorless syntax, verify current status before generating any.

**Anything not listed above is unverified here — check it before you quote it.** Version facts and advisory floors on this stack decay fast; Angular shipped eight sanitization-bypass advisories in nine months.
