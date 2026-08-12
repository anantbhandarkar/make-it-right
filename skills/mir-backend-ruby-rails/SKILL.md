---
name: mir-backend-ruby-rails
description: "Make It Right (Rails module). Ruby on Rails 8.1 specific reliability augmentation. Use alongside mir-backend and mir-backend-ruby when the target stack is Rails — carries the mechanical footguns the framework-agnostic skills deliberately omit: ActiveRecord N+1 and eager-loading strategies, params.expect / strong parameters and mass-assignment safety, callback side-effect timing (after_commit vs after_save) and jobs enqueued inside transactions, transaction semantics and nested transactions, migration safety on populated tables (the #1 Rails production incident class), connection pool sizing across the Rails 8 primary/cache/queue/cable databases, and the Rails security defaults and Active Storage advisories. TRIGGER only when the Ruby backend is Rails — building, reviewing, or debugging a Rails controller, model, concern, migration, Active Storage attachment, or background job that uses ActiveRecord. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-ruby (YARV runtime: GVL, Ractors, YJIT, Puma fork-safety, CoW memory, Rack/Puma/Bundler advisories); this module only adds Rails/ActiveRecord library mechanics — send Ruby-runtime, Puma, Rack or gem-supply-chain questions to mir-backend-ruby instead. SKIP for Sinatra, Hanami, pure Rack apps, or non-Ruby runtimes."
trigger: /mir-backend-ruby-rails
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-ruby-rails · Make It Right (Rails)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-ruby` (YARV runtime + Rack/Puma/Bundler) → **this** (Rails/ActiveRecord library mechanics). Run the gates first; load the Ruby runtime tier for the GVL, Puma, and fork-safety model; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (GVL, Ractors, YJIT, fork-safety, CoW memory, Rack and Puma CVEs, gem supply chain) live in `mir-backend-ruby` — not here.**

**Stack assumed:** Rails 8.1 · ActiveRecord + PostgreSQL · Solid Queue (the Rails 8 default) or Sidekiq/GoodJob for background jobs. Current release is **8.1.3.1** (29 Jul 2026); **8.0.5.1** is the security-only 8.0 line (8.0 bugfix support ended 7 May 2026); 8.2 exists only on edge and is **not released** — anything below marked "8.2" is not something you can ship today. Rails 8.1's gemspec requires Ruby **>= 3.2.0**. Verified against rubyonrails.org and rubygems.org on 13 Aug 2026. If the project uses a different DB adapter, note divergences (especially around migration concurrency flags) before applying these.

## What changed in Rails 8.0 / 8.1 (check before trusting older Rails advice)

| Change | Version | What it means for your code |
|---|---|---|
| `params.expect` | 8.0 | The current recommended strong-parameters API. `require(...).permit(...)` still works but turns malformed input into 500s — see §2 |
| Solid Queue / Solid Cache / Solid Cable are the defaults for new apps | 8.0 | Jobs, cache and Action Cable are now **database-backed**. Every one is another connection pool and another write path on your DB — see §6 |
| `bin/rails generate authentication` | 8.0 | Sessions table + `has_secure_password` + `rate_limit`. Authentication only — no registration, confirmation, MFA, lockout, or **authorization** — see Security |
| `rate_limit to:, within:` on Action Controller | 8.0 | Built-in throttling; the auth generator ships one on `SessionsController#create` |
| `enqueue_after_transaction_commit` symbol values **and the app-wide config both removed** | 8.1 | `:always`/`:never`/`:default` now raise; `config.active_job.enqueue_after_transaction_commit` is gone from 8.1 — set it per job class. Default is still OFF — see §3 |
| Built-in `sidekiq` Active Job adapter deprecated; `sucker_punch` adapter removed | 8.1 | Switch to the adapter shipped by the `sidekiq` gem (requires sidekiq ≥ 7.3.3) |
| `include ActiveJob::Continuable` | 8.1 | Long jobs resume from the last completed step after a restart. Steps must still be idempotent — a continuation saves the cursor, not the side effect |
| `Rails.event.notify` (structured event reporting) | 8.1 | A second place application data leaves the process. `config.filter_parameters` does **not** filter it |
| Associations can be marked `deprecated: true` (`:warn` / `:raise` / `:notify`) | 8.1 | Use `:raise` in CI to make removal safe |
| Order-dependent finders without an explicit order deprecated; `insert_all`/`upsert_all` with unpersisted records deprecated; `signed_id_verifier_secret` deprecated | 8.1 | Add explicit `.order(...)` before `.first`/`.last`; these become errors later |
| Leading-bracket param parsing, `;` query separators, multi-path routes removed | 8.1 | Old query-string handling and `get ["/a", "/b"] => ...` routes fail |

