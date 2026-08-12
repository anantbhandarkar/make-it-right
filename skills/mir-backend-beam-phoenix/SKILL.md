---
name: mir-backend-beam-phoenix
description: "Make It Right (Phoenix module). Phoenix + LiveView + Ecto + Plug + PostgreSQL specific reliability augmentation. Use alongside mir-backend and mir-backend-beam when the target stack is Phoenix — it carries the mechanical footguns that the framework-agnostic tiers deliberately omit: LiveView per-connection process memory and streams vs temporary_assigns for large collections, the current async-assign APIs (assign_async/4, start_async/4 + handle_async/3, cancel_async/3) and why a bare Task.async in a LiveView kills the LiveView on crash, LiveView handle_event as a public websocket endpoint that must re-authorize on every event, Phoenix 1.8 scopes threading %Scope{} through context functions, Ecto N+1 with unloaded associations, changeset cast/4 as the mass-assignment allow-list, migration safety on populated tables (concurrently, disable_ddl_transaction, expand/contract, NOT VALID + VALIDATE), PubSub fan-out payload size, channel join limits, and idempotent events for double-click races. TRIGGER only when the BEAM backend stack is Phoenix — building, reviewing, or debugging a Phoenix controller, LiveView, channel, Ecto query, changeset, or migration. Always loads TOGETHER WITH mir-backend (the gates) and mir-backend-beam (BEAM runtime concerns: supervision, mailbox growth, GenServer bottlenecks, ETS, distributed Erlang, atom exhaustion, Hex supply chain); this module only adds Phoenix/LiveView/Ecto/Plug library mechanics. SKIP for plain Erlang/OTP applications, Nerves, and Broadway-only pipelines with no web layer (those stop at mir-backend-beam), and for every non-BEAM runtime."
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

Bottom tier of the chain: `mir-backend` (generic gates) → `mir-backend-beam` (BEAM/Erlang VM process model) → **this** (Phoenix/LiveView/Ecto/Plug library mechanics). Run the gates first; load the BEAM runtime tier for supervision, mailbox, ETS and atom-exhaustion concerns; reach for *this* at Gate 5 (design mechanics), Gate 6 (implementation), and Gate 7 review. **Runtime-level concerns (supervision trees, unbounded mailboxes, GenServer bottlenecks, `:persistent_term`, distributed Erlang, `binary_to_term`, Hex supply chain) live in `mir-backend-beam` — not here.**

**Stack assumed:** Phoenix · LiveView · Ecto · Plug · Bandit · PostgreSQL · PubSub. Verified 13 Aug 2026 on hex.pm: **Phoenix 1.8.11** (12 Aug 2026, 1.9 is unreleased and in development), **phoenix_live_view 1.2.9** (10 Aug 2026, with 1.1.33 as a maintained line), **ecto 3.14.1** (9 Jul 2026), **ecto_sql 3.14.0**, **plug 1.20.3** (9 Jul 2026), **bandit 1.12.4** (27 Jul 2026), **postgrex 0.22.4** (7 Aug 2026), **phoenix_pubsub 2.2.0**. Bandit is the default adapter in generated Phoenix apps. Phoenix 1.8 requires OTP 25+. If the project uses an Ecto adapter other than Postgres, note the divergence before applying the migration-safety items — the `CONCURRENTLY` and `NOT VALID` patterns below are PostgreSQL-specific.

## The Phoenix footguns AI walks into most

### 1. LiveView state is per-connection — memory scales with concurrent users

Each connected LiveView client is a **stateful process** holding all socket assigns in its heap. A list of thousands of records in assigns is multiplied by the number of connected users.

- **Use LiveView streams** (`stream/4`, `stream_insert/4`, `stream_delete/3`) for large or dynamically updating collections. The server keeps only the diff bookkeeping; the list itself is not retained in assigns.
- **The template half is mandatory and is where streams silently break.** The immediate parent of the items needs a unique DOM `id` **and** `phx-update="stream"`, and every item element needs the stream-generated `dom_id`. Miss either and inserts and deletes stop patching correctly — the assigns are fine, the DOM is not:

```heex
<tbody id="songs" phx-update="stream">
  <tr :for={{dom_id, song} <- @streams.songs} id={dom_id}>{song.title}</tr>
</tbody>
```
- **`temporary_assigns` is not deprecated** and is still a documented `mount/3` option in 1.2.9 — "a keyword list of assigns that are temporary and must be reset to their value after every render." It solves a different problem from streams: it resets a value after render rather than managing a client-side collection. Use it for one-shot render data, not as a substitute for streams.
- **Streams fix socket memory, not query size.** `stream(socket, :items, Repo.all(Item))` still loads every row into the LiveView process for the duration of that call. Paginate or use a cursor in the query itself.
- **LiveView 1.1 added `stream_async/4`** to insert stream items from an async task, and an `:update_only` option on `stream_insert/4` for updating an existing item without inserting when it is absent.
- **LiveView 1.1 tracks changes per item inside `:for` comprehensions**, and adds `:key` so reordering produces a minimal diff instead of a full re-send:

