---
name: mir-backend-php-symfony
description: "Make It Right (Symfony module). Symfony 8.1 / 7.4 LTS + Doctrine ORM 3 + PostgreSQL/MySQL + Messenger + API Platform — mechanical reliability augmentation. Use alongside mir-backend and mir-backend-php when the target stack is Symfony; it carries the footguns the framework-agnostic tiers deliberately omit: Doctrine N+1 via lazy proxies, Unit of Work memory exhaustion in batch loops, the EntityManager closing after a failed flush, service-container singletons and ResetInterface under worker runtimes, Serializer normalization AND denormalization groups, Messenger with #[AsMessageHandler] and idempotent handlers, request-to-DTO mapping with #[MapRequestPayload]/#[MapQueryString]/#[MapUploadedFile], and the removal of Request::get() in Symfony 8. TRIGGER only when the PHP backend stack is Symfony — building, reviewing, or debugging a Symfony controller, Doctrine entity/repository, Messenger handler, Voter, migration, or DI service. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-php (Zend Engine runtime concerns: shared-nothing lifecycle, FPM worker model, worker-runtime state bleed, opcache/JIT, php.ini security, Composer supply chain); this module only adds Symfony/Doctrine library mechanics. SKIP for Laravel/Eloquent/Octane/Artisan work (that is mir-backend-php-laravel), for WordPress, Slim, or any non-Symfony PHP stack (each gets its own mir-backend-php-<framework> module), and for non-PHP runtimes."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-php` (Zend Engine runtime model) → **this** (Symfony/Doctrine library mechanics). Run the gates first; load the PHP runtime tier for the lifecycle/process model; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (shared-nothing lifecycle, FPM sizing, worker-runtime state bleed and superglobal resets, opcache/JIT, php.ini hardening, `unserialize`, Composer supply chain) live in `mir-backend-php` — not here.**

**Versions, verified 13 August 2026:**

| Branch | Status | PHP min | Bug fixes until | Security until |
|---|---|---|---|---|
| 8.1 | current stable (8.1.4, May 2026) | 8.4 | Jan 2027 | Jan 2027 |
| 8.0 | **unmaintained since July 2026** (last 8.0.16) | 8.4 | ended | ended |
| 7.4 | **LTS** (7.4.16, Nov 2025) | 8.2 | Nov 2028 | Nov 2029 |
| 6.4 | LTS, in maintenance | 8.1 | Nov 2026 | Nov 2027 |
| 5.4 | security fixes only | 7.2 | ended | Feb 2029 |

7.4 and 8.0 shipped together in November 2025 with the same feature set — 8.0 is 7.4 minus the deprecated code, which is why an 8.0 project moves to 8.1 (not to 7.4) to get back on a maintained branch. 8.2 lands November 2026; the next LTS is 8.4 in November 2027.

**Doctrine, verified 13 August 2026:** ORM 3.6.8 (5 Aug 2026) with DBAL 4. ORM 2.x is in limited maintenance since March 2026 — only PHP-compatibility changes are merged. **ORM 4 has not been released**; its milestone is open with no date. ORM 3.4+ supports native lazy objects and property hooks.

**Stack assumed:** Symfony 8.1 or 7.4 LTS · Doctrine ORM 3 · PostgreSQL or MySQL · Symfony Messenger · API Platform (optional). If the project uses DBAL only (no ORM), a different message bus, or is still on ORM 2, note the divergence before applying these.

## The Symfony/Doctrine footguns AI walks into most

### 1. Doctrine N+1 via lazy proxies — silent DB storm

Doctrine wraps unloaded associations in lazy-loading proxies. Accessing a proxy property (`$order->getCustomer()->getName()`) inside a loop triggers a SELECT per iteration — N orders produce N+1 queries. AI writes this constantly because the PHP *looks* correct.

- **Fix:** make the join a *fetch* join by putting the joined alias in the `SELECT`. **Doctrine DQL has no `FETCH` keyword** — JPQL's `JOIN FETCH` is a syntax error here, and AI writes it constantly because the JPQL form dominates training data. The Doctrine docs are explicit: "There is no special DQL keyword that distinguishes a regular join from a fetch join. A join becomes a 'fetch join' as soon as fields of the joined entity appear in the SELECT part." In QueryBuilder that is `addSelect()` on the alias — a bare `innerJoin` without it still leaves a proxy. In API Platform, configure eager fetch on the relation or add the join in a custom extension.
- Mapping-level `fetch: 'EAGER'` works for to-one associations and is a blunt instrument: it applies to every query, including ones that do not need the relation. Prefer per-query fetch joins.

```php
// WRONG — N+1: one SELECT per $order->getCustomer()
$orders = $em->getRepository(Order::class)->findAll();
foreach ($orders as $order) {
    echo $order->getCustomer()->getName(); // proxy fetch per row
}

