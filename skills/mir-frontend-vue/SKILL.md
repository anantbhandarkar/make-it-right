---
name: mir-frontend-vue
description: "Make It Right (Vue reactivity tier). Vue 3.5 reactivity footguns shared across EVERY Vue meta-framework (Nuxt, Vite SPA, Quasar, legacy Vue CLI) — distinct from the generic frontend gates and from any one framework's mechanics. Covers: where reactivity is silently lost (destructuring a reactive object, reassigning a reactive array or object wholesale, passing a primitive property into a function), toRef/toRefs/toValue, computed purity (a side effect or fetch in a getter is a bug — the getter is cached and may never re-run), watch vs watchEffect and the pre/post/sync flush timing that decides whether you read pre-update DOM, implicit deep-watch traversal cost, cleanup via onWatcherCleanup/onUnmounted/effectScope and the post-await registration trap, provide/inject InjectionKey typing and the non-reactive snapshot trap, v-for key correctness (index keys attach row state to the wrong row) and v-if's higher priority on the same element, defineModel and the update:modelValue contract, template refs being null before mount plus useTemplateRef and defineExpose, Suspense still being experimental, KeepAlive deactivation (onUnmounted never fires) and stale state on return, shallowRef/markRaw for large and foreign objects, the Composition-vs-Options API boundary, and what <script setup> compiles to. Also carries Vue-runtime security mechanics: v-html, template injection, dynamic :is, SSR cross-request state pollution from module-scope singletons, and VITE_-prefixed secrets inlined into the client bundle. TRIGGER when the frontend reactivity library is Vue 3 — sits between mir-frontend (generic) and the framework module (e.g. mir-frontend-vue-nuxt). SKIP for React/Angular/Svelte (each gets its own mir-frontend-<lib> tier), and for meta-framework mechanics — Nuxt server islands, useAsyncData/useFetch, Nitro routes, file-based routing, Nuxt payload caching, Quasar build modes — those live in the framework module."
trigger: /mir-frontend-vue
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-frontend-vue · Make It Right (Vue reactivity tier)

Middle tier. `mir-frontend` decides **what is correct** (any reactive UI); this owns **Vue's reactivity model**, shared by every Vue meta-framework; the framework module knows the library's mechanics. Load order: `mir-frontend` → `mir-frontend-vue` → `<framework module>`.

These footguns apply equally to Nuxt, a Vite SPA, and Quasar. Framework-specific wiring (server islands, `useAsyncData`/`useFetch`, Nitro routes, file-based routing, payload caching) lives in the framework module — not here.

## Version floor (checked against the npm registry and vuejs.org, 13 Aug 2026)

| Package | Current stable | Notes |
|---|---|---|
| `vue` | **3.5.41** (5 Aug 2026) | Vue 3 is still the current major. There is no Vue 4. |
| `vue` (next minor) | `3.6.0-rc.3` (11 Aug 2026) | **RC, not stable.** Vapor Mode + the alien-signals reactivity rewrite ship here. |
| `eslint-plugin-vue` | **10.10.0** | Most footguns below have a rule in `flat/recommended`. |
| `vue-tsc` | 3.3.9 | Type-checks templates. Run it in CI; `tsc` alone does not see `.vue`. |
| `vite` | 8.2.1 | The build for every current Vue setup, Nuxt and Quasar included. |
| `nuxt` · `quasar` · `pinia` · `vue-router` | 4.5.2 · 2.24.0 · 4.0.3 · 5.2.0 | Pinia 4 and Vue Router 5 are current majors — old snippets target 2 and 4. |

**Three things stale advice still gets wrong.**

1. **Reactivity Transform (`$ref`, `$computed`, `$()`) was deprecated in 3.3 and removed in 3.4.** It is not coming back. It survives only in the third-party Vue Macros plugin. Do not generate `$ref` code, and do not tell anyone to enable `reactivityTransform` — that compiler option no longer exists.
2. **Vapor Mode is not stable.** It is opt-in per SFC (`<script setup vapor>`), needs `createVaporApp()` or `vaporInteropPlugin`, and **does not support the Options API, `getCurrentInstance()`, `v-memo`, or `$el`/`$props`/`$refs` on component template refs**. Treat it as a per-page performance choice, not a default.
3. **`Vue.set` / `this.$set` is Vue 2.** Vue 3 tracks property addition and deletion through a Proxy. Adding a key to a `reactive()` object is reactive; adding one to `shallowReactive()`, a `markRaw()` object, or a `toRaw()` result is not.

