---
name: mir-backend-jvm-spring
description: "Make It Right (Spring Boot module). Spring Boot 4.x / Spring Framework 7 + Spring Data JPA/Hibernate + Spring Security 7 + Spring MVC/WebFlux reliability footguns specific to this framework stack. Covers: @Transactional self-invocation (same-bean call bypasses the proxy → no transaction), checked exceptions not rolling back by default, propagation/isolation pitfalls, JPA/Hibernate N+1 with lazy associations, LazyInitializationException outside the session, the open-in-view default, singleton bean scope storing per-request state, @Async against Boot's auto-configured applicationTaskExecutor (unbounded queue, exception swallowing, what spring.threads.virtual.enabled changes), Jackson 3 and the Spring Boot 3.x→4 migration cliff (3.x is out of OSS support), @Valid + DTOs against overposting, and Spring Security object-level authorization plus the current authorization-bypass advisories (CVE-2026-22748 NimbusJwtDecoder issuer validation, CVE-2025-41248/41249 method-security annotations on parameterized types, CVE-2026-22731 Actuator health-group paths, CVE-2026-41843 versioned static-resource path traversal). Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-jvm (JVM runtime concerns: thread pools, virtual threads and JEP 491, GC, container heap, cold start, JMM visibility, ThreadLocal hygiene, JDK-level security defaults); this module only adds Spring Boot / Spring Data / Spring Security library mechanics. TRIGGER only when the JVM backend stack is Spring Boot — building, reviewing, or debugging a controller, service, @Transactional method, JPA entity, Spring Security config, Actuator exposure, or @Async task. SKIP for Quarkus, Micronaut, Vert.x, Helidon, Ktor, or any non-Spring JVM framework (those get their own mir-backend-jvm-<framework> module), and for non-JVM runtimes."
trigger: /mir-backend-jvm-spring
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-jvm-spring · Make It Right (Spring Boot)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-jvm` (JVM runtime model) → **this** (Spring Boot / Spring Data library mechanics). Run the gates first; load the JVM runtime tier for threading, GC, and container-heap concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (virtual-thread pinning, pool sizing, GC tuning, `-XX:MaxRAMPercentage`, ThreadLocal hygiene) live in `mir-backend-jvm` — not here.**

**Stack assumed:** Spring Boot 4.x · Spring Framework 7 · Spring Security 7 · Spring Data JPA (Hibernate 7.x, the version Boot 4 manages) · Spring MVC or WebFlux · PostgreSQL / MySQL. Notes call out WebFlux divergences explicitly.

**Version and support status, verified 13 Aug 2026:**

| Line | Status | Consequence |
|---|---|---|
| **Spring Boot 4.1** | Current. Released 30 Jun 2026; OSS support to 31 Jul 2027 | Target for new work |
| **Spring Boot 4.0** | Released 30 Nov 2025; OSS support ends 31 Dec 2026 | Plan the 4.1 bump now |
| **Spring Boot 3.5** | **OSS support ended 30 Jun 2026** (last OSS release 3.5.16). Commercial support to Jun 2032 | No free patches — new Spring CVEs are fixed in 4.x and, for paying customers, in 3.5.x. An unpatched 3.5 app is an unpatched app |
| Spring Boot 3.4 and older | Out of OSS support | Upgrade path is 3.5 → clear every deprecation → 4.x. Do not jump from 3.2/3.3 straight to 4 |

**What Boot 4 / Framework 7 changed that breaks code AI writes from memory:**

- **Java baseline stays 17**; Java 25 is the tested first-class target (AOT and native-image builds are validated on 25).
- **Jakarta EE 11** — Servlet 6.1, JPA 3.2, Bean Validation 3.1. Needs Tomcat 11+ or Jetty 12.1+. **`spring-boot-starter-undertow` was removed.**
- **Jackson 3 is the default.** Group id moves from `com.fasterxml.jackson` to `tools.jackson`; `Jackson2ObjectMapperBuilderCustomizer` becomes `JsonMapperBuilderCustomizer`; Jackson 2 properties move under `spring.jackson2`. Any answer that reaches for `com.fasterxml.jackson.databind.ObjectMapper` config on Boot 4 is targeting the wrong library.
- **Kotlin 2.2 baseline; the codebase is split into 70+ focused jars.** Starters still pull the right set, but a hand-written dependency list from a Boot 3 project will miss modules.

## The Spring Boot footguns AI walks into most

### 1. @Transactional self-invocation: the proxy bypass

Spring's `@Transactional` is implemented via a proxy (AOP). When a bean method calls *another method on the same bean instance* (`this.someMethod()`), the call goes **directly to the target object, not through the proxy** — the transaction advice is never applied.

```java
@Service
public class OrderService {

