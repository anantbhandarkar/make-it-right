---
name: mir-backend-beam
description: "Make It Right (BEAM runtime tier). Erlang VM / BEAM reliability footguns shared across every BEAM-based backend (Phoenix, Nerves, pure Erlang) — distinct from the generic backend gates and from any one framework's mechanics. Covers: let-it-crash + supervision tree design (restart strategies, poison-message crash-loops), unbounded mailbox growth and backpressure (GenStage/Broadway/synchronous call), hot GenServer as a serial bottleneck and when to use ETS instead, blocking handle_call timing out callers and queuing all others, isolated per-process heap and how to share state (messages / ETS / persistent_term), and distributed Erlang hazards (netsplits, :global name clashes, pairwise ordering). TRIGGER when the backend runtime is Elixir or Erlang — sits between mir-backend (generic) and the framework module (e.g. mir-backend-beam-phoenix). SKIP for Python, Node, Go, JVM, Rust, .NET, Ruby, PHP runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics (those live in the framework module)."
trigger: /mir-backend-beam
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-beam · Make It Right (BEAM runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-beam-phoenix`) knows the **library's mechanics**. This tier owns what's true for **all BEAM backends because they run on the Erlang VM** — the process model, message-passing concurrency, and fault-tolerance primitives that Phoenix, Nerves, and plain Erlang all inherit.

**Runtime assumed:** Elixir 1.16+ on OTP 26+ (the notes hold for Erlang directly; syntax examples are Elixir). Load order: `mir-backend` → `mir-backend-beam` → `<framework module>`.

## The BEAM footguns AI walks into (framework-agnostic)

### 1. Let-it-crash + supervision — design the tree, don't rescue everything

The BEAM's fault-tolerance story is built on supervision: a crashed worker process is restarted by its supervisor without taking down the rest of the system. AI frequently undermines this by wrapping every operation in `try/rescue` or `try/catch`, converting clean crashes into silently swallowed errors that leave the process in a corrupt-but-alive state.

- **Let unexpected errors crash the process.** A supervisor restart resets state cleanly. A rescued exception that continues execution with invalid state is harder to detect and recover from.
- **Design supervision trees intentionally.** Choose the right restart strategy: `one_for_one` (worker failures are independent), `one_for_rest` (failure in one cascades to later-started siblings — use for ordered startup dependencies), `rest_for_one` (same but only downstream siblings), `one_for_all` (all workers are interdependent; any failure restarts the group).
- **Know what SHOULD NOT crash-loop.** A poison message — a message that always causes the GenServer to crash before it can `ack` or dequeue — will cause a supervisor to restart the worker, which immediately receives the same message and crashes again. `max_restarts` / `max_seconds` limits will eventually take down the supervisor itself. Fix: validate/discard unknown messages with a catch-all `handle_info/2` clause or use a dead-letter strategy; do NOT silently rescue crashes.

```elixir
# Dangerous: GenServer crashes on bad message -> supervisor restarts -> same message -> loop
def handle_info({:process, item}, state) do
  result = dangerous_work!(item)   # raises on malformed item
  {:noreply, %{state | last: result}}
end

# Safe: catch-all discards the poison message; log and move on
def handle_info({:process, item}, state) do
  result = dangerous_work!(item)
  {:noreply, %{state | last: result}}
end
def handle_info(unknown, state) do
  Logger.warning("Unexpected message: #{inspect(unknown)}")
  {:noreply, state}
end
```

### 2. Unbounded mailbox growth — backpressure or bust

Every BEAM process has a mailbox (an in-process queue). If a GenServer receives messages faster than it processes them, the mailbox grows without bound — consuming memory and increasing tail latency for every caller waiting in line, until the node runs out of memory or the process is killed.

- **Monitor** `Process.info(pid, :message_queue_len)` in production; alert when it grows consistently.
- **Apply backpressure at the source.** Use `GenServer.call/3` (synchronous) instead of `cast/2` (fire-and-forget) for flow control — `call` blocks the caller until the server processes the message, naturally throttling producers.
- **For high-throughput pipelines**, use **GenStage** (demand-driven producer/consumer), **Broadway** (opinionated pipeline with back-pressure built-in, ideal for consuming message queues like SQS/RabbitMQ), or **Flow** (parallel data processing with GenStage underneath).
- **Never use `cast` for work that must be bounded** — it's appropriate only when losing a message is acceptable (e.g., best-effort metrics) or when the producer is itself rate-limited by external means.

```elixir
# Unbounded: producer can flood the GenServer
GenServer.cast(worker, {:process, item})

# Bounded: caller blocks until GenServer is ready — natural backpressure
GenServer.call(worker, {:process, item})

# Pipeline: Broadway handles backpressure, retries, and acking automatically
defmodule MyPipeline do
  use Broadway
  def handle_message(_, message, _) do
    # process one message; Broadway controls concurrency and demand
    message
  end
end
```

### 3. Hot GenServer = serial bottleneck — use ETS for shared concurrent reads

A GenServer processes **one message at a time**. Routing high-read-throughput traffic through a single GenServer serializes all reads through that one process, turning what should be parallel into a queue. Under load, callers time out and the mailbox blooms.

- **Identify the access pattern first.** If many callers only need to *read* shared data (a config table, a registry, a rate-limit counter snapshot), a GenServer is the wrong tool.
- **Use ETS for concurrent reads.** ETS tables support many concurrent readers with no locking. A GenServer can still *own* the ETS table (for writes and ownership semantics), but readers hit ETS directly without going through the GenServer process.

```elixir
# Bottleneck: every read is serialized through one process
def handle_call(:get_config, _from, state) do
  {:reply, state.config, state}
end

# Scalable: write via GenServer, read directly from ETS (zero process hop)
def init(_) do
  :ets.new(:config_cache, [:named_table, :public, read_concurrency: true])
  {:ok, %{}}
end

def handle_cast({:update_config, config}, state) do
  :ets.insert(:config_cache, {:config, config})
  {:noreply, state}
end

# Callers read directly — no GenServer roundtrip
def get_config, do: :ets.lookup(:config_cache, :config)
```

Also consider `persistent_term` for large, rarely-changing global data (e.g., compiled regex, static config loaded at startup) — it has extremely fast reads with no ETS lookup overhead, but writes are expensive (global GC pause).

### 4. Long work in handle_call blocks callers and times out

`GenServer.call/3` has a default 5-second timeout. Any expensive computation or blocking I/O inside `handle_call/3` holds the GenServer — every other caller queues up behind it. When the timeout fires, the caller crashes (or returns `{:error, :timeout}` if caught), but the server continues processing the slow message and may try to reply to a dead process.

- **Never do heavy work inside a shared GenServer's call handler.** Options:
  - Do the work **in the caller** (if it doesn't need the server's state).
  - Delegate to a `Task` or `Task.Supervisor` spawned from the server, reply asynchronously via `handle_info`.
  - Use `GenServer.cast` + a separate notification mechanism when the result can be delivered out-of-band.
  - Use a **pool** (e.g., `Poolboy`, `NimblePool`) so many workers process in parallel.

```elixir
# Dangerous: blocks the GenServer for the duration of the HTTP call
def handle_call({:fetch, url}, _from, state) do
  result = HTTPoison.get!(url)   # could take seconds
  {:reply, result, state}
end

# Safe: offload to a Task; the GenServer stays responsive
def handle_call({:fetch, url}, from, state) do
  Task.start(fn ->
    result = HTTPoison.get!(url)
    GenServer.reply(from, result)
  end)
  {:noreply, state}
end
```

### 5. State is isolated per process — no shared memory

BEAM processes share **nothing**. Each process has its own heap; passing data between processes copies it (unless using ETS or shared binaries via the binary heap). This is not a bug — it's what makes crash isolation safe — but it means:

- **In-process state is lost when the process crashes** (and is reset to `init/1` state after restart). Never store durable state only in a GenServer's `state` map — persist to a DB or ETS (with a backing store) if it must survive crashes.
- **Large data passed in messages is copied** for each recipient. For large binaries (> 64 bytes), BEAM uses a reference-counted binary heap, so they are passed by reference — but structured data (maps, lists) is always copied. Avoid broadcasting large maps to many processes.
- **Processes are cheap** — spawn them liberally for isolated units of work. Each holds its own GC-collected heap; there is no stop-the-world GC across all processes.

### 6. Distributed Erlang hazards — netsplits, :global, and ordering

Running a multi-node cluster introduces failure modes beyond a single node:

- **Netsplits create split-brain.** If two nodes lose connectivity, each believes the other is down and may elect its own "leader" or accept writes independently. Use consensus libraries (Raft via `Ra`, or coordinate via the DB) for operations that must be consistent across nodes. Never assume a node is permanently gone just because it's unreachable.
- **`:global` name registration is not partition-safe.** `:global.register_name/2` uses a distributed lock that can create conflicts or inconsistencies during netsplits. Prefer `Horde.Registry` or `Registry` (node-local) + explicit routing for distributed registries.
- **Message ordering is only guaranteed pairwise.** Messages between the *same two processes* arrive in send order. Messages from *different senders* to the same receiver arrive in an arbitrary order — do not assume interleaved messages from multiple senders maintain any relative order.
- **`:rpc.call/4` on a downed node blocks** for the timeout duration — use `Task.Supervisor` with a timeout or `:erpc.call/5` (OTP 23+) which raises immediately if the node is down.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** state the supervision strategy and concurrency model (process-per-connection, pool, pipeline). A missing supervision tree on stateful workers is a runtime-level design defect — flag it. Identify ETS vs GenServer access patterns.
- **Gate 6 (implementation):** never block `handle_call` with slow work; apply backpressure before shipping any cast-based pipeline; match restart strategies to actual failure semantics.
- **Gate 7 (review):** the reliability-reviewer checks items 1–6 here for any Elixir/Erlang service. Verify `message_queue_len` is observable and poison-message handling exists.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates) → **up** to `mir-backend`.
- A specific library's mechanics (Phoenix LiveView assigns, Ecto query patterns, PubSub, channel backpressure) → **down** to the framework module (`mir-backend-beam-phoenix`).
- **Here:** only what every BEAM backend shares because of the Erlang VM — process model, supervision, mailbox dynamics, ETS vs GenServer, distributed Erlang hazards, per-process heap isolation.
- A different runtime (Python, Go, Node…) → its own `mir-backend-<runtime>` tier. Never widen this one.
