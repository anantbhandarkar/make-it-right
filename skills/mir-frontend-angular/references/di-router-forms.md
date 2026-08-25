# DI, Router, and Forms

Read at Gate 5 (state and rendering ownership) and Gate 6 (implementation). This is the half of Angular that no other reactivity tier has, because no other reactivity library ships a DI container, a router, and three form systems in the same package.

Version basis: `@angular/core` 22.1.3, verified 25 Aug 2026.

---

## Part 1 — Dependency injection

### 1.1 The injection context is a runtime window, not a syntax

`inject()` only works while Angular has a current injector set. That window is open in:

- a constructor of a class Angular instantiates,
- a **field initializer** of such a class,
- a provider `useFactory`,
- inside `runInInjectionContext(injector, fn)`,
- and inside the APIs Angular explicitly documents as running in one — functional Router guards and resolvers, `effect()`, and the `resource` loader.

Anywhere else — a `setTimeout` callback, a `.subscribe()` body, a `then()`, a lifecycle hook such as `ngOnInit`, a plain method — `inject()` throws **`NG0203`**.

```ts
// BAD — NG0203 at runtime; the DI window closed when the constructor returned
ngOnInit() { const http = inject(HttpClient) }

// GOOD — field initializer
private readonly http = inject(HttpClient)

// GOOD — deliberately re-entering the context later
private readonly injector = inject(Injector)
onLater() { runInInjectionContext(this.injector, () => { const x = inject(Thing) }) }
```

`runInInjectionContext` is a real escape hatch, not a workaround to sprinkle. If you need it, the usual cause is that a factory ran too late; capture the `Injector` once and say in the design why.

**The async trap:** anything after an `await` is no longer in the injection context that started the function, even if the first line was. Resolve every dependency before the first `await`.

### 1.2 Provider scope decides lifetime, and lifetime decides bugs

| Where the provider is declared | One instance per | Destroyed when |
|---|---|---|
| `providedIn: 'root'` | application | the app is destroyed (i.e. never, in practice) |
| `providers: []` in `bootstrapApplication` / `ApplicationConfig` | application | same |
| `providers: []` on a **component** | component *instance* | that component is destroyed |
| `providers: []` on a lazy route | that route's injector | the route is torn down |
| `viewProviders: []` on a component | component instance, view children only | that component is destroyed |

Two failure shapes:

- **A root service holding per-user or per-form state.** It outlives every component, so the next user of the same tab sees the last one's data. On an SSR path the equivalent mistake is worse — see `ssr-hydration-and-build.md` §"cross-request state".
- **A component-level provider expected to be shared.** Put a `providers: [CartService]` on a component that renders three times and you have three carts. If the state is shared, the provider goes up; if it is per-instance, keep it down and say so in the Gate 5 ownership table.

`providedIn: 'root'` services are tree-shaken when unreferenced, which is why it is the default advice — but "cheap when unused" is not an argument for "correct when used."

### 1.3 Subscription teardown — `DestroyRef` and `takeUntilDestroyed`

Angular does not unsubscribe for you. The old `ngOnDestroy` + `Subject` + `takeUntil` boilerplate is superseded:

```ts
// GOOD — injection context: the DestroyRef is found implicitly
private readonly svc = inject(DataService)
constructor() {
  this.svc.stream$.pipe(takeUntilDestroyed()).subscribe(v => this.value.set(v))
}

// GOOD — outside an injection context: pass the DestroyRef explicitly
private readonly destroyRef = inject(DestroyRef)
start() { this.poll$.pipe(takeUntilDestroyed(this.destroyRef)).subscribe(...) }

// GOOD — non-RxJS teardown (a listener, a timer, a WebSocket)
constructor() {
  const id = setInterval(fn, 1000)
  inject(DestroyRef).onDestroy(() => clearInterval(id))
}
```

Rules:
- `takeUntilDestroyed()` with no argument **must** be called in an injection context; otherwise `NG0203`.
- Put it **last** in the pipe. An operator after it can still resubscribe.
- `toSignal()` already handles its own teardown. Do not add `takeUntilDestroyed` to it.
- `HttpClient` requests complete on their own, so they do not leak *memory* — but a late response still writing component state after destroy is a real bug class, and `takeUntilDestroyed` fixes it.
- A leak that only shows under route churn will not appear in a unit test. It appears as a browser tab that gets slower over an afternoon.

### 1.4 Standalone is the shape of new code

Since v19, `standalone: true` is the default for components, directives and pipes — you write `standalone: false` to opt a class *into* an `NgModule` declaration, not the other way round. New applications bootstrap with `bootstrapApplication(App, appConfig)` and an `ApplicationConfig` of `providers`, not `platformBrowserDynamic().bootstrapModule(AppModule)`.

NgModules are not removed and mixed codebases are supported, but a model that emits `@NgModule({declarations, imports, providers})` for a new feature is generating a shape the CLI has not scaffolded in three majors. Signals of that staleness in generated code: `declarations`, `entryComponents`, `CUSTOM_ELEMENTS_SCHEMA` used to silence a missing import, `forRoot()`/`forChild()` on your own modules, and `SharedModule`.

The replacement for `SharedModule` is: import the specific standalone components a template needs, in that component's `imports` array. A barrel of everything defeats tree-shaking.

---

## Part 2 — Router

### 2.1 Functional guards, and what a guard is *for*

Write guards as functions typed `CanActivateFn` / `CanMatchFn` / `CanDeactivateFn` / `ResolveFn`. They run in an injection context, so `inject()` works directly, and they compose without a class per rule.