    // WRONG — calling settle() from within the same class bypasses the proxy;
    // settle() does NOT run in a transaction even though it is annotated.
    public void process(Order order) {
        settle(order);              // direct call on 'this' — proxy not involved
    }

    @Transactional
    public void settle(Order order) { ... }
}

// FIX — extract settle() to a separate @Service bean and inject that
@Autowired private SettlementService settlementService;
public void process(Order order) { settlementService.settle(order); }

// NOT A FIX — self-injection. Spring Boot sets spring.main.allow-circular-references=false
// by default (since 2.6), so this fails the context at startup rather than working.
// @Autowired private OrderService self;
```

### 2. Checked exceptions do NOT roll back @Transactional by default

Spring rolls back only on unchecked exceptions (`RuntimeException` and its subclasses) by default. A checked exception thrown inside a `@Transactional` method **commits the transaction** unless you say otherwise.

```java
// WRONG — IOException is checked; Spring commits even on failure
@Transactional
public void importFile(MultipartFile f) throws IOException {
    parse(f);           // may throw IOException
    repo.saveAll(rows); // saved to DB, then IOException rolls back nothing
}

// RIGHT — declare rollbackFor
@Transactional(rollbackFor = Exception.class)
public void importFile(MultipartFile f) throws IOException { ... }

// OR convert to unchecked in the domain layer
throw new FileProcessingException("...", cause); // extends RuntimeException
```

### 3. Propagation and isolation: pick deliberately, not by default

`@Transactional` defaults are `PROPAGATION_REQUIRED` (join existing tx or create one) and `ISOLATION_DEFAULT` (whatever the DB default is, usually READ COMMITTED). AI accepts both defaults everywhere — that is wrong for several common patterns:

| Scenario | Correct setting | Why the default is wrong |
|---|---|---|
| Audit log that must persist even if the outer tx rolls back | `PROPAGATION_REQUIRES_NEW` | `REQUIRED` rolls the audit row back with the outer tx |
| Read-only report query | `@Transactional(readOnly = true)` | Hibernate reduces flush and dirty-check work. It is a hint — it does **not** route to a read replica; that needs an explicit routing `DataSource` or a proxy |
| Preventing lost-update on concurrent state machine | `ISOLATION_REPEATABLE_READ` or row-lock | READ COMMITTED allows a concurrent read of the row before the update commits |
| Independent retry unit inside a larger operation | `PROPAGATION_REQUIRES_NEW` | `REQUIRED` merges into the outer tx; a rollback undoes everything |

### 4. JPA/Hibernate N+1: lazy associations in a loop

Hibernate defaults to lazy loading for `@OneToMany` and `@ManyToMany`. A loop that accesses a lazy collection issues one SELECT per entity — N+1 selects.

```java
// WRONG — triggers N+1: one query for orders, then one per order.items
List<Order> orders = orderRepo.findAll();
for (Order o : orders) {
    o.getItems().size(); // LazyInitializationException if session is closed here
}

// RIGHT — fetch join / @EntityGraph
@EntityGraph(attributePaths = {"items"})
List<Order> findAll(); // single query with LEFT JOIN FETCH

// OR in JPQL
@Query("SELECT o FROM Order o LEFT JOIN FETCH o.items")
List<Order> findAllWithItems();
```

**`LazyInitializationException`** occurs when a lazy field is accessed after the Hibernate session has closed (outside the `@Transactional` boundary). If you serialize an entity in a Spring MVC controller that sits outside the service's transaction, Hibernate can't issue the lazy SELECT. Fix: load everything you need inside the service/transaction, or use a DTO projection.

### 5. open-in-view: still enabled by default in Boot 4

`spring.jpa.open-in-view` **still defaults to `true` in Spring Boot 4** — the proposal to flip it (spring-boot issue #47547) is open, not merged. Boot logs a startup warning telling you to configure it explicitly; that warning is the only signal you get. The setting keeps the Hibernate session and a pooled DB connection open for the entire HTTP request including view rendering. Lazy loads then work in the controller layer — which **masks N+1** instead of fixing it and holds connections far longer than needed.

This gets worse with virtual threads: `spring.threads.virtual.enabled=true` removes the thread-count ceiling that used to limit how many requests could hold a connection at once, so open-in-view turns straight into connection-pool exhaustion under load.

```yaml
# Add to application.yml explicitly and fix lazy loads at the service layer
spring:
  jpa:
    open-in-view: false
