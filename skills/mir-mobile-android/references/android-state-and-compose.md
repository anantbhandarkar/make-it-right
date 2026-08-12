# Android state, process death, and Compose — expanded

Read at **Gate 6** (implementation) and **Gate 7** (review). Versions verified 13 Aug 2026: Compose BOM 2026.08.00 (`compose.ui` 1.12.0, `material3` 1.4.0), Kotlin 2.4.10, androidx.lifecycle 2.11.0.

---

## 1. Forcing process death — the test procedure

The bug only exists in a state you never reach by hand. Run all four, per feature:

```bash
# 1. SIGKILL the backgrounded process. No onDestroy, no onSaveInstanceState.
adb shell am kill com.example.app

# 2. Kill only the process but keep the task, the closest simulation of a low-memory kill
adb shell am kill-all

# 3. "Don't keep activities" — destroys each Activity as soon as it leaves the foreground
adb shell settings put global always_finish_activities 1
adb shell settings put global always_finish_activities 0     # turn it off afterwards

# 4. Background process limit — Developer options, or:
adb shell settings put global background_process_limit 1
```

`adb shell am force-stop` is **not** the same test: it clears the task, so the system does not restore your saved instance state and you never exercise the restore path.

Expected result for every screen: it comes back to the declared `RESTORING` state, then to the same content, with user-entered text intact. "Comes back empty" and "crashes on a null" are the two bugs this finds.

Also confirm the release build survives it. R8 can strip a `Parcelable.Creator` if the model class is only referenced reflectively — the crash appears only in release.

---

## 2. `SavedStateHandle` — the code, and its limits

```kotlin
class EditorViewModel(private val handle: SavedStateHandle) : ViewModel() {

    // Backed by the Bundle. Survives config change AND process death.
    val draftId: String = checkNotNull(handle["draftId"])

    // MutableStateFlow view over the Bundle entry (lifecycle 2.9.0+)
    val title: MutableStateFlow<String> = handle.getMutableStateFlow("title", "")

    // @Serializable classes via the `saved` delegate (lifecycle 2.9.0+)
    var cursor: Cursor by handle.saved { Cursor(0, 0) }
}
```

Rules:
- **Roughly 1 MB across every bundle in the process.** Past it the binder transaction fails with `TransactionTooLargeException`, which surfaces as a crash on backgrounding — far from the code that saved too much.
- Save identifiers and re-fetch. Never save a list of models, a bitmap, or a parsed response.
- `handle` only accepts what a `Bundle` accepts. `@Serializable` works through the `saved` delegate; arbitrary objects do not.
- Navigation arguments arrive in `SavedStateHandle` automatically — read them from there rather than threading them through a factory.

## 3. `rememberSaveable` with a custom `Saver`

```kotlin
data class DateRange(val start: LocalDate, val end: LocalDate)

val DateRangeSaver = listSaver<DateRange, String>(
    save = { listOf(it.start.toString(), it.end.toString()) },
    restore = { DateRange(LocalDate.parse(it[0]), LocalDate.parse(it[1])) }
)

var range by rememberSaveable(stateSaver = DateRangeSaver) { mutableStateOf(defaultRange) }
```

`mapSaver` when the shape is named; `Saver { save, restore }` for the general case. If you find yourself writing a `Saver` for something large, the state belongs in Room, not the Bundle.

---

## 4. Compose side effects — decision table

