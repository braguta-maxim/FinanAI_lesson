# Code Review: backend/app/market and tests

Scope: `backend/app/market/*.py`, `backend/tests/**`, `backend/pyproject.toml`.
Spec: `planning/PLAN.md` (sections 6, 12) and `planning/MARKET_DATA_SUMMARY.md`.
Method: static reading only. No tests were run, and the `massive` package source was not available locally.

Findings are ordered by severity.

## Status

Fixed (84 tests pass, ruff clean): H1 (verified against the real `massive` model: `LastTrade` has no `timestamp`; the cache now stamps poll time), H2, H3, H4, M1, M2, M4 (resilience and event-probability tests now assert behaviour), M5 (random events), L3, S3, S5.

Not changed: M3 (normalization stays at the API layer per PLAN section 8), M5 remaining coverage, M6, L1, L2 (harmless log noise), L4 (needs a real free key), S1, S2, S4, S6-S8.

---

## High

### H1. The Massive snapshot parsing may use an attribute that does not exist (needs verification)
`backend/app/market/massive_client.py:101-103`
```python
price = snap.last_trade.price
timestamp = snap.last_trade.timestamp / 1000.0
```
In the Polygon client that `massive` is based on, `TickerSnapshot.last_trade` is a `LastTrade` model. Its timestamp fields are `sip_timestamp` / `participant_timestamp`, not `timestamp`, and the API's `t` value is in **nanoseconds**, not milliseconds. If that holds for `massive`, every snapshot raises `AttributeError`. The code at `:110` catches that error and logs a warning, so **no ticker ever gets a price in Massive mode**. If the attribute does exist but holds nanoseconds, timestamps come out about 10^6 times too large.
The tests cannot catch this because `tests/market/test_massive.py:11-18` builds snapshots with a bare `MagicMock()`, which accepts any attribute name.
Fix: check the field name and units against the installed `massive` package, e.g. `uv run python -c "from massive.rest.models import TickerSnapshot; help(TickerSnapshot)"`. Build test snapshots with the real model (`TickerSnapshot.from_dict({...})`) or at least `MagicMock(spec=...)`. The simplest robust option is to use `time.time()` as the cache timestamp and drop the vendor timestamp.

### H2. `create_stream_router` registers its route on a module-level router
`backend/app/market/stream.py:17`, `:26`
`router` is a module global, and every call to `create_stream_router(cache)` adds another `/prices` route to it. A second call (a test app factory, or a second `create_app()`) produces duplicate routes. The first route wins, so it stays bound to the **first** cache and the new cache is silently ignored. This also contradicts the docstring ("without globals").
Fix: create the router inside the function:
```python
def create_stream_router(price_cache: PriceCache) -> APIRouter:
    router = APIRouter(prefix="/api/stream", tags=["streaming"])
    ...
```

### H3. Race in Massive: a removed ticker can come back into the cache
`backend/app/market/massive_client.py:72-76` vs `:97-108`
`_poll_once` awaits `asyncio.to_thread(...)`, which is a network call that takes time. If `remove_ticker("X")` runs during that await, it pops `X` from the cache. When the poll returns, `X` is written back at `:104`. After that, `X` is never polled again, so it stays in the cache with a frozen price and keeps appearing in every SSE event. Trade validation also accepts it because it "has a current price".
Fix: when handling results, skip tickers that are no longer tracked:
```python
for snap in snapshots:
    if snap.ticker not in self._tickers:
        continue
```
The simulator does not have this problem because nothing awaits between `step()` and `cache.update()` (`simulator.py:265-267`).

### H4. There are no tests for the SSE endpoint
`backend/app/market/stream.py` (entire file); PLAN section 12 requires testing API routes (status codes, response shapes).
Nothing covers the `retry: 1000` preamble, the `data:` payload shape (dict keyed by ticker with all seven fields), version-based change detection (no duplicate event when nothing changed), or stopping on disconnect. A single test would have caught H2.
Fix: test `_generate_events` directly with a stub `request` whose `is_disconnected()` returns False, then True, and assert on the yielded strings. Add one `TestClient`/`httpx.AsyncClient` smoke test for the route.

---

## Medium

### M1. `PriceCache.remove` does not bump `version`
`backend/app/market/cache.py:59-62`
SSE sends events only when `version` changes (`stream.py:75-76`). After a removal nothing is pushed, so connected clients keep the removed ticker until the next price write. With Massive that is up to 15 s. It also means `version` is not a true "state changed" counter.
Fix: add `self._version += 1` inside `remove`, and add a test for it.

### M2. The SSE generator swallows `CancelledError`
`backend/app/market/stream.py:86-87`
Catching `CancelledError` and returning, without re-raising, hides the cancellation from Starlette/uvicorn. This can delay a clean shutdown and goes against asyncio guidance. The `try/except` only exists to log.
Fix: remove the `try/except`, or use `try/finally` for the log line and let the cancellation propagate.

### M3. Tickers are normalized inconsistently between the two sources
`backend/app/market/massive_client.py:67`, `:73` uppercase and strip the ticker. `backend/app/market/simulator.py:242-255` do not. The same call (`add_ticker(" aapl")`) therefore creates a different ticker depending on the env var, which breaks the "one interface" contract in PLAN section 6. PLAN section 8 puts strip/uppercase in the Trade Rules, so the API layer is the right owner.
Fix: normalize once at the API/service boundary and remove it from `MassiveDataSource`. Otherwise, apply it in both sources and add a shared interface test.

