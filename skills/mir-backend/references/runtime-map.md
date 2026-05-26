# Runtime selection map

Used at **Gate 0** to catch a **stack/workload mismatch** before any code — and to know which `mir-backend-<runtime>` tier and framework module to pull.

## How to use this at Gate 0
1. Identify the chosen (or implied) runtime + framework from the task or existing code.
2. Check the workload against **"When NOT to use"** below. If the chosen stack lands in its own anti-pattern column, **stop and flag it** — that's a runtime-level defect no amount of correct code fixes. Example: "This is a microsecond-latency matching path on Python — CPython's GIL/async model is a poor fit; Go or Rust suits this. Proceed on Python anyway, or reconsider?"
3. Load the matching `mir-backend-<runtime>` tier (runtime footguns) and the framework module (library mechanics).

A mismatch isn't an automatic blocker — the team may have valid reasons (existing expertise, ecosystem). But it must be a **conscious, surfaced** choice, recorded in the Assumption Ledger — never a silent default.

## The map

| Runtime / Platform | Slug | Language | Top frameworks | Use this stack when… | Do NOT use when… | Typically used by |
|---|---|---|---|---|---|---|
| Node.js (V8) | `node` | JS / TS | Express, Fastify, NestJS | I/O-heavy apps, real-time (chat, notification streams), full-stack teams sharing JS/TS | Heavily CPU-bound compute, data science, heavy ML workflows | Startups, mid-market SaaS, full-stack engineers |
| JVM | `jvm` | Java / Kotlin | Spring Boot, Quarkus, Micronaut | Massive scale, strictly transactional banking/finance, heavy enterprise where backward-compat is mandatory | Fast prototyping, tiny scripts, serverless with zero cold-start tolerance | Enterprise, Fortune 500, FinTech |
| .NET CLR | `dotnet` | C# / F# | ASP.NET Core, Minimal APIs | High-performance enterprise web, corporate clouds, deep Microsoft Azure integration | Open-source community experiments where .NET familiarity is zero | Enterprise software, enterprise cloud architects |
| CPython / PyPy | `python` | Python | FastAPI, Django, Flask | AI/ML model inference, big-data pipelines, data-science APIs, rapid prototype backends | Ultra-low latency / high-concurrency microsecond request speeds | AI/ML engineers, data scientists, AI startups |
| Zend (PHP) | `php` | PHP | Laravel, Symfony | Monolithic web/SaaS, traditional e-commerce, content platforms, solo-dev bootstrapped projects | Distributed microservice meshes, highly real-time distributed calc | Freelancers, indie hackers, digital agencies |
| Go runtime (compiled) | `go` | Go | Gin, Fiber, Echo | Cloud-native microservices, containerized infra (Docker/K8s), high-concurrency network routing | Rich web apps needing built-in admin panel, auto migrations, out-of-box UI tooling | DevOps & platform engineers, scaled tech cos |
| Ruby VM (YARV) | `ruby` | Ruby | Ruby on Rails | Fast-to-market MVPs, DB-driven web apps where productivity > execution speed | High-frequency data pipelines, intense processing | Early-stage startups, product-driven shops |
| Rust (compiled / LLVM) | `rust` | Rust | Axum, Actix-web | HFT, cryptographic backends, mission-critical safety services needing 100% memory efficiency | Junior-heavy teams, strict short delivery deadlines | Systems programmers, crypto/Web3, core infra |
| BEAM (Erlang) | `beam` | Elixir / Erlang | Phoenix | Massively concurrent real-time (telecoms, gaming lobbies, live-bidding) needing fault-tolerant uptime | Basic CRUD forms where functional programming over-engineers it | Real-time engineers, scale-systems architects |

## Naming reminder
- Runtime tier skill: `mir-backend-<slug>` (e.g. `mir-backend-python`, `mir-backend-node`).
- Framework module: `mir-backend-<slug>-<framework>` (e.g. `mir-backend-python-fastapi`, `mir-backend-node-express`).
- Currently shipping — **all 9 runtimes + their framework modules**:
  - `mir-backend-python` → `-fastapi`, `-django`, `-flask`
  - `mir-backend-node` → `-express`, `-fastify`, `-nestjs`
  - `mir-backend-jvm` → `-spring`, `-quarkus`, `-micronaut`
  - `mir-backend-dotnet` → `-aspnetcore`
  - `mir-backend-go` → `-gin`, `-fiber`, `-echo`
  - `mir-backend-php` → `-laravel`, `-symfony`
  - `mir-backend-ruby` → `-rails`
  - `mir-backend-rust` → `-axum`, `-actix`
  - `mir-backend-beam` → `-phoenix`
  - Add more frameworks/runtimes via the recipe in `EXTENDING.md`.
