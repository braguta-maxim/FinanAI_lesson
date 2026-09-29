# Unified Market Data Interface

Design for the Python interface that both market data sources (Massive API, GBM simulator) implement. This
describes what is already built in `backend/app/market/` (verified against the source; see the file
references throughout) and the rationale connecting it to `MASSIVE_API.md`'s findings and PLAN.md §6. It is
the shared contract other agents build against — see `backend/CLAUDE.md` for the import surface.

## 1. Why one interface, and why polling

Both PLAN.md and the implemented code treat the simulator and Massive as interchangeable: the API layer, the
SSE stream, trade validation, and the frontend never know which one is running. This requires:

1. **The same abstract lifecycle for both** — `start`, `stop`, `add_ticker`, `remove_ticker`, `get_tickers` —
   so the FastAPI lifespan handler and the watchlist/trade services call one code path regardless of source
   (`interface.py`).
2. **The same write target** — both sources write into one `PriceCache`; nothing downstream reads from a
   source directly (`cache.py`).
3. **Both sources poll, neither streams from the vendor.** The simulator "polls" itself every 500ms by
   construction. Massive polls its REST snapshot endpoint on a timer rather than opening its own WebSocket.
   This is a deliberate simplification, not a limitation: a vendor WebSocket would need its own reconnect
   logic, its own auth, and would deliver updates on Massive's schedule instead of the fixed cadence the SSE
   layer expects — two different liveness models to reconcile instead of one. Polling both ways means "did
   the cache change" is the only question the SSE layer ever has to ask (§5).

## 2. The interface (`interface.py`)

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None: ...
    async def stop(self) -> None: ...
    async def add_ticker(self, ticker: str) -> None: ...
    async def remove_ticker(self, ticker: str) -> None: ...
    def get_tickers(self) -> list[str]: ...
```

Contract notes (from the docstrings and how both implementations honor them):

- `start()` is called exactly once, with the initial `watchlist ∪ positions` set (PLAN.md §6). Calling it
  twice is undefined — neither implementation guards against it, matching the lifespan handler's single call.
- `add_ticker` / `remove_ticker` are no-ops if the ticker is already present / already absent.
- `remove_ticker` also removes the ticker from the `PriceCache` — the source owns that side effect, so the
  caller (the watchlist/trade service) never touches the cache directly.
- `get_tickers()` is synchronous and side-effect-free; it's for introspection (tests, debugging), not on the
  request hot path.
- `stop()` is safe to call multiple times and after a `start()` that never happened.

## 3. The shared price cache (`cache.py`)

```python
class PriceCache:
    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate: ...
    def get(self, ticker: str) -> PriceUpdate | None: ...
    def get_all(self) -> dict[str, PriceUpdate]: ...
    def get_price(self, ticker: str) -> float | None: ...
    def remove(self, ticker: str) -> None: ...
    version: int  # property; increments on every update and every removal that changed something
```

- Thread-safe (`threading.Lock`), not asyncio-lock: both sources currently only ever write from the event
  loop (the simulator's own task, or Massive's `asyncio.to_thread` result being written back on the loop), so
  this is defensive rather than load-bearing today. It costs nothing and protects the demo scenario used in
  `market_data_demo.py`, which reads `get_all()` from a separate thread.
- `update()` computes `previous_price` from whatever was cached before, then produces an immutable
  `PriceUpdate` (§4). The first update for a new ticker has `previous_price == price` (`direction: "flat"`),
  which is what makes a freshly added ticker not flash on its first tick.
- `version` is the single signal the SSE layer polls (§5) — a plain counter, not a hash, so "did anything
  change" is an integer comparison. Both a price change and a removal bump it; a removal of an unknown ticker
  does not (verified by `tests/market/test_cache.py::test_remove_increments_version` /
  `test_remove_nonexistent_keeps_version`).
- `timestamp` defaults to wall-clock time if not passed. Both sources pass no explicit timestamp today — the
  simulator because a tick has no vendor time to report, and Massive because `MASSIVE_API.md` §3 found no
  reliable per-trade timestamp field worth using instead of poll time.

## 4. The price model (`models.py`)

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float
    previous_price: float
    timestamp: float
    # properties: change, change_percent, direction, to_dict()
```

`to_dict()` produces exactly the SSE event shape from PLAN.md §6 (`ticker, price, previous_price, timestamp,
change, change_percent, direction`) — this dataclass is the one place that shape is defined; the SSE
generator (§5) just serializes whatever `get_all()` returns.

## 5. SSE integration (`stream.py`)

`create_stream_router(price_cache)` returns an `APIRouter` built fresh per call (not a module-level global —
this matters because a stray second call, e.g. from a test app factory, must not silently bind to the first
`PriceCache` instance). The generator:

1. Sends `retry: 1000` once, so `EventSource`'s built-in reconnect retries after 1s.
2. Every 500ms, compares `price_cache.version` to the last value it sent. Unchanged → sleep and check again;
   changed → serialize `get_all()` (every tracked ticker, not just the one that moved — PLAN.md §6's payload
   shape) and yield one `data:` line.