### M4. Several tests do not check what their names claim
- `tests/market/test_simulator_source.py:96-111` `test_exception_resilience` never injects an exception. It only checks that the task is running. Fix: patch `source._sim.step` with `side_effect=[RuntimeError(), {...}]` and assert the loop keeps updating the cache.
- `tests/market/test_simulator_source.py:127-138` `test_custom_event_probability` asserts nothing.
- `tests/market/test_simulator.py:68-78` `test_prices_change_over_time` passes for any single step and does not check GBM correctness.

### M5. Missing coverage for spec-required behaviour (PLAN section 12)
- **GBM math is correct**: no test exists. With `sigma=0`, check the deterministic drift `S*exp(mu*dt)`. With a fixed `np.random.seed`, check that the std of log-returns is close to `sigma*sqrt(dt)`.
- **Correlated moves**: no test that the sampled correlation between AAPL and MSFT is about 0.6, or that `np.linalg.cholesky` succeeds with many unknown tickers mixed with known ones. The matrix is positive definite by construction, but this is not locked in.
- **Random events**: with `event_probability=1.0`, assert that the per-tick move is within 2-5%. Today only a "starts cleanly" test exists.
- **Both implementations conform to the interface**: no parametrized test runs the same add/remove/get_tickers/stop contract against both sources.
- **Simulator `add_ticker`/`remove_ticker` while the loop is running**: not tested. It is safe today, but should be regression-protected.
- **Massive `remove_ticker` during an in-flight poll**: see H3.

### M6. Timing-based tests can be flaky
`tests/market/test_simulator_source.py:113-125` expects more than 2 version bumps within 50 ms at a 10 ms interval, and `:27-39` depends on real sleeps. These can fail on a loaded CI machine.
Fix: call `_sim.step()` / the loop body directly, or give the assertions generous margins.

---

## Low

### L1. `SimulatorDataSource.add_ticker` before `start()` silently drops the ticker
`backend/app/market/simulator.py:242-249` guards with `if self._sim:`, while `MassiveDataSource.add_ticker` works before start. The interface says start must come first, so this is documented, but it is a silent no-op. Either keep the pending tickers or accept the documented contract. Low impact because the lifespan handler calls `start()` first.

### L2. `MassiveDataSource.stop()` sets `_client = None` while a worker thread may still run
`backend/app/market/massive_client.py:55-63`. Cancelling the task does not stop the `to_thread` worker. If it reads `self._client` after it has been set to `None`, it raises an `AttributeError` that gets caught and logged. This is harmless but noisy. Capture the client locally in `_poll_once` and pass it to `_fetch_snapshots`.

### L3. `timestamp or time.time()` treats `0.0` as missing
`backend/app/market/cache.py:30`. Use `timestamp if timestamp is not None else time.time()`. This is trivial, but it is the usual idiom.

### L4. The Massive free tier may not include the snapshot endpoint (verify)
PLAN section 6 says polling fits "the free tier of 5 calls/min". Polygon's free (Basic) plan has historically not included the full-market snapshot endpoint. If Massive works the same way, free keys get 403 on every poll, logged at `massive_client.py:118-119`. Verify with a real free key. If needed, document the plan requirement or fall back to per-ticker previous-close endpoints.

---

## Simplification opportunities

- **S1. Rounding happens twice.** `simulator.py:116` rounds to 2 decimals, and `cache.py:36-37` rounds again. Keep the rounding only in the cache (the single write point) and return `self._prices[ticker]` from `step()`.
- **S2. `rich` is a runtime dependency but is only used by the demo.** `pyproject.toml:12`; only `market_data_demo.py:15-20` imports it. Move it to the `dev` extra so the Docker image stays smaller.
- **S3. The `conftest.py` `event_loop_policy` fixture is unnecessary.** `tests/conftest.py:6-11`. It returns the default policy, and `asyncio.DefaultEventLoopPolicy` is deprecated as of Python 3.14. Delete it.
- **S4. `@pytest.mark.asyncio` is redundant** with `asyncio_mode = "auto"` (`pyproject.toml:35`): `test_massive.py:21`, `test_simulator_source.py:11`.
- **S5. `"Connection": "keep-alive"` is a hop-by-hop header** (`stream.py:43`). The ASGI server manages it, and it is invalid under HTTP/2. Remove it.
- **S6. The `SEED_PRICES.get(ticker, random.uniform(...))` default is evaluated on every call** (`simulator.py:151`). It is harmless, but `SEED_PRICES.get(ticker) or random.uniform(50, 300)` reads more clearly.
- **S7. Tests reach into private attributes** (`_tickers`, `_cholesky`, `_client`, `_api_key`, `_cache`) throughout `test_simulator.py`, `test_massive.py` and `test_factory.py`. Use `get_tickers()` and behaviour-level assertions where possible, so refactors do not break tests.
- **S8. `MARKET_DATA_SUMMARY.md` has a wrong test count.** It lists `test_simulator.py` as 17 tests; the file has 19. The total of 73 is still correct.

---

## Verified as matching the spec

- The SSE event shape (dict keyed by ticker with `ticker, price, previous_price, timestamp, change, change_percent, direction`), the `retry: 1000` preamble, and ~500 ms change-detected pushes match PLAN section 6.
- Unknown simulator tickers start at a random price in $50-300 with default params (`simulator.py:151-152`). Seed prices and the 10 default tickers match.
- GBM formula, dt, and event rate (~1 event per 50 s across 10 tickers) are correct. The correlation matrix is positive definite for any mix of known and unknown tickers (it is a sum of PSD blocks plus a positive diagonal).
- The factory selects the source from `MASSIVE_API_KEY`, treating empty or whitespace as unset (`factory.py:24-31`).
- `SimulatorDataSource.add_ticker` seeds the cache immediately, which satisfies the Trade Rule "the simulator prices it immediately".