```ts
export const authGuard: CanActivateFn = (route, state) => {
  const auth = inject(AuthService), router = inject(Router)
  return auth.isLoggedIn() || router.createUrlTree(['/login'], { queryParams: { r: state.url } })
}
```

- Return `true`, `false`, or a `UrlTree` (the redirect). Returning `false` with no redirect leaves the user on the current URL with nothing rendered and no explanation — a real, shipped UX bug.
- **`CanMatch` vs `CanActivate`:** `CanMatch` decides whether the route matches at all, so the router can fall through to a later route and the lazy chunk is never loaded. `CanActivate` runs after the match and after the chunk is fetched. For "hide this feature from this user," `CanMatch` is the right one — but see the next point before calling either of them a security control.

### 2.2 A guard is a UX gate, never authorization

This is the single most dangerous Angular-shaped misconception, because the guard *looks* like a server-side middleware and is written in the same vocabulary.

- A guard runs in the browser. The user controls the browser. `auth.isLoggedIn()` reads a value the user can set.
- `CanMatch` prevents the lazy chunk from being *requested by the router*. It is a plain URL in `.../chunk-XXXX.js`; anyone can `fetch` it. Route configuration is not a secret.
- Every API call the guarded feature would have made is still callable directly. **The server authorizes each object on each request.** Write that sentence into the Assumption Ledger at Gate 2 so nobody later reads the guard as a control.
- The same applies to `*ngIf`/`@if` on a role, a disabled button, and a hidden menu item.

Angular 22 changed two Router defaults that quietly alter behaviour on upgrade: `paramsInheritanceStrategy` now defaults to `'always'` (child routes inherit parent params and data even without a path-less segment), and `CanMatchFn`'s `currentSnapshot` parameter is now required. `provideRoutes()` was removed — use `provideRouter(routes)`.

### 2.3 Route-level data loading

`ResolveFn` blocks the navigation until it settles: nothing renders, and a slow resolver looks like a frozen app. Use it only when the route genuinely cannot render without the data, and pair it with a global navigation-progress indicator. The alternative — render the shell immediately and let a `resource()` inside the component own LOADING/EMPTY/ERROR — is usually the better UX and maps directly onto the Gate 3 state machine.

Also decide, at Gate 5: `withComponentInputBinding()` (route params delivered as component `input()`s, which is what you want for signal-based components), scroll restoration (`withInMemoryScrolling`), and preloading strategy for lazy routes.

---

## Part 3 — Forms

### 3.1 Three systems, and the choice is a Gate 5 decision

| | Template-driven (`FormsModule`) | Reactive (`ReactiveFormsModule`) | **Signal Forms** (`@angular/forms/signals`) |
|---|---|---|---|
| Status | supported, legacy shape | supported, not deprecated | **stable since v22.0** |
| Source of truth | the template | the `FormGroup` tree | a `WritableSignal` model you own |
| Validation | directives on the element | validator functions on controls | rules in a **schema** function |
| Typing | weak | typed forms since v14 | inferred from the model type |
| Good for | a two-field filter | anything with dynamic structure on v20/v21 | new work on v22 |

Angular has **not** deprecated Reactive Forms, and the migration guide is explicit that the two coexist: `compatForm` lifts existing `FormControl`s into a signal form (top-down) and `SignalFormControl` exposes a signal form inside an existing `FormGroup` (bottom-up). Migrate a screen at a time; do not rewrite a working form because a newer API exists.

Do not mix template-driven and reactive bindings on the same control — `[(ngModel)]` together with `formControlName` is a documented error and, where it does not error, produces two writers for one value.

### 3.2 Signal Forms shape

```ts
model = signal({ email: '', password: '' })
loginForm = form(this.model, (path) => {
  required(path.email, { message: 'Email is required' })
  email(path.email, { message: 'Enter a valid email address' })
  required(path.password)
})
// read state by calling the node: this.loginForm().valid(), this.loginForm.email().errors()
```

- `form(model, schema)` **does not copy** the model — it reads and writes your signal. That is the point, and it means anything else holding that signal sees edits live. If you need a cancellable draft, clone into a draft signal and commit on submit.
- Rules live in the schema: `required`, `email`, `min`/`max`, `validate`, plus behavioural rules `disabled`, `hidden`, `readonly`, `debounce`, `metadata`, each accepting reactive logic (`when`) so cross-field rules re-evaluate automatically.
- `validateStandardSchema` accepts any Standard Schema validator (Zod, Valibot) — reuse the server's schema instead of writing the rules twice.
- Client validation is UX. **The server revalidates.** Every rule in the schema exists again on the endpoint or it is not enforced.

### 3.3 Custom controls and identity

For Reactive/Template-driven forms, a custom input implements `ControlValueAccessor` and registers with `NG_VALUE_ACCESSOR` using `useExisting: forwardRef(() => MyInput)`. Two failures dominate:

- **`useClass` instead of `useExisting`.** DI constructs a *second* instance; the form writes to one object and the DOM shows the other. Nothing errors.
- **A missing `multi: true`** on the `NG_VALUE_ACCESSOR` provider silently replaces the whole accessor list.

`registerOnChange`/`registerOnTouched` must actually be called — a control that never calls `onTouched` never becomes `touched`, so `ng-touched`-gated error messages never appear and the field looks valid while it is not.

For Signal Forms the equivalent contract is `FormValueControl` / `FormCheckboxControl` with the `FormField` directive; the control receives `invalid()` and `errors()` signals and **does not validate**. The docs are explicit: *do not* implement both `ControlValueAccessor` and `FormValueControl` on the same component — pick one.
