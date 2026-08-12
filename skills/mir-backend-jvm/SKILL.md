---
name: mir-backend-jvm
description: "Make It Right (JVM runtime tier). Java 25 LTS / Java 21 LTS / Kotlin runtime reliability footguns shared across EVERY JVM backend framework (Spring Boot, Quarkus, Micronaut, Vert.x, Helidon) — distinct from the generic backend gates and from any one framework's mechanics. Covers: thread-pool sizing and pool-exhaustion deadlock, blocking I/O on platform threads, virtual threads after JEP 491 (synchronized no longer pins on Java 24+, -Djdk.tracePinnedThreads removed, jdk.VirtualThreadPinned JFR event instead), virtual threads not bounding concurrency, GC choice (G1 vs ZGC vs Generational Shenandoah), container-aware heap sizing (-XX:MaxRAMPercentage vs hard -Xmx, -XX:+UseCompactObjectHeaders), cold start and the Leyden AOT cache (-XX:AOTCacheOutput / -XX:AOTCache) vs AppCDS vs GraalVM native image vs CRaC, shared-mutable-state visibility (happens-before, volatile, final, data races), ThreadLocal leaks in pooled threads, and JVM-level security (untrusted deserialization and ObjectInputFilter, XXE defaults in DocumentBuilderFactory, SSLSocket hostname verification, SSRF to the cloud metadata IP, ProcessBuilder argument vectors, zip-slip, heap-dump secret leakage, Security Manager permanently disabled since JDK 24, Maven/Gradle dependency verification and dependency confusion). TRIGGER when the backend runtime is Java or Kotlin on the JVM — sits between mir-backend (generic gates) and the framework module (e.g. mir-backend-jvm-spring). SKIP for Python, Node, Go, Rust, .NET, Ruby, PHP, BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics — @Transactional, Panache, @ExecuteOn, Hibernate fetch strategies and each framework's security config live in mir-backend-jvm-spring / -quarkus / -micronaut, not here."
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

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-jvm-spring`) knows the **library's mechanics**. This tier owns what's true for **all JVM backends because they run on the HotSpot JVM** — the threading model, garbage collector, memory model, process lifecycle, and JDK-level security defaults that Spring Boot, Quarkus, Micronaut, and Vert.x all inherit.

**Runtime assumed:** Java 25 LTS or Java 21 LTS, Java or Kotlin. Notes reference OpenJDK/HotSpot defaults. Load order: `mir-backend` → `mir-backend-jvm` → `<framework module>`.

**Read the actual JDK version out of the build file before applying any of this.** Spring Boot 4, Quarkus 3.x, and Micronaut 4.x all still declare a **Java 17 baseline**, so a service that uses a current framework can be running a JDK where half of this file does not apply. `<maven.compiler.release>`, `java.toolchain.languageVersion`, and the base image tag are the three places to look, and they disagree often.

**Version status, verified 13 Aug 2026:**

| Release | Status | What it means for a new service |
|---|---|---|
| **Java 25** | Current LTS. GA 16 Sep 2025, OpenJDK updates through Sep 2030 | The default target. Brings the AOT cache with method profiling, Generational Shenandoah, compact object headers |
| **Java 21** | Previous LTS, still supported | Fine to stay on, but `synchronized` still pins virtual threads here — see §2. Oracle's own builds move to the OTN license for updates published after Sep 2026 |
| **Java 17** | Older LTS, still the declared baseline of Spring Boot 4 / Quarkus 3.x / Micronaut 4.x | **Virtual threads do not exist.** All of §2 is inapplicable: no `Thread.ofVirtual()`, no `newVirtualThreadPerTaskExecutor()`, no `ScopedValue`, no AOT cache, no ZGC generational mode. Thread-per-request means platform threads and §1's pool sizing is the whole story |
| **Java 26** | Non-LTS. GA 17 Mar 2026, updates end Sep 2026 | Do not put a production SLA on it. JDK 27 (also non-LTS) is due Sep 2026 |
| **Java 24** | Non-LTS, past EOL | Matters only as the release where `synchronized` stopped pinning (JEP 491) |

## The JVM footguns AI walks into (framework-agnostic)

### 1. Thread-pool sizing and pool-exhaustion deadlock

Platform threads are expensive (≈1 MB stack each by default). The JVM's blocking I/O model means every request that waits on the DB, cache, or downstream HTTP holds a thread.

**Little's Law sets the floor:** `threads_needed = throughput_rps × latency_s`. At 500 req/s with 200 ms average latency you need 100 threads just to break even — the JVM default `ForkJoinPool.commonPool()` has only `CPU_cores - 1`.

**Pool-exhaustion deadlock** is the failure that produces no error: a pooled request thread submits a subtask to the *same* pool and then `get()`s on the future. If all pool slots are occupied waiting on their own subtasks, no thread is available to run those subtasks — the whole pool gridlocks. Every `.get()` or `join()` inside a thread-pool-backed executor is a suspect.

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

Bulkhead: give each downstream (DB, payment service, search) its own bounded pool. A single slow downstream can then only exhaust its own bulkhead, not every request path.

### 2. Virtual threads: what JEP 491 changed, and what it did not

On **platform threads**, blocking I/O (JDBC, sync `HttpClient`, `Thread.sleep`) holds the OS thread for the full duration. That is why reactive frameworks exist: they free the thread during the wait.

**Virtual threads** unmount from their carrier thread while blocked on I/O, so a thread-per-request blocking design scales without reactive code. JDK HTTP client, `Socket`, and file I/O all park correctly.

**The pinning advice in most existing code and blog posts is now out of date:**

| JDK | `synchronized` / `Object.wait()` while blocked | Correct action |
|---|---|---|
| 21–23 | Pins the carrier thread | Upgrade the JDK. Rewriting locks is the expensive way to fix this |
| **24+ (JEP 491)** | **Does not pin** — the carrier is released | Nothing. Leave `synchronized` alone |

```java
// STALE ADVICE — swapping synchronized for ReentrantLock "so virtual threads don't pin"
// buys nothing on Java 24+. Do not run that refactor for that reason.
private final ReentrantLock lock = new ReentrantLock();

