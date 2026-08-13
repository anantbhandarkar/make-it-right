---
name: mir-mobile-android
description: "Make It Right (Android module). Kotlin + Jetpack Compose + coroutines mechanics for the mir-mobile pillar — the Android-specific footguns the platform-agnostic skill omits: what SavedStateHandle and rememberSaveable actually survive (process death) versus rotation only, repeatOnLifecycle and collectAsStateWithLifecycle, viewModelScope vs lifecycleScope, Compose recomposition storms and the LaunchedEffect wrong-key bug, WorkManager plus the mandatory android:foregroundServiceType and the dataSync 6h/24h cap, permanently-denied runtime permissions, predictive back under targetSdk 36, and Android security (Keystore, SharedPreferences is not secure storage, android:exported, intent redirection, PendingIntent FLAG_IMMUTABLE, App Links assetlinks.json, WebView JS interfaces). Chains: mir-mobile → this. TRIGGER when the mobile target is native Android — Kotlin, Jetpack Compose, ViewModel, Room, DataStore, WorkManager, Gradle/R8, AndroidManifest, Play Console. In React Native or Flutter apps, TRIGGER for the native Android layer (manifest, Gradle/AGP, R8, targetSdk migration, service and permission declarations, signing, Play Console), skipping Compose/Kotlin sections during JS/Dart work. SKIP for iOS/Swift/SwiftUI (mir-mobile-ios), for mobile web and PWAs (mir-frontend), and for the server API the app calls (mir-backend)."
trigger: /mir-mobile-android
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-mobile-android · Make It Right (Android)

Platform module. `mir-mobile` decides **what is correct** on any mobile platform; this carries **Android SDK and Jetpack mechanics**. Load order: `mir-mobile` → `mir-mobile-android`. Reach for this at Gate 5 (design mechanics), Gate 6 (implementation), Gate 7 (review). Store deadlines and cross-platform behavior-change lists live in `mir-mobile/references/store-compliance.md` — read that, don't restate it here.

**Stack assumed**, versions verified 13 Aug 2026 against `developer.android.com` and `kotlinlang.org`:

| Component | Version | Note |
|---|---|---|
| Compose BOM | **2026.08.00** (12 Aug 2026) | maps to `compose.ui`/`runtime`/`foundation` **1.12.0**, `material3` **1.4.0** |
| Kotlin | **2.4.10** (14 Jul 2026) | Compose compiler ships inside the Kotlin plugin; there is no separate `compose.compiler` version to pin past 1.5.15 |
| androidx.lifecycle | **2.11.0** (17 Jun 2026) | `SavedStateHandle` is KMP-compatible since 2.9.0 |
| androidx.work | **2.11.2** (25 Mar 2026) | 2.12.0-rc01 exists (12 Aug 2026) — not stable, don't design on it |
| compileSdk / targetSdk | **36** | Play requires new apps *and updates* to target API 36 from **31 Aug 2026**; extension on request to 1 Nov 2026 |
| androidx.security:security-crypto | 1.1.0 (30 Jul 2025) | **every API deprecated** as of 1.1.0-beta01 (June 2025). The stable release ships deprecated |

## Lifecycle and process death

### 1. Configuration change and process death are different events — know what survives which

The single most common Android state bug is code that survives rotation in testing and loses data in production.

| Where state lives | Survives recomposition | Survives config change (rotate, dark mode, locale, font size) | Survives process death |
|---|---|---|---|
| local `var`, `remember { }` | ✅ (remember only) | ❌ | ❌ |
| Activity/Fragment field | ❌ | ❌ | ❌ |
| ViewModel property, `viewModelScope` | ✅ | ✅ | ❌ |
| `rememberSaveable`, `SavedStateHandle` | ✅ | ✅ | ✅ (via the saved-instance `Bundle`) |
| Room / DataStore / a file | ✅ | ✅ | ✅ (and app restart, and reboot) |

`SavedStateHandle` is not "the ViewModel's state" — it is the Bundle. It only holds what can be parceled, and **all bundles for a process share roughly a 1 MB limit** (`TransactionTooLargeException` past it). Save the ID, refetch the object. Never put a list of models in it.