// WRONG — `JOIN FETCH` is JPQL, not DQL. Doctrine throws a QueryException.
// $em->createQuery('SELECT o FROM App\Entity\Order o JOIN FETCH o.customer c');

// RIGHT — fetch join: `c` in the SELECT is what makes it eager. One query.
$orders = $em->createQuery(
    'SELECT o, c FROM App\Entity\Order o JOIN o.customer c'
)->getResult();

// QueryBuilder equivalent — addSelect() is the part AI omits
$qb->select('o')->addSelect('c')
   ->from(Order::class, 'o')
   ->innerJoin('o.customer', 'c');
```

Fetch-joining a to-many collection **breaks `setFirstResult`/`setMaxResults`** — the row multiplication makes the limit apply to joined rows, not entities. Use `Doctrine\ORM\Tools\Pagination\Paginator` when you need both.

### 2. Unit of Work memory exhaustion in batch processing

The Unit of Work holds a reference to **every managed entity** it has seen since the EntityManager was created. In a batch loop — console command, Messenger handler, import script — it grows without bound until the process hits `memory_limit`.

- **Fix:** `flush()` then `clear()` every chunk. `clear()` detaches everything so GC can reclaim it. Anything you still hold a variable to after `clear()` is now **detached** — calling `flush()` on it does nothing and lazy-loading from it throws. Re-fetch after clearing.

```php
// WRONG — the UoW retains every Product; OOM after tens of thousands of rows
$products = $em->getRepository(Product::class)->findAll(); // 500k rows
foreach ($products as $product) {
    $product->setUpdatedAt(new \DateTimeImmutable());
    $em->persist($product);
}
$em->flush(); // also: one enormous transaction

// RIGHT — chunk with flush+clear; the UoW stays bounded
$batchSize = 500;
$i = 0;
$q = $em->createQuery('SELECT p FROM App\Entity\Product p')->toIterable();
foreach ($q as $product) {
    $product->setUpdatedAt(new \DateTimeImmutable());
    $em->persist($product);
    if (++$i % $batchSize === 0) {
        $em->flush();
        $em->clear(); // detach all; GC can reclaim the prior batch
    }
}
$em->flush();
$em->clear();
```

For read-only iteration add `->setHint(Query::HINT_READ_ONLY, true)` to skip change tracking entirely. Also disable the SQL logger in long CLI runs — it buffers every query in memory.

### 3. EntityManager closed after an exception — the stale EM trap

When Doctrine catches a database exception during flush (constraint violation, deadlock, connection error) it **closes the EntityManager**. `$em->isOpen()` returns `false` and every subsequent ORM call throws "EntityManagerInterface is closed". AI code that catches the exception and keeps using the same instance produces cryptic errors on the next request or the next batch chunk.

- **Fix:** inject `ManagerRegistry`, not `EntityManagerInterface`, in any service that has to survive a failed flush. `resetManager()` returns a fresh, open EM. Anything you were holding is detached — re-fetch it.

```php
// WRONG — continues using a closed EM after a flush exception
try {
    $em->flush();
} catch (\Throwable $e) {
    $logger->error('Flush failed', ['e' => $e]);
    // $em is closed — the next persist()/flush() throws
}

