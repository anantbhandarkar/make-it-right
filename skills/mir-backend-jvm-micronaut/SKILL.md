---
name: mir-backend-jvm-micronaut
description: "Make It Right (Micronaut module). Micronaut 5.x / 4.x + Micronaut Data + Micronaut Security + Netty footguns. Covers: compile-time DI and AOT (bean definitions are generated at build, but resolution is still runtime, so NoSuchBeanException/NonUniqueBeanException surfaces after startup because singletons are lazy), bean scope pitfalls (@Singleton default), blocking the Netty event loop (@ExecuteOn(TaskExecutors.BLOCKING), virtual-thread backed where supported, or reactive types), Micronaut Data repository transaction scoping and self-invocation, compile-time AOP interceptor limits on final/private/new-ed instances, and Micronaut security (CORS wide open when enabled with no configurations, allowCredentials defaulting to true on 4.x and false on 5.x, @Secured object-level authorization, and the HTTP-client credential-leakage and unbounded-redirect advisories). Chains: mir-backend -> mir-backend-jvm -> this, which adds only Micronaut library mechanics. TRIGGER only when the JVM backend stack is Micronaut — building, reviewing, or debugging a Micronaut controller, service, repository, filter, HTTP client, or security rule. SKIP for Spring Boot (mir-backend-jvm-spring), Quarkus (mir-backend-jvm-quarkus), standalone Vert.x, Helidon, Ktor, every other non-Micronaut JVM framework, and every non-JVM runtime."
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

**Stack assumed:** Micronaut 5.x (or 4.x — the differences are called out) · Micronaut Data JPA (Hibernate) or Micronaut Data JDBC · Micronaut Security · Micronaut HTTP Server (Netty) · GraalVM for native image.

**Version status, verified 13 Aug 2026:**

| Line | Status | What changes |
|---|---|---|
| **Micronaut 5.x** | GA 20 May 2026 (5.0.0). Latest verified release 5.1.0, 27 Jul 2026 | **Java 25 baseline** — virtual threads, structured concurrency, scoped values and pattern matching are all available unconditionally. Groovy 5 and Kotlin 2.3 baselines, GraalVM 25.0.3. Adds a `ScopedValue`-based context-propagation implementation alongside the thread-local one |
| **Micronaut 4.x** | Still maintained (4.10.x line) | Keeps Java 17 and 21 support and runs on 25. Stay here if you cannot move to a Java 25 baseline |

A Micronaut 5 answer written from Micronaut 4 memory will usually still compile; the risk is the opposite direction — 5-only APIs suggested for a 4.x project, and a Java 25 baseline assumed for a service still built on 17.

## The Micronaut footguns AI walks into most

### 1. Compile-time DI — missing bean is a compile error, not a runtime NPE

Micronaut generates bean *definitions* and injection metadata at compile time via annotation processors — no runtime classpath scanning, no reflection for injection. What that does **not** buy you:

- **Candidate resolution still happens at runtime.** A missing or ambiguous dependency raises `NoSuchBeanException`, `NonUniqueBeanException`, or `DependencyInjectionException` when the dependent bean is created — and because singletons are lazy, that can be well after startup rather than at build time. Do not tell a reviewer "if it compiled, the graph is wired." Force it with eager initialization or a context-startup test.
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

Micronaut beans are not implicitly singletons — a class needs a bean-defining annotation, and `@Bean`/`@Prototype` gives prototype scope. But almost every service you or an AI writes carries `@Singleton`, which is one lazily created instance shared by all threads. That is the same trap as Spring's singleton scope, and Micronaut's lack of a default `@RequestScope` proxy makes it easier to miss.

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

`@ThreadLocal` scope and thread-local context propagation both behave differently once every request has its own virtual thread — see `mir-backend-jvm` §2 and §7. Micronaut 5 offers a `ScopedValue`-based context-propagation implementation as an alternative to the thread-local one.

### 3. Blocking the Netty event loop: @ExecuteOn is mandatory for blocking work