### 2. "It worked in dev because the process never died"

The debugger holds the process alive, and nobody in development backgrounds the app for 20 minutes on a low-RAM device. So the process-death branch ships untested and the OEM battery manager finds it on day one.

Force it, every feature, before you call it done:

```bash
adb shell am kill com.example.app          # background the app first; SIGKILL, no onDestroy
adb shell settings put global always_finish_activities 1   # "Don't keep activities"
```

User-entered text goes to Room or DataStore **as it changes**, not in `onStop()`. There is no guaranteed exit callback: `onDestroy()` is not called on process death, and `onSaveInstanceState()` only runs when the system *chooses* to save, not when it kills.

### 3. Predictive back — `onBackPressed()` is dead at targetSdk 36

For apps targeting API 36 on Android 16+, `onBackPressed()` **is not called** and `KeyEvent.KEYCODE_BACK` **is not dispatched**. An app that intercepts back that way silently loses every custom back behavior — unsaved-changes dialogs, closing a sheet, popping an in-screen step.

- Views: `OnBackPressedDispatcher.addCallback(owner, OnBackPressedCallback(enabled) { ... })`. Tie `isEnabled` to observable state, one callback per responsibility.
- Compose: `BackHandler(enabled) { }`, or `PredictiveBackHandler(enabled) { progress -> ... }` when you animate the gesture and must handle `CancellationException` (the user let go).
- `android:enableOnBackInvokedCallback="false"` is a temporary escape hatch, not a fix. Record it in the Assumption Ledger with a removal date.

Edge-to-edge is also enforced at targetSdk 36 with no opt-out, and orientation/resizability restrictions are ignored on displays ≥ 600dp — so "we lock it to portrait" is no longer a design you can rely on.

## Coroutines

### 4. Pick the scope by what should cancel it

| Scope | Cancels when | Use for |
|---|---|---|
| `viewModelScope` | ViewModel `onCleared()` — **survives config change** | screen-scoped work: loading, form submission |
| `lifecycleScope` | the owner is destroyed — **a rotation destroys it** | UI-only work that is meaningless after the view is gone |
| `GlobalScope` | never | nothing. It leaks and outlives every cancel |
| `WorkManager` | not by scope — it persists across process death and reboot, and the system reschedules | anything that must survive the screen: upload, sync, receipt |

`lifecycleScope.launch { uploadFile() }` restarts the upload on every rotation. Work that must finish is not a coroutine scope problem — it is a WorkManager job.

### 5. `repeatOnLifecycle` — collecting in `onCreate` is a battery bug, not a style choice

`lifecycleScope.launch { viewModel.state.collect { render(it) } }` in `onCreate` keeps collecting while the app is in the background. The upstream stays hot: the location callback, the socket, the Room query observer all keep producing, and you burn battery rendering into a view nobody can see.

```kotlin
// WRONG — collector never stops; upstream stays active off-screen
lifecycleScope.launch { viewModel.state.collect { render(it) } }

// RIGHT — cancelled at STOPPED, restarted at STARTED
lifecycleScope.launch {
    repeatOnLifecycle(Lifecycle.State.STARTED) {
        viewModel.state.collect { render(it) }
    }
}

// RIGHT in Compose — never plain collectAsState() for a hot upstream
val state by viewModel.state.collectAsStateWithLifecycle()
```

`launchWhenStarted` / `whenStarted` are deprecated and were never equivalent: they *suspend* the collector but leave the upstream producing. `flowWithLifecycle(lifecycle)` is the single-flow form. On the producer side, `stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), initial)` gives the upstream a 5-second grace period so a rotation doesn't tear down and re-run the query.

### 6. Cancellation is cooperative — and `catch (e: Exception)` breaks it

A cancelled coroutine keeps running until it hits a suspension point that checks. A tight CPU loop, a blocking JNI call, or `Thread.sleep` never notices.

