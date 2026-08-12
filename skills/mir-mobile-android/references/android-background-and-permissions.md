# Android background work and permissions — expanded

Read at **Gate 5** (design) and **Gate 6** (implementation). Verified 13 Aug 2026 against `developer.android.com`. androidx.work stable is **2.11.2** (25 Mar 2026); 2.12.0-rc01 exists but is not stable.

Dated Play deadlines and targetSdk behavior-change lists are in `mir-mobile/references/store-compliance.md`. This file is the mechanics.

---

## 1. Choosing the background API

| Need | API | What actually limits it |
|---|---|---|
| Must eventually happen, can be deferred (sync, upload, cleanup) | `WorkManager` | standby bucket quota; Doze windows; on Android 16 the JobScheduler quota is shared with a concurrently running foreground service |
| Must happen soon, short, user-triggered | `WorkManager.setExpedited(OutOfQuotaPolicy…)` | an app-level expedited quota; falls back to a regular job when exhausted |
| User-visible ongoing work (playback, navigation, active call) | foreground service + declared type | the type's cap (below); notification is mandatory and non-dismissible for most types |
| Large user-initiated transfer | user-initiated data transfer job | exempt from ordinary job quotas; still needs the user to have started it |
| A real clock event the user set | `AlarmManager.setAlarmClock` / `setExactAndAllowWhileIdle` | the exact-alarm permission (section 4) |
| Server-driven wake-up | FCM high-priority message | high-priority quota; a low-priority message is deferred in Doze |

If none of these fit, the answer is "sync when the app next opens." Say that at Gate 5 rather than shipping a design that requires background work to be on time.

## 2. Foreground service types

Declaring a type is mandatory since **API 34**. Three things must agree or you crash at `startForeground()`: the manifest `android:foregroundServiceType`, the `FOREGROUND_SERVICE_<TYPE>` permission, and the type passed to `startForeground(id, notification, type)`.

| Type | Permission | Prerequisite / cap |
|---|---|---|
| `camera` | `FOREGROUND_SERVICE_CAMERA` | `CAMERA` runtime permission; while-in-use restricted |
| `connectedDevice` | `FOREGROUND_SERVICE_CONNECTED_DEVICE` | a Bluetooth/NFC/companion permission |
| `dataSync` | `FOREGROUND_SERVICE_DATA_SYNC` | **6 h / 24 h cap**; cannot start from `BOOT_COMPLETED` |
| `health` | `FOREGROUND_SERVICE_HEALTH` | `READ_HEART_RATE` / `ACTIVITY_RECOGNITION`; while-in-use restricted |
| `location` | `FOREGROUND_SERVICE_LOCATION` | foreground location granted; background needs `ACCESS_BACKGROUND_LOCATION` |
| `mediaPlayback` | `FOREGROUND_SERVICE_MEDIA_PLAYBACK` | cannot start from `BOOT_COMPLETED` |
| `mediaProcessing` | `FOREGROUND_SERVICE_MEDIA_PROCESSING` | **6 h / 24 h cap** |
| `mediaProjection` | `FOREGROUND_SERVICE_MEDIA_PROJECTION` | user grant via `createScreenCaptureIntent()`; cannot start from `BOOT_COMPLETED` |
| `microphone` | `FOREGROUND_SERVICE_MICROPHONE` | `RECORD_AUDIO`; while-in-use restricted |
| `phoneCall` | `FOREGROUND_SERVICE_PHONE_CALL` | `MANAGE_OWN_CALLS` or default dialer |
| `remoteMessaging` | `FOREGROUND_SERVICE_REMOTE_MESSAGING` | — |
| `shortService` | none beyond `FOREGROUND_SERVICE` | **~3 min**; cannot start another FGS; not sticky |
| `specialUse` | `FOREGROUND_SERVICE_SPECIAL_USE` | requires `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` and a Play Console justification |
| `systemExempted` | `FOREGROUND_SERVICE_SYSTEM_EXEMPTED` | reserved for device-owner / VPN / emergency-role apps. A normal app gets `ForegroundServiceTypeNotAllowedException` — it is not an escape hatch from the caps |

The 6-hour cap is tracked **per type, across all your services of that type**, not per service instance.

```kotlin
// API 35+ overload, called for dataSync, mediaProcessing, and shortService
override fun onTimeout(startId: Int, fgsType: Int) {
    // You have seconds. Checkpoint, then stop — or the process is killed by a
    // FATAL android.app.RemoteServiceException:
    // "A foreground service of <type> did not stop within its timeout"
    checkpoint()
    stopSelf(startId)
}

// API 34 shortService only had the one-argument form
override fun onTimeout(startId: Int) { checkpoint(); stopSelf(startId) }
```

