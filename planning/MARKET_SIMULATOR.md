# Market Simulator Design

Design and code structure of the GBM-based price simulator, the default market data source (used whenever
`MASSIVE_API_KEY` is unset — the common case per PLAN.md §5). Describes what is implemented in
`backend/app/market/simulator.py`, verified against the source. See `MARKET_INTERFACE.md` for how this plugs
into the shared `MarketDataSource` interface and `PriceCache`.

## 1. Why simulate, and why GBM

PLAN.md §6 requires a zero-dependency default: the app must show live-updating prices with no API key and no
network access. A simulator also gives the demo controllable drama (correlated sector moves, occasional
shocks) that real market data wouldn't reliably provide during a live classroom demo outside market hours.

**Geometric Brownian Motion** is the standard model for a single stock price walk: it guarantees the price
stays positive (unlike, say, adding Gaussian noise directly to the price) and its parameters (drift, volatility)
map directly onto intuitive concepts — "this stock trends up 5%/year with 22% annualized volatility" — that
are easy to seed per ticker and easy to explain in a course about AI-built trading tools.

## 2. The math

```
S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)
```

Where `S(t)` is the current price, `mu` is annualized drift, `sigma` is annualized volatility, `dt` is the
tick length as a fraction of a trading year, and `Z` is a (correlated, see §3) standard normal draw.

- **`dt`**: 500ms expressed as a fraction of a 252-trading-day, 6.5-hour trading year:
  `0.5 / (252 * 6.5 * 3600) ≈ 8.48e-8`. This tiny `dt` is deliberate — it produces realistic sub-cent moves
  per 500ms tick that compound into a believable daily range, rather than the price jumping around wildly
  tick to tick.
- **The `-sigma^2/2` drift correction** is the standard Itô correction that makes the *expected* price path
  match the stated annual return `mu`; without it, GBM's mean would drift upward with volatility due to
  Jensen's inequality on the log-normal distribution.
- **Per-tick output is rounded to 2 decimal places** (cents) before being returned — real prices don't have
  more precision than that, and rounding here (rather than only in `PriceCache`) also keeps `step()`'s return
  value the same as what ends up in the cache.

## 3. Correlated moves across tickers

A single shared `Z` per group would make sector-mates move in lockstep; independent `Z`s per ticker would
remove the "tech stocks move together" effect PLAN.md §6 asks for. The simulator instead draws `n` independent
standard normals per tick and multiplies by the Cholesky decomposition of a correlation matrix, producing `n`
correlated normals in one vectorized step (`numpy`):

```python
z_independent = np.random.standard_normal(n)
z_correlated = cholesky @ z_independent   # cholesky @ cholesky.T == correlation matrix
```

Pairwise correlation is assigned by sector grouping (`seed_prices.py`):