Micronaut's HTTP server runs on Netty's event loop. Like Quarkus reactive, **blocking the event loop thread degrades the entire server** — one blocking call can stall all concurrent requests on that loop.

```java
// WRONG — controller method runs on the event loop by default; JDBC blocks it
@Get("/report")
public Report getReport() {
    return reportService.buildHeavyReport(); // blocking JDBC on event loop → server stall
}

// RIGHT — @ExecuteOn dispatches off the event loop
@Get("/report")
@ExecuteOn(TaskExecutors.BLOCKING)   // current name; virtual threads when available
public Report getReport() {
    return reportService.buildHeavyReport(); // event loop is free
}

// ALSO RIGHT — return a reactive type; Micronaut keeps the call non-blocking
@Get("/report")
public Mono<Report> getReport() {
    return reportService.buildHeavyReportAsync(); // reactive, event loop not blocked
}
```

**`TaskExecutors.BLOCKING` is the name to use.** Micronaut detects virtual-thread support and backs the blocking executor with virtual threads when the JVM provides them, falling back to the platform-thread I/O pool otherwise. On Micronaut 5 (Java 25 baseline) that means virtual threads always. `TaskExecutors.IO` still exists and still names the pooled executor — code that hard-codes it opts out of virtual threads without saying so.

Two consequences of the virtual-thread path:
- **The pool no longer bounds concurrency.** Size the datasource pool and any downstream limit explicitly; that is now the only ceiling. `micronaut.executors.io.type=fixed` with `nThreads` still applies to the platform-thread pool if you deliberately choose it.
- `@ExecuteOn` is required on **filters** that block too (`@ServerFilter` / `@RequestFilter` methods), not just controllers. A blocking filter stalls the event loop exactly like a blocking controller.

For **Kotlin coroutines**: mark the controller function `suspend`; Micronaut handles the dispatch. Never call `runBlocking` on the event loop.

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
// Annotate third-party reflection targets (@ReflectiveAccess in Micronaut 4 and 5)
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

For JWT: Micronaut Security validates the signature and expiry when a signature configuration is present — `micronaut.security.token.jwt.signatures.secret.*` for a shared secret, or `micronaut.security.token.jwt.signatures.jwks.*` for a JWKS URL. Configure one. Do not "fix" an auth problem in staging by setting `micronaut.security.enabled=false`; that disables every rule in the application, and the setting outlives the debugging session.

## Security

Micronaut-specific mechanics. JDK-level items (deserialization filters, XXE, SSRF address checks, heap dumps, Maven/Gradle supply chain) are in `mir-backend-jvm`.

### Current advisories on the default HTTP client path

Both were published 9 Jul 2026 against `io.micronaut:micronaut-http-client` and affect `DefaultHttpClient`'s redirect handling — the client every declarative `@Client` interface uses. Neither carries a CVE id, so a scanner keyed on CVE ids alone will miss them.

| Advisory | What it does | Fixed in |
|---|---|---|
| **GHSA-q6gh-6v2r-hjv3** (CVSS 6.8) | `DefaultHttpClient` follows redirects and forwards `Authorization`, `Cookie`, and `Proxy-Authorization` headers **across domain boundaries** — the header blocklist only stripped `Host`/`Connection`/`TE`/`Content-Type`/`Content-Length`. A redirect to an attacker's host receives your credentials | Micronaut 5 ≥ 5.0.1, Micronaut 4 ≥ 4.10.24, Micronaut 3 ≥ 3.10.6 |
| **GHSA-387m-935m-c4vw** (CVSS 7.5) | No maximum redirect count — a redirect loop drives CPU, thread, and memory exhaustion | Micronaut 5 ≥ 5.0.1, Micronaut 4 ≥ 4.10.24, Micronaut 3 ≥ 3.10.7 |