**Behaviour that changed under you:**

| Changed in | What changed | What it breaks |
|---|---|---|
| Vue 3 (from 2) | `v-if` now outranks `v-for` on the same element | Vue-2 snippets that filter inline — see footgun 8 |
| Vue 3.4 | `defineModel()` became stable | The hand-rolled `modelValue` prop + `update:modelValue` emit pair is now boilerplate |
| Vue 3.5 | Reactive props destructure on by default | Destructured props are reactive now, and `watch(destructuredProp, ...)` became a compile error |
| Vue 3.5 | `watch` returns a `WatchHandle`; `deep` accepts a number | Nothing breaks, but the depth cap is the fix for footgun 5 |
| Vue 3.6-rc | `@vue/reactivity` rewritten on alien-signals | Code that depended on the old scheduler ordering. Re-run timing-sensitive tests before adopting |

## The Vue reactivity footguns AI walks into (framework-agnostic)

### 1. `ref` vs `reactive` — and exactly where reactivity is lost

`ref()` wraps any value in a `.value` box. `reactive()` returns a Proxy and only works on objects. Tracking happens on **property access through the proxy**, so anything that steps outside the proxy drops the connection silently — no warning, no error, just a UI that stops updating.

| What you write | What happens |
|---|---|
| `const { count } = reactive({count:0})` | `count` is a plain number. Disconnected. |
| `let state = reactive({}); state = reactive({...})` | The template still holds the first proxy. Disconnected. |
| `let arr = reactive([]); arr = newArray` | Same — the reassignment replaces the local binding, not the tracked object. |
| `state.items = newArray` | Fine. Assigning to a *property* of a proxy is tracked. |
| `let c = ref(0); c = 1` | Replaces the binding with a plain number; the template still holds the old ref. Declare refs `const` and write `c.value`. Lint: `vue/no-ref-as-operand`. |
| `doThing(state.count)` | The function receives a number, not a tracked source. |

**Default to `ref()` for everything.** It survives reassignment (`r.value = newArray`), holds primitives, and is unambiguous in composable signatures. Reserve `reactive()` for an object you will only ever mutate in place.

```js
// BAD — replacing a reactive array
let rows = reactive([])
rows = await fetchRows()             // template never updates

// GOOD
const rows = ref([])
rows.value = await fetchRows()
```

Two ref-unwrapping traps: refs auto-unwrap as properties of a deep `reactive()` object but **not** inside arrays or `Map`/`Set` (`arr[0].value`), and not inside `shallowReactive()`.

### 2. `toRef` / `toRefs` / `toValue` — the connection-preserving conversions

When you must destructure or pass a single field around, convert instead of copying.

```js
const state = reactive({ count: 0, name: 'a' })
const { count } = toRefs(state)        // count is a Ref, stays connected
const nameRef = toRef(state, 'name')   // single field
const lazy = toRef(() => props.total)  // 3.3+: getter form, read-only ref
```

Composables should accept `Ref | getter | plain` and normalize with `toValue(x)` — never take `props.x` as a bare value, that argument is a one-time snapshot.

**Reactive props destructure (stable since 3.5)** is the exception: `const { count = 0 } = defineProps<{count?: number}>()` compiles every read of `count` into `props.count`, so it stays reactive in the template and in `computed`. But passing the destructured variable anywhere still loses it — `watch(count, ...)` is a compile error; write `watch(() => count, ...)`. Lint: `vue/no-setup-props-reactivity-loss`.

### 3. `computed` must be pure — a side effect in a getter is a bug

`computed()` caches on its tracked dependencies and re-evaluates lazily, only when read. A getter that mutates state, fetches, logs to analytics, or writes to `localStorage` fires an unpredictable number of times: sometimes never (nothing read it), sometimes on every unrelated re-read, sometimes during SSR on the server.

```js
// BAD — side effect + async in a getter
const user = computed(async () => {
  analytics.track('viewed')            // fires arbitrarily, or never
  return await fetchUser(id.value)     // returns a Promise, not a user
})

// GOOD — derivation only
const fullName = computed(() => `${first.value} ${last.value}`)

// GOOD — the effect belongs in a watcher
watch(id, (v) => { analytics.track('viewed', v) })
```