## The Rails footguns AI walks into most

### 1. ActiveRecord N+1 — silent query explosion in loops

N+1 is the most common Rails performance bug: load a collection, then access an association inside a loop, triggering one query per record.

```ruby
# WRONG — 1 query for orders + N queries for users
Order.all.each { |o| puts o.user.email }

# RIGHT — 2 queries total
Order.includes(:user).each { |o| puts o.user.email }
```

**Choose the right eager-loading strategy:**

| Method | SQL shape | Use when |
|---|---|---|
| `preload` | Two separate queries, always | Association is large; you don't need to `WHERE` on it |
| `eager_load` | Single `LEFT OUTER JOIN` | You need to filter/sort by the association (`where("users.role = ?", "admin")`) |
| `includes` | Picks `preload` or `eager_load` automatically based on whether a `references` or `where` clause touches the association | Default — let Rails decide unless behavior is surprising |

- **Bullet gem** (`gem "bullet"`) detects N+1 and unused eager loads in development/test — add it to `Gemfile` and enable in `config/environments/development.rb`.
- **`strict_loading`**: `Order.strict_loading.all` raises `ActiveRecord::StrictLoadingViolationError` on any lazy association access — zero-overhead enforcement in CI.

```ruby
# Model-level enforcement
class Order < ApplicationRecord
  self.strict_loading_by_default = true
end
```

Turning this on across an existing app raises everywhere at once. Stage it with `config.active_record.action_on_strict_loading_violation = :log`, fix what appears in the logs, then flip to `:raise`.

### 2. Mass assignment — `params.expect` is the current API, `permit!` is never the answer

Rails prevents assigning arbitrary request params to models. **Rails 8 replaced the `require`/`permit` pair with `params.expect`**, and AI still writes the old form (or `permit!`).

```ruby
# WRONG — permits any param the attacker submits (is_admin, role, tenant_id)
params.require(:user).permit!

# LEGACY — works, but `?user=hello` makes `permit` run on a String -> NoMethodError -> 500
params.require(:user).permit(:name, :email, :password)

# RIGHT (Rails 8) — one step, structure-checked
params.expect(user: [:name, :email, :password])
```

Why it matters beyond style: `expect` validates the *shape*, so parameter tampering raises `ActionController::ParameterMissing` and returns **400**, where `require`/`permit` raises `NoMethodError` and returns **500** with a backtrace. RuboCop's `Rails/StrongParametersExpect` will flag the old form.

- **Arrays of hashes need double brackets.** `params.expect(post: [:title, categories: [[:name]]])`. A single bracket means "an array of scalars" and the nested hashes are silently dropped.
- **`accepts_nested_attributes_for` is where this bites.** Indexed nested-attribute hashes get filtered out with *no error* — the record just saves without the children and a validation fails somewhere unrelated. Write `line_items_attributes: [[:id, :description, :quantity, :_destroy]]`.
- Never permit `:id`, `:user_id`, `:tenant_id`, `:account_id`, `:role`, `:admin`, `:stripe_customer_id`. Ownership columns are set from the session, never from params.
- Use distinct parameter shapes for `create` vs `update` when a field is only settable at creation.

### 3. Callbacks and job enqueues — external effects belong after the transaction commits

ActiveRecord callbacks (`after_save`, `after_create`, `after_update`) fire **inside the wrapping database transaction**. If the transaction later rolls back, the callback already ran — but the DB row was never committed.

