# Mobile failure modes

Read at **Gate 3/4** when declaring invariants and filling the Risk Register. Each entry: what breaks, why it breaks, what to write instead.

---

## 1. Process death — the failure AI never plans for

**What breaks.** The user fills a form, backgrounds the app to check a message, comes back to a blank screen. Or the app crashes on resume with a null dereference on state that was reconstructed from nothing.

**Why.** The OS reclaims memory by killing backgrounded processes. Android and iOS both do this, and **neither guarantees a callback**. On Android, `onDestroy()` is not called. On iOS, `applicationWillTerminate` is **not** called for a backgrounded app. When the user returns, the system recreates the app at the last screen — with an empty object graph. Aggressive OEM battery managers make this far more frequent on Android than a Pixel suggests.

**What to write instead.**
- Classify every piece of state: **transient UI state** (scroll position, expanded rows, selected tab) → saved-instance mechanism (`SavedStateHandle` / `rememberSaveable` on Android, `@SceneStorage` / `NSUserActivity` on iOS). **User-entered data** → the database or a file, written **as it changes**, not on navigate-away. **Server data** → refetchable; do not save it in the instance bundle.
- The saved-instance bundle is small (Android throws `TransactionTooLargeException` past roughly 1 MB across all bundles). Never put a list of models in it — put the ID and refetch.
- The restored screen has its own state: `RESTORING`. Render it. "Restore then immediately show empty" is the bug users report as "it lost my data".
- **Force it in test:** `adb shell am kill <pkg>` with the app backgrounded; enable Developer Options → "Don't keep activities". On iOS, background the app then stop it from Xcode.

---

## 2. Retry without a persisted idempotency key → duplicate writes

**What breaks.** The user taps Pay. The request reaches the server, the server charges the card, the response never arrives because the train entered a tunnel. The app retries. Two charges.

