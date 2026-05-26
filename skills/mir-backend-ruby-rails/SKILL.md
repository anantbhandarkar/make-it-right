---
name: mir-backend-ruby-rails
description: "Make It Right (Rails module). Ruby on Rails 7+ specific reliability augmentation. Use alongside mir-backend and mir-backend-ruby when the target stack is Rails — carries the mechanical footguns the framework-agnostic skills deliberately omit: ActiveRecord N+1 and eager-loading strategies, strong parameters and mass-assignment safety, callback side-effect timing (after_commit vs after_save), transaction semantics and nested transactions, migration safety on populated tables (the #1 Rails production incident class), and connection pool sizing tied to Puma threads. TRIGGER only when the Ruby backend is Rails — building, reviewing, or debugging a Rails controller, model, concern, migration, or background job that uses ActiveRecord. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-ruby (YARV runtime: GVL, Puma fork-safety, CoW memory, job hygiene); this module only adds Rails/ActiveRecord library mechanics. SKIP for Sinatra, Hanami, pure Rack apps, or non-Ruby runtimes."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-ruby` (YARV runtime model) → **this** (Rails/ActiveRecord library mechanics). Run the gates first; load the Ruby runtime tier for the GVL, Puma, and fork-safety model; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (GVL, fork-safety, CoW memory, Puma pool sizing, job idempotency) live in `mir-backend-ruby` — not here.**

**Stack assumed:** Rails 7.1+ · ActiveRecord + PostgreSQL · Sidekiq or GoodJob for background jobs. If the project uses a different DB adapter, note divergences (especially around migration concurrency flags) before applying these.

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
- **`strict_loading`** (Rails 6.1+): `Order.strict_loading.all` raises `ActiveRecord::StrictLoadingViolationError` on any lazy association access — zero-overhead enforcement in CI.

```ruby
# Model-level enforcement
class Order < ApplicationRecord
  self.strict_loading_by_default = true
end
```

### 2. Strong Parameters — stop mass assignment at the boundary

Rails prevents assigning arbitrary request params to models via `permit`. AI commonly skips this, uses `permit!`, or forgets to exclude sensitive fields.

```ruby
# WRONG — permits any param the attacker submits (is_admin, role, tenant_id)
def user_params
  params.require(:user).permit!
end

# RIGHT — explicit allowlist; sensitive fields never appear
def user_params
  params.require(:user).permit(:name, :email, :password)
  # Never: :is_admin, :role, :tenant_id, :stripe_customer_id
end
```

- Always create distinct parameter shapes for `create` vs `update` if some fields are only settable at creation (e.g. `tenant_id`, `email`).
- For nested attributes (`accepts_nested_attributes_for`), each nested hash needs its own `permit` clause — Rails does not recurse automatically.

### 3. Callbacks — side effects with external systems belong in `after_commit`

ActiveRecord callbacks (`after_save`, `after_create`, `after_update`) fire **inside the wrapping database transaction**. If the transaction later rolls back (e.g. a validation fails deeper in the call stack, or another `after_save` raises), the callback already ran — but the DB row was never committed.

```ruby
# WRONG — email fires inside the transaction; rolls back -> sent but no record
after_create :send_welcome_email

# RIGHT — fires only after the transaction commits successfully
after_commit :send_welcome_email, on: :create
```

**Rule:** any callback that touches an external system (email, Stripe charge, Slack notification, enqueuing a job, writing to S3) **must** use `after_commit`. Callbacks that only mutate the same record's in-memory state are fine in `before_save`/`after_save`.

- Keep callback chains shallow. More than 2–3 callbacks on a model is a smell — business logic is getting buried. Move orchestration into a service object or command that calls `after_commit` hooks explicitly.

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

### 5. Migrations on populated tables — the #1 Rails production incident class

**AI writes migrations as if the table is empty.** Production tables are not empty. Schema changes on large tables cause table locks that queue every request behind them, leading to timeouts and downtime.

**The four common disasters and their fixes:**

**a) Adding an index without `CONCURRENTLY`**

```ruby
# WRONG — acquires full table lock; blocks reads + writes for minutes on large tables
def change
  add_index :orders, :user_id
