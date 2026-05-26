---
name: mir-backend-jvm-micronaut
description: "Make It Right (Micronaut module). Micronaut + Micronaut Data + Micronaut Security + Netty HTTP server reliability footguns specific to this framework stack. Covers: compile-time DI and AOT (no runtime reflection — missing bean is a compile error, not a runtime NPE), bean scope pitfalls (@Singleton default, don't hold request state), blocking the Netty event loop (use @ExecuteOn or reactive types), Micronaut Data repository transaction scoping, reactive types (RxJava / Reactor / Kotlin coroutines) and not blocking the event loop inside them, native-image / GraalVM considerations at the Micronaut layer, and compile-time AOP interceptor limitations. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-jvm (JVM runtime concerns: thread pools, virtual threads, GC, container heap, cold start, JMM visibility, ThreadLocal hygiene); this module only adds Micronaut library mechanics. TRIGGER only when the JVM backend stack is Micronaut — building, reviewing, or debugging a Micronaut controller, service, repository, filter, or security rule. SKIP for Spring Boot, Quarkus, Vert.x standalone, or any non-Micronaut JVM framework (those get their own mir-backend-jvm-<framework> module), and for non-JVM runtimes."
trigger: /mir-backend-jvm-micronaut
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-jvm-micronaut · Make It Right (Micronaut)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-jvm` (JVM runtime model) → **this** (Micronaut library mechanics). Run the gates first; load the JVM runtime tier for threading, GC, and container-heap concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (virtual-thread pinning, pool sizing, GC tuning, `-XX:MaxRAMPercentage`, ThreadLocal hygiene, JMM visibility) live in `mir-backend-jvm` — not here.**

**Stack assumed:** Micronaut 4.x · Micronaut Data JPA (Hibernate) or Micronaut Data JDBC · Micronaut Security · Micronaut HTTP Server (Netty) · GraalVM CE / Mandrel for native image.

## The Micronaut footguns AI walks into most

### 1. Compile-time DI — missing bean is a compile error, not a runtime NPE

Micronaut generates all DI glue at compile time via annotation processors (no runtime reflection for injection). This means:

- A missing or ambiguous bean binding fails the **build**, not the first request. This is strictly better than Spring's runtime `NoSuchBeanDefinitionException` — but AI sometimes works around compilation failures by adding `@Nullable` or suppressing errors instead of fixing the missing binding.
- Classes used only via reflection (Jackson mixins, custom TypeConverter, serialization targets for native image) are **not automatically discovered**. Register them explicitly.

```java
// WRONG — assumes runtime classpath scanning will find the bean
@Controller("/orders")
public class OrderController {
    @Inject
    OrderService service; // compile error if OrderService has no @Singleton/@Service
}

// RIGHT — ensure the injected type has a bean-defining annotation
@Singleton
public class OrderService { ... }

// Ambiguous bean: two @Singleton implementations of the same interface
// → compile error "Multiple bean definitions found for type..."
// FIX: qualify with @Named or @Primary
@Singleton
@Named("fast")
public class FastOrderService implements OrderService { ... }

@Inject @Named("fast") OrderService service;
```

**`@Factory` for third-party types:** if you need to inject a type you can't annotate, use `@Factory`:
```java
@Factory
public class RedisFactory {
    @Singleton
    RedisClient redisClient(RedisConfig cfg) {
        return RedisClient.create(cfg.getUri());
    }
}
```

### 2. @Singleton default scope: never store per-request state

All Micronaut beans default to `@Singleton` — one instance, all threads. This is the same trap as Spring's singleton scope, but Micronaut's lack of a default `@RequestScope` proxy makes it easier to miss.

```java
// WRONG — tenantId is a shared mutable field; threads stomp on each other
@Singleton
public class TenantContext {
    private String tenantId; // shared across all concurrent requests
    public void set(String t) { this.tenantId = t; }
    public String get() { return tenantId; }
}

