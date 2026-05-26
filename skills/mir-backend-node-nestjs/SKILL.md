---
name: mir-backend-node-nestjs
description: "Make It Right (NestJS module). NestJS + TypeScript specific reliability augmentation. Use alongside mir-backend and mir-backend-node when the target stack is NestJS — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: singleton DI scope bleeding request state across users, the full execution-order pipeline (middleware → guards → interceptors → pipes → handler → interceptors → exception filters), ValidationPipe with whitelist and forbidNonWhitelisted to stop mass assignment, and offloading durable work to a queue (Bull/BullMQ) rather than running it in a request. TRIGGER only when the Node backend stack is NestJS — building, reviewing, or debugging a NestJS controller, provider, guard, pipe, interceptor, or exception filter. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-node (V8 event-loop / process-model concerns: blocking, worker_threads, unhandled rejections, backpressure, timeouts); this module only adds NestJS library mechanics. SKIP for Express, Fastify (standalone), Hapi, Koa, or any non-NestJS stack (those get their own mir-backend-node-<framework> module), and for non-Node runtimes."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-node` (V8/Node event-loop model) → **this** (NestJS library mechanics). Run the gates first; load the Node runtime tier for event-loop and process-model concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (blocking the event loop, worker_threads, unhandled rejections, stream backpressure, AbortController timeouts, heap limits) live in `mir-backend-node` — not here.**

**Stack assumed:** NestJS 10/11 on Node 20+ LTS with TypeScript strict mode. NestJS runs on Express by default; Fastify adapter noted where behavior differs.

## The NestJS footguns AI walks into most

### 1. Singleton scope stores request state — bleeds across users

**All providers in NestJS are SINGLETON by default.** The same class instance handles every request for the lifetime of the application. Storing per-request data (current user, tenant ID, request ID, transaction handle) in a singleton service property is a race condition that bleeds one user's data into another's request under concurrent load — a serious security defect:

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
2. **`AsyncLocalStorage`** (from `mir-backend-node` §9) — carry request context without scope change; NestJS's `ClsModule` (nestjs-cls) wraps this cleanly.
3. **`REQUEST` scope** (`@Injectable({ scope: Scope.REQUEST })`) — NestJS creates a new instance per request. Fixes the bleed, but **propagates scope up the DI chain**: any singleton that injects a request-scoped provider also becomes request-scoped, defeating singleton reuse for the whole subtree. Reserve for cases where the other two patterns genuinely don't work.

```ts
// RIGHT — pass context explicitly; no scope change
@Injectable()
export class OrderService {
  async getOrders(userId: string) { // userId comes from the controller, which got it from the guard
    return this.db.findOrders({ userId });
  }
}
```

### 2. Execution order — know where each concern belongs

NestJS has a strict, non-negotiable pipeline order. Putting a concern in the wrong slot means it either runs too late to protect, or doesn't run at all for some code paths:

```
Incoming request
  │
  ├── Middleware          (Express-level: logging, cors, cookie parsing, body parsing)
  ├── Guards              (AuthN/AuthZ: can this request proceed?)
  ├── Interceptors (pre)  (cross-cutting: logging, caching, tracing — before handler)
  ├── Pipes               (input transform + validation: DTO parsing, sanitization)
  ├── Route Handler       (controller method)
  ├── Interceptors (post) (response transform: envelope wrapping, logging duration)
  └── Exception Filters   (error mapping: normalize thrown exceptions to HTTP responses)
```

Common mislacement AI makes:
- **Auth logic in a Pipe** — pipes run after guards; a pipe can't block an unauthenticated request.
- **Validation in a Guard** — guards return true/false (proceed/deny), not validated DTOs.
- **Response transformation in Middleware** — middleware runs before the NestJS pipeline and can't see what the handler returned.
- **Exception mapping in an Interceptor** — use `ExceptionFilter`; interceptors don't catch handler throws cleanly.

### 3. `ValidationPipe` without `whitelist` — mass assignment / overposting

NestJS ships `ValidationPipe` but its defaults allow extra properties through. Without `whitelist: true`, a client can send `{ "email": "a@b.com", "isAdmin": true }` and the DTO receives `isAdmin` if the underlying class has that field anywhere. Without `forbidNonWhitelisted: true`, extra fields are silently stripped — no error, no signal that the client is sending unexpected data:

```ts
// WRONG — validation is on, but mass assignment still possible
app.useGlobalPipes(new ValidationPipe());

// RIGHT — strip extras + reject requests with unexpected fields
app.useGlobalPipes(
  new ValidationPipe({
    whitelist: true,           // strips properties not in the DTO class
    forbidNonWhitelisted: true, // throws 400 if unexpected properties are sent
    transform: true,           // auto-coerce plain objects to DTO class instances
  }),
);
```

DTOs are the validation boundary — **never bind `req.body` / `@Body()` directly to an entity class**. Use separate `CreateUserDto` / `UpdateUserDto` classes with class-validator decorators and explicit allowed fields. Shared fields between create and update go in a `PartialType` / `PickType` from `@nestjs/mapped-types`:

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
  create(@Body() dto: CreateUserDto) { // ValidationPipe runs first, only safe fields arrive
    return this.userService.create(dto);
  }
}
```

