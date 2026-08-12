# Mobile platform & approach selection map

Used at **Gate 0** to catch a **platform/approach vs workload mismatch** before any code — and to know which `mir-mobile-<platform>` module to load.

## How to use this at Gate 0

1. Identify the target platforms and the implementation approach (chosen or implied) from the task or the existing project.
2. Check the workload against **"Do NOT use when…"** below. If the chosen approach lands in its own anti-pattern column, **stop and flag it** — that is a platform-level defect no amount of correct code fixes. Example: "This is a real-time AR feature with heavy per-frame camera processing on React Native — the bridge and JS thread are the wrong tool; write it native and expose it as a TurboModule. Proceed on RN anyway, or reconsider?"
3. Record the outcome in the Assumption Ledger as a **conscious choice**, never a silent default.
4. Load `mir-mobile-android` and/or `mir-mobile-ios` at Gate 5.

A mismatch is not an automatic blocker — team skills, hiring, and existing code are valid reasons. It must be surfaced, not discovered in production.

---

## Part 1 — Android vs iOS: what differs and why it matters

The two platforms disagree on almost every constraint this skill gates on. Design decisions that are correct on one are wrong on the other.

| Concern | Android | iOS | Consequence for the design |
|---|---|---|---|
| **Process death** | Aggressive and OEM-specific. Chinese OEM skins (Xiaomi, Huawei, OPPO, vivo) kill backgrounded apps far sooner than AOSP. `SavedStateHandle` / `rememberSaveable` for transient state | The system terminates backgrounded apps under memory pressure; `applicationWillTerminate` is **not** called for a backgrounded app. `@SceneStorage` / `NSUserActivity` for transient state | Never treat "the app is still running" as an assumption on either. Assume Android kills sooner and more unpredictably |
| **Background execution** | `WorkManager` for deferrable work; foreground service with a declared **type** for user-visible work. `dataSync` and `mediaProcessing` are capped at **6 background h per 24 h** (targetSdk 35+; foregrounding resets it) → `Service.onTimeout()` then you must `stopSelf(startId)` in seconds or the process dies with a fatal `RemoteServiceException`. `shortService` capped at ~3 min (overrun is an ANR). On Android 16+, jobs running concurrently with a foreground service obey the JobScheduler runtime quota — this hits WorkManager and DownloadManager regardless of target | `BGAppRefreshTask` (~30 s, opportunistic — the OS decides *if*, not just when), `BGProcessingTask` (minutes, typically overnight on charger), `BGContinuedProcessingTask` (iOS 26+, user-initiated, system-provided progress UI, cancellable by the user), `URLSession` background configuration for transfers | Android background work is *scheduled but capped*; iOS background work is *requested and may never run*. Any design that requires background work to complete on a schedule is wrong on iOS. Checkpoint and resume on both |
| **Permissions** | Runtime prompts. Two denials on modern Android = permanently denied (`shouldShowRequestPermissionRationale` returns false and the dialog no longer appears). `POST_NOTIFICATIONS` is a runtime permission since Android 13 | Prompt shown **once ever**; after that only Settings. Partial grants for Photos (limited library) and Location (While Using / Once / Precise off) | Both platforms have a permanently-denied state you must render. iOS's one-shot prompt makes pre-prompt rationale mandatory, not optional |
| **Local storage that survives reinstall** | App data is deleted on uninstall, but **Auto Backup / Backup & Restore** copies it off-device by default — control with `android:dataExtractionRules`. Keystore keys do not survive | **Keychain items persist after the app is deleted** (long-standing, undocumented). iCloud Keychain can also sync them to other devices unless `…ThisDeviceOnly` | The iOS reinstall-inherits-credentials trap has no Android equivalent. The Android backup-exfiltration trap has no iOS equivalent. Handle each explicitly |
| **Secure key storage** | Android Keystore (hardware-backed / StrongBox where available). `androidx.security:security-crypto` (`EncryptedSharedPreferences`, `EncryptedFile`) had **every API deprecated at 1.1.0-beta01, June 2025** — the 1.1.0 stable release (July 2025) ships deprecated, with no drop-in successor — current guidance is DataStore + Tink + Keystore, or a maintained community fork | Keychain with an explicit `kSecAttrAccessible…` class; Secure Enclave for keys via `SecAccessControl` | The Android answer changed recently. Do not copy pre-2025 sample code |
| **Deep links** | App Links verified via `assetlinks.json` + the correct SHA-256 signing fingerprint. Custom schemes are claimable by any other installed app | Universal Links verified via `apple-app-site-association`. Custom schemes are equally claimable | Anything carrying a token (OAuth redirect, magic link) must use the verified form on both |
| **Release control** | Staged rollout with halt; you can ship a fix within hours. Play policy gates: targetSdk deadline, restricted-permission declarations, Data safety form, billing library version | Phased release (7 days) with the ability to pause; expedited review by request. Apple gates: Xcode/SDK minimum, `PrivacyInfo.xcprivacy`, age-rating questionnaire, App Review Guidelines | iOS fixes are slower to reach users. Ship a remote kill switch for any risky new path on both |
| **Fragmentation** | Thousands of device models, OEM skins, and API levels down to your `minSdk`; wildly different memory and thermal behavior | A short, known device list; users upgrade quickly | Android needs a real device matrix (low-RAM + an OEM skin), not just a Pixel |

