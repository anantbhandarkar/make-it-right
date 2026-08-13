---
name: mir-mobile-ios
description: "Make It Right (iOS module). Swift 6 + SwiftUI + Swift Concurrency mechanical footguns on Apple platforms — the ones the platform-agnostic mobile gates omit: what Swift 6 language mode rejects that Swift 5 allowed (global mutable state, non-Sendable values crossing isolation boundaries), Xcode 26's MainActor-by-default plus nonisolated(nonsending)/@concurrent silently keeping async work ON the main actor, Task lifetime (a Task {} in a view is NOT cancelled on disappear — only .task is), the SwiftUI ForEach(id: \\.self) bug that reuses @State across rows, @State vs @StateObject vs @ObservedObject, BGTaskScheduler's non-guarantee, and iOS security (Keychain accessibility classes, PrivacyInfo.xcprivacy required-reason APIs, App Groups, universal links vs URL-scheme hijacking). Chains: mir-mobile → this. TRIGGER for an Apple-platform app in Swift/SwiftUI/UIKit — iOS, iPadOS, watchOS, tvOS, visionOS — views, view models, concurrency, background work, Keychain, push, deep links, App Store submission. In React Native or Flutter apps, TRIGGER for the native iOS layer (Xcode build settings, entitlements, Info.plist background modes, PrivacyInfo.xcprivacy, CocoaPods/SPM, signing, submission). SKIP for Android/Kotlin (mir-mobile-android), for mobile web / PWA (mir-frontend), and for the server API the app calls (mir-backend)."
trigger: /mir-mobile-ios
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-mobile-ios · Make It Right (iOS)

Platform module. `mir-mobile` decides **what is correct** on any mobile platform (the 8 gates, the 3 invariants, store deadlines); **this** carries Apple SDK mechanics. Load order: `mir-mobile` → `mir-mobile-ios`. Reach for it at Gate 5 (design mechanics), Gate 6 (implementation), Gate 7 (review).

**Stack assumed, versions verified 13 Aug 2026:** Xcode **26.6** stable (Swift **6.3**, needs macOS Tahoe 26.2+) · Xcode 27 **beta** (Swift 6.4, iOS 27 SDK) — don't put a beta toolchain in a Gate 5 design without saying so · iOS **26** shipping, iOS 27 in public beta since Jul 2026, GA expected ~Sept 2026 · Swift 6.3 released 24 Mar 2026 · Swift 5 language mode still compiles.

**Submission floor: Xcode 26 + the iOS 26 SDK, in force since 28 Apr 2026**, no announced grace period. Rebuilding against the iOS 26 SDK opts native controls into the Liquid Glass appearance — satisfying the floor is a **UI change**, not a no-op. See `mir-mobile/references/store-compliance.md`.

---

## Swift Concurrency

### 1. What the Swift 6 language mode rejects that Swift 5 allowed

Swift 5 mode reports these as warnings (or nothing). Swift 6 mode makes them errors. This is the single largest source of "it built last year" failures.

| Error you get | Why | What to write instead |
|---|---|---|
| `global variable 'x' is not concurrency-safe because it is non-isolated global shared mutable state` | `static var` / global `var` is reachable from every isolation domain | `let` if it never changes · `@MainActor static var` · an `actor` · `nonisolated(unsafe)` **only** when a lock you own already guards it |
| `sending 'x' risks causing data races` | A non-`Sendable` value crosses an isolation boundary | Make the type `Sendable` (a struct of Sendable fields gets it free) · isolate the type to `@MainActor` · make it an `actor` · mark the parameter `sending` |
| `main actor-isolated instance method 'f()' cannot be used to satisfy nonisolated protocol requirement` | An unannotated protocol requirement is `nonisolated`; your `@MainActor` type cannot satisfy it synchronously | Annotate the protocol or the requirement `@MainActor` · make the requirement `async` · `@preconcurrency` conformance as a staging step only |
| Conformance errors on `@MainActor` types conforming to `Equatable`/`Hashable` | Global-actor isolation vs. nonisolated requirement | Isolated conformances (SE-0470, Swift 6.2): `@MainActor class M: @MainActor Equatable`, or turn on `InferIsolatedConformances` |

