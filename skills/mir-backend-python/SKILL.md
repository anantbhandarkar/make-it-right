---
name: mir-backend-python
description: "Make It Right (Python runtime tier). CPython runtime reliability footguns that are shared across EVERY Python backend framework (FastAPI, Django, Flask, Celery) — distinct from the generic backend gates and from any one framework's mechanics. Covers: the GIL and the free-threaded build (PEP 703/779, officially supported since 3.14 but not the default), async-vs-sync 'coloring', blocking the event loop, choosing asyncio vs threads vs multiprocessing vs subinterpreters vs a worker queue, fork-safety of connection pools and the 3.14 forkserver default change, lazy annotations (PEP 649/749), serverless cold starts, dropped-task exceptions, and runtime-level security (unsafe deserialization, archive extraction, shell arguments, SSRF, packaging supply chain). TRIGGER when the backend runtime is Python — sits between mir-backend (generic) and the framework module (e.g. mir-backend-python-fastapi). SKIP for Node/JVM/Go/Rust/.NET/Ruby/PHP/BEAM runtimes (each has its own mir-backend-<runtime> tier), and for framework-library mechanics — FastAPI/SQLAlchemy session and Pydantic rules live in mir-backend-python-fastapi, Django ORM/migrations/tasks in mir-backend-python-django, Flask contexts and config in mir-backend-python-flask."
trigger: /mir-backend-python
argument-hint: "<task or files>"
allowed-tools:
  - Read
  - Write
  - Edit
  - Bash
  - Glob
  - Grep
---

# /mir-backend-python · Make It Right (Python runtime)

The middle tier. `mir-backend` decides **what is correct** (any language). The framework module (e.g. `mir-backend-python-fastapi`) knows the **library's mechanics**. This tier owns what's true for **all Python backends because they run on CPython** — the concurrency model and process model that FastAPI, Django, Flask, and Celery all inherit.

**Runtime assumed:** CPython 3.12+. Verified 13 Aug 2026: **3.14 is the current stable line** (first released 7 Oct 2025); 3.15 is in prerelease, due 1 Oct 2026; 3.13 is in bugfix; 3.12 and 3.11 are security-only; **3.10 reaches end-of-life in Oct 2026** — if the project is on it, an upgrade plan is a Gate 4 risk row, not a "later"; 3.9 died 31 Oct 2025. Load order: `mir-backend` → `mir-backend-python` → `<framework module>`.

## The CPython footguns AI walks into (framework-agnostic)

### 1. The GIL — still on by default, but "is there a GIL" is now a build choice
Default CPython builds hold one interpreter lock, so **CPU-bound work does not run in parallel across threads** — it serializes and you pay context-switch overhead on top. That has not changed. What changed: CPython 3.14 promoted the **free-threaded build** from experimental to officially supported (PEP 779). It is a separate binary (`python3.14t`), not the default interpreter.

| Situation | Do threads give CPU parallelism? |
|---|---|
| Default `python3.14` | No. GIL is on. |
| Free-threaded `python3.14t` | Yes. |
| Free-threaded build + a C extension that hasn't declared itself thread-safe | **No.** Importing it re-enables the GIL for the whole process. Threads keep running; they just stop running in parallel. CPython prints a warning, but it is one line in startup output, not an error — nothing fails. |

If you deploy the free-threaded build, log `sys._is_gil_enabled()` at startup and alert on it; a dependency upgrade can turn your parallelism off and the build still "works". `PYTHON_GIL=0` / `-X gil=0` forces the GIL to stay off, which turns a silent slowdown into whatever the unprepared extension actually does — do not set it to make the warning go away. Costs: CPython's published pyperformance figures for 3.14 range from about **1% slower single-threaded on macOS arm64 to about 8% on x86-64 Linux**; memory use is higher (larger object headers, mimalloc, deferred reclamation) and CPython publishes no single number — PEP 779's 20% was an acceptance ceiling, not a measurement. Every C extension must be rebuilt against the `Py_GIL_DISABLED` ABI. Adopt it for a specific measured CPU-bound workload with a verified extension set — not as a default.