// Choose ReentrantLock for its own features instead: tryLock(), timed acquisition,
// multiple Conditions, fairness. Choose synchronized when you need none of those.
```

Older JDBC drivers that used `synchronized` internally (a well-known source of pinning on Java 21) stop pinning on 24+ for the same reason. Driver upgrades are still worth doing, but they are no longer the fix for pinning.

**Still pins on Java 24+,** per JEP 491's own list: blocking inside native code, or when native/JNI/FFM code calls back into Java and *that* callback blocks or waits on a monitor; blocking while a symbolic reference is being resolved and a class loaded; blocking inside a class initializer; and waiting for another thread to finish initializing a class.

**The diagnostic flag was removed in JDK 24.** Setting it is not an error — it is a system property, so the JVM starts normally, prints nothing, and you conclude there is no pinning.

```bash
# WRONG on Java 24+ — removed, silently does nothing
java -Djdk.tracePinnedThreads=full -jar app.jar

# RIGHT — the jdk.VirtualThreadPinned JFR event, enabled by default with a 20 ms threshold
jcmd <pid> JFR.start name=pin settings=profile duration=60s filename=pin.jfr
jfr summary pin.jfr | grep VirtualThreadPinned
```

**Virtual threads do not bound concurrency — they remove the bound.** A million virtual threads hitting a 20-connection JDBC pool produce a million-deep queue at the pool, not a faster service. Whatever is finite (DB connections, a downstream rate limit, a vendor quota) is the real ceiling. Declare it: a `Semaphore` per dependency, or a bounded pool, and shed load when it is full instead of queueing without limit.

**Never pool virtual threads.** They are cheap to create and are meant to be one per task. `Executors.newVirtualThreadPerTaskExecutor()` is the whole API; a fixed-size virtual-thread pool re-introduces the queueing you switched to avoid.

**`ThreadLocal` caching becomes an allocation problem.** Libraries that cache an expensive object per thread (`ThreadLocal<SimpleDateFormat>`, per-thread byte buffers, object pools) assume a small, stable thread set. One virtual thread per request means one fresh copy per request. Audit hot paths for `ThreadLocal` pooling before moving them onto virtual threads.

### 3. GC pauses: G1 vs ZGC vs Shenandoah

The default collector (G1) targets throughput with occasional stop-the-world pauses. A full GC can pause all application threads for hundreds of milliseconds to several seconds on large heaps.

| Collector | Pause target | Heap sweet spot | Use when |
|---|---|---|---|
| G1 (default) | 200 ms (tunable with `-XX:MaxGCPauseMillis`) | 4 GB – 64 GB | General-purpose; CPU to spare |
| ZGC (`-XX:+UseZGC`) | Sub-millisecond, concurrent | Any | Latency-sensitive APIs, trading, real-time |
| Shenandoah (`-XX:+UseShenandoahGC`) | Sub-millisecond, concurrent | Any | Same as ZGC; Java 25 adds a generational mode |

ZGC's old non-generational mode has been removed from current JDKs, so flags copied from pre-2024 articles (`-XX:+ZGenerational` and friends) may be unrecognised or ignored. Confirm against your exact JDK with `java -XX:+PrintFlagsFinal -version | grep -i zgc` instead of trusting a blog post.

**Allocation pressure** is the root cause of most G1 trouble: objects allocated faster than G1 reclaims regions trigger full GC. Reduce it by reusing buffers, avoiding boxing (`int` → `Integer`) in hot paths, and keeping short-lived objects short-lived so they die in Eden instead of being promoted.

Tune G1 targets before switching collectors:
```
-XX:MaxGCPauseMillis=100 -XX:G1HeapRegionSize=16m -XX:InitiatingHeapOccupancyPercent=35
```

### 4. Container memory: use -XX:MaxRAMPercentage, not a hard -Xmx

Container awareness is on by default (`-XX:+UseContainerSupport`), but **AI almost always hard-codes `-Xmx`** — and that value is then wrong the moment the pod's memory limit changes.

```
# WRONG — hardcoded; becomes wrong when limits change; ignores non-heap (metaspace, code cache, off-heap)
-Xmx512m

