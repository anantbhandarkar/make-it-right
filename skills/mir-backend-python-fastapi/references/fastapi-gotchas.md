# FastAPI + Async SQLAlchemy 2.0 gotchas — right vs wrong

Code-level examples of the footguns in SKILL.md. Stack verified 13 Aug 2026: FastAPI 0.141.x · Starlette 1.6.x · Pydantic 2.13.x · SQLAlchemy 2.0.52 (`AsyncSession` over `asyncpg` or `psycopg` 3) · Postgres · Redis.

---

## 1. Engine in `lifespan`, session per request via Depends

```python
from contextlib import asynccontextmanager
from typing import Annotated, AsyncIterator
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # built AFTER the worker process exists — not at import time, not before fork
    app.state.engine = create_async_engine(
        DSN, pool_size=10, max_overflow=20, pool_pre_ping=True
    )
    app.state.sessionmaker = async_sessionmaker(app.state.engine, expire_on_commit=False)
    yield
    await app.state.engine.dispose()

app = FastAPI(lifespan=lifespan)

async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:
        yield session            # one session per request, closed on exit

SessionDep = Annotated[AsyncSession, Depends(get_session)]

@app.post("/orders")
async def create_order(body: OrderCreate, session: SessionDep):
    ...
```

```python
# WRONG — module-level session shared across requests
session = SessionLocal()         # cross-request bleed, InterfaceError under concurrency

# WRONG — engine at import time. Under `gunicorn --preload` the master builds the pool
# and every forked child inherits a corrupt copy. (`uvicorn --workers` spawns, so it
# dodges that one — but you still lose the dispose() and the per-worker lifetime.)
engine = create_async_engine(DSN)

# WRONG — silently dead. Setting lifespan= makes FastAPI ignore every on_event handler.
app = FastAPI(lifespan=lifespan)
@app.on_event("startup")
async def connect_redis(): ...   # never runs, no warning
```

`expire_on_commit=False` so returning an ORM object after commit doesn't trigger a lazy load on a now-expired attribute.

## 2. Never share a session across gather

```python
# WRONG — one session, concurrent tasks corrupt its connection state
await asyncio.gather(load_a(session), load_b(session))

# RIGHT — a session per concurrent unit of work
async def load(factory):
    async with factory() as s: ...
await asyncio.gather(load(sessionmaker), load(sessionmaker))
```

## 3. Eager-load to avoid the async N+1 (which raises, not just slows)

```python
# WRONG — order.items lazy-loads outside an active session → MissingGreenlet / DetachedInstanceError
order = await session.get(Order, oid)
return [i.sku for i in order.items]      # boom, or N queries

# RIGHT — load the relationship in the query
stmt = select(Order).where(Order.id == oid).options(selectinload(Order.items))
order = (await session.execute(stmt)).scalar_one()
return [i.sku for i in order.items]
```

## 4. Don't block the event loop — and don't overflow the threadpool

```python
# WRONG — sync driver / blocking call in async route stalls ALL concurrent requests
import requests, psycopg2          # both sync
@app.get("/x")
async def x(): r = requests.get(...)     # blocks the loop

# RIGHT — async client for I/O, threadpool for BLOCKING I/O you can't make async
import httpx
async with httpx.AsyncClient() as c: r = await c.get(...)
from starlette.concurrency import run_in_threadpool
result = await run_in_threadpool(legacy_blocking_io_call, arg)

# WRONG — run_in_threadpool for CPU work. The GIL means it does not run in parallel;
# it just moves the stall off the loop thread and eats the shared 40-token limiter,
# so unrelated plain-def endpoints slow down too. CPU-bound → ProcessPoolExecutor,
# InterpreterPoolExecutor, a native extension that releases the GIL, or a worker service.
result = await run_in_threadpool(cpu_heavy, arg)
```

Async DSNs: `postgresql+asyncpg://…` or `postgresql+psycopg://…` (psycopg 3). `postgresql+psycopg2://` under `create_async_engine` is the classic mistake.

The threadpool is a bounded resource. Every plain-`def` route and every `run_in_threadpool` call shares one AnyIO limiter (40 tokens by default):

```python
# Symptom: unrelated sync endpoints get slow, CPU is idle, DB is idle.
# Cause: >40 concurrent plain-def handlers queued on the limiter.
import anyio
anyio.to_thread.current_default_thread_limiter().total_tokens = 80   # deliberate, sized vs the DB pool
```

Raising it without raising the DB pool just moves the queue.

## 5. Input schema is an allow-list (anti mass-assignment) + response_model