| You want to… | Use | Key discipline |
|---|---|---|
| run suspend work when the screen appears or an identity changes | `LaunchedEffect(id)` | key on every value the work depends on. `Unit` means "once per entry into the composition" — leaving and navigating back runs it **again**, so it is not a once-per-app hook |
| register/unregister a listener, receiver, or callback | `DisposableEffect(key)` { … `onDispose { unregister() }` } | `onDispose` is mandatory and must undo exactly what the body did |
| push Compose state into a non-Compose object | `SideEffect { view.setX(state) }` | runs after **every** successful recomposition — keep it trivial |
| call the latest lambda from a long-lived effect without restarting it | `val cb by rememberUpdatedState(onDone)` inside `LaunchedEffect(Unit)` | without it, the effect calls the callback captured on first composition |
| launch work from a click or gesture | `rememberCoroutineScope()` | never `LaunchedEffect` for event-driven work; never `GlobalScope` |
| produce a `State` from a non-Compose source | `produceState(initial, key) { value = … }` | the block is cancelled on key change and on leaving composition |
| convert a stream to state | `flow.collectAsStateWithLifecycle()` | not `collectAsState()` — that keeps collecting off-screen |

### The wrong-key bug, both directions

```kotlin
// A. Key too narrow — never re-runs. The screen keeps the previous user's data.
LaunchedEffect(Unit) { load(userId) }

// B. Key unstable — re-runs every recomposition, cancelling in-flight work each frame.
LaunchedEffect(filterObject) { search(filterObject) }     // filterObject is a `var`-holding data class

// Fix for B: key on stable identity, not the object
LaunchedEffect(filter.query, filter.sort) { search(filter) }
```

### `rememberUpdatedState` in practice

```kotlin
@Composable
fun AutoDismiss(onTimeout: () -> Unit) {
    val currentOnTimeout by rememberUpdatedState(onTimeout)
    LaunchedEffect(Unit) {                 // deliberately never restarts
        delay(5_000)
        currentOnTimeout()                 // calls the newest lambda, not the first
    }
}
```

---

## 5. Diagnosing recomposition

### Compiler metrics

```kotlin
// build.gradle.kts (module)
composeCompiler {
    reportsDestination = layout.buildDirectory.dir("compose_reports")
    metricsDestination = layout.buildDirectory.dir("compose_metrics")
    // stabilityConfigurationFile = rootProject.file("compose_stability.conf")
}
```

Build a **release** variant, then read `*-composables.txt`. Look for `restartable` without `skippable`, and for parameters marked `unstable`. `*-classes.txt` tells you which of your own classes Compose inferred as unstable and why.

The stability configuration file marks third-party types you cannot annotate as stable, one fully-qualified name (or wildcard) per line. Use it for types you *know* are effectively immutable — lying to the compiler produces stale UI, not a crash.

### Layout Inspector

Enable recomposition counts. A composable whose counts climb while nothing visible changes is either taking an unstable parameter (footgun 7) or reading a fast-changing state too high in the tree.

### Where to look first

1. Any parameter typed `List`/`Map`/`Set` → make it `ImmutableList` or hoist + `remember`.
2. Any `data class` parameter with a `var` → make the properties `val`, or `@Immutable`.
3. Any read of `scrollState.value`, an `Animatable`, or a per-frame flow at screen level → move the read into a `Modifier.offset { }` / `graphicsLayer { }` lambda.
4. Lambdas passed down: strong skipping auto-`remember`s them, but a lambda capturing an unstable value is keyed by identity and changes every frame anyway.

---

## 6. Configuration changes worth remembering

Recreation is triggered by rotation, dark mode, locale, font size and display size, keyboard availability, multi-window resize, and (on modern Android) more. `android:configChanges` opts out of recreation but then **you** own reloading resources — and Compose reads resources through the composition, so a half-handled `configChanges` produces stale strings and wrong-density images. Prefer letting the Activity recreate, with state in ViewModel + `SavedStateHandle`.

At targetSdk 36, `android:screenOrientation`, `android:resizableActivity`, `android:minAspectRatio` and `setRequestedOrientation()` are **ignored** on displays with smallest width ≥ 600dp. The opt-out property `android.window.PROPERTY_COMPAT_ALLOW_RESTRICTED_RESIZABILITY` expires at API 37. Any layout that assumed a fixed orientation needs an adaptive pass now, not later.