- A `computed` getter returning a Promise gives the template a Promise. Async data belongs in a watcher or a data-fetching library.
- Never mutate another ref inside a getter. Two-way derivation is the writable form: `computed({ get, set })`.
- Lint: `vue/no-side-effects-in-computed-properties`, `vue/no-async-in-computed-properties`.

### 4. `watch` vs `watchEffect`, and the flush timing that decides what DOM you read

`watch(source, cb)` is explicit and lazy — you name the dependencies and it fires only on change. `watchEffect(fn)` auto-tracks whatever the body reads and runs immediately. Its dependency set is re-collected on **every** run, so a branch not taken this run contributes nothing this run — the effect can under-subscribe until something else re-triggers it, and the set silently changes shape between runs. Anything read only after an `await` inside it is never tracked at all. **Prefer `watch` when the dependency list matters.**

Flush timing is the part AI gets wrong:

| `flush` | When the callback runs | Use for |
|---|---|---|
| `'pre'` (default) | Before the owner component's DOM update | Reacting to data. **The DOM is pre-update here.** |
| `'post'` | After Vue patched the DOM | Measuring, scrolling, focusing, third-party DOM libraries |
| `'sync'` | Immediately on every mutation, no batching | Rarely. Never on arrays or anything mutated in a loop. |

```js
// BAD — reads stale DOM; default flush runs before the patch
watch(items, () => { list.value.scrollTop = list.value.scrollHeight })

// GOOD
watch(items, () => { ... }, { flush: 'post' })   // or watchPostEffect(...)
// GOOD — one-off
watch(items, async () => { await nextTick(); measure() })
```

### 5. Deep watching is a full traversal, and it is implicit on reactive objects

`watch(reactiveObject, cb)` is **implicitly deep** — Vue walks every nested property to establish tracking, on creation and again on change. On a large tree that is a real cost, and the callback fires with `newValue === oldValue` (the same proxy), so diffing is on you.

```js
// BAD — deep traversal of the whole form on every keystroke
watch(form, saveDraft, { deep: true })

// GOOD — watch the exact fields
watch(() => [form.title, form.body], saveDraft)

// GOOD — cap the traversal depth (3.5+)
watch(form, saveDraft, { deep: 2 })
```

A getter source (`() => obj.a.b`) is shallow by default and compares by reference — usually what you want.

### 6. Cleanup and effect scope — where effects die

Watchers and `watchEffect` created **synchronously inside `setup()`** are bound to the component and stopped automatically on unmount. Anything else leaks.

- **Created after an `await`** in `<script setup>` or async `setup()`: not bound, never stopped. Same for lifecycle hooks. Lint: `vue/no-watch-after-await`, `vue/no-lifecycle-after-await`, `vue/no-expose-after-await`.
- **Created outside a component** (a store, a singleton service): not bound. Wrap in `effectScope()` and call `scope.stop()`.
- Per-run cleanup (cancel an in-flight request, clear a timer) belongs in `onWatcherCleanup()` (3.5+) or the callback's third argument — **called synchronously, before the first `await`**.

```js
watch(query, (q) => {
  const ctrl = new AbortController()
  onWatcherCleanup(() => ctrl.abort())     // must be before any await
  fetch(`/s?q=${q}`, { signal: ctrl.signal })
})

// composable used outside a component, or a long-lived store
const scope = effectScope()
scope.run(() => { watchEffect(...) })
onScopeDispose(() => socket.close())       // fires when the scope stops
scope.stop()
```

`watch()` returns a `WatchHandle` that is callable to stop, plus `pause()` / `resume()` / `stop()` (3.5+). Use `onUnmounted` for component-owned resources; `onScopeDispose` for composables that must also work outside a component.

### 7. `provide` / `inject` — the typing hole and the non-reactive snapshot

Two independent bugs, both common in generated code.

```ts
// BAD — string key: injected type is unknown; a typo is a silent undefined
provide('user', user)
const user = inject('user')

// GOOD — InjectionKey carries the type and the identity
export const UserKey: InjectionKey<Ref<User>> = Symbol('user')
provide(UserKey, user)
const user = inject(UserKey)             // Ref<User> | undefined
const user = inject(UserKey, fallback)   // no undefined
```

```js
// BAD — provides a snapshot; the injector never sees an update
provide(CountKey, count.value)

// GOOD — provide the ref/computed itself
provide(CountKey, count)
provide(CountKey, readonly(count))       // injector cannot mutate
```