A `dataSync`/`mediaProcessing` overrun is a **crash** (`RemoteServiceException`), so it lands in your crash reporter. A `shortService` overrun is an **ANR**. The 6-hour budget counts background time only — bringing the app to the foreground resets it to a full 6 hours. After the cap you cannot start another service of that type until the user foregrounds the app — `ForegroundServiceStartNotAllowedException`. Force it locally:

```bash
adb shell am compat enable FGS_INTRODUCE_TIME_LIMITS com.example.app
```

## 3. WorkManager

```kotlin
class SyncWorker(ctx: Context, params: WorkerParameters) : CoroutineWorker(ctx, params) {
    override suspend fun doWork(): Result {
        // doWork() re-runs from the top on retry. It MUST be idempotent.
        return try {
            outbox.drain()                       // each item carries its persisted idempotency key
            Result.success()
        } catch (e: IOException) {
            Result.retry()                       // backoff applies
        } catch (e: PermanentException) {
            Result.failure()                     // no retry; surface it to the user
        }
    }
}

WorkManager.getInstance(ctx).enqueueUniqueWork(
    "sync-outbox",
    ExistingWorkPolicy.KEEP,                     // APPEND_OR_REPLACE / REPLACE re-enqueue on every call
    OneTimeWorkRequestBuilder<SyncWorker>()
        .setConstraints(
            Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .setRequiresBatteryNotLow(true)
                .build()
        )
        .setBackoffCriteria(BackoffPolicy.EXPONENTIAL, 30, TimeUnit.SECONDS)
        .build()
)
```

- **Observe with Flow**, not LiveData: `getWorkInfosForUniqueWorkFlow(name)` (2.9.0+).
- **Log why it stopped**: `getStopReason()` on `WorkInfo` and on the worker. `STOP_REASON_FOREGROUND_SERVICE_TIMEOUT`, `STOP_REASON_QUOTA`, `STOP_REASON_CONSTRAINT_*`, `STOP_REASON_DEVICE_STATE` are the ones you will see in production. Without this you cannot tell "never ran" from "ran and was killed".
- `updateWork()` (2.8.0+) changes a request without losing its enqueue time or chain — `REPLACE` resets both.
- `setRequiredNetworkRequest()` (2.10.0+) for finer network selection than `NetworkType`.
- Periodic work has a **15-minute minimum** and is not a timer. It fires within the interval, batched with other apps' work.
- Checkpoint long work. On Android 16 a worker running alongside a foreground service can be stopped when the shared JobScheduler quota runs out; a worker that restarts from zero re-downloads on the user's data plan.

## 4. Exact alarms

```kotlin
val am = ctx.getSystemService(AlarmManager::class.java)
if (am.canScheduleExactAlarms()) {
    am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi)   // fires in Doze
} else {
    am.setAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, triggerAt, pi)        // inexact fallback
    // and offer the user Settings.ACTION_REQUEST_SCHEDULE_EXACT_ALARM, with a reason
}
```

- `SCHEDULE_EXACT_ALARM`: user-grantable, revocable at any time, and denied after a backup-restore onto a new device. **On devices running Android 14+ it is no longer pre-granted to newly installed apps that target Android 13 (API 33) or higher** — an app already holding it keeps it across an OS upgrade, so this shows up as "works on my upgraded device, denied on a fresh install".
- `USE_EXACT_ALARM`: auto-granted, non-revocable, restricted to alarm-clock and calendar-style apps and subject to Play policy. Do not declare it to dodge the permission check.
- `setAlarmClock` **is an exact-alarm API and needs the same permission** — calling it without one throws `SecurityException`. It pulls the device out of Doze and shows a system alarm indicator, at a real battery cost.
- Register a receiver for `AlarmManager.ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED` and reschedule when it fires. **It is sent on grant only.** On revocation the system stops your app and cancels every exact alarm you set, so there is nothing to listen for — re-check `canScheduleExactAlarms()` on start and before every schedule.
- **Alarms are lost on reboot.** Reschedule from a `BOOT_COMPLETED` receiver, reading durable state.

## 5. Doze, standby buckets, and OEMs