// RIGHT — reset so subsequent work gets a live instance
try {
    $em->flush();
} catch (\Throwable $e) {
    $logger->error('Flush failed', ['e' => $e]);
    if (!$em->isOpen()) {
        $em = $this->managerRegistry->resetManager(); // fresh, open EM
    }
    throw $e; // do not swallow
}
```

In a long-running `messenger:consume` worker this matters more than in a web request: one closed EM poisons every message after it until the worker restarts. Let the message fail and be retried rather than silently continuing with a broken EM.

### 4. Services are shared singletons — never store request state in them

Symfony's container creates services as **shared** by default — one instance for the process lifetime. Under FPM that is one request. Under FrankenPHP worker mode, Swoole, or RoadRunner the same instance handles many requests, and stored request state bleeds between users.

- **Fix first:** pass request context as method arguments, DTOs, or value objects. A service with no state has nothing to leak.
- **When state is unavoidable:** implement `Symfony\Contracts\Service\ResetInterface`. Symfony auto-tags such services `kernel.reset` and calls `reset()` between requests in worker mode. This is the current mechanism; `shared: false` is a blunter alternative that costs a new instance on every injection.
- **Symfony resets only what it knows about.** Static properties, function-level `static $x`, and class-level caches are never reset by `kernel.reset`. Those are runtime-tier concerns — see `mir-backend-php`. And `kernel.reset` only fires if the runner actually calls it: a hand-rolled RoadRunner or Swoole loop resets nothing.
- **The EntityManager is the service that bites hardest here.** Its identity map holds every entity the worker has touched, so under a worker runtime yesterday's tenant rows stay managed and memory climbs across requests; and a single failed `flush()` closes it for every *subsequent* request on that worker (§3), not just the one that failed. `messenger:consume` resets resettable services between messages unless you pass `--no-reset`; an HTTP worker runtime needs the equivalent clear-or-reset per request, plus `resetManager()` on the error path.
- **Symfony 8.1 escape hatch:** `FRANKENPHP_RESET_KERNEL=1` makes `FrankenPhpWorkerRunner` clone the application after each request so the next one starts from a freshly booted kernel. Use it when auditing every service is not realistic; it costs throughput.

```php
// WRONG — the service stores the authenticated user as a property
class OrderService {
    private User $currentUser;
    public function setUser(User $u): void { $this->currentUser = $u; }
    public function place(Cart $cart): Order { /* uses $this->currentUser */ }
}
// Under a worker runtime: request 1's user is still there for request 2

// RIGHT — pass it in; nothing is stored
class OrderService {
    public function place(Cart $cart, User $actor): Order { /* pure */ }
}

// If state is genuinely required, make it resettable
class ImportBuffer implements \Symfony\Contracts\Service\ResetInterface {
    private array $rows = [];
    public function reset(): void { $this->rows = []; }
}
```

### 5. Serializer groups — both directions, not just output

The Serializer serializes every accessible property by default. Returning a full `User` entity can expose `password`, `resetToken`, `stripeCustomerId`, and internal timestamps. AI reliably remembers the output side and forgets the input side.

- **Output (`normalizationContext`):** put `#[Groups]` on the properties you intend to expose and pass the group when serializing. `#[Ignore]` excludes a property from every group. `AbstractNormalizer::IGNORED_ATTRIBUTES` does it at call time.
- **Input (`denormalizationContext`) is the mass-assignment control.** A group on the write side is what stops a client setting `isAdmin` or `id`. Without it, any property the denormalizer can write, it will write.
- **Unknown properties are silently ignored by default.** `AbstractNormalizer::ALLOW_EXTRA_ATTRIBUTES` defaults to `true`, so a typo'd field is dropped with no error. Set `framework.serializer.default_context.allow_extra_attributes: false` to make it throw — it turns silent client bugs into 400s.
- **Never denormalize straight into a Doctrine entity.** Denormalize into an input DTO and copy the fields you accept. Symfony 8's `ObjectMapper` component (now stable) is the supported way to map DTO → entity.

```php
use Symfony\Component\Serializer\Attribute\Groups;
use Symfony\Component\Serializer\Attribute\Ignore;

class User {
    #[Groups(['user:read'])]
    public int $id;                   // read-only: not in any write group

    #[Groups(['user:read', 'user:write'])]
    public string $email;

    #[Ignore]
    public string $password;          // never serialized

    #[Groups(['admin:read'])]         // and NOT in user:write — clients cannot set it
    public bool $isAdmin;
}

$json = $serializer->serialize($user, 'json', ['groups' => ['user:read']]);
```

In API Platform, declare `normalizationContext` and `denormalizationContext` **per operation** on `#[ApiResource]`, and declare `operations:` explicitly — the default exposes the full CRUD set, DELETE included.

### 6. Messenger — `#[AsMessageHandler]`, idempotency, and a failure transport

`MessageHandlerInterface` and `MessageSubscriberInterface` were **removed in Symfony 7.0**. Any code or generated snippet still implementing them will not work. Use the attribute.