```heex
<li :for={item <- @items} :key={item.id}>{item.name}</li>
```

```elixir
# Dangerous: every connected client holds the full item list in memory
def mount(_p, _s, socket), do: {:ok, assign(socket, items: Repo.all(Item))}

# Safe: bounded query + stream
def mount(_p, _s, socket), do: {:ok, stream(socket, :items, Items.list_page(limit: 50))}
```

Two LiveView 1.1 upgrade breaks that bite immediately: `:phoenix_live_view` must be **prepended to the `:compilers` list in `mix.exs`**, and `Phoenix.LiveViewTest` moved from Floki to LazyHTML, so the `fl-contains` / `fl-icontains` selectors no longer work — use the `text_filter` argument, `element("main a", "Sign up")`.

### 2. Don't block the LiveView process in `handle_event`

A LiveView process **serializes all events for that client** — one `handle_event/3` at a time. Heavy work inside it freezes that client's UI; if the client times out the socket drops and the LiveView remounts.

The current async APIs, verified against LiveView 1.2.9:

| API | Returns to | Use when |
|---|---|---|
| `assign_async(socket, key_or_keys, func, opts \\ [])` | Assigns an `AsyncResult` under each key; the function must return `{:ok, assigns}` or `{:error, reason}` | The result *is* the assign and you want `:loading` / `:ok` / `:failed` tracked for you |
| `start_async(socket, name, func, opts \\ [])` | Invokes your `handle_async/3` with `{:ok, result}` or `{:exit, reason}` | You need to do something with the result other than assign it directly |
| `cancel_async(socket, async_or_keys, reason \\ {:shutdown, :cancel})` | Kills the underlying process | Superseded searches, navigation away, an explicit cancel button |

**Do not use a bare `Task.async/1` in a LiveView.** `Task.async` links the task to the caller and leaves the exit unhandled, so a crash inside the task takes the LiveView process down with it and the user's page resets. `start_async/4` also links, but it **wraps errors and exits** and hands them to `handle_async/3` as `{:exit, reason}` — that wrapping, not supervision, is what keeps the LiveView alive. It is not supervised by default; pass `supervisor: MyApp.TaskSup` if the work must run under a `Task.Supervisor`. Rolling your own means `Task.Supervisor.async_nolink/3`.

```elixir
# Dangerous: blocks the LiveView process for the duration of the query
def handle_event("search", %{"q" => q}, socket) do
  results = Search.run(socket.assigns.current_scope, q)
  {:noreply, assign(socket, results: results)}
end

# Safe: assign_async tracks :loading / :ok / :failed for you.
# Copy what the closure needs out of socket first — capturing socket sends every assign.
def handle_event("search", %{"q" => q}, socket) do
  scope = socket.assigns.current_scope
  {:noreply, assign_async(socket, :results, fn -> {:ok, %{results: Search.run(scope, q)}} end)}
end

# Safe: start_async when the result is not itself an assign.
# Write BOTH clauses — an unmatched {:exit, _} crashes the LiveView you were protecting.
def handle_event("export", _params, socket) do
  scope = socket.assigns.current_scope
  {:noreply, start_async(socket, :export, fn -> Reports.build(scope) end)}
end

def handle_async(:export, {:ok, url}, socket), do: {:noreply, redirect(socket, external: url)}
def handle_async(:export, {:exit, r}, socket), do: {:noreply, put_flash(socket, :error, inspect(r))}
```

For work the user can supersede — a search box firing on every keystroke, a filter change — **reuse the same async name**: LiveView already discards the earlier result when a later `start_async` under that name is in flight, so a slow first query cannot overwrite a fresh one. `cancel_async/3` is for stopping the superseded task's actual work (the DB query, the HTTP call) rather than for ordering. Use a different name per operation and you lose the built-in ordering guarantee.

### 3. Ecto N+1 — unloaded associations raise, they do not silently query

Accessing an unloaded association returns `%Ecto.Association.NotLoaded{}`; it does not transparently issue a query the way ActiveRecord does. AI writes loops over returned structs and ships code that either fails at render or issues N roundtrips when something preloads lazily inside the loop.

- **Preload what you will access**, either in the query (`from u in User, preload: [:posts]`) or explicitly (`Repo.preload(users, :posts)`).
- **`join` + `preload` in one query** when you also filter on the association — otherwise preload issues a second query.
- **In LiveView, preload in `mount/3` or in the context function that fills the assign.** Never touch an association during render.

