---
name: mir-backend-jvm-spring
description: "Make It Right (Spring Boot module). Spring Boot + Spring Data JPA/Hibernate + Spring Security + Spring MVC/WebFlux reliability footguns specific to this framework stack. Covers: @Transactional self-invocation (same-bean call bypasses the proxy → no transaction), checked exceptions not rolling back by default, propagation/isolation pitfalls, JPA/Hibernate N+1 with lazy associations, LazyInitializationException outside the session, OPEN_IN_VIEW antipattern, singleton bean scope storing per-request state, @Async needing an explicit thread pool and swallowing exceptions, @Valid + DTOs against overposting, and Spring Security method-level authorization for object-level checks. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-jvm (JVM runtime concerns: thread pools, virtual threads, GC, container heap, cold start, JMM visibility, ThreadLocal hygiene); this module only adds Spring Boot / Spring Data library mechanics. TRIGGER only when the JVM backend stack is Spring Boot — building, reviewing, or debugging a controller, service, @Transactional method, JPA entity, Spring Security config, or @Async task. SKIP for Quarkus, Micronaut, Vert.x, or any non-Spring JVM framework (those get their own mir-backend-jvm-<framework> module), and for non-JVM runtimes."
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

**Stack assumed:** Spring Boot 3.x · Spring Data JPA (Hibernate 6) · Spring MVC or WebFlux · Spring Security · PostgreSQL / MySQL. Notes call out WebFlux divergences explicitly.

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

// FIX A — inject self (Spring 4.3+ allows this, though it's a code smell)
@Autowired private OrderService self;
public void process(Order order) { self.settle(order); }

// FIX B — extract settle() to a separate @Service bean and inject that
@Autowired private SettlementService settlementService;
public void process(Order order) { settlementService.settle(order); }
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
| Read-only report query | `@Transactional(readOnly = true)` | Hibernate skips dirty-checking; DB can use read replica |
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

### 5. OPEN_IN_VIEW: the antipattern enabled by default

Spring Boot enables `spring.jpa.open-in-view=true` by default. This keeps the Hibernate session (and a DB connection from the pool) open for the entire HTTP request, including the time spent in view rendering. This lets lazy loads work in the controller/view layer — which **masks N+1** instead of fixing it and silently holds connections far longer than needed.

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

### 7. @Async: explicit thread pool and exception swallowing

`@Async` without configuration uses Spring's `SimpleAsyncTaskExecutor` — it **creates a new thread per invocation with no pool**, effectively unbounded. Under load this spawns thousands of threads, collapses under memory pressure, and OOM-kills the process.

```java
// WRONG — default SimpleAsyncTaskExecutor; unbounded thread creation
@Async
public void sendEmail(String to) { ... }

// RIGHT — configure a named executor
@Bean(name = "emailPool")
public Executor emailPool() {
    ThreadPoolTaskExecutor ex = new ThreadPoolTaskExecutor();
    ex.setCorePoolSize(4);
    ex.setMaxPoolSize(10);
    ex.setQueueCapacity(500);
    ex.setThreadNamePrefix("email-");
    ex.initialize();
    return ex;
}

@Async("emailPool")
public void sendEmail(String to) { ... }
```

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

Use `@Valid` on method parameters to trigger Bean Validation (JSR-380). Use `response_model`-equivalent `@JsonView` or a dedicated response DTO to suppress outbound fields (password hash, internal flags).

### 9. Spring Security: method-level authorization for object-level checks

`@PreAuthorize("isAuthenticated()")` or `httpSecurity.authorizeHttpRequests(...)` confirms the user is logged in and has the right role — it does **not** confirm the user owns the specific object. AI implements authentication and stops there → IDOR (Insecure Direct Object Reference).

```java
// WRONG — authenticated and ROLE_USER, but can read any order by ID
@GetMapping("/orders/{id}")
@PreAuthorize("hasRole('USER')")
public Order get(@PathVariable Long id) {
    return orderRepo.findById(id).orElseThrow();
}

// RIGHT — object-level ownership check
@GetMapping("/orders/{id}")
@PreAuthorize("hasRole('USER')")
public Order get(@PathVariable Long id, @AuthenticationPrincipal UserDetails principal) {
    Order order = orderRepo.findById(id).orElseThrow();
    if (!order.getOwnerId().equals(principal.getId())) {
        throw new ResponseStatusException(HttpStatus.FORBIDDEN);
    }
    return order;
}

// OR use @PostAuthorize / @PreAuthorize with SpEL bean method for the check
@PreAuthorize("@orderSecurity.isOwner(#id, authentication)")
```

## How this slots into the core pipeline

- **Gate 5 (Design):** when stating transaction boundaries, call out propagation (`REQUIRES_NEW` for audit logs, `readOnly = true` for reports), rollback scope (`rollbackFor`), and fetch strategy (no lazy in loops). Confirm `OPEN_IN_VIEW` is disabled.
- **Gate 6 (Implementation):** code against items 1–9 above. No singleton mutable fields; name your `@Async` executor; narrow DTOs with `@Valid`; ownership check after role check.
- **Gate 7 (Review):** the reliability-reviewer additionally checks items 1–9 here for any Spring Boot service.

## Edit boundary (what belongs here vs. above/below)

Apply the 3-tier placement test before adding anything:

- True for Go/Node/Python too (idempotency, invariants, gates, observability principles)? → **generic core** (`mir-backend`).
- True for every JVM framework (thread-pool deadlock, GC tuning, `-XX:MaxRAMPercentage`, virtual-thread pinning, ThreadLocal hygiene, JMM visibility)? → **runtime tier** (`mir-backend-jvm`).
- A mechanical footgun of *this library* (`@Transactional` proxy bypass, Hibernate N+1, OPEN_IN_VIEW, `SimpleAsyncTaskExecutor`, Spring Security IDOR)? → **here**.
- A *different* JVM framework (Quarkus, Micronaut) → its own `mir-backend-jvm-<framework>` module. A *different* runtime → its own tier. Never widen this one.