```

After disabling, `LazyInitializationException` surfaces in controllers that relied on it — good. Fix each case with eager fetch or DTO.

### 6. Singleton bean scope: never store per-request state

All Spring beans are `@Scope("singleton")` by default — one instance shared across all threads. Storing mutable state in a singleton service field is a **data race** with request bleed.

```java
// WRONG — currentUser is a shared mutable field; all threads stomp on it
@Service
public class InvoiceService {
    private User currentUser; // NOT thread-safe

    public void generate(User u) {
        this.currentUser = u; // set by thread A, read by thread B
    }
}

// RIGHT — pass state as method arguments; use SecurityContextHolder for auth context
public void generate(User u) {
    // use u directly; never store in a field
}
```

Use `@RequestScope` beans sparingly (they require a proxy wrapper to inject into singletons) and only when you have a genuine per-request lifecycle need.

### 7. @Async: which executor you actually get, and its unbounded queue

The common claim that "`@Async` with no configuration uses `SimpleAsyncTaskExecutor` and spawns an unbounded number of threads" is **true for plain Spring Framework and wrong for Spring Boot**. Boot auto-configures an `applicationTaskExecutor` bean (a `ThreadPoolTaskExecutor`, 8 core threads by default) and `@EnableAsync` picks it up. The real Boot failure modes are different:

1. **The queue absorbs everything, so `max-size` is never reached.** A `ThreadPoolTaskExecutor` only creates threads beyond core size once the queue is full. Boot's defaults are `core-size=8`, `max-size=Integer.MAX_VALUE`, `queue-capacity=Integer.MAX_VALUE` — an unbounded queue in front of 8 threads. The pool never grows, work piles up in heap, latency climbs, nothing scales out, and a restart drops every queued task.
2. **One shared executor for everything.** `applicationTaskExecutor` also backs Spring MVC async request handling, WebSocket, and other framework work. A slow `@Async` email job starves unrelated framework tasks.
3. **Virtual threads silently change the semantics.** With `spring.threads.virtual.enabled=true`, the auto-configured executor becomes a `SimpleAsyncTaskExecutor` on virtual threads and the scheduler becomes `SimpleAsyncTaskScheduler` — **all pooling properties are ignored**. Every `spring.task.execution.pool.*` value you set is silently dropped, and there is no backpressure at all.

```yaml
# Bound the shared executor explicitly — never rely on the defaults
spring:
  task:
    execution:
      pool:
        core-size: 8
        max-size: 16
        queue-capacity: 100   # bounded: forces threads to spawn, then rejects
```

```java
// RIGHT — a dedicated, named, bounded executor for each workload
@Bean(name = "emailPool")
public Executor emailPool() {
    ThreadPoolTaskExecutor ex = new ThreadPoolTaskExecutor();
    ex.setCorePoolSize(4);
    ex.setMaxPoolSize(10);
    ex.setQueueCapacity(500);           // bounded
    ex.setThreadNamePrefix("email-");
    ex.setRejectedExecutionHandler(new ThreadPoolExecutor.AbortPolicy()); // fail loudly
    ex.initialize();
    return ex;
}

@Async("emailPool")
public void sendEmail(String to) { ... }
```

`@Async` is still not a queue. Anything that must survive a pod restart belongs in a real broker or an outbox table, not in an in-memory executor queue.

**Exception swallowing:** exceptions thrown inside `@Async` methods are **not propagated to the caller**. They are silently swallowed unless you implement `AsyncUncaughtExceptionHandler` or return a `Future` / `CompletableFuture` and handle it.

```java
@Configuration
public class AsyncConfig implements AsyncConfigurer {
    @Override
    public AsyncUncaughtExceptionHandler getAsyncUncaughtExceptionHandler() {
        return (ex, method, params) ->
            log.error("Async exception in {}: {}", method.getName(), ex.getMessage(), ex);
    }
}
```

### 8. Input validation and overposting: @Valid + narrow DTOs

Never bind the request body directly to a JPA entity — the client can supply `id`, `role`, `isAdmin`, `tenantId`, or any other field Hibernate will happily write.

```java
// WRONG — client sends {"id":1,"role":"ADMIN","name":"Alice"} and overwrites role
@PostMapping("/users")
public User create(@RequestBody User user) { return repo.save(user); }

