---
name: mir-backend-beam-phoenix
description: "Make It Right (Phoenix module). Phoenix + LiveView + Ecto + PostgreSQL specific reliability augmentation. Use alongside mir-backend and mir-backend-beam when the target stack is Phoenix — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: LiveView per-connection process memory scaling and temporary_assigns/streams for large collections, blocking the LiveView process in handle_event freezing the client, Ecto N+1 with unloaded associations raising DetachedInstanceError, migration safety on populated tables (create index concurrently, disable_ddl_transaction!, expand/contract for NOT NULL), PubSub fan-out backpressure, and idempotent handle_event for double-click races. TRIGGER only when the BEAM backend stack is Phoenix — building, reviewing, or debugging a Phoenix controller, LiveView, Ecto query, or migration. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-beam (BEAM runtime concerns: supervision, mailbox growth, GenServer bottlenecks, ETS, distributed Erlang); this module only adds Phoenix/Ecto library mechanics. SKIP for plain Erlang/OTP apps, Nerves, or any non-Phoenix stack (those get their own mir-backend-beam-<framework> module), and for non-BEAM runtimes."
trigger: /mir-backend-beam-phoenix
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-beam-phoenix · Make It Right (Phoenix)

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-beam` (BEAM/Erlang VM process model) → **this** (Phoenix/Ecto/LiveView library mechanics). Run the gates first; load the BEAM runtime tier for supervision, mailbox, and ETS concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (supervision trees, unbounded mailboxes, GenServer bottlenecks, ETS, distributed Erlang) live in `mir-backend-beam` — not here.**

**Stack assumed:** Phoenix 1.7+ · LiveView 0.20+ · Ecto 3.x · PostgreSQL · PubSub. If the project uses Ecto with a different adapter, note the divergence before applying migration-safety items.

## The Phoenix footguns AI walks into most

### 1. LiveView state is per-connection — memory scales with concurrent users

Each connected LiveView client is a **stateful process** holding all socket assigns in its heap. Storing large collections (a paginated list of thousands of records, a full dataset) in assigns means that memory multiplies by the number of connected users.

- **Use `temporary_assigns`** (Phoenix 1.6 and earlier patterns) to tell LiveView that a key should be reset to its default after each render — the data is not kept in the socket between diffs.
- **Use LiveView streams** (Phoenix 1.7+ `stream/3` and `stream_insert/4`) for large, dynamically updating collections. Streams keep only the rendered DOM diff state server-side; the full list is not stored in assigns.
- **Paginate aggressively on mount.** Never `Repo.all(MySchema)` into assigns for a list page — always `Repo.paginate` or a cursor-based query.

```elixir
# Dangerous: every connected client holds the full item list in memory
def mount(_params, _session, socket) do
  {:ok, assign(socket, items: Repo.all(Item))}
end

# Safe (streams): server holds only diff state, not the full list
def mount(_params, _session, socket) do
  {:ok, stream(socket, :items, Repo.all(Item))}
end

# Safe (temporary_assigns): resets to [] after render — not kept between events
def mount(_params, _session, socket) do
  {:ok, assign(socket, items: Repo.all(Item)), temporary_assigns: [items: []]}
end
```

### 2. Don't block the LiveView process in handle_event

A LiveView process is stateful and **serializes all events for that client** — one `handle_event/3` call at a time. Doing heavy work (DB queries, HTTP calls, compute) inside `handle_event` freezes that client's UI until it completes. The client sees a hung interface; if it times out the socket drops.

- **Offload to a Task** and handle the result in `handle_info/2`. Use `assign_async/3` (Phoenix 1.7.9+ / LiveView 0.20+) for the idiomatic async-assign pattern that tracks loading state automatically.
- **Keep `handle_event` fast**: validate input, update UI state optimistically, and kick off async work.

```elixir
# Dangerous: blocks the LiveView process for the duration of the query
def handle_event("search", %{"q" => q}, socket) do
  results = Repo.all(from r in Record, where: ilike(r.name, ^"%#{q}%"))
  {:noreply, assign(socket, results: results)}
end

# Safe: assign_async tracks :loading/:ok/:failed automatically
def handle_event("search", %{"q" => q}, socket) do
  {:noreply,
   assign_async(socket, :results, fn ->
     {:ok, %{results: Repo.all(from r in Record, where: ilike(r.name, ^"%#{q}%"))}}
   end)}
end

# Alternatively: spawn a Task, handle result in handle_info
def handle_event("search", %{"q" => q}, socket) do
  Task.async(fn -> Repo.all(from r in Record, where: ilike(r.name, ^"%#{q}%")) end)
  {:noreply, assign(socket, loading: true)}
end

def handle_info({ref, results}, socket) when is_reference(ref) do
  Process.demonitor(ref, [:flush])
  {:noreply, assign(socket, results: results, loading: false)}
end
```

### 3. Ecto N+1 — unloaded associations raise, not silently query

In Ecto, accessing an unloaded association outside an active repo session raises `%Ecto.Association.NotLoaded{}` — it does NOT transparently issue a query the way ActiveRecord does. AI routinely writes loops that access associations on returned structs and ships code that either raises at runtime or causes N database roundtrips when preloaded lazily in a loop.

- **Always preload associations you intend to access**, either in the query (`from u in User, preload: [:posts]`) or explicitly (`Repo.preload(users, :posts)`).
- **Use `join` + `preload` in a single query** to avoid the extra roundtrip when filtering on the association.
- **In LiveView**, preload in `mount/3` or the query that populates assigns — never access associations during rendering.

```elixir
# Raises %Ecto.Association.NotLoaded{} or makes N queries in a loop
users = Repo.all(User)
Enum.map(users, fn u -> u.posts end)   # each access is either a raise or N+1