- **Idempotency:** a message can be delivered more than once under normal operation — a worker can process it successfully and crash before acknowledging, and the transport redelivers. Prefer handlers that are naturally idempotent (set an absolute value, not `decrement()`). Where the operation cannot be, guard on an idempotency key **derived from the business event**, stored with a unique constraint. A UUID generated at dispatch time is not an idempotency key; it changes on every dispatch of the same logical event.
- **Failure transport:** configure `framework.messenger.failure_transport` so exhausted messages land in a dead-letter queue instead of vanishing. Recover with `messenger:failed:show` and `messenger:failed:retry`.
- **Retry strategy:** set `retry_strategy` per transport (`max_retries`, `delay`, `multiplier`, `max_delay`, `jitter`). The default retries; it does not back off the way your downstream needs.
- **Flush explicitly at the end of a handler unless this bus has `doctrine_transaction` middleware** — that middleware wraps the handler in a transaction and flushes and commits on success, rolling back on failure. Read the bus's actual middleware stack before deciding; the two configurations need different handler code.
- **Dispatch after commit, and know that `flush()` is not commit.** With `doctrine_transaction` on the bus, `flush()` runs inside a transaction that has not committed yet, so "dispatch after the flush" still publishes a message for rows that may roll back. Use `DispatchAfterCurrentBusStamp` with `dispatch_after_current_bus` ordered **before** `doctrine_transaction`, or write an outbox row in the same transaction and dispatch from that. Only outside an explicit transaction does `flush()` commit on its own.

```php
use Symfony\Component\Messenger\Attribute\AsMessageHandler;

// WRONG — no idempotency guard, and MessageHandlerInterface no longer exists
#[AsMessageHandler]
final class ProcessPaymentHandler {
    public function __invoke(ProcessPayment $message): void {
        $this->paymentGateway->charge($message->orderId, $message->amount);
        // worker crashes after charge, before ack → redelivered → double charge
    }
}

// RIGHT — guard on a stable key, then flush
#[AsMessageHandler]
final class ProcessPaymentHandler {
    public function __invoke(ProcessPayment $message): void {
        if ($this->paymentRepo->existsByIdempotencyKey($message->idempotencyKey)) {
            return; // already processed; safe to ack
        }
        $payment = $this->paymentGateway->charge($message->orderId, $message->amount);
        $this->paymentRepo->saveWithKey($payment, $message->idempotencyKey);
        $this->em->flush();
    }
}
```

### 7. Validate input DTOs — and use the mapping attributes

Validating HTTP input against entities couples your API to your schema and lets an invalid payload reach the DB. Symfony's current answer is attribute-based request mapping, not manual `$validator->validate()` in the controller.

| Attribute | Source | Since |
|---|---|---|
| `#[MapQueryParameter]` | one query parameter | 6.3 |
| `#[MapQueryString]` | the whole query string → DTO | 6.3 |
| `#[MapRequestPayload]` | the request body → DTO | 6.3 |
| `#[MapUploadedFile]` | uploaded file(s) | 7.1 |

They deserialize **and validate** — constraint violations return 422 with a serialized `ConstraintViolationList`, malformed data returns 400, an unsupported format returns 415. Override with `validationGroups:` and `validationFailedStatusCode:`.

```php
use Symfony\Component\Validator\Constraints as Assert;
use Symfony\Component\HttpKernel\Attribute\MapRequestPayload;

final class CreateOrderInput {
    public function __construct(
        #[Assert\Positive]                             public int $productId,
        #[Assert\Range(min: 1, max: 1000)]             public int $quantity,
        #[Assert\Currency]                             public string $currency,
    ) {}
}

#[Route('/orders', methods: ['POST'])]
public function create(#[MapRequestPayload] CreateOrderInput $input): JsonResponse
{
    // Already validated. No manual $validator call, no entity touched yet.
}
```

**The empty-payload trap:** by default an empty body or empty query string short-circuits to `null` on a nullable parameter **without running the Serializer or the validator** — so `#[Assert\NotBlank]` never fires and a completely empty request passes. Symfony 8.1 adds `mapWhenEmpty` on `#[MapRequestPayload]` and `#[MapQueryString]` to force mapping (and therefore validation) on empty input. On 7.4, make the parameter non-nullable, or validate for the empty case explicitly. Symfony 8.1 also lets a DTO carry both scalar fields and uploaded files through `#[MapRequestPayload]`, which previously required splitting the controller argument.