```kotlin
// WRONG — swallows CancellationException; the parent thinks the child is still alive
try { doWork() } catch (e: Exception) { log(e) }

// RIGHT — let cancellation propagate
try { doWork() }
catch (e: CancellationException) { throw e }
catch (e: Exception) { log(e) }
```

- Call `ensureActive()` or `yield()` inside long loops; check `isActive`.
- Release resources in `finally`. Suspending work inside `finally` after cancellation needs `withContext(NonCancellable)` — and only for cleanup, never to keep real work alive.
- `coroutineScope { }` fails the whole block if any child fails; `supervisorScope { }` isolates siblings. Picking the wrong one is how one failed image load kills a whole screen load.
- `Dispatchers.IO` defaults to `max(64, CPU count)` parallelism. Blocking all of it stalls every other I/O in the process — use `limitedParallelism(n)` for a chatty subsystem.

## Compose

### 7. Unstable parameters cause recomposition storms — even with strong skipping on

Strong skipping has been on by default since **Kotlin 2.0.20**. It makes every restartable composable skippable, but it compares **unstable** parameters by *instance identity* (`===`), not `equals`. So an unstable value rebuilt each frame is always "different".

```kotlin
// WRONG — a new List instance every recomposition; ItemList never skips
ItemList(items = allItems.filter { it.visible })

// RIGHT — hoist the derivation, or make the type stable
val visible = remember(allItems) { allItems.filter { it.visible }.toImmutableList() }
ItemList(items = visible)
```

- `List`, `Set`, `Map` are always unstable. Use `kotlinx.collections.immutable` (`ImmutableList`), or annotate your model `@Immutable`.
- A `data class` with any `var` is unstable. Types from a module where the Compose compiler doesn't run are unstable.
- **Defer state reads to the smallest scope that needs them.** Reading a fast-changing state (scroll offset, animation value) at the top of a screen recomposes the whole subtree. Read it inside a lambda-taking modifier — `Modifier.offset { IntOffset(scroll.value, 0) }`, `Modifier.graphicsLayer { alpha = fade.value }` — so only layout or draw re-runs.
- Diagnose with Compose compiler metrics and Layout Inspector recomposition counts. See `references/android-state-and-compose.md`.

### 8. `remember` vs `rememberSaveable`

`remember` survives recomposition only. It is gone on rotation and on process death. `rememberSaveable` writes into the saved-instance Bundle, so it survives both — at the cost of the Bundle limit and a `Saver` requirement.

```kotlin
var query by rememberSaveable { mutableStateOf("") }         // user input — must survive
val formatter = remember { NumberFormat.getInstance() }       // derived object — must not be saved
var rows by rememberSaveable(stateSaver = RowsSaver) { ... }  // non-Parcelable needs a Saver
```

Never `rememberSaveable` a large list, a bitmap, or a lambda. Never `remember` something the user typed.

### 9. `derivedStateOf` is for high-frequency in, low-frequency out

Use it when a state that changes constantly produces a result that rarely changes:

```kotlin
val showScrollToTop by remember { derivedStateOf { listState.firstVisibleItemIndex > 0 } }
```

Two failure shapes:
- Using it as a general memo. `remember(a, b) { expensive(a, b) }` is the memo. `derivedStateOf` adds a snapshot observer and an allocation for nothing.
- **Capturing parameters instead of state.** `remember { derivedStateOf { a + b } }` where `a` and `b` are function parameters never updates — the lambda captured the values from first composition. It must read `State` objects, or the `remember` needs keys.

### 10. Effect keys — the wrong key is the bug

```kotlin
// WRONG — never re-keys; navigating to a different user shows the previous user's data
LaunchedEffect(Unit) { viewModel.load(userId) }

// RIGHT — cancels and relaunches when the identity changes
LaunchedEffect(userId) { viewModel.load(userId) }
```

The opposite failure is just as common: keying on an unstable object restarts the effect every recomposition, cancelling in-flight work each frame.