// RIGHT — dedicated input DTO that excludes privileged fields
public record CreateUserRequest(
    @NotBlank String name,
    @Email String email
) {}

@PostMapping("/users")
public UserResponse create(@Valid @RequestBody CreateUserRequest req) {
    User user = mapper.toEntity(req); // only safe fields mapped
    return mapper.toResponse(repo.save(user));
}
```

Use `@Valid` on method parameters to trigger Bean Validation (Jakarta Bean Validation 3.1 on Boot 4). For the outbound direction use a dedicated response DTO — it is the only mechanism that fails safe. `@JsonView` and `@JsonIgnore` are opt-out lists: a new entity field is exposed by default the day someone adds it. Their package did **not** move — Jackson 3 keeps consuming the separately versioned Jackson 2 annotations artifact, so these stay in `com.fasterxml.jackson.annotation`. Only `databind`/`core` types moved to `tools.jackson`; rewriting annotation imports during a Boot 4 migration breaks a build that was fine.

For form/query binding (`@ModelAttribute`, not `@RequestBody`) the allow-list mechanism is `DataBinder`: set `binder.setAllowedFields(...)` in an `@InitBinder` method. A `disallowedFields` deny-list has the same failure mode as `@JsonIgnore` — it protects only the fields you thought of.

### 9. Spring Security: method-level authorization for object-level checks

`@PreAuthorize("isAuthenticated()")` or `httpSecurity.authorizeHttpRequests(...)` confirms the user is logged in and has the right role — it does **not** confirm the user owns the specific object. AI implements authentication and stops there → IDOR (Insecure Direct Object Reference).

```java
// WRONG — authenticated and ROLE_USER, but can read any order by ID
@GetMapping("/orders/{id}")
@PreAuthorize("hasRole('USER')")
public Order get(@PathVariable Long id) {
    return orderRepo.findById(id).orElseThrow();
}

// RIGHT — ownership predicate in the query. Note UserDetails has NO getId():
// its identity accessor is getUsername(). Code that calls principal.getId() does
// not compile unless you supply your own UserDetails implementation.
@GetMapping("/orders/{id}")
@PreAuthorize("hasRole('USER')")
public Order get(@PathVariable Long id, @AuthenticationPrincipal UserDetails principal) {
    return orderRepo.findByIdAndOwnerUsername(id, principal.getUsername())
        .orElseThrow(() -> new ResponseStatusException(HttpStatus.NOT_FOUND));
}

