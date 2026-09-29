# Backend — Developer Guide

## Project Setup

```bash
cd backend
uv sync --extra dev   # Install all dependencies including test/lint tools
```

## Market Data API

The market data subsystem lives in `app/market/`. See `planning/MARKET_DATA_DESIGN.md` for the
full design and `planning/MOEX_API.md` for the underlying MOEX ISS API research. Use these imports:

```python
from app.market import (
    PriceCache, PriceUpdate, MarketDataSource, Candle, InstrumentInfo, SourceStatus, Interval,
    create_market_data_source, create_candle_service, create_stream_router, create_market_router,
    normalize_ticker, sync_tracking,
)
```

### Core Types

- **`PriceUpdate`** — Immutable dataclass: `ticker`, `price`, `previous_price`, `timestamp`, plus properties `change`, `change_percent`, `direction` ("up"/"down"/"flat"), and `to_dict()` for JSON serialization.

- **`PriceCache`** — Thread-safe in-memory store. Key methods:
  - `update(ticker, price, timestamp=None) -> PriceUpdate` (price rounded to 4 decimals)
  - `get(ticker) -> PriceUpdate | None`
  - `get_price(ticker) -> float | None`
  - `get_all() -> dict[str, PriceUpdate]`
  - `get_history(ticker) -> list[tuple[float, float]]` — `(timestamp, price)` ticks since server start
  - `remove(ticker)`
  - `version` property — monotonic counter, increments on every update and removal that changed something (for SSE change detection)

- **`MarketDataSource`** — Abstract interface implemented by `SimulatorDataSource` and `MoexDataSource`. Lifecycle: `start(tickers)` -> `add_ticker()` / `remove_ticker()` -> `stop()`. Also has a non-abstract `status() -> SourceStatus` (source name, health, delay, last error).

- **`create_market_data_source(cache)`** — Factory. Returns `MoexDataSource` if `MOEX_ENABLED=true`, otherwise `SimulatorDataSource`. No API key is required for MOEX.

- **`create_candle_service(cache, source)`** — Factory for a `CandleService`: MOEX-backed candles when `source` is a `MoexDataSource`, synthetic (GBM-derived) candles otherwise, so the chart and analytics always have history to work with.

- **`normalize_ticker(raw)`** — strip + uppercase + validate "1-5 Latin letters"; raises `InvalidTickerError`. Shared by the market data REST API and (per PLAN §8) the trade/watchlist service.

- **`sync_tracking(source, ticker, *, in_watchlist, has_position)`** — the single place that decides whether a source tracks a ticker; call after any watchlist/position change.

### SSE Streaming

```python
from app.market import create_stream_router

router = create_stream_router(price_cache)  # Returns FastAPI APIRouter
# Endpoint: GET /api/stream/prices (text/event-stream)
```

### Market Data REST API

```python
from app.market import create_market_router

router = create_market_router(price_cache, source, candle_service, instrument_lookup)
# Endpoints: GET /api/market/status|quotes|quotes/{t}|instruments/{t}|history/{t}|analytics/{t}|correlations
```

`instrument_lookup` is an `async def(ticker: str) -> InstrumentInfo | None` — for MOEX,
`source.client.get_instruments([t])`; for the simulator, a small static name lookup.

### Seed Data

Default tickers (MOEX TQBR board): SBER, GAZP, LKOH, GMKN, ROSN, NVTK, MTSS, TATN, PLZL, VTBR.
Seed prices and per-ticker volatility/drift/sector-group params are in `app/market/seed_prices.py`.

## Running Tests

```bash
uv run pytest -v                          # All tests
uv run pytest --cov=app                   # With coverage
uv run ruff check app/ tests/             # Lint
```

Market data tests use `httpx.MockTransport` and recorded JSON fixtures in
`tests/market/fixtures/` — no real network calls in the unit test suite.

## Demo

```bash
uv run market_data_demo.py   # Live terminal dashboard with simulated MOEX prices
```
