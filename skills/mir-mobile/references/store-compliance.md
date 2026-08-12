# Store compliance & platform deadlines

Read at **Gate 0** (does a deadline block this release?) and **Gate 7** (does this build actually submit?).

**Verified 13 Aug 2026** against primary sources: Google Play Console Help, `developer.android.com`, and `developer.apple.com`. Dates move. Re-check the two primary pages before you rely on a date:
- <https://support.google.com/googleplay/android-developer/answer/11926878>
- <https://developer.apple.com/news/upcoming-requirements/>

---

## 1. Google Play — release gates

### Target API level (verified, Play Console Help)

| Who | Requirement | Date |
|---|---|---|
| New apps and app **updates** | Target **Android 16 (API 36)** or higher | **31 Aug 2026** |
| Wear OS and Android Automotive apps | Target Android 15 (API 35) or higher | 31 Aug 2026 |
| Android TV and Android XR apps | Target Android 14 (API 34) or higher | 31 Aug 2026 |
| **Existing** apps (not being updated) | Stay discoverable to new users on newer devices. The floor differs by form factor: phones/tablets and Android Auto **API 35**, Wear OS **34**, Android XR **34**, Android TV **33**, Android Automotive OS **32** | 31 Aug 2026 |
| Extension | Request in Play Console **before** the deadline; extends to **1 Nov 2026** | — |

Missing the deadline does not remove the app. It blocks new publishing, and apps targeting API 34 or lower stop appearing for users on newer Android versions. `minSdkVersion` is not constrained by this policy.

**Android 17 (API 37):** shipped mid-2026. A Google DevRel engineer stated in Feb 2026 that Play would require API 37 in **August 2027**, but as of this writing that date is **not on Google's published target-API page** — treat it as expected, not confirmed, and verify before planning around it.

### Other Play gates

| Gate | Requirement | Date |
|---|---|---|
| **Play Billing Library** | New apps and updates must use **PBL 8 or later** (PBL 9.0.0 shipped 19 May 2026). Extension to 1 Nov 2026 on request. Applies only if you sell through Play | **31 Aug 2026** |
| **16 KB page size** | Apps with native `.so` libraries targeting Android 15+ must support 16 KB pages. Needs NDK r28+ / AGP 8.5.1+. Verify with the APK Analyzer alignment warning | In force since **1 Nov 2025** |
| **Photo & video permissions** | `READ_MEDIA_IMAGES` / `READ_MEDIA_VIDEO` only if the Android Photo Picker is insufficient for core functionality; requires a Play Console declaration. Having a custom picker does not exempt you | Fully enforced since **28 May 2025** |
| **Contacts permission** | Announced 15 Apr 2026. Apps targeting **API 37+** may only request `READ_CONTACTS` if the Android Contact Picker is insufficient; Play Console declaration required. Console prompts from Sept 2026; Google's help page states mandatory compliance in **January 2027** (some secondary reporting says late Oct 2026 — check your app's Play Console for the exact date) | Jan 2027 |
| **Developer verification** | Apps installed on certified Android devices must be registered to a verified developer. Live for users in **Brazil, Indonesia, Singapore, Thailand from 30 Sept 2026**; global expansion in 2027. Verify via Android Developer Console / Play Console | 30 Sept 2026 → 2027 |
| **Data safety form** | Must match what the app *and its SDKs* actually collect. Using the system Photo Picker does not by itself exempt you from declaring photo/video data collection | Ongoing; re-verify on every SDK change |

---

## 2. Apple — release gates

| Gate | Requirement | Date |
|---|---|---|
| **SDK minimum** | Uploads to App Store Connect must be built with **Xcode 26 or later** using the **iOS 26 / iPadOS 26 / tvOS 26 / visionOS 26 / watchOS 26 SDK** or later. No grace period announced | In force since **28 Apr 2026** |
| **Privacy manifest** | Apps must declare approved reasons for **required-reason APIs** used by app code *and* third-party SDKs. Missing/incorrect → rejection (commonly `ITMS-91053`) | In force since **1 May 2024** |
| **Third-party SDK signature** | SDKs on Apple's "SDKs that require a privacy manifest and signature" list must ship a manifest **and** a valid signature | Ongoing |
| **Age rating questionnaire** | Responses to the updated questions were required to avoid submission interruption | Was due **31 Jan 2026** |