- CPU-bound on the default build (parsing, crypto, image/ML compute) → `ProcessPoolExecutor`, `concurrent.interpreters` (PEP 734, new in 3.14), a native extension that releases the GIL, or a separate service. Never "add threads" expecting speedup.
- I/O-bound → threads or asyncio are fine on either build.
- This is the runtime-level reason the runtime-map says "SKIP Python for heavily CPU-bound / microsecond-latency paths."

### 2. Async/sync "coloring" — pick one model per path and don't mix carelessly
An `async def` function can only be awaited from async context; a sync function that calls blocking I/O must not run on the event loop. Mixing them is the #1 source of "the server mysteriously stalls under load."
- Don't call **blocking** I/O (sync DB driver, `requests`, `time.sleep`, big CPU) inside `async def` — it freezes the **entire** event loop for every concurrent request. Offload with `await asyncio.to_thread(...)` / `anyio.to_thread.run_sync(...)`, or use an async client.
- Don't call `asyncio.run()` inside an already-running loop.
- Decide per endpoint/job: **async all the way** (async web framework + async drivers) **or sync all the way** (sync framework + thread workers). Half-async is where the bugs live.
- **Debugging a stall in production:** `python -m asyncio pstree <pid>` (new in 3.14) prints the awaiting call tree of a *running* process; `python -m asyncio ps <pid>` gives the flat task table. Put the command in the runbook at Gate 5 — it turns "the service is hung" into "task X is awaiting Y".

### 3. Concurrency-model decision (state it in Gate 5)
| Workload | Right model | Wrong model (the trap) |
|---|---|---|
| I/O-bound, high concurrency | asyncio (single thread, many awaits) | thread-per-request at huge counts |
| I/O-bound, moderate | thread pool | — |
| CPU-bound, default build | `ProcessPoolExecutor` / native ext / separate service | threads (GIL serializes) |
| CPU-bound, isolated tasks, picklable arguments and results | `concurrent.futures.InterpreterPoolExecutor` (3.14) over `concurrent.interpreters` | threads on the default build |
| Long/durable/at-least-once work | worker queue (Celery/RQ/arq/Django Tasks) | request thread / asyncio task that dies with the process |

Subinterpreters are **isolated**, not shared memory with cheaper threads: arguments and results are copied or pickled across the boundary, so "avoid serialization" is not a reason to pick them. A bare `Interpreter.call()` also runs on the *calling* thread — the parallelism comes from driving several interpreters from several threads, which is what `InterpreterPoolExecutor` does for you. Interpreter startup is not yet optimized and many C extensions still don't support per-interpreter state. Benchmark before committing, and record the result in the Assumption Ledger.

### 4. Process model — workers are shared-NOTHING
Production runs N worker processes (gunicorn/uvicorn `--workers`, Celery prefork). **In-process global state is per-worker and is lost on restart** — never use a module-level dict/counter as if it were shared across workers. Shared state goes in Redis/DB.
- **Fork-safety:** connection pools, async event loops, and client sockets created **before** a fork are corrupt in the child. Initialize DB/Redis pools **after** fork (gunicorn `post_fork`, FastAPI `lifespan`, or lazily on first use in the worker). Know which of your servers actually forks, because the advice differs:

| Server | Forks? | When a module-level pool breaks |
|---|---|---|
| `gunicorn` (default) | yes, `os.fork()` | Safe by luck — the app module is imported *inside* each worker after the fork. |
| `gunicorn --preload` | yes | **Broken.** The master imports the app and builds the pool; every child inherits a corrupt copy. This is the combination to grep for. |
| `uvicorn --workers N` | **no** | Uvicorn uses `multiprocessing.get_context("spawn")`; children re-import your module, so each builds its own pool. |
| Celery prefork | yes, `os.fork()` | Broken if the pool is built at import in the parent. Use the `worker_process_init` signal. |

  All of these are unaffected by the `multiprocessing` default change below, which only governs pools *you* create.
