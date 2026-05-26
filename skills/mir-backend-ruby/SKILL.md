---
name: mir-backend-ruby
description: "Make It Right (Ruby runtime tier). YARV/MRI Ruby 3.3+ runtime reliability footguns shared across EVERY Ruby backend framework (Rails, Sinatra, Hanami, Sidekiq workers) — distinct from the generic backend gates and from any one framework's mechanics. Covers: the GVL (threads give no CPU parallelism, like Python's GIL), Puma's forked-worker + thread model, fork-safety of DB/Redis connections, copy-on-write memory and per-worker bloat, background job hygiene (Sidekiq/GoodJob idempotency, retries), and GC/frozen-string pressure. TRIGGER when the backend runtime is Ruby — sits between mir-backend (generic) and the framework module (e.g. mir-backend-ruby-rails). SKIP for Node/JVM/Go/Rust/.NET/Python/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics (those live in the framework module)."
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

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-ruby-rails`) knows the **library's mechanics**. This tier owns what's true for **all Ruby backends because they run on YARV (MRI)** — the concurrency model and process model that Rails, Sinatra, Hanami, and Sidekiq all inherit.

**Runtime assumed:** MRI Ruby 3.3+ running under Puma. Notes hold for JRuby except where GVL specifics differ (JRuby has no GVL; true thread parallelism is available). Load order: `mir-backend` → `mir-backend-ruby` → `<framework module>`.

## The YARV footguns AI walks into (framework-agnostic)

### 1. The GVL — threads are NOT CPU parallelism

Ruby's Global VM Lock (GVL, formerly GIL) allows only one thread to execute Ruby bytecode at a time. **CPU-bound work does not run in parallel across threads** — multiple threads serialize on the GVL and you pay context-switch overhead on top.

- **CPU-bound** (image processing, crypto, heavy computation): use multiple **processes** (Puma workers), a C extension that releases the GVL (`mini_magick`, `bcrypt`, `openssl` release it internally), or offload to a dedicated service / background worker. Adding more threads will not help — it will make things worse.
- **I/O-bound** (DB queries, HTTP calls, disk reads): threads are fine — the GVL releases during blocking I/O syscalls, so other threads run while one waits.
- This is the runtime-level reason the runtime-map says "SKIP Ruby for high-frequency data pipelines or intense processing."

```ruby
# WRONG — adding threads to speed up CPU-bound work; GVL serializes them
threads = data.map { |chunk| Thread.new { heavy_cpu_transform(chunk) } }
threads.each(&:join)

# RIGHT — for CPU-bound parallelism, use processes
results = data.each_slice(chunk_size).flat_map do |slice|
  fork { compute(slice) }   # or a worker queue — Sidekiq, GoodJob
end
```

### 2. Puma's process × thread model — pool sizing must match DB connections

Puma = **N forked worker processes** × **M threads per worker**. Each thread can hold one DB connection at a time. If `RAILS_MAX_THREADS` (thread count) does not equal the database connection pool `pool` setting, threads starve waiting for a connection at load spikes.

```yaml
# config/database.yml — pool must equal RAILS_MAX_THREADS (default 5)
production:
  pool: <%= ENV.fetch("RAILS_MAX_THREADS") { 5 } %>
```

- Total DB connections consumed = `workers × threads`. Size your Postgres `max_connections` (or PgBouncer pool) accordingly — a 3-worker × 5-thread Puma plus 5 Sidekiq threads = up to 20 connections.
- AI commonly generates a pool much smaller than thread count (e.g. `pool: 2` with 5 threads), causing `ActiveRecord::ConnectionTimeoutError` under any real load.

### 3. Fork-safety — connections created before fork are invalid in children

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

### 4. Memory: copy-on-write and per-worker bloat

Puma's forked workers share memory pages read-only (copy-on-write, CoW) until a page is modified. Once a worker writes to a page — mutating a shared object, updating a class variable, running the GC — that page is copied into the worker's own address space. Memory use grows per worker over time.

- **Don't mutate shared objects after boot.** Memoize with `||=` only on thread-local or request-scoped objects, not on class-level constants or module variables that will be written during requests.
- **Frozen string literals** reduce object churn and help CoW; add `# frozen_string_literal: true` to every file. Ruby 3+ warns on mutation of unfrozen string literals.
- **Memory bloat mitigation:** use `jemalloc` (link Puma against it with `LD_PRELOAD`) to reduce fragmentation. Use `puma-worker-killer` to restart workers that exceed a memory threshold, preventing unbounded bloat.

```ruby
# Gemfile — memory management
gem "puma-worker-killer"

# config/initializers/puma_worker_killer.rb
PumaWorkerKiller.config do |config|
  config.ram           = 1024  # MB — restart worker above this
  config.frequency     = 5     # seconds between checks
  config.percent_usage = 0.98
end
PumaWorkerKiller.start
```

### 5. Background jobs — async/durable work belongs in a worker, not the request thread

A request thread lives for the duration of one HTTP request. Any work that must survive a deploy, be retried on failure, or take longer than ~100ms should run in a background worker (Sidekiq, GoodJob, Delayed::Job). AI commonly writes inline work in the controller that should be a job.

- **Idempotency is mandatory.** Workers retry on failure — a job may run more than once. Guard side effects with a unique key, a DB uniqueness constraint, or an idempotency check before executing.

```ruby
class SendReceiptJob < ApplicationJob
  queue_as :default

  def perform(order_id)
    order = Order.find(order_id)
    return if order.receipt_sent?          # idempotency guard
    ReceiptMailer.send_receipt(order).deliver_now
    order.update!(receipt_sent: true)
  end
end
```

- **Do not pass ActiveRecord objects into jobs.** Pass IDs. The object state at enqueue time differs from state at execution time, and serializing full objects bloats the queue and causes stale data bugs.

```ruby
# WRONG
SendReceiptJob.perform_later(order)    # serializes full object

# RIGHT
SendReceiptJob.perform_later(order.id) # re-fetches fresh state at runtime
```

### 6. GC pressure and frozen string literals

MRI's GC is stop-the-world (per-thread stop in Ruby 3.x with ractors, but single-threaded GC in standard use). Object allocation spikes cause GC pauses that manifest as p99 latency spikes.

- Add `# frozen_string_literal: true` to every file. String interpolation still allocates; repeated identical string literals do not.
- Avoid building large arrays/hashes in tight loops just to discard them. Prefer `each` over `map` when you don't use the result.
- Profile with `rack-mini-profiler` + `memory_profiler` before assuming GC is the bottleneck — but frozen literals cost nothing and are always worth doing.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** state the concurrency model (Puma workers + threads, Sidekiq, inline). A mismatch — e.g. threads for CPU-bound work, or inline code for durable work — is a runtime-level design defect. Flag it.
- **Gate 6 (implementation):** add `on_worker_boot` reconnects; size pool = threads; guard job idempotency; frozen string literals.
- **Gate 7 (review):** the reliability-reviewer additionally checks items 1–6 here for any Ruby service.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability) → **up** to `mir-backend`.
- A specific library's mechanics (ActiveRecord N+1, strong parameters, Rails callbacks, migrations) → **down** to the framework module (`mir-backend-ruby-rails`).
- **Here:** only what every Ruby backend shares because of YARV (GVL, Puma process/thread model, fork-safety, CoW memory, job hygiene, GC pressure).
- A different runtime (Python, Go, Node…) → its own `mir-backend-<runtime>` tier. Never widen this one.
