---
name: mir-frontend-vanilla
description: "Make It Right (vanilla JS / no-framework reactivity tier). Plain-DOM footguns that no reactive library is present to hide. Covers event listeners never removed (the #1 leak) and AbortController as the removal mechanism; detached DOM nodes retained by a closure or a module-scope map; Intersection/Mutation/ResizeObservers never disconnected and timers that outlive their element; innerHTML as an XSS sink and the current alternatives (textContent, Element.setHTML + Sanitizer, Trusted Types CSP); manual state/DOM divergence and the idempotent render-from-state discipline; custom-element lifecycle and upgrade timing, shadow DOM style/focus/ARIA consequences; stale-response-overwrites-fresh-response fetch races; and manual focus management (focus after route change, dialog focus traps, aria-live). Chains: mir-frontend → this. TRIGGER when the UI is built with plain DOM APIs and no reactive library — vanilla JS/TypeScript, jQuery-era code, hand-written Web Components, a static site with its own script, a browser-extension content script, or an embedded third-party widget. SKIP when a reactive library owns rendering — React (mir-frontend-react), Vue (mir-frontend-vue), Angular (mir-frontend-angular), Svelte — SKIP for the generic UX/state gates (mir-frontend), and SKIP for build/meta-framework mechanics (Astro islands, Eleventy, Vite config)."
trigger: /mir-frontend-vanilla
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-frontend-vanilla · Make It Right (vanilla JS tier)

Middle tier. `mir-frontend` decides **what is correct** (any UI); this owns **what plain DOM code gets wrong**. Load order: `mir-frontend` → `mir-frontend-vanilla` → `<framework module, if any>`.

With no library there is **no automatic teardown and no render loop**. Every listener, observer, timer, and DOM write is yours to undo, and every state change is yours to reflect. Those two absences produce almost every bug in this file.

**Platform baseline, verified 13 Aug 2026** (source: webstatus.dev). Check this before you write a fallback — and before you skip one.

| Feature | Baseline | Notes |
|---|---|---|
| `AbortController` / `addEventListener({signal})` | Widely | The listener-removal mechanism. Use it |
| `<dialog>` · `inert` · `:focus-visible` | Widely | Native modal focus trap. Do not hand-roll one |
| Form-associated custom elements (`attachInternals`) | Widely (2025-09-27) | Safari 16.4+ |
| Popover API | Newly (2025-01-27) | Safari iOS 18.3 closed the gap |
| Declarative Shadow DOM · `:state()` | Newly | |
| Invoker commands (`command` / `commandfor`) | Newly (2025-12-12) | Safari 26.2 |
| **Trusted Types** | **Newly (2026-02-24)** | Firefox 148 shipped it; Safari 26. Enforceable everywhere now |
| `setHTMLUnsafe()` / `parseHTMLUnsafe()` | Newly (2025-09-15) | Explicitly *unsafe*; parses declarative shadow roots |
| View transitions (same-document) | Newly (2025-10-14) | Firefox 144 |
| **`Element.setHTML()` / Sanitizer API** | **Limited** | Chrome 146, Firefox 148, **no Safari**. Needs a fallback |
| Cross-document view transitions | Limited | Chrome 126, Safari 18.2, **no Firefox** |
| `<dialog closedby>` · `moveBefore()` · `scheduler.yield()` | Limited | No Safari in any of the three |
| Customized built-ins (`is="..."`) | Limited | Apple's position is **oppose**. Treat as unavailable |

---

## Memory and lifecycle

### 1. Event listeners that are never removed — the #1 leak
A listener on `window`, `document`, or any node that outlives the component keeps the handler alive, and the handler's closure keeps every variable it captured alive — including DOM nodes. Removing the element does not remove the listener from `window`.

`removeEventListener` requires the *identical* function reference and the same `capture` value, so `removeEventListener('click', this.onClick.bind(this))` removes nothing. **Use one `AbortController` per component instead** — one `abort()` removes every listener registered with its signal, including ones you forgot.