Migrate module by module: keep Swift 5 mode, turn on `-strict-concurrency=complete` first, fix the warnings, *then* flip the language mode. Flipping first buries you. Read `references/swift-concurrency.md` for the per-error recipes.

### 2. `nonisolated async` no longer means "off the main actor"

Swift 6.2 (SE-0461) changed where a `nonisolated async` function runs. Under the upcoming feature `NonisolatedNonsendingByDefault`, it runs **on the caller's actor**. Called from `@MainActor`, it stays on the main actor.

```swift
// Pre-6.2 intent: "this hops off the main actor". Post-6.2 with the flag on: it does NOT.
nonisolated func resize(_ image: UIImage) async -> UIImage { heavyCPUWork(image) }   // blocks the main actor

@concurrent func resize(_ image: UIImage) async -> UIImage { heavyCPUWork(image) }   // always runs off-actor
```

- `@concurrent` = the old behavior, always switches off the actor.
- `nonisolated(nonsending)` = explicitly "run on the caller's actor".
- The symptom of getting this wrong is a hitchy UI with no compiler complaint. Profile with the Main Thread Checker and the Hangs instrument, not by reading the code.

### 3. Default actor isolation — know what your module is set to

SE-0466 (Swift 6.2) adds `-default-isolation MainActor` (SwiftPM: `SwiftSetting.defaultIsolation(MainActor.self)` — it takes the metatype; `.MainActor` does not compile). Without it the default stays `nonisolated`. Xcode 26's new-app template turns on approachable concurrency and the `MainActor` default; the settings are per-target, so **read your own target's Swift Compiler build settings rather than assuming** (reported as `SWIFT_APPROACHABLE_CONCURRENCY` / `SWIFT_DEFAULT_ACTOR_ISOLATION`, which I could not confirm against a primary Apple source).

**Why it matters:** the same file compiles differently in an app target (MainActor default) and a package target (nonisolated default). Code copied between them changes meaning silently. State the module's default isolation in the Gate 5 design.

### 4. `Sendable` — pick the mechanism, don't reach for `@unchecked`

In order of preference: **struct** of Sendable fields (implicit; public types still need an explicit `: Sendable`) → **`@MainActor`** the type if it only ever lives on the main actor (most view models) → **`actor`** if it owns mutable state used from several places → **`sending`** on the parameter when ownership genuinely transfers → **`@unchecked Sendable`** only with a comment naming the lock that already guards it. Closures crossing a boundary are `@Sendable` and capture values, not `self`.

`@unchecked Sendable` deletes the diagnostic, not the race. It is a promise, not a proof.

### 5. Task lifetime — `Task {}` in a view is not cancelled when the view disappears

```swift
// BAD — unstructured Task; survives the view, keeps the network call and self alive
.onAppear { Task { await model.load() } }

// GOOD — child of the view's lifetime; cancelled on disappear, restarted when id changes
.task { await model.load() }
.task(id: userID) { await model.load(userID) }
```

- `.task` inherits the view's `@MainActor` isolation and is cancelled on disappear. `Task {}` is unstructured and is not.
- **Cancellation is cooperative.** A cancelled task keeps running until something checks. `try Task.checkCancellation()`, `Task.isCancelled`, `try await Task.sleep(...)` (throws on cancel), and `URLSession`'s async methods (throw `URLError.cancelled`) are the checkpoints. A `while true` loop with no check ignores cancellation forever.
- An unstructured `Task` **captures `self` strongly until it finishes**. A task that never finishes (`for await` on an endless stream) keeps the view model alive forever, and `deinit` never runs, so you cannot cancel it from `deinit`. Use `[weak self]`, or store the handle and cancel it from an explicit lifecycle hook.
- `Task.detached` inherits nothing — not isolation, not priority, not task-locals. You almost never want it; prefer a structured child task. `@concurrent` is **not** a substitute: it sets where an `async` *function* runs, it does not create or own a task, so swapping one for the other changes whether the caller waits.

### 6. `async let` vs `TaskGroup`