# RIGHT — fraction of the container limit; non-heap overhead is taken from the remainder
-XX:MaxRAMPercentage=75.0
```

Leave 20–25% for off-heap: metaspace, code cache (JIT), direct byte buffers, thread stacks. At `MaxRAMPercentage=90`, metaspace growth pushes the process past the cgroup limit and the OOM killer terminates the container while your heap metrics still look healthy.

Do **not** set `-XX:MaxMetaspaceSize` as a reflex — an arbitrary cap converts normal class-metadata growth into an avoidable `OutOfMemoryError: Metaspace`, and it hides a classloader leak instead of surfacing it. Measure class count and native memory first; add a tested cap only when the container budget demands one, with headroom for framework-generated classes, proxies, and agents.

On Java 25, `-XX:+UseCompactObjectHeaders` (JEP 519, a product option since 25 — confirm with `java -XX:+PrintFlagsFinal -version | grep UseCompactObjectHeaders`) cuts the HotSpot object header to 64 bits, from 96 with compressed class pointers or 128 without. Actual savings depend on field layout and alignment, so measure the real heap before raising a pod's memory request.

### 5. Cold start: JIT warmup and the AOT cache

The JVM starts fast enough for long-running servers but is **hostile to serverless / scale-to-zero**:
- Bytecode is interpreted until HotSpot's profiler decides a method is "hot" (C1 after ~2000 invocations, C2 after ~10000).
- A freshly started JVM can be 5–20× slower for the first few thousand requests as JIT compilation runs concurrently.

This is the runtime-map reason "SKIP JVM for serverless with zero cold-start tolerance."

Mitigations, current as of Java 25:

| Option | Needs | What it removes |
|---|---|---|
| **AOT cache** (`-XX:AOTCache`) | JDK 24+ (JEP 483) | Class loading and linking work at startup |
| **AOT cache + method profiles** | JDK 25+ (JEP 514/515) | Also the JIT's profiling phase — the JIT starts from recorded profiles |
| AOT object caching with any GC | JDK 26+ (JEP 516) | The earlier restriction that excluded ZGC |
| AppCDS | pre-24 JDKs | Class metadata parsing only — superseded by the AOT cache |
| GraalVM native image | closed-world build | The JIT entirely; millisecond startup, but reflection must be registered and peak throughput can be lower than a warmed JVM |
| CRaC | Linux + CRIU | Everything — restores a warmed process image. Supported by Spring Boot since 3.2 and Micronaut since 4.2 |
| Traffic warm-up | a load balancer you control | Nothing, but it keeps cold instances out of rotation until they are warm |

```bash
# Training run — writes app.aot (and app.aot.config)
java -XX:AOTCacheOutput=app.aot -jar app.jar
# Production run
java -XX:AOTCache=app.aot -jar app.jar
```

Two AOT-cache footguns:
- **The cache is only used when the production JVM and its command-line options match the training run.** Change the JDK build, the GC, or `-Xmx` and the cache is dropped; startup quietly returns to baseline and nothing tells you. Build the cache in the same CI step that builds the image, and pin the JDK and the flags together.
- **The one-command training workflow runs a second JVM with the same heap size**, so it needs roughly double the training heap. In a memory-capped CI runner, use the explicit two-step workflow instead.

### 6. Shared mutable state: happens-before, volatile, final, and data races

The Java Memory Model does not guarantee that writes by one thread are visible to another **without synchronization**. Invisible writes cause stale reads silently — no exception, just wrong values.

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

**Immutability via `final`:** fields written in a constructor and published safely (through a `final` reference or volatile) are guaranteed visible without further synchronization. Prefer immutable value types (`record`).

**`AtomicInteger`, `AtomicReference`, `LongAdder`:** use for lock-free counters/state. Use `LongAdder` over `AtomicLong` under high contention (stripes the counter).

### 7. ThreadLocal leaks: pooled threads and classloader leaks on redeploy

`ThreadLocal` state set on a pooled thread (Tomcat connector thread, HikariCP thread, thread-pool worker) **persists across requests** unless explicitly cleared. Two distinct failures follow.

**Context bleed:** request A stores a tenant ID in `ThreadLocal`; the thread returns to the pool; request B gets the same thread and reads request A's tenant ID. This is a data-leak bug, not only a correctness bug — see Security below.

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

On Java 25, `ScopedValue` is the immutable, structured alternative for request context: the value is bound for the duration of a call and unbound automatically, so there is nothing to clear and nothing to leak.

## Security

JDK-level security defaults and mechanics. Framework auth configuration (`@PreAuthorize`, `@RolesAllowed`, `@Secured`) belongs in the framework module.

### Insecure or surprising JDK defaults, named

| API / setting | Default | What breaks | Write instead |
|---|---|---|---|
| `ObjectInputStream` | No filter — accepts any class named in the stream | Gadget-chain RCE from untrusted bytes | An allow-list filter (below), or do not accept Java serialization from untrusted sources at all |
| `DocumentBuilderFactory`, `SAXParserFactory`, `TransformerFactory`, `XMLInputFactory` | DTDs and external entities processed | XXE: local file read, SSRF, billion-laughs DoS | `dbf.setFeature("http://apache.org/xml/features/disallow-doctype-decl", true)` plus `setAttribute(XMLConstants.ACCESS_EXTERNAL_DTD, "")` and `ACCESS_EXTERNAL_SCHEMA`/`ACCESS_EXTERNAL_STYLESHEET` set to `""` |
| `SSLSocket` / `SSLEngine` used directly | Certificate chain validated, **hostname not** | A valid certificate for any host passes — MITM | `SSLParameters p = sock.getSSLParameters(); p.setEndpointIdentificationAlgorithm("HTTPS"); sock.setSSLParameters(p);` |
| `-XX:+HeapDumpOnOutOfMemoryError` | Writes the whole heap — secrets, tokens, PII — to `HeapDumpPath` | Credentials land on a shared volume or in a support bundle | Point `-XX:HeapDumpPath` at a restricted path; treat dumps as secret material |
| Attach mechanism and JMX | Attach enabled; an unauthenticated JMX port is remote code execution | Local or remote takeover | `-XX:+DisableAttachMechanism` in production images; never expose JMX without TLS and authentication |
| Security Manager | **Permanently disabled since JDK 24 (JEP 486)** | `-Djava.security.manager` makes the JVM exit; `AccessController.checkPermission` always throws | Do not design an in-JVM sandbox for untrusted code. Isolate with a separate process, container, or seccomp profile |
| JNI / FFM native access | JDK 24's default is `--illegal-native-access=warn`; `deny` is the announced future default | A later JDK upgrade turns today's warnings into `IllegalCallerException` at runtime | Declare `--enable-native-access=ALL-UNNAMED` (or the explicit module list) now |

### Deserialization of untrusted input

There is no restrictive JDK-wide default filter: an unconfigured JVM deserializes whatever the stream names. Gadget chains are not a legacy problem — research published through 2024–2026 shows chains that still resolve against current library and JDK releases, including reconstructed chains that work on Java 16+ and evade pattern-based filters.

```java
// WRONG — untrusted bytes straight into ObjectInputStream
Object o = new ObjectInputStream(request.getInputStream()).readObject();

