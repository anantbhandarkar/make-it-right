---
name: mir-backend-jvm-quarkus
description: "Make It Right (Quarkus module). Quarkus + Hibernate ORM / Panache + RESTEasy / Reactive Routes + SmallRye Mutiny reliability footguns specific to this framework stack. Covers: build-time DI / annotation processing (reflection for GraalVM native image must be registered with @RegisterForReflection or fails only at native runtime), blocking the Vert.x I/O thread on reactive routes (annotate blocking work @Blocking), Mutiny async composition pitfalls (blocking inside Uni/Multi pipelines), build-time vs runtime config key differences, Dev Services, and native-image gotchas (dynamic proxies, serialization, resources). Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-jvm (JVM runtime concerns: thread pools, virtual threads, GC, container heap, cold start, JMM visibility, ThreadLocal hygiene); this module only adds Quarkus library mechanics. TRIGGER only when the JVM backend stack is Quarkus — building, reviewing, or debugging a Quarkus REST resource, CDI bean, Panache entity, Mutiny pipeline, or native image build. SKIP for Spring Boot, Micronaut, Vert.x standalone, or any non-Quarkus JVM framework (those get their own mir-backend-jvm-<framework> module), and for non-JVM runtimes."
trigger: /mir-backend-jvm-quarkus
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-jvm-quarkus · Make It Right (Quarkus)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-jvm` (JVM runtime model) → **this** (Quarkus library mechanics). Run the gates first; load the JVM runtime tier for threading, GC, and container-heap concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (virtual-thread pinning, pool sizing, GC tuning, `-XX:MaxRAMPercentage`, ThreadLocal hygiene, JMM visibility) live in `mir-backend-jvm` — not here.**

**Stack assumed:** Quarkus 3.x · CDI (ArC) · RESTEasy Reactive or Classic · Hibernate ORM with Panache · SmallRye Mutiny · GraalVM CE / Mandrel for native image.

## The Quarkus footguns AI walks into most

### 1. Build-time DI — missing beans fail at build, not runtime (mostly)

Quarkus resolves CDI beans at build time using ArC (a build-time CDI implementation). Most injection errors surface as build failures, which is good. But AI regularly writes code that assumes runtime reflection is available, then ships a native image that fails on first deployment.

```java
// WRONG — a class only accessed via reflection (e.g., Jackson deserialization target,
// JPA converter, or a dynamically-loaded strategy) is silently excluded in native image
public class PayloadConverter implements AttributeConverter<Payload, String> { ... }

// RIGHT — register for reflection explicitly
@RegisterForReflection
public class PayloadConverter implements AttributeConverter<Payload, String> { ... }

// For third-party classes you can't annotate, use a @RegisterForReflection holder:
@RegisterForReflection(targets = { ThirdPartyClass.class, AnotherClass.class })
public class ReflectionConfig {}
```

Also register dynamic proxies and resources:
```java
// Dynamic proxy for an interface used at runtime (e.g., custom RestClient interface)
// quarkus.native.additional-build-args=--initialize-at-build-time=com.example.MyInterface

