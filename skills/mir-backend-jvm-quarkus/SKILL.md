---
name: mir-backend-jvm-quarkus
description: "Make It Right (Quarkus module). Quarkus 3.x (LTS 3.33) + Hibernate ORM/Panache + Quarkus REST (formerly RESTEasy Reactive) + Mutiny footguns. Covers: build-time DI (reflection must be registered with @RegisterForReflection or it fails only at native runtime), the Quarkus REST execution model (the return type picks the thread — Uni/Multi/CompletionStage run on the Vert.x event loop, everything else on a worker thread), blocking inside Mutiny pipelines, @RunOnVirtualThread, build-time vs runtime config keys and secrets baked into a native binary, native-image gotchas, and Quarkus security (deny-unannotated-endpoints defaulting to false, CORS config, the quarkus-rest-csrf extension, Panache active-record mass assignment, and the path-normalization authorization-bypass advisories against quarkus.http.auth.permission policies). Chains: mir-backend -> mir-backend-jvm -> this, which adds only Quarkus library mechanics. TRIGGER only when the JVM backend stack is Quarkus — building, reviewing, or debugging a Quarkus REST resource, CDI bean, Panache entity, Mutiny pipeline, HTTP security policy, or native image build. SKIP for Spring Boot (mir-backend-jvm-spring), Micronaut (mir-backend-jvm-micronaut), standalone Vert.x, Helidon, Ktor, every other non-Quarkus JVM framework, and every non-JVM runtime."
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

**Stack assumed:** Quarkus 3.x · CDI (ArC) · Quarkus REST (or the classic imperative stack) · Hibernate ORM with Panache · SmallRye Mutiny · GraalVM CE / Mandrel for native image.

**Version status, verified 13 Aug 2026:**

| Line | Status | Use it when |
|---|---|---|
| **Quarkus 3.33 LTS** | Current LTS, patches to 25 Mar 2027 | Production default |
| **Quarkus 3.27 LTS** | Older LTS, support ends 24 Sep 2026 | Migrate off it now |
| **3.38.x** | Latest non-LTS minor (new minors every 4–6 weeks) | Only if you track releases continuously — a non-LTS minor is supported only until the next one |
| Quarkus 4.0 | Beta 1 targeted Sep 2026 (JPMS modularization, JLink images) | Not yet |

Java baseline is **17**, with 21 and 25 supported. The Leyden AOT cache needs Java 25; `@RunOnVirtualThread` wants Java 24+ (see §2).

**Naming:** the reactive REST stack was renamed **Quarkus REST** (`quarkus-rest`, `quarkus-rest-jackson`). `quarkus-resteasy-reactive` coordinates are legacy — the same rename hit the CSRF extension (`quarkus-csrf-reactive` → `quarkus-rest-csrf`). Code generated from older training data will name extensions that no longer match the current artifact ids.

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

Also register resources, and any dynamic proxy no extension owns:
```properties
# Static resources included in the native image
quarkus.native.resources.includes=templates/*.html,certs/*.pem
```
`@RegisterRestClient` interfaces are proxied by the REST Client extension — you do not register those yourself. For an unrelated runtime-generated proxy, supply GraalVM proxy metadata under `META-INF/native-image`, or emit a `NativeImageProxyDefinitionBuildItem` from an extension. **`--initialize-at-build-time` controls class-initialization timing and is not proxy registration** — reaching for it here is a common wrong turn.

**Build-time vs runtime config:** Quarkus bakes some config values into the native binary at build time (tagged `@ConfigProperty` defaults that affect build-time extensions — e.g., datasource URL during codegen). If you change a build-time key at container start-time only, the native image ignores it. Runtime config keys (most application keys) are read at startup and work as expected. Check the Quarkus config reference for which keys are `STATIC_INIT` vs `RUNTIME_INIT`. The same mechanism has a security consequence — a credential supplied at build time is baked into the artifact and cannot be rotated at start-up. See Security below.

### 2. Blocking the Vert.x I/O thread — the rule is the return type, not the annotation

Quarkus's reactive core runs on a Vert.x event loop. **Quarkus REST picks the thread from the method signature**, and most existing advice gets the direction backwards:

| Method signature | Runs on | Consequence |
|---|---|---|
| `Uni`, `Multi`, `CompletionStage`, `Publisher`, Kotlin `suspend` | **I/O thread (event loop)** | Any blocking call inside stalls every request on that loop |
| Anything else (`Report`, `String`, `Response`, `void`) | **Worker thread** | Blocking is expected and safe. `@Blocking` here is a no-op |
| Annotated `@Blocking` | Worker thread | Forces a reactive-returning method off the loop |
| Annotated `@NonBlocking` | I/O thread | Forces a plain-returning method *onto* the loop — now it must not block |
| Annotated `jakarta.transaction.Transactional` | Worker thread | JTA needs blocking, so it implies `@Blocking` |

