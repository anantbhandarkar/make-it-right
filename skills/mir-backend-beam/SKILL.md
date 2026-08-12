---
name: mir-backend-beam
description: "Make It Right (BEAM runtime tier). Erlang VM / BEAM reliability footguns shared across every BEAM-based backend (Phoenix, Nerves, Broadway, pure Erlang/OTP) — distinct from the generic backend gates and from any one framework's mechanics. Covers: let-it-crash + supervision tree design (restart strategies, max_restarts escalation, poison-message crash-loops and why a catch-all handle_info does NOT stop them), unbounded mailbox growth and real backpressure (a GenServer.call timeout does not shed load; GenStage/Broadway; OTP 28 priority messages are not backpressure), hot GenServer as a serial bottleneck and when to use ETS / :counters / :persistent_term instead, blocking handle_call timing out callers, per-process heap isolation and refc-binary retention, distributed Erlang hazards (netsplits, :global, pairwise ordering, :erpc), what Elixir's set-theoretic type checker does and does not catch, and VM-level security (binary_to_term, atom exhaustion, distribution cookie/EPMD, :os.cmd vs System.cmd, OTP TLS/term-decoding advisories, Hex supply chain). TRIGGER when the backend runtime is Elixir or Erlang — sits between mir-backend (generic) and the framework module (mir-backend-beam-phoenix). SKIP for Python, Node, Go, JVM, Rust, .NET, Ruby, PHP runtimes (each has its own mir-backend-<runtime> tier), and for Phoenix / LiveView / Ecto / Plug library mechanics — those load mir-backend-beam-phoenix, not this."
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

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-beam-phoenix`) knows the **library's mechanics**. This tier owns what's true for **all BEAM backends because they run on the Erlang VM** — the process model, message-passing concurrency, and fault-tolerance primitives that Phoenix, Nerves, Broadway, and plain Erlang all inherit.

**Runtime assumed:** Elixir 1.20 on OTP 27–29; syntax examples are Elixir, the notes hold for Erlang directly. Verified 13 Aug 2026: **Elixir 1.20.3** is the current stable line (1.21 is unreleased, in development on `main`); **OTP 29.0.5** (4 Aug 2026) is the current major, with **28.5.0.5** and **27.3.4.16** as maintained lines. Elixir 1.20 requires OTP 27+ and supports up to OTP 29; Elixir 1.19 required OTP 28.1+ to run on the OTP 28 line. If the project is on OTP 26 or older, that is a Gate 4 risk row — the July 2026 security batch below was not backported below 27.3.4.15. Load order: `mir-backend` → `mir-backend-beam` → `<framework module>`.

## The BEAM footguns AI walks into (framework-agnostic)

### 1. Let-it-crash + supervision — design the tree, don't rescue everything

A crashed worker is restarted by its supervisor without taking down the rest of the system. AI undermines this by wrapping every operation in `try/rescue`, converting a clean crash into a swallowed error that leaves the process alive with invalid state.

- **Let unexpected errors crash.** A restart resets state cleanly. A rescued exception that keeps executing with invalid state is harder to detect and harder to recover from.
- **Pick the restart strategy deliberately.** `:one_for_one` (worker failures are independent), `:rest_for_one` (a failure restarts that child and every child started after it — use for ordered startup dependencies), `:one_for_all` (all children are interdependent).
- **Know where the escalation ends.** `Supervisor` defaults to `max_restarts: 3` within `max_seconds: 5`. Exceed that and the supervisor itself terminates and escalates to *its* supervisor. At the top of the tree that stops the whole application. A crash-loop is therefore not "contained" — it is a delayed outage.

**A catch-all `handle_info/2` does not fix a poison message.** This is the most common wrong fix AI writes. A catch-all clause only handles messages that match *no other clause*. A message that matches your real clause and then raises inside it still crashes the process.

Be precise about why that loops, because the fix depends on it: the crashed process and **its mailbox both die** — OTP does not hand the old mailbox to the restarted process. The loop comes from the *source*. An unacknowledged broker message is redelivered to the restarted consumer; a producer that retries re-sends. An in-process sender that fired once and moved on will not.

```elixir
# Dangerous: a malformed item raises, the supervisor restarts the worker,
# the source redelivers the same message, and it crashes again — until
# max_restarts trips and the outage moves up the tree.
def handle_info({:process, item}, state) do
  result = dangerous_work!(item)   # raises on malformed item
  {:noreply, %{state | last: result}}