The 5.0.1 release announcement told Micronaut 4 users to move to 4.10.15, while the GHSA records list 4.10.24 as the patched 4.x floor. **Take the higher number** — scanners use the GHSA data. Both matter most when a URL, or a host that can issue a redirect, is influenced by user input: that combination is the SSRF and credential-leak path described in `mir-backend-jvm`.

### CORS: enabling it without configuring it is wide open

| Key | Default | Effect |
|---|---|---|
| `micronaut.server.cors.enabled` | `false` | CORS requests are rejected — the safe starting point |
| `micronaut.server.cors.configurations.*.allowed-origins` | unset | **Omitting it allows any origin.** Micronaut's own guide calls a bare `enabled: true` a "wide open" configuration |
| `micronaut.server.cors.configurations.*.allow-credentials` | **`true` on 4.x, `false` on 5.x** | The default flipped in Micronaut 5 (`CorsOriginConfiguration`, micronaut-core #12614). On 4.x a bare `enabled: true` means any origin *with credentials* |

On **Micronaut 4.x** those two defaults combine badly: `micronaut.server.cors.enabled=true` alone means any origin, with credentials. **Micronaut 5.x** flipped `allow-credentials` to `false`, so the same config is any origin *without* credentials — still wrong, just less catastrophic. Do not carry a 4.x mental model onto 5.x or the reverse: declare a named configuration with an explicit `allowed-origins` list and an explicit `allow-credentials` decision on both. Regex origins are matched with `Pattern.matches` — a pattern that admits any subdomain admits any attacker-controlled subdomain.

### Authorization: deny by default, then be careful how you open it

Micronaut Security's guide states that adding the security module locks down every endpoint, including static resources, until a rule allows access — a better default than most frameworks. The ways people break it:

- `@Secured(SecurityRule.IS_ANONYMOUS)` **at the class level** opens every method on that controller, including ones added later.
- `intercept-url-map` patterns with broad wildcards (`/api/**` → `isAnonymous()`) open more than intended, and rule resolution has method-matching subtleties: a mapping that specifies an HTTP method wins over one that does not, and among method-less mappings the first match wins.
- `micronaut.security.reject-not-found` defaults to `true`, so a request to a non-existent path returns an authorization failure rather than a 404 — that is deliberate, it stops route enumeration. Do not set it to `false` to "clean up" error codes.
- Management endpoints: never set `endpoints.all.sensitive=false`. Sensitive endpoints (`env`, `beans`, `loggers`, `threaddump`) print configuration and internals.

### Object-level authorization (IDOR / BOLA)

Item 7 above is the mechanism. A role check and a valid JWT answer *who* and *what kind of user*, never *which row*. Put the owner or tenant predicate in the repository method (`invoiceRepo.findByIdAndOwnerId(id, auth.getName())`) so an unowned row is never loaded, rather than loading first and comparing afterwards.

### Mass assignment

Micronaut Serialization is stricter than classic Jackson databind — a type must be annotated `@Serdeable` (or `@Introspected` with serde support) to be deserializable at all, so random classpath types are not reachable. That protection disappears the moment you annotate the entity itself and bind a request body onto it: every property, including `id`, `role`, and `tenantId`, is then bindable.

```java
// WRONG — the persisted entity is the request type
@Post("/users") User create(@Body User user) { return repo.save(user); }

// RIGHT — a request record with only the fields a client may set
@Serdeable record CreateUserRequest(@NotBlank String name, @Email String email) {}

@Post("/users")
UserResponse create(@Body @Valid CreateUserRequest req) { ... }
```

Use a separate response type as well. `@JsonProperty(access = READ_ONLY)` on an entity is a deny-list: the next field someone adds is exposed by default.

### SQL injection in Micronaut Data

Compile-time-generated finder methods (`findByNameAndStatus`) are parameterized and safe. The injection paths are the ones you write by hand:

```java
// WRONG — query text assembled at runtime from request data. It cannot appear
// inside @Query: annotation values must be compile-time constants, so in real
// code this always shows up as a hand-built string.
entityManager.createQuery("SELECT o FROM Order o WHERE o.status = '" + status + "'")
             .getResultList();

// RIGHT — named binding
@Query("SELECT o FROM Order o WHERE o.status = :status")
List<Order> byStatus(String status);
```

Same for `@Query(nativeQuery = true)`, for any `Criteria`/`PredicateSpecification` built from raw strings, and for `Sort`: sort property names are emitted as identifiers, so a client-supplied sort field must be checked against an allow-list before it reaches `Sort.of(...)`.

### File serving and uploads

`micronaut.router.static-resources.*.paths` pointed at a `file:` location serves whatever resolves under it. Never build such a path from request input. For uploads, `CompletedFileUpload.getFilename()` is client-controlled — generate your own storage name, keep the original as metadata, and apply the `normalize()` + `startsWith(baseDir)` containment check from the JVM tier. Set an explicit `micronaut.server.multipart.max-file-size` and `micronaut.server.max-request-size`.

### Secrets, errors, and logs

- Keep signing secrets and datasource passwords out of `application.yml`; supply them through environment variables or a secrets client. A committed `micronaut.security.token.jwt.signatures.secret.*` value is a permanent forgery key.
- Micronaut's default error responses can carry exception messages. Register an `@Error` handler that returns a generic body plus a correlation id, and keep the detail in the log.
- Java `record` and Lombok `toString()` print every field — see the JVM tier before logging a domain object.

### Supply chain

Import the Micronaut Platform BOM and let it resolve module versions across the 70+ modules instead of pinning each one; a hand-pinned `micronaut-http-client` version is exactly how a service stays on a pre-5.0.1 client after the platform moved. Everything in the JVM tier's supply-chain table (checksum verification, banning dynamic versions, dependency confusion, build scripts executing code) applies unchanged.

## How this slots into the core pipeline

- **Gate 5 (Design):** confirm blocking/reactive model per controller method; identify `@ExecuteOn` boundaries and what actually bounds concurrency once the blocking executor is virtual-thread backed; state transaction scope for multi-step data operations; review bean scopes for per-request state; state the Micronaut line (4.x vs 5.x) and the Java baseline that implies.
- **Gate 6 (Implementation):** `@ExecuteOn(TaskExecutors.BLOCKING)` on all blocking controllers *and* blocking filters; `@Transactional` at the service layer (not in the controller); no `final`/`private` on AOP-intercepted methods; `@ReflectiveAccess` for third-party reflection targets in native image; request DTOs rather than entities on `@Body`; explicit CORS origins and `allow-credentials`.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–7; the security-reviewer checks the Security section — CORS defaults, class-level `IS_ANONYMOUS`, ownership predicates, query binding, upload paths, and the `micronaut-http-client` version against the two July 2026 advisories. Native image builds should be verified with `native-image-agent` output reviewed before release.

## Edit boundary (what belongs here vs. above/below)

Apply the 3-tier placement test before adding anything:

- True for Go/Node/Python too (idempotency, invariants, gates, observability)? → **generic core** (`mir-backend`).
- True for every JVM framework (thread-pool sizing, virtual-thread pinning and JEP 491, GC tuning, container heap, ThreadLocal hygiene, JMM visibility, `ObjectInputFilter`, XXE defaults, Maven/Gradle verification)? → **runtime tier** (`mir-backend-jvm`).
- A mechanical footgun of *this library* (compile-time DI binding, `@Singleton` request-state bleed, `@ExecuteOn(TaskExecutors.BLOCKING)` for Netty, Micronaut Data `@Transactional` self-invocation and query binding, compile-time AOP `final`/`private` limits, `@ReflectiveAccess` for native image, `micronaut.server.cors.*` defaults, Micronaut Security IDOR, the HTTP-client redirect advisories)? → **here**.
- A *different* JVM framework (Spring Boot, Quarkus) → its own `mir-backend-jvm-<framework>` module. A *different* runtime → its own tier. Never widen this one.