```elixir
# Raises %Ecto.Association.NotLoaded{} or issues N queries
users = Repo.all(User)
Enum.map(users, & &1.posts)

# Correct
users = Repo.all(from u in User, preload: [:posts])
users = User |> Repo.all() |> Repo.preload(:posts)
```

Two Ecto API notes that change what new code should look like: `Repo.transaction/2` was **soft-deprecated in Ecto 3.13 in favour of `Repo.transact/2`**, which takes a function returning `{:ok, value}` or `{:error, reason}` and rolls back on `{:error, _}` without `Repo.rollback/1`. `Repo.all_by/3` (3.13) replaces the `Repo.all(from x in X, where: ...)` boilerplate for simple filters. Ecto 3.14 also requires **Decimal v3** — that is a transitive-dependency break, not an opt-in.

### 4. Ecto migration safety on populated tables

AI writes migrations as if the table is empty. In production, `ALTER TABLE` takes an ACCESS EXCLUSIVE lock and blocks reads and writes.

- **Adding an index:** `create index(:table, [:col], concurrently: true)` plus `@disable_ddl_transaction true` and `@disable_migration_lock true` — `CONCURRENTLY` cannot run inside a transaction. **If a concurrent index build fails it leaves an INVALID index behind**, which costs write time and is never used by the planner. Check `pg_index.indisvalid` after a failed deploy, then `drop index(:table, [:col])` and retry, or `REINDEX INDEX CONCURRENTLY`. **Do not reach for `create_if_not_exists` as the recovery** — `IF NOT EXISTS` matches on name only, so it silently skips and leaves the invalid index in place forever.
- **Adding a NOT NULL column:** Postgres 11 and later store a *non-volatile* default in the catalog with no rewrite — and that includes `now()`, which is `STABLE`, not volatile. The rewrite comes from a genuinely **volatile** default: `gen_random_uuid()`, `clock_timestamp()`, `random()`. The safe sequence: (1) add the column nullable, (2) backfill in batches, (3) `ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID`, (4) `VALIDATE CONSTRAINT` in a later step — that takes only SHARE UPDATE EXCLUSIVE, (5) `SET NOT NULL`. Postgres 12 and later use the validated constraint to skip the `SET NOT NULL` scan; confirm your server version before relying on that.
- **Renaming a column or table:** never rename while old code is still running. Expand/contract — add the new column, dual-write, migrate reads, drop the old column in a later deploy.
- **Backfilling:** never one unbounded `UPDATE`. It locks every row and fills WAL. Batch with `Repo.update_all` over a bounded id range, and put the backfill in its own migration with `@disable_ddl_transaction true` so each batch commits.

```elixir
# Dangerous: ACCESS EXCLUSIVE lock on :orders for the whole build
def change, do: create(index(:orders, [:status]))

# Safe: concurrent build, outside a transaction and outside the migration lock
@disable_ddl_transaction true
@disable_migration_lock true
def change, do: create(index(:orders, [:status], concurrently: true))
```

### 5. PubSub fan-out — don't broadcast large payloads to many subscribers

`Phoenix.PubSub.broadcast/3` delivers to every subscriber of a topic, and each subscriber is a separate process. Broadcasting a full Ecto struct with nested preloads copies that data into every subscriber's heap at once — see the per-process heap rules in `mir-backend-beam`.

- **Broadcast an event name and an ID**, not the struct. Each LiveView refetches exactly what it renders, scoped to its own user.
- **High fan-out** (thousands of subscribers on one topic) wants partitioned topics, or `:persistent_term` for data every subscriber reads identically.
- **Don't broadcast from inside `handle_event`** in a way that cascades re-renders across every connected client per keystroke. Debounce or batch.

### 6. Channels — bounded joins, bounded state, bounded pushes

Channels are stateful processes, one per joined topic per client. The LiveView rules apply: don't store large collections in socket assigns, don't block `handle_in/3`.

- **`push/3` is not flow-controlled.** Rapid pushes back up in the client's receive buffer. For live feeds, throttle server-side or send deltas.
- **`handle_in/3` is serialized for that socket** — offload slow work exactly as in a LiveView.
- **Cap channel joins.** A single transport connection could join unlimited channels until **CVE-2026-56811**; the fix added `max_channels_per_transport`, which now defaults to **100**. If you raised it, you re-opened the memory-exhaustion path.

### 7. Idempotent events — guard against double-clicks and duplicate deliveries

A user who sees no immediate response clicks again. LiveView does not deduplicate events. Anything that writes or charges must be idempotent.

- **A unique constraint in the database is the only real guard** — an idempotency key column with a unique index, checked via `Ecto.Changeset.unique_constraint/3`.
- **Track in-flight state in assigns** and pattern-match the duplicate away.
- **`phx-disable-with` is UI only.** A second browser tab, a websocket reconnect, or a crafted event bypasses it entirely.

