---
name: mir-backend-ruby
description: "Make It Right (Ruby runtime tier). YARV/MRI Ruby 4.0 runtime reliability footguns shared across EVERY Ruby backend framework (Rails, Sinatra, Hanami, Sidekiq/Solid Queue workers) — distinct from the generic backend gates and from any one framework's mechanics. Covers: the GVL (threads give no CPU parallelism, like Python's GIL), the reworked Ractor API in Ruby 4.0, YJIT/ZJIT enablement, Puma's forked-worker + thread model, fork-safety of DB/Redis connections, copy-on-write memory and per-worker bloat, background job hygiene (idempotency, retries), GC/string-literal pressure, and the Rack/Puma/Bundler security layer every Ruby web app inherits. TRIGGER when the backend runtime is Ruby — sits between mir-backend (generic) and the framework module (e.g. mir-backend-ruby-rails). SKIP for Node/JVM/Go/Rust/.NET/Python/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for Rails/ActiveRecord library mechanics — N+1, strong parameters, callbacks, migrations, Active Storage — which belong to mir-backend-ruby-rails, not here."
trigger: /mir-backend-ruby
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-ruby · Make It Right (Ruby runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-ruby-rails`) knows the **library's mechanics**. This tier owns what's true for **all Ruby backends because they run on YARV (MRI) and speak Rack** — the concurrency model, the process model, and the HTTP/dependency layer that Rails, Sinatra, Hanami, and every job worker inherit.

**Runtime assumed:** MRI Ruby 4.0 under Puma. Current stable is **Ruby 4.0.6**; 3.4.x, 3.3.x and 3.2.x are still maintained, 3.1 is EOL (verified against ruby-lang.org on 13 Aug 2026). Notes hold for JRuby except where GVL specifics differ (JRuby has no GVL; true thread parallelism is available). Load order: `mir-backend` → `mir-backend-ruby` → `<framework module>`.

## What changed in Ruby 4.0 (check this before trusting older Ruby advice)

Ruby 4.0.0 shipped 25 Dec 2025. It is the release that was planned and written about as "Ruby 3.5" — blog posts and AI training data referring to 3.5 mean this.

| Change | What breaks | What to write instead |
|---|---|---|
| `Ractor.yield`, `Ractor#take`, `#close_incoming`, `#close_outgoing` **removed** | Every Ractor pipeline written against Ruby 3.x raises `NoMethodError` | `Ractor::Port` for message passing; `Ractor#join` / `#value` to wait for termination |
| `--rjit` **removed** | Boot flags / Dockerfiles carrying `--rjit` fail to start | Drop the flag; use YJIT |
| `Kernel#open` / `IO` with a leading `\|` **removed** | Code that relied on `open("\|cmd")` to spawn a process | `IO.popen` explicitly (and see Security — this removal kills one command-injection path, not all of them) |
| `ObjectSpace._id2ref` **removed** | Object-id-keyed caches and some debug tooling | Hold real references, or `ObjectSpace::WeakMap` |
| `cgi` stdlib **removed** (only `cgi/escape` remains) | Legacy code calling `CGI.parse`, `CGI::Cookie` | `Rack::Utils.parse_nested_query`, `CGI.escapeHTML` (still present via `cgi/escape`) |
| `Set` is now a core C class | Subclasses that poked at the internal `@hash` ivar | Use the public API; `Set::SubclassCompatible` is the escape hatch |
| Bundler/RubyGems 4 (4.0.3 ships with Ruby 4.0.0) | `Gemfile.lock` gains a `CHECKSUMS` section and re-indents 3→2 spaces; lockfile-parsing tooling and some deploy paths break | Regenerate the lockfile deliberately, commit it, and pin CI to Bundler ≥ 4 — see Security |

## The YARV footguns AI walks into (framework-agnostic)

### 1. The GVL — threads are NOT CPU parallelism

Ruby's Global VM Lock (GVL, formerly GIL) allows only one thread to execute Ruby bytecode at a time. **CPU-bound work does not run in parallel across threads** — multiple threads serialize on the GVL and you pay context-switch overhead on top. This is still true in Ruby 4.0; no M:N scheduler and no GVL removal landed.

- **CPU-bound** (image processing, crypto, heavy computation): use multiple **processes** (Puma workers), a C extension that releases the GVL (`bcrypt`, `openssl`, libvips-backed image work release it internally), or offload to a background worker. Adding more threads will not help — it will make things worse.
- **I/O-bound** (DB queries, HTTP calls, disk reads): threads are fine — the GVL releases during blocking I/O syscalls, so other threads run while one waits.
- This is the runtime-level reason the runtime-map says "SKIP Ruby for high-frequency data pipelines or intense processing."

```ruby
# WRONG — adding threads to speed up CPU-bound work; GVL serializes them
threads = data.map { |chunk| Thread.new { heavy_cpu_transform(chunk) } }
threads.each(&:join)

# RIGHT — for CPU-bound parallelism, use processes.
# Do NOT hand-roll this with bare `fork`: in the parent it returns a PID, not the
# block's value, so results are lost, and an unwaited child becomes a zombie.
# Use a pool that marshals results back and reaps (gem "parallel")…
results = Parallel.map(data.each_slice(chunk_size).to_a, in_processes: 4) { |s| compute(s) }

# …or push the work to a job runner — Sidekiq, Solid Queue, GoodJob.
```

### 2. Ractors give real parallelism — and the Ruby 3.x Ractor API no longer exists

Ractors run Ruby code in parallel because each has its own GVL. Ruby 4.0 reworked them substantially (lock-free frozen-string sets, per-ractor counters) but **they are still marked experimental** — every `Ractor.new` prints a warning, and most gems are not ractor-safe. Do not put a Ractor on a request path without a load test.

The API change is a hard break, not a deprecation:

```ruby
# WRONG on Ruby 4.0 — NoMethodError; these were removed
r = Ractor.new { Ractor.yield(compute) }
r.take

# RIGHT — Ractor::Port, or #value for a single result
port = Ractor::Port.new
Ractor.new(port) { |p| p << compute }
port.receive

r = Ractor.new { compute }
r.value          # waits for termination and returns the result
r.join           # waits without taking a value
```

- Only the ractor that created a port may read from it; any ractor holding the port may write. `Ractor.select` now accepts ports as well as ractors.
- Objects crossing a ractor boundary must be shareable (frozen and deeply immutable) or they are copied. `Ractor.shareable_proc` / `Ractor.shareable_lambda` exist for procs.
- If your workload is CPU-bound and you were about to reach for Ractors: processes are still the boring, correct answer.

### 3. YJIT is not on by default — and Rails turns it on for you

Ruby ships YJIT compiled in but **not enabled at VM start**. ZJIT, new in 4.0, is also compiled in and also off; upstream says it is faster than the interpreter but still slower than YJIT and should not be deployed to production yet.

| Goal | Do this |
|---|---|
| Rails app | Nothing. Rails ≥ 7.2 sets `config.yjit = true` under `load_defaults` and calls `RubyVM::YJIT.enable` **after boot**, so boot-only code is never compiled |
| Non-Rails Ruby process (plain Rack app, standalone worker, CLI) | Call `RubyVM::YJIT.enable` yourself after initialization |
| Anything | Do **not** set `RUBY_YJIT_ENABLE=1` or `--yjit` on a Rails process — it compiles initialization code that runs once and inflates memory |
| Experiment with ZJIT | `--zjit` / `RUBY_ZJIT_ENABLE` / `RubyVM::ZJIT.enable`. Build needs Rust 1.85+. Not production |

- YJIT costs memory per worker. The code-region cap is `--yjit-mem-size` (default 128 MiB). Multiply by Puma worker count before sizing a container.
- Verify it is actually on: `RubyVM::YJIT.enabled?`. A binary built without Rust has no YJIT at all — `ruby -v` will not show `+YJIT`.
- Measure with `RubyVM::YJIT.runtime_stats`. In Ruby 4.0 the `ratio_in_yjit` metric requires a build configured with `--enable-yjit=stats`.

### 4. Puma's process × thread model — pool sizing must match DB connections

Puma = **N forked worker processes** × **M threads per worker**. Each thread can hold one DB connection at a time. If `RAILS_MAX_THREADS` (thread count) does not equal the database connection pool `pool` setting, threads starve waiting for a connection at load spikes.

```yaml
# config/database.yml — pool must be at least RAILS_MAX_THREADS (default 5)
production:
  pool: <%= ENV.fetch("RAILS_MAX_THREADS") { 5 } %>
```

- Total DB connections consumed = `workers × threads × databases`, **per host**. Compare that number against Postgres `max_connections` (default 100) or your PgBouncer pool before deploying.
- The pool is per process and per database. An app with four database entries (Rails 8 generates `primary`, `cache`, `queue`, `cable`) opens four pools in every Puma worker and every job process. AI copies the same `RAILS_MAX_THREADS` value into all four and blows past `max_connections` before serving a request. Size the non-primary pools explicitly for their real concurrency.
- AI commonly generates a pool much smaller than thread count (e.g. `pool: 2` with 5 threads), causing `ActiveRecord::ConnectionTimeoutError` under any real load.
- Puma's current lines are **8.0.2** and **7.2.1** (both 27 May 2026). Older Puma is a security problem, not just a stale dependency — see Security.

### 5. Fork-safety — connections created before fork are invalid in children

When Puma forks worker processes, any DB connection, Redis socket, or file handle opened **before the fork** is shared at the OS level. The child process ends up with a corrupted or multiplexed connection. This causes silent data corruption, `PG::UnableToSend`, or deadlocked sockets.

**Never open DB/Redis connections at boot time before forking.** Initialize them lazily or reconnect in the Puma hook:

```ruby
# config/puma.rb
on_worker_boot do
  ActiveRecord::Base.establish_connection   # reconnect DB pool in child
  # If using Redis directly:
  # $redis = Redis.new(url: ENV["REDIS_URL"])
end
```

- The same applies to any persistent socket: Elasticsearch clients, gRPC stubs, SMTP connections, custom TCP sockets. Audit every initializer that opens a connection — move connection establishment to `on_worker_boot` or lazy first-use.
- AI routinely writes `$redis = Redis.new(...)` in `config/initializers/redis.rb` without an `on_worker_boot` reconnect, producing a race condition that only surfaces intermittently under load.

### 6. Memory: copy-on-write and per-worker bloat

Puma's forked workers share memory pages read-only (copy-on-write, CoW) until a page is modified. Once a worker writes to a page — mutating a shared object, updating a class variable, running the GC — that page is copied into the worker's own address space. Memory use grows per worker over time.

- **Don't mutate shared objects after boot.** Memoize with `||=` only on thread-local or request-scoped objects, not on class-level constants or module variables that will be written during requests.
- **Frozen string literals** reduce object churn and help CoW; add `# frozen_string_literal: true` to every file.
- **Memory bloat mitigation:** use `jemalloc` (link Puma against it with `LD_PRELOAD`) to reduce fragmentation. Use `puma-worker-killer` to restart workers that exceed a memory threshold, preventing unbounded bloat. Budget for YJIT's per-worker code memory on top (§3).

```ruby
# Gemfile — memory management
gem "puma-worker-killer"

# config/puma.rb — NOT an initializer. In clustered mode the killer has to run in
# the master (it restarts workers), and a thread started before fork is not
# inherited by the children. before_fork is the supported hook.
before_fork do
  PumaWorkerKiller.config do |config|
    config.ram           = 1024  # MB — restart worker above this
    config.frequency     = 5     # seconds between checks
    config.percent_usage = 0.98
  end
  PumaWorkerKiller.start
end
```

### 7. Background jobs — async/durable work belongs in a worker, not the request thread

A request thread lives for the duration of one HTTP request. Any work that must survive a deploy, be retried on failure, or take longer than ~100ms should run in a background worker (Solid Queue, Sidekiq, GoodJob). AI commonly writes inline work in the controller that should be a job.

- **Idempotency is mandatory.** Workers retry on failure — a job may run more than once. Guard side effects with a unique key, a DB uniqueness constraint, or an idempotency check before executing.

```ruby
class SendReceiptJob < ApplicationJob
  queue_as :default

  def perform(order_id)
    order = Order.find(order_id)
    return if order.receipt_sent?          # collapses the common retry
    ReceiptMailer.send_receipt(order).deliver_now
    order.update!(receipt_sent: true)
  end
end
```

A check-then-flag guard closes the common case, not the real one: a crash between `deliver_now` and `update!` sends the receipt twice. For anything that costs money or is externally visible, the guard has to be the side effect's own dedup key — a provider idempotency key on the charge, a `Idempotency-Key` header on the send, or a unique index on an outbox row written in the same transaction as the state change.

- **Prefer IDs over ActiveRecord objects in job arguments.** Get the mechanism right, because the usual explanation is wrong: Active Job does **not** serialize the model's attributes. A persisted record is serialized as a **GlobalID** URI (`gid://app/Order/42`) and re-fetched at execution time. What actually bites is the failure mode — if the row is gone by the time the worker runs, deserialization raises `ActiveJob::DeserializationError` before `perform` is entered, so you cannot handle the missing record yourself. An explicit id lets you decide.

```ruby
# WORKS, but a deleted row raises ActiveJob::DeserializationError before perform
SendReceiptJob.perform_later(order)

# BETTER — you control the not-found path, and the payload is backend-neutral
SendReceiptJob.perform_later(order.id)
```

- Job processes fork and thread just like Puma. Everything in §4, §5 and §6 applies to them — including their own DB pool per database.

### 8. GC pressure and string literals

MRI's GC is stop-the-world. Object allocation spikes cause GC pauses that show up as p99 latency spikes. Ruby 4.0 grows GC heap pools independently per size class, which lowers steady-state memory, but does not change the shape of an allocation-heavy request.

- Add `# frozen_string_literal: true` to every file. String interpolation still allocates; repeated identical string literals do not.
- **Do not claim Ruby enforces this yet.** Since Ruby 3.4 a literal in a file *without* the magic comment is "chilled": mutating it still works and emits a deprecation warning — and that warning is **off unless deprecation warnings are enabled** (`-W:deprecated` or `Warning[:deprecated] = true`; minitest turns them on, RSpec does not). Frozen-by-default had not shipped as of Ruby 4.0, and the core team has not committed to a version. Write the magic comment; don't rely on the runtime to catch you.
- Avoid building large arrays/hashes in tight loops just to discard them. Prefer `each` over `map` when you don't use the result.
- Profile with `rack-mini-profiler` + `memory_profiler` before assuming GC is the bottleneck — but frozen literals cost nothing and are always worth doing.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** state the concurrency model (Puma workers + threads, job runner, inline). A mismatch — threads for CPU-bound work, Ractors on a request path, or inline code for durable work — is a runtime-level design defect. Flag it.
- **Gate 6 (implementation):** add `on_worker_boot` reconnects; size every pool against `workers × threads × databases`; guard job idempotency; frozen string literals.
- **Gate 7 (review):** the reliability-reviewer additionally checks items 1–8 here; the security-reviewer checks the section below for any Ruby service.

## Security

Runtime- and Rack-level mechanics. Rails/ActiveRecord-specific controls (strong parameters, IDOR scoping, CSRF config, Active Storage) live in `mir-backend-ruby-rails`.

### Deserialization of untrusted input

- **`Marshal.load` on anything an attacker can influence is remote code execution.** There is no safe mode and no allow-list. Never Marshal a session, a cache entry crossing a trust boundary, a queue payload from another system, or an uploaded file. Use JSON or MessagePack.
- **`CVE-2026-41316` (ERB, disclosed 21 Apr 2026, CVSS 8.1)** makes this concrete: `ERB#def_method`, `#def_module` and `#def_class` skipped the `@_init` guard that `ERB#result` had, so `erb` + `activesupport` — i.e. every Rails app — form a universal `Marshal.load` RCE gadget chain. Fixed in erb **4.0.3.1 / 4.0.4.1 / 6.0.1.1 / 6.0.4**. Prior Ruby 3.4 / RubyGems 3.6 hardening does not cover it; you must bump the gem.
- `YAML.load` is safe by default on currently maintained Rubies (Psych 4+ restricts classes). The dangerous call is **`YAML.unsafe_load`** — and `Psych.load(..., permitted_classes: [...])` when someone widens the list to `Object`.

### Injection

- **Command:** `system("convert #{path}")`, backticks, `IO.popen("cmd #{arg}")` and `%x{}` all go through a shell when given one string. Pass an argument array instead — `system("convert", path)`, `Open3.capture3("convert", path)` — which never invokes a shell. Ruby 4.0 removed `Kernel#open("|cmd")`, so that one vector is gone; the rest are not.
- **Template:** `ERB.new(user_input).result(binding)` is arbitrary code execution, full stop. Never build a template from request data.
- **Header/response splitting:** don't interpolate user input into header values; Rack rejects `\n` in most paths but not every custom middleware does.
- **Regex (ReDoS):** set `Regexp.timeout` process-wide. Ruby's linear-time matcher covers many but not all patterns, and back-reference/look-around patterns still blow up. Never build a `Regexp` from user input without `Regexp.escape`.

### SSRF from user-supplied URLs

`URI.open` (open-uri), `Net::HTTP` and every HTTP client will happily fetch `http://169.254.169.254/latest/meta-data/` (AWS IMDS), `http://metadata.google.internal/`, `http://127.0.0.1:6379/`, and `file:///etc/passwd` via `URI.open`.

- Resolve the hostname yourself, check the **resolved IP** against a deny-list of private/link-local ranges, then connect to that IP with the `Host` header set. Checking the hostname string is not enough — DNS can rebind between check and connect.
- Disable redirects, or re-validate after every hop.
- Prefer an allow-list of exact hosts over any deny-list.
- On AWS, require IMDSv2 so a bare GET cannot read credentials.

### Secret and PII leakage

- Exception messages print object `inspect`, which prints instance variables — an exception inside an HTTP client can put an API key in your error tracker. Define `inspect` on credential-carrying objects.
- Never log `ENV`, a full request env hash, or a raw job payload. Rack's env contains cookies and `Authorization`.
- Compare tokens with `ActiveSupport::SecurityUtils.secure_compare` or `OpenSSL.secure_compare`, never `==` — `==` returns early and leaks length and prefix through timing.

### Path traversal in file serving and uploads

- Never join request data to a base path and serve it: `File.read(base.join(params[:name]))` accepts `../../`. Expand first and verify containment — `File.expand_path(name, base).start_with?(base + "/")` — after `File.expand_path`, not before.
- Uploaded filenames are attacker-controlled. Store a generated name; keep the original only as a display label. Never let an uploaded name choose the path or the extension used to pick a handler.

### Rack — the layer under every Ruby web framework

Current Rack is **3.2.6** (1 Apr 2026), with 3.1.21 and 2.2.23 as maintained backports. All of these are exploitable on a default path if the named component is in your stack:

| Advisory | Component | What it does | Fixed in |
|---|---|---|---|
| CVE-2026-22860 | Rack path handling | Path traversal — containment was checked with a string prefix match | 2.2.22 / 3.1.20 / 3.2.5 |
| CVE-2026-25500 | `Rack::Directory` | Stored XSS — a file whose basename starts with `javascript:` is emitted into an `href` unescaped | 2.2.22 / 3.1.20 / 3.2.5 |
| CVE-2026-34829 | `Rack::Multipart::Parser` | Unbounded disk write — without `CONTENT_LENGTH` (chunked upload) no size limit is applied and parts stream straight to temp files | 2.2.23 / 3.1.21 / 3.2.6 |
| CVE-2026-34826 | `Rack::Utils.get_byte_ranges` | Range-header DoS — the *count* of ranges was never capped (`0-0,0-0,...`) | 2.2.23 / 3.1.21 / 3.2.6 |
| CVE-2026-34830 | `Rack::Sendfile#map_accel_path` | The `X-Accel-Mapping` **request** header is interpolated into a regex, letting a client steer `X-Accel-Redirect` and make nginx serve internal files | 2.2.23 / 3.1.21 / 3.2.6 |

Do not mount `Rack::Directory` on anything user-writable. Strip `X-Accel-Mapping` at the proxy. Cap upload size in the proxy as well as the app — the app-level limit was the thing that failed in CVE-2026-34829.

**`rack-session` CVE-2026-39324 (CVSS 9.3, public PoC).** `Rack::Session::Cookie` configured with the plural `secrets:` option fell back to the default coder when decryption failed, so an attacker could forge a session with no secret at all — and the default coder is Marshal, so that is also a deserialization path. Affects rack-session ≥ 2.0.0 < **2.1.2**. **Rails is not affected** (it uses `ActionDispatch::Session::CookieStore`); Sinatra/Hanami/plain-Rack apps are.

### Puma

Current: **8.0.2** and **7.2.1** (27 May 2026). Two 2026 advisories, both in PROXY protocol v1 handling and both reachable only if you enabled it with `set_remote_address proxy_protocol: :v1`:

- **CVE-2026-47736** — the pre-parse buffer grew without bound while waiting for `\r\n`, so one TCP connection that never sends CRLF drives the process to OOM.
- **CVE-2026-47737** — PROXY headers were re-parsed after every keep-alive request, so a client behind a trusted proxy could inject a second header and overwrite `REMOTE_ADDR`. Anything using `REMOTE_ADDR` for rate limiting, allow-listing or audit was spoofable.

Affected 5.5.0 → <7.2.1 and 8.0.0 → <8.0.2. If you cannot upgrade, terminate PROXY v1 at the load balancer and drop the option.

### Supply chain

- **Bundler 4 writes a `CHECKSUMS` section into `Gemfile.lock` by default** (SHA-256 per gem, verified before install). Commit it. If your tooling strips or half-updates it — `release-please`, some Dependabot paths, hand-edited version bumps — installs fail closed with an empty-checksum error, and people "fix" that by deleting the section. Don't; regenerate with `bundle lock`.
- Run installs in frozen/deployment mode in CI and production so a build cannot silently resolve a different version than the lockfile.
- **`bundle install` executes code.** Any gem with a native extension runs `extconf.rb` as your user at install time. Treat adding a gem as running its author's code on your build machine and on every deploy.
- Gems sourced with `git:`/`github:` in the Gemfile are not covered by gem checksums — pin a full commit SHA, never a branch.
- Run `bundler-audit` (`bundle audit check --update`) in CI. It reads the same `rubysec/ruby-advisory-db` these advisories come from and is the only thing that will tell you your Rack is three CVEs stale.

### Ruby core advisories on the default path

- **CVE-2026-46727** (20 May 2026, CVSS 8.1) — use-after-free in the pthread `getaddrinfo` timeout handler. Reached through `Addrinfo.getaddrinfo(..., timeout:)` and `Socket.tcp(..., resolv_timeout:)`, which is exactly the DNS timeout you were told to set. An attacker who can delay DNS responses near the timeout crashes the process. **Affects Ruby 4.0.0–4.0.4 only** (3.4 and earlier are not affected); fixed in **4.0.5**. If you cannot patch, drop the `timeout:`/`resolv_timeout:` arguments and enforce DNS timeouts at the resolver.
- **CVE-2026-27820** (5 Mar 2026) — buffer overflow in `Zlib::GzipReader`. Anything decompressing uploaded or fetched gzip is exposed.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability) → **up** to `mir-backend`.
- A specific library's mechanics (ActiveRecord N+1, strong parameters, Rails callbacks, migrations, Active Storage) → **down** to the framework module (`mir-backend-ruby-rails`).
- **Here:** what every Ruby backend shares because of YARV and Rack — GVL, Ractors, JIT enablement, Puma process/thread model, fork-safety, CoW memory, job hygiene, GC pressure, and the Rack/Puma/Bundler/Ruby-core security layer.
- A different runtime (Python, Go, Node…) → its own `mir-backend-<runtime>` tier. Never widen this one.