**Load `mir-mobile-android` for:** Kotlin, Jetpack Compose, `SavedStateHandle`, WorkManager, Room, Gradle/R8/ProGuard, manifest, Play Console.
**Load `mir-mobile-ios` for:** Swift, SwiftUI, Swift concurrency and actor isolation, `BGTaskScheduler`, SwiftData/Core Data, Keychain APIs, Xcode/App Store Connect.

---

## Part 2 — Implementation approach fitness

| Approach | Stack | Use when… | Do NOT use when… | Cost you are accepting |
|---|---|---|---|---|
| **Fully native, two codebases** | Swift/SwiftUI + Kotlin/Compose | The app *is* the product and platform feel is a differentiator; heavy platform-API use (camera pipeline, ARKit/ARCore, HealthKit, background audio, widgets, watch apps); strict performance or battery budgets; you have (or will hire) engineers on both platforms | You have one mobile engineer and two platforms to ship; the app is mostly forms, lists, and API calls; time-to-market dominates | Two implementations of every feature and every bug fix. Highest total cost, highest ceiling |
| **KMP + Compose Multiplatform** | Kotlin Multiplatform for logic; Compose Multiplatform for shared UI | You want one implementation of business logic, networking, and persistence with genuinely native builds; the team is Kotlin-strong; you want to share UI *incrementally* rather than all at once. CMP for iOS reached **Stable in Compose Multiplatform 1.8.0 (May 2025)**; Google supports KMP for Android, and Room, DataStore, and ViewModel have KMP support | Your iOS engineers will not accept a non-SwiftUI UI layer (this is a real, common failure — it is a team decision, not a technical one); the app is iOS-first and UI-heavy; you need mature Swift-facing API ergonomics today (direct Swift export landed in Kotlin 2.2.20 but is still experimental; Objective-C bridging remains the stable path) | Still need Xcode, Swift, and iOS build-phase knowledge on the team. Debugging crosses two toolchains. Shared UI is all-or-nothing per screen |
| **React Native** | TS/JS + New Architecture (JSI, Fabric, TurboModules) | Strong existing React/TS team; heavy product iteration on standard UI (feeds, forms, commerce); you want over-the-air JS updates for non-native changes; Expo's managed build and update pipeline is a fit. The New Architecture is the default since 0.76 and the legacy bridge is **removed** in recent versions — `newArchEnabled=false` no longer does anything | Sustained per-frame native work (real-time camera/AR/video processing, complex gesture-driven canvases); you need the newest OS APIs on day one (you wait for a library, or write the TurboModule yourself); very tight binary size or cold-start budgets | Hermes is the default engine and the supported path; JavaScriptCore is no longer bundled in core. Every native dependency is a maintenance liability. New-Architecture migration of old third-party libraries is the usual blocker |
| **Flutter** | Dart + Skia/Impeller | Pixel-identical UI across platforms matters more than platform-native feel; a design-system-heavy app; a team willing to standardise on Dart; you also want desktop/web from the same code. Stable line as of Aug 2026 is **3.44.x** | Deep OS integration is central (widgets, watch app, App Intents/Siri, share sheet, extensive background modes) — all need platform channels and native code anyway; you need to reuse existing Kotlin/Swift libraries; the org has no Dart experience and does not want it | Everything is drawn, not native — accessibility, text input, and platform conventions need explicit attention. Native OS APIs come through platform channels you maintain |
| **WebView wrapper / Capacitor / Cordova** | Web app in a native shell | An existing web app needs store presence with minimal investment; content-first, low-interaction apps | Any of the Gate 0 risk-surface rows tick — background execution, secure storage, offline sync, deep OS integration. Also a known App Store 4.2 "minimum functionality" rejection risk for a thin wrapper with no native value | You inherit every WebView security footgun (bridge exposure, `file://` access, untrusted URL loading) and the store review risk |

### Choosing, in one pass

1. **Do any of the Gate 0 rows tick "background execution", "secure storage", or "deep OS integration"?** If yes, a WebView wrapper is out.
2. **Is platform feel a differentiator, or is the app mostly forms/lists/API calls?** Differentiator → lean native. Forms → cross-platform is defensible.
3. **What language is the team already strong in?** Kotlin → KMP/CMP. TypeScript → React Native. Neither, and design consistency matters most → Flutter.
4. **Whichever you pick, you still need someone who can open Xcode and Android Studio and read a native crash.** No cross-platform framework removes that. Say so out loud if the team does not have it.

---

## Naming reminder

- Platform module: `mir-mobile-<platform>` — `mir-mobile-android`, `mir-mobile-ios`.
- Cross-platform framework specifics belong in the platform modules or a future `mir-mobile-<platform>-<framework>` module; do not widen this pillar.
- Add more via the recipe in `EXTENDING.md`.

## Currency note

Versions and stability claims above were checked on 13 Aug 2026 against JetBrains, React Native, and Flutter release channels. Framework version numbers move fast — re-verify the exact version before pinning it in a project. Where a claim is a team/organisational judgement (for example "iOS engineers reject non-SwiftUI UI"), it is an observed pattern, not a measured fact.