```swift
// Sequential — the most common AI-generated concurrency bug. N round trips, one at a time.
for id in ids { results.append(try await fetch(id)) }

// async let — fixed, small, known-at-compile-time set of concurrent operations
async let profile = fetchProfile(), settings = fetchSettings()
let (p, s) = try await (profile, settings)

// TaskGroup — dynamic N
try await withThrowingTaskGroup(of: (Int, Item).self) { group in
    for (i, id) in ids.enumerated() { group.addTask { (i, try await fetch(id)) } }
    for try await (i, item) in group { out[i] = item }     // completion order, not submission order
}
```

- An `async let` you never `await` is implicitly cancelled **and awaited** at scope exit — leaving the scope can still block. Always await it, including on the error path.
- Group results arrive in **completion order**. If order matters, key them (as above) instead of appending.
- Everything `addTask` captures must be `Sendable`. Do not share one mutable buffer across child tasks; return values and merge in the parent.
- Unbounded groups melt the network stack. For large N use `withTaskGroup` plus a manual concurrency window (add K, then add one more per completion).

## SwiftUI

### 7. Identity — `ForEach(id: \.self)` reuses `@State` across rows

SwiftUI keys view state by identity. Get identity wrong and state attaches to the wrong row.

```swift
// BAD — identity IS the value. Edit the value and the row is destroyed and rebuilt (focus, @State, animation lost).
// Two equal values = duplicate IDs = one row's state rendered for both.
ForEach(names, id: \.self) { name in RowView(name: name) }

// BAD — count is read once; changing it triggers a runtime warning and stale rows
ForEach(0..<items.count) { i in RowView(item: items[i]) }

// GOOD — stable identity stored on the model, not derived from its contents
struct Item: Identifiable { let id: UUID; var title: String }
ForEach(items) { item in RowView(item: item) }
```

`id: \.self` is safe only for values that are unique **and** never edited. `ForEach(0..<n)` is for constant ranges only.

### 8. `@State` vs `@StateObject` vs `@ObservedObject` — the recreated-every-render bug

A SwiftUI view struct is re-initialized constantly. Whatever you write in a property initializer runs again every time.

```swift
// BAD — a brand-new ViewModel every time the parent re-renders: state resets, in-flight loads restart
@ObservedObject var model = ViewModel()

// GOOD (ObservableObject) — @StateObject's initializer is @autoclosure @escaping: evaluated once per view identity
@StateObject private var model = ViewModel()

// GOOD (Observation, iOS 17+) — @State owns the object; plain `let` for one passed in; @Bindable for two-way binding
@State private var model = ViewModel()
```

- **Ownership rule:** the view that creates the object uses `@StateObject`/`@State`. A view that receives one uses `@ObservedObject` (or a plain `let` with `@Observable`).
- **`@State` still evaluates its initializer every re-init** and throws the result away — unlike `@StateObject`, whose autoclosure defers it. `@State private var model = ExpensiveThing()` builds and discards `ExpensiveThing` on every re-init. Inject it, or keep the initializer cheap.
- `@StateObject` guarantees one instance per view *identity*. Change the view's `id` and you get a new object — that is the mechanism, not a bug.

### 9. Observation changes what invalidates

`ObservableObject` invalidates **every** view holding the object when **any** `@Published` property changes. `@Observable` (Observation framework, iOS 17+) tracks the properties each view actually **reads during `body`**, so only those views re-render. That is the win — and the trap.

- **Only reads inside `body` are tracked.** Reading `model.count` in a button action, in `onAppear`, or inside an escaping closure registers nothing. The view will not update when it changes.
- Reading `model.items` in a parent to pass an array down makes the **parent** the observer, so the parent re-renders on every item change. Pass the model down and read in the child to keep invalidation narrow.
- An `@Observable` class must not also conform to `ObservableObject` and must not use `@Published`. Mixing them gives you the coarse invalidation back with none of the warnings.
- Use `@Bindable` for `$model.property` bindings. For non-UI observation, `Observations` (SE-0475, Swift 6.2) gives an `AsyncSequence` of changes without `withObservationTracking`'s manual re-registration.

### 10. Writing state during `body` evaluation → update loop

`body` must be a pure function of state. Writing state while SwiftUI is computing it is undefined behavior and the runtime says so:

- `Modifying state during view update, this will cause undefined behavior.`
- `Publishing changes from within view updates is not allowed, this will cause undefined behavior.`