end

# This clause does NOT help. {:process, bad_item} still matches the clause
# above. A catch-all only covers messages nothing else matched.
def handle_info(unknown, state) do
  Logger.warning("unexpected message: #{inspect(unknown)}")
  {:noreply, state}
end
```

What actually stops the loop, in order of preference:

| Where the message comes from | Fix |
|---|---|
| A broker (SQS, RabbitMQ, Kafka) via Broadway | Let it crash, then use `handle_failed/2` to set the ack action for failed messages (`Broadway.Message.configure_ack/2`, e.g. `on_failure: :reject` on RabbitMQ). `handle_failed/2` only shapes the message before acknowledgement — the **broker** still has to have the dead-letter route configured (an SQS redrive policy, a RabbitMQ DLX). Both halves, or it is redelivered forever |
| A job queue (Oban) | Set `max_attempts` on the worker; after the last attempt the job moves to `discarded` and stops being fetched |
| Another process in your own app | Validate at the boundary and drop or park the bad message explicitly. That is a validation decision with a log line and a metric — not a `rescue` |

Use `significant: true` on a child plus `auto_shutdown:` on its supervisor when a child terminating normally *should* bring the subtree down. Otherwise a "finished" worker leaves an empty supervisor running forever.

### 2. Unbounded mailbox growth — and why a `call` timeout is not backpressure

Every process has a mailbox. If a GenServer receives faster than it processes, the mailbox grows without bound: memory climbs, GC gets more expensive because the mailbox is scanned, and tail latency rises for everyone queued behind.

- **Monitor `Process.info(pid, :message_queue_len)`** and alert on sustained growth. Call `Process.set_label/1` (Elixir 1.17+) in `init/1` so a runaway process is identifiable in `:observer` and in crash reports instead of showing up as a bare pid.
- **`GenServer.call/3` gives you flow control** — the caller blocks until the server replies, which throttles the producer. **But a `call` timeout does not shed load.** The caller gives up; the message stays in the server's mailbox and is still processed. Under overload, timeouts hide the queue instead of draining it. If you need to shed, cap concurrency at the entry point (a pool with a bounded checkout timeout) rather than relying on `call` timeouts.
- **`cast/2` is only appropriate when losing the message is acceptable** (best-effort metrics) or the producer is rate-limited elsewhere. Never use it for work that must be bounded.
- **For pipelines**, use **GenStage** (demand-driven producer/consumer), **Broadway** (queue consumption with backpressure, batching, retries and failure handling built in), or **Flow**.
- **Selective receive is O(n) in mailbox length.** A `receive` that waits for a specific pattern scans from the front of the queue. A long mailbox makes every such receive slow, which makes the mailbox longer.
- **`Process.flag(:message_queue_data, :off_heap)`** on processes that legitimately hold big mailboxes keeps the messages out of the process heap so GC does not copy them.

```elixir
# Unbounded: producer can flood the GenServer
GenServer.cast(worker, {:process, item})

# Flow-controlled: caller blocks until the GenServer is ready
GenServer.call(worker, {:process, item})

# Pipeline: Broadway controls demand, batching, and failure handling
defmodule MyPipeline do
  use Broadway
  def handle_message(_, message, _), do: message
  def handle_failed(messages, _), do: messages   # nack -> dead-letter