```ruby
# WRONG — email fires inside the transaction; rolls back -> sent but no record
after_create :send_welcome_email

# RIGHT — fires only after the transaction commits successfully
after_commit :send_welcome_email, on: :create
```

**Rule:** any callback that touches an external system (email, Stripe charge, Slack notification, writing to S3) **must** use `after_commit`. Callbacks that only mutate the same record's in-memory state are fine in `before_save`/`after_save`.

**`perform_later` inside a transaction is the same bug, and Rails does not fix it for you by default.** With a database-backed queue (Solid Queue, GoodJob, delayed_job) the worker can be running in another process and pick the job up before your transaction commits — the classic `ActiveRecord::RecordNotFound` on a record you just created. With Redis-backed Sidekiq it is guaranteed to be enqueued even if you roll back.

```ruby
class SendReceiptJob < ApplicationJob
  self.enqueue_after_transaction_commit = true   # per job class — Rails 8.1
end
```

- In Rails 8.1 the symbol values `:always` / `:never` / `:default` **raise**; the value is a boolean.
- Rails 8.1 **removed** `config.active_job.enqueue_after_transaction_commit` (the 8.1 release notes list it under Active Job removals). Setting it in an initializer configures nothing that reads it. Set it on `ApplicationJob` (and remember `ActionMailer::MailDeliveryJob` is a separate class that defaults to `false`).
- The app-wide config is restored and defaults on in Rails 8.2, which is not released. Do not write code that assumes it.
- Alternative that works on every version: enqueue from `after_commit`, or write to an outbox row inside the transaction and enqueue from that.
- Keep callback chains shallow. More than 2–3 callbacks on a model means business logic is buried in the model. Move orchestration into a service object that calls the effects explicitly after commit.

### 4. Transactions — semantics, nesting, and rollback

```ruby
# Explicit transaction block — all-or-nothing
ActiveRecord::Base.transaction do
  order.update!(status: "paid")
  payment.update!(confirmed: true)
  # raise ActiveRecord::Rollback here to abort without re-raising to the caller
end
```

**Nested transactions — the silent swallowing trap:**

By default, nested `transaction` calls **join the outer transaction** rather than creating a savepoint. `raise ActiveRecord::Rollback` inside the inner block does nothing — the outer transaction absorbs it without rolling back.

```ruby
# WRONG — inner rollback is silently swallowed into outer tx
ActiveRecord::Base.transaction do
  outer_work
  ActiveRecord::Base.transaction do
    raise ActiveRecord::Rollback   # <-- ignored; outer tx continues
  end
end

# RIGHT — savepoint gives the inner block true rollback semantics
ActiveRecord::Base.transaction do
  outer_work
  ActiveRecord::Base.transaction(requires_new: true) do
    raise ActiveRecord::Rollback   # rolls back to savepoint only
  end
end
```

- Irreversible external effects (send email, charge card, publish event) go **after commit**, never inside the transaction. Guard with an idempotency key (see `mir-backend-ruby` — job idempotency).
- For contended row updates, use a `SELECT ... FOR UPDATE` lock: `Order.lock.find(id)` inside a transaction to prevent concurrent double-processing.
- HTTP calls inside a transaction hold a DB connection for the duration of the request to the third party. Under a slow provider this drains the pool and the whole app times out. Move the call out of the transaction.
- With Rails 8 defaults, Solid Queue/Cache/Cable may share the primary database. A long transaction on `primary` now also delays job pickup and cache writes.

### 5. Migrations on populated tables — the #1 Rails production incident class

**AI writes migrations as if the table is empty.** Production tables are not empty. Schema changes on large tables take an `ACCESS EXCLUSIVE` lock; every query behind it queues, and the app times out.

**Set a lock timeout first.** A DDL statement that waits for a lock blocks everything queued behind it, even if the DDL itself is instant. Fail fast and retry instead:

```ruby
class AddRegionToOrders < ActiveRecord::Migration[8.1]
  def change
    # bounded wait; better a failed migration than a stalled app
    execute "SET LOCAL lock_timeout = '5s'"
    add_column :orders, :region, :string
  end
end
```

**a) Adding an index without `CONCURRENTLY`**