- **Changed in 3.14:** the default start method for `multiprocessing` and `ProcessPoolExecutor` on Linux moved from `fork` to `forkserver` (macOS has defaulted to `spawn` since 3.8). Children no longer inherit the parent's memory, so code that quietly relied on inheriting a warm client now gets a fresh one, and arguments that were never serializable now raise. Pick the context explicitly — `multiprocessing.get_context("forkserver")` — rather than letting the default move under you.
- `os.fork()` from a process that already has threads is unsafe; CPython emits a `DeprecationWarning` for it since 3.12. If you fork after starting a background thread, restructure.

### 5. Serverless / cold starts
- Heavy work at **import time** (loading a model, building a pool, importing huge deps) is paid on every cold start. Lazy-init expensive resources on first request; keep import side-effect-free.
- Reuse connections across invocations via a module-level client created **lazily** (warm-container reuse) — but cap pool size to the platform's concurrency.

### 6. Dropped task & coroutine exceptions
- A bare `asyncio.create_task(...)` whose result is never awaited will **swallow exceptions** and can be garbage-collected mid-flight. Keep a reference and attach a done-callback / await it, or use a task group (`asyncio.TaskGroup` / `anyio` nursery). `TaskGroup` is the default answer — it keeps the references and re-raises as an `ExceptionGroup`.
- An un-awaited coroutine is a silent no-op (`coroutine was never awaited` warning) — the work simply never ran.
- A task group cancels its siblings when one member raises. If you wanted independent best-effort work, that's `asyncio.gather(..., return_exceptions=True)`, not a task group — say which you meant.

### 7. Annotations are evaluated lazily from 3.14 (PEP 649/749)
Annotations are no longer evaluated at function/class definition time. Practical consequences:
- `from __future__ import annotations` is no longer needed for forward references. Leaving it in is not an error, but it forces string-only annotations, which some libraries still resolve differently.
- Anything that reads `obj.__annotations__` directly (hand-rolled serializers, DI containers, older ORM glue) can now see unresolved forward refs. Read them through `annotationlib.get_annotations(obj, format=...)` with `Format.VALUE` / `Format.FORWARDREF` / `Format.STRING` instead.
- Pydantic and Django handle this. Your own introspection code probably doesn't — check it before calling a 3.14 upgrade clean.

### 8. Micro-version pinning is not cosmetic
CPython 3.14.0–3.14.4 shipped an incremental garbage collector; it was **reverted to the generational GC in 3.14.5** after production memory-pressure reports. If you benchmarked pause times or RSS on 3.14.0–.4, those numbers do not carry to ≥3.14.5. Pin the micro version in the image and re-benchmark on upgrade instead of assuming a patch release is behaviour-neutral.

## How this slots into the pipeline
- **Gate 0/5 (model choice):** state the concurrency model (async/threads/processes/subinterpreters/queue) and justify it against the workload table above. A mismatch here (e.g. threads for CPU-bound on the default build) is a runtime-level design defect — flag it. State the CPython minor version and whether the build is free-threaded.
- **Gate 6 (implementation):** never block the event loop; init pools post-fork; offload CPU work; no bare `create_task`.
- **Gate 7 (review):** the reliability-reviewer should additionally check items 1–8 here for any Python service; the security-reviewer checks the Security section below.

## Security

Runtime-level mechanics only. The framework module carries its own library's issues (Django settings, Starlette middleware, Flask config); these bite on **every** Python backend regardless of framework.