end
```

**OTP 28 priority messages are not a backpressure mechanism.** OTP 28 added them (EEP 76): a priority alias from `:erlang.alias([:priority])`, sends via `:erlang.send/3` with the `priority` option, and a `priority` option on `:erlang.monitor/3` and `:erlang.link/2`. They exist so a control message (shutdown, health probe) can jump an already-long queue. The Erlang docs are explicit: you "very seldom need to resort to usage of priority messages", and "receiving processes have *not* been optimized for handling large amounts of priority messages." Enabling them also means the message queue order no longer reflects the order signals arrived. Do not reach for them to fix a queue that is growing — fix the demand.

### 3. Hot GenServer = serial bottleneck — use ETS, `:counters`, or `:persistent_term`

A GenServer processes **one message at a time**. Routing high-read traffic through one process serializes it. Under load, callers time out and the mailbox grows.

- **Many readers of shared data** (config, a registry, a rate-limit snapshot) should not go through a GenServer. Let the GenServer *own* the ETS table for writes; let readers hit ETS directly.
- **ETS is not transactional.** `:ets.lookup` followed by `:ets.insert` is a read-modify-write race between concurrent processes. For counters use `:ets.update_counter/3` (atomic), or `:counters` / `:atomics` for a fixed set of counters with no table lookup at all.
- **An ETS table dies with its owning process.** If the owner is a GenServer that crashes, the table and everything in it are gone — and the restarted GenServer creates an empty one. Set `heir:` if the data must outlive a restart, or accept the loss explicitly.

```elixir
# Bottleneck: every read is serialized through one process
def handle_call(:get_config, _from, state), do: {:reply, state.config, state}

# Scalable: write via the GenServer, read straight from ETS
def init(_) do
  :ets.new(:config_cache, [:named_table, :public, read_concurrency: true])
  {:ok, %{}}
end

def handle_cast({:update_config, config}, state) do
  :ets.insert(:config_cache, {:config, config})
  {:noreply, state}
end

def get_config, do: :ets.lookup(:config_cache, :config)   # no process hop
```

`:persistent_term` is for global data that is read constantly and written almost never — reads are a direct memory access with no table lookup. **Every `:persistent_term.put/2` is expensive** (it scans processes holding references to the old value). Write it once at boot; never per request.

**Do not put a compiled `Regex` in a compile-time position on OTP 28+.** OTP 28 replaced PCRE with PCRE2 and changed the compiled-regex representation, so a regex that was compiled at build time no longer works at runtime. Elixir 1.19 made regexes-as-struct-field-defaults an error for exactly this reason; module attributes holding regexes break the same way. Build it in `Application.start/2` and stash it in `:persistent_term`. While you are there: OTP 28 also changed the `re` module's default character encoding from Latin-1 to ASCII — patterns that used to match non-ASCII input now need `unicode` passed explicitly.

### 4. Long work in `handle_call` blocks callers and times out

`GenServer.call/3` defaults to a **5000 ms** timeout, and a timeout **exits the caller** (`exit(:timeout)`) unless it is caught. In a supervised worker that means the caller crashes too. Meanwhile the server is still chewing on the slow message and will eventually try to reply to a dead process.

- Do the work **in the caller** if it does not need the server's state.
- Delegate to a **supervised** task and reply out of band with `GenServer.reply/2`.
- Use a pool (`NimblePool`, `poolboy`) so many workers run in parallel.

```elixir
# Dangerous: blocks the GenServer for the whole HTTP call
def handle_call({:fetch, url}, _from, state) do
  {:reply, Req.get!(url), state}
end

# Safe: hand off to a supervised task, reply later.
# Task.start/1 is NOT the fix — it is unlinked and unsupervised, so a crash
# is silent and the caller just times out with no diagnostic.
def handle_call({:fetch, url}, from, state) do
  Task.Supervisor.start_child(MyApp.TaskSup, fn ->
    reply =
      case Req.get(url) do
        {:ok, resp} -> {:ok, resp}
        {:error, e} -> {:error, e}
      end

    GenServer.reply(from, reply)
  end)

  {:noreply, state}