**Why.** Mobile networks drop **responses**, not just requests. A timeout tells you nothing about whether the write happened. Automatic retry in the HTTP client (or in the user's finger) turns one intent into N writes. Generating a fresh UUID inside the retry loop is the specific mistake — it produces a *different* key each attempt, which is not idempotency.

**What to write instead.**
- Generate the key **once, when the user's intent is captured**, and **persist it with the pending operation** in the local database — before the first network call. Reuse it on every retry, including retries after a process restart. Clear it only on a confirmed terminal response.
- Retry only on network errors, timeouts, `408`, `425`, `429` (honour `Retry-After`), and `5xx`. Never retry **any other** `4xx` — those are your bug, not a transient fault, and repeating them just burns the user's battery. One retry of a `401` after a successful token refresh is the single allowed exception. Cap the attempts, use exponential backoff with jitter, and stop.
- If the server does not support idempotency keys, that is a `mir-backend` task, not something the client can fix. Say so at Gate 5 rather than shipping optimistic retry.
- The corollary invariant: a screen that shows a spinner during a write must not let a second tap fire a second request. Disable the control, keyed on the pending operation, not on a local boolean that resets on recomposition.

---

## 3. Permissions are four states, not a boolean

**What breaks.** The camera button does nothing. Forever. The user denied once, the code calls `requestPermission()` again, the OS silently refuses to show the dialog, the callback returns denied, and the app shows no feedback.

**Why.** On iOS the prompt is shown **once, ever** — after that the only path is Settings. On Android, two denials mark it permanently denied and `shouldShowRequestPermissionRationale` returns false. There are also **partial grants**: Photos limited-library on iOS and the Android photo picker equivalent; Location While-Using vs Once, with Precise toggled off. And permissions can be **revoked while the app is backgrounded** — the running code holds a stale grant.

**What to write instead.**
- Render four states explicitly: `GRANTED`, `GRANTED_PARTIAL`, `DENIED` (can re-ask, show rationale first), `DENIED_PERMANENT` (show the reason and a deep link to Settings — never a dead button).
- Show rationale **before** the system prompt, not after the denial. You get one prompt on iOS; spend it after the user understands why.
- Re-check the permission on every resume, not once at screen creation.
- Design the degraded path as a real feature, not an error. "Pick from the photo picker instead" is better UX *and* avoids the Play restricted-permission declaration entirely.
- Ask at the moment of use, never at launch.

---

## 4. Background work that the OS never runs, or cuts off

**What breaks.** "Sync every 15 minutes" ships. Users report data hours stale. Or a long upload is killed at the 6-hour mark by a `RemoteServiceException`.

**Why.** iOS `BGAppRefreshTask` is opportunistic — the system decides *whether* to run it based on usage patterns, battery, and network; a low-engagement user may get it rarely or never. On Android, `dataSync` and `mediaProcessing` foreground services are capped at 6 hours per 24 (targetSdk 35+), and on Android 16 jobs running alongside a foreground service consume the JobScheduler quota, which stops WorkManager workers mid-flight. OEM battery managers add their own kills.

**What to write instead.**
- Never design a flow that *requires* background work to complete on a schedule. Background work is an optimisation; foreground sync on next open is the guarantee.
- Checkpoint. Every long operation records progress durably so a resumed run continues instead of restarting. This also makes the 6-hour cap survivable.
- Handle `Service.onTimeout()` and call `stopSelf(startId)` within seconds. Miss it and `dataSync`/`mediaProcessing` die with a fatal `RemoteServiceException`; `shortService` gets an ANR.
- Use the right API for the intent: user-initiated transfer → user-initiated data transfer job (Android, quota-exempt) / `BGContinuedProcessingTask` or a background `URLSession` (iOS). Deferrable maintenance → `WorkManager` / `BGProcessingTask`.
- Log why work stopped: `WorkInfo.getStopReason()`, `JobParameters.getStopReason()`, `ApplicationExitInfo`.

---

## 5. Offline writes, sync, and conflict

**What breaks.** The user edits offline, reconnects, and their edit vanishes — or reappears as a duplicate, or overwrites a colleague's newer edit.

**Why.** Offline support means the local store is the source of truth for a while. Without an explicit model for ordering, conflict, and acknowledgement, "sync" is a loop that replays local rows against a server that has moved on.

**What to write instead.**
- **Outbox pattern.** Local writes go into a durable, ordered queue with the idempotency key attached. Sync drains it in order. The queue survives process death because it is in the database, not in memory.
- Pick and *state* the conflict rule: last-write-wins on a server timestamp, server-wins, client-wins, or explicit user resolution. "We'll figure it out later" means silent data loss.
- Ordering matters for dependent writes — a create followed by an update on the same entity cannot be reordered or parallelised. Either serialise per entity, or make the server accept them in any order.
- Give the user a visible state: `PENDING_SYNC`, `SYNC_CONFLICT`, `SYNC_FAILED_PERMANENT`. Silent failure is the worst outcome; the user believes the data is saved.
- Permanent failures need an exit. A poison item that fails forever blocks the queue behind it — cap attempts, move it aside, surface it.
- Clock skew: device time is user-settable. Never order or expire on device time alone.

---

## 6. Storage that outlives the install

**What breaks.** A user sells their phone after "deleting" the app. The next owner reinstalls it and is logged in as them.

**Why.** iOS Keychain items persist after app deletion (long-standing, undocumented behavior). On Android the app sandbox is deleted, but Auto Backup / Backup & Restore may have already copied app data off-device, and it will be restored.

**What to write instead.**
- iOS: on first launch, check a flag in `UserDefaults` (which *is* wiped on uninstall). If absent, delete every Keychain item for your service before doing anything else, then set the flag. This is **best-effort, not a boundary**: a restore from backup brings the flag back, so the wipe is skipped. Neither the flag's absence nor the Keychain's persistence is a documented contract — pair it with short-lived, server-revocable tokens and revalidate any recovered token before you trust it.
- Choose the accessibility class deliberately: `…WhenUnlockedThisDeviceOnly` for session tokens; `…AfterFirstUnlockThisDeviceOnly` only when a background task genuinely needs the value before the user unlocks. `ThisDeviceOnly` means it will **not** survive a device migration — decide whether that is intended.
- Android: configure `android:dataExtractionRules` (and legacy `android:fullBackupContent`) to exclude credential stores, caches, and databases holding PII.
- Best answer: do not store a long-lived secret on the device at all. Short-lived tokens with server-side refresh limit what a stolen device yields.

---

## 7. Battery, cellular, and thermal

**What breaks.** A one-star review saying the app ate 40% of the battery, or a bill complaint after a 500 MB sync on a metered plan abroad.

**Why.** Polling loops, an always-on socket, high-accuracy location, unbatched wakeups, and unconditional media prefetch. None of these show up on a simulator plugged into a Mac.

**What to write instead.**
- Batch and coalesce network work; let `WorkManager` / `BGProcessingTask` constraints (`requiresCharging`, `requiresDeviceIdle`, unmetered network) do the scheduling instead of a timer.
- Check for a metered connection before large transfers and defer, with an explicit user override. State the policy at Gate 5.
- Location: request the coarsest accuracy that works, and stop updates when the screen is off or the screen is gone.
- Measure on a real device, unplugged: Android Studio Energy Profiler / `dumpsys batterystats`, Xcode Energy gauge and MetricKit. Thermal throttling only reproduces on hardware.

---

## 8. Cold start from a deep link or push

**What breaks.** Tapping a notification opens the app to a blank screen, or crashes, or opens a screen the signed-out user should not see.

**Why.** The warm path has a populated object graph and a signed-in session; the cold path has neither. AI writes and tests the warm path.

**What to write instead.**
- One route handler used by both paths. It must handle: not signed in (route to auth, then continue to the intended destination), stale/deleted target entity, malformed parameters, and a link claiming a permission the account does not have.
- Treat every deep-link parameter as untrusted input. Validate before use — never pass it straight into a WebView, a file path, or a query.
- Test by killing the app first, every time.

---

## 9. Old clients live forever

**What breaks.** A server change ships, and users on a 14-month-old build start crashing on a field that is now missing.

**Why.** There is no forced upgrade on mobile. Users stay on old builds for years, and on iOS a fix takes a review cycle to reach them.

**What to write instead.**
- Additive API evolution only, for as long as you support the version. Never remove or retype a field an old client reads.
- Clients ignore unknown fields by default — verify this is actually true of your decoder rather than assuming it.
- Version the API or the payload, and keep a documented minimum supported app version that the server team agrees to.
- Ship a remote kill switch / feature flag for every risky new client path, so a bad release can be turned off without a store submission.

---

## Quick invariant starters

Copy and adapt at Gate 3:

> INV: Any user-entered text is durable within one second of the keystroke that produced it.
> INV: Exactly one server-side write results from one user intent, regardless of retries or process restarts.
> INV: No interactive control is ever inert — every disabled state carries a visible reason and a next action.
> INV: No screen renders server data while offline without a stale indicator.
> INV: No credential is readable by the next installation of the app.
> INV: Every long-running operation resumes from its last checkpoint rather than restarting.