```ruby
# WRONG — acquires a lock that blocks writes for the duration of the build
class AddIndexToOrders < ActiveRecord::Migration[8.1]
  def change
    add_index :orders, :user_id
  end
end

# RIGHT — disable_ddl_transaction! is a CLASS-level statement, not a line inside change
class AddIndexToOrders < ActiveRecord::Migration[8.1]
  disable_ddl_transaction!

  def change
    add_index :orders, :user_id, algorithm: :concurrently
  end
end
```

Putting `disable_ddl_transaction!` inside `def change` does nothing useful and the migration still fails — Postgres cannot run `CREATE INDEX CONCURRENTLY` inside a transaction. The same applies to `add_reference :orders, :user, index: { algorithm: :concurrently }`; the plain `add_reference ... index: true` builds a blocking index.

**b) Adding a column with a default — usually fine now, adding NOT NULL to an existing column is not**

On Postgres 11+ `add_column :orders, :region, :string, default: "us-east-1", null: false` is a metadata-only change; there is no table rewrite. The remaining risk is the brief exclusive lock (see `lock_timeout` above).

The dangerous one is applying `NOT NULL` to a column that already exists, which scans the whole table under an exclusive lock. Use a validated check constraint so the scan happens without blocking:

```ruby
# Migration 1 — add the constraint unvalidated (instant, no scan)
class AddRegionCheck < ActiveRecord::Migration[8.1]
  def change
    add_check_constraint :orders, "region IS NOT NULL", name: "orders_region_null", validate: false
  end
end

# Backfill in batches, outside a migration:
#   Order.where(region: nil).in_batches(of: 1_000) { |b| b.update_all(region: "us-east-1") }

# Migration 2 — validate (scans, but takes only a SHARE UPDATE EXCLUSIVE lock), then promote
class ValidateRegionNotNull < ActiveRecord::Migration[8.1]
  disable_ddl_transaction!

  def change
    validate_check_constraint :orders, name: "orders_region_null"
    change_column_null :orders, :region, false
    remove_check_constraint :orders, name: "orders_region_null"
  end
end
```

The same two-step applies to foreign keys: `add_foreign_key :orders, :users, validate: false`, then `validate_foreign_key :orders, :users` in a later migration.

**c) Renaming or dropping a column** — a running app still holds the old column name in its schema cache, so the old and new code must both work during the deploy. **`ignored_columns` goes last, not first**: it hides the column from the model, so declaring it before the replacement exists makes reads return nothing and writes stop persisting.

*Renaming* (never `rename_column` on a live table):

1. **Deploy 1:** add the new nullable column; deploy code that writes **both** old and new.
2. **Deploy 2:** backfill the new column in batches, outside a migration.
3. **Deploy 3:** switch reads to the new column, still dual-writing.
4. **Deploy 4:** stop writing the old column and add `self.ignored_columns += ["old_name"]`. Use `+=`, not `=` — assigning clobbers anything another concern added.
5. **Deploy 5:** drop the old column, once every running process ignores it.

*Dropping* is steps 4 and 5 alone: remove every code reference, deploy `ignored_columns`, then drop in the next deploy.

**d) Removing an index** — cheap, but a plain `DROP INDEX` still takes `ACCESS EXCLUSIVE` and waits behind any open transaction on the table. Use `remove_index :orders, :user_id, algorithm: :concurrently` with class-level `disable_ddl_transaction!` on a hot table.

**Use the `strong_migrations` gem.** It intercepts unsafe migration patterns at dev/CI time and prints the safe alternative, so nobody has to remember this list.

```ruby
# Gemfile
gem "strong_migrations"
```

### 6. Connection pools across four databases (tie-back to runtime tier)

This is the Rails-level manifestation of the runtime rule in `mir-backend-ruby`.

The `pool:` default is `RAILS_MAX_THREADS`, and each pool is **per process, per database**. Rails 8 generates four database entries — `primary`, `cache`, `queue`, `cable` — so the arithmetic changed:

```
connections = pools_per_process × processes_per_host × hosts
            = (primary + cache + queue + cable) × (puma_workers + job_processes) × hosts
```