// RIGHT — per-stream allow-list filter plus resource limits; deny everything else
var filter = ObjectInputFilter.Config.createFilter(
    "com.example.dto.*;java.base/java.lang.*;maxdepth=10;maxarray=1000;!*");
ObjectInputStream in = new ObjectInputStream(bytes);
in.setObjectInputFilter(filter);
```

- A JVM-wide filter belongs in `$JAVA_HOME/conf/security/java.security` as the `jdk.serialFilter` **security property**, not as a `-D` system property — the system-property form has been shown to be overridable at runtime (the CVE-2020-2604 pattern).
- The filter covers `ObjectInputStream` only. Jackson, SnakeYAML, Kryo, Hessian, and XML decoders have separate paths and need separate hardening: no polymorphic typing on untrusted JSON, `new Yaml(new SafeConstructor(new LoaderOptions()))` for SnakeYAML, the XXE flags above for XML.
- Best result: keep `Serializable` off your wire protocol. Records plus schema-validated JSON or protobuf have no implicit gadget path.

### SSRF, redirects, and the metadata IP

```java
// WRONG — user-supplied URL fetched as-is, redirects followed
var res = HttpClient.newBuilder().followRedirects(Redirect.ALWAYS).build()
    .send(HttpRequest.newBuilder(URI.create(userUrl)).build(), ofString());