- `DisposableEffect(key)` for anything that must be unregistered — `onDispose` is mandatory, and a missing one leaks a listener per key change.
- `SideEffect` runs after **every** successful recomposition. It is for publishing state to a non-Compose object, not a "run once" hook.
- `rememberUpdatedState(callback)` when a long-lived `LaunchedEffect(Unit)` must call the newest lambda without restarting.
- Never launch a coroutine from `remember { }` (use `rememberCoroutineScope()` for events) and never mutate state during composition.

Full effect decision table and the `rememberUpdatedState` pattern: `references/android-state-and-compose.md`.

### 11. State hoisting and per-row state

Compose keys `remember` by position in the composition. `remember { mutableStateOf(false) }` inside a list item means the expanded flag belongs to *slot 3*, not to *that row* — reorder or filter the list and the state stays behind on whatever now occupies slot 3.

```kotlin
LazyColumn {
    items(rows, key = { it.id }) { row -> RowItem(row) }   // stable identity
}
```

Hoist anything the ViewModel or a sibling must read (selection, checked set, current tab). Keep purely local, non-critical state (a ripple, a local animation) where it is. The rule: **state goes up to the lowest common ancestor that reads it, events go down as lambdas.**

## Background work

### 12. WorkManager is the *persistent*-work API — persistent is not the same as guaranteed

Deferrable work that must eventually happen goes in WorkManager, with `Constraints` instead of your own timer, and `enqueueUniqueWork(name, ExistingWorkPolicy.KEEP, …)` so re-entering a screen doesn't enqueue a duplicate. It survives process death and reboot. It does **not** promise a deadline or eventual completion: quota, force-stop, cancellation, replacement, and a constraint that never becomes true can all leave the work unrun indefinitely.

- **Workers must be idempotent.** `Result.retry()` re-runs the whole `doWork()` from the top.
- Log why work stopped: `WorkInfo.getStopReason()` / `ListenableWorker.getStopReason()`. `STOP_REASON_FOREGROUND_SERVICE_TIMEOUT`, `STOP_REASON_QUOTA` and `STOP_REASON_CONSTRAINT_*` are what you will actually see. Without it you cannot tell "never ran" from "ran and was killed".
- **On Android 16, jobs running concurrently with a foreground service consume the JobScheduler runtime quota** — regardless of your target. A long worker can be stopped mid-flight, so checkpoint and resume; a worker that restarts from zero re-downloads on the user's data plan.
- `setExpedited(OutOfQuotaPolicy…)` is quota-limited and is not a foreground service. For a user-triggered transfer use a user-initiated data transfer job.
- Periodic work has a 15-minute minimum and is not a timer — it fires *within* the interval, batched with other apps.

Worker code, backoff, and the stop-reason list: `references/android-background-and-permissions.md`.

### 13. Foreground service types are mandatory, and two of them have a hard cap

Since **API 34** every `<service>` started in the foreground needs `android:foregroundServiceType` in the manifest **and** the matching `FOREGROUND_SERVICE_*` permission, **and** the `startForeground()` call must pass the same type. Miss any of the three and you get a `MissingForegroundServiceTypeException` / `SecurityException` at runtime — not at build time.

Before any of that, check *where* you are starting it from. **Since API 31 you cannot start a foreground service while the app is in the background at all** — from a `BroadcastReceiver`, an `AlarmManager` callback, or a coroutine that resumes after the user leaves — and you get `ForegroundServiceStartNotAllowedException`. This is the most common foreground-service crash in production, and it does not reproduce in development because your test app is on screen. There is a short exemption list (a high-priority FCM message, an exact alarm, a visible activity); if you are not on it, the answer is WorkManager or a user-initiated data transfer job, not a retry.

```xml
<uses-permission android:name="android.permission.FOREGROUND_SERVICE" />
<uses-permission android:name="android.permission.FOREGROUND_SERVICE_DATA_SYNC" />
<service android:name=".SyncService" android:foregroundServiceType="dataSync" android:exported="false" />
```