```swift
// BAD — assignment during body; loops or corrupts the update
var body: some View { model.visitCount += 1; return Text("hi") }

// BAD — write inside the builder: size → state → re-layout → new size → loop
GeometryReader { g in { width = g.size.width; return Color.clear }() }

// GOOD (iOS 18+) — write from a real event, only when the value actually changed
.onGeometryChange(for: CGFloat.self) { $0.size.width } action: { if $0 != width { width = $0 } }
```

The same guard applies to `.onChange(of:)`: a handler that writes the value it observes is an infinite loop unless you compare first.

### 11. `onAppear` fires more than you expect

It is not `viewDidLoad`. It fires on **every** appearance: popping back to a view in a `NavigationStack`, switching tabs, and each time a row scrolls into a `List` / `LazyVStack`. `onDisappear` is not guaranteed before deallocation and does not fire on app termination.

- One-time setup goes in the model's initializer or behind an explicit `hasLoaded` flag, not in `onAppear`. A "screen viewed" event on a lazy row fires once per scroll-in, not once per screen.
- Prefer `.task` for anything async, so re-appearing cancels the previous run instead of stacking a second one.

## Lifecycle, background, and memory

### 12. Scene phase, background expiration, state restoration

- `@Environment(\.scenePhase)` gives `.active` / `.inactive` / `.background`. **The app-switcher snapshot is taken at `.inactive`** — redact sensitive content there, not at `.background`, or the stored screenshot contains it.
- On the `App` struct the phase aggregates all scenes (iPadOS multi-window). Observe it on the `Scene` or view for one window.
- `.background` gives no guaranteed time and no guaranteed callback afterwards. Persist on every meaningful change, not "on background."
- `UIApplication.beginBackgroundTask(withName:expirationHandler:)` buys a short, unguaranteed window (historically ~30 s). You **must** call `endBackgroundTask(_:)`, including from the expiration handler, or the watchdog kills the process (`0x8badf00d`).
- Restoration: `@SceneStorage` for small per-scene UI state (not durable, not secure), `@AppStorage` for preferences (also not secure), a real store for anything the user typed. Read `references/background-and-lifecycle.md`.

### 13. `BGTaskScheduler` is a request, not a schedule

```swift
// Registration must complete before application(_:didFinishLaunchingWithOptions:) returns, or it throws.
BGTaskScheduler.shared.register(forTaskWithIdentifier: "com.x.refresh", using: nil) { task in
    task.expirationHandler = { /* stop work now; setTaskCompleted(success: false) */ }
    schedule()                                  // re-submit FIRST — you may never reach the end
    Task { await refresh(); task.setTaskCompleted(success: true) }
}
```

- Every identifier also goes in `BGTaskSchedulerPermittedIdentifiers` in `Info.plist`, with the matching Background Modes capability (`fetch` for `BGAppRefreshTask`, `processing` for `BGProcessingTask`).
- **It may never run.** The system decides from usage patterns, battery, and thermals; Low Power Mode suppresses background refresh; and if the user swipes the app out of the switcher, iOS stops running its background tasks until the next manual launch. Never make correctness depend on it — it is an optimization over a foreground path that already works.
- For transfers that must complete, use a `URLSession` **background configuration**: it survives process termination and relaunches the app via `application(_:handleEventsForBackgroundURLSession:completionHandler:)`. iOS 26 also adds `BGContinuedProcessingTask` for user-started work with system progress UI (`mir-mobile/references/store-compliance.md`).
- Silent push (`"content-available": 1`; headers `apns-push-type: background`, `apns-priority: 5` — APNs rejects priority 10 here) is **best-effort**: throttled, coalesced, delayed, suppressed in Low Power Mode, and not delivered at all while the app is force-quit. It is a hint to fetch, never the delivery mechanism for data that must arrive.

### 14. Retain cycles

