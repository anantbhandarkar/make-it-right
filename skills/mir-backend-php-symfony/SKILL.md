---
name: mir-backend-php-symfony
description: "Make It Right (Symfony module). Symfony 6/7 + Doctrine ORM + PostgreSQL/MySQL + Messenger + API Platform specific reliability augmentation. Use alongside mir-backend and mir-backend-php when the target stack is Symfony — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: Doctrine N+1 via lazy proxies, Unit of Work memory exhaustion in batch loops, EntityManager becoming closed/stale after an exception, service container singleton semantics and request-state bleed, Serializer group discipline to avoid data leaks, Messenger for async/queued work with idempotent handlers, Validator on DTOs, and explicit flush boundaries. TRIGGER only when the PHP backend stack is Symfony — building, reviewing, or debugging a Symfony controller, Doctrine entity/repository, Messenger handler, migration, or service. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-php (Zend Engine runtime concerns: shared-nothing lifecycle, FPM worker model, persistent-runtime state bleed, opcache, error model); this module only adds Symfony/Doctrine library mechanics. SKIP for Laravel, WordPress, Slim, or any non-Symfony PHP stack (those get their own mir-backend-php-<framework> module), and for non-PHP runtimes."
trigger: /mir-backend-php-symfony
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-php-symfony · Make It Right (Symfony)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-php` (Zend Engine runtime model) → **this** (Symfony/Doctrine library mechanics). Run the gates first; load the PHP runtime tier for the lifecycle/process model; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (shared-nothing lifecycle, FPM sizing, Swoole/RoadRunner/FrankenPHP state bleed, opcache, pconnect, error model) live in `mir-backend-php` — not here.**

**Stack assumed:** Symfony 6/7 · Doctrine ORM 2/3 · PostgreSQL or MySQL · Symfony Messenger · API Platform (optional). If the project uses DBAL only (no ORM) or a different message bus, note the divergence before applying these.

## The Symfony/Doctrine footguns AI walks into most

### 1. Doctrine N+1 via lazy proxies — silent DB storm

Doctrine wraps unloaded associations in lazy-loading proxy objects. Accessing a proxy property (e.g. `$order->getCustomer()->getName()`) inside a loop triggers a SELECT per iteration — N orders produce N+1 queries total. AI writes this constantly because the PHP code *looks* correct.

- **Fix:** use DQL fetch joins (`JOIN FETCH`) or `findBy` with `fetch: 'EAGER'` configuration. For collections, use `Criteria` or a custom repository method with `leftJoin`/`addSelect`. In API Platform, configure `eager` fetch on the relation or use an `ApiFilter` with join.

```php
// WRONG — N+1: one SELECT per $order->getCustomer()
$orders = $em->getRepository(Order::class)->findAll();
foreach ($orders as $order) {
    echo $order->getCustomer()->getName(); // proxy fetch per row
}

// RIGHT — fetch join: single query with JOIN
$orders = $em->createQuery(
    'SELECT o, c FROM App\Entity\Order o JOIN FETCH o.customer c'
)->getResult();

// Or in a repository method using QueryBuilder
$qb->select('o', 'c')
   ->from(Order::class, 'o')
   ->innerJoin('o.customer', 'c'); // eager in the query
```

### 2. Unit of Work memory exhaustion in batch processing

The Doctrine Unit of Work (UoW) holds a reference to **every managed entity** it has seen since the EntityManager was created. In a batch loop that processes thousands of rows — common in console commands, Messenger handlers, or import scripts — the UoW grows without bound until the process hits PHP's `memory_limit`.

- **Fix:** call `$em->clear()` periodically (every batch chunk) to detach all managed entities and allow GC to reclaim them. If you need to keep working with a subset, use `$em->detach($entity)` for individual objects. Pair with `$em->flush()` before clearing so you don't lose pending changes.

```php
// WRONG — UoW retains every Product entity; OOM after ~50k rows
$products = $em->getRepository(Product::class)->findAll(); // 500k rows
foreach ($products as $product) {
    $product->setUpdatedAt(new \DateTimeImmutable());
    $em->persist($product);
}
$em->flush(); // also: flush of 500k entities is one massive transaction

