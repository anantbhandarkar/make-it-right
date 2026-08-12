# Runtime selection map

Used at **Gate 0** to catch a **stack/workload mismatch** before any code — and to know which `mir-backend-<runtime>` tier and framework module to pull.

## How to use this at Gate 0
1. Identify the chosen (or implied) runtime + framework from the task or existing code.
2. Check the workload against **"Do NOT use when…"** below. If the chosen stack lands in its own anti-pattern column, **stop and flag it** — that's a runtime-level defect no amount of correct code fixes. Example: "This is a microsecond-latency matching path on Python — CPython's GIL/async model is a poor fit; Go or Rust suits this. Proceed on Python anyway, or reconsider?"
3. Check the **version floor** table. A runtime line past end of life gets no security patches; that is a Gate 4 risk row with an owner and a date, not a footnote.
4. Load the matching `mir-backend-<runtime>` tier (runtime footguns) and the framework module (library mechanics).

A mismatch isn't an automatic blocker — the team may have valid reasons (existing expertise, ecosystem). But it must be a **conscious, surfaced** choice, recorded in the Assumption Ledger — never a silent default.

## The map

| Runtime / Platform | Slug | Language | Top frameworks | Use this stack when… | Do NOT use when… | Typically used by |
|---|---|---|---|---|---|---|
| Node.js (V8) | `node` | JS / TS | Express, Fastify, NestJS | I/O-heavy apps, real-time (chat, notification streams), full-stack teams sharing JS/TS | Heavily CPU-bound compute, data science, heavy ML workflows | Startups, mid-market SaaS, full-stack engineers |
| Bun (JavaScriptCore) | `bun` | JS / TS | Hono, Elysia, `Bun.serve` | I/O-heavy TS services where you want one toolchain — runtime, package manager, test runner, bundler — instead of five; fast installs and test runs in CI; a single self-contained binary via `bun build --compile`; built-in SQLite/Postgres/Redis clients | Your production path depends on a native (N-API) addon, or on a Node API Bun stubs rather than implements — the gaps present as silent no-ops (`module.register` exists and does nothing) and native modules that `require()` cleanly then abort the process on first use. Also: you need a mature profiler/APM/debugger ecosystem; you need a published LTS and EOL calendar to point at in a compliance review (Bun ships one rolling line, no LTS); the work is CPU-bound (same single-threaded story as Node) | TS-first startups, CLI and tooling authors, teams consolidating a JS toolchain |
| JVM | `jvm` | Java / Kotlin | Spring Boot, Quarkus, Micronaut | Massive scale, strictly transactional banking/finance, heavy enterprise where backward-compat is mandatory | Fast prototyping, tiny scripts, serverless with zero cold-start tolerance | Enterprise, Fortune 500, FinTech |
| .NET CLR | `dotnet` | C# / F# | ASP.NET Core, Minimal APIs | High-performance enterprise web, corporate clouds, deep Microsoft Azure integration | Open-source community experiments where .NET familiarity is zero | Enterprise software, enterprise cloud architects |
| CPython / PyPy | `python` | Python | FastAPI, Django, Flask | AI/ML model inference, big-data pipelines, data-science APIs, rapid prototype backends | Ultra-low latency / high-concurrency microsecond request speeds | AI/ML engineers, data scientists, AI startups |
| Zend (PHP) | `php` | PHP | Laravel, Symfony | Monolithic web/SaaS, traditional e-commerce, content platforms, solo-dev bootstrapped projects | Distributed microservice meshes, highly real-time distributed calc | Freelancers, indie hackers, digital agencies |
| Go runtime (compiled) | `go` | Go | Gin, Fiber, Echo | Cloud-native microservices, containerized infra (Docker/K8s), high-concurrency network routing | Rich web apps needing built-in admin panel, auto migrations, out-of-box UI tooling | DevOps & platform engineers, scaled tech cos |
| Ruby VM (YARV) | `ruby` | Ruby | Ruby on Rails | Fast-to-market MVPs, DB-driven web apps where productivity > execution speed | High-frequency data pipelines, intense processing | Early-stage startups, product-driven shops |
| Rust (compiled / LLVM) | `rust` | Rust | Axum, Actix-web | HFT, cryptographic backends, mission-critical safety services needing 100% memory efficiency | Junior-heavy teams, strict short delivery deadlines | Systems programmers, crypto/Web3, core infra |
| BEAM (Erlang) | `beam` | Elixir / Erlang | Phoenix | Massively concurrent real-time (telecoms, gaming lobbies, live-bidding) needing fault-tolerant uptime | Basic CRUD forms where functional programming over-engineers it | Real-time engineers, scale-systems architects |

## Version floors (verified 13 Aug 2026)