| Shape | The cycle | Fix |
|---|---|---|
| Escaping closure stored by `self` | `self` → closure → `self` | `{ [weak self] in guard let self else { return } … }`. Non-escaping closures cannot cycle — don't weakify them |
| Combine | `self` → `Set<AnyCancellable>` → subscription → sink → `self` | `sink { [weak self] … }` or `assign(to: &$published)`. **`assign(to:on: self)` captures `self` strongly** — the classic Combine leak |
| Delegate | `child.delegate = self` on a strong property | `weak var delegate: SomeDelegate?`, protocol constrained to `AnyObject` |
| `Timer`, `NotificationCenter.addObserver(forName:…using:)` | run loop / center → block → `self` | `invalidate()`; keep the observer token and `removeObserver`; `[weak self]` in the block |
| Long-lived `Task` | `Task` → `self` until it completes | `[weak self]`; cancel from `.onDisappear`, or use `.task` |

Verify with the Memory Graph Debugger and the Leaks instrument on a device, and keep a `deinit` log on every view model during development. A view model that never deinits is the bug, whatever the screen looks like.

## Security

iOS SDK mechanics. The platform-agnostic items (object-level authorization, mass assignment, secrets in the binary, biometric theatre, PII in logs) are in `mir-mobile`; server-side authorization is `mir-backend`. These are the Apple-specific ones. Depth: `references/ios-storage-and-privacy.md`.

| Concern | The actual failure | What to write instead |
|---|---|---|
| **Keychain accessibility class** | The default `kSecAttrAccessibleWhenUnlocked` is included in an encrypted backup, so the token restores onto a **different device**. `kSecAttrAccessibleAlways` is deprecated since iOS 12 | Choose deliberately. `…AfterFirstUnlockThisDeviceOnly` for tokens a background task reads; `…WhenUnlockedThisDeviceOnly` for foreground-only; `…WhenPasscodeSetThisDeviceOnly` for the strictest. Any `ThisDeviceOnly` class never restores elsewhere. Never set `kSecAttrSynchronizable` on a device-bound credential |
| **Keychain survives uninstall** | Deleting the app leaves items behind; the next install inherits the previous user's credentials | First launch: if a `UserDefaults` flag is absent (that *is* wiped), `SecItemDelete` everything for the service, then set it. Treat it as best-effort, not a boundary — a restore from backup returns the flag and skips the wipe, so tokens must also be short-lived and server-revocable |
| **`UserDefaults` as storage** | An unencrypted plist in the container — backed up, readable from a backup or a jailbroken filesystem, shared with every App Group extension. Also a privacy-manifest **required-reason API** | Keychain for credentials, an encrypted store for PII. `UserDefaults`/`@AppStorage` for non-secret preferences only |
| **App Group sharing** | Everything in `containerURL(forSecurityApplicationGroupIdentifier:)` is readable by every app and extension in the group, and is backed up | Secrets go in a shared **keychain access group** (`kSecAttrAccessGroup`), not a shared file. Set `FileProtectionType.complete` explicitly on files you do write there |
| **ATS exceptions** | `NSAllowsArbitraryLoads` disables TLS enforcement **app-wide**, and a debug-only exception in a shared `Info.plist` ships to the App Store | Scope to one host: `NSExceptionDomains` + `NSExceptionAllowsInsecureHTTPLoads`. `NSAllowsArbitraryLoadsInWebContent` relaxes only `WKWebView`. Grep the built app's `Info.plist`, not the source |
| **Pasteboard leakage** | `UIPasteboard.general` is shared with every app and, via Universal Clipboard, the user's other devices. A copied OTP sits there indefinitely | `setItems(_:options:)` with `.expirationDate` + `.localOnly`. iOS 16+ prompts on programmatic **reads** — use `UIPasteControl` or `detectPatterns` rather than reading blind |
| **Privacy manifest** | Missing or under-declared required-reason API (`UserDefaults`, file timestamps, boot time, free disk space, active keyboards) → `ITMS-91053`. A malformed manifest → `ITMS-91056`. A listed third-party SDK with no manifest → `ITMS-91061`; with no signature → `ITMS-91065` (two separate requirements, two separate codes) | Ship your own manifest — an SDK's does not cover your code. Take reason codes from TN3183, don't guess. Re-archive, diff the privacy report against App Store Connect after **every** dependency change |
| **URL scheme hijacking** | Any app can register `myapp://` and iOS does not define which wins — an OAuth code or magic-link token on a custom scheme can be taken by another installed app | Universal Links for anything carrying a token (`applinks:` entitlement + `apple-app-site-association` over HTTPS at `/.well-known/`, no redirects). OAuth via `ASWebAuthenticationSession` + PKCE. Validate every parameter: a link is an unauthenticated entry point that arrives at cold start |
| **`WKWebView` bridge** | A `userContentController.add(_:name:)` handler reachable from remote content; `loadFileURL(_:allowingReadAccessTo:)` scoped to the whole container | Never load an untrusted URL into a bridged web view. Allowlist origins, scope file read access to one directory, authorize every bridge call as if it came off the network |
| **Deserialization / traversal** | `NSKeyedUnarchiver.unarchiveObject(with:)` on server data; a server-supplied filename with `../` appended to the documents directory | `unarchivedObject(ofClass:from:)` with `NSSecureCoding`, or `Codable`. Generate your own filenames; `.standardized` then verify the path is still under the intended directory |
| **Logging** | `print()` compiles into release builds and redacts nothing. With `os.Logger`, dynamic strings are private by default but **numeric interpolations are public** — `logger.log("user \(userID)")` on an `Int` writes it in the clear | `os.Logger` only, with explicit `privacy: .private` / `.sensitive` on anything identifying. `.public` must be deliberate |
| **Biometrics as a boolean** | `LAContext.evaluatePolicy` returns `true`; a hooked process returns `true` too | Bind it to a key: `SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly, .biometryCurrentSet, &err)`. `.biometryCurrentSet` invalidates the item on new enrolment — handle that path |
| **Supply chain** | SPM **macros and build-tool plugins run arbitrary code at build time**, on your machine and in CI. An unpinned `binaryTarget` has no integrity check. CocoaPods Trunk goes **read-only 2 Dec 2026** — existing pods still resolve, but no new versions or security patches can be published | Commit `Package.resolved`; pin by exact version or a narrow range; require `checksum:` on every `binaryTarget`; review any package shipping a macro or plugin. Plan the move off CocoaPods before Dec 2026 |

