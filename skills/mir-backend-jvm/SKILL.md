---
name: mir-backend-jvm
description: "Make It Right (JVM runtime tier). Java 21+ / Kotlin runtime reliability footguns shared across EVERY JVM backend framework (Spring Boot, Quarkus, Micronaut, Vert.x) — distinct from the generic backend gates and from any one framework's mechanics. Covers: thread-pool sizing and pool-exhaustion deadlock, blocking I/O on platform threads, Java 21 virtual threads and carrier-thread pinning inside synchronized/native calls, GC pause tuning (G1 vs ZGC/Shenandoah), container-aware heap sizing (-XX:MaxRAMPercentage vs hard -Xmx), cold-start / JIT warmup cost and mitigations (GraalVM native image, CRaC, AOT), shared-mutable-state visibility (happens-before, volatile, final, data races), and ThreadLocal leaks in pooled threads. TRIGGER when the backend runtime is Java or Kotlin — sits between mir-backend (generic gates) and the framework module (e.g. mir-backend-jvm-spring). SKIP for Python, Node, Go, Rust, .NET, Ruby, PHP, BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics (those live in the framework module)."
trigger: /mir-backend-jvm
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-jvm · Make It Right (JVM runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-jvm-spring`) knows the **library's mechanics**. This tier owns what's true for **all JVM backends because they run on the HotSpot JVM** — the threading model, garbage collector, memory model, and process lifecycle that Spring Boot, Quarkus, Micronaut, and Vert.x all inherit.

**Runtime assumed:** Java 21+ (LTS) or Kotlin on the JVM. Notes reference OpenJDK/HotSpot defaults. Load order: `mir-backend` → `mir-backend-jvm` → `<framework module>`.

## The JVM footguns AI walks into (framework-agnostic)

### 1. Thread-pool sizing and pool-exhaustion deadlock

Platform threads are expensive (≈1 MB stack each by default). The JVM's blocking I/O model means every request that waits on the DB, cache, or downstream HTTP holds a thread.

**Little's Law sets the floor:** `threads_needed = throughput_rps × latency_s`. At 500 req/s with 200 ms average latency you need 100 threads just to break even — the JVM default `ForkJoinPool.commonPool()` has only `CPU_cores - 1`.

**Pool-exhaustion deadlock** is the silent killer: a pooled request thread submits a subtask to the *same* pool and then `get()`s on the future. If all pool slots are occupied waiting on their own subtasks, no thread is available to run those subtasks — the whole pool gridlocks. Every `.get()` or `join()` inside a thread-pool-backed executor is a suspect.

```java
// WRONG — if every request thread reaches this, pool deadlocks
ExecutorService pool = Executors.newFixedThreadPool(10);
Future<Result> f = pool.submit(() -> callDownstream()); // submitted to same pool
return f.get(); // blocks the submitting thread

// RIGHT — separate pools / bulkheads, or don't block at all
ExecutorService ioPool = Executors.newFixedThreadPool(50); // dedicated I/O pool
Future<Result> f = ioPool.submit(() -> callDownstream());
return f.get(); // request thread is distinct from the I/O pool
```

Bulkhead: give each downstream (DB, payment service, search) its own bounded pool. A single downstream going slow can only exhaust its own bulkhead, not the whole request path.

### 2. Blocking I/O ties up a platform thread — and virtual threads change the calculus, but introduce pinning

On **platform threads**, blocking I/O (JDBC, `HttpClient` with sync API, `Thread.sleep`) holds the OS thread for the full duration. That's why async frameworks (reactive, Vert.x, Quarkus reactive) exist: they free the thread during the wait.

**Java 21 virtual threads (Project Loom)** flip this: a virtual thread is parked (unmounted from its carrier thread) while blocked on I/O, so you can have millions of virtual threads with far fewer carrier threads. JDK HTTP client, `Socket`, file I/O all park correctly. This makes the "thread-per-request blocking model" viable again at high concurrency *without* reactive programming.

**The PINNING trap — the one thing virtual threads do NOT fix:**

A virtual thread that enters a `synchronized` block or calls a native method **cannot be unmounted** from its carrier thread — it pins the carrier for the duration of the lock. If many virtual threads pin simultaneously, you can exhaust all carrier threads despite having millions of virtual threads.

```java
// WRONG — synchronized pins the carrier thread when blocking inside
synchronized (this) {
    result = jdbcStatement.executeQuery(); // I/O while pinned → carrier blocked
}

// RIGHT — ReentrantLock allows unmounting during I/O inside the lock
private final ReentrantLock lock = new ReentrantLock();
lock.lock();
try {
    result = jdbcStatement.executeQuery(); // virtual thread parks, carrier freed
} finally {
    lock.unlock();
}
```

Detect pinning: run with `-Djdk.tracePinnedThreads=full` to log pinning events. JDBC drivers that use `synchronized` internally (many older ones do) will pin — prefer drivers updated for Loom (PostgreSQL JDBC 42.7+, HikariCP 6+).

### 3. GC pauses: G1 vs ZGC/Shenandoah

The default collector (G1 in Java 9+) targets throughput with occasional stop-the-world pauses. A full GC can pause all application threads for hundreds of milliseconds to several seconds on large heaps.

| Collector | Pause target | Heap sweet spot | Use when |
|---|---|---|---|
| G1 (default) | 200 ms (tunable with `-XX:MaxGCPauseMillis`) | 4 GB – 64 GB | General-purpose; CPU to spare |
| ZGC (`-XX:+UseZGC`) | Sub-millisecond (concurrent) | Any | Latency-sensitive APIs, trading, real-time |
| Shenandoah (`-XX:+UseShenandoahGC`) | Sub-millisecond (concurrent) | Any | Same as ZGC; available in OpenJDK |

**Allocation pressure** is the root cause: objects allocated at high rates fill regions faster than G1 can reclaim them, triggering full GC. Reduce pressure by: reusing objects (pooling `byte[]` buffers), avoiding unnecessary boxing (`int` → `Integer` in hot paths), and keeping short-lived objects truly short-lived so they die in Eden without being tenured.

Tune G1 targets before switching collectors:
```
-XX:MaxGCPauseMillis=100 -XX:G1HeapRegionSize=16m -XX:InitiatingHeapOccupancyPercent=35
```

### 4. Container memory: use -XX:MaxRAMPercentage, not a hard -Xmx

The JVM does not know it is in a container by default on older versions. On Java 8u191+ and Java 11+ container-awareness is on by default (`-XX:+UseContainerSupport`), but **AI almost always hard-codes `-Xmx`** — and that value is then wrong when the pod's memory limit is changed.

```
# WRONG — hardcoded; becomes wrong when limits change; ignores non-heap (metaspace, code cache, off-heap)
-Xmx512m

# RIGHT — fraction of the container limit; non-heap overhead is taken from the remainder
-XX:MaxRAMPercentage=75.0
```

Leave 20–25% for off-heap: metaspace, code cache (JIT), direct byte buffers, stack memory. If you set `MaxRAMPercentage=90`, metaspace growth will push you over the cgroup limit → the OOM killer terminates the container even though the heap looks fine in your metrics.

Also set `-XX:MaxMetaspaceSize` explicitly to prevent unbounded metaspace growth on hot-redeploy or heavy reflection use.

### 5. Cold start: JIT warmup cost and mitigations

The JVM starts fast enough for long-running servers but is **hostile to serverless / scale-to-zero**:
- Bytecode is interpreted until HotSpot's profiler decides a method is "hot" (C1 after ~2000 invocations, C2 after ~10000).
- A freshly started JVM can be 5–20× slower for the first few thousand requests as JIT compilation runs concurrently.

This is the runtime-map reason "SKIP JVM for serverless with zero cold-start tolerance."

Mitigations (in ascending order of complexity):
1. **JVM warm-up in staging:** replay production traffic on a warm instance before directing real traffic to it (cloud load-balancer health-check delay).
2. **GraalVM native image:** AOT-compiles to a native binary — startup in milliseconds, no JIT warmup. Trades: no dynamic class loading at runtime, reflection must be configured, peak throughput may be lower than a warmed JVM.
3. **CRaC (Coordinated Restore at Checkpoint):** checkpoint a warmed JVM process image, restore it in milliseconds. Requires Linux kernel ≥ 5.9 + `libcriu`. Spring Boot 3.2+, Micronaut 4.2+ support CRaC.
4. **Class Data Sharing (AppCDS):** dump and share class metadata across JVM restarts — reduces startup by ~30%.

### 6. Shared mutable state: happens-before, volatile, final, and data races

The Java Memory Model (JMM) does not guarantee that writes by one thread are visible to another **without synchronization**. Invisible writes cause stale reads silently — no exception, just wrong values.

**Happens-before rules that matter:**
- A write to a `volatile` field happens-before every subsequent read of that field.
- `synchronized` exit happens-before subsequent entry on the same monitor.
- `Thread.start()` happens-before any action in the started thread.
- `Thread.join()` happens-before code after the join.

```java
// WRONG — not volatile; the reading thread may see stale `done`
private boolean done = false;
// Thread A: done = true;
// Thread B: while (!done) { ... } // may loop forever

// RIGHT
private volatile boolean done = false;

// RIGHT for compound check-then-act
private final AtomicBoolean done = new AtomicBoolean(false);
```

**Immutability via `final`:** fields written in a constructor and published safely (through a `final` reference or volatile) are guaranteed visible without further synchronization. Prefer immutable value types.

**`AtomicInteger`, `AtomicReference`, `LongAdder`:** use for lock-free counters/state. Use `LongAdder` over `AtomicLong` under high contention (stripes the counter).

### 7. ThreadLocal leaks: pooled threads and classloader leaks on redeploy

`ThreadLocal` state set in a pooled thread (Tomcat connector thread, HikariCP thread, thread-pool worker) **persists across requests** unless explicitly cleared. This causes two distinct failures:

**Context bleed:** request A stores tenant ID in `ThreadLocal`; the thread returns to the pool; request B gets the same thread and reads request A's tenant ID.

```java
// WRONG — set but never cleared
MDC.put("tenantId", tenantId);
// ... handle request ...
// thread returns to pool with tenantId still set

// RIGHT — always remove in finally
MDC.put("tenantId", tenantId);
try {
    // ... handle request ...
} finally {
    MDC.remove("tenantId"); // or MDC.clear() if you own the full context
}
```

**Classloader leak on hot-redeploy (Tomcat, JBoss):** a `ThreadLocal` holding a reference to a class loaded by the web-app classloader prevents that classloader from being garbage-collected. The old classloader hangs around → metaspace fills → `OutOfMemoryError: Metaspace` after several redeployments. Always `remove()` `ThreadLocal` values in a `finally` block or `Filter`/`Interceptor` cleanup.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** state the threading model (platform threads, virtual threads, reactive/event-loop) and justify it against the workload. Check pool sizing against Little's Law. A hard-coded `-Xmx` in a container manifest is a Gate 5 defect — flag it.
- **Gate 6 (implementation):** apply `ReentrantLock` over `synchronized` in virtual-thread paths; verify `ThreadLocal.remove()` in finally; confirm `MaxRAMPercentage` rather than fixed heap.
- **Gate 7 (review):** additionally check items 1–7 here for any JVM service.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability principles) → **up** to `mir-backend`.
- A specific library's mechanics (Spring `@Transactional`, Quarkus `@Blocking`, Micronaut `@ExecuteOn`, Hibernate fetch strategies) → **down** to the framework module (`mir-backend-jvm-<framework>`).
- **Here:** only what every JVM backend shares because of HotSpot — threading model, GC, container memory, cold start, JMM visibility, ThreadLocal hygiene.
- A different runtime (Python, Go, Node…) → its own `mir-backend-<runtime>` tier. Never widen this one.