```js
class Widget {
  #ac = new AbortController();
  mount(el) {
    const { signal } = this.#ac;
    el.addEventListener("click", (e) => this.#onClick(e), { signal });
    window.addEventListener("resize", this.#onResize, { signal });
    document.addEventListener("keydown", this.#onKey, { signal, capture: true });
    const ro = new ResizeObserver(this.#onSize);
    ro.observe(el);
    signal.addEventListener("abort", () => ro.disconnect(), { once: true });
  }
  destroy() { this.#ac.abort(); }   // everything above is gone, in one call
}
```

Rules: one controller per instance, never a shared module-level one. A controller is single-use — after `abort()` create a new one to remount. Pass the same signal to `fetch` (footgun 10) so navigating away cancels the network request too.

### 2. Detached DOM held by a closure or a map
A node removed from the document is only collected when **nothing** references it. A module-scope array/object keyed by element, a captured `NodeList`, or a callback closure all count.

```js
// LEAK: cache holds the node after replaceChildren(); the closure holds cache
const cache = {};
function attach(el) { cache[el.id] = el; el.addEventListener("click", () => log(cache)); }
```

- Key element metadata with `WeakMap`/`WeakSet`, never a plain object or `Map`. `querySelectorAll` returns a static `NodeList` that pins every node in it — do not hold one past the render that produced it.
- Confirm, do not assume: DevTools Memory → heap snapshot → filter **Detached**. Procedure in `references/teardown-and-leak-hunting.md`.

### 3. Observers that are never disconnected
`IntersectionObserver`, `MutationObserver`, and `ResizeObserver` hold strong references to every observed node and to your callback. An observer that is never disconnected keeps the whole observed subtree alive.

- `unobserve(el)` when a single row leaves the list; `disconnect()` when the component dies — wired to the abort signal as in footgun 1.
- A `MutationObserver` whose callback mutates the same subtree re-triggers itself. Guard with a flag, or narrow the `attributeFilter`/`subtree` options so your own writes are not observed.
- `ResizeObserver loop completed with undelivered notifications` means your callback resized the observed element. Write to a *different* element, or skip the write when the size is unchanged.

### 4. Timers that outlive the element
`setInterval` runs forever. Its callback keeps the element, the closure, and any fetched data alive after the element is gone — and often throws on a null node.

```js
const id = setInterval(tick, 1000);
signal.addEventListener("abort", () => clearInterval(id), { once: true });
```

- Every `setInterval` needs a matching `clearInterval`, every pending `setTimeout` a `clearTimeout`, every `requestAnimationFrame` loop a `cancelAnimationFrame` **and** a bail-out check — a rAF loop that never checks `el.isConnected` keeps running every frame against a detached node.
- Prefer `IntersectionObserver`/`ResizeObserver`/CSS over polling with a timer at all.

## DOM correctness

### 5. `innerHTML` is an XSS sink — and the safe path depends on the browser
The `innerHTML` and `outerHTML` setters, `insertAdjacentHTML`, `Document.write()`, and `Range.createContextualFragment` all parse a string as markup. A `<script>` inserted this way does not run, which is why the bug survives review — but `<img src=x onerror=...>`, `<svg onload=...>`, and `<iframe srcdoc=...>` do run.

Pick in this order:

| Need | Write |
|---|---|
| Plain text | `el.textContent = value` — never a markup setter |
| Known structure | `document.createElement` + `append` + `textContent`, or `<template>` + `cloneNode(true)` |
| Untrusted HTML that must stay HTML | `el.setHTML(str)` where supported, else DOMPurify |

`setHTML()` always strips `<script>`, `<iframe>`, `<embed>`, `<object>`, `<frame>`, SVG `<use>`, and every `on*` attribute — no config can re-enable them. It is **Baseline Limited (no Safari)**, so ship a fallback and pick one behaviour for both paths:

```js
function setSafeHTML(el, html) {
  if ("setHTML" in Element.prototype) return el.setHTML(html);
  el.replaceChildren(DOMPurify.sanitize(html, { RETURN_DOM_FRAGMENT: true }));
}
```

`setHTMLUnsafe()` is not a sanitizer. Its only job is parsing declarative shadow roots; it accepts everything the raw markup setters do.

### 6. Layout thrashing from interleaved read/write
Reading a geometry property (`offsetWidth`, `getBoundingClientRect()`, `scrollTop`, `getComputedStyle`) after a style write forces the browser to run layout synchronously. In a loop that is one forced layout per iteration.

```js
// BAD: read → write → read → write; N forced layouts
for (const el of items) el.style.width = el.offsetWidth + 10 + "px";
// GOOD: all reads, then all writes; one layout
const w = items.map((el) => el.offsetWidth);
items.forEach((el, i) => (el.style.width = w[i] + 10 + "px"));
```

- Batch inserts through a `DocumentFragment` and commit once with `replaceChildren(frag)`. Appending to a live parent does not force layout by itself (the browser batches until the next frame or the next geometry read) — but it does fire mutation observers, style invalidation, and any interleaved read per append. One commit is the cheap shape.
- Measure in a `requestAnimationFrame` callback, write in the next one. Never measure inside a `scroll` handler. Toggle one class rather than setting five `style` properties, and use `el.hidden`/`content-visibility` instead of removing and re-inserting subtrees.
- For long synchronous work, `scheduler.yield()` is Baseline Limited (no Safari) — feature-detect it and fall back to `await new Promise(r => setTimeout(r, 0))`.

### 7. Per-node listeners where delegation belongs
One listener per row means N closures, N teardown obligations, and re-binding after every render. Attach one listener to the stable container and match inside it.

```js
list.addEventListener("click", (e) => {
  const row = e.target.closest("[data-id]");
  if (row && list.contains(row)) select(row.dataset.id);
}, { signal });
```

- Delegation only works for events that bubble. `focus`, `blur`, `load`, and `error` do not — use `focusin`/`focusout`, or the capture phase.
- Always guard with `closest()` plus a `contains()` check; `e.target` is the innermost node, which may be an icon inside the button.
- `passive` defaults to `false` **except** on `window`, `document`, and `document.body`, where every browser but Safari defaults `wheel`, `mousewheel`, `touchstart`, and `touchmove` to `true`. On any other element the default is `false`. If you need `preventDefault()` on one of those events at the document level, pass `{ passive: false }` explicitly or the call is ignored.

## State

### 8. Manual state/DOM divergence — render from state, idempotently
Without a render loop, handlers that write to the DOM directly make the DOM the second source of truth. It drifts: a class set on one path is never cleared on another, a counter in `textContent` disagrees with the array behind it, and undo is impossible.

**One rule: handlers change state, never the DOM. Only `render()` touches the DOM, and it only reads state.**

```js
let state = { items: [], filter: "", status: "idle" };
const setState = (patch) => { state = { ...state, ...patch }; render(); };
function render() {              // pure function of `state`; safe to call twice
  form.filter.value = state.filter;
  root.replaceChildren(buildList(state.items, state.filter));
  root.dataset.status = state.status;
}
```

- `render()` must be **idempotent** — calling it twice with the same state must produce the same DOM and must not double-append, double-bind, or reset focus and scroll. Test that explicitly.
- Full `replaceChildren` is correct and fast enough for lists under a few hundred rows. Beyond that, reconcile by key and reuse nodes — and note `moveBefore()` (which preserves iframe/video/focus state across a move) is Baseline Limited with no Safari.
- Re-rendering a subtree containing the focused element steals focus. Record `document.activeElement` and its selection before, restore after.
- Never read state back out of the DOM (`if (el.classList.contains("open"))`). Read `state.open`.

## Web components

