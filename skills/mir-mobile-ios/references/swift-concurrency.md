# Swift 6 concurrency — migration recipes

Read at **Gate 6** when the Swift 6 language mode is being enabled, or when a data-race diagnostic blocks a build.

**Verified 13 Aug 2026:** Swift 6.3 is the compiler in Xcode 26.6 (stable). SE-0461 (`nonisolated(nonsending)` / `@concurrent`), SE-0466 (`-default-isolation`), SE-0470 (isolated conformances), and SE-0475 (`Observations`) all shipped in **Swift 6.2**.

---

## 1. Migration order that works

Flipping straight to the Swift 6 language mode on a large target produces hundreds of errors at once and you cannot tell which are real races. Do it in this order, one target at a time, leaf dependencies first:

1. Stay in Swift 5 mode. Set `-strict-concurrency=minimal` → build → fix.
2. `-strict-concurrency=targeted` → build → fix. This checks code that already uses concurrency.
3. `-strict-concurrency=complete` → build → fix. Substantially the Swift 6 diagnostics, but as **warnings**.
4. Only when (3) is clean, set the language mode to Swift 6. Expect it to be close to a no-op — not guaranteed identical, because the language mode and any upcoming features you switch on can still change semantics or promote new diagnostics.

Enable upcoming features individually rather than all at once: `-enable-upcoming-feature NonisolatedNonsendingByDefault`, `-enable-upcoming-feature InferIsolatedConformances`. Each one has its own migration mode with fix-its.

`@preconcurrency import SomeOldModule` suppresses Sendable diagnostics coming from a dependency you do not control. It is a staging tool. Leave a TODO with the dependency name.

---

## 2. Error → fix, expanded

### `global variable 'x' is not concurrency-safe…`

```swift
// BAD
class Analytics { static var shared = Analytics() }        // mutable global

// GOOD — immutable
final class Analytics: Sendable { static let shared = Analytics() }

// GOOD — isolated
@MainActor final class Router { static var current: Route = .home }

// LAST RESORT — you are asserting an external lock protects it
nonisolated(unsafe) var legacyCache: [String: Data] = [:]  // guarded by `cacheLock`
```

`static let` of a non-Sendable type is still an error — the type must be Sendable too.

### `sending 'x' risks causing data races`

Order of preference:

1. Make the type a `struct` of Sendable fields. Most model types should be this.
2. `@MainActor` the type if it only ever exists on the main actor (most view models).
3. `actor` if it owns mutable state used from several places.
4. `sending` on the parameter if ownership genuinely transfers and the caller keeps no reference.
5. `@unchecked Sendable` only with a named lock and a comment.

### Protocol conformance isolation mismatch

```swift
protocol Styler { func applyStyle() }                    // implicitly nonisolated

@MainActor class Widget: Styler {
    func applyStyle() { … }                              // ERROR in Swift 6 mode
}
```

Fixes, best first:

- `@MainActor protocol Styler` — if every conformer is UI.
- `protocol Styler { @MainActor func applyStyle() }` — per-requirement.
- `func applyStyle() async` — lets an isolated conformer satisfy it by hopping.
- `nonisolated func applyStyle()` on the conformer — only if it touches no isolated state.
- `@preconcurrency` on the conformance — staging only.

### Isolated conformances (SE-0470)

```swift
@MainActor final class Model: @MainActor Equatable {
    static func == (l: Model, r: Model) -> Bool { l.id == r.id }
}
```

The conformance is usable only from the main actor. Turning on `InferIsolatedConformances` infers this for global-actor-isolated types so you can drop the annotation.

---

## 3. Actor reentrancy — the bug the compiler will not catch

Data-race safety is not atomicity. An `actor` method suspends at every `await`, and another call can interleave.

```swift
actor TokenStore {
    private var token: Token?
    // BAD — two callers both see nil, both refresh, second write clobbers the first
    func token() async throws -> Token {
        if let token { return token }
        let fresh = try await network.refresh()   // suspension point
        token = fresh
        return fresh
    }
}
```

Fix by holding the in-flight work, not the result:

```swift
actor TokenStore {
    private var task: Task<Token, Error>?
    func token() async throws -> Token {
        if let task { return try await task.value }
        let t = Task { try await network.refresh() }
        task = t
        do { return try await t.value }
        catch { task = nil; throw error }   // clear ONLY on failure, so the next caller retries
    }
}
```

Do not reach for `defer { task = nil }` here. `defer` runs on the success path too, which throws the cached task away and puts you back to N concurrent refreshes — the bug you were fixing.

**Re-check every invariant after every `await`.** State you read before a suspension may be stale after it.

---

## 4. Structured vs unstructured

| | Structured (`async let`, `TaskGroup`) | Unstructured (`Task {}`, `Task.detached`) |
|---|---|---|
| Cancellation | Propagates from the parent automatically | Must be cancelled by hand |
| Lifetime | Bounded by the enclosing scope | Independent; can outlive the creator |
| Isolation | Inherits | `Task {}` inherits, `Task.detached` does not |
| Errors | Propagate to the awaiting parent | Silently swallowed unless you `await` the handle |

SwiftUI's `.task` is a third thing and does not belong in either column: it creates **unstructured** work whose cancellation SwiftUI manages for you on disappear. That gives you the cancellation benefit of a child task, but its errors still do not propagate to any enclosing caller — handle them inside the closure.

A `Task {}` whose result nobody awaits swallows thrown errors. If the work matters, keep the handle and `await` it, or log inside the task.

## 5. Testing async code

- Swift Testing (`@Test`, `#expect`) is the current framework; it supports async tests and test cancellation directly. XCTest still works.
- Do not `sleep` to wait for concurrency — inject a clock, or await the handle you kept.
- Test cancellation explicitly: start the task, cancel it, assert the side effect did **not** happen. This is the test that catches "cancellation is cooperative and nothing checks it."
- Run the test suite with the Thread Sanitizer on at least once in CI. Swift 6 mode covers Swift-to-Swift races; it does not cover races through C, Objective-C, or `@unchecked Sendable`.
