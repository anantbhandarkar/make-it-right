# Teardown and leak hunting

Depth for `mir-frontend-vanilla` footguns 1–4. Two parts: the pattern that prevents leaks, and the procedure that finds the ones already shipped.

## Part 1 — one controller per instance

Every component gets exactly one `AbortController`. Everything that needs undoing is registered against its signal, so teardown is a single `abort()` and cannot be partially forgotten.

```js
class Component {
  #ac;
  mount(host) {
    this.#ac = new AbortController();
    const { signal } = this.#ac;

    // listeners — removed automatically on abort
    host.addEventListener("click", this.#onClick, { signal });
    window.addEventListener("hashchange", this.#onRoute, { signal });

    // observers — abort is the single teardown hook
    const io = new IntersectionObserver(this.#onVisible);
    io.observe(host);
    signal.addEventListener("abort", () => io.disconnect(), { once: true });

    // timers
    const t = setInterval(this.#poll, 30_000);
    signal.addEventListener("abort", () => clearInterval(t), { once: true });

    // animation frames
    let raf = 0;
    const loop = () => {
      if (signal.aborted || !host.isConnected) return;   // both checks
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    signal.addEventListener("abort", () => cancelAnimationFrame(raf), { once: true });

    // network — the same signal cancels in-flight requests
    fetch("/api/data", { signal }).then(/* … */).catch(this.#ignoreAbort);

    // third-party widgets with their own destroy()
    const chart = new ThirdPartyChart(host);
    signal.addEventListener("abort", () => chart.destroy(), { once: true });
  }
  unmount() { this.#ac.abort(); this.#ac = null; }
  #ignoreAbort = (e) => { if (e.name !== "AbortError") throw e; };
}
```

Rules:

- **One controller per instance.** A module-level controller shared by every instance means aborting one kills all of them.
- **Controllers are single-use.** After `abort()` the signal stays aborted; `addEventListener` with it registers nothing. Create a fresh controller on remount.
- **`{ once: true }` on the abort listener**, otherwise the abort listener itself is a retained reference.
- Register the abort handler **immediately after** creating the thing it tears down. If they drift apart in the file, one gets deleted without the other.
- In a custom element: create the controller in `connectedCallback`, abort in `disconnectedCallback` (see `web-components.md`).
- `AbortSignal.any([userSignal, AbortSignal.timeout(ms)])` composes a component-lifetime signal with a per-request deadline. The composed signal is aborted by whichever fires first.

## What abort does not cover

| Thing | Why | Do instead |
|---|---|---|
| `element.onclick = fn` | Property handlers ignore the options bag | Use `addEventListener`, or null the property in teardown |
| Inline `onclick="..."` attributes | Not registered through `addEventListener` at all | Remove them; they also break CSP and Trusted Types |
| A `WebSocket` / `EventSource` | Not signal-aware | `ws.close()` from an abort listener |
| A module-scope `Map`/array holding elements | Nothing aborts a data structure | `WeakMap`/`WeakSet`, or delete the entry explicitly |
| A `MutationObserver` on `document.body` | Survives your component | `disconnect()` from an abort listener; check the observer is not also observed by another instance |
| A promise already resolved | Abort does not un-resolve | Guard the continuation: `if (signal.aborted) return;` after every `await` |

## Part 2 — finding a leak that already shipped

Do this in a Chromium DevTools window with no extensions (extensions inject their own retainers and make snapshots unreadable).

1. **Reproduce with a repeat action.** Pick one interaction that mounts and unmounts the suspect component — open/close a modal, navigate to a route and back. A leak is only provable across repetitions.
2. **Memory panel → check "Detached elements"** (or take a heap snapshot and type `Detached` in the class filter). Detached nodes that persist after a forced collection are the finding.
3. **Three-snapshot method.**
   - Load the page, interact once to warm caches, take **Snapshot 1**.
   - Perform the mount/unmount cycle 10 times.
   - Click the trash-can icon to force garbage collection, take **Snapshot 2**.
   - Repeat the 10 cycles, force collection, take **Snapshot 3**.
   - Set the comparison dropdown to "Comparison" against the previous snapshot. Objects with a **positive delta that repeats between snapshot 2 and 3** are leaking. A single positive delta is often just a cache filling.
4. **Read the retainers, not the totals.** Select the leaked object and open the **Retainers** pane. That tree names the thing holding the reference. In practice it is one of: a closure in an event listener (look for `context in ...`), an entry in a `Map`/array, an observer's internal callback list, or a `setInterval` callback.
5. **Confirm the listener count** independently: in the console, `getEventListeners(window)` and `getEventListeners(document)` list every listener with its handler source. Run it before and after the cycle — a growing count is a listener that is never removed.
6. **Performance panel → Memory checkbox** gives the time-series view. A JS heap sawtooth that trends upward after each collection, and a **DOM node count that never returns to baseline**, is the shape you are looking for. A flat-topped heap that drops fully on collection is not a leak.

## Automating it

- `performance.memory` is Chromium-only and coarse. `performance.measureUserAgentSpecificMemory()` is the accurate replacement, requires cross-origin isolation, and returns a promise — usable in a Playwright/Puppeteer regression test that fails the build when a mount/unmount loop grows the heap past a threshold.
- Cheaper and browser-independent: assert `document.getElementsByTagName("*").length` returns to its baseline after N mount/unmount cycles. It catches detached-subtree leaks without any memory API.
- In unit tests, a `FinalizationRegistry` can assert that a component instance is collectable after unmount. Timing is non-deterministic, so keep it out of CI gating and use it as a local diagnostic.