```elixir
# already in flight — drop the duplicate
def handle_event("submit_order", _params, %{assigns: %{submitting: true}} = socket),
  do: {:noreply, socket}

def handle_event("submit_order", params, socket) do
  scope = socket.assigns.current_scope
  {:noreply,
   socket
   |> assign(submitting: true)
   |> start_async(:place_order, fn -> Orders.place(scope, params) end)}
end

# Every path must clear :submitting, including the crash path — miss one and the
# button is dead for the rest of the session.
def handle_async(:place_order, {:ok, {:ok, order}}, socket),
  do: {:noreply, assign(socket, submitting: false, order: order)}

def handle_async(:place_order, {:ok, {:error, cs}}, socket),
  do: {:noreply, assign(socket, submitting: false, form: to_form(cs))}

def handle_async(:place_order, {:exit, _}, socket),
  do: {:noreply, socket |> assign(submitting: false) |> put_flash(:error, "Try again")}
```

### 8. Phoenix 1.8 scopes — context functions take the scope first

Phoenix 1.8 added **scopes** to the generators so that, in the framework's words, secure data access is "the *default*, not something you remember (or forget) to do later." `mix phx.gen.auth` generates a `Scope` struct, and `mix phx.gen.{context,html,live,json}` thread it as the first argument to every context function.

```elixir
defmodule MyApp.Accounts.Scope do
  defstruct user: nil
  def for_user(%User{} = user), do: %__MODULE__{user: user}
  def for_user(nil), do: nil
end

def list_posts(%Scope{} = scope) do
  Repo.all(from post in Post, where: post.user_id == ^scope.user.id)
end
```

Two ways AI breaks this: writing new context functions without the scope argument because the older tutorial shape is more common in training data, and taking the scope but not using it in the query. A context function whose signature accepts a `%Scope{}` and whose query does not reference it is worse than one that never took it — it reads as if it is scoped.

## How this slots into the core pipeline

- **Gate 5 (Design):** state the LiveView memory strategy (streams vs `temporary_assigns` vs pagination), transaction boundaries for Ecto mutations (`Repo.transact/2`, `Ecto.Multi`), the idempotency mechanism and where its key lives, and the scope that every context function will enforce. The migration plan must name concurrency and expand/contract for anything destructive.
- **Gate 6 (Implementation):** code against items 1–8. Every `handle_event` that does I/O offloads via `start_async`/`assign_async`. Every query that renders associations preloads. Every index migration is concurrent. Every `handle_event` re-checks authorization (see Security).
- **Gate 7 (Review):** the reliability-reviewer checks items 1–8; the migration-reviewer audits for missing `concurrently`, missing `@disable_ddl_transaction`, unbatched backfills and unsafe `SET NOT NULL`; the security-reviewer walks the Security section below. Run `mix sobelow` and `mix deps.audit` as part of this gate.

## Security

Phoenix / LiveView / Ecto / Plug mechanics only. VM-level items — `binary_to_term`, atom exhaustion, distribution cookie and EPMD, `:os.cmd`, SSRF from HTTP clients, OTP TLS advisories, Hex lockfile and `mix deps.audit` — are in `mir-backend-beam`.

### LiveView events are a public endpoint

This is the single most common Phoenix authorization defect, because the code *looks* protected. A LiveView renders a page; whatever buttons the template omits appear to be unreachable. They are not. The client holds an open websocket and can send **any event name with any payload at any time**, regardless of what was rendered. The LiveView docs are blunt about it: "A savvy user can directly talk to the server and request a deletion anyway. For this reason, **you must always verify permissions on the server**."

- **`on_mount` runs once, at mount — not per event.** `live_session :authenticated, on_mount: [{MyAppWeb.UserAuth, :require_authenticated}]` establishes who the user is when the LiveView starts. It does nothing when, twenty minutes later, that same socket sends `"delete_invoice"` with someone else's id. Authorization rules belong in `mount/3` (may they see this page), `handle_params/3` (may they navigate here), **and `handle_event/3` (may they do this, to this record, right now)**.
- **`phx-value-*` and every key in the event params are attacker-controlled.** `handle_event("delete", %{"id" => id}, socket)` must load through the scope, not by bare id.
- **Know which navigation re-runs your hooks.** `push_patch/2` stays in the current LiveView and only calls `handle_params/3` — `mount/3` and `on_mount` do **not** run again, so a patch to a different record is authorized by `handle_params/3` or by nothing. `push_navigate/2` dismounts and mounts the destination, re-running `mount/3` and the `live_session`'s `on_mount`. Crossing a `live_session` boundary is a full page reload. Two pages with different permission requirements belong in different `live_session`s — but the per-event check below is what actually holds, because none of this runs between events.
- **Permission changes do not reach a live socket.** Revoking a role leaves the connected LiveView running under the old assigns. `mix phx.gen.auth` handles this by putting `live_socket_id` in the session and exposing `disconnect_sessions/1`, which broadcasts `"disconnect"` on that topic; the client reconnects and re-runs `mount/3`. Call it from your role-revocation and logout paths, not just from password change.