Jailbreak detection, certificate pinning, and obfuscation raise cost; a hooked process defeats all three. Do not put an authorization decision on the device.

---

## How this slots into the pipeline

- **Gate 3 (invariants):** INV-1 "state survives process death" becomes concrete here — what is in `@SceneStorage`, what is in the store, and the exact moment it is written. Section 12.
- **Gate 5 (design):** state the module's default actor isolation (§3), the Keychain accessibility class per item (Security), the background API and its cap (§13), and the storage plan including App Group contents.
- **Gate 6 (implementation):** code against §1–14. Every `Task {}` needs an answer to "who cancels this?"
- **Gate 7 (review):** the reliability-reviewer checks §5, §6, §13, §14; the security-reviewer checks the Security table; run the store-submission check against `mir-mobile/references/store-compliance.md`. Verify §12 and §14 on a device — the simulator does not kill your process or apply thermal pressure.

## References

- `references/swift-concurrency.md` — per-error migration recipes for the Swift 6 language mode, actor reentrancy, `MainActor` migration order, structured vs unstructured concurrency, testing async code.
- `references/background-and-lifecycle.md` — scene-phase table, `beginBackgroundTask` rules, `BGTaskScheduler` registration and LLDB simulation, background `URLSession`, push types and delivery guarantees, state restoration.
- `references/ios-storage-and-privacy.md` — Keychain accessibility matrix with backup/restore behavior, App Groups, ATS keys, pasteboard options, the `PrivacyInfo.xcprivacy` audit procedure, universal-link setup and validation.

## Edit boundary (what belongs here vs. above)

1. True for Android and Flutter too (process death, idempotency, permission states, store-gate awareness)? → **up** to `mir-mobile`.
2. A mechanical footgun of the Apple SDK — Swift concurrency, SwiftUI, UIKit, Keychain, `BGTaskScheduler`, Xcode, App Store Connect? → **here**.
3. Android's SDK (Compose, WorkManager, Room, Gradle, Play Console)? → `mir-mobile-android`. Never widen this one.
4. React Native or Flutter framework mechanics? → their own module. This applies only to the Swift you write yourself.

Full edit map: `mir-mobile/SKILL.md` → "Where these instructions live".