Depth — full lifecycle table, reflection rules, shadow styling and cross-root ARIA, `ElementInternals` validity, and SSR/declarative shadow DOM — is in `references/web-components.md`. Read it before writing a custom element.

### 9. Upgrade timing, lifecycle, and property shadowing
An element that appears in the HTML before its definition loads is inert until `customElements.define` upgrades it. Anything set on it in the meantime becomes an **own property that permanently shadows your class accessor**, so your setter never runs again.

```js
connectedCallback() {
  for (const p of ["value", "disabled"])          // run before any other setup
    if (Object.hasOwn(this, p)) { const v = this[p]; delete this[p]; this[p] = v; }
}
```

- `constructor` may not touch attributes, children, or the parent — only `super()` and own state. Attribute and DOM work goes in `connectedCallback`.
- `connectedCallback` fires on **every** insertion, including a re-parent. Make it idempotent (footgun 8) or you get duplicate listeners.
- `disconnectedCallback` is the teardown hook: `this.#ac.abort()`. It is not guaranteed to run on page unload — do not put "save my data" there.
- `attributeChangedCallback` fires only for names listed in the static `observedAttributes` getter, and fires once per attribute during upgrade, before `connectedCallback`.
- Attribute → property, property → attribute, with no guard, is an infinite loop. Guard with `if (oldValue === newValue) return;` and never reflect object or array properties to attributes at all.
- `is="..."` (customized built-in elements) is Baseline Limited and Apple's position is oppose. Do not use it. Wrap the native element, or accept the loss of native semantics and add ARIA yourself.

## Async

### 10. The stale response overwrites the fresh response
Type "ab", then "abc". If the `/search?q=ab` response is slower it lands last, and the user sees results for a query they already replaced. Responses arrive in completion order, not request order. The same bug hits tab switches, route changes, and pagination.

```js
let ac;
async function search(q) {
  ac?.abort();                                       // cancel the previous request
  ac = new AbortController();
  const signal = AbortSignal.any([ac.signal, AbortSignal.timeout(8000)]);
  try {
    const res = await fetch(`/search?q=${encodeURIComponent(q)}`, { signal });
    if (!res.ok) throw new Error(res.status);        // fetch does NOT reject on 4xx/5xx
    setState({ items: await res.json(), status: "ok" });
  } catch (e) {
    if (e.name === "AbortError") return;             // expected; not an error state
    setState({ status: "error" });
  }
}
```

- `fetch` rejects only on network failure. A 500 with an HTML error page resolves — check `res.ok` yourself or you will render an error body as data.
- Aborting is not optional bookkeeping: without it the in-flight request still completes, still costs bandwidth, and still runs its `.then`.
- If you genuinely cannot cancel (a third-party SDK), stamp each request with a sequence number and drop any response whose number is not the latest. Cancelling is better — it stops the work.
- `await` between a read and a DOM write means the element may be gone by the time you write. Check `el.isConnected` or `signal.aborted` after every await.

## Accessibility

### 11. Nothing manages focus for you
- **After a client-side route change** the URL changed but focus stayed on the clicked link, and screen readers announced nothing. Move focus to the new view's heading (`tabindex="-1"`, then `.focus()`), and update `document.title`.
- **Modals: use `<dialog>` + `showModal()`.** It is Baseline Widely and gives you the focus trap, background `inert`, Esc-to-close, top-layer stacking, and `::backdrop` for free. A hand-rolled trap misses shadow DOM, iframes, and the browser chrome. Put `autofocus` on the intended first control, and return focus to the trigger on `close`. Light dismiss via `closedby="any"` is Baseline Limited (no Safari) — feature-detect it, do not depend on it.
- **Live regions must exist in the DOM before the text changes.** Creating a `role="status"` element and filling it in the same tick is frequently not announced. Render an empty `<div role="status" aria-live="polite">` on load and write `textContent` into it later. Use `aria-live="assertive"` only for errors that interrupt.
- Removing the focused element sends focus to `<body>` and the user loses their place — move focus deliberately first. Never put `aria-hidden="true"` on an ancestor of the focused element; use `inert`, which removes focusability and semantics together.