end
```

If the task can crash before replying, the caller still waits the full timeout. Either wrap the reply so every path replies, or monitor the task and reply `{:error, :crashed}` from the `:DOWN` handler.

### 5. State is isolated per process — no shared memory

Processes share nothing. Each has its own heap; sending data copies it. That is what makes crash isolation safe, and it has consequences:

- **In-process state is lost on crash** and reset to `init/1` state on restart. Never keep durable state only in a GenServer's `state` map — persist it if it must survive.
- **Structured data in messages is copied** per recipient. Binaries larger than 64 bytes live on a shared reference-counted heap and are passed by reference; maps, lists and tuples are always copied. Do not broadcast large maps to many processes.
- **A small slice of a large binary keeps the whole binary alive.** A long-lived process that stores `binary_part(big, 0, 10)` pins all of `big`. `:binary.copy/1` before storing releases it. This is the usual cause of "memory grows and never comes back" on connection-holding processes.
- **Cap runaway processes.** `Process.flag(:max_heap_size, %{size: n, kill: true})` kills one bad process instead of letting it OOM the node.
- **Processes are cheap.** Spawn liberally for isolated units of work; GC is per-process and never stops the world.
- **Do not depend on map iteration order.** OTP 29 made iteration order consistent across access methods, which makes accidental order dependence *look* fine locally. It is still not an API and still changes across versions and map sizes. Sort explicitly when order matters.

### 6. Distributed Erlang hazards — netsplits, `:global`, and ordering

- **Netsplits create split-brain.** Two disconnected nodes each believe the other is down and may each accept writes. For anything that must be consistent across nodes, use consensus (`Ra`) or coordinate through the database. Unreachable never means permanently gone.
- **`:global` name registration is not partition-safe.** It uses a distributed lock that produces conflicts when the cluster heals. Prefer `Horde.Registry`, or a node-local `Registry` plus explicit routing.
- **Message ordering is only guaranteed pairwise.** Between the *same two processes*, send order is preserved. Messages from *different* senders arrive in arbitrary relative order.
- **`:rpc.call/4` blocks for the full timeout on a downed node.** Use `:erpc.call/5`, which raises immediately when the node is unreachable.

### 7. The type checker is not a reliability checker

Elixir 1.19 and 1.20 substantially expanded the set-theoretic type system: inference now flows through guards, map operations, protocol dispatch, anonymous functions and multiple clauses, and the compiler reports incompatible arguments, dead clauses and impossible branches — with no type annotations written. Elixir 1.20 still ships no user-written type signatures ("without a need to introduce type signatures (yet)").

What that changes for this skill: **nothing about what you must still verify.** The checker sees one module's data flow. It does not see mailbox growth, a missing supervision strategy, a `call` inside a `handle_call`, an ETS read-modify-write race, or a netsplit. Treat a clean compile as "the shapes line up," not "this is correct."

What it does change: warnings are now numerous and meaningful. Run `mix compile --warnings-as-errors` in CI. An AI-generated module that produces a new type warning is usually calling something with the wrong shape, and that is worth blocking on.

## How this slots into the pipeline

- **Gate 0/5 (model choice):** state the supervision strategy and the concurrency model (process-per-connection, pool, GenStage/Broadway pipeline). A stateful worker with no supervision tree is a runtime-level design defect — flag it. Decide ETS vs GenServer vs `:persistent_term` per access pattern, and name where backpressure comes from.
- **Gate 6 (implementation):** never block `handle_call` with slow work; never ship a cast-based pipeline without a bound; match restart strategies to actual failure semantics; supervise every task that replies to a caller.
- **Gate 7 (review):** the reliability-reviewer checks items 1–7 for any Elixir/Erlang service. Verify `message_queue_len` is observable, that poison messages have a terminating path (dead-letter or `max_attempts`), and that `mix compile --warnings-as-errors` passes.

## Security

VM- and OTP-level mechanics. Phoenix/Plug/Ecto/LiveView controls (scopes, `handle_event` authorization, changeset allow-lists, `check_origin`, session config) live in `mir-backend-beam-phoenix`.

### Deserialization of untrusted terms

- **`:erlang.binary_to_term/1` on attacker-influenced bytes is not safe.** It creates atoms (see atom exhaustion below) and can decode funs. `binary_to_term(bin, [:safe])` blocks new atom creation and executable terms, and is the minimum — but it is a decode-time filter, not a decoder-hardness guarantee.
- Two 2026 OTP advisories are in the term decoder itself, so `[:safe]` does not help: **CVE-2026-54890** (High, OTP 27.0 and later) — a crafted `BIT_BINARY_EXT` triggers an integer underflow and crashes the VM; **CVE-2026-55737** (Medium, OTP 25.0 and later) — a signed/unsigned mismatch in `LARGE_TUPLE_EXT` decoding corrupts a heap pointer. Both fixed in **27.3.4.15 / 28.5.0.4 / 29.0.4**. Anything that decodes external terms — a signed cookie, a queue payload, a distribution link — is in range.
- In Elixir web stacks use **`Plug.Crypto.non_executable_binary_to_term/2`**, which rejects executable terms, and pass `[:safe]` when the payload cannot legitimately contain new atoms.
- Signing the payload proves who produced it, not that it is harmless. A leaked signing key plus a term decoder is a code-execution path.

### Atom exhaustion

The atom table is never garbage-collected and is capped (default 1,048,576 entries, `+t` at boot). Filling it crashes the whole node, not one process.

```elixir
# WRONG — one endpoint that reaches this is a node-level DoS
String.to_atom(params["type"])
List.to_atom(charlist)
Jason.decode!(body, keys: :atoms)