### 8. `Request::get()` is gone in Symfony 8

`Request::get()` was deprecated in 7.4 and **removed in 8.0**. It checked `attributes`, then `query`, then `request` — so a query-string parameter silently overrode the POST body value you thought you were reading. That precedence is why it was removed, and it is a correctness bug even on 7.4 where it still works.

```php
// WRONG — removed in 8.0, and ambiguous before that
$id = $request->get('id');

// RIGHT — name the source explicitly
$id = $request->query->getInt('id');       // ?id=
$id = $request->request->getInt('id');     // form/POST body
$id = $request->attributes->get('id');     // route placeholder
```

Find every occurrence before the 8.0 upgrade: the PHPUnit bridge prints a deprecation summary at the end of the test run. `Request::setFormat()` no longer accepts a null `$format` either.

## Security

Symfony/Doctrine-specific and mechanical. php.ini, `unserialize`, SSRF fundamentals, path traversal, and Composer hardening live in `mir-backend-php`.

### Object-level authorization (IDOR / BOLA)

A valid token proves identity. `access_control` in `security.yaml` matches **paths and roles** — it cannot know whether *this* user owns *this* order. `#[IsGranted('ROLE_USER')]` is the same mistake in attribute form.

```php
// WRONG — any authenticated user can read any order
#[IsGranted('ROLE_USER')]
#[Route('/orders/{id}')]
public function show(Order $order): Response { /* ... */ }

// RIGHT — a Voter decides on the subject
#[IsGranted('ORDER_VIEW', subject: 'order')]
#[Route('/orders/{id}')]
public function show(Order $order): Response { /* ... */ }
```

- Write a `Voter` per resource type. `supports()` narrows to the attribute and subject class; `voteOnAttribute()` compares the subject's owner to `$token->getUser()`.
- In API Platform, set `security:` (and `securityPostDenormalize:` for writes) **per operation**. An operation with no `security:` is open to anyone the firewall let in.
- For multi-tenancy, add a Doctrine filter (`SQLFilter`) enabled per request, so the tenant predicate is in every query rather than in every repository method. Assert it in a test.
- Check before the write. Denormalizing into a managed entity and then authorizing has already changed the object.

### Mass assignment / overposting

Symfony has no `$fillable`. The allow-list mechanisms it does have:

| Mechanism | What it controls |
|---|---|
| `#[Groups]` on the **denormalization** context | which properties a client may write |
| `AbstractNormalizer::ALLOW_EXTRA_ATTRIBUTES => false` | reject unknown fields instead of ignoring them |
| an input DTO + `ObjectMapper` / explicit copy | the entity is never a denormalization target |
| API Platform `denormalizationContext` per operation | per-endpoint write allow-list |
| Form `allow_extra_fields` (default `false`) | Forms already reject unmapped fields — keep it false |

The failure is denormalizing a request body directly into a Doctrine entity with no write group. Every settable property, including `id`, `roles`, and `isVerified`, becomes client-controlled.

### Injection

- **DQL is not immune.** `EntityManager#find()`, `getReference()`, and `EntityRepository` finder methods are safe. Concatenating user input into DQL, QueryBuilder expressions, or native SQL is not. Use `setParameter()` — and note DQL positional parameters are numbered (`?1`, `?2`), not bare `?`.
- **Literals still let an attacker change meaning.** DQL quotes literals, but a valid DQL statement built from attacker-chosen literals is still attacker-controlled logic. The parser cannot detect it. That is your responsibility, not Doctrine's.
- **Identifiers cannot be parameterized.** Table names, column names, `ORDER BY` fields and direction must come from a hard-coded allow-list. This includes field names passed to the `Criteria` API on `PersistentCollection` and `EntityRepository` — the values are parameterized, the field names are not.
- **`AbstractPlatform::quoteIdentifier()` is not a sanitizer** — GHSA-76w8-mqx4-wjrf covers passing user input to it. Doctrine's own position is that `$connection->insert($table, $values)` does not escape the table name or the array keys, and that this part of the API cannot be secured. Never build either from a request.
- **Template injection:** never pass user input to `Twig\Environment::createTemplate()` or render a user-supplied template string. That is remote code execution.