**Unsafe deserialization**
- Loading a `pickle` payload — and equally `marshal`, `shelve`, `dill`, or `yaml.load(...)` without `Loader=yaml.SafeLoader` — runs arbitrary code from the payload. Signing the blob proves who sent it, not that it is harmless, so a leaked signing key becomes remote code execution. Use `json`, or `yaml.safe_load`. Never choose that format for a session, cache entry, or queue payload a client can influence.
- `numpy.load(..., allow_pickle=True)` and `torch.load(...)` use the same format underneath. Use `torch.load(..., weights_only=True)`, or `safetensors` for model files.
- Untrusted XML: `xml.etree.ElementTree`, `xml.dom.minidom`, and `xml.sax` are vulnerable to entity-expansion denial of service ("billion laughs"). Parse untrusted XML with `defusedxml`.

**Archive extraction and path traversal**
- `tarfile.extractall()` defaults to `filter="data"` from Python 3.14 (before that it was unfiltered). Do not read that as "safe for untrusted archives." The filter has been bypassed repeatedly: **CVE-2024-12718** (mtime/chmod outside the destination), **CVE-2025-4517** (arbitrary write with `filter="data"`), **CVE-2026-4360** (`filter` not applied when extracting hardlinks), **CVE-2026-7774** (`data_filter` bypass via empty or directory-like link names), **CVE-2026-11940** (hardlink-referencing-symlink escape, an incomplete fix of CVE-2025-4330). The filter arrived in 3.12 and was backported to 3.11.4, 3.10.12, 3.9.17 and 3.8.17 — test for it with `hasattr(tarfile, "data_filter")`, not with a version comparison. Stay on a patched micro release **and** validate members yourself before extracting anything a user uploaded.
- `zipfile` has no filter. Check every `ZipInfo.filename` for absolute paths, `..`, and symlink entries before writing.
- `os.path.join(base, user_path)` returns `user_path` outright when it is absolute — the base silently disappears. Write: `p = (Path(base) / user_path).resolve()` then reject unless `p.is_relative_to(Path(base).resolve())`.

**Shell argument injection**
- `subprocess.run(cmd, shell=True)` with any interpolated user value is injection. Pass a list of arguments and leave the shell off. Same for `os.system` and `os.popen`. If a shell is genuinely unavoidable, `shlex.quote` every interpolated value — and know that quoting does not stop a value starting with `-` from being read as a flag by the target program.

**Injection into SQL, templates, and headers**
- An f-string into SQL is injection regardless of driver. Bind parameters. PEP 750 t-strings (3.14) let a library tell literal text from interpolated values apart, but only libraries that accept a `Template` object get that benefit — an f-string is still just a `str` by the time the driver sees it.
- `jinja2.Template(user_string).render()` is server-side template injection and generally means remote code execution. Never build a template out of user input; pass user input as a *context variable* to a fixed template.
- Any header value built from user input must be rejected if it contains `\r` or `\n`. The stdlib blocks most cases; hand-written ASGI/WSGI middleware often does not.

**SSRF from user-supplied URLs**
- `requests` follows redirects by default. `urllib.request.urlopen` follows too. A hostname validated before the first request means nothing after a 302 — re-validate on every hop, and cap the redirect count.
- Block the cloud metadata endpoints by IP, not by name: `169.254.169.254` (AWS/Azure/GCP IMDS) and `fd00:ec2::254`. Blocking the string `metadata.google.internal` is not enough — DNS resolves it to the same address.
- Resolve the hostname yourself, reject the resulting address with `ipaddress.ip_address(x).is_private / .is_loopback / .is_link_local / .is_reserved`, then connect to **that address**. Resolving twice (check, then connect) is a DNS-rebinding hole. Connecting to a bare IP also drops the vhost and the TLS identity, so carry the original name explicitly — set the `Host` header and pass the hostname as the TLS `server_hostname`/SNI (httpx: a custom transport; requests: an `HTTPAdapter` that pins the resolved address). Dropping this step breaks every multi-tenant host and silently weakens certificate verification.
- An allow-list of destination hosts beats any deny-list. Prefer it whenever the set of callable hosts is known.

