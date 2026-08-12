# Custom elements — lifecycle, reflection, shadow DOM, forms

Depth for `mir-frontend-vanilla` footgun 9. Baseline data verified 13 Aug 2026 (webstatus.dev).

## Lifecycle callbacks

| Callback | Fires | Safe to do | Never do |
|---|---|---|---|
| `constructor()` | On upgrade or `document.createElement` | `super()`, own fields, `attachShadow`, `attachInternals` | Read/set attributes, touch children or `parentNode`, render |
| `connectedCallback()` | Every time the element is inserted into a document — except a state-preserving `moveBefore()`, which fires `connectedMoveCallback()` instead | Read attributes, render, add listeners, start observers | Assume it runs once |
| `disconnectedCallback()` | Every removal | `abort()` the controller, `disconnect()` observers, clear timers | Rely on it for persistence — it does not run on tab close |
| `attributeChangedCallback(name, old, new)` | Only for names in static `observedAttributes`; once per observed attribute during upgrade, **before** `connectedCallback` | Sync the property, mark dirty | Re-enter by writing the same attribute |
| `adoptedCallback()` | On `document.adoptNode` across documents | Re-resolve document-scoped refs | Assume it ever fires (rare) |
| `formAssociatedCallback` / `formResetCallback` / `formDisabledCallback` / `formStateRestoreCallback` | Only when `static formAssociated = true` | See "Form association" below | |

`connectedCallback` firing on every insertion is the one that bites. Moving an element with `append()` fires `disconnectedCallback` then `connectedCallback`. If `connectedCallback` appends children or adds listeners without a guard, you get duplicates.

```js
connectedCallback() {
  if (!this.#initialized) { this.#renderOnce(); this.#initialized = true; }
  this.#ac = new AbortController();          // a used controller cannot be reused
  this.addEventListener("click", this.#onClick, { signal: this.#ac.signal });
}
disconnectedCallback() { this.#ac.abort(); }
```

## Upgrade timing

Three states: **undefined** (parsed, no class), **upgraded** (class applied, callbacks run), **failed**. Order in the page matters.

- Script in `<head>` with `defer`, or a module: definition registers before the parser reaches your elements in most cases, but never rely on it.
- `customElements.whenDefined("my-el").then(...)` waits for a definition. `await customElements.whenDefined(...)` before measuring or focusing a custom element.
- `customElements.upgrade(root)` forces upgrade of a subtree you built with `Document.parseHTMLUnsafe` or an inert template before inserting it.
- `:defined` in CSS lets you hide un-upgraded elements and avoid layout shift: `my-el:not(:defined) { visibility: hidden; }`.
- **Property shadowing** is the classic upgrade bug. A framework or inline script sets `el.value = "x"` before upgrade; that creates an own data property that shadows your prototype accessor forever. Run the delete-and-reassign loop from footgun 9 first thing in `connectedCallback`.

## Attribute vs property reflection

Attributes are strings in HTML. Properties are JS values. They are not the same channel and reflecting the wrong things causes loops and data loss.

| Kind | Reflect? | Why |
|---|---|---|
| Boolean (`disabled`, `open`) | Yes, both ways | Presence-based; `toggleAttribute(name, !!v)` |
| Scalar config (`variant`, `count`) | Yes, both ways | Serializes cleanly; styleable with `[variant="x"]` |
| Object / array / function | **No** | `[object Object]` in the DOM; use a property only |
| Frequently changing internal state | No — use `:state()` | `this.#internals.states.add("loading")`, styled with `:state(loading)`. Baseline Newly (2024-05-17) |
| Current value of an input-like element | Property only | Matches `<input>`: the attribute is the *default*, the property is the *current* value |

Loop guard, both directions:

```js
static observedAttributes = ["variant"];
set variant(v) { if (v !== this.getAttribute("variant")) this.setAttribute("variant", v); }
get variant() { return this.getAttribute("variant") ?? "default"; }
attributeChangedCallback(name, oldV, newV) {
  if (oldV === newV) return;                 // stops the ping-pong
  this.#requestRender();
}
```

Batch renders. An element with five observed attributes renders five times during upgrade unless `#requestRender()` coalesces (a dirty flag plus `queueMicrotask`).

## Shadow DOM consequences

**Styles.** Page CSS does not reach into a shadow root, and shadow CSS does not leak out. That is the point, and it is also what breaks theming.

- Inherited properties (`color`, `font`, `line-height`) and **CSS custom properties do cross the boundary**. Expose a themable surface as custom properties: `background: var(--btn-bg, #eee)`.
- `::part(name)` lets the page style a marked element: `<span part="label">` ← `my-el::part(label)`. Parts are the supported styling contract; class names inside a shadow root are not.
- `::slotted(sel)` styles slotted light-DOM children, but only one level deep and only the top-level slotted node.
- Use `adoptedStyleSheets` with a module-level `CSSStyleSheet` shared by every instance, not a `<style>` tag cloned per instance — one sheet parsed once instead of N.
- `:host`, `:host(.modifier)`, and `:host-context()` style the element itself. `:host` has the specificity of a single class, so page styles on the tag name can override it.