// RIGHT — chunk with flush+clear; keep UoW bounded
$batchSize = 500;
$i = 0;
$q = $em->createQuery('SELECT p FROM App\Entity\Product p')->toIterable();
foreach ($q as $product) {
    $product->setUpdatedAt(new \DateTimeImmutable());
    $em->persist($product);
    if (++$i % $batchSize === 0) {
        $em->flush();
        $em->clear(); // detach all; GC can now reclaim prior batch
    }
}
$em->flush();
$em->clear();
```

For read-only batch iteration, add `->setHint(Query::HINT_READ_ONLY, true)` to skip change-tracking overhead entirely.

### 3. EntityManager closed after an exception — the stale EM trap

When Doctrine catches a database exception during a flush (constraint violation, deadlock, connection error), it **closes the EntityManager** — `$em->isOpen()` returns `false`. Any subsequent ORM call on that EM throws `EntityManagerInterface is closed`. AI code that catches the exception and continues using the same EM instance produces cryptic errors on the next request or the next batch chunk.

- **Fix:** after any exception that may have closed the EM, check `$em->isOpen()` and reset via the ManagerRegistry: `$managerRegistry->resetManager()` returns a fresh EM. In Symfony services, inject `ManagerRegistry` rather than `EntityManagerInterface` directly so you can recover.

```php
// WRONG — continues using a closed EM after a flush exception
try {
    $em->flush();
} catch (\Exception $e) {
    $logger->error('Flush failed', ['e' => $e]);
    // $em is now closed — next $em->persist() or $em->flush() throws
}

// RIGHT — reset the EM so subsequent work gets a live instance
try {
    $em->flush();
} catch (\Exception $e) {
    $logger->error('Flush failed', ['e' => $e]);
    if (!$em->isOpen()) {
        $em = $this->managerRegistry->resetManager(); // fresh, open EM
    }
    // handle / rethrow as appropriate
}
```

In Messenger handlers, consider marking the message as failed (`ShouldNotRetryMessageInterface` or via stamps) rather than silently swallowing the error with a broken EM.

### 4. Service container — services are singletons; never store request state in them

Symfony's DI container creates services as **shared** (singleton-scope) by default — one instance for the lifetime of the process. Under FPM this is a single request, but under Swoole/RoadRunner (see `mir-backend-php`) the same instance handles many requests.

Even under FPM, AI often stores request-derived data in a service property and then uses it from a second service during the same request — this is fine under FPM but a correctness bug in persistent runtimes, and a code-smell regardless:

- **Fix:** pass request context explicitly (method arguments, DTOs, value objects) rather than storing it in service properties. For data that genuinely must be shared across collaborators within one request, use a `RequestStack`-scoped value or a Symfony-native request attribute.
- When running under a persistent runtime (Swoole/Symfony Runtime), mark services that hold request state as `shared: false` (a new instance per injection) or use a request-scoped service with a resetter implementing `ResetInterface`.

```php
// WRONG — service stores the authenticated user as a property
class OrderService {
    private User $currentUser;
    public function setUser(User $u): void { $this->currentUser = $u; }
    public function place(Cart $cart): Order { /* uses $this->currentUser */ }
}
// Under Swoole: $currentUser from request 1 bleeds into request 2

// RIGHT — pass via method argument; no state stored on the service
class OrderService {
    public function place(Cart $cart, User $actor): Order { /* pure */ }
}
```

### 5. Serializer groups — avoid over-exposing entity fields

Symfony's Serializer (and API Platform) serializes all public / annotated properties by default. Returning a full `User` entity serialization in an API response can expose `password`, `resetToken`, `stripeCustomerId`, or internal timestamps.

- **Fix:** use `#[Groups]` (or `@Groups` annotation) on entity properties to define named serialization contexts (`read`, `write`, `user:read`, `admin:read`). Pass the correct context array when serializing. In API Platform, define `normalizationContext` and `denormalizationContext` per operation. Never serialize a whole entity blindly — always specify groups.

```php
use Symfony\Component\Serializer\Annotation\Groups;

class User {
    #[Groups(['user:read', 'admin:read'])]
    public int $id;

    #[Groups(['user:read', 'admin:read'])]
    public string $email;

    // NOT in any group — never exposed in API responses
    public string $password;

    #[Groups(['admin:read'])] // only admins see this
    public bool $isAdmin;
}

// Serialize with explicit context — only 'user:read' fields are included
$json = $serializer->serialize($user, 'json', ['groups' => ['user:read']]);
```

In API Platform, declare groups on the `#[ApiResource]` attribute per operation to lock down what each endpoint exposes and accepts.

### 6. Messenger — async handlers with idempotency