---

## Security

Plain-DOM mechanics. Generic frontend security discipline (client-side authorization is a hint, never a gate — the server decides) stays in `mir-frontend`.

**DOM XSS sinks.** The `innerHTML`/`outerHTML` setters, `insertAdjacentHTML`, `Document.write()`, `Range.createContextualFragment`, `setHTMLUnsafe`, `eval`, the `Function` constructor, `setTimeout` called with a string, and `iframe.srcdoc`. Also `setAttribute` on `href`, `src`, `action`, `formaction`, `xlink:href`, and `style` — a user-controlled value there can be `javascript:` or `data:text/html`. Validate the parsed `URL.protocol` against an allow-list (`https:`, `mailto:`); do not string-match the prefix.

**Trusted Types is now enforceable — turn it on.** Baseline Newly since 2026-02-24 (Firefox 148 was the last holdout; Safari 26). It converts the **injection sinks** above from a silent risk into a `TypeError` at the call site — the sink stops accepting strings and accepts only the type your policy produced: `TrustedHTML` for markup sinks, `TrustedScript` for `eval`/`Function`/`setTimeout`-with-a-string, `TrustedScriptURL` for script and worker URLs. **It does not cover `href`, `src`, `action`, `formaction`, or `style` attribute assignment** — a `javascript:` or `data:text/html` URL there still needs your own parsed-protocol allow-list.

```
Content-Security-Policy: require-trusted-types-for 'script'; trusted-types app-html;
```
```js
const policy = trustedTypes.createPolicy("app-html", { createHTML: (s) => DOMPurify.sanitize(s) });
el.insertAdjacentHTML("beforeend", policy.createHTML(userHtml));  // ok — a TrustedHTML
el.insertAdjacentHTML("beforeend", userHtml);                     // now throws TypeError
```
Roll it out with `Content-Security-Policy-Report-Only` first and read the reports; a single unconverted sink in a dependency breaks the page. Keep the number of policies small — a policy literally named `default` applies everywhere and quietly undoes the benefit.

**CSP beyond Trusted Types.** `script-src 'unsafe-inline'` defeats the whole header; use a per-response nonce or hashes. `'unsafe-eval'` is what keeps the string-to-code sinks working — remove it and template libraries that compile strings fail loudly, which is the point. Add `object-src 'none'`, `base-uri 'none'` (an injected `<base>` redirects every relative script URL), and `frame-ancestors 'none'` for clickjacking.

**DOM clobbering.** Named access means `<img name="config">` in user HTML becomes `window.config`, and two elements sharing a `name` become an `HTMLCollection`. Code shaped like `const cfg = window.config || defaults` is then attacker-controlled. Never rely on implicit globals or `document.forms`/`document.images` lookups; use module-scope `const` and `document.getElementById` with IDs you generated.

**`postMessage`.** `postMessage(data, "*")` broadcasts to whatever origin the frame has navigated to. Always pass an explicit `targetOrigin`. On receipt, compare `event.origin` against an exact string **before** touching `event.data`, and check `event.source` if you expect a specific frame. Never pass `event.data` into a sink from the list above.

**Tokens and secrets.** Anything in the bundle is public: API keys, feature-flag payloads, internal hostnames, source maps containing server code. A token in `localStorage` is readable by any XSS on the origin — session tokens belong in a `Secure; HttpOnly; SameSite=Lax` cookie. Do not log request bodies, tokens, or PII to `console` in production; extensions and error reporters read it.