`dataSync` and `mediaProcessing` are capped at **6 background hours per 24**, tracked per type across all your services; foregrounding the app resets the allowance. At the cap the system calls `Service.onTimeout(int, int)` and you have **seconds** to `stopSelf(startId)` or the process dies with a fatal `RemoteServiceException` — a crash, not an ANR. `shortService` is ~3 minutes, cannot start another foreground service, and overrunning *it* is an ANR. Play Console requires a declaration for the type you use. Full type table and test commands: `references/android-background-and-permissions.md`.

### 14. Doze and app standby buckets decide whether your work runs at all

Doze batches network access and alarms into maintenance windows. App standby buckets (active / working set / frequent / rare / restricted) cap jobs and alarms per day; **restricted is roughly one job per day**, and users can put your app there manually. OEM battery managers on Xiaomi, Huawei, OPPO and vivo are stricter than AOSP and are not configurable from your app.

Never design a flow that requires background work on a schedule. Background work is an optimisation; a foreground sync on next open is the guarantee. Test the bad case:

```bash
adb shell dumpsys deviceidle force-idle
adb shell am set-standby-bucket com.example.app restricted
```

### 15. Exact alarms are a permission you will not have

- `SCHEDULE_EXACT_ALARM` is **not pre-granted to new installs on Android 14+ devices for apps targeting API 33 or higher**, and the user can revoke it at any time. `setAlarmClock()` is an exact-alarm API too — it needs the permission, and throws `SecurityException` without it.
- `USE_EXACT_ALARM` is auto-granted and non-revocable but is restricted to alarm-clock and calendar-style apps, and is a Play policy matter — declaring it in a generic app is a review risk.
- Always branch on `alarmManager.canScheduleExactAlarms()` with a real inexact fallback (`setAndAllowWhileIdle`), and register a receiver for `AlarmManager.ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED` to reschedule on **grant**. Revocation sends no broadcast — it force-stops your app and deletes its exact alarms — so re-check on every start.
- **Alarms do not survive reboot.** Reschedule from a `BOOT_COMPLETED` receiver, from durable state — not from memory.
- An exact alarm is not a background-work scheduler. If it is not a user-visible clock event, it is a WorkManager job.

## Permissions

### 16. The permanently-denied branch, and the API that does not tell you about it

Use `registerForActivityResult(ActivityResultContracts.RequestPermission())`. Show rationale **before** the prompt.

The trap: `shouldShowRequestPermissionRationale()` returns `false` **both** before the very first request **and** after a permanent denial. It is not a state test on its own — persist "have we ever asked" yourself in DataStore and combine the two. `hasAsked && !shouldShowRationale && !granted` is the permanently-denied state: render the consequence plus a deep link to `Settings.ACTION_APPLICATION_DETAILS_SETTINGS`, and never re-request. A silent re-request there does nothing visible, so the button just looks broken.

Also: re-check on every resume (permissions can be revoked while backgrounded, and unused-app auto-revoke exists); `POST_NOTIFICATIONS` is a runtime permission since Android 13; `ACCESS_BACKGROUND_LOCATION` must be a **separate, later** request after foreground location is granted — bundling them fails. Full state table: `references/android-background-and-permissions.md`.

### 17. Photo picker first, media permissions almost never

`ActivityResultContracts.PickVisualMedia` / `PickMultipleVisualMedia` needs **no permission at all**, so it also skips the Play declaration. Take a persistable grant with `contentResolver.takePersistableUriPermission(uri, FLAG_GRANT_READ_URI_PERMISSION)` if you need the URI past the session (max 5,000 grants).

`READ_MEDIA_IMAGES` / `READ_MEDIA_VIDEO` are Play-restricted and require a Console declaration; "we have our own gallery UI" is not an exemption. If you genuinely need them, on Android 14+ you must also request `READ_MEDIA_VISUAL_USER_SELECTED` — a grant of *only* that means **selected photos only**, the set can change between sessions, and your code must handle the library shrinking under it. `MANAGE_EXTERNAL_STORAGE` is effectively unapprovable for a normal app.

The **privacy dashboard** shows the user every permission access attributed to your app, including ones a third-party SDK made. Audit your own with data access auditing (`AppOpsManager.setOnOpNotedCallback`) before a user finds a camera access you did not know about.

