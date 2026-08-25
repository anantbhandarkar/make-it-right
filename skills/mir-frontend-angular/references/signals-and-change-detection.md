# Signals and change detection

Read at Gate 3 (UI state machine) and Gate 6 (implementation). Everything here is true in both zoned and zoneless apps unless the text says otherwise.

Version basis: `@angular/core` 22.1.3, verified 25 Aug 2026.

---

## 1. The signal graph is pull-based, and that is the whole model

A `signal()` holds a value. A `computed()` derives one. Neither pushes: reading a `computed` is what makes it recompute, and only if a producer it read last time has changed. Nothing runs because a value changed; things run because something *read* them afterwards.

Three consequences AI-generated code violates constantly:

- **A `computed` that nobody reads never runs.** Putting a side effect in it — a `fetch`, a `console.log` you rely on, a write to another signal — means the effect happens at an unpredictable time or never. The getter is memoized.
- **A `computed` must be pure and must not write.** Writing a signal inside a `computed` is a bug in every mode. Angular does not always throw; you get a value that depends on read order.
- **Dependencies are collected dynamically, per execution.** A branch that was not taken did not register its signals. `cond() ? a() : b()` depends on `a` or `b`, never both — which is usually what you want and occasionally the reason a recompute you expected does not happen.

```ts
// BAD — side effect in a derivation; runs on an unspecified schedule
readonly total = computed(() => { this.log(this.items()); return sum(this.items()) })

// GOOD — pure derivation
readonly total = computed(() => sum(this.items()))
```

## 2. `linkedSignal` — the writable derived value (stable since v20.0)

The recurring shape is "a selection that resets when the list changes, but the user can override it in between." A `computed` cannot be written. A `signal` plus an `effect` that resets it is the classic derived-state-in-state bug: two sources of truth and one frame of the wrong one.

```ts
// BAD — mirror + effect reset; flickers, and the effect can loop
selected = signal<Item | null>(null)
constructor() { effect(() => { this.items(); this.selected.set(null) }) }

// GOOD — writable, but re-derived when the source changes
selected = linkedSignal({
  source: this.items,
  computation: (items, prev) => items.find(i => i.id === prev?.value?.id) ?? items[0] ?? null,
})
```

The advanced form's `computation(source, previous)` is the only supported way to keep part of the old selection across a reset. Do not reach for `untracked()` inside a `computed` to fake it.

## 3. `effect()` is for synchronizing with things outside the signal graph

`effect()` exists for the same reason React's `useEffect` does, and it is misused for the same reason: it looks like a general "run this when data changes" hook. It is not. Anything you can derive, derive.

- **Effects are for external systems** — a canvas, a third-party widget, `localStorage`, an analytics call, a `matchMedia` listener.
- **An effect that writes a signal that the effect also reads is a loop.** Angular detects some of these and throws; others just re-run forever and pin a core.
- **Effects run inside change detection, before rendering.** They are the wrong place to measure the DOM. `afterRenderEffect()` (stable) exists for that and has four ordered phases — `earlyRead`, `write`, `mixedReadWrite`, `read`. Prefer `read` and `write`; `mixedReadWrite` forfeits the batching that avoids layout thrash.
- **`afterNextRender()`** is the one-shot version, and it only runs in the browser — it is the correct place for DOM/`window` access in an SSR app.
- An effect created in an injection context is destroyed with that context. One created via `Injector.get(EffectRef)` machinery outside one needs a manual `destroy()`. If you cannot name the thing that destroys your effect, you have written a leak.

```ts
// BAD — derived state via effect; extra render pass, and a loop risk
effect(() => this.filtered.set(this.items().filter(this.fn())))

// GOOD — derive
readonly filtered = computed(() => this.items().filter(this.fn()))

// GOOD — effect for a genuinely external system, with teardown
effect((onCleanup) => {
  const chart = renderChart(el, this.data())
  onCleanup(() => chart.destroy())
})
```

## 4. Writing a signal during render — the modern `NG0100`

The classic `ExpressionChangedAfterItHasBeenChecked` (`NG0100`) came from a template reading a value that a parent had already checked. Signals give you a new way to produce the same class of bug: setting a signal from inside a template expression, a `computed`, or a getter that the template calls. Change detection then has to run again to converge, and in the pathological case it does not converge at all.