**Focus.** A shadow root does not create a focus trap, but it does change what you can see.

- `document.activeElement` returns the *host*, not the inner control. Walk `el.shadowRoot.activeElement` to find the real one.
- `delegatesFocus: true` on `attachShadow` sends focus on the host to the first focusable child and applies `:focus-visible` to the host. It also makes clicking anywhere on the host focus that child — which is wrong for elements with multiple controls.
- `closed` mode blocks ordinary outside access, including test tooling and your own bug reports — and it is **not** a security boundary: same-page script can patch `attachShadow` before you call it. Use `open` unless hiding the API surface is worth losing your own tooling. Against a hostile host page the answer is a cross-origin sandboxed iframe, not `closed`.
- Tab order across the boundary follows DOM order and works by default. `tabindex` on the host makes the host itself focusable — usually not what you want alongside a focusable inner control.

**Accessibility.** This is the real cost of shadow DOM.

- **ID references do not cross the boundary.** `aria-labelledby`, `aria-describedby`, `aria-controls`, and `for` are ID-based, so a label in the light DOM cannot point at an input inside a shadow root, and vice versa.
- The supported fix is **ARIA element reflection** — set the *element* rather than the ID: `input.ariaLabelledByElements = [labelEl]`. Baseline **Widely** since 2026-04-24.
- **Reference Target** (declaratively forwarding an outside ID reference to an inner element) is Baseline **Limited**. Do not depend on it.
- Set default semantics from inside with `ElementInternals`: `this.#internals.role = "button"`, `this.#internals.ariaLabel = "Close"`. These are defaults — an author-set `role`/`aria-*` attribute on the host wins, which is the correct precedence.
- Simplest escape hatch: do not put the focusable, labelled control in a shadow root at all. Slot the light-DOM `<input>` and keep only presentation inside.

## Form association

Baseline **Widely** since 2025-09-27 (Chrome 77, Firefox 98, Safari 16.4). This is how a custom element participates in a real `<form>` — submission, validation, reset, and autofill restore.

```js
class MyInput extends HTMLElement {
  static formAssociated = true;               // required; enables the form callbacks
  #internals = this.attachInternals();
  set value(v) { this.#value = v; this.#internals.setFormValue(v); this.#validate(); }
  #validate() {
    const bad = this.required && !this.#value;
    this.#internals.setValidity(
      bad ? { valueMissing: true } : {},
      bad ? "This field is required" : "",
      this.#control                            // the element focused by reportValidity()
    );
  }
  formResetCallback() { this.value = this.getAttribute("value") ?? ""; }
  formStateRestoreCallback(state, mode) { this.value = state; }   // bfcache + autofill
  formDisabledCallback(disabled) { this.#control.disabled = disabled; }
}
```

- `setFormValue(v)` is what puts a name/value pair in the submitted `FormData`. Without it the element submits nothing, silently.
- `setValidity()` needs all three arguments when invalid. Omit the third and `reportValidity()` has nothing to focus, so the browser cannot show its bubble.
- Implement `formResetCallback` or the element keeps its value after a form reset.
- `formStateRestoreCallback` handles back-navigation and autofill. Skipping it means the user's typed value disappears on back.
- The host must be labellable for a `<label>` to work — `formAssociated = true` provides that.
- Delegate `:invalid`/`:valid` styling through `this.#internals.states` or the built-in validity pseudo-classes on the host; do not hand-roll an `is-invalid` class that diverges from the real validity state.

## Declarative shadow DOM and SSR

Baseline Newly since 2024-02-20.

```html
<my-card>
  <template shadowrootmode="open"><slot></slot></template>
  <p>Server-rendered light DOM</p>
</my-card>
```

- The parser attaches the shadow root, so the component renders before any JS loads. On upgrade, check `this.shadowRoot` and reuse it instead of calling `attachShadow` again — a second call throws.
- `innerHTML` does **not** parse `<template shadowrootmode>`. Only the HTML parser and `setHTMLUnsafe` / `Document.parseHTMLUnsafe` do. That is the entire reason those two methods exist.
- Declarative shadow roots and `adoptedStyleSheets` do not combine on the server; ship a `<style>` inside the template for the pre-JS paint, then swap to the shared sheet on upgrade if you care about the parse cost.

## Do not use

- **Customized built-in elements** (`class X extends HTMLButtonElement`, `<button is="x-btn">`). Baseline **Limited**; Apple's standards position is oppose and there is no Safari implementation. Wrap the native element, or use an autonomous element plus `ElementInternals` for semantics.