// RIGHT — check the scheme, resolve the host, reject internal addresses,
// and re-run the check on every redirect hop you follow yourself
if (!Set.of("http", "https").contains(uri.getScheme())) throw new IllegalArgumentException();
InetAddress addr = InetAddress.getByName(uri.getHost());
if (addr.isLoopbackAddress() || addr.isLinkLocalAddress()
        || addr.isSiteLocalAddress() || addr.isAnyLocalAddress()) {
    throw new IllegalArgumentException("blocked address");
}
```

`169.254.169.254` is link-local, so rejecting link-local addresses rejects the cloud instance-metadata endpoint — the payoff for most SSRF attempts. Restrict schemes explicitly: `URL`/`URLConnection` also reach `file:`, `jar:`, and `ftp:`. `java.net.http.HttpClient` does not follow redirects unless configured to; keep it that way and follow them yourself, so an `Authorization` header is never replayed to a host the user chose.

**The check above is necessary and not sufficient.** `getByName` returns one address, and the HTTP client then resolves the hostname *again* — a DNS-rebinding TOCTOU, and a name with several A records can pass on the address you checked and connect to another. Check every address `getAllByName` returns, and treat an egress proxy or firewall allow-list as the actual control; the in-process check is defence in depth.

### Command injection, path traversal, zip slip

`Runtime.exec(String)` does **not** invoke a shell — it splits the string on whitespace with a `StringTokenizer`. So `;` and `|` do not execute anything; the bug is that a filename containing a space silently becomes two arguments, and an argument starting with `-` becomes a flag to the callee. Real metacharacter injection needs you to launch a shell yourself (`sh -c`, `cmd /c`) — never put untrusted text there.

```java
// WRONG — whitespace in userFile changes the argument boundaries
Runtime.getRuntime().exec("convert " + userFile + " out.png");

// RIGHT — explicit argument vector, boundaries fixed by you
new ProcessBuilder(List.of("convert", "--", userFile, "out.png")).start();
```

```java
// WRONG — "../../etc/passwd" escapes baseDir
Path p = baseDir.resolve(userPath);