// OR use @PostAuthorize / @PreAuthorize with SpEL bean method for the check
@PreAuthorize("@orderSecurity.isOwner(#id, authentication)")
```

Push the check into the query so an unowned row is never loaded, as above. A `404` for someone else's row also stops the endpoint confirming that the ID exists. If you need a real user id rather than the username, publish your own `UserDetails` implementation (or a custom `AuthenticationPrincipal` resolver) and type the parameter to it — do not assume the interface exposes one.

## Security

Spring-specific mechanics. JDK-level items (deserialization filters, XXE, SSRF address checks, heap dumps, Maven/Gradle supply chain) are in `mir-backend-jvm`.

### Current advisories on the default path — check your versions

| Advisory | What it breaks | Affected | Fixed |
|---|---|---|---|
| **CVE-2026-22748** | `NimbusJwtDecoder.withIssuerLocation(...)` / `NimbusReactiveJwtDecoder` reads the issuer metadata but does **not** validate the `iss` claim unless you set a validator. A token from a different issuer is accepted → authentication bypass | Spring Security 7.0.0–7.0.4, 6.5.0–6.5.9, 6.4.0–6.4.14, ≤6.3.14 | 7.0.5, 6.5.10 (OSS); 6.4.15, 6.3.15 are enterprise-only |
| **CVE-2025-41248 / CVE-2025-41249** | Method-security annotations declared on a generic superclass or interface are not resolved through an unbounded parameterized type → `@PreAuthorize` silently not applied → authorization bypass. Only affects apps using `@EnableMethodSecurity` | Spring Security 6.4.0–6.4.9, 6.5.0–6.5.3; Spring Framework 5.3.x/6.1.x/6.2.x annotation resolution | Patched 6.4.x/6.5.x releases; interim workaround is to redeclare the annotation on the concrete class |
| **CVE-2026-22731** | An authenticated endpoint mapped under an Actuator health-group `additional-path` (e.g. `management.endpoint.health.group.x.additional-path=server:/healthz` with an app endpoint at `/healthz/admin`) is reachable without authentication | Spring Boot 4.0.0–4.0.3, 3.5.0–3.5.11, 3.4.0–3.4.13 | 4.0.4, 3.5.12 (OSS); no OSS fix for 3.4.x. Also: stop mapping application endpoints under Actuator paths |
| **CVE-2026-41843** | Path traversal when Spring MVC/WebFlux serves **versioned** static resources from the filesystem | Spring Framework 7.0.0–7.0.7, 6.2.0–6.2.18, 6.1.0–6.1.21, ≤5.3.39 | 7.0.8, 6.2.19 (OSS); no OSS fix for 6.1.x or 5.3.x |

New Spring advisories land in batches (there were further spring-security, spring-cloud-gateway, and spring-ai advisories in June 2026). Treat `spring.io/security` as a release-gate check, not a one-time read — and remember that on Boot 3.5 the fix will not be published to Maven Central.

### Object-level authorization (IDOR / BOLA)

Item 9 above is the mechanism. The rule: a valid token proves *who*, never *which row*. `@PreAuthorize("hasRole('USER')")`, `authorizeHttpRequests()`, and a passing JWT signature check all answer authentication and role questions and none of them answer ownership. Every handler that accepts an identifier from the client needs an ownership or tenancy predicate — preferably in the repository method.

### SpEL and template injection — the Spring-specific RCE paths

The danger is **expression text built from request data**, not expressions in general.

```java
// WRONG — the expression string itself comes from the request. This is RCE.
new SpelExpressionParser().parseExpression(userSupplied).getValue();
String expr = "hasRole('" + roleFromRequest + "')";   // then evaluated

// FINE — the expression is static; #id and the bean call bind values, they do not parse them
@PreAuthorize("@orderSecurity.isOwner(#id, authentication)")
```

The same rule covers `@Value` strings assembled at runtime, SpEL in filter/rule configuration read from a database row, and any user-controlled property path that a framework evaluates as an expression (the Spring Data Commons class of bug).

**Template injection:** in a Thymeleaf application, returning a client-controlled string as a view name — `return "redirect_" + userInput;` or a fragment expression built from a parameter — makes the template engine evaluate attacker text as an expression. View names must come from a fixed set in your code, never from the request.

### SQL injection in Spring Data

`@Query` with `:param` binding is safe; string concatenation is not, and `nativeQuery = true` removes JPQL's remaining protection.

```java
// WRONG — SQL text assembled at runtime from request data.
// (Note the injection cannot live in the @Query annotation itself: annotation
// values must be compile-time constants, so this always appears in real code
// as a runtime-built string handed to EntityManager or JdbcTemplate.)
String sql = "SELECT * FROM orders WHERE status = '" + status + "'";
entityManager.createNativeQuery(sql).getResultList();