Two Puma workers plus two Solid Queue processes, each with four pools of 5, is a ceiling of **80 connections per host** against a Postgres `max_connections` default of 100 — before you add a second host, a console, or a cron dyno. Pools open connections lazily, so 80 is the configured ceiling, not the count you will see idle; size against the ceiling anyway, because that is what a traffic spike claims.

- Do not copy `RAILS_MAX_THREADS` into all four entries. `cache` and `cable` need far fewer. **`pool:` is per process**, so size `queue` at roughly `threads + 2` for one Solid Queue worker (job threads plus polling and heartbeat) and multiply by the process count only in the host-wide budget, never inside the `pool:` value itself. Dispatcher and scheduler processes get their own budget.
- If Puma and a job runner share one process/dyno, count both against the same pool.
- Put a connection pooler (PgBouncer in transaction mode) in front once the total approaches `max_connections`. Transaction mode pins one server connection for the whole transaction, so `SELECT ... FOR UPDATE` is fine as long as every dependent statement stays inside that transaction. What it does break: `SET`/`RESET` session state, session-level advisory locks (`pg_advisory_lock`; the `_xact_` variants are fine), and `LISTEN`. Protocol-level prepared statements work only on PgBouncer ≥ 1.21 with `max_prepared_statements` set — otherwise ActiveRecord's default `prepared_statements: true` fails with `prepared statement "a1" does not exist`, and you must set `prepared_statements: false` in `database.yml`.

```yaml
# config/database.yml — size each database for its own concurrency
production:
  primary:
    pool: <%= ENV.fetch("RAILS_MAX_THREADS") { 5 } %>
  queue:
    pool: <%= ENV.fetch("SOLID_QUEUE_POOL") { 8 } %>
  cache:
    pool: 2
  cable:
    pool: 2
```

## How this slots into the core pipeline

- **Gate 5 (Design):** state the eager-loading strategy for every association accessed in the request, the transaction boundary, the callback/enqueue timing for every external side effect, and the total connection count from §6.
- **Gate 6 (Implementation):** code against the patterns above — `includes`/`preload`/`eager_load`, `params.expect`, `after_commit` + `enqueue_after_transaction_commit` for external effects, `requires_new:` for nested tx, class-level `disable_ddl_transaction!` + `algorithm: :concurrently` for indexes.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–6; the security-reviewer checks the section below. Pay special attention to any migration touching a table with > 100k rows.

## Security

Rails/ActiveRecord mechanics only. Ruby-runtime, Rack, Puma and gem-supply-chain items are in `mir-backend-ruby`.

### Object-level authorization (IDOR) — a session is not a permission

Rails gives you authentication and parameter filtering. It decides nothing about who may read or mutate a given row. `bin/rails generate authentication` (Rails 8) ships sessions, `has_secure_password` and rate limiting — and **no authorization layer at all**.

```ruby
# WRONG — any logged-in user can read any order by guessing an id
@order = Order.find(params[:id])

# RIGHT — scope through the ownership association; a wrong id is a 404, not a leak
@order = Current.user.orders.find(params[:id])
```

- Scope in the query, not in an `if` after the load. A post-load check is one early `return` away from being skipped.
- Sequential integer ids make enumeration trivial. `has_secure_token` or `signed_id` for anything exposed in a URL — but note `ActiveRecord::Base.signed_id_verifier_secret` is deprecated in 8.1; move to the per-purpose verifier API.
- Multi-tenant apps: `default_scope` is not isolation — `unscoped`, `joins`, raw SQL and `find_by_sql` all bypass it. Enforce tenancy in the association chain or at the database with row-level security.
- The Rails auth generator also omits registration, email confirmation, password-strength rules, MFA, account lockout and any audit trail. Every one of those is yours to add; list them in the Assumption Ledger rather than assuming the generator covered them.

### SQL injection — the APIs that still interpolate

ActiveRecord parameterizes `where(id: x)`. These do not:

```ruby
# WRONG
User.where("name = '#{params[:name]}'")
Order.order(params[:sort])                       # raises on bare columns, but not on all input
User.find_by_sql("SELECT * FROM users WHERE id = #{params[:id]}")
Order.pluck(Arel.sql(params[:col]))              # Arel.sql means "I promise this is safe"

# RIGHT
User.where("name = ?", params[:name])
User.where(name: params[:name])
Order.order(sort_column => sort_direction)       # both looked up in an allow-list Hash
User.find_by_sql(["SELECT * FROM users WHERE id = ?", params[:id]])
```

- `Arel.sql` is a trust assertion. Never wrap request data in it — build an allow-list of permitted column names and index into it.
- `LIKE` needs `sanitize_sql_like(params[:q])` as well as parameterization, or `%`/`_` turn into a table scan.

### Server-side template injection

`render inline: params[:tpl]`, `render(params[:template])` and `ERB.new(user_input)` are arbitrary code execution. Never let request data choose or build a template. See `mir-backend-ruby` for CVE-2026-41316, the ERB deserialization gadget that makes every Rails app a `Marshal.load` RCE target.

### Session cookies and deserialization

- `config.action_dispatch.cookies_serializer` must be `:json` (the default under `load_defaults 7.0`+) or `:message_pack`. **`:marshal` turns a leaked `secret_key_base` into remote code execution** rather than session forgery. Apps that upgraded from Rails 5 and never revisited this still carry `:marshal` or `:hybrid`.
- `secret_key_base` in `config/credentials.yml.enc` is what signs every cookie and signed id. If it leaks, rotate it — that invalidates all sessions, which is the point.
- Rails 8.1 adds `rails credentials:fetch`, which prints decrypted secrets to stdout for Kamal. Anything that captures CI logs now captures secrets.

### CSRF, SameSite, and the API-only hole

- `protect_from_forgery with: :exception` is on by default in generated apps via `config.action_controller.default_protect_from_forgery`. Cookie-authenticated endpoints need it; `Authorization: Bearer` endpoints do not.
- **`--api` apps do not include `ActionController::RequestForgeryProtection` at all.** If you then add cookie sessions to an API-only app — a very common AI-generated shape — you have cookie auth with zero CSRF protection. Either use bearer tokens or explicitly `include ActionController::RequestForgeryProtection` and enable it.
- `skip_forgery_protection` on a webhook controller is only acceptable when that controller verifies a provider signature (Stripe `Stripe-Signature`, GitHub `X-Hub-Signature-256`) with `ActiveSupport::SecurityUtils.secure_compare`.
- `config.action_dispatch.cookies_same_site_protection = :lax` is the default. Dropping it to `:none` for a cross-site embed removes the browser-side protection, so the CSRF token is the only thing left — never set `:none` on a controller that also skips forgery protection.
- Rails 8.2 adds `Sec-Fetch-Site`-header CSRF (`protect_from_forgery using: :header_only`). It is not in a released version; do not write it into an 8.1 app.

### Host header and open redirect

- **`config.hosts` is populated only in `development` by the generator.** In production it is empty, and `ActionDispatch::HostAuthorization` then permits any `Host` header. An attacker-controlled host lands in `url_for`-generated links — including the password-reset email. Set `config.hosts << "app.example.com"` in production explicitly.
- `config.action_controller.raise_on_open_redirects` is the default under `load_defaults 7.0`+. When AI hits `ActionController::Redirecting::UnsafeRedirectError` it "fixes" it with `redirect_to url, allow_other_host: true` — that reintroduces the open redirect. Validate against an allow-list of hosts instead.

### CORS

With `rack-cors`:

- `origins "*"` combined with `credentials: true` is invalid and browsers reject it. The usual "fix" is reflecting the request origin (`origins { |source, env| true }`), which is strictly worse — it makes every site a trusted origin with cookies attached.
- List exact origins. If the list must be dynamic, match against a stored allow-list, and never match with `String#start_with?` or a regex missing `\A`/`\z` (`evil-example.com.attacker.net` passes both).

### File serving and uploads