Mutate provided state **in the provider**. If an injector must change it, provide a function alongside the value. Under SSR, provided state must be created per request — see Security.

### 8. `v-for` keys, and `v-if`'s higher priority on the same element

Without a stable `:key`, Vue patches list elements **in place by index**. On reorder, filter, or prepend, DOM and child-component state (input values, focus, open/closed, animation) stays at the old index and attaches to the wrong row.

```vue
<!-- BAD — index key; reordering moves state to the wrong row -->
<Row v-for="(item, i) in items" :key="i" :item="item" />

<!-- GOOD — stable identity from the data -->
<Row v-for="item in items" :key="item.id" :item="item" />
```

Index keys are acceptable only for a static, append-only, never-reordered, never-filtered list of stateless nodes. Keys must be primitives (string/number/symbol), not objects.

**`v-if` has higher priority than `v-for` on the same node in Vue 3** — the reverse of Vue 2. The `v-if` expression cannot see the loop variable and throws. Move `v-for` onto a wrapping `<template>`, or filter in a `computed`. Lint: `vue/no-use-v-if-with-v-for`, `vue/require-v-for-key`.

### 9. `v-model` on components — `defineModel()` and the update contract

`v-model="x"` on a component is `:modelValue="x"` + `@update:modelValue="x = $event"`. Props are one-way — mutating one directly is a lint error (`vue/no-mutating-props`) and the parent overwrites it on the next render.

```vue
<script setup>
// GOOD — 3.4+: declares the prop and emits update:modelValue on write
const model = defineModel()
const count = defineModel('count', { type: Number })   // v-model:count

// v-model.trim — modifiers come back from the array form
const [text, mods] = defineModel({
  set: (v) => (mods.trim ? v.trim() : v)
})
</script>
```

Watch the documented desync: giving `defineModel` a `default` while the parent binds an empty ref leaves the parent at `undefined` and the child at the default — they disagree from the first render. Put the default in the parent. Object/array defaults must be factory functions.

### 10. Template refs are `null` before mount

A template ref is populated on mount and set back to `null` on unmount. Reading it in `setup()`, in a `pre`-flush watcher, or in a `computed` gets `null`.

```vue
<script setup>
const input = useTemplateRef('input')   // 3.5+; matches ref="input" at runtime
onMounted(() => input.value?.focus())   // only safe here or later
</script>
<template><input ref="input"></template>
```

- `<script setup>` components are **closed by default** — a parent holding a ref to the child sees nothing until the child calls `defineExpose({ ... })`.
- `ref` inside `v-for` yields an array whose order is not guaranteed to match the source.
- Vapor components do not expose `$el` / `$props` / `$refs` on component template refs.
- Lint: `vue/prefer-use-template-ref`.

### 11. Async components and `<Suspense>` — still experimental

**`<Suspense>` is documented as experimental as of Vue 3.5 and 3.6-rc; the API may still change.** If the app's loading behaviour depends on it, record that as an accepted risk at Gate 4 rather than assuming it is stable.

- Top-level `await` in `<script setup>` compiles to `async setup()`, which **requires a `<Suspense>` ancestor**. Without one the component never renders. If you do not want Suspense, fetch in `onMounted` or a watcher and render your own LOADING state.
- `defineAsyncComponent()` must be called somewhere that runs **once**: module scope, or the `<script setup>` body (which runs once per instance). Calling it in a render function, a `computed`, or any expression re-evaluated per render produces a new component definition each time — the subtree remounts and loses all its state on every parent update.
- Pass `loadingComponent`, `errorComponent`, `delay`, and `timeout`; the defaults give you no error UI at all.
- Under SSR, `hydrate: hydrateOnVisible()` / `hydrateOnIdle()` / `hydrateOnInteraction()` (3.5+) defers hydration. Framework sugar over this lives in the framework module.

### 12. `<KeepAlive>` — deactivated is not unmounted

A cached component is **deactivated**, not destroyed. `onUnmounted` never fires, so any cleanup you put there never runs: intervals keep ticking, sockets stay open, observers stay attached. On return the instance is restored with its old state and no refetch.

```js
onDeactivated(() => clearInterval(id))   // not onUnmounted
onActivated(() => refetch())             // otherwise the user sees stale data
```