**iOS 27** was announced at WWDC on 8 June 2026 and is in beta as of Aug 2026, with general release expected around September 2026. **It is not yet the required build SDK** — the current requirement is the 26 SDK line. Expect the requirement to advance roughly a year after its release; do not assume a date.

### Rebuild side effect

An app rebuilt with the iOS 26 SDK picks up the **Liquid Glass** appearance on native UI components by default unless explicitly opted out. Bumping the SDK to satisfy the submission requirement is therefore a **UI change**, not a no-op. Re-run visual and layout regression before shipping.

### App Review Guidelines — 8 June 2026 revision

Sections touched: developer identity information and export compliance (3.1, 14.8); Sensitive Content Analysis framework (3.3.3(N)); Suggested Actions API (3.3.3(Q)); Trust Insights framework (3.3.3(R)). Guideline **4.3 (Spam)** was tightened — apps in oversaturated categories that are not updated or improved may be removed. **4.5.3** bars using Live Activities to spam, phish, or send unsolicited messages. Apple states that on average over 40% of unresolved review issues are Guideline **2.1 App Completeness** — crashes, placeholder content, missing review credentials.

---

## 3. `PrivacyInfo.xcprivacy` — the mechanics

One plist per app target; SDKs ship their own. Xcode merges them into the privacy report at archive time. **Your app still needs its own file — an SDK's manifest does not cover your code.**

Four keys:

| Key | What it holds |
|---|---|
| `NSPrivacyTracking` | Boolean — does the app track (as Apple defines it)? |
| `NSPrivacyTrackingDomains` | Domains contacted for tracking. If `NSPrivacyTracking` is true and ATT is not granted, connections to these domains fail |
| `NSPrivacyCollectedDataTypes` | Data types collected, linkage to identity, and use for tracking |
| `NSPrivacyAccessedAPITypes` | Required-reason API categories + the approved reason codes |

Required-reason API categories include `UserDefaults`, file timestamp APIs, system boot time, available disk space, and active keyboard information. Apple's TN3183 is the authoritative list of categories and permitted reason codes — read it rather than guessing a code.

**Audit procedure before every release:**
1. `grep -r "PrivacyInfo.xcprivacy"` across Pods / SPM checkouts — list which dependencies ship one.
2. For any dependency without one that touches a required-reason API, upgrade it or replace it. A missing SDK manifest is *your* rejection.
3. Archive, open the privacy report, and diff it against the App Store Connect privacy answers. They must agree.
4. Re-run steps 1–3 after **any** dependency change. This is the step that gets skipped and causes the rejection.

---

## 4. Behavior changes that break existing code

These are not policy — they are runtime behavior that changes when you raise `targetSdk` to satisfy the Play deadline.

### targetSdk 36 (Android 16)
- **Edge-to-edge is mandatory.** `windowOptOutEdgeToEdgeEnforcement` is deprecated and disabled. Content draws behind the status and navigation bars unless you apply window insets. This is the single most common visual breakage on the API 36 bump.
- **Predictive back animations enabled by default.** Apps still overriding `onBackPressed()` need to migrate.
- **Full-screen intents require `USE_FULL_SCREEN_INTENT`.**
- **Adaptive layouts enforced on large screens.**

### All apps running on Android 16, regardless of target
- **Jobs running concurrently with a foreground service now obey the JobScheduler runtime quota.** This hits `WorkManager`, `JobScheduler`, and `DownloadManager`. A long-running worker can exhaust the quota and be stopped. Diagnose with `WorkInfo.getStopReason()`. For user-triggered transfers, use a **user-initiated data transfer job** — those are exempt from ordinary quotas.

### targetSdk 35 (Android 15) — still relevant, often missed
- `dataSync` and `mediaProcessing` foreground services are capped at **6 background hours per 24**, tracked separately per type and shared across all your services of that type. **Bringing the app to the foreground resets the allowance to a full 6 hours.** At the cap the system calls `Service.onTimeout(int, int)`; you have **seconds** to call `stopSelf(startId)` or the process is killed by a **fatal `RemoteServiceException`** — *"A foreground service of `<type>` did not stop within its timeout"*. This is a crash in your crash reporter, not an ANR. Starting another one afterwards throws `ForegroundServiceStartNotAllowedException` until the user foregrounds the app.
- `shortService` is capped at ~3 minutes and cannot start another foreground service. Overrunning it produces an ANR rather than a `RemoteServiceException`.
- `BOOT_COMPLETED` receivers can no longer start `dataSync`, `camera`, `mediaPlayback`, `phoneCall`, or `mediaProjection` foreground services.
- Force the timeout locally to test: `adb shell am compat enable FGS_INTRODUCE_TIME_LIMITS <pkg>`.