// Static resources included at build time
quarkus.native.resources.includes=templates/*.html,certs/*.pem
```

**Build-time vs runtime config:** Quarkus bakes some config values into the native binary at build time (tagged `@ConfigProperty` defaults that affect build-time extensions — e.g., datasource URL during codegen). If you change a build-time key at container start-time only, the native image ignores it. Runtime config keys (most application keys) are read at startup and work as expected. Check the Quarkus config reference for which keys are `STATIC_INIT` vs `RUNTIME_INIT`.

### 2. Blocking the Vert.x I/O thread: the silent performance cliff

Quarkus's reactive core runs on a Vert.x event loop. When you annotate a JAX-RS resource with `@Path` and it uses RESTEasy Reactive (the default in Quarkus 3), the method **runs on the I/O thread by default**. Blocking that thread — with JDBC, `Thread.sleep`, synchronous HTTP calls, or any blocking I/O — stalls *all* requests handled by that event-loop thread.

```java
// WRONG — runs on I/O thread; JDBC call blocks the event loop
@GET
@Path("/report")
public Report getReport() {
    return reportRepository.buildHeavyReport(); // blocking JDBC inside event loop
}

// RIGHT — annotate with @Blocking to move to the worker thread pool
@GET
@Path("/report")
@Blocking
public Report getReport() {
    return reportRepository.buildHeavyReport(); // now runs on Quarkus worker pool
}

// ALSO RIGHT — use Panache reactive and return Uni/Multi
@GET
@Path("/report")
public Uni<Report> getReport() {
    return reportRepository.buildHeavyReportAsync(); // non-blocking, stays on I/O thread
}
```

The distinction: `@Blocking` tells RESTEasy Reactive to dispatch to the worker pool. Omitting it on a blocking path is a correctness issue, not just a performance one — the event loop will back up silently.

For imperative (Classic) REST resources and CDI beans using `@Inject` + Hibernate ORM (non-reactive), the blocking is expected and Quarkus runs them on worker threads automatically — `@Blocking` is not needed there.

### 3. Mutiny async composition: blocking inside Uni/Multi pipelines

SmallRye Mutiny is Quarkus's reactive library. AI writes Mutiny chains that look reactive but smuggle blocking calls inside lambdas.

```java
// WRONG — blocking call inside Uni.createFrom().item() runs on the calling (I/O) thread
Uni<Order> getOrder(Long id) {
    return Uni.createFrom().item(() -> jdbcOrderRepo.findById(id)); // BLOCKS I/O thread
}

// RIGHT — use Uni.createFrom().item().runSubscriptionOn(executor) to offload
Uni<Order> getOrder(Long id) {
    return Uni.createFrom().item(() -> jdbcOrderRepo.findById(id))
              .runSubscriptionOn(Infrastructure.getDefaultWorkerPool());
}

// PREFERRED — use Hibernate Reactive / Panache Reactive so the call is truly non-blocking
Uni<Order> getOrder(Long id) {
    return Order.findById(id); // Panache reactive — no blocking I/O
}
```

**Never call `.await().indefinitely()` or `.await().atMost(...)` on the I/O thread.** These are blocking terminal operations and will deadlock or degrade the event loop. They are acceptable only in tests or main-thread startup code.

**`Multi` back-pressure:** if you produce a `Multi` faster than the subscriber consumes it and don't apply `onOverflow()`, items are silently dropped or the pipeline throws `MissingBackPressureFailure`. Always declare overflow strategy explicitly when producing unbounded streams.

### 4. Panache and the active record gotcha: query inside a transaction

Panache's active record pattern (`Entity.find(...)`, `Entity.persist()`) is convenient but the entity methods execute within the **current transaction context** (or no transaction if called outside one). AI forgets to annotate service methods `@Transactional` when multiple Panache calls must be atomic.

```java
// WRONG — two Panache operations not wrapped in a transaction;
// if the second fails, the first persists anyway
public void transfer(Long fromId, Long toId, BigDecimal amount) {
    Account from = Account.findById(fromId);
    from.balance = from.balance.subtract(amount);
    from.persist(); // committed immediately in auto-commit mode

    Account to = Account.findById(toId);
    to.balance = to.balance.add(amount);
    to.persist(); // if this throws, from is already saved
}

// RIGHT
@Transactional
public void transfer(Long fromId, Long toId, BigDecimal amount) { ... }
```

For reactive Panache, the transaction must be reactive too: use `@WithTransaction` (Hibernate Reactive Panache) or `Panache.withTransaction(() -> ...)`.

### 5. Dev Services: they don't exist in production

Quarkus Dev Services automatically starts containers (PostgreSQL, Redis, Kafka…) in dev and test modes via Testcontainers. This is great for fast iteration but causes AI to omit real datasource configuration, assuming Dev Services will always be available.

```properties
# WRONG — no production datasource config; works in dev, fails on deploy
# (application.properties is empty of datasource settings)

# RIGHT — always provide production config in application.properties;
# Dev Services kick in only when no matching config is found
%prod.quarkus.datasource.db-kind=postgresql
%prod.quarkus.datasource.username=${DB_USER}
%prod.quarkus.datasource.password=${DB_PASSWORD}
%prod.quarkus.datasource.jdbc.url=jdbc:postgresql://${DB_HOST}:5432/${DB_NAME}
```

Use profile-prefixed keys (`%prod.`, `%staging.`) to keep dev/prod configs clearly separated. The absence of `%prod.` keys is a common cause of "works locally, broken in CI" issues.

### 6. Native image: initialization order and static initializers

GraalVM native image runs class initializers at build time unless told otherwise. A static initializer that opens a network connection, reads from the filesystem, or depends on environment variables will run during the `native-image` build — and will fail or bake stale state into the binary.

```java
// WRONG — static initializer contacts a service at build time
public class ConfigClient {
    private static final String remoteValue = fetchFromConfigServer(); // runs at build!
}

// RIGHT — initialize lazily at runtime
public class ConfigClient {
    private static volatile String remoteValue;
    public static String get() {
        if (remoteValue == null) remoteValue = fetchFromConfigServer();
        return remoteValue;
    }
}

// OR force runtime init in build config
quarkus.native.additional-build-args=--initialize-at-run-time=com.example.ConfigClient
```

Also: lambdas that capture mutable state, `Random` seeded at build time, and `java.time` initialization can bake unexpected build-time state. The native image build log lists substitutions and initialization — read it before declaring the build successful.

## How this slots into the core pipeline

- **Gate 5 (Design):** identify whether the route is reactive or imperative. For reactive routes, state which operations are blocking and where `@Blocking` or `runSubscriptionOn` is applied. Confirm production config profiles exist alongside Dev Services setup.
- **Gate 6 (Implementation):** `@RegisterForReflection` on all reflection targets; `@Blocking` on any blocking JAX-RS method in reactive mode; `@Transactional` (or `@WithTransaction` for reactive) wrapping multi-step Panache operations; production datasource keys under `%prod.`.
- **Gate 7 (Review):** the reliability-reviewer additionally checks items 1–6 here for any Quarkus service; native image builds should be smoke-tested with the reflection and static-init checklist.

## Edit boundary (what belongs here vs. above/below)

Apply the 3-tier placement test before adding anything:

- True for Go/Node/Python too (idempotency, invariants, gates, observability)? → **generic core** (`mir-backend`).
- True for every JVM framework (thread-pool sizing, virtual-thread pinning, GC tuning, container heap, ThreadLocal hygiene, JMM visibility)? → **runtime tier** (`mir-backend-jvm`).
- A mechanical footgun of *this library* (`@RegisterForReflection`, `@Blocking` on I/O thread, Mutiny pipeline blocking, Panache `@Transactional`, Dev Services absent in prod, native-image static initializers)? → **here**.
- A *different* JVM framework (Spring Boot, Micronaut) → its own `mir-backend-jvm-<framework>` module. A *different* runtime → its own tier. Never widen this one.