```php
// WRONG — the sort field comes straight from the query string
$qb->orderBy('o.' . $request->query->get('sort'), 'ASC');

// RIGHT — allow-list the identifier, bind the value
$allowed = ['createdAt' => 'o.createdAt', 'total' => 'o.total'];
$field = $allowed[$request->query->get('sort')] ?? 'o.createdAt';
$qb->orderBy($field, 'ASC')
   ->andWhere('o.status = :status')->setParameter('status', $status);
```

### CSRF and SameSite

- Session-cookie auth needs CSRF. Stateless JWT/API-token auth in an `Authorization` header does not.
- **Symfony 7.2+ ships stateless CSRF** — a double-submit cookie plus header, validated without a session, so it works with HTTP caching. Configure in `config/packages/csrf.yaml`: `framework.form.csrf_protection.token_id`, and `framework.csrf_protection.stateless_token_ids: ['submit', 'authenticate', 'logout']`. `check_header: true` requires the header check. A token id not in `stateless_token_ids` falls back to the stateful manager, which still works — so a silent misconfiguration is possible.
- The cookie is `SameSite=Strict` and `__Host-` prefixed on HTTPS, and is cleared on the response so a token cannot be replayed.
- **It needs to know its own origin.** Behind a reverse proxy, configure `framework.trusted_proxies` and `trusted_headers` so `X-Forwarded-*` is honoured, or origin validation fails or is bypassable.
- The Stimulus controller from `symfony/stimulus-bundle` must actually run. A hidden input still holding the literal `csrf-token` placeholder means it did not, and every submit fails.

### CORS

Symfony has no built-in CORS layer — it is usually `nelmio/cors-bundle` or a listener someone wrote. Both fail the same way:

- `allow_credentials: true` with `allow_origin: ['*']` is rejected by browsers, so teams "fix" it by reflecting the request `Origin` header. Reflecting `Origin` with credentials means **every origin** is trusted. Use an explicit list, or a regex anchored at both ends (`^https://([a-z0-9-]+\.)?example\.com$` — an unanchored pattern matches `example.com.evil.tld`).
- CORS is enforced by browsers only. It is not authorization. Every endpoint must still authenticate and authorize on its own.

### Debug, profiler, and information disclosure

- `APP_ENV=prod`, `APP_DEBUG=0`. The debug exception page prints the environment, config, and stack frames.
- `symfony/web-profiler-bundle` and `symfony/debug-bundle` belong in `require-dev` only. Verify `/_profiler` and `/_wdt` return 404 in production.
- The fragment renderer signs URIs with `framework.secret`. A weak or leaked `APP_SECRET` makes `/_fragment` render arbitrary controllers. Rotate `APP_SECRET` if it ever appeared in a repo, an image, or a log.
- `CodeExtension::fileExcerpt()` in TwigBridge had an XSS (CVE-2026-45072). It renders source excerpts — another reason debug tooling must not reach production.

### Uploads and file handling

- Validate with `#[Assert\File(maxSize: ..., mimeTypes: [...])]` / `#[Assert\Image]`. The constraint sniffs the file; `UploadedFile::getClientMimeType()` and `getClientOriginalName()` are client-supplied strings.
- Never use `getClientOriginalName()` as the stored filename — generate one. Combine with the `realpath()` containment check in `mir-backend-php`.
- `#[MapUploadedFile]` gives you the constraint check as part of argument resolution instead of a separate branch.

### Deserialization and message payloads

- Never `unserialize()` client data (runtime tier). In Symfony, the equivalent is denormalizing into a class the client chose: always pass a hard-coded target type to `$serializer->deserialize($data, Order::class, 'json')`, never a type read from the payload.
- **Messenger transports carry serialized payloads.** A queue that an untrusted producer can write to is a deserialization entry point into your workers. Restrict who can publish, and prefer the JSON serializer over PHP native serialization for transports crossing a trust boundary.

### Current advisories on the default path

The July 2026 batch is fixed in **5.4.52 / 6.4.40 / 7.4.12 / 8.0.12** (and the corresponding 8.1 patch — check the release notes for your branch):