# RIGHT
String.to_existing_atom(params["type"])   # raises on unknown, which is what you want
Jason.decode!(body, keys: :strings)       # then map to atoms through your own allow-list
```

- Elixir is moving the naming to match the risk: on `main` (unreleased 1.21) `String.to_atom/1` and `List.to_atom/1` are soft-deprecated in favour of `String.to_unsafe_atom/1` and `List.to_unsafe_atom/1`, with new `String.to_existing_atom/2` and `List.to_existing_atom/2`. Treat every `to_atom` on request data as a finding today.
- This is not theoretical: **CVE-2026-48597** (High) is atom exhaustion in `tesla` via an untrusted URL scheme, fixed in **tesla 1.18.3**. A dependency can do it for you.
- Monitor `:erlang.system_info(:atom_count)` against `:erlang.system_info(:atom_limit)` and alert well before the ceiling.
- OTP 28 removed the 255-byte cap on atom size, so each junk atom can now be much larger. The count limit is still the one that kills you first.

### Command and code injection

| Call | Shell? | Verdict |
|---|---|---|
| `System.cmd("convert", [path])` | No | Safe — arguments never touch a shell |
| `:os.cmd(~c"convert #{path}")` | **Yes** | Injection. `;`, `` ` ``, `$()` all execute |
| `Port.open({:spawn, "cmd #{arg}"}, [])` | **Yes** | Injection |
| `Port.open({:spawn_executable, path}, args: [arg])` | No | Safe |

- `Code.eval_string/2`, `Code.eval_quoted/2`, `Code.string_to_quoted!/1` + eval, and `Module.create/3` on request data are arbitrary code execution. There is no safe mode. `Kernel.apply/3` with a module or function name derived from input is the same problem wearing a different hat — resolve through an explicit allow-list map, never `String.to_existing_atom` into `apply`.
- **OTP 29 moved `.` from the first to the last position in the default code path.** Before OTP 29, a `.beam` file written into the working directory shadowed a stdlib module. If you run on 27 or 28, an attacker with write access to the CWD gets code execution at the next module load. Do not run the release from a writable directory.

### Distributed Erlang and EPMD

Distribution is the highest-value target in a BEAM deployment, and it is not protected by anything you would call authentication.