```elixir
# WRONG — mount checked who they are; this event never checks what they own
def handle_event("delete", %{"id" => id}, socket) do
  Invoices.get_invoice!(id) |> Invoices.delete_invoice()
  {:noreply, socket}
end

# RIGHT — scope the load; a wrong id is a NoResultsError, not a deletion
def handle_event("delete", %{"id" => id}, socket) do
  scope = socket.assigns.current_scope
  invoice = Invoices.get_invoice!(scope, id)      # scoped query, raises on miss
  {:ok, _} = Invoices.delete_invoice(scope, invoice)
  {:noreply, stream_delete(socket, :invoices, invoice)}
end
```

This is not hypothetical. **CVE-2026-48592** (GHSA-389x-rgxr-8m33, Medium, 30 Jun 2026) is exactly this bug in `oban_web`: a missing authorization check on the save-job event handler, affecting 2.12.0 through 2.12.4, fixed in **2.12.5**. A mature library with a small event surface still shipped it.

The same rule covers `handle_info/2` from PubSub — a message on a topic is not proof the receiving socket is allowed to see its contents. Re-check before assigning.

### Object-level authorization (IDOR) in contexts

`mix phx.gen.auth` gives you authentication — sessions, tokens, `on_mount(:mount_current_scope)`, `on_mount(:require_authenticated)`, `on_mount(:require_sudo_mode)`, and the matching plugs. It gives you **no authorization layer**. Deciding who may read or mutate a given row is entirely yours.

```elixir
# WRONG — any authenticated user reads any order by guessing an integer id
def get_order!(id), do: Repo.get!(Order, id)

# RIGHT — ownership is part of the query, so a wrong id cannot return a row
def get_order!(%Scope{} = scope, id) do
  Repo.get_by!(Order, id: id, user_id: scope.user.id)
end
```

- Scope in the query, not in an `if` after the load. A post-load check is one early return away from being skipped.
- Sequential integer primary keys make enumeration trivial. Ecto 3.14 added **UUIDv7 helpers to `Ecto.UUID`** — note that UUIDv7 is time-ordered, so it hides the row count but not the creation order. Use `:binary_id` primary keys for anything exposed in a URL, and still scope the query.
- Multi-tenant: enforce tenancy in the query or with Postgres row-level security. `Ecto.Repo` `prefix:` and a `default` filter in a single helper function are both bypassed the moment someone writes `Repo.all(Schema)` directly.
- `on_mount(:require_sudo_mode)` (and the `require_sudo_mode/2` plug) re-checks a recent authentication before sensitive actions. Use it for email/password changes and destructive admin actions; it is generated but not wired to anything by default.

### Mass assignment

`Ecto.Changeset.cast/4` is the allow-list, and it works — as long as the list is written by hand.

```elixir
# WRONG — every field is castable, including :role and :account_balance
def changeset(user, attrs), do: cast(user, attrs, __MODULE__.__schema__(:fields))

# WRONG — change/2 does no filtering at all; it writes whatever it is given
def changeset(user, attrs), do: Ecto.Changeset.change(user, attrs)

# RIGHT — enumerate, and keep privileged fields in a separate changeset
def changeset(user, attrs) do
  user |> cast(attrs, [:name, :email]) |> validate_required([:name, :email])
end

def admin_role_changeset(user, attrs), do: cast(user, attrs, [:role])
```

- **`cast_assoc/3` and `cast_embed/3` extend the allow-list into nested params.** A nested changeset that casts `:role` reintroduces the hole one level down. `:on_replace` also decides whether a client can *delete* associated rows by omitting them.
- **`Repo.insert_all/3` and `Repo.update_all/3` bypass changesets entirely.** Never hand them a map built from request params.
- Ecto 3.13 added a **`:writable` field option** (`:always`, `:insert`, `:never`) and 3.14 added **`:on_writable_violation`** so a non-writable field can raise instead of being silently dropped. Use `writable: :insert` for fields that must never change after creation, and set the violation behaviour explicitly rather than accepting the silent default.

### SQL injection — what Ecto parameterizes and what it does not

Ecto's query DSL parameterizes everything, and `fragment/1` only accepts a compile-time literal string, so the obvious hole is closed. These are the ones that are open:

```elixir
# WRONG
Ecto.Adapters.SQL.query!(Repo, "SELECT * FROM users WHERE email = '#{email}'")
Postgrex.query!(conn, "SELECT * FROM t WHERE id = #{id}", [])
from(o in Order, order_by: ^String.to_atom(params["sort"]))   # + atom exhaustion

# RIGHT
Ecto.Adapters.SQL.query!(Repo, "SELECT * FROM users WHERE email = $1", [email])
sort = Map.fetch!(%{"date" => :inserted_at, "total" => :total_cents}, params["sort"])
from(o in Order, order_by: ^sort)
```