**Secret and PII leakage**
- `logging` an exception with `%s` routinely prints a DSN including the password (`postgresql://user:pass@host/db`). Redact in a logging filter or formatter, not at each call site — you will miss one.
- Returning `str(exc)` or a traceback in an HTTP body leaks table names, absolute file paths, and environment values. Log the traceback with a correlation ID; return the ID.
- Compare tokens, HMACs, and signatures with `hmac.compare_digest`, never `==`. Generate tokens with `secrets.token_urlsafe`/`secrets.token_hex`, never `random` — the Mersenne Twister state is recoverable from its output.
- `repr()` of a dataclass or model prints every field, and repr shows up in tracebacks and log records. Use `pydantic.SecretStr`, or write a `__repr__` that omits credentials.
- `hashlib.md5`/`sha1` are not password hashes. Use `argon2`, `bcrypt`, or `hashlib.scrypt`.
- `ssl._create_unverified_context()` and `requests.get(..., verify=False)` disable certificate validation entirely. If a corporate CA is the reason, install the CA bundle instead.

**Remote debugging (new in 3.14)**
- PEP 768 added `sys.remote_exec`, which injects and runs a script in another process by PID — this is what `python -m pdb -p PID` uses. Anything holding the OS privileges needed to attach to the process (ptrace on Linux, gated by Yama `ptrace_scope` and capabilities; same UID is necessary but often not sufficient) gets code execution inside it. That is a much shorter path than it looks like from a compromised sidecar or a debug shell in the same pod. Set `PYTHON_DISABLE_REMOTE_DEBUG=1` (or `-X disable-remote-debug`) in production images, and keep it in the container's default env.

**Supply chain (this ecosystem specifically)**
- **pip ≥ 26.1.** Before 26.1, pip ran its self-update check *after* installing wheels, while the freshly-installed package was already on `sys.path`. A wheel shipping a top-level `utils.py` got imported by pip itself — **CVE-2026-6357**, arbitrary code execution. **CVE-2026-1703** was a separate path traversal in pip's wheel extraction (`os.path.commonprefix` where `os.path.commonpath` was needed). Latest verified this run: pip 26.2.1.
- **setuptools ≥ 83.0.0** — **CVE-2026-59890**, a `MANIFEST.in` exclusion bypass via Unicode normalization collision on macOS filesystems, so files you believed excluded ship inside the sdist.
- **`.pth` files in `site-packages` run at interpreter startup**, before any of your code and without you importing the package. A malicious dependency does not need to be imported to run. This is the mechanism the March 2026 `litellm` PyPI compromise used.
- **Pin transitively, with hashes.** `pip install --require-hashes -r requirements.txt`, or `uv.lock` / `poetry.lock`. PEP 751's `pylock.toml` is the new tool-agnostic format — `pip lock` (25.1) writes it and `pip install -r pylock.toml` (26.1) installs from it, but **both sides are still marked experimental**, the file is only valid for the Python version and platform that produced it, and extras/dependency-groups aren't supported on the install side. Treat it as an interchange format, not yet as the single source of truth.
- **CI is inside the chain.** The 2026 campaign that reached PyPI entered through an unpinned scanner in a build pipeline, not through PyPI itself. Pin CI actions and base images by digest, not by tag.
- **Private index + public PyPI is a name-confusion hole.** With `--extra-index-url`, an attacker who publishes your internal package name on public PyPI can win resolution. Serve internal packages through a single proxying index, or pin the source per package with your tool's explicit-index mechanism.

## Edit boundary (what belongs here vs. above/below)
- Generic, all-language rules (idempotency, invariants, gates) → **up** to `mir-backend`.
- A specific library's mechanics (FastAPI `Depends`, SQLAlchemy `selectinload`, Django ORM `select_related`, Alembic, Flask contexts) → **down** to the framework module (`mir-backend-python-<framework>`).
- **Here:** only what every Python backend shares because of CPython (concurrency model, process/fork model, GIL and free-threading, annotations, cold start, asyncio task hygiene) and stdlib/packaging-level security.
- A different runtime (Node, Go, JVM…) → its own `mir-backend-<runtime>` tier. Never widen this one.