## Security

Android SDK mechanics. Platform-agnostic mobile security (secrets in the binary, object-level authorization, idempotency, biometric theatre) is in `mir-mobile`.

| Concern | The actual failure | What to write instead |
|---|---|---|
| **"Encrypted" preferences** | `SharedPreferences` and DataStore are plain files in the sandbox — readable on a rooted device, copied by Auto Backup. `androidx.security:security-crypto` (`EncryptedSharedPreferences`, `EncryptedFile`) had **every API deprecated at 1.1.0-beta01, June 2025**; no drop-in successor | A Keystore key (`KeyGenParameterSpec`, `setUserAuthenticationRequired(true)`, StrongBox when `FEATURE_STRONGBOX_KEYSTORE` is present) doing AES-GCM over DataStore, or Tink. Better: a short-lived token you can re-mint |
| **Auto Backup exfiltration** | Backup & Restore copies app data off-device by default; the token database goes with it | `android:dataExtractionRules` (API 31+) *and* `android:fullBackupContent` excluding credential stores and caches; `android:allowBackup="false"` if nothing should leave |
| **Exported components** | A `<service>`/`<receiver>` with an intent-filter is **exported by default** — any app can start it | `android:exported="false"` explicitly (API 31+ won't install without the attribute). Guard the genuinely public ones with a `signature` permission |
| **Intent redirection** | `startActivity(intent.getParcelableExtra("next"))` lets a caller reach your unexported components and inherit your URI grants | `IntentSanitizer.Builder()…sanitizeByThrowing(intent)`, or check `resolveActivity(pm)` against an allowlist and strip `FLAG_GRANT_*_URI_PERMISSION`. Android 16 hardens this by default — **never** add `removeLaunchSecurityProtection()`. Detect with StrictMode `detectUnsafeIntentLaunch()` |
| **Implicit intents leaking data** | An implicit broadcast or `startService` carrying user data in the extras reaches whatever app registered for it | Explicit intents, or `intent.setPackage(packageName)`. In-process events use a `SharedFlow` (`LocalBroadcastManager` is deprecated). API 34+ `registerReceiver` requires `RECEIVER_EXPORTED`/`RECEIVER_NOT_EXPORTED` |
| **PendingIntent mutability** | A mutable `PendingIntent` over an empty base Intent lets the receiver fill in the component and act as you | `PendingIntent.FLAG_IMMUTABLE` (required since API 31). `FLAG_MUTABLE` only where the platform demands it (direct reply, bubbles), and then set an explicit component |
| **Deep links** | Any installed app can claim `myapp://` — the OAuth code lands in the attacker's app | Verified **App Links**: `android:autoVerify="true"` + `https://<domain>/.well-known/assetlinks.json` with the **Play App Signing** SHA-256 fingerprint (using the upload key is why verification silently fails). Check `adb shell pm get-app-links <pkg>`. Validate every parameter |
| **WebView JS bridge** | `addJavascriptInterface` exposes `@JavascriptInterface` methods to **whatever page loads**. Pair that with a URL from a deep link and remote content calls your native code | Don't bridge a WebView that can load remote content. If unavoidable: allowlist origins in `shouldOverrideUrlLoading`, `setJavaScriptEnabled` only when needed, re-check the origin inside every bridge method |
| **WebView file access** | A loaded `file://` page with `setAllowFileAccessFromFileURLs` / `setAllowUniversalAccessFromFileURLs` reads the app sandbox and posts it out | Set `setAllowFileAccess`, `setAllowFileAccessFromFileURLs`, `setAllowUniversalAccessFromFileURLs` all `false`; serve bundled content via `WebViewAssetLoader` on `https://appassets.androidplatform.net`. Never `onReceivedSslError { it.proceed() }` |
| **Cleartext and trust anchors** | A debug network security config with `<certificates src="user"/>` merged into release trusts every user-installed proxy CA. `android:usesCleartextTraffic="true"` copied from a sample | `res/xml/network_security_config.xml` per build variant; user trust anchors in **debug only**. Android 17 enforces Certificate Transparency by default — don't disable it. If you pin, `<pin-set>` needs a backup pin and an expiry or key rotation bricks installed apps |
| **Tapjacking** | An overlay sits over your consent or payment confirmation; the user taps what they see and your view gets the touch | `android:filterTouchesWhenObscured="true"` / `setFilterTouchesWhenObscured(true)` on the host view or `ComposeView` for any confirmation UI. `FLAG_SECURE` on windows showing secrets — it also blocks screenshots, recording, and the recents thumbnail |
| **SQL injection** | Room `@RawQuery` / `SupportSQLiteQuery` built by concatenation; a `ContentProvider.query` selection concatenated from the caller's arguments | Bound args only (`?` + `selectionArgs`); allowlist column names for dynamic `ORDER BY`. An exported provider must do its own authorization — being exported is not authentication |
| **Path traversal** | A server- or intent-supplied filename written under `filesDir`; a zip entry named `../../databases/app.db` | Reject anything that is not a plain filename; assert `file.canonicalPath.startsWith(dir.canonicalPath + File.separator)`. Cap extracted size and entry count |
| **Deserialization from intents** | Untyped `intent.getParcelableExtra(key)` (deprecated on API 33+) and `Serializable` extras instantiate attacker-chosen types | `IntentCompat.getParcelableExtra(intent, key, Foo::class.java)` and `BundleCompat` typed getters, then validate every field. Never `ObjectInputStream` on data that crossed a process boundary |
| **Supply chain** | A Gradle plugin or unpinned transitive dependency runs code at build time on your CI machine | Version catalogs with exact versions (no `+`, no ranges), a committed lockfile, `gradle/verification-metadata.xml`, and review for new plugins |
| **Root / tamper detection** | Play Integrity verdicts and root checks used as an authorization decision — bypassable on a rooted device, and they false-negative for real users | Integrity signals feed server-side risk scoring, never the decision. R8 renames, it does not encrypt — anything in the APK is public. **The only boundary is the server** |

---

## How this slots into the pipeline

- **Gate 3 (invariants / state machine):** the survival matrix in footgun 1 *is* the "what survives process death" answer. State the store for every piece of state before writing a ViewModel.
- **Gate 5 (design):** name the background API (WorkManager job vs foreground service type vs exact alarm) and the cap that applies to it. Name the permission states you will render. Name the storage for each secret.
- **Gate 6 (implementation):** code against footguns 1–17. Read `references/android-state-and-compose.md` for the Compose effect and saved-state code, `references/android-background-and-permissions.md` for the WorkManager, foreground-service and permission recipes.
- **Gate 7 (review):** the reliability-reviewer works footguns 1–15; the security-reviewer works the Security table. Run the `adb` process-death, Doze, and standby-bucket commands above on a real device — the emulator does not reproduce OEM kills.

## Edit boundary

1. True for iOS and Flutter and React Native too (idempotency, permission-as-four-states, background work is never guaranteed, store deadlines)? → **up** to `mir-mobile`.
2. A mechanical footgun of the Android SDK, Jetpack, Kotlin coroutines, or Compose? → **here**.
3. iOS SDK mechanics (SwiftUI, `@SceneStorage`, `BGTaskScheduler`, Keychain APIs)? → `mir-mobile-ios`.
4. A cross-platform framework's own layer (React Native TurboModules, Flutter platform channels)? → its own `mir-mobile-android-<framework>` module. Never widen this one.

Dated store deadlines and targetSdk behavior-change lists belong in `mir-mobile/references/store-compliance.md`, not here — one place, one date to re-verify.

## References

- `references/android-state-and-compose.md` — process-death test procedure, `SavedStateHandle` and custom `Saver` code, the full Compose side-effect decision table, recomposition diagnosis with compiler metrics and Layout Inspector.
- `references/android-background-and-permissions.md` — foreground service type table with permissions and caps, WorkManager recipes and stop reasons, Doze/bucket test commands, the permission state machine, photo picker and partial media access code.