3. Stops when `request.is_disconnected()` is true, or the task is cancelled (cancellation propagates rather
   than being swallowed, so Starlette's shutdown isn't held up).

This is why §1's "both sources poll" matters: the SSE generator's only job is to notice `version` changed. It
does not care whether that change came from a 500ms simulator tick or a 15s Massive poll landing.

## 6. The two implementations

### 6.1 `SimulatorDataSource` (`simulator.py`) — see `MARKET_SIMULATOR.md` for the GBM design in full.

### 6.2 `MassiveDataSource` (`massive_client.py`)

```python
class MassiveDataSource(MarketDataSource):
    def __init__(self, api_key: str, price_cache: PriceCache, poll_interval: float = 15.0) -> None: ...
```

- `start(tickers)` constructs the `massive.RESTClient` and does one blocking poll immediately (PLAN.md §7
  startup order relies on this: the source has prices before the startup snapshot is recorded), then launches
  the recurring poll task.
- `_poll_once()` calls `get_snapshot_all` via `asyncio.to_thread` (§3 of `MASSIVE_API.md` — the client is
  synchronous), and for each returned snapshot, writes `day.close` into the cache (see `MASSIVE_API.md` §3 for
  why `day.close` rather than `last_trade.price`, and the verification step still owed on that field). A
  snapshot for a ticker that was removed from `_tickers` while the poll was in flight is discarded rather than
  written back — a network round trip is long enough for a concurrent `remove_ticker` to race it.
- `add_ticker` / `remove_ticker` normalize (`.upper().strip()`) before touching `_tickers`; the simulator does
  not need to, since untracked tickers never reach it un-normalized (the API/trade-service layer normalizes
  per PLAN.md §8 — see the note in §7 below).
- Poll failures (`AuthError`, `BadResponse`, or any other exception from the network call) are caught, logged,
  and the loop continues at the next interval — never raised out of the background task.

### 6.3 Plan-tier requirement (carried from `MASSIVE_API.md` §1)

`MASSIVE_API_KEY` requires a **Starter-tier Massive key or higher**. A Basic (free) key's first poll fails
every time with a `BadResponse` (403) from `get_snapshot_all`, because the snapshot endpoint isn't included in
that tier. Document this next to the env var in PLAN.md §5 and the project README, rather than silently
degrading — a frozen end-of-day price for an entire session would violate the "watch prices stream" promise
that's central to the product (§1 of PLAN.md's Vision). Recommended handling: if the first poll in `start()`
raises, log the failure clearly (mention the tier requirement in the message) and let `create_app`'s startup
fail fast rather than proceeding with an empty cache — this surfaces a misconfigured key immediately instead
of shipping a terminal with every price stuck at "—".

## 7. Where normalization and unknown-ticker handling live

PLAN.md §8 assigns ticker normalization (strip, uppercase, 1-5 letter format check) to the trade/watchlist
service layer, not to the market data sources — both sources receive already-normalized tickers from that
layer in the common case. `MassiveDataSource` additionally normalizes defensively inside `add_ticker` /
`remove_ticker` (harmless, and protects direct callers such as tests or the demo script);
`SimulatorDataSource` relies entirely on the caller. This asymmetry is intentional per PLAN.md §8, not a bug —
noted here so it isn't "fixed" into inconsistency later by making the simulator normalize too, or by moving
normalization down from the service layer.

Unknown-ticker seeding (PLAN.md §6): the simulator assigns a random $50-300 price with default
volatility/drift params immediately on `add_ticker` (`seed_prices.py`'s `DEFAULT_PARAMS`, `simulator.py`'s
`_add_ticker_internal`). Massive has no equivalent concept — an unknown symbol simply doesn't appear in the
next snapshot response, so its cache entry stays absent (`current_price: null`) until Massive has data for it
or forever if the symbol is invalid, matching PLAN.md §6's documented behavior exactly.

## 8. The factory (`factory.py`)

```python
def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
    return MassiveDataSource(api_key=api_key, price_cache=price_cache) if api_key \
        else SimulatorDataSource(price_cache=price_cache)
```

Empty or whitespace-only `MASSIVE_API_KEY` is treated as unset (`.strip()` before the truthiness check),
matching PLAN.md §5's "absent or empty → simulator" rule. Returns an unstarted source; the lifespan handler
owns calling `await source.start(...)`.

## 9. Testing the contract

`backend/tests/market/` currently tests each implementation's behavior separately. Not yet covered, and worth
adding per PLAN.md §12 ("both implementations conform to the interface"): a single parametrized test file that
runs the same `start` → `add_ticker` → `remove_ticker` → `get_tickers` → `stop` sequence against both
`SimulatorDataSource` and a `MassiveDataSource` with a mocked `RESTClient`, asserting identical externally
visible behavior (cache state, `get_tickers()` output) at each step. This is the test that would have caught
the normalization asymmetry in §7 if it had ever become a real bug instead of a documented decision.