// RIGHT
@Query(value = "SELECT * FROM orders WHERE status = :status", nativeQuery = true)
List<Order> byStatus(@Param("status") String status);
```

Two Spring-specific extras: **`Sort` and `Pageable` property names are injected into the generated SQL/JPQL as identifiers**, so passing a raw client string into `Sort.by(clientField)` lets a caller reference arbitrary properties — validate against an allow-list of sortable fields. And `EntityManager.createQuery(String)` built by concatenation has the same problem as any other dynamic SQL; use the Criteria API or parameter binding.

### CSRF, CORS, and cookies

- **CSRF is needed exactly when the browser attaches credentials automatically** — session cookies, `Basic`, or any cookie-based auth. It is not needed for a stateless `Authorization: Bearer` API. `http.csrf(csrf -> csrf.disable())` copied into a cookie-authenticated app is a real vulnerability, and it is the single most copy-pasted line in Spring Security.
- Spring Security's default token handler is `XorCsrfTokenRequestAttributeHandler` (BREACH protection; `XorServerCsrfTokenRequestAttributeHandler` on WebFlux). For a cookie-reading SPA, use the built-in `http.csrf(csrf -> csrf.spa())` rather than hand-rolling a `CsrfTokenRequestHandler` — hand-rolled versions usually re-expose the encoded token.
- Set `server.servlet.session.cookie.same-site=lax` (or `strict`) and `secure=true` explicitly. `SameSite` is defence in depth, not a CSRF replacement.
- **CORS:** Spring rejects `allowCredentials(true)` combined with `allowedOrigins("*")`, so the trap is the workaround: `allowedOriginPatterns("*")` with credentials **reflects whatever `Origin` the caller sends** and is equivalent to trusting every site. Enumerate origins. `@CrossOrigin` on a controller silently overrides your global `CorsConfigurationSource` for that handler — grep for it.

### Actuator and error responses

- `management.endpoints.web.exposure.include` defaults to `health` only. The failure is the copy-pasted `=*`, which publishes `/actuator/env`, `/actuator/configprops`, `/actuator/heapdump`, and `/actuator/threaddump`. `env` and `configprops` print your configuration (Boot masks known-sensitive keys, not custom ones), and `heapdump` hands over every secret in memory. Expose only what you scrape, and put Actuator on a separate port (`management.server.port`) that the ingress does not route.
- Keep `spring.web.error.include-stacktrace` and `include-message` off in production (`server.error.*` on Boot 3.x). Return a correlation ID, log the detail.

### Path traversal and file serving

`ResourceHttpRequestHandler`/`addResourceHandlers` pointed at `file:` locations is the code path behind CVE-2026-41843. Serve user files through an explicit handler that resolves against a base directory and re-checks containment (`Path.normalize()` + `startsWith`, see the JVM tier), and never build a `Resource` location from request input.

### Deserialization

- Do not enable Jackson polymorphic typing on untrusted input (`activateDefaultTyping`, `@JsonTypeInfo(use = Id.CLASS)`). It turns any JSON body into a class-instantiation primitive.
- Spring Session with the JDK serializer, `spring-cloud-function` routing, and message converters that accept Java serialization from a broker all inherit the JVM's deserialization problem — see the JVM tier's `ObjectInputFilter` guidance and prefer JSON serializers with fixed types.

### If the service calls an LLM (Spring AI)

Spring AI 1.1.x is the stable line for Boot 3.x; 2.0 is the line that targets Boot 4 (2.0 milestones at last check — verify GA status before pinning). Whichever version: a `@Tool` method is a normal entry point that an attacker can reach through text in a document, an email, or a database row. Apply the same authorization inside the tool method that you would on a controller (`@PreAuthorize` plus an ownership check), validate the arguments the model supplies rather than trusting them, and keep system instructions in a `SystemMessage` instead of concatenating them with user text.

## How this slots into the core pipeline

- **Gate 5 (Design):** when stating transaction boundaries, call out propagation (`REQUIRES_NEW` for audit logs, `readOnly = true` for reports), rollback scope (`rollbackFor`), and fetch strategy (no lazy in loops). Confirm `spring.jpa.open-in-view` is set explicitly to `false`. State the Spring Boot line and whether it is still in OSS support.
- **Gate 6 (Implementation):** code against items 1–9 above. No singleton mutable fields; a named, bounded `@Async` executor; narrow DTOs with `@Valid` and a response DTO; ownership predicate in the repository query.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–9; the security-reviewer checks the Security section — CSRF vs auth scheme, CORS patterns, Actuator exposure, SpEL and `Sort` injection, and the four advisories above against the versions in the build file.

## Edit boundary (what belongs here vs. above/below)

Apply the 3-tier placement test before adding anything:

- True for Go/Node/Python too (idempotency, invariants, gates, observability principles)? → **generic core** (`mir-backend`).
- True for every JVM framework (thread-pool deadlock, GC tuning, `-XX:MaxRAMPercentage`, virtual-thread pinning and JEP 491, ThreadLocal hygiene, JMM visibility, `ObjectInputFilter`, XXE defaults, Maven/Gradle verification)? → **runtime tier** (`mir-backend-jvm`).
- A mechanical footgun of *this library* (`@Transactional` proxy bypass, Hibernate N+1, `open-in-view`, `applicationTaskExecutor` queueing, SpEL injection, Spring Security IDOR and its CVEs, Actuator exposure)? → **here**.
- A *different* JVM framework (Quarkus, Micronaut) → its own `mir-backend-jvm-<framework>` module. A *different* runtime → its own tier. Never widen this one.