# Correct: preload in the query
users = Repo.all(from u in User, preload: [:posts])
Enum.map(users, fn u -> u.posts end)   # safe, already loaded

# Or: preload separately
users = User |> Repo.all() |> Repo.preload(:posts)
```

### 4. Ecto migration safety on populated tables

AI writes migrations as if the table is empty. In production, `ALTER TABLE` on large tables locks and blocks reads/writes. The safe patterns differ by operation:

- **Adding an index:** use `create index(:table, [:col], concurrently: true)` and add `@disable_ddl_transaction true` + `@disable_migration_lock true` to the migration module — `CONCURRENTLY` cannot run inside a transaction.
- **Adding a NOT NULL column:** never add a `NOT NULL` column with a default in a single migration on a large table (it rewrites the table on older Postgres; on Postgres 11+ it's safe for constant defaults, but verify your version). The safe pattern: (1) add the column nullable, (2) backfill in batches, (3) add the NOT NULL constraint with `NOT VALID`, (4) validate in a subsequent deploy.
- **Renaming a column or table:** never rename while old code is deployed — use the expand/contract pattern: add the new column, dual-write both, migrate reads, drop the old column in a later deploy.
- **Backfilling large tables:** never `UPDATE table SET ...` without a WHERE on batches — it locks every row and can fill WAL. Use a batched `Repo.update_all` with `limit/offset` or a cursor.

```elixir
# Dangerous: locks the table for index creation
def change do
  create index(:orders, [:status])
end

# Safe: concurrent index, no transaction
@disable_ddl_transaction true
@disable_migration_lock true
def change do
  create index(:orders, [:status], concurrently: true)
end
```

### 5. PubSub fan-out — don't broadcast huge payloads to many subscribers

`Phoenix.PubSub.broadcast/3` sends a message to every subscriber of a topic. Each subscriber is a separate process (e.g., each connected LiveView). Broadcasting large structs (full Ecto schemas with nested preloads) copies that data into every subscriber's process heap simultaneously.

- **Broadcast minimal payloads** — event type + entity ID, not the full struct. Let each LiveView re-fetch only the data it needs.
- **For high fan-out** (thousands of subscribers on one topic), consider partitioned topics or using `FastGlobal` / `persistent_term` for data that all subscribers read identically.
- **Do not broadcast from inside a `handle_event`** in a way that causes a cascade of re-renders — debounce or batch updates.

### 6. Channels — don't hold huge state in socket assigns; backpressure for pushes

Phoenix Channels are also stateful processes (one per connected client). The same rules as LiveView apply: don't store large collections in the socket, don't block `handle_in/3` with slow work.

- **`push/3` is not flow-controlled.** If you push many messages rapidly to a channel, the client's receive buffer can back up. For high-frequency data (live price feeds, game state), throttle pushes server-side or use a delta/diff strategy.
- **`handle_in` is synchronous for that socket** — offload slow work to a Task the same way as LiveView.

### 7. Idempotent handle_event — guard against double-clicks and duplicate events

Network latency means a user may click a button, see no immediate response, and click again — sending two events. LiveView does not deduplicate these. An event that triggers a DB write or a charge must be idempotent.

- **Use a unique constraint on the DB** as the final safety net (idempotency key in the row).
- **Disable the button in the socket state** after the first event and re-enable only on explicit success/failure response.
- **Do not rely on LiveView's phx-disable-with alone** — it's UI-only and is bypassed by network retries or duplicate tabs.

```elixir
def handle_event("submit_order", _params, %{assigns: %{submitting: true}} = socket) do
  # Already in flight — ignore duplicate
  {:noreply, socket}
end

def handle_event("submit_order", params, socket) do
  socket = assign(socket, submitting: true)
  Task.async(fn -> Orders.place(params) end)
  {:noreply, socket}
end

def handle_info({ref, {:ok, order}}, socket) when is_reference(ref) do
  Process.demonitor(ref, [:flush])
  {:noreply, assign(socket, submitting: false, order: order)}
end
```

## How this slots into the core pipeline

- **Gate 5 (Design):** state LiveView memory strategy (streams vs temporary_assigns vs pagination), transaction boundaries for Ecto mutations, and the idempotency mechanism. Migration plan must include concurrency and expand/contract for destructive changes.
- **Gate 6 (Implementation):** code against items 1–7 above. Every `handle_event` that does I/O must offload. Every Ecto query that renders associations must preload. Every index migration must be concurrent.
- **Gate 7 (Review):** the reliability-reviewer checks items 1–7 here; the migration-reviewer audits for missing `concurrently`, missing `@disable_ddl_transaction`, and unsafe backfills.

## Edit boundary (what belongs here vs. the core)

**This module holds ONLY one library's mechanics — Phoenix · LiveView · Ecto · PostgreSQL · PubSub.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register)? → **generic core** (`mir-backend`).
- True for every BEAM backend (supervision design, mailbox growth, hot GenServer, ETS, distributed Erlang)? → **runtime tier** (`mir-backend-beam`).
- A mechanical footgun of *this library* (LiveView assign memory, `assign_async`, Ecto N+1 raising, migration `concurrently`, PubSub payload size, double-click idempotency)? → **here**.
- A *different* framework on BEAM (plain OTP app, Nerves) → new `mir-backend-beam-<framework>` module. A *different* runtime → its own tier. Never widen this one.