```python
class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")     # reject unknown fields
    sku: str
    qty: int
    # NOTE: no id, no user_id, no is_admin — client cannot set these

@app.post("/orders", response_model=OrderOut)     # response_model strips internal fields
async def create_order(body: OrderCreate, user: CurrentUser, session: SessionDep):
    order = Order(sku=body.sku, qty=body.qty, user_id=user.id)   # server sets owner
    # WRONG would be: Order(**body.model_dump()) then trusting client fields

# PATCH: exclude_unset so an omitted field isn't written as None
for field, value in body.model_dump(exclude_unset=True).items():
    setattr(order, field, value)
```

## 6. Auth vs authorization are two dependencies

```python
from typing import Annotated

async def get_current_user(token: Annotated[str, Depends(oauth2)]) -> User: ...  # authentication
CurrentUser = Annotated[User, Depends(get_current_user)]

async def get_owned_order(order_id: int, user: CurrentUser, session: SessionDep) -> Order:
    order = await session.get(Order, order_id)
    if order is None or order.user_id != user.id:
        raise HTTPException(404)        # 404 not 403 — don't leak existence
    return order                        # authorization happens HERE, at object load

OwnedOrder = Annotated[Order, Depends(get_owned_order)]
```
The IDOR bug is fetching by `order_id` with only `get_current_user` and no ownership check. `Annotated[...]` is the current syntax; `user=Depends(get_current_user)` still works but is the older form. Router-level `dependencies=[Depends(...)]` lists still take a bare `Depends`.

## 7. Durable work needs a real queue, not BackgroundTasks

```python
# OK for best-effort only (lost if process dies, no retry):
@app.post("/x")
async def x(bg: BackgroundTasks): bg.add_task(log_something)

# For "must happen" work — enqueue to a Redis-backed worker, idempotent handler:
await arq_pool.enqueue_job("send_receipt", order_id, _job_id=f"receipt:{order_id}")
# _job_id dedupes; the handler itself must also be idempotent (at-least-once delivery)
```

## 8. Idempotency + locking with Redis; correctness with the DB

```python
# Idempotency: first writer wins atomically, store result for replay
ok = await redis.set(f"idem:{key}", "in-progress", nx=True, ex=86400)
if not ok:
    return await wait_or_return_stored_result(key)   # retry → same response, no 2nd effect

# Money correctness: DB row lock, not Redis (Redis lock is best-effort under failover)
async with session.begin():
    stock = (await session.execute(
        select(Stock).where(Stock.sku == sku).with_for_update())).scalar_one()
    if stock.available < qty:
        raise HTTPException(409, "out of stock")
    stock.available -= qty            # INV: available >= 0 preserved under concurrency

# State transition without read-modify-write race:
res = await session.execute(
    update(Order).where(Order.id == oid, Order.status == "PENDING")
    .values(status="PAID"))
if res.rowcount == 0:
    raise HTTPException(409, "order not in PENDING state")   # concurrent transition lost the race, safely
```

Irreversible effects (capture charge, send email) go **after** `session.commit()`, guarded by the idempotency key — never inside the tx block.

## 9. Raw SQL and dynamic ordering

```python
# WRONG — injection
await session.execute(text(f"SELECT * FROM orders WHERE user_id = {uid}"))
await session.execute(select(Order).order_by(text(request.query_params["sort"])))

# RIGHT — bound parameter, and an allow-list for anything that becomes an identifier
await session.execute(text("SELECT * FROM orders WHERE user_id = :uid"), {"uid": uid})

SORTABLE = {"created_at": Order.created_at, "total": Order.total}
col = SORTABLE.get(request.query_params.get("sort", "created_at"))
if col is None:
    raise HTTPException(400, "bad sort field")
stmt = select(Order).order_by(col)
```

Bound parameters cover values. A column or table name can never be a bound parameter — it has to come from a fixed mapping.

## 10. Middleware order and the Host header

```python
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.cors import CORSMiddleware

app.add_middleware(CORSMiddleware,
                   allow_origins=["https://app.example.com"],   # enumerate; never ["*"] with credentials
                   allow_credentials=True)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["app.example.com"])  # added last = runs first
```

`add_middleware` prepends, so the **last** one added runs **first**. `TrustedHostMiddleware` must reject a forged `Host` before anything reads `request.url` — that is the mitigation for the Starlette Host-header class (CVE-2026-48710). With `allow_origins=["*"]` and `allow_credentials=True`, `CORSMiddleware` reflects the caller's `Origin` instead of refusing — that is not a wildcard, it is "everyone allowed."