- **The cookie is the only credential, and it is equivalent to root on the node.** Anyone who connects can call `:erpc.call(node, :os, :cmd, [~c"..."])`. Never use the auto-generated `~/.erlang.cookie` in production and never bake one into an image.
- **Distribution traffic is unencrypted by default.** Enable TLS distribution (`-proto_dist inet_tls` plus `-ssl_dist_optfile`) or keep the distribution port on a private network that nothing else reaches.
- **EPMD listens on 4369 and binds all interfaces by default.** Set `ERL_EPMD_ADDRESS=127.0.0.1` (or the private interface) and pin the distribution port with `inet_dist_listen_min`/`max` so you can firewall it. Exposed EPMD leaks node names and live node inventory, and it is the front door to the cookie attack. OTP 29.0.5 (4 Aug 2026) fixed a regression in EPMD's localhost binding — if you rely on that binding, be on 29.0.5 or later.
- Remote shells (`iex --remsh`, `:observer`) ride the same channel. If distribution is reachable, so are they.

### TLS and the `:ssl` application

- OTP 26 changed the **client** default to `verify: :verify_peer` (OTP-18455), which also made supplying CA certificates mandatory. The failure mode AI produces is "fix" the resulting handshake error by setting `verify: :verify_none` — that silently disables certificate validation for that client. Supply `cacerts: :public_key.cacerts_get()` instead.
- The July 2026 OTP batch includes **CVE-2026-55953 (Critical)** — a (D)TLS 1.2 **server certificate verification bypass** via an unoffered anonymous cipher suite, affecting OTP 17 and later — plus **CVE-2026-58227** (unbounded recursion on invalid cert chains) and **CVE-2026-59251** (exponential certificate-policy-tree growth), both TLS denial of service. All are fixed in **27.3.4.15 / 28.5.0.4 / 29.0.4**. Verification bypass is on the default client path; there is no configuration that mitigates it. Patch.
- Earlier 2026 batch, fixed in **27.3.4.14 / 28.5.0.3 / 29.0.3**: CVE-2026-55952 (TLS 1.3 session-ticket DoS), CVE-2026-55950 (DTLS demux race), CVE-2026-54891 (plaintext injection toward a DTLS client during handshake), CVE-2026-54887 (DTLS cookie bypass with predictable HMAC).
- Running an OTP SSH daemon in-process (a common "ops backdoor" pattern) pulls in CVE-2026-54886 (SFTP infinite loop DoS) and CVE-2026-53422 (SFTP `REALPATH` path-existence oracle), same fix versions. OTP 29 disables SSH shell and exec services and the SFTP subsystem **by default** and defaults key exchange to `mlkem768x25519-sha256`; on 27 and 28 those services are on unless you turned them off.

### SSRF from user-supplied URLs

No BEAM HTTP client validates destinations. `:httpc`, `Finch`, `Mint`, `Req`, `Tesla` and `HTTPoison` will all fetch `http://169.254.169.254/latest/meta-data/` (AWS IMDS), `http://metadata.google.internal/`, and `http://127.0.0.1:6379/`.

- Resolve the hostname yourself, check the **resolved IP** against private/loopback/link-local ranges, then connect to that address with the `Host` header set. Checking the hostname string leaves a DNS-rebinding window between check and connect.
- Disable redirect following, or re-validate after every hop. `Req` follows redirects by default.
- An allow-list of exact destination hosts beats any deny-list.
- On AWS, require IMDSv2 so a plain GET cannot read credentials.
- Client-library advisories that hit here, all 2026: **CVE-2026-48595** (High) — `tesla` leaked the `Authorization` header across a cross-origin redirect because the strip was case-sensitive, fixed in **1.18.3**; **CVE-2026-48594** / **CVE-2026-49755** — decompression bombs on response bodies in `tesla` (**1.18.3**) and `req` (**0.6.1**); **CVE-2026-49754** and **CVE-2026-48862** (High) — HTTP/2 CONTINUATION flood and unbounded `PUSH_PROMISE` stream growth in `mint`, fixed in **1.9.0**. `Finch` and `Req` sit on `mint`, so the mint fix is transitive and easy to miss.

