# iOS lifecycle, background execution, and push

Read at **Gate 5** (choosing the background API) and **Gate 7** (verifying it on a device).

**Verified 13 Aug 2026** against Apple's developer documentation and upcoming-requirements page. Apple does not publish hard numbers for background budgets; where a duration appears below it is the long-observed behavior, not a contract. Design so that missing the window is correct, not fatal.

---

## 1. Scene phases

| Phase | When | What to do |
|---|---|---|
| `.active` | Frontmost, receiving events | Resume timers, refresh stale data |
| `.inactive` | Transitioning — incoming call, Control Center, **app-switcher snapshot**, multitasking drag | **Redact sensitive content here.** Pause video and games |
| `.background` | No longer on screen | Nothing new should start. You have no guaranteed time and no guaranteed later callback |

```swift
@Environment(\.scenePhase) private var scenePhase
…
.onChange(of: scenePhase) { _, phase in
    isRedacted = (phase != .active)     // covers the snapshot
}
```

On the `App` struct the phase is the aggregate of all scenes; on iPadOS with two windows open, backgrounding one does not move the app-level phase. Observe on the `Scene` or the view when you need per-window behavior.

**Process death has no callback.** `applicationWillTerminate` is only delivered when the system terminates a *foreground* app. A backgrounded app is killed silently. Anything you "save on exit" is not saved.

---

## 2. Finishing foreground work after backgrounding

```swift
var bgTask: UIBackgroundTaskIdentifier = .invalid
bgTask = UIApplication.shared.beginBackgroundTask(withName: "flush-outbox") {
    // Expiration handler. Runs on the main thread. Stop immediately.
    UIApplication.shared.endBackgroundTask(bgTask); bgTask = .invalid
}
defer { UIApplication.shared.endBackgroundTask(bgTask); bgTask = .invalid }
```

- Historically about 30 seconds, and the system may give less under memory or thermal pressure.
- Failing to call `endBackgroundTask` is a watchdog kill (`0x8badf00d` in the crash report).
- This is for *finishing* something already started. It is not a way to run periodic work.

---

## 3. `BGTaskScheduler`

| Type | Purpose | Constraints you can request |
|---|---|---|
| `BGAppRefreshTaskRequest` | Short refresh of content (seconds) | `earliestBeginDate` |
| `BGProcessingTaskRequest` | Longer maintenance (minutes) | `requiresNetworkConnectivity`, `requiresExternalPower` |
| `BGContinuedProcessingTaskRequest` (iOS 26+) | Continue user-started work with system progress UI the user can cancel | Must begin from an explicit user action; GPU access needs an entitlement and a `supportedResources` check |

Setup, all three required or the task never fires:

1. Background Modes capability: **Background fetch** for app refresh, **Background processing** for processing.
2. `BGTaskSchedulerPermittedIdentifiers` array in `Info.plist` containing every identifier.
3. `register(forTaskWithIdentifier:using:launchHandler:)` called **before** `application(_:didFinishLaunchingWithOptions:)` returns. Registering later throws.

Inside the handler: set `expirationHandler` first, submit the *next* request second, do the work third, and always call `setTaskCompleted(success:)` exactly once.

**Testing it.** You cannot wait for the scheduler. Pause in the debugger after the request is submitted and run:

```
e -l objc -- (void)[[BGTaskScheduler sharedScheduler] _simulateLaunchForTaskWithIdentifier:@"com.x.refresh"]
```

and the expiration path with `_simulateExpirationForTaskWithIdentifier:`. Both are private and debug-only — never ship a call to them.

**What suppresses it:** Low Power Mode, low battery, thermal pressure, an app the user rarely opens, and — decisively — the user force-quitting the app from the switcher, after which iOS runs no background tasks for it until the next manual launch.

---

## 4. Background `URLSession` — the reliable path

For uploads and downloads that must complete, this is the only mechanism that survives process termination.

- `URLSessionConfiguration.background(withIdentifier:)`, one identifier per session, recreated with the same identifier on relaunch.
- Delegate-based only. Completion-handler convenience methods are unavailable on a background session.
- The system relaunches the app **into the background** and calls `application(_:handleEventsForBackgroundURLSession:completionHandler:)`. Store that completion handler and call it after `urlSessionDidFinishEvents(forBackgroundURLSession:)`, on the main thread, or the app is killed.
- `isDiscretionary` and `earliestBeginDate` let the system pick a good moment; set `isDiscretionary = false` only for user-initiated transfers.
- Uploads must be from a **file**, not `Data`, or they will not survive termination.

---

## 5. Push

| Type | Headers | Guarantee |
|---|---|---|
| Alert | `apns-push-type: alert`, priority 10 | Best effort even once APNs accepts it — it may be stored, throttled, collapsed, delayed, or discarded. The app is not woken unless the user interacts |
| Background / silent | `apns-push-type: background`, **priority 5** (10 is rejected), payload `"content-available": 1` | Best-effort. Throttled, coalesced, delayed, suppressed in Low Power Mode, and **not delivered while the app is force-quit** |
| VoIP | `apns-push-type: voip` | Wakes the app, but you must report a call to CallKit immediately or the app is terminated and can lose the entitlement. Do not use it as a general wake-up |
| Liveactivity | `apns-push-type: liveactivity` | Guideline 4.5.3 bars using Live Activities to spam or phish |

- `apns-collapse-id` replaces an undelivered push with the same id — use it for status updates, not for a queue of distinct events.
- `apns-expiration: 0` means deliver now or discard; a non-zero value stores and forwards.
- A Notification Service Extension has a short window (~30 s) to mutate the payload and **must** call the content handler; if it does not, the original payload is shown.
- Design rule: the push carries a *hint*, the app fetches the truth. A payload that is the only copy of the data is a lost write.

---

## 6. State restoration

| Mechanism | Holds | Survives |
|---|---|---|
| `@SceneStorage` | Small, reconstructible per-scene UI state (selected tab, scroll target) | Process death; **not** app deletion, and it is not encrypted |
| `@AppStorage` / `UserDefaults` | Preferences | Process death and reinstall-from-backup; not secure |
| `NSUserActivity` (`userActivity(_:element:_:)`, `onContinueUserActivity`) | Deep-link-shaped state; also drives Handoff and Spotlight | Process death; crosses devices if you opt in |
| Your own store (SwiftData, Core Data, files) | Anything the user typed or paid for | Process death, relaunch, reboot — and device migration if it is in the backup. **Not** app deletion, and not a failed or destructive schema migration |

`@SceneStorage` values must be small — it is not a document store, and putting a large blob there is a startup cost on every scene restore. Never put a token or PII in `@SceneStorage` or `@AppStorage`. **Draft text the user typed is not scene state**: scene restoration can be absent, discarded, or reset, so a draft goes in the real store and `@SceneStorage` holds at most a pointer to it.

The rule from Gate 3 (INV-1): **write on change, not on exit.** Debounce a text field to a few hundred milliseconds and persist; do not wait for `onDisappear` or `.background`.