- `send_file` with request-derived paths is path traversal. Look the record up by id and serve its stored path; never join `params` onto `Rails.root`.
- Active Storage's Disk service carried both **CVE-2026-33195 (path traversal)** and **CVE-2026-33202 (glob injection)** — patched in 7.2.3.1 / 8.0.4.1 / 8.1.2.1. The local disk service is the default in development and in many small production apps.
- Set `config.active_storage.content_types_allowed_inline` and serve untrusted uploads from a separate host, or a stored SVG becomes stored XSS on your origin.

### Secret and PII leakage

- `config.filter_parameters` filters **logs only**. It does not filter `Rails.event.notify` payloads (new in 8.1), exception-tracker breadcrumbs, or job arguments recorded by your queue backend. Sidekiq's default web UI shows full job arguments.
- `ActiveRecord::Base.filter_attributes` masks columns in `inspect`; without it a record with a `token` column prints the token into any exception message.
- `config.consider_all_requests_local = true` in production renders the full debug page — source, locals and env — to whoever triggers the error. It must stay `false`. The debug-exceptions page also carried **CVE-2026-33167 (XSS)**.

### Current advisories on the default path

Do not ship below these. All ranges verified against rubyonrails.org release announcements on 13 Aug 2026.

| Advisory | Component | What it does | Fixed in |
|---|---|---|---|
| CVE-2026-66066 (GHSA-xr9x-r78c-5hrm) | Active Storage variant processing | Arbitrary file read → RCE through libvips loaders fed hostile uploads. Affects 7.0.0–7.2.3.1, 8.0.0–8.0.5, 8.1.0–8.1.3 when Active Storage uses Vips | **7.2.3.2 / 8.0.5.1 / 8.1.3.1** |
| CVE-2026-33195 · CVE-2026-33202 | Active Storage `DiskService` | Path traversal; glob injection | 7.2.3.1 / 8.0.4.1 / 8.1.2.1 |
| CVE-2026-33173 | Active Storage direct uploads | Insufficient metadata filtering | same |
| CVE-2026-33174 · CVE-2026-33658 | Active Storage proxy mode | DoS via `Range` / multi-range requests | same |
| CVE-2026-33167 · CVE-2026-33168 · CVE-2026-33170 | Action Pack debug exceptions · Action View tag helpers · `SafeBuffer#%` | XSS | same |
| CVE-2026-33169 · CVE-2026-33176 | Active Support `number_to_delimited` and number helpers | ReDoS / DoS | same |

The CVE-2026-66066 patch is not drop-in: it disables untrusted libvips loaders at boot and **requires libvips ≥ 8.13 and ruby-vips ≥ 2.2.1 or Rails will not boot**. BMP/ICO/PSD variants stop generating — remove those types from `config.active_storage.variable_content_types`. Rebuild container images, not just the Gemfile, or you ship the old libvips.

### Rate limiting and job dashboards

- `rate_limit to: 10, within: 3.minutes, only: :create` on any credential-checking action. The generated `SessionsController` has one; password reset, signup, and token exchange usually do not.
- `mount Sidekiq::Web => "/sidekiq"` and Mission Control::Jobs are **unauthenticated by default** and expose job arguments — meaning tokens, emails and ids. Wrap them: `authenticate :user, ->(u) { u.admin? } { mount ... }`, or a route `constraints` class. HTTP basic auth is acceptable only with `ActiveSupport::SecurityUtils.secure_compare`.

## Edit boundary (what belongs here vs. the core)

**This module holds ONLY Rails/ActiveRecord library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability principle)? → **generic core** (`mir-backend`).
- True for every Ruby backend on YARV or Rack (GVL, Ractors, YJIT, Puma worker/thread model, fork-safety, CoW memory, job idempotency, Rack/Puma CVEs, Bundler supply chain)? → **runtime tier** (`mir-backend-ruby`).
- A mechanical footgun of *this library* (ActiveRecord N+1, `params.expect`, callback timing, transaction nesting, migration safety, pool sizing across the Rails 8 databases, Active Storage advisories)? → **here**.
- A *different* framework on Ruby (Sinatra, Hanami) → new `mir-backend-ruby-<framework>` module. A *different* runtime → its own tier. Never widen this one.