**CSRF and CORS from the client side.** `fetch` defaults to `credentials: "same-origin"` — it **does** send cookies to your own origin, so a same-origin cookie-authenticated `fetch` POST is already CSRF-relevant. `credentials: "include"` extends that to cross-origin. Either way, pair it with `SameSite` cookies plus an origin check or a double-submit token on the server. `mode: "no-cors"` is not a CORS fix — it returns an opaque response you cannot read and hides the real failure. A CORS error is a server-configuration bug; do not route around it with a proxy that forwards credentials.

**Third-party scripts and supply chain.** A `<script src="https://cdn.example/lib.js">` with no `integrity` runs whatever that host serves tomorrow. Pin an exact version, add `integrity` + `crossorigin="anonymous"`, and prefer self-hosting. Any third-party script on the page has full DOM and cookie access — analytics, chat widgets, and tag managers are the usual path in. For npm: commit the lockfile, use `npm ci`, and set `ignore-scripts=true` so a transitive postinstall cannot run at install time. **DOMPurify is 3.4.13 (2026-08-03), and 3.4.5 was a security release for a bypass introduced in 3.4.4** — pin it and treat sanitizer upgrades as security patches, not chores.

**Embedded widgets.** Code that runs inside someone else's page shares their global scope, their CSP, and their prototypes. **Shadow DOM is encapsulation, not a security boundary** — `closed` mode does not stop host-page script, because the host can patch `Element.prototype.attachShadow` before your bundle loads and keep the root. If the host page is untrusted, the only real isolation is a cross-origin sandboxed `<iframe>` talking over `postMessage` with an exact-origin check. Inside the page, capture the built-ins you need (`const { fetch } = window`) at load time, and never trust `window.location` or a global config object for an authorization decision.

---

## How this slots into the pipeline

- **Gate 3 (UI state machine):** the states are real fields in one state object, not classes on nodes. Write the transitions as the only functions allowed to call `setState`. Footgun 8 is the mechanism that makes the Gate 3 machine true at runtime.
- **Gate 5 (design):** name the **teardown owner** for every component (which `AbortController`, aborted by whom, when), the sanitization path and its non-Safari fallback, the focus-management plan, and the render granularity (full `replaceChildren` vs keyed reconcile). State the browser support floor explicitly — half this file's advice depends on it.
- **Gate 6 (implementation):** every listener, observer, and timer registered against a signal; `render()` idempotent and state-only; every fetch abortable and `res.ok`-checked; no raw markup setter on any value that touched user input or an API response.
- **Gate 7 (review):** reliability-reviewer works footguns 1–4 and 10; a11y-reviewer works 11 plus the shadow DOM section of `references/web-components.md`; frontend-perf-reviewer works 6 and 7; security-reviewer works the Security section, starting with a repo-wide grep for each sink name listed there.

## References

| File | Read it when |
|---|---|
| `references/web-components.md` | Writing or reviewing a custom element — lifecycle table, upgrade, reflection, shadow DOM styling/focus/ARIA, `ElementInternals` and form association, declarative shadow DOM + SSR |
| `references/teardown-and-leak-hunting.md` | A leak is suspected or teardown is being designed — the per-instance controller pattern, and the DevTools heap-snapshot procedure for finding detached nodes |

## Edit boundary (what belongs here vs. above/below)

1. True for React, Vue, and Angular too (loading/empty/error states, interaction contracts, the gates, client-authz-is-a-hint)? → **up** to `mir-frontend`.
2. True for any plain-DOM code because it is a DOM, browser, or Web Components mechanic (listener teardown, observer disconnect, layout thrash, markup setters, custom-element lifecycle, manual focus)? → **here**.
3. Specific to one library's reactivity model (hooks, signals, the virtual DOM, directives, Lit's `@property` and `ReactiveElement`)? → **that library's** `mir-frontend-<lib>` tier — including Lit, which is a library and does not belong here.
4. Specific to one build tool or site framework (Astro islands, Eleventy, Vite config, bundler splitting)? → **down** to a framework module. Never widen this tier.

Cross-ref: full edit map is in `mir-frontend/SKILL.md` → "Where these instructions live."