Checked this date against endoflife.date's release data for each runtime. The runtime tier carries the per-release detail and the behaviour changes; this table is the Gate 0 sanity check. **Re-verify before quoting a date** — these move every quarter.

| Runtime | Target for a new service | Lowest defensible floor | Do not ship on |
|---|---|---|---|
| Node.js | 24 LTS (24.19.0) | 22 LTS (22.23.2) — maintenance to 30 Apr 2027 | 20 (ended 30 Apr 2026), 18, and any odd-numbered line (25 ended 1 Jun 2026; odd lines never become LTS). 26 (26.7.0) is Current and becomes LTS in Oct 2026 — not the production default yet |
| Bun | 1.3.x (1.3.14, May 2026) | the same. One rolling line, no LTS, no published EOL calendar — upgrading is your only patch path | below 1.2, which predates the text `bun.lock` |
| CPython | 3.14 (3.14.7) or 3.13 (3.13.15) | 3.11 (3.11.15) — security-only to Oct 2027, the last line with real runway | 3.9 and below (3.9 ended 31 Oct 2025). **3.10 (3.10.20) still gets fixes but ends 31 Oct 2026** — that is inside most project timelines; schedule the upgrade at Gate 4 |
| JVM | Java 25 LTS (Temurin 25.0.4+7, to Sep 2031) | Java 21 LTS (Temurin 21.0.12+8, to Dec 2029). Adoptium still patches 17 (to Oct 2027), 11 and 8; other vendors' windows differ, check yours | Java 26 — non-LTS, stops Sep 2026, so no production SLA. Non-LTS lines get roughly six months and then nothing |
| .NET | 10 LTS (10.0.11, to 14 Nov 2028) | there isn't one with runway: 8 LTS (8.0.30) and 9 STS (9.0.19) **both end 10 Nov 2026**, the same day | 7 and below. A new service targeting `net8.0` or `net9.0` today has under three months of support left — say that at Gate 0, don't accept the TFM silently |
| Go | 1.26 (1.26.5) | 1.25 (1.25.12) | 1.24 and below. Go patches only the two newest majors, so the floor moves every six months |
| Rust | stable 1.97.1 (Jul 2026), edition 2024 | whatever `rust-version` (MSRV) pins in `Cargo.toml` — check it against your CI image, not against the newest release | there is no LTS and no back-porting: only the current stable gets fixes, and it moves every six weeks. Framework MSRVs already exceed some CI images |
| Ruby | 4.0 (4.0.6) | 3.3 (3.3.12), to 31 Mar 2027 | 3.2 ended 31 Mar 2026; 3.1 and below. 3.4 (3.4.10) runs to Mar 2028 and is also fine |
| PHP | 8.5 (8.5.9) or 8.4 (8.4.24) | 8.3 (8.3.33) — security-only, but to 31 Dec 2027 | 8.1 and below. **8.2 (8.2.33) still gets security fixes but ends 31 Dec 2026** |
| BEAM | Elixir 1.20.3 on OTP 29.0.5 | OTP 27.3.4.16, to May 2027 | OTP 26 ended May 2026. Elixir has no LTS either — each release supports a rolling window of recent OTP majors, so the two versions have to move together |

## Naming reminder
- Runtime tier skill: `mir-backend-<slug>` (e.g. `mir-backend-python`, `mir-backend-node`).
- Framework module: `mir-backend-<slug>-<framework>` (e.g. `mir-backend-python-fastapi`, `mir-backend-node-express`).
- Currently shipping — **all 10 runtimes + their framework modules**:
  - `mir-backend-python` → `-fastapi`, `-django`, `-flask`
  - `mir-backend-node` → `-express`, `-fastify`, `-nestjs`
  - `mir-backend-bun` → `-hono`
  - `mir-backend-jvm` → `-spring`, `-quarkus`, `-micronaut`
  - `mir-backend-dotnet` → `-aspnetcore`
  - `mir-backend-go` → `-gin`, `-fiber`, `-echo`
  - `mir-backend-php` → `-laravel`, `-symfony`
  - `mir-backend-ruby` → `-rails`
  - `mir-backend-rust` → `-axum`, `-actix`
  - `mir-backend-beam` → `-phoenix`
  - Add more frameworks/runtimes via the recipe in `EXTENDING.md`.
- **Bun and Node are different tiers, not interchangeable.** Load `mir-backend-bun` when Bun is the production runtime — or when a Node-deployed project uses `bun install` / `bun test` in CI, where only its lockfile, install-script, and test-runner sections apply. Load `mir-backend-node` otherwise; its advice on lockfiles, install scripts, and the test runner is wrong for Bun, and Bun's is wrong for npm/pnpm/yarn.