So the real defect is the opposite of the usual advice: **the trap is blocking work inside a method that returns `Uni`/`Multi`**, not a plain method missing `@Blocking`.

```java
// WRONG — returns Uni, so this runs on the event loop, and the JDBC call blocks it
@GET @Path("/report")
public Uni<Report> getReport() {
    return Uni.createFrom().item(reportRepository.buildHeavyReport()); // blocking JDBC on the loop
}

// WRONG — @NonBlocking moves a blocking method onto the loop
@GET @Path("/report")
@NonBlocking
public Report getReport() { return reportRepository.buildHeavyReport(); }

// RIGHT — plain return type; Quarkus already dispatches this to the worker pool
@GET @Path("/report")
public Report getReport() { return reportRepository.buildHeavyReport(); }

// RIGHT — stay reactive all the way down
@GET @Path("/report")
public Uni<Report> getReport() { return reportRepository.buildHeavyReportAsync(); }
```

Annotations resolve most-specific-first: method, then class, then `Application`. A class-level `@NonBlocking` silently puts every plain-returning method on the event loop.

**`@RunOnVirtualThread` swaps the worker pool for one virtual thread per request**, and the advice around it changed with the JDK:

```java
@GET @Path("/report")
@RunOnVirtualThread          // blocking code, one virtual thread per request
public Report getReport() { return reportRepository.buildHeavyReport(); }
```