// RIGHT — pass tenant context as a method argument, or use a @RequestScope bean
// @RequestScope requires the HttpRequest to be in scope; use carefully
@RequestScope
public class TenantContext {
    private String tenantId;
    public void set(String t) { this.tenantId = t; }
    public String get() { return tenantId; }
}
// Inject into singleton with @Inject Provider<TenantContext> ctx;
// so each invocation gets the request-scoped instance, not a singleton
```

Available scopes: `@Singleton`, `@Prototype` (new instance per injection point), `@RequestScope` (per HTTP request), `@ThreadLocal` (per thread — correct for pooled-thread use). Default to `@Singleton` for stateless services; use `@Prototype` for stateful helpers.

### 3. Blocking the Netty event loop: @ExecuteOn is mandatory for blocking work

Micronaut's HTTP server runs on Netty's event loop. Like Quarkus reactive, **blocking the event loop thread degrades the entire server** — one blocking call can stall all concurrent requests on that loop.

```java
// WRONG — controller method runs on the event loop by default; JDBC blocks it
@Get("/report")
public Report getReport() {
    return reportService.buildHeavyReport(); // blocking JDBC on event loop → server stall
}

// RIGHT — @ExecuteOn dispatches to a named thread pool
@Get("/report")
@ExecuteOn(TaskExecutors.IO)    // Micronaut's built-in I/O thread pool
public Report getReport() {
    return reportService.buildHeavyReport(); // runs on IO pool, event loop is free
}

// ALSO RIGHT — return a reactive type; Micronaut keeps the call non-blocking
@Get("/report")
public Single<Report> getReport() {
    return reportService.buildHeavyReportAsync(); // reactive, event loop not blocked
}
```

`TaskExecutors.IO` is a cached thread pool sized to `2 × CPU_cores` by default. For sustained blocking workloads, configure a custom executor via `micronaut.executors.io.type=fixed` and set `nThreads` explicitly.

For **Kotlin coroutines**: mark the controller function `suspend` — Micronaut detects this and runs it on `Dispatchers.IO` automatically when the function does blocking I/O. Do not mix `runBlocking` on the event loop.

### 4. Micronaut Data transactions: @Transactional and repository scope

Micronaut Data uses compile-time-generated repositories (no runtime proxies). `@Transactional` is applied via a compile-time AOP interceptor — which means **self-invocation bypasses the interceptor**, same as Spring.

```java
// WRONG — saveWithNotification calls notify() on this; interceptor not involved
@Singleton
public class OrderService {
    @Transactional
    public void saveWithNotification(Order o) {
        repo.save(o);
        notify(o); // direct call — @Transactional on notify() is not applied
    }

    @Transactional(Transactional.TxType.REQUIRES_NEW)
    public void notify(Order o) { ... } // never runs in its own tx
}

// RIGHT — extract to a separate @Singleton bean
@Singleton
public class NotificationService {
    @Transactional(Transactional.TxType.REQUIRES_NEW)
    public void notify(Order o) { ... }
}
```

Micronaut Data repositories operate in the transaction of their caller. If you call a repository from outside a `@Transactional` boundary, each repository call gets its own auto-committed mini-transaction. This is correct for reads but breaks for multi-step writes.

**JDBC repositories:** Micronaut Data JDBC uses a simpler mapping layer than JPA — no lazy loading, no session, no `LazyInitializationException`. N+1 is still possible via explicit nested queries in a loop; use `@Join` on repository finder methods to specify eager joins.

### 5. Compile-time AOP interceptors: what they can and cannot intercept

Micronaut AOP is compile-time — the framework generates interceptor wrappers at build time. This is fast and native-image-friendly but has hard constraints:

- **Only beans managed by Micronaut's DI container can be intercepted.** Objects created with `new` bypass AOP. `@Transactional`, `@Cacheable`, `@Retryable` all silently do nothing on a `new`-ed instance.
- **`final` methods cannot be intercepted** (they can't be overridden in the generated subclass).
- **`private` methods cannot be intercepted** for the same reason.

```java
// WRONG — @Cacheable on a final method; compile-time warning, caching silently skipped
@Cacheable("products")
public final Product findProduct(Long id) { ... }

// RIGHT — remove final
@Cacheable("products")
public Product findProduct(Long id) { ... }

// WRONG — calling @Retryable from within the same class
public void upload(File f) {
    sendToS3(f);         // self-call; @Retryable not applied
}

