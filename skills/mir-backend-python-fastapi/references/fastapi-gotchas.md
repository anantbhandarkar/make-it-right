# FastAPI + Async SQLAlchemy 2.0 gotchas — right vs wrong

Code-level examples of the footguns in SKILL.md. Stack: FastAPI · `AsyncSession` (asyncpg) · Postgres · Redis.

---

## 1. Session per request — via Depends

```python
# session factory (module level — the FACTORY is global, the SESSION is not)
engine = create_async_engine(DSN, pool_size=10, max_overflow=20, pool_pre_ping=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session            # one session per request, closed on exit

# route
@app.post("/orders")
async def create_order(body: OrderCreate, session: AsyncSession = Depends(get_session)):
    ...
```

```python
# WRONG — module-level session shared across requests
session = SessionLocal()         # cross-request bleed, InterfaceError under concurrency
```

`expire_on_commit=False` so returning an ORM object after commit doesn't trigger a lazy load on a now-expired attribute.

## 2. Never share a session across gather

```python
# WRONG — one session, concurrent tasks corrupt its connection state
await asyncio.gather(load_a(session), load_b(session))

# RIGHT — a session per concurrent unit of work
async def load(factory):
    async with factory() as s: ...
await asyncio.gather(load(SessionLocal), load(SessionLocal))
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

## 4. Don't block the event loop

```python
# WRONG — sync driver / blocking call in async route stalls ALL concurrent requests
import requests, psycopg2          # both sync
@app.get("/x")
async def x(): r = requests.get(...)     # blocks the loop

# RIGHT — async client, or offload blocking work
import httpx
async with httpx.AsyncClient() as c: r = await c.get(...)
# for unavoidable blocking/CPU:
from starlette.concurrency import run_in_threadpool
result = await run_in_threadpool(cpu_heavy, arg)
```

## 5. Input schema is an allow-list (anti mass-assignment) + response_model

```python
class OrderCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")     # reject unknown fields
    sku: str
    qty: int
    # NOTE: no id, no user_id, no is_admin — client cannot set these

@app.post("/orders", response_model=OrderOut)     # response_model strips internal fields
async def create_order(body: OrderCreate, user=Depends(get_current_user), ...):
    order = Order(sku=body.sku, qty=body.qty, user_id=user.id)   # server sets owner
    # WRONG would be: Order(**body.model_dump()) then trusting client fields
```

## 6. Auth vs authorization are two dependencies

```python
async def get_current_user(token=Depends(oauth2)) -> User: ...     # authentication

async def get_owned_order(order_id: int, user=Depends(get_current_user),
                          session=Depends(get_session)) -> Order:
    order = await session.get(Order, order_id)
    if order is None or order.user_id != user.id:
        raise HTTPException(404)        # 404 not 403 — don't leak existence
    return order                        # authorization happens HERE, at object load
```
The IDOR bug is fetching by `order_id` with only `get_current_user` and no ownership check.

## 7. Durable work needs a real queue, not BackgroundTasks

```python
# OK for best-effort only (lost if process dies, no retry):
@app.post("/x")
async def x(bg: BackgroundTasks): bg.add_task(log_something)

# For "must happen" work — enqueue to Redis-backed worker, idempotent handler:
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