- Any dynamic column, table, or direction must come from an allow-list map keyed by the request value. `String.to_atom` on a sort parameter is both an injection risk and a node-level DoS (see atom exhaustion in `mir-backend-beam`).
- `like`/`ilike` need the user's `%` and `_` escaped as well as parameterized, or a search box becomes a full table scan.
- **Three 2026 Postgrex advisories are injections in the driver itself**, all reachable from application code that looked parameterized: **CVE-2026-32687** (High) — channel-name SQL injection in `Postgrex.Notifications.listen/3`, which is what `Phoenix.PubSub.Postgres` and Oban use, fixed after **0.22.2**; **CVE-2026-66838** (Medium) — SQL injection via the `:comment` option in `Postgrex.stream/4`, fixed in **0.22.4**; **CVE-2026-58225** (Low) — unescaped `$$` dollar-quote in the reconnect `DO` block, fixed in **0.22.3**. If any topic or comment string is user-derived, be on 0.22.4.

### XSS and open redirect in HEEx and LiveView

- HEEx escapes `{@value}` and `<%= value %>` automatically. The holes are **`raw/1`**, `Phoenix.HTML.raw/1`, and any struct that implements `Phoenix.HTML.Safe` to return unescaped content. Never `raw` user-supplied HTML; sanitize server-side into a fixed allow-list of tags if rich text is a requirement.
- **Attribute interpolation into `href`/`src` is not covered by HTML escaping.** `javascript:` and `data:` URLs pass through escaping untouched. Validate the scheme.
- **CVE-2026-58228** (GHSA-5cgh-g58j-m9cq, Medium, 13 Jul 2026) — a scheme validation bypass in `Phoenix.LiveView.Utils` allowed XSS through `<.link>`, affecting 1.2.2 through 1.2.6, fixed in **1.2.7**.
- **CVE-2026-64941** (GHSA-36m4-rm57-3prf, 10 Aug 2026) — open redirect in `validate_local_url!` via ASCII tab, LF and CR, which is the validation behind `push_navigate/2`, `push_patch/2` and `<.link navigate={...}>`. Affects the 1.0, 1.1 and 1.2 lines; fixed in **1.0.19 / 1.1.33 / 1.2.9**. A "return to" parameter taken from the query string is the default path into it.
- Never build a redirect target from request data without matching it against known routes. `~p` verified routes give you a compile-checked path; use them instead of string concatenation.

### CSRF, SameSite, and the `:api` pipeline hole

- The generated `:browser` pipeline includes `plug :protect_from_forgery` and `plug :put_secure_browser_headers`. **The generated `:api` pipeline is `plug :accepts, ["json"]` and nothing else** — no session, no CSRF, no security headers. That is correct for bearer-token APIs. It is a hole the moment someone adds `fetch_session` to an API-only app to "make login work," which is a very common AI-generated shape: cookie authentication with zero CSRF protection.
- The generated `@session_options` sets `same_site: "Lax"`. Dropping it to `"None"` for a cross-site embed removes the browser-side protection and leaves the CSRF token as the only defence — never combine `"None"` with a pipeline that skips `protect_from_forgery`.
- Bearer-token endpoints do not need CSRF. Cookie-authenticated endpoints always do.
- Webhook endpoints that skip CSRF must verify a provider signature (Stripe `Stripe-Signature`, GitHub `X-Hub-Signature-256`) with `Plug.Crypto.secure_compare/2`, and must read the **raw body** — configure a `body_reader` that caches it, because `Plug.Parsers` consumes the body before your controller sees it.

### `check_origin` — the setting `config/dev.exs` ships as `false`

The generated `config/dev.exs` contains `check_origin: false`, `debug_errors: true`, and a literal `secret_key_base` committed to the repository. All three are right for development and wrong everywhere else. `check_origin: false` is the one that gets copied most, because it is the fastest way to silence a websocket connection error in staging. Phoenix's documentation: "If `false` and you do not validate the session in your socket, your app is vulnerable to Cross-Site WebSocket Hijacking (CSWSH) attacks. Only use in development, when the host is truly unknown or when serving clients that do not send the `origin` header, such as mobile apps."

For a mobile client that genuinely sends no `Origin`, do not disable the check globally — validate the session inside `connect/3` and reject unauthenticated sockets there. The option also accepts a list of allowed origins and `:conn`.

### CORS

Phoenix ships no CORS handling; projects add `cors_plug` or `corsica`.

- `origin: "*"` combined with credentials is rejected by browsers. The usual "fix" is reflecting the request `Origin` header back, which makes every website a trusted origin with cookies attached. Do not.
- List exact origins. If the list must be dynamic, match against stored values — never with `String.starts_with?/2` or a regex missing `\A`/`\z`, because `https://app.example.com.attacker.net` passes both.