| Bucket | Rough job/alarm allowance |
|---|---|
| active | effectively unrestricted |
| working set | frequent |
| frequent | a few times a day |
| rare | ~once a day |
| restricted | ~one job per day, alarms heavily deferred |

Users can put an app in **restricted** manually, and the system does it to rarely-opened apps. Chinese OEM skins add their own kill policies that are not part of AOSP and not configurable from your app.

```bash
adb shell dumpsys deviceidle force-idle          # enter Doze
adb shell dumpsys deviceidle unforce
adb shell am set-standby-bucket com.example.app restricted
adb shell am get-standby-bucket com.example.app
adb shell dumpsys jobscheduler | grep -A 20 com.example.app
```

Test the restricted bucket before release. If the feature is unusable there, it is unusable for a meaningful share of real users.

---

## 6. Runtime permissions — the state machine

```kotlin
val launcher = registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
    prefs.markAsked(PERM)                      // you must record this yourself
    if (granted) onGranted() else refreshState()
}
```

| Observed | `hasAsked` (yours) | `shouldShowRequestPermissionRationale` | Render |
|---|---|---|---|
| not granted | false | false | rationale UI → request |
| not granted | true | true | rationale UI → may request again |
| not granted | true | **false** | **permanently denied**: explain the consequence + open app settings. Never re-request |
| granted | — | — | the feature |

`shouldShowRequestPermissionRationale()` returns `false` both before the first request and after permanent denial — it is not a state test on its own, which is why you persist `hasAsked`.

```kotlin
startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
    Uri.fromParts("package", packageName, null)))
```

Other specifics:
- **Re-check on every resume.** A permission can be revoked from Settings while your app is backgrounded, and the OS may kill the process when it happens.
- `POST_NOTIFICATIONS` is a runtime permission since Android 13. Request it at a moment where the value is obvious, not at launch.
- `ACCESS_BACKGROUND_LOCATION` must be a **separate, later** request after foreground location is granted, and it opens Settings rather than a dialog on modern Android. Requesting it together with foreground location fails.
- Permissions your app has not used for months are **auto-revoked** by the system for unused apps. Handle "was granted, now is not" on resume, not as an impossible state.

## 7. Photo picker and partial media access

```kotlin
val pick = registerForActivityResult(ActivityResultContracts.PickMultipleVisualMedia(10)) { uris ->
    uris.forEach {
        contentResolver.takePersistableUriPermission(it, Intent.FLAG_GRANT_READ_URI_PERMISSION)
    }
}
pick.launch(PickVisualMediaRequest(ActivityResultContracts.PickVisualMedia.ImageAndVideo))
```

- **No permission is required**, so no Play restricted-permission declaration is required either. This is the default answer.
- Persistable grants are capped at 5,000 per app. Copy what you need into your own storage instead of holding thousands of grants.
- `PickVisualMedia.isPhotoPickerAvailable(context)` for the availability check; the Jetpack contract falls back to `ACTION_OPEN_DOCUMENT` automatically.

If you genuinely need library-wide access:
- `READ_MEDIA_IMAGES` / `READ_MEDIA_VIDEO` require a Play Console declaration and are restricted to use cases where the picker is insufficient. Having your own gallery UI is **not** an exemption.
- On Android 14+ request `READ_MEDIA_VISUAL_USER_SELECTED` alongside them. If only that one is granted, you are in **selected photos only** mode: you see a subset, the subset can change, and re-requesting shows the picker again rather than a dialog. Handle items disappearing between sessions.
- At targetSdk 36 on Android 16+, previously-selected photos are pre-selected when the user reopens the picker, and deselecting revokes access to those items.
- `MANAGE_EXTERNAL_STORAGE` is effectively unapprovable outside file managers and backup apps.

## 8. Privacy dashboard and data access auditing

The privacy dashboard attributes every location, camera, and microphone access to your app by name — including accesses made by a third-party SDK inside your process. Find them before your users do:

```kotlin
val appOps = getSystemService(AppOpsManager::class.java)
appOps.setOnOpNotedCallback(mainExecutor, object : AppOpsManager.OnOpNotedCallback() {
    override fun onNoted(op: SyncNotedAppOp) = log(op.op, Throwable().stackTrace)
    override fun onSelfNoted(op: SyncNotedAppOp) = log(op.op, Throwable().stackTrace)
    override fun onAsyncNoted(op: AsyncNotedAppOp) = log(op.op(), op.message)
})
```

Run it in a debug build across your main flows and read the stack traces. An access you cannot explain is either a bug or an SDK you need to justify in the Data safety form.