### 4. Object-level authorization — guards check identity, not ownership

NestJS guards are the right place for authentication. They are **not** the right place for object-level authorization ("does this user own this specific resource?"). AI routinely adds a `JwtAuthGuard` and ships the feature — any authenticated user can read any other user's resource (IDOR):

```ts
// WRONG — auth guard present but no ownership check
@Get(':id')
@UseGuards(JwtAuthGuard)
async getAccount(@Param('id') id: string) {
  return this.accountService.findOne(id); // any authenticated user gets any account
}

// RIGHT — load the resource, assert ownership in the service or handler
@Get(':id')
@UseGuards(JwtAuthGuard)
async getAccount(@Param('id') id: string, @CurrentUser() user: User) {
  const account = await this.accountService.findOne(id);
  if (!account || account.userId !== user.id) {
    throw new NotFoundException(); // 404, not 403 — don't leak resource existence
  }
  return account;
}
```

For complex permission models, use a `PoliciesGuard` or CASL — but the rule is the same: guards decide "can this user call this endpoint at all"; the handler or service decides "can this user act on this specific record."

### 5. Durable work in a request — use Bull/BullMQ instead

NestJS makes it easy to inject a service and call a method synchronously in the request cycle. AI routinely puts work that must be reliable (sending email, charging a card, generating a report) directly in the controller — it's simple, but if the process restarts after the response is sent and before the work finishes, the work is lost:

```ts
// WRONG — "fire and forget" inside the request; no retry, no durability
@Post('orders')
async createOrder(@Body() dto: CreateOrderDto, @CurrentUser() user: User) {
  const order = await this.orderService.create(dto, user);
  await this.emailService.sendConfirmation(order); // if this or anything after fails, silent loss
  return order;
}

// RIGHT — return the response; enqueue durable work
@Post('orders')
async createOrder(@Body() dto: CreateOrderDto, @CurrentUser() user: User) {
  const order = await this.orderService.create(dto, user);
  await this.emailQueue.add('send-confirmation', { orderId: order.id }); // durable, retryable
  return order;
}
```

Use `@nestjs/bull` or `@nestjs/bullmq` for durable background jobs. Jobs are persisted in Redis; workers retry on failure; at-least-once delivery is built in. Make job handlers **idempotent** (the at-least-once / idempotency rule from `mir-backend` lands here). NestJS's `@Process()` / `@Worker()` decorators keep the processor in the same DI context.

### 6. Exception filters — consistent error mapping across the app

Unhandled exceptions that aren't `HttpException` subclasses bubble up to NestJS's default handler, which returns a generic 500 with no detail. Wire a global exception filter to normalize all errors to a consistent envelope before they reach the client:

```ts
@Catch()
export class AllExceptionsFilter implements ExceptionFilter {
  constructor(private readonly logger: Logger) {}

  catch(exception: unknown, host: ArgumentsHost) {
    const ctx = host.switchToHttp();
    const response = ctx.getResponse<Response>();
    const request = ctx.getRequest<Request>();

    const status = exception instanceof HttpException
      ? exception.getStatus()
      : HttpStatus.INTERNAL_SERVER_ERROR;

    if (status >= 500) {
      this.logger.error({ exception }, 'Unhandled exception');
    }

    response.status(status).json({
      statusCode: status,
      timestamp: new Date().toISOString(),
      path: request.url,
      message: exception instanceof HttpException
        ? exception.message
        : 'Internal server error',
    });
  }
}

// main.ts
app.useGlobalFilters(new AllExceptionsFilter(app.get(Logger)));
```

Register the filter globally in `main.ts` or via `APP_FILTER` in a module. Validation errors from `ValidationPipe` throw `BadRequestException` (an `HttpException`) — they're handled automatically.

## How this slots into the core pipeline

- **Gate 5 (Design):** declare provider scopes; identify which concerns belong in guards vs. pipes vs. interceptors vs. exception filters; design DTO classes with explicit allow-lists before writing handlers.
- **Gate 6 (Implementation):** `ValidationPipe` with `whitelist + forbidNonWhitelisted + transform` global; separate DTOs for create/update; object-level authz in every entity-loading handler; durable work via Bull/BullMQ; global exception filter.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–6 here; specifically checks that no singleton service has mutable per-request state fields and that no route relies on guard alone for object-level authorization.

## Edit boundary (what belongs here vs. above/below)

**This module holds ONLY NestJS library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Python/Java too (idempotency, invariants, gates)? → **generic core** (`mir-backend`).
- True for every Node framework (blocking the event loop, unhandled rejections, backpressure, heap limits, AbortController)? → **runtime tier** (`mir-backend-node`).
- A mechanical footgun of NestJS itself (DI singleton scope, execution pipeline order, ValidationPipe defaults, IDOR from missing object-level authz, durable work in request cycle, exception filter wiring)? → **here**.
- A different Node framework (Express, Fastify) → its own `mir-backend-node-<framework>` module. A different runtime → its own tier. Never widen this one.