// RIGHT — normalize, then prove containment
Path p = baseDir.resolve(userPath).normalize();
if (!p.startsWith(baseDir)) throw new SecurityException("traversal");
```

`normalize()` + `startsWith` is **lexical** containment: it rejects `..` and rooted paths, but a symlink already under `baseDir` still resolves outside it. When the target exists, compare `toRealPath()` instead; when creating, write into a directory the user cannot plant links in.

The same check applies to every `ZipEntry.getName()` when unpacking an upload (zip slip) and to multipart `Content-Disposition` filenames. Never use a client-supplied filename as a path element — generate your own name and keep the original as metadata.

### Secrets, PII, and logs

- **Never pass a secret as a command-line argument.** Any local process can read it via `ProcessHandle.info().arguments()` or `/proc/<pid>/cmdline`. Use an environment variable, a mounted file, or a secrets client.
- **`record` and Lombok `@Data` generate a `toString()` that prints every field.** One `log.info("user={}", user)` puts a password hash, email, and token in the log. Override `toString()` on any type holding credentials or PII, or log an explicit projection.
- **Log injection:** user input containing `\n` or `\r` forges log entries and can poison a downstream parser. Use parameterized logging (`log.info("id={}", id)`), never concatenation into the pattern, and strip control characters from user-controlled fields.
- **Thread-context bleed is a data leak.** A `ThreadLocal`/MDC tenant or user ID left on a pooled thread (§7) can put tenant A's identifier on tenant B's audit record. Clear it in `finally`, or use `ScopedValue`.
- Stack traces returned to clients disclose class names, SQL fragments, and file paths. Map exceptions to a generic response body in the framework's error handler and keep the detail in the log behind a correlation ID.

### Supply chain (Maven / Gradle)

| Risk | Mechanism in this ecosystem |
|---|---|
| Tampered artifact | Gradle: `gradle/verification-metadata.xml` (`--write-verification-metadata sha256,pgp`) verifies checksums and signatures for every dependency and plugin. Maven: `-C` / `--strict-checksums` fails the build on a checksum mismatch |
| Unpinned transitives | Maven: `<dependencyManagement>` or an imported BOM pins every transitive version. Gradle: a `platform()` dependency, or `dependencyLocking` writing `gradle.lockfile` |
| Silent version drift | Ban dynamic versions — `LATEST`, `RELEASE`, `1.+`, ranges like `[1.0,2.0)`. `maven-enforcer-plugin` with `banDynamicVersions` and `requireReleaseDeps` fails the build instead of resolving whatever shipped last night |
| Dependency confusion | An internal coordinate resolved from Maven Central because someone published the same groupId publicly. Gradle: `exclusiveContent { forRepository { ... } filter { includeGroup("com.yourco") } }`. Maven: one virtual repository in Nexus/Artifactory with the internal repo ordered first, everything else mirrored through it |
| Code execution at build time | `build.gradle(.kts)`, `settings.gradle`, Gradle plugins, and Maven build extensions run arbitrary code during the build — this ecosystem's equivalent of npm install scripts. Review plugin version bumps as carefully as application dependencies, and validate `gradle-wrapper.jar` in CI |

Run a dependency scanner in CI (OWASP Dependency-Check or your platform's equivalent) and fail the build on known-vulnerable **direct and transitive** versions. At the JDK level, the vulnerability class that reaches the default path is untrusted deserialization; there is no JDK-shipped filter protecting you, so it is your configuration or nothing.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** state the threading model (platform threads, virtual threads, reactive/event-loop) and justify it against the workload. Check pool sizing against Little's Law. State the JDK version explicitly — the correct virtual-thread advice differs between 21 and 24+. A hard-coded `-Xmx` in a container manifest is a Gate 5 defect — flag it.
- **Gate 6 (implementation):** verify `ThreadLocal.remove()` in `finally`; confirm `MaxRAMPercentage` rather than fixed heap; declare a concurrency bound per downstream when running on virtual threads; apply the Security defaults table to any XML parser, `ObjectInputStream`, raw `SSLSocket`, or process launch in the diff.
- **Gate 7 (review):** check items 1–7 plus the Security section for any JVM service. Confirm the JDK build and JVM flags used to produce an AOT cache match the runtime image.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability principles) → **up** to `mir-backend`.
- A specific library's mechanics (Spring `@Transactional`, Quarkus `@Blocking`, Micronaut `@ExecuteOn`, Hibernate fetch strategies, and each framework's own auth config and CVEs) → **down** to the framework module (`mir-backend-jvm-<framework>`).
- **Here:** only what every JVM backend shares because of HotSpot and the JDK — threading model, GC, container memory, cold start, JMM visibility, ThreadLocal hygiene, and JDK-level security defaults.
- A different runtime (Python, Go, Node…) → its own `mir-backend-<runtime>` tier. Never widen this one.
