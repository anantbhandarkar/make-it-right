---
name: mir-backend-node-nestjs
description: "Make It Right (NestJS module). NestJS 11 + TypeScript specific reliability augmentation. Use alongside mir-backend and mir-backend-node when the target stack is NestJS — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: singleton DI scope bleeding request state across users, the full execution-order pipeline (middleware → guards → interceptors → pipes → handler → interceptors → exception filters) and why middleware is not a security boundary on the Fastify adapter, ValidationPipe with whitelist and forbidNonWhitelisted to stop mass assignment, ClassSerializerInterceptor as the outbound allow-list, the Express 5 route-syntax break that NestJS 11 inherits, the TypeScript 7 compiler-API break that stops nest build, and offloading durable work to BullMQ rather than running it in a request. TRIGGER only when the Node backend stack is NestJS — building, reviewing, or debugging a NestJS controller, provider, module, guard, pipe, interceptor, or exception filter, on either the Express or the Fastify adapter. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-node (V8 event-loop / process-model concerns: blocking, worker_threads, unhandled rejections, backpressure, timeouts, npm supply chain); this module only adds NestJS library mechanics. SKIP for standalone Express (mir-backend-node-express), standalone Fastify (mir-backend-node-fastify), Hapi, Koa, and for non-Node runtimes."
trigger: /mir-backend-node-nestjs
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-node-nestjs · Make It Right (NestJS)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-node` (V8/Node event-loop model) → **this** (NestJS library mechanics). Run the gates first; load the Node runtime tier for event-loop and process-model concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (blocking the event loop, worker_threads, unhandled rejections, stream backpressure, timeouts, heap limits, npm supply chain) live in `mir-backend-node` — not here.**

**Stack assumed (npm registry, 13 Aug 2026):** `@nestjs/core@11.1.29` is `latest`. NestJS 12 exists only as `12.0.0-alpha.5` on the `next` tag — a pre-release; do not target it. `engines.node` says `>= 20`, but Node 20 is EOL — run on Node 22 or 24 per the runtime tier.

**Run 11.1.24 or newer across every `@nestjs/*` package.** Older 11.1.x patches carry the middleware-bypass CVEs listed in Security. The packages release in lockstep, so mismatched `@nestjs/*` versions in a lockfile are themselves the finding.

**Adapter pins, which matter when you audit transitive versions:** `@nestjs/platform-express@11.1.29` pins `express@5.2.1`, `path-to-regexp@8.4.2`, `multer@2.2.0`, `cors@2.8.6` — exact, not caret, so you cannot bump them independently without an override. `@nestjs/platform-fastify@11.1.29` pins `fastify@5.11.0`, `@fastify/cors@11.3.0`, `fastify-plugin@6.0.0`.

## The NestJS footguns AI walks into most

### 1. Singleton scope stores request state — bleeds across users

**All providers in NestJS are SINGLETON by default.** The same class instance handles every request for the lifetime of the application. Storing per-request data (current user, tenant ID, request ID, transaction handle) in a singleton service property is a race condition that bleeds one user's data into another's request under concurrent load — a security defect, not a style problem:

```ts
// WRONG — singleton; currentUser is shared across all concurrent requests
@Injectable()
export class OrderService {
  private currentUser: User; // ← shared mutable state; data bleeds between requests

  async getOrders() {
    return this.db.findOrders({ userId: this.currentUser.id }); // wrong user under load
  }
}
```

Three correct patterns, in order of preference:

1. **Pass context as method parameters** — the cleanest; no scope change needed.
2. **`AsyncLocalStorage`** (from `mir-backend-node` §9) — carries request context without a scope change; `nestjs-cls` wraps it cleanly.
3. **`REQUEST` scope** (`@Injectable({ scope: Scope.REQUEST })`) — a new instance per request. Fixes the bleed, but **propagates up the DI chain**: any singleton that injects a request-scoped provider becomes request-scoped too, and so does everything that injects *it*. Reserve for cases where the other two genuinely don't work.

```ts
// RIGHT — pass context explicitly; no scope change
@Injectable()
export class OrderService {
  async getOrders(userId: string) { // comes from the controller, which got it from the guard
    return this.db.findOrders({ userId });
  }
}
```

The same rule covers module-level `let` variables and any cache keyed without the tenant ID.

### 2. Execution order — know where each concern belongs

NestJS has a strict, non-negotiable pipeline. Putting a concern in the wrong slot means it either runs too late to protect or does not run at all on some paths:

```
Incoming request
  │
  ├── Middleware          (adapter-level: logging, cookie parsing, body parsing)
  ├── Guards              (AuthN/AuthZ: may this request proceed?)
  ├── Interceptors (pre)  (cross-cutting: logging, caching, tracing)
  ├── Pipes               (input transform + validation: DTO parsing)
  ├── Route Handler       (controller method)
  ├── Interceptors (post) (response transform: serialization, timing)
  └── Exception Filters   (error mapping: exceptions → HTTP responses)
```

Common misplacements AI makes:
- **Auth logic in a Pipe** — pipes run after guards; a pipe cannot block an unauthenticated request.
- **Validation in a Guard** — guards return true/false, not validated DTOs.
- **Response transformation in Middleware** — middleware runs before the Nest pipeline and cannot see what the handler returned.
- **Final HTTP error mapping in an Interceptor** — interceptors *can* see handler failures (RxJS `catchError`), so use one when the transformation is genuinely part of the cross-cutting pipeline. But an interceptor does not run for exceptions thrown by guards or pipes, which run outside it. `ExceptionFilter` is the only slot that catches everything, so that is where the response shape belongs.
- **Authentication in Middleware** — see Security. On the Fastify adapter this has been bypassable three separate times. Use Guards.

### 3. `ValidationPipe` without `whitelist` — mass assignment / overposting

`ValidationPipe`'s defaults let extra properties through. Without `whitelist: true`, a client can send `{ "email": "a@b.com", "isAdmin": true }` and `isAdmin` reaches your handler **even though the DTO never declares it** — class-transformer copies every key it finds, and with `transform: false` you get the raw body object anyway. "My DTO doesn't have that field" is not a defence. With `whitelist: true` extras are stripped silently; without `forbidNonWhitelisted: true` you get no error and no signal that a client is probing:

```ts
// WRONG — validation is on, but mass assignment still possible
app.useGlobalPipes(new ValidationPipe());

// RIGHT — strip extras and reject requests that send them
app.useGlobalPipes(
  new ValidationPipe({
    whitelist: true,
    forbidNonWhitelisted: true,
    transform: true,
    transformOptions: { enableImplicitConversion: false }, // see below
  }),
);
```

`enableImplicitConversion: true` looks convenient and causes silent coercion bugs: `"0"` becomes `0`, `"false"` becomes `true` for a `boolean` property, and a `@IsNumber()` check can pass on something the client sent as a string. Leave it off and declare `@Type(() => Number)` where you want conversion.

DTOs are the validation boundary — **never bind `@Body()` directly to an entity class**. Use separate `CreateUserDto` / `UpdateUserDto` with class-validator decorators and explicit allowed fields; derive shared shapes with `PartialType` / `PickType` / `OmitType` from `@nestjs/mapped-types`:

```ts
export class CreateUserDto {
  @IsEmail()
  email: string;

  @IsString() @MinLength(1) @MaxLength(100)
  name: string;
  // id, isAdmin, role are NOT here
}

@Controller('users')
export class UserController {
  @Post()
  create(@Body() dto: CreateUserDto) { // ValidationPipe runs first; only safe fields arrive
    return this.userService.create(dto);
  }
}
```

Global pipes registered with `app.useGlobalPipes()` in `main.ts` cannot inject dependencies. If your pipe needs DI, register it with the `APP_PIPE` token in a module instead.

### 4. Object-level authorization — guards check identity, not ownership

NestJS guards are the right place for authentication. They are **not** the right place for object-level authorization ("does this user own this specific resource?"). AI routinely adds a `JwtAuthGuard` and ships the feature — any authenticated user can then read any other user's resource (IDOR):

```ts
// WRONG — auth guard present but no ownership check
@Get(':id')
@UseGuards(JwtAuthGuard)
async getAccount(@Param('id') id: string) {
  return this.accountService.findOne(id); // any authenticated user gets any account
}

// RIGHT — scope the query, don't filter after the fact
@Get(':id')
@UseGuards(JwtAuthGuard)
async getAccount(@Param('id') id: string, @CurrentUser() user: User) {
  const account = await this.accountService.findOneForUser(id, user.id);
  if (!account) throw new NotFoundException(); // 404, not 403 — don't confirm it exists
  return account;
}
```

Put the ownership predicate in the query (`WHERE id = ? AND user_id = ?`) rather than loading and then comparing — the load-then-compare form is the one people forget to copy onto the update and delete paths. For richer permission models use a `PoliciesGuard` or CASL, but the split stands: guards decide "may this user call this endpoint at all"; the service decides "may this user act on this record".

### 5. Durable work in a request — use BullMQ instead

NestJS makes it easy to call a service method inline in the request cycle. AI routinely puts work that must be reliable (sending email, charging a card, generating a report) directly in the controller. If the process restarts after the response is sent and before the work finishes, the work is lost:

```ts
// WRONG — no retry, no durability
@Post('orders')
async createOrder(@Body() dto: CreateOrderDto, @CurrentUser() user: User) {
  const order = await this.orderService.create(dto, user);
  await this.emailService.sendConfirmation(order); // silent loss on crash
  return order;
}

// RIGHT — return the response; enqueue durable work
@Post('orders')
async createOrder(@Body() dto: CreateOrderDto, @CurrentUser() user: User) {
  const order = await this.orderService.create(dto, user);
  await this.emailQueue.add('send-confirmation', { orderId: order.id });
  return order;
}
```

Use `@nestjs/bullmq` with BullMQ (`bullmq@6.1.0` is the registry latest today). The original `bull` package and `@nestjs/bull` are the legacy line — do not start new work there. Jobs persist in Redis and workers retry, which means delivery is **at-least-once**: job handlers must be idempotent (the idempotency rule from `mir-backend` lands exactly here). Set `attempts`, a backoff, and `removeOnComplete`/`removeOnFail` limits, or Redis grows without bound.

### 6. Exception filters — consistent error mapping without leaking internals

Exceptions that are not `HttpException` subclasses reach the default handler and become a generic 500. Wire a global filter to normalize everything, and make sure it does not echo internal messages:

```ts
@Catch()
export class AllExceptionsFilter implements ExceptionFilter {
  constructor(
    private readonly logger: Logger,
    private readonly httpAdapterHost: HttpAdapterHost,   // adapter-neutral send
  ) {}

  catch(exception: unknown, host: ArgumentsHost) {
    const { httpAdapter } = this.httpAdapterHost;
    const ctx = host.switchToHttp();

    const status = exception instanceof HttpException
      ? exception.getStatus()
      : HttpStatus.INTERNAL_SERVER_ERROR;

    if (status >= 500) this.logger.error({ exception }, 'Unhandled exception');

    httpAdapter.reply(ctx.getResponse(), {
      statusCode: status,
      timestamp: new Date().toISOString(),
      // exception.message only for 4xx — 5xx messages carry SQL, file paths, driver detail
      message: status < 500 && exception instanceof HttpException
        ? exception.message
        : 'Internal server error',
    }, status);
  }
}

// main.ts — APP_FILTER in a module if the filter needs injected dependencies
app.useGlobalFilters(new AllExceptionsFilter(app.get(Logger), app.get(HttpAdapterHost)));
```

**`ctx.getResponse()` returns the adapter's native object, not a Nest abstraction.** On Express that is `Response`, which has `.json()`. On Fastify it is `FastifyReply`, which does **not** — it has `.code()`/`.status()` and `.send()`. The `response.status(status).json(...)` form that most NestJS examples show throws `response.status(...).json is not a function` the moment someone swaps to `FastifyAdapter`. `httpAdapter.reply(res, body, status)` is the only form that survives both. `ValidationPipe` throws `BadRequestException`, so validation errors go through this same path.

### 7. NestJS 11 runs Express 5 — the route syntax changed underneath you

`@nestjs/platform-express@11.x` depends on Express 5, which uses `path-to-regexp` 8. Unnamed wildcards are gone:

| NestJS 10 (Express 4) | NestJS 11 (Express 5) |
|---|---|
| `@Get('users/*')` | `@Get('users/*splat')` |
| `@Get('users/*')` that must also match `/users` | `@Get('users/{*splat}')` |
| `@Get(':file.:ext?')` | `@Get(':file{.:ext}')` |

NestJS 11 auto-converts legacy patterns and logs `WARN [LegacyRouteConverter] Unsupported route path: "..."`. Treat that warning as a defect, not noise — the conversion is a compatibility shim and the converted route may not match what you intended. The same syntax applies to `setGlobalPrefix()` exclusions, `MiddlewareConsumer.forRoutes()`, and third-party modules that register routes; a 404 that appeared right after the v11 upgrade is almost always this.

Also changed in 11: termination lifecycle hooks (`OnModuleDestroy`, `OnApplicationShutdown`) now run in reverse order, and `CacheModule` moved to cache-manager v6 / Keyv, which changes external store configuration.

### 8. TypeScript 7 breaks `nest build` — pin the compiler

`npm install typescript` now resolves **7.0.2** (registry latest, published 2026-07-08), the native compiler line. TypeScript 7.0 does not ship the legacy compiler API, and `nest build` is an API consumer — it calls `createProgram()` and `program.emit()` with its own transformers. Reports indicate `nest build` fails on TypeScript 7 for that reason, and the same applies to the Swagger and GraphQL CLI plugins, `ts-jest`, `ts-loader`, and type-aware ESLint rules. NestJS has not published an official position or a `tsgo` builder as of this writing — verify against your own `@nestjs/cli` version before upgrading rather than trusting either this note or a blog post.

Practical setup today: pin `typescript` to the 6.x stable line (`6.0.3` is the latest 6.x on the registry) as the project dependency used for building, and if you want the faster checker run it out-of-tree rather than installing it into `node_modules/typescript`. `nest build -b swc` is the other option — much faster, but it does no type checking, so pair it with a separate `tsc --noEmit` in CI.

Related, from the runtime tier: **parameter properties are not erasable syntax**, so `node service.ts` cannot run idiomatic NestJS DI code. NestJS needs a real build step regardless of Node's native type stripping.

## Security

NestJS-specific mechanics. Runtime-level items (SSRF, command injection, prototype pollution, path traversal, npm supply chain, secrets in logs) live in `mir-backend-node`. Adapter-library items live in the Express and Fastify modules — but note the pinned adapter versions above mean you inherit those libraries' advisories through `@nestjs/platform-*`.

### Middleware is not a security boundary on the Fastify adapter — three CVEs prove it

Every one of these lets an unauthenticated client reach a handler that `MiddlewareConsumer.forRoutes()` was supposed to protect, on default adapter configuration:

| CVE | Affected | Fixed in | Bypass |
|---|---|---|---|
| CVE-2025-69211 (HIGH) | `@nestjs/platform-fastify` < 11.1.11 | 11.1.11 | URL-encoded path segment — `/%61dmin` does not match the middleware's `/admin` string, but the controller resolves it after decoding (time-of-check/time-of-use) |
| CVE-2026-33011 (HIGH) | `@nestjs/platform-fastify` ≤ 11.1.15 | 11.1.16 | `HEAD` request — Fastify routes HEAD to the GET handler, and the middleware registered for GET is skipped |
| CVE-2026-54281 (HIGH, CWE-863) | `@nestjs/platform-fastify` ≤ 11.1.23 | 11.1.24 | trailing slash — `GET /resource/` skips the middleware registered for `/resource` |

Two conclusions, and the second matters more than the patch:

1. Pin `@nestjs/platform-fastify` ≥ **11.1.24** (11.1.29 is current).
2. **Put authentication and authorization in Guards, not middleware.** Guards run inside the Nest pipeline against the resolved route, so they are not sensitive to path-string matching. Middleware runs at the adapter level against a raw path string, which is why this class of bypass keeps recurring. Use middleware for logging, correlation IDs, and body parsing — not for deciding who gets in.

The same reasoning applies to `setGlobalPrefix({ exclude: [...] })` and any path-string allow-list: they compare strings, not routes.

### Other current NestJS advisories

- **CVE-2026-35515** (`@nestjs/core` ≤ 11.1.17, fixed 11.1.18, CVSS 6.3): `SseStream._transform()` interpolates `message.type` and `message.id` without stripping `\r`/`\n`, so user-influenced values can inject forged SSE events or corrupt reconnection state via `Last-Event-ID`. Only reachable if your code maps user data into those fields — so also strip newlines yourself before they reach an SSE payload.
- **CVE-2026-40879** (`@nestjs/microservices` ≤ 11.1.18, fixed 11.1.19, HIGH, CVSS 7.5): `JsonSocket.handleData()` recurses once per message in a TCP frame, so many small valid JSON messages bypass the `maxBufferSize` check and overflow the call stack. About 47 KB crashes the process. Do not expose the TCP transport to untrusted networks.
- **CVE-2025-54782** (`@nestjs/devtools-integration` ≤ 0.2.0, fixed 0.2.1, CVSS 8.8): the devtools HTTP server passed request JSON into `vm.runInNewContext` with no origin checks, so any website a developer visited got code execution on their machine. Never enable devtools outside a local machine, and remove the dependency from projects that do not use it — a dev dependency that opens a listening port is production risk on a developer laptop.

### CORS, headers, rate limiting

`app.enableCors()` with no arguments allows any origin. As on the raw adapters, the exploitable form is reflecting the request origin together with credentials:

```ts
// WRONG — any site can make credentialed requests as the logged-in user
app.enableCors({ origin: true, credentials: true });

// RIGHT
app.enableCors({ origin: ['https://app.example.com'], credentials: true });
```

Nothing else is on by default either: add `helmet` (Express adapter) or `@fastify/helmet` (Fastify adapter) for security headers, and `@nestjs/throttler` for rate limiting. `ThrottlerGuard` keys on IP, so it inherits the trust-proxy problem from the adapter — set `trust proxy` to a hop count or subnet on Express, or `trustProxy` on Fastify, and never to `true`.

### `ClassSerializerInterceptor` is the outbound allow-list

`ValidationPipe` controls what comes in. Nothing controls what goes out unless you add it — returning an entity from a controller serializes every column, including `passwordHash` and `stripeCustomerId`:

```ts
export class User {
  id: string;
  email: string;

  @Exclude()          // dropped by ClassSerializerInterceptor
  passwordHash: string;
}

// main.ts — must be registered, or @Exclude() does nothing
app.useGlobalInterceptors(new ClassSerializerInterceptor(app.get(Reflector)));
```

Two traps: the interceptor only acts on **class instances**, so a raw object returned by a query builder or by Prisma passes through untouched — construct the DTO explicitly. And `@Exclude()` is opt-out; a column added later is exposed by default. `@Expose()` with `excludeExtraneousValues: true` is the allow-list form, and it is the safer default for anything holding credentials or payment data.

### Configuration and secrets

- Validate configuration at boot with `ConfigModule.forRoot({ validationSchema })` so a missing `JWT_SECRET` fails the deploy. Without it the container starts healthy and every sign/verify throws `secretOrPrivateKey must have a value` on the first login — an outage discovered by users, at the worst possible moment in a rollout.
- `ConfigModule` with `isGlobal: true` makes every value reachable from every provider — fine, but it means a debug endpoint that dumps `configService` dumps the secrets too.
- The default Nest logger prints the object you hand it. `this.logger.log(dto)` on a login DTO writes the password. Use a structured logger with redaction (`nestjs-pino`) and configure redact paths.

### CSRF applies to cookie sessions, not bearer tokens

If the browser attaches the credential automatically (session cookie), state-changing routes need a token check; `Authorization: Bearer` from JavaScript does not. Set `sameSite: 'lax'` and `secure: true` on session cookies, verify `Origin`/`Sec-Fetch-Site` on mutations, and use a maintained double-submit library — `csurf` has been unmaintained since 2022.

### File uploads

`FileInterceptor` uses `multer` (pinned to `2.2.0` by the Express adapter). Set `limits: { fileSize, files }` in the interceptor options — there is no useful default — generate the stored filename yourself rather than trusting `file.originalname`, and check the content type from the bytes rather than the client-supplied header. The Fastify adapter needs `@fastify/multipart` registered instead; the two paths have different defaults, so a working upload route does not port between adapters unchanged.

## How this slots into the core pipeline

- **Gate 5 (Design):** declare provider scopes; state which adapter (Express or Fastify) and why; place each concern in guards vs. pipes vs. interceptors vs. exception filters; design DTOs (inbound) and serialization classes (outbound) with explicit allow-lists before writing handlers.
- **Gate 6 (Implementation):** `ValidationPipe` with `whitelist + forbidNonWhitelisted + transform` and implicit conversion off; separate DTOs per operation; `ClassSerializerInterceptor` registered; object-level authorization pushed into the query on every entity-loading handler; durable work through BullMQ with idempotent handlers; global exception filter that does not echo 5xx messages.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–8; the security-reviewer confirms every `@nestjs/*` package is ≥ 11.1.24, that no authentication lives in middleware, that `@nestjs/devtools-integration` is absent or ≥ 0.2.1, and that no singleton provider holds per-request state.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY NestJS library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Java too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Node framework (blocking the event loop, unhandled rejections, backpressure, heap limits, timeouts, SSRF, command injection, npm supply chain)? → **runtime tier** (`mir-backend-node`).
- A mechanical footgun of NestJS itself (DI singleton scope, pipeline order, ValidationPipe and serializer defaults, guards vs. middleware, adapter pins, `nest build`, IDOR from missing object-level authorization, durable work in the request cycle)? → **here**.
- A footgun of the underlying library used standalone (Express middleware arity, Fastify plugin encapsulation) → its own module. A different runtime → its own tier. Never widen this one.