### targetSdk 37 (Android 17) — plan ahead
- **`ACCESS_LOCAL_NETWORK`** runtime permission is now mandatory for local-network access (was optional in Android 16). Breaks device-discovery and casting features silently.
- **SMS OTP protection extended to standard SMS**: a **3-hour delay** before the message reaches most apps; `SMS_RECEIVED_ACTION` is withheld and provider queries are filtered. Migrate to the SMS Retriever or SMS User Consent APIs — an OTP autofill built on raw SMS reading stops working.
- **Background audio hardening**: audio from a non-visible app requires a foreground service with while-in-use capability (or exact-alarm + `USAGE_ALARM`).
- **Orientation / resizability restrictions ignored on `sw >= 600dp`**, opt-out removed.
- **Static final fields are unmodifiable via reflection** → `IllegalAccessException`; via JNI `SetStatic<Type>Field` → immediate crash. This breaks some test and mocking libraries.
- **Contacts Provider 2**: `ACCOUNT_NAME` / `ACCOUNT_TYPE` restricted on `ContactsContract.Data`; strict SQL validation when queried without `READ_CONTACTS`.
- **Certificate Transparency enforced by default** (was opt-in in Android 16). **Encrypted Client Hello on by default**, configurable via `<domainEncryption>` in the network security config. `usesCleartextTraffic` is slated for deprecation — move to a network security config.
- **Native dynamic code loading**: `System.load()` on a library that is not read-only throws `UnsatisfiedLinkError`.
- **Background activity launch**: `MODE_BACKGROUND_ACTIVITY_START_ALLOWED` → `MODE_BACKGROUND_ACTIVITY_START_ALLOW_IF_VISIBLE`.
- **Per-app memory limits based on device RAM** apply to all apps regardless of target; kills surface in `ApplicationExitInfo` as `REASON_OTHER` with a description containing `MemoryLimiter:AnonSwap`.
- `BluetoothSocket` RFCOMM `InputStream.read()` now returns `-1` on close instead of throwing `IOException` — read loops that only catch `IOException` hang.

### iOS 26 background execution
`BGContinuedProcessingTask` (iOS/iPadOS 26+) finishes user-initiated foreground work after backgrounding, with system-provided progress UI the user can cancel. Submit via `BGTaskScheduler.shared.submit(_:)`; `strategy` is `.queue` or `.fail`; drive `task.progress`, call `task.updateTitle(_:subtitle:)`, finish with `task.setTaskCompleted(success:)`. GPU access requires checking `BGTaskScheduler.supportedResources` for `.gpu` and the `com.apple.developer.background-tasks.continued-processing.gpu` entitlement, and is not available on all devices. It **must** start from an explicit user action.

---

## 5. Pre-submission checklist (Gate 7)

- [ ] `targetSdk` / deployment target and build SDK meet the current store minimums above
- [ ] Release build runs (not just debug); R8/ProGuard rules verified; obfuscated crashes deobfuscate
- [ ] `PrivacyInfo.xcprivacy` present, privacy report diffed against App Store Connect answers, all SDKs audited
- [ ] Play Data safety form re-verified against the current SDK list
- [ ] Restricted-permission declarations filed (photo/video, contacts, location, accessibility) — and the permission removed from **every** track's build if you do not qualify
- [ ] Billing library version meets the deadline (if you sell through Play)
- [ ] **Purchase lifecycle, not just the library version** — purchases verified server-side and deduplicated by purchase token / transaction ID; `PENDING` never grants entitlement; Play purchases acknowledged or consumed **within 3 days** or Google auto-refunds and revokes them; on Apple, `Transaction.updates` and `Transaction.unfinished` are observed on every launch and the entitlement is persisted *before* `finish()`
- [ ] Review credentials and a working demo account in the review notes
- [ ] Staged/phased rollout configured, with a remote kill switch for the new path
- [ ] Minimum supported app version documented for the server team