Symfony Messenger routes messages to handlers synchronously or asynchronously via a configured transport (AMQP, Redis, Doctrine). AI commonly ships handlers that are not safe to retry:

- **Idempotency:** a handler can be called more than once (transport redelivery, worker crash mid-ack). Check for a unique constraint or a sentinel record before performing the side effect. Use the message's ID (stamp or payload field) as the idempotency key.
- **Flush boundary:** the EntityManager is shared within a handler invocation — flush explicitly at the end of the handler, not relying on the bus to flush for you. Some Messenger middleware handles this, but be explicit.
- **Failure transport:** configure `failure_transport` so failed messages go to a dead-letter queue rather than disappearing. Use `messenger:failed:show` and `messenger:failed:retry` for ops recovery.

```php
// WRONG — no idempotency guard; double-processing charges twice
class ProcessPaymentHandler implements MessageHandlerInterface {
    public function __invoke(ProcessPayment $message): void {
        $this->paymentGateway->charge($message->orderId, $message->amount);
        // if the worker crashes after charge but before ack → redelivered → double charge
    }
}

// RIGHT — idempotency via DB sentinel
class ProcessPaymentHandler implements MessageHandlerInterface {
    public function __invoke(ProcessPayment $message): void {
        $existing = $this->paymentRepo->findByIdempotencyKey($message->idempotencyKey);
        if ($existing !== null) {
            return; // already processed; safe to ack
        }
        $payment = $this->paymentGateway->charge($message->orderId, $message->amount);
        $this->paymentRepo->saveWithKey($payment, $message->idempotencyKey);
        $this->em->flush();
    }
}
```

### 7. Validator on DTOs — validate input, not entities

Symfony Validator can annotate entity classes, but validating HTTP input directly against entities couples your API surface to your DB schema. AI often skips validation entirely or validates the entity after it has already been persisted.

- **Fix:** create a DTO class for each request (input) and use `#[Assert\...]` constraints on the DTO. Inject `ValidatorInterface` and validate before constructing the entity. In API Platform, use input DTOs (`input: MyInputDto::class`) with constraints. In plain Symfony, use a Form or a manual `$validator->validate($dto)` check in the controller/command handler.
- Validate early — before any DB read or entity construction. Return a 422/400 with violation details rather than letting a constraint violation bubble up from the DB as a 500.

```php
use Symfony\Component\Validator\Constraints as Assert;

class CreateOrderInput {
    #[Assert\NotBlank]
    #[Assert\Positive]
    public int $productId;

    #[Assert\NotBlank]
    #[Assert\Range(min: 1, max: 1000)]
    public int $quantity;

    #[Assert\NotBlank]
    #[Assert\Currency]
    public string $currency;
}

// In the handler / controller — validate before touching the DB
$violations = $this->validator->validate($input);
if (count($violations) > 0) {
    throw new ValidationFailedException($input, $violations);
}
// Only now construct and persist the Order entity
```

## How this slots into the core pipeline

- **Gate 5 (Design):** confirm fetch strategy (fetch joins vs. lazy proxies), identify Messenger-bound work, define Serializer groups per endpoint, specify flush boundaries and EM reset strategy for error paths.
- **Gate 6 (Implementation):** DQL fetch joins; `flush()`+`clear()` in batch loops; `managerRegistry->resetManager()` in exception handlers; `#[Groups]` on every serialized entity; idempotency sentinel in every Messenger handler; `#[Assert]` on input DTOs.
- **Gate 7 (Review):** reliability-reviewer checks items 1–7 here for any Symfony service; migration-reviewer applies the expand/contract pattern (add nullable, backfill in a separate command, add NOT NULL + index separately) to every schema change on a populated table.

## Edit boundary (what belongs here vs. the core)

**This module holds ONLY Symfony/Doctrine library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability principle)? → **generic core** (`mir-backend`).
- True for every PHP framework on Zend Engine (shared-nothing lifecycle, FPM sizing, Swoole state bleed at the runtime level, opcache, pconnect, error model)? → **runtime tier** (`mir-backend-php`).
- A mechanical footgun of *this library* (Doctrine N+1 via lazy proxies, UoW clear in batch, EM closed after exception, service singleton state, Serializer groups, Messenger idempotency, Validator on DTOs)? → **here**.
- A *different* PHP framework (Laravel, WordPress) → new `mir-backend-php-<framework>` module. A *different* runtime → its own tier. Never widen this one.