### Session cookies: signed, not encrypted

The generated endpoint says so in a comment above `@session_options`: the session "will be stored in the cookie and signed, this means its contents can be read but not tampered with. Set `:encryption_salt` if you would also like to encrypt it." The generator sets `signing_salt` and `same_site: "Lax"`, and no `encryption_salt`.

- **Anything you put in the session is readable by the client.** Never put a role, a feature flag that gates access, an internal id you treat as secret, or PII in a signed-only session. Add `encryption_salt:` if the contents must be confidential.
- **`Plug.Session.COOKIE` defaults to `serializer: :external_term_format`** — that is `term_to_binary`/`binary_to_term` under the signature. Plug decodes with `non_executable_binary_to_term`, so it is not a direct fun-execution path, but a leaked `secret_key_base` becomes attacker-controlled term decoding, and the term decoder has its own 2026 CVEs (see `mir-backend-beam`). Rotate `secret_key_base` if it leaks; that invalidates all sessions and signed tokens, which is the point.
- `secret_key_base` must be read from the environment in `config/runtime.exs`. `mix phx.gen.secret` generates one. Never commit a production value.

### File serving and uploads

- **`Plug.Static` is generated with `only: MyAppWeb.static_paths()`.** Widening that to `from: "priv"` or serving a user-writable directory exposes whatever lands there. Keep the allow-list.
- **`send_file/3` and `send_download/3` with request-derived paths are path traversal.** Look the record up by id (scoped) and serve its stored path. If you must build a path from a name, gate it through `Path.safe_relative/2` and refuse on `:error`.
- **LiveView `allow_upload/3` `accept:` matches extensions and client-declared MIME types.** Both come from the client. The docs are explicit: "While client metadata cannot be trusted, max file size validations are enforced as each chunk is received when performing direct to server uploads." So `max_file_size` and `max_entries` are real limits; `accept` is a UI filter. Sniff the content server-side in `consume_uploaded_entries/3` before storing.
- **`entry.client_name` is attacker-controlled** — never use it as a filename, never `Path.join` it, never let it choose an extension that selects a processing handler. Generate the stored name; keep the client name as a display label only.
- Serve untrusted uploads from a separate host or with `Content-Disposition: attachment`. An uploaded SVG or HTML file served inline from your origin is stored XSS.
- **CVE-2026-56814** (Medium, 9 Jul 2026): Plug's multipart `:length` limit **was not charged for file uploads** — the upload size cap you configured was not enforced for the file parts it existed to bound. Fixed in **1.16.6 / 1.17.4 / 1.18.5 / 1.19.5 / 1.20.3**. Also note the generated endpoint configures `Plug.Parsers` with `pass: ["*/*"]` and **no `:length`**, so the limit is Plug's 8 MB default unless you set it. Cap upload size at the proxy as well.

### Secret and PII leakage

- **`config :phoenix, :filter_parameters` defaults to `["password"]`.** That is the entire default list. `token`, `api_key`, `secret`, `authorization`, `credit_card`, `ssn`, `otp` are all written to your logs in full. Set the list explicitly, or invert it: `config :phoenix, :filter_parameters, {:keep, ["id", "page"]}` filters everything except the named keys, which fails closed when someone adds a new field.
- **Ecto struct fields are printed by `inspect`, which appears in logs, crash reports and exception messages.** Use `field :password_hash, :string, redact: true`, and `@schema_redact :all_except_primary_keys` (Ecto 3.13) on schemas holding regulated data.
- **`debug_errors: true` and `Plug.Debugger` render source lines, local variables and the full stack trace in the response body.** They are dev-only in the generated config; the failure is a staging environment built by copying `dev.exs`.
- **Returning a changeset directly as JSON leaks your schema** — field names, constraint names, and the exact validation that failed on fields the client never sent. Map errors to a fixed, public shape.
- Log the stack trace with a correlation ID (`Plug.RequestId` is in the generated endpoint); return the ID, not the trace.
- **`Phoenix.LiveDashboard` shows processes, ETS contents, and application env — including secrets.** The generator correctly puts it behind `if Application.compile_env(:my_app, :dev_routes)`. Moving it into production requires real authentication in front of it, not obscurity.

### Settings that ship insecure by default, named

| Where | Setting | Why it matters |
|---|---|---|
| `config/dev.exs` | `check_origin: false` · `debug_errors: true` · literal `secret_key_base` | CSWSH; source, locals and stack traces in responses; a committed signing key |
| Router | `:api` pipeline has no CSRF, no session, no security headers | Correct for bearer tokens, a hole the moment cookies are added |
| Endpoint | `@session_options` has `signing_salt` but no `encryption_salt` | Session contents are readable by the client |
| Endpoint | `Plug.Session.COOKIE` `serializer: :external_term_format` | A leaked `secret_key_base` becomes term decoding |
| Endpoint | `Plug.Parsers` with `pass: ["*/*"]` and no `:length` | Upload bound is Plug's default, not yours |
| App config | `:filter_parameters` default `["password"]` | Tokens and PII logged in full |
| Generators | `mix phx.gen.auth` provides authentication only | No authorization; scopes must be used in every query |