Also set `:max="N"` — an unbounded `<KeepAlive>` around a router view retains every page the user ever visited, with their data, for the life of the tab. `include`/`exclude` match on component `name`, which minifiers and `<script setup>` inference can change; set `defineOptions({ name: 'X' })` explicitly if you rely on them.

### 13. `shallowRef` / `shallowReactive` / `markRaw` — the escape hatch

`reactive()` and `ref()` deep-proxy every nested object on access. For a 10k-row dataset, a large parsed document, or a third-party class instance, that conversion cost and the proxy-per-object overhead dominate.

```js
const rows = shallowRef([])          // only .value assignment is tracked
rows.value = await fetchRows()       // fine — reassignment triggers

rows.value.push(r)                   // NOT tracked
triggerRef(rows)                     // force it, after in-place mutation

const map = markRaw(new mapboxgl.Map(...))  // never make this reactive
const raw = toRaw(state)             // hand the un-proxied object to a lib
```

Use `markRaw` for anything with internal identity checks or private class fields — chart, map, editor, and WebGL instances break when wrapped in a Proxy, and `#private` field access through a Proxy throws. Use `toRaw()` when passing state to a non-Vue library that compares object identity.

## Composition API vs Options API, and what `<script setup>` compiles to

| Boundary | Rule |
|---|---|
| One component, one API | Do not mix `setup()` state with `data()`/`methods` in the same component. Migrate a whole file at a time. |
| `<script setup>` | Composition API only. `this` does not exist. Top-level bindings are auto-exposed to the template. |
| Vapor Mode (3.6) | **Options API is not supported.** `app.config.globalProperties` and `getCurrentInstance()` do not work. |
| Reuse | Composables (functions returning refs), not mixins. Mixins have implicit name collisions and no traceable source. |
| Tree-shaking | `__VUE_OPTIONS_API__: false` drops Options API support from the bundle — and breaks any dependency that uses it. |

`defineProps`, `defineEmits`, `defineModel`, `defineExpose`, `defineOptions`, `defineSlots` are **compiler macros, not runtime imports**. They are erased at compile time, so they cannot be called conditionally, aliased, assigned to a variable, or moved into a helper. Type-only declarations must be statically analyzable by the compiler. `<script setup>` cannot use the `src` attribute.

## Security

Vue-runtime-level mechanics. Object-level authorization, request handling, and server routes are backend concerns; Nuxt server islands, Nitro route rules, and `runtimeConfig` are framework-module concerns.

**Vue's baseline:** `{{ }}` interpolation and `:attr` bindings are escaped through `textContent` / `setAttribute`. Vue blocks `<style>{{ x }}</style>`. Everything below is a way to step outside that.

### The injection sinks Vue does not protect

| Vector | The bug | What to write |
|---|---|---|
| HTML injection | `v-html="userHtml"` (and `innerHTML` in a render fn / JSX) is unescaped | Render structured data. If you truly need HTML, sanitize server-side before storing. Turn on `vue/no-v-html` |
| Template injection | A template string built from user input is arbitrary JS — **and during SSR it executes on your server**. Vue calls this Rule No.1 | Never pass user content to `template:`, `compile()`, or a runtime-compiler build. Nuxt's CVE-2026-71320 (server-side RCE via template injection in server island props, Aug 2026) is this exact bug at framework level |
| Component injection | `<component :is="userString" />` resolves any globally registered component | Map the user value through an allow-list object to a component reference |
| URL injection | `:href="userUrl"` accepts `javascript:` | Validate the scheme on the **backend** before storing. Frontend-only sanitization means the API is already exposed |
| Style injection | `:style="userStyles"` allows a transparent full-page overlay (clickjacking) | Object syntax with an allow-list of properties: `:style="{ color: userColor }"` |

### Client-side authorization is a hint, never a gate

`v-if="user.isAdmin"` removes DOM. It does not remove the route's JavaScript chunk, the props already serialized into the SSR payload, or the API endpoint. Anyone can read all three. The server does object-level authorization; write that down in the Assumption Ledger so nobody mistakes the `v-if` for a control.

Same class of bug on the write path: `v-model` bound to a whole object and then `PATCH`ed as a whole object sends `role`, `isAdmin`, and `id` along with the fields the user edited. Build the request body from an explicit field list.

### SSR cross-request state pollution — the Vue-specific data leak