| Pair | Correlation |
|---|---|
| Two tech tickers (`AAPL, GOOGL, MSFT, AMZN, META, NVDA, NFLX`) | 0.6 |
| Two finance tickers (`JPM, V`) | 0.5 |
| Either ticker is TSLA | 0.3 (TSLA "does its own thing" even though it's nominally tech) |
| Anything else (cross-sector, or involving an unknown/dynamically-added ticker) | 0.3 |

The correlation matrix is `n x n` with 1s on the diagonal and the table above off-diagonal — a sum of
block-constant matrices plus the identity, which is positive semi-definite by construction (each block is a
non-negative scalar times an all-ones matrix, itself PSD), so `np.linalg.cholesky` never fails regardless of
how many known or unknown tickers are mixed in. This holds at any `n`, including `n=0` or `n=1`, which are
special-cased (`_rebuild_cholesky` returns `None` for `n <= 1`, and `step()` skips the multiply when there's no
Cholesky factor).

The matrix is rebuilt on every `add_ticker`/`remove_ticker` call (O(n²), fine for `n < 50`) — see §5.

## 4. Random events

On top of the smooth GBM walk, each ticker has an independent chance per tick (`event_probability`, default
`0.001`) of an extra 2-5% shock in a random direction:

```python
if random.random() < event_probability:
    shock_magnitude = random.uniform(0.02, 0.05)
    shock_sign = random.choice([-1, 1])
    price *= 1 + shock_magnitude * shock_sign
```

At the default 0.1% per tick with 10 tickers ticking twice a second, that's roughly one event every 50 seconds
across the whole watchlist — frequent enough that a few-minute demo session reliably shows at least one
"dramatic" move (PLAN.md §6: "occasional random events... for drama"), without every session looking
identical.

## 5. Code structure

Two classes, matching the split between "pure simulation math" and "the async wrapper that drives it":

### `GBMSimulator` — the math, no asyncio

```python
class GBMSimulator:
    def __init__(self, tickers: list[str], dt: float = DEFAULT_DT, event_probability: float = 0.001): ...
    def step(self) -> dict[str, float]: ...          # advance all tickers by one tick
    def add_ticker(self, ticker: str) -> None: ...     # seed + rebuild Cholesky
    def remove_ticker(self, ticker: str) -> None: ...  # drop + rebuild Cholesky
    def get_price(self, ticker: str) -> float | None: ...
    def get_tickers(self) -> list[str]: ...
```

Kept free of `asyncio` and of `PriceCache` on purpose: it's a plain, synchronous, unit-testable object (see
`tests/market/test_simulator.py`) — `step()` is a pure function of its internal state plus fresh randomness,
with no I/O. Seeding (`_add_ticker_internal`) looks up `SEED_PRICES` / `TICKER_PARAMS` for the 10 default
tickers (`seed_prices.py`) and falls back to a random price in $50-300 with `DEFAULT_PARAMS` volatility/drift
for any other 1-5 letter symbol, matching PLAN.md §6's "unknown tickers" rule.

### `SimulatorDataSource` — the `MarketDataSource` adapter

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache: PriceCache, update_interval: float = 0.5, event_probability: float = 0.001): ...
    async def start(self, tickers: list[str]) -> None: ...
    async def stop(self) -> None: ...
    async def add_ticker(self, ticker: str) -> None: ...
    async def remove_ticker(self, ticker: str) -> None: ...
    def get_tickers(self) -> list[str]: ...
```

Owns the `GBMSimulator` instance, the `PriceCache` reference, and one `asyncio.Task` (`_run_loop`) that calls
`step()` every `update_interval` seconds and writes every returned price into the cache. `start()` also seeds
the cache immediately with each ticker's starting price *before* launching the loop, so the very first SSE
push after startup already has data rather than waiting for the first tick. `add_ticker` does the same
immediate seed-then-cache-write, so a ticker added mid-session (manually, via chat, or by trading an untracked
symbol) has a price on the same event loop turn it's added, matching PLAN.md §8's trade-rule ordering.

`_run_loop`'s body is wrapped in a bare `try/except Exception: logger.exception(...)` around the
step-and-write, *inside* the `while True`, so one bad tick (in practice: never, since `step()` has no I/O and
no user input — but the guard is cheap insurance) logs and the loop keeps going on the next interval rather
than silently dying and leaving every price frozen for the rest of the session.

## 6. Tuning knobs and where they live

| Parameter | Default | Where | Effect |
|---|---|---|---|
| `update_interval` | 0.5s | `SimulatorDataSource.__init__` | Tick rate; PLAN.md §6 specifies ~500ms |
| `event_probability` | 0.001 | both classes' `__init__` | Chance of a 2-5% shock per ticker per tick |
| `dt` | `0.5 / (252*6.5*3600)` | `GBMSimulator.__init__` | Should track `update_interval`; not currently derived from it automatically — if `update_interval` is ever changed, `dt` should be recomputed to match, or the simulated annualized volatility will be wrong |
| Per-ticker `sigma`, `mu` | `seed_prices.TICKER_PARAMS` | seed data | Volatility / drift per known ticker |
| Sector correlations | `seed_prices.py` constants | seed data | `INTRA_TECH_CORR=0.6`, `INTRA_FINANCE_CORR=0.5`, `TSLA_CORR=0.3`, `CROSS_GROUP_CORR=0.3` |

The `dt`/`update_interval` coupling noted above is the one sharp edge in the current design: they're
independent constructor parameters that happen to agree today because both default correctly, but nothing
enforces that relationship if either is changed later. Worth a code comment at minimum, or deriving `dt` from
`update_interval` directly (`dt = update_interval / TRADING_SECONDS_PER_YEAR`) so there's only one knob to
turn.