- On **Java 21–23**, `synchronized` inside such a method pins the carrier thread, and JDBC drivers older than PostgreSQL 42.6.0 pinned frequently. That was the reason to avoid virtual threads for JDBC work.
- On **Java 24+ (JEP 491)** `synchronized` no longer pins, and most JDBC drivers stop pinning. If you are adopting `@RunOnVirtualThread`, run on 24+ (25 for LTS).
- Quarkus ships a pinning-detection extension built on JFR events; it works on 21 and 24+. Do not reach for `-Djdk.tracePinnedThreads` — it was removed in JDK 24 and now prints nothing (see `mir-backend-jvm` §2).
- Virtual threads do not raise your database's connection limit. Keep the datasource pool bounded and expect it, not the thread count, to be the ceiling.

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
// WRONG — no transaction boundary. persist() is NOT an auto-committed mini-transaction:
// Hibernate ORM requires an active transaction, so the write either throws or is
// silently never flushed. Either way the transfer does not happen and nothing says so.
public void transfer(Long fromId, Long toId, BigDecimal amount) {
    Account from = Account.findById(fromId);
    from.balance = from.balance.subtract(amount);
    from.persist();

    Account to = Account.findById(toId);
    to.balance = to.balance.add(amount);
    to.persist();
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

**The direction most write-ups get wrong:** GraalVM Native Image initializes *application* classes at **run time** by default. Build-time initialization is opt-in — and Quarkus opts a large set of framework classes in. So the hazard is not "every static initializer runs at build time"; it is that a class Quarkus (or your own `--initialize-at-build-time`) promoted to build-time initialization captures environment variables, clock, randomness, filesystem, or network state into the binary.

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

## Security

Quarkus-specific mechanics. JDK-level items (deserialization filters, XXE, SSRF address checks, heap dumps, Maven/Gradle supply chain) are in `mir-backend-jvm`.

### Current advisory class: path-normalization bypass of path-based HTTP policies

Two 2026 advisories break `quarkus.http.auth.permission.*` in the same way — the security layer and the routing layer normalize the request path differently, so a request that the router maps to a protected endpoint is not seen as protected by the policy check.

| Advisory | Vector | Affected | Fixed |
|---|---|---|---|
| **CVE-2026-39852** | A literal semicolon (matrix parameter) appended to the path — `/api/admin;x=1/data` — passes the policy check but still routes to `/api/admin/data` | <3.20.6.1, 3.21.0–3.27.3.1, 3.30.0–3.33.1.1, 3.34.0–3.35.1.1 | 3.20.6.1, 3.27.3.1, 3.33.1.1, 3.35.1.1 and later |
| **CVE-2026-50559** (GHSA-qcxp-gm7m-4j5v) | **Encoded** semicolons (`%3B`), and encoded slashes/backslashes (`%2F`, `%5C`) reaching protected static resources. `AbstractPathMatchingHttpSecurityPolicy` decodes only unreserved characters; `StaticHandlerImpl` / `FileSystemStaticHandler` decode fully | 3.20.6.1, 3.27.3.1, 3.33.1.1, 3.35.1.1, 3.35.2, 3.34.7, 3.36, 3.38 | 3.20.6.2, 3.27.4.1, 3.27.5, 3.33.2.1, 3.33.3, 3.36.3, 3.37.0, 3.38.1 |

No workaround was published for CVE-2026-50559 — patch. Two durable lessons:

- **Prefer annotation-based authorization to path-based policies.** `@RolesAllowed` / `@Authenticated` on the resource method is evaluated after routing, so path-normalization mismatches cannot skip it. Quarkus's own documentation recommends the annotation route over path matching. Applications with no path-based policies were not affected by the encoded-slash vectors.
- If you keep path policies, they are a second layer, not the only one.

### Endpoints are open unless you close them

| Key | Default | Effect |
|---|---|---|
| `quarkus.security.jaxrs.deny-unannotated-endpoints` | `false` | A REST endpoint with **no** security annotation is publicly reachable. A new endpoint added without `@RolesAllowed` is live and anonymous |
| `quarkus.security.jaxrs.default-roles-allowed` | unset | Alternative to the above: give unannotated endpoints a default requirement (`**` = any authenticated user). Mutually exclusive with `deny-unannotated-endpoints` |
| `quarkus.security.deny-unannotated-members` | `false` | Denies CDI methods and endpoints without annotations *in classes that already use them* — catches the method someone forgot |

Set `quarkus.security.jaxrs.deny-unannotated-endpoints=true` and let `@PermitAll` mark the deliberately public routes. Note the interaction with `quarkus.http.auth.proactive=false`: with lazy authentication, identity is resolved only when something asks for it, which changes when failures surface — decide it deliberately rather than copying it from a blog post about a 401 problem.

### CORS: the key is `quarkus.http.cors.enabled`, and origins are deny-by-default

Two things AI gets wrong here, in opposite directions:

- **The key is `quarkus.http.cors.enabled`, not `quarkus.http.cors`.** The bare form is the legacy name. Set the wrong one and the filter is simply not installed.
- **Enabling CORS with `origins` unset does *not* open the app to every origin.** Quarkus documents `origins` as: when not specified, CORS is not permitted and only same-origin requests are allowed. Do not raise this as a finding on an app that enables CORS without origins — it fails closed. What *is* permissive once an origin is allowed is methods and request headers, which default to any.

```properties
# Filter installed, but no cross-origin request is permitted
quarkus.http.cors.enabled=true

# RIGHT — enumerate origins in prod, and pin methods/headers rather than taking the any-default
quarkus.http.cors.enabled=true
quarkus.http.cors.origins=https://app.example.com
quarkus.http.cors.methods=GET,POST
%dev.quarkus.http.cors.origins=/.*/
```

Regex origins in `application.properties` need their special characters escaped correctly (Quarkus's docs call for four backslashes); a mis-escaped pattern matches more than you intended. Treat a regex origin plus credentials as equivalent to trusting every host that pattern can match, including attacker-controlled subdomains.

### CSRF

Cookie-based auth (form login, OIDC session cookies) needs CSRF protection; a stateless bearer-token API does not. The extension is **`quarkus-rest-csrf`** (`quarkus.rest-csrf.*`), implementing double-submit cookie plus request header, with a filter over POST/PUT/PATCH/DELETE. Set `quarkus.rest-csrf.token-signature-key` to at least 32 characters. It is a Quarkus REST filter, so it does not cover Vert.x routes or a login endpoint that is not served by a JAX-RS resource — check that your actual login path is inside its scope.

### Panache: injection and mass assignment

```java
// WRONG — string-concatenated HQL
Person.find("name = '" + name + "'");
getEntityManager().createNativeQuery("select * from person where name = '" + name + "'");

// RIGHT — bound parameters
Person.find("name = ?1", name);
Person.find("name = :name", Parameters.with("name", name));
```

Sort and ordering strings are concatenated into the query as identifiers — never pass a raw client field name into `Sort.by(...)`; validate against an allow-list.

**Mass assignment is easier to hit in Panache than in most stacks** because active-record entities expose *public fields*. Binding a request body straight onto a `PanacheEntity` lets the client set `id`, `tenantId`, `role`, or any other column. Take a dedicated request record, map explicitly, and never return the entity as the response type. If you use REST Data Panache to generate CRUD resources, understand that it exposes the entity's full shape and every listed row — do not point it at a multi-tenant table.

Ownership checks are yours: `@RolesAllowed("user")` on `GET /orders/{id}` says nothing about who owns that order. Query with the tenant/owner predicate included.

### Build-time config, native images, and secrets

Quarkus records build-time configuration into the artifact. **A secret supplied at build time is baked into the binary or the augmented jar** and cannot be rotated by changing an environment variable at start-up. Keep every credential in a runtime config source (environment variable, mounted file, Vault) and verify by checking that the value is absent from a built image. The same applies to a `%prod` profile block committed with real values.

Native image also affects deserialization: `@RegisterForReflection` opens a class for reflective construction. Registering broad packages to "make the build work" can hand a deserializer more classes than you intended — register the specific types.

### Dev mode belongs nowhere near production

Dev Services (Testcontainers-backed) and the Dev UI exist only in dev/test, and that is the point — the Dev UI's config editor has historically been a remote-code-execution path against a developer's own machine (CVE-2022-4116, CVSS 9.8, drive-by localhost attack). Two rules: never enable dev mode or the Dev UI in a deployed environment, and never bind a dev-mode process to `0.0.0.0` on a shared network.

### File uploads and path traversal

`FileUpload.fileName()` is client-controlled. Never use it to build a path under `quarkus.http.body.uploads-directory` — generate your own name and store the original as metadata. Cap `quarkus.http.limits.max-body-size` and `quarkus.http.body.multipart` limits explicitly; an unbounded upload directory is a disk-exhaustion path.

### SSRF via the REST Client

A `@RegisterRestClient` interface with a static base URI is fine. The risk is `RestClientBuilder.baseUri(userSupplied)` or a base URL taken from config that a user can influence. Validate the resolved address before connecting (see the JVM tier's SSRF section, including the `169.254.169.254` metadata case), and do not let a client-supplied URL inherit your outbound credentials.

### If the service calls an LLM (quarkus-langchain4j)

`@RegisterAiService` methods and `@Tool` methods are entry points reachable through any text the model reads — a document, an email body, a database row. Put the same authorization on a `@Tool` method that you would put on a REST resource, validate the arguments the model produces instead of trusting them, and keep retrieved content in a separate message from your instructions.

### Supply chain

Import `io.quarkus.platform:quarkus-bom` (matching your LTS) in `dependencyManagement` and let it pin extension and transitive versions rather than naming versions per dependency; a hand-pinned extension that drifts from the platform BOM is how a patched CVE gets un-patched. Everything in the JVM tier's supply-chain table (checksum verification, banning dynamic versions, dependency confusion, build scripts executing code) applies unchanged.

## How this slots into the core pipeline

- **Gate 5 (Design):** for each route, state the return type and therefore the thread it runs on (§2 table). For every `Uni`/`Multi`/`@NonBlocking` route, list the operations inside it and prove none of them block. Confirm production config profiles exist alongside Dev Services setup. State the Quarkus version against the two path-normalization advisories.
- **Gate 6 (Implementation):** `@RegisterForReflection` on all reflection targets; no blocking call inside a `Uni`/`Multi`/`@NonBlocking` method without `runSubscriptionOn`; `@Transactional` (or `@WithTransaction` for reactive) wrapping multi-step Panache operations; production datasource keys under `%prod.`; `deny-unannotated-endpoints=true`; parameterized Panache queries; no request body bound onto a `PanacheEntity`.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–6; the security-reviewer checks the Security section — CORS origins, unannotated endpoints, CSRF scope, upload paths, secrets not baked at build time, and the Quarkus version against CVE-2026-39852 / CVE-2026-50559.

## Edit boundary (what belongs here vs. above/below)

Apply the 3-tier placement test before adding anything:

- True for Go/Node/Python too (idempotency, invariants, gates, observability)? → **generic core** (`mir-backend`).
- True for every JVM framework (thread-pool sizing, virtual-thread pinning and JEP 491, GC tuning, container heap, ThreadLocal hygiene, JMM visibility, `ObjectInputFilter`, XXE defaults, Maven/Gradle verification)? → **runtime tier** (`mir-backend-jvm`).
- A mechanical footgun of *this library* (`@RegisterForReflection`, `@Blocking` on I/O thread, `@RunOnVirtualThread`, Mutiny pipeline blocking, Panache `@Transactional` and query binding, Dev Services absent in prod, native-image static initializers, `quarkus.security.*` and `quarkus.http.cors.*` defaults, the path-normalization advisories)? → **here**.
- A *different* JVM framework (Spring Boot, Micronaut) → its own `mir-backend-jvm-<framework>` module. A *different* runtime → its own tier. Never widen this one.