### Current advisories on the default path

Every row below is reachable without any unusual configuration. Patch floors verified 13 Aug 2026.

| Advisory | Package | What it does | Fixed in |
|---|---|---|---|
| CVE-2026-32689 | phoenix | Long-poll NDJSON body splitting causes a large memory allocation. The generated endpoint enables the `longpoll` transport alongside `websocket`. | 1.7.22 / 1.8.6 |
| CVE-2026-56811 | phoenix | Unbounded channel joins per transport — a few connections exhaust memory. Fix added `max_channels_per_transport`, default 100. | 1.5.15 / 1.6.17 / 1.7.24 / 1.8.9 |
| CVE-2026-56812 | phoenix (hex **and** npm) | Presence keys colliding with `Object.prototype` members break existence checks in the JS client. Bump the npm `phoenix` package too. | 1.5.15 / 1.6.17 / 1.7.24 / 1.8.9 |
| CVE-2026-58228 | phoenix_live_view | Scheme validation bypass in `Phoenix.LiveView.Utils` → XSS via `<.link>`. | 1.2.7 |
| CVE-2026-64941 | phoenix_live_view | Open redirect in `validate_local_url!` via ASCII tab, LF and CR — affects `push_navigate`, `push_patch`, `<.link navigate=>`. | 1.1.33 / 1.2.9 |
| CVE-2026-8468 | plug | Unbounded buffer accumulation in multipart header parsing → DoS. | 1.15.4 / 1.16.3 / 1.17.1 / 1.18.2 / 1.19.2 |
| CVE-2026-54892 | plug | Quadratic-time decoding of nested query and body parameters → DoS from one request. | 1.15.5 / 1.16.4 / 1.17.2 / 1.18.3 / 1.19.3 |
| CVE-2026-56814 | plug | Multipart `:length` limit not charged for file uploads. | 1.16.6 / 1.17.4 / 1.18.5 / 1.19.5 / 1.20.3 |
| CVE-2026-56813 | plug | Cookie attribute injection — `Plug.Conn.Cookies.encode/2` interpolated attribute values unescaped. | 1.16.6 / 1.17.4 / 1.18.5 / 1.19.5 / 1.20.3 |
| CVE-2026-32687 · 58225 · 66838 | postgrex | SQL injection in `Postgrex.Notifications.listen/3` (channel name), the reconnect `DO` block (`$$`), and `Postgrex.stream/4` (`:comment`). | 0.22.4 |
| CVE-2026-39803 · 39804 · 39805 · 39806 · 39807 · 42786 · 42788 | bandit (the default adapter) | Seven unauthenticated issues: DoS via `Transfer-Encoding: chunked` and via chunked trailers; memory exhaustion via WebSocket continuation buffering, a single-frame inflate bomb, and an HTTP/2 frame-size-limit bypass; CL.CL request smuggling on duplicate `Content-Length`; trusting a client-supplied URI scheme on plaintext connections. | 1.11.0, and 1.11.1 for the trailers and chunked DoS |
| CVE-2026-48592 | oban_web | Missing authorization check on the save-job event handler — the LiveView `handle_event` failure described above, in a shipped library. | 2.12.5 |

The current stable releases (Phoenix 1.8.11, LiveView 1.2.9, Plug 1.20.3, Bandit 1.12.4, Postgrex 0.22.4) are above every floor in this table. Run `mix deps.audit` in CI so the next batch does not need a human to notice it.

## Edit boundary (what belongs here vs. the core)

**This module holds ONLY one library's mechanics — Phoenix · LiveView · Ecto · Plug · PostgreSQL · PubSub.** Apply the 3-tier placement test before adding anything:

- True for Go/Node/Java too (idempotency, invariants, gates, risk register)? → **generic core** (`mir-backend`).
- True for every BEAM backend (supervision design, mailbox growth, hot GenServer, ETS, distributed Erlang, `binary_to_term`, atom exhaustion, Hex supply chain)? → **runtime tier** (`mir-backend-beam`).
- A mechanical footgun of *this library* (LiveView assign memory, `assign_async`/`start_async`, `handle_event` authorization, Ecto N+1 and changeset allow-lists, migration `concurrently`, PubSub payload size, `check_origin`, `filter_parameters`)? → **here**.
- A *different* framework on BEAM (plain OTP application, Nerves) → new `mir-backend-beam-<framework>` module. A *different* runtime → its own tier. Never widen this one.