Rules:
- Never call `.set()` / `.update()` from a template expression, a `computed`, or a pipe's `transform`.
- Set signals from event handlers, from `effect()` (carefully), and from async callbacks.
- If you need "reset when input changes," that is `linkedSignal`, not an effect and not a setter in a getter.

## 5. `OnPush` is the default in v22 — and what "dirty" means depends on the mode

Angular 22 changed the default `changeDetection` for a component with no explicit setting from `Default` to **`OnPush`**. `ChangeDetectionStrategy.Default` is deprecated in favour of the new `ChangeDetectionStrategy.Eager`, which is what you now write to get the old always-check behaviour.

Under `OnPush`, a component is checked only when it is marked dirty. What marks it dirty:

| Trigger | Zoned (`provideZoneChangeDetection`) | Zoneless (default v21+) |
|---|---|---|
| A signal read in the template changes | marks dirty | marks dirty |
| A template event binding fires (`(click)`) | marks dirty | marks dirty |
| An `@Input()` / signal `input()` reference changes | marks dirty | marks dirty |
| `AsyncPipe` emits | marks dirty | marks dirty |
| `markForCheck()` called explicitly | marks dirty | marks dirty |
| A bare `setTimeout` / `setInterval` mutating a plain field | schedules a CD pass; an `OnPush` component still is not dirty, but an `Eager` ancestor is checked and often hides the bug | **nothing happens at all** |
| An RxJS subscription writing a plain field | same as above | **nothing happens at all** |
| A third-party callback (WebSocket, IntersectionObserver, chart lib) | same as above | **nothing happens at all** |

**This row is the migration hazard.** Code written against zoned Angular that mutates a plain class field from an async callback appears to work because zone.js scheduled a global pass and an `Eager` ancestor dragged the view along. Remove zone.js and the UI silently stops updating — no error, no warning. The fix is not `markForCheck()` sprinkled at each call site; it is to make the state a signal.

```ts
// BAD — plain field mutated from outside Angular; renders nothing when zoneless
socket.onmessage = (e) => { this.rows.push(JSON.parse(e.data)) }

// GOOD — signal write; the graph notifies change detection in both modes
rows = signal<Row[]>([])
socket.onmessage = (e) => this.rows.update(r => [...r, JSON.parse(e.data)])
```

Note the `[...r, x]` — a signal holding an array compares by reference by default. `r.push(x); return r` returns the same reference and notifies nobody.

## 6. `ChangeDetectorRef.detectChanges()` is a smell, and `checkNoChanges()` is gone

`detectChanges()` synchronously checks this view and its children, outside Angular's scheduling. Reaching for it almost always means state lives somewhere change detection cannot see — which is the bug. Fix the state, not the schedule.

- `detectChanges()` in a loop or in a resize/scroll handler is a performance incident waiting to happen.
- `ApplicationRef.tick()` from application code is the same smell one level up.
- `ChangeDetectorRef.checkNoChanges()` was **removed in v22**. In tests use `fixture.detectChanges()`.
- `detach()` / `reattach()` for a high-frequency subtree is legitimate and rare. Document why in the Gate 5 design.

The two defensible uses: driving a third-party library that renders synchronously and demands committed DOM, and a test. Everything else is a signal you did not write.

## 7. Zoneless specifics

`provideZonelessChangeDetection()` is **stable since v20.2** and is the **default for new applications from v21** — there is no provider to add, and `zone.js` is no longer in `polyfills`. Ensure `provideZoneChangeDetection()` is not present, or you have silently opted back in.

- Change detection is scheduled, not synchronous. After a signal write the DOM is not updated yet; `await new Promise(r => setTimeout(r))` or, in tests, `await fixture.whenStable()`.
- `fakeAsync`/`tick()` depend on zone.js patching timers. A zoneless app that still wants them keeps `zone.js` as a **test-only** dependency; do not conclude from a passing `fakeAsync` test that the production app is zoned.
- Configure `TestBed` with `provideZonelessChangeDetection()` so tests fail the same way production does. A test suite still running zoned is the single most common reason a zoneless regression ships.
- `NgZone.onStable`, `NgZone.run()`, and `runOutsideAngular()` still exist but mean much less. Code that waits on `onStable` to know rendering finished should use `afterNextRender()`.