| Identifier | What | Affected |
|---|---|---|
| CVE-2026-46626, High | `symfony/runtime` — bypass of the CVE-2024-50340 fix. The old fix gated argv parsing on `empty($_GET)`; `parse_str()` and the web SAPI disagree on some inputs, so a crafted query string can leave `$_GET` empty while `$_SERVER['argv']` carries `--env` / `--no-debug`. An unauthenticated GET can flip `APP_ENV` and `APP_DEBUG`. | ≥5.4.46 <5.4.52, ≥6.4.14 <6.4.40, ≥7.1.7 <7.4.12, ≥8 <8.0.12 |
| CVE-2026-47212 | Twilio notifier webhooks: `TwilioRequestParser::doParse()` received the configured secret and **ignored the `X-Twilio-Signature` HMAC**, so unauthenticated POSTs could inject forged status payloads | <6.4.40, <7.4.12, <8.0.12 |
| CVE-2026-45754 | Lox24 notifier webhooks accepted a missing or invalid token | same |
| CVE-2026-45066, CVSS 6.1 | HtmlSanitizer: `allowLinkHosts()`/`allowMediaHosts()` bypass — `UrlSanitizer::parse()` follows RFC 3986 while browsers follow WHATWG, and `<area href>` was checked against the media policy instead of the link policy | ≥6.1.0-BETA1, fixed as above |
| CVE-2026-45753 | HtmlSanitizer URL sanitization in `action`, `formaction`, `poster`, `cite` | same |
| CVE-2026-45064 | HtmlSanitizer did not reject BiDi override characters | same |
| CVE-2026-45072 | XSS in TwigBridge `CodeExtension::fileExcerpt()` | same |
| CVE-2026-46636 (27 May 2026) | Twig sandbox: filter, tag, and function allow-list bypass when sandbox state changes between renders | see the advisory for the Twig version range |

**Precondition for CVE-2026-46626 / CVE-2024-50340: `register_argc_argv=On`.** It is on by default in the official PHP Docker images. Turning it off is the mitigation that works without upgrading, and no web application needs it.

The Symfony UX packages took a cluster of May–June 2026 advisories — path traversal in `ux-toolkit`, SVG XSS in `ux-icons`, and CSRF bypass, hydrator-HMAC and child-component XSS issues across `ux-live-component` and `ux-autocomplete`. If any `symfony/ux-*` package is installed, `composer audit --locked` is the check; do not hand-match version ranges.

## How this slots into the core pipeline

- **Gate 5 (Design):** fetch strategy per query (fetch joins vs. proxies, `Paginator` where a to-many join meets a limit); which work is Messenger-bound and what its idempotency key is; Serializer groups for **both** directions per endpoint; flush boundaries and the EM reset path on error; the Voter for every object-level operation.
- **Gate 6 (Implementation):** DQL fetch joins with `addSelect`; `flush()`+`clear()` in batch loops; `resetManager()` on the error path; `#[Groups]` on read and write; `#[AsMessageHandler]` with an idempotency guard, a `failure_transport`, and a `retry_strategy`; `#[MapRequestPayload]`/`#[MapQueryString]` on input DTOs; explicit request bags instead of `Request::get()`; `ResetInterface` on any stateful service.
- **Gate 7 (Review):** reliability-reviewer checks items 1–8. security-reviewer walks the Security section: a Voter on every object-level route, denormalization groups, identifier allow-lists in DQL/QueryBuilder, CORS origin handling, `APP_ENV`/`APP_DEBUG`/`APP_SECRET`, `register_argc_argv`, and the advisory table against `composer.lock`. migration-reviewer applies expand/contract — add nullable, backfill in a separate command, add NOT NULL and the index later — to every Doctrine migration on a populated table.

## Edit boundary (what belongs here vs. the core)

**This module holds ONLY Symfony/Doctrine library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability principle)? → **generic core** (`mir-backend`).
- True for every PHP framework on the Zend Engine (shared-nothing lifecycle, FPM sizing, worker-runtime state bleed at the runtime level, opcache/JIT, php.ini hardening, `unserialize`, SSRF fundamentals, Composer supply chain)? → **runtime tier** (`mir-backend-php`).
- A mechanical footgun of *this library* (Doctrine N+1 via lazy proxies, UoW `clear()` in batch, EM closed after exception, `ResetInterface`, Serializer groups both directions, `#[AsMessageHandler]` idempotency, `#[MapRequestPayload]`, `Request::get()` removal, Voters)? → **here**.
- A *different* PHP framework (Laravel, WordPress) → new `mir-backend-php-<framework>` module. A *different* runtime → its own tier. Never widen this one.