end

# RIGHT — builds index without blocking
def change
  disable_ddl_transaction!
  add_index :orders, :user_id, algorithm: :concurrently
end
```

`algorithm: :concurrently` requires `disable_ddl_transaction!` at the top of the migration — Postgres's `CREATE INDEX CONCURRENTLY` cannot run inside a transaction.

**b) Adding a NOT NULL column with a default (Postgres < 11: full rewrite; Postgres 11+: metadata-only, but Rails still rewrites)**

```ruby
# WRONG — rewrites entire table in older Postgres; locks for minutes
def change
  add_column :orders, :region, :string, null: false, default: "us-east-1"
end

# RIGHT — 3-step expand/contract over multiple deploys
# Deploy 1: add nullable column
def change
  add_column :orders, :region, :string
end

# Deploy 2 (separate migration, after backfill): add NOT NULL + default
# Backfill in a Rake task / script in batches first:
#   Order.in_batches(of: 1000) { |b| b.update_all(region: "us-east-1") }
def change
  change_column_null :orders, :region, false
  change_column_default :orders, :region, "us-east-1"
end
```

**c) Renaming or dropping a column** — a running app still holds the old column name in its schema cache. The safe multi-deploy dance:
1. **Deploy 1:** tell ActiveRecord to ignore the column via `self.ignored_columns = ["old_name"]` (Rails 5+). Deploy.
2. **Deploy 2:** add the new column (if rename) and backfill data. Deploy.
3. **Deploy 3:** update all code references to the new name. Deploy.
4. **Deploy 4:** drop the old column. Deploy.

**d) Removing an index** — always safe (`DROP INDEX CONCURRENTLY` is also available but rarely needed).

**Use the `strong_migrations` gem.** It intercepts unsafe migration patterns at dev/CI time and tells you the safe alternative — zero production incidents, no memorization required.

```ruby
# Gemfile
gem "strong_migrations"
```

### 6. Connection pool sizing vs Puma threads (tie-back to runtime tier)

This is the Rails-level manifestation of the runtime rule in `mir-backend-ruby`.

- `config/database.yml` `pool:` must equal `ENV["RAILS_MAX_THREADS"]`. Rails itself warns when these diverge in newer versions, but AI still hardcodes a mismatch.
- With Active Job and Sidekiq running in the same dyno/process, count those threads too. A 5-thread Puma + 10-thread Sidekiq sharing one pool of 5 = guaranteed `ActiveRecord::ConnectionTimeoutError` under load.

```yaml
# config/database.yml — correct sizing
production:
  pool: <%= ENV.fetch("RAILS_MAX_THREADS") { 5 }.to_i + ENV.fetch("SIDEKIQ_CONCURRENCY") { 0 }.to_i %>
```

## How this slots into the core pipeline

- **Gate 5 (Design):** state the eager-loading strategy for every association accessed in the request, the transaction boundary, and the callback timing for any external side effect.
- **Gate 6 (Implementation):** code against the patterns above — `includes`/`preload`/`eager_load`, `after_commit` for external effects, `requires_new:` for nested tx, `algorithm: :concurrently` + `disable_ddl_transaction!` for indexes.
- **Gate 7 (Review):** the reliability-reviewer additionally checks items 1–6 here; pay special attention to any migration touching a table with > 100k rows.

## Edit boundary (what belongs here vs. the core)

**This module holds ONLY Rails/ActiveRecord library mechanics.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register, observability principle)? → **generic core** (`mir-backend`).
- True for every Ruby backend on YARV (GVL, Puma worker/thread model, fork-safety, CoW memory, job idempotency, frozen string literals)? → **runtime tier** (`mir-backend-ruby`).
- A mechanical footgun of *this library* (ActiveRecord N+1, strong parameters, callback timing, transaction nesting, migration safety, connection pool sizing)? → **here**.
- A *different* framework on Ruby (Sinatra, Hanami) → new `mir-backend-ruby-<framework>` module. A *different* runtime → its own tier. Never widen this one.
