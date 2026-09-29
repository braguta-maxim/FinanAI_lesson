# FinAlly Backend

FastAPI backend for the FinAlly AI Trading Workstation.

## Structure

- `app/` - Application code
  - `market/` - Market data subsystem
    - `models.py` - PriceUpdate, Candle, InstrumentInfo, SourceStatus, Interval
    - `errors.py` - MarketDataError and subclasses
    - `tickers.py` - Ticker normalization and watchlist/position tracking sync
    - `cache.py` - Thread-safe price cache with tick history
    - `interface.py` - MarketDataSource abstract interface
    - `simulator.py` - GBM-based market simulator
    - `iss.py` - MOEX ISS response parsing and HTTP client
    - `moex_client.py` - MoexDataSource (MOEX ISS API poller)
    - `factory.py` - Data source and candle service factories
    - `stream.py` - SSE streaming endpoint (`/api/stream/prices`)
    - `history.py` - Historical candles (MOEX and synthetic providers)
    - `analytics.py` - Indicators, risk, and correlations (pure numpy)
    - `sessions.py` - Trading hours and session tick statistics
    - `api.py` - Read-only REST API (`/api/market/*`)
    - `seed_prices.py` - Default MOEX watchlist seed prices and GBM parameters

- `tests/` - Unit and integration tests
  - `market/` - Market data tests (fixtures/ holds recorded MOEX ISS responses)

## Running Tests

```bash
# Install dependencies
uv sync --extra dev

# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=app --cov-report=html

# Run specific test file
uv run pytest tests/market/test_simulator.py

# Run with verbose output
uv run pytest -v
```

## Environment Variables

- `MOEX_ENABLED` - Optional. Set to `true` to fetch real prices from the MOEX ISS API (no API key
  needed; free/anonymous access is ~15 minutes delayed). If unset or anything else, the built-in
  GBM simulator is used.

## Development

```bash
# Install dependencies
uv sync --extra dev

# Run linter
uv run ruff check .

# Format code
uv run ruff format .
```

## Demo

```bash
uv run market_data_demo.py
```

A Rich terminal dashboard of the default MOEX watchlist (SBER, GAZP, LKOH, ...) driven by the
simulator. See `planning/MARKET_DATA_SUMMARY.md` for the full subsystem writeup.