Module-scope `ref()` / `reactive()` is a singleton. In the browser modules are re-initialized per page load, so it looks fine. On the server the module is initialized **once at boot and reused for every request**, so one user's data is served to the next user.

```js
// BAD — module scope, shared by every SSR request on this process
export const currentUser = ref(null)

// GOOD — a new app and a new store per request
export function createApp() {
  const app = createSSRApp(Root)
  const store = createStore()
  app.provide(StoreKey, store)
  return { app, store }
}
```

The same rule covers `app.config.globalProperties`, module-level caches, and any Pinia store instantiated outside `createApp`. This is a cross-tenant leak, not a performance note.

### Secrets in the bundle

Vite inlines every `VITE_`-prefixed env var into the shipped JavaScript at build time. `import.meta.env.VITE_API_SECRET` is public — it is in `dist/`, readable by anyone. Nothing secret may carry the client prefix (`VITE_`, or the framework's own — Nuxt `runtimeConfig.public`, checked in the framework module). Grep the built output for known secret values in CI.

### Defaults that ship on

- `__VUE_PROD_DEVTOOLS__` defaults to `false`. Turning it on ships the component tree and every component's state to anyone with the devtools extension installed.
- `data-allow-mismatch` silences hydration-mismatch warnings. If server and client diverge because the server rendered a *cached other user's* content, you have silenced the detector, not fixed the bug. Use it only on values that are inherently client-specific (locale-formatted dates, timezones).
- The default `vue.runtime.esm-bundler` build has no compiler. Aliasing `vue` to `vue/dist/vue.esm-bundler.js` pulls in the in-browser compiler, which builds render functions through the `Function` constructor — that requires `script-src 'unsafe-eval'` in your CSP. Keep the runtime-only build so the CSP can forbid it.

### Supply chain, Vue-specific

The `vue` package declares no install scripts and its runtime dependencies are all first-party `@vue/*`. The risk is the rest of the tree: commit the lockfile, install with `npm ci` (not `npm install`) in CI, and pass `--ignore-scripts` where the toolchain allows. Official-adjacent packages do get real CVEs — `vue-i18n` had prototype pollution in `handleFlatJson` (CVE-2025-27597, high) and DOM XSS through `escapeParameterHtml` tag attributes (CVE-2025-53892, medium). Run `npm audit` / `osv-scanner` in CI, and pin the Vue minor deliberately rather than floating on `^3`.

## How this slots into the pipeline

- **Gate 3 (UI state machine):** model the interaction in Vue terms. Each footgun maps to a state-machine edge that is easy to miss — no STALE state usually means a `computed` doing a fetch (footgun 3); no post-mutation measurement means the wrong flush timing (footgun 4); a KeepAlive'd route with no ACTIVATED edge means users see stale data (footgun 12).
- **Gate 5 (state ownership):** name every piece of state and its owner — component `ref`, composable inside an `effectScope`, Pinia store, or `provide`/`inject` with an `InjectionKey`. Declare which state is created per SSR request. Footguns 1, 2, 6, 7 and the cross-request pollution rule are Gate-5 decisions, not code-review findings.
- **Gate 6 (implementation):** code against footguns 1–13. Enable `eslint-plugin-vue` (10.x) `flat/recommended` plus `vue-tsc` in CI — most of these have a rule that catches them.
- **Gate 7 (review):** the reliability-reviewer checks async correctness (3, 4, 6, 11, 12); the frontend-perf-reviewer checks 5, 13; the security-reviewer works the Security section, where cross-request state pollution and `v-html` are the two most commonly missed. Items 1, 2, 7, 8, 9, 10 are correctness issues any reviewer should flag.

## Edit boundary (what belongs here vs. above/below)

**The 4-question placement test:**

1. True for React, Angular, and Svelte too (any reactive UI library)? → **up** to `mir-frontend`.
2. True for every Vue meta-framework (Nuxt, Vite SPA, Quasar) because they all run Vue's reactivity system and compiler? → **here**.
3. Specific to one meta-framework's mechanics (Nuxt `useAsyncData`/`useFetch`, server islands, Nitro routes, `runtimeConfig`, file-based routing, payload caching, Quasar build modes)? → **down** to `mir-frontend-vue-<framework>`.
4. Different reactivity library (React, Angular, Svelte)? → its own `mir-frontend-<lib>` tier. Never widen this one.

Cross-ref: full edit map is in `mir-frontend/SKILL.md` → "Where these instructions live."