## 8. `resource()` and `httpResource()` — async as part of the graph (stable since v22.0)

`resource({params, loader})` turns a reactive params signal into an async load, exposing `value()`, `status()`, `error()`, `isLoading()`, plus `reload()` and `set`/`update`. `httpResource(url)` is the `HttpClient`-backed form (interceptors and `HttpTestingController` apply); `httpResource.text()`, `.blob()`, `.arrayBuffer()` are the non-JSON variants.

The rules that matter:
- **Read only.** The docs are explicit: a resource is for reads, not mutations. It aborts the in-flight request via `AbortSignal` when params change or the component is destroyed — which would abort your POST halfway.
- A stale response cannot overwrite a fresh one, because the abort is the cancellation. This is the Gate 4 race-condition mitigation for read paths; state it as such.
- `status()` is the state machine. Map it onto the Gate 3 states rather than deriving a private `loading` boolean beside it.
- It has no cross-component cache and no invalidation protocol. If you need shared server-state caching, say so at Gate 5 and pick a library; do not grow one out of a service full of `BehaviorSubject`s.

## 9. `toSignal` / `toObservable` — the RxJS boundary

Angular did not delete RxJS; `HttpClient`, the Router, and most of the ecosystem still emit Observables. `@angular/core/rxjs-interop` is the seam, and the seam is where the leaks are.

- **`toSignal(obs$)` subscribes immediately and unsubscribes on destroy** — it must be called in an injection context (field initializer or constructor), or you must pass `{injector}`. This is the correct default and it is why `| async` in the template is no longer the only leak-free option.
- Without `initialValue` (and without `requireSync: true`), the signal's type includes `undefined`. Code that ignores this renders a flash of "undefined" or crashes on `.length`. `requireSync: true` throws if the source is not synchronous — use it for `BehaviorSubject`-backed sources and nothing else.
- `toSignal` swallows nothing: an error on the source is rethrown on read. Handle it in the pipeline (`catchError`) and represent the failure as a value, or your ERROR state never renders.
- **`toObservable(sig)` emits on an effect**, so it is *not* synchronous and it does not replay every intermediate value — signals are not a stream and glitch-free evaluation means intermediate states are skipped. Never use `toObservable` to build an audit trail of every write.
- Round-tripping (`toObservable` → operators → `toSignal`) for something that is a pure derivation is a `computed` written expensively.
- Manual `.subscribe()` still needs teardown: `takeUntilDestroyed()` (injection context, or pass a `DestroyRef`). See `di-router-forms.md`.

## 10. Signal inputs, outputs, queries, and `model()`

Prefer the signal-based authoring APIs; they participate in the graph, so an `@Input()` setter that copies into a field is no longer needed and is a derived-state bug when written.

| Old | New | Note |
|---|---|---|
| `@Input() x` | `x = input<T>()` / `input.required<T>()` | read as `x()`; `transform` option replaces the setter |
| `@Output() y = new EventEmitter()` | `y = output<T>()` | not an Observable; no `.pipe()` |
| `@ViewChild` / `@ContentChild` | `viewChild()` / `contentChild()` (+ `.required`) | resolve as signals; readable in `computed`/`effect` |
| two-way `@Input`+`@Output` pair | `model<T>()` | writable input; `[(x)]` binds it |

`input()` is read-only from inside the component. Writing to a signal derived from an input, then expecting the parent to see it, is the same mistake as mutating props.

## 11. Template control flow and `@defer`

`@if` / `@for` / `@switch` are built in; `*ngIf`, `*ngFor`, `*ngSwitch` and `NgIf`/`NgForOf` imports are legacy output from a stale model. `@for` **requires** `track`. `track $index` on a reorderable list attaches row state (focus, input value, animation) to the wrong row — exactly the React index-key failure. `track item.id`.

`@defer` blocks lazily load their dependencies on a trigger (`on idle`, `on viewport`, `on interaction`, `on hover`, `on timer`, `when`). Two traps: the deferred component must not be eagerly imported anywhere else in the same chunk graph, or nothing is deferred; and `@placeholder` / `@loading` / `@error` are the block's own state machine — an unhandled `@error` is a blank region. `@defer (hydrate on ...)` is a different feature — see `ssr-hydration-and-build.md`.