### Secret and PII leakage

- **A GenServer crash report prints the process state and the last message.** That is the OTP default `:logger` report. A worker holding an API token, a decrypted payload, or a full user struct writes all of it to your log aggregator on any crash. Implement `format_status/1` on the behaviour to redact before it is logged.
- `inspect/2` prints every field of a struct, and inspect output appears in exception messages, crash reports and `Logger` metadata. Use `@derive {Inspect, except: [:token, :password_hash]}` on any struct that carries a credential.
- Never log `conn.params`, a raw job payload, or `System.get_env()`. Filtering happens at the framework layer — see `mir-backend-beam-phoenix` for `:filter_parameters`, whose default covers only `["password"]`.
- Compare tokens and MACs with `Plug.Crypto.secure_compare/2`, or `:crypto.hash_equals/2` outside Plug ("compare two binaries in constant time"). `==` returns early and leaks length and prefix through timing.
- `Process.info(pid, :dictionary)` and `:sys.get_state/1` expose everything a process holds to anything with a shell on the node — which is another reason distribution access is total access.

### Path traversal

- Never join request data onto a base path. `Path.join(base, params["name"])` accepts `../../etc/passwd`, and an absolute second argument replaces the base outright.
- Use **`Path.safe_relative/2`** (Elixir 1.14+): it returns `{:ok, path}` only when the result stays inside the given directory, and `:error` otherwise. Match on `:error` and refuse.
- Uploaded filenames are attacker-controlled. Generate the stored name; keep the client-supplied one only as a display label, and never let it choose the extension used to pick a handler.

### Supply chain

- **`mix.lock` records two checksums per Hex dependency** (the package tarball hash and the registry's outer hash) and `mix deps.get` verifies both. Commit the lockfile. Run **`mix deps.get --check-locked`** in CI — it "raises if there are pending changes to the lockfile", which is what stops a build from silently resolving a different version than the one you reviewed.
- **`mix compile` runs dependency code on your machine.** Compile-time macros execute, `@on_load` hooks run at module load, and `elixir_make`/`rustler` deps invoke a Makefile or a Cargo build. Adding a dependency means running its author's code on every build machine and in every deploy image. There is no `--ignore-scripts`.
- Git dependencies are pinned to a commit SHA in `mix.lock`, but nothing verifies the tree beyond that SHA. Never depend on a branch or tag.
- Run **`mix deps.audit`** (the `mix_audit` package, 2.1.5) against the Elixir security advisory database, and **`mix hex.audit`** for retired packages, in CI. Add **`sobelow`** (0.15.0, 5 Aug 2026) for Phoenix-specific static analysis. `mix deps.audit` is the only thing that will tell you your `plug` is four advisories behind.
- Elixir itself carries **CVE-2026-49762** (Medium, 9 Jun 2026): unbounded integer parsing in `Version` parsing and matching, affecting 1.5.0 through 1.20.0, fixed in **1.20.1**. If you call `Version.parse/1` or `Version.match?/2` on anything a user supplies, this is your default path.

## Edit boundary (what belongs here vs. above/below)

- Generic, all-language rules (idempotency, invariants, gates, observability) → **up** to `mir-backend`.
- A specific library's mechanics (LiveView assigns and `handle_event` authorization, Ecto queries and migrations, Plug parsers, PubSub, channel backpressure) → **down** to the framework module (`mir-backend-beam-phoenix`).
- **Here:** only what every BEAM backend shares because of the Erlang VM — process model, supervision, mailbox dynamics, ETS vs GenServer vs `:persistent_term`, per-process heap isolation, distributed Erlang, the Elixir type checker's limits, and the VM/OTP/Hex security layer.
- A different runtime (Python, Go, Node…) → its own `mir-backend-<runtime>` tier. Never widen this one.