@Retryable
public void sendToS3(File f) { ... }

// RIGHT — inject the bean and call through it, or restructure
@Inject S3Uploader uploader; // separate bean
public void upload(File f) { uploader.sendToS3(f); }
```

### 6. GraalVM native image with Micronaut: what Micronaut handles vs. what you must declare

Micronaut generates GraalVM metadata (reflect-config.json, resource-config.json, proxy-config.json) automatically for its own classes and for beans it discovers at compile time. The gaps:

- **Dynamic class loading or reflection outside Micronaut's DI** — third-party libraries, JDBC drivers, Jackson polymorphic subtypes — must still be registered.
- **Resources not on the classpath path** that are loaded with `getClass().getResourceAsStream()`.
- **Runtime-initialized classes** with side effects in static initializers (same trap as Quarkus — see runtime-map JVM notes).

```java
// Annotate third-party reflection targets (Micronaut 4 uses @ReflectiveAccess)
@ReflectiveAccess
public class ThirdPartyDto { ... }

// Or use GraalVM reflect-config.json directly for classes you can't annotate
// src/main/resources/META-INF/native-image/reflect-config.json
```

Run `native-image-agent` with your test suite before your first native build to auto-generate the bulk of the config:
```bash
java -agentlib:native-image-agent=config-output-dir=src/main/resources/META-INF/native-image \
     -jar app.jar
```

Then review and trim the generated config (it over-reports). Commit it alongside the source.

### 7. Micronaut Security: token validation and route-level authorization

Micronaut Security's `@Secured` or security rules in `SecurityRule` beans control access. The same IDOR trap applies as in Spring: role/authentication checks do not confirm object ownership.

```java
// WRONG — checks role, not object ownership
@Get("/invoices/{id}")
@Secured("ROLE_USER")
public Invoice get(Long id) {
    return invoiceRepo.findById(id).orElseThrow();
}

// RIGHT — pull the authenticated identity and verify ownership
@Get("/invoices/{id}")
@Secured("ROLE_USER")
public Invoice get(Long id, Authentication auth) {
    Invoice inv = invoiceRepo.findById(id).orElseThrow();
    if (!inv.getOwnerId().equals(auth.getName())) {
        throw new HttpStatusException(HttpStatus.FORBIDDEN, "Not your invoice");
    }
    return inv;
}
```

For JWT: Micronaut Security validates the signature and expiry automatically when `micronaut.security.token.jwt.signatures.secret.generator.secret` (or JWKS URL) is configured. Do not skip token validation by setting `micronaut.security.enabled=false` in non-dev profiles — AI sometimes does this to "fix" auth issues in staging.

## How this slots into the core pipeline

- **Gate 5 (Design):** confirm blocking/reactive model per controller method; identify `@ExecuteOn` boundaries; state transaction scope for multi-step data operations; review bean scopes for per-request state.
- **Gate 6 (Implementation):** `@ExecuteOn(TaskExecutors.IO)` on all blocking controllers; `@Transactional` at the service layer (not in the controller); no `final`/`private` on AOP-intercepted methods; `@ReflectiveAccess` for third-party reflection targets in native image.
- **Gate 7 (Review):** the reliability-reviewer additionally checks items 1–7 here for any Micronaut service; native image builds should be verified with `native-image-agent` output reviewed before release.

## Edit boundary (what belongs here vs. above/below)

Apply the 3-tier placement test before adding anything:

- True for Go/Node/Python too (idempotency, invariants, gates, observability)? → **generic core** (`mir-backend`).
- True for every JVM framework (thread-pool sizing, virtual-thread pinning, GC tuning, container heap, ThreadLocal hygiene, JMM visibility)? → **runtime tier** (`mir-backend-jvm`).
- A mechanical footgun of *this library* (compile-time DI binding, `@Singleton` request-state bleed, `@ExecuteOn` for Netty, Micronaut Data `@Transactional` self-invocation, compile-time AOP `final`/`private` limits, `@ReflectiveAccess` for native image, Micronaut Security IDOR)? → **here**.
- A *different* JVM framework (Spring Boot, Quarkus) → its own `mir-backend-jvm-<framework>` module. A *different* runtime → its own tier. Never widen this one.
