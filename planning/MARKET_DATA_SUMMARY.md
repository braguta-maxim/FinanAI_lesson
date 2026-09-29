# Market Data Backend — Summary

**Status:** Complete, tested, for the Russian market (MOEX). Built per `planning/MARKET_DATA_DESIGN.md`,
which supersedes the archived US/Massive design (`planning/archive/`).

## What Was Built

A complete market data subsystem in `backend/app/market/` (14 modules) providing live price
simulation, real MOEX ISS market data, historical candles, and analytics behind one interface.

### Architecture

```
MarketDataSource (ABC)
├── SimulatorDataSource  →  GBM simulator (default, no network needed)
└── MoexDataSource       →  MOEX ISS REST poller (when MOEX_ENABLED=true, no API key needed)
        │
        ▼
   PriceCache (thread-safe, in-memory, 4-decimal rounding, per-ticker tick history)
        │
        ├──→ SSE stream endpoint (/api/stream/prices)
        ├──→ Market data REST API (/api/market/*) — status, quotes, instruments, history, analytics, correlations
        ├──→ Portfolio valuation
        └──→ Trade execution

CandleService (TTL cache + dedup + stale-if-error)
├── MoexCandleProvider       →  real MOEX candles (reuses MoexDataSource's HTTP client)
└── SyntheticCandleProvider  →  GBM-derived history, anchored to the live cached price
```

### Modules

| File | Purpose |
|------|---------|
| `models.py` | `PriceUpdate`, `Candle`, `InstrumentInfo`, `SourceStatus`, `Interval` |
| `errors.py` | `MarketDataError`, `InvalidTickerError`, `UpstreamError`, `NoDataError` |
| `tickers.py` | `normalize_ticker()`; `sync_tracking()` — the one place deciding "watchlist ∪ positions" |
| `interface.py` | `MarketDataSource` ABC — `start/stop/add_ticker/remove_ticker/get_tickers`, non-abstract `status()` |
| `cache.py` | `PriceCache` — thread-safe price store, version counter for SSE change detection, tick history |
| `seed_prices.py` | MOEX seed prices, per-ticker GBM params, sector correlation groups |
| `simulator.py` | `GBMSimulator` (Cholesky-correlated GBM, `dt` derived from `update_interval`) + `SimulatorDataSource` |
| `iss.py` | MOEX ISS response parsing (`extract_quotes/instruments/candles`) + `MoexClient` (httpx, retries) |
| `moex_client.py` | `MoexDataSource` — polls MOEX ISS, immediate price on `add_ticker`, exponential backoff on failure |
| `factory.py` | `create_market_data_source()` (`MOEX_ENABLED`) and `create_candle_service()` |
| `stream.py` | `create_stream_router()` — FastAPI SSE endpoint factory |
| `history.py` | `CandleService` + `MoexCandleProvider` + `SyntheticCandleProvider` |
| `analytics.py` | SMA/EMA/RSI/Bollinger, annualized volatility, max drawdown, beta, correlations, portfolio risk |
| `sessions.py` | `is_main_session()`, `session_stats()`, `market_context_lines()` (for the LLM context) |
| `api.py` | `create_market_router()` — read-only REST API, `/api/market/*` |

### Key Design Decisions

- **Strategy pattern** — both data sources implement the same ABC; downstream code is source-agnostic.
- **PriceCache as single point of truth** — producers write, consumers read; no direct coupling.
- **No API key for real data** — MOEX ISS's free/anonymous tier needs no registration, unlike the
  Massive/Polygon path it replaces (see `planning/MASSIVE_API.md` for why that path was dropped:
  its free tier has no snapshot endpoint at all).
- **GBM with correlated moves** — Cholesky decomposition of a sector-based correlation matrix;
  banks and oil & gas correlate at 0.6, metals at 0.5, cross-sector/telecom/unknown at 0.3. `dt` is
  derived from `update_interval * time_scale` so there is one knob for simulation speed, not two.
- **Price fallback chain** (`LAST` → `LCURRENTPRICE` → `LCLOSEPRICE` → `PREVPRICE`) — a valid ticker
  that hasn't traded yet today still shows a price, so the app isn't "empty" outside market hours.
- **Synthetic candles in simulator mode** — a deterministic (per-ticker-seeded) GBM path anchored to
  the live cached price, so the chart and analytics work with zero network dependency.
- **SSE over WebSockets** — simpler, one-way push, universal browser support.

## Test Suite

**234 tests, all passing** (98% coverage). See `planning/MARKET_DATA_REVIEW.md` for the code review
that closed the gap from 217 to 234 tests. 13 test modules in `backend/tests/market/`, plus recorded MOEX ISS
JSON fixtures in `tests/market/fixtures/` (no real network calls in the unit suite).

| Module | Focus |
|--------|-------|
| `test_models.py` | `PriceUpdate`, `Candle`, `InstrumentInfo`, `SourceStatus`, `Interval` |
| `test_cache.py` | Rounding, version counter, tick history |
| `test_simulator.py` | GBM math, correlation groups, random events, `dt` derivation |
| `test_simulator_source.py` | `SimulatorDataSource` lifecycle, `status()`, `time_scale` |
| `test_factory.py` | `MOEX_ENABLED` parsing, source/candle-service selection |
| `test_iss.py` | ISS response parsing, price fallback chain, `MoexClient` retries/pagination |
| `test_moex_source.py` | `MoexDataSource` lifecycle, the H3 remove-during-poll race, backoff, status |
| `test_contract.py` | One parametrized test suite run against both `MarketDataSource` implementations |
| `test_history.py` | `CandleService` TTL/dedup/stale-if-error; synthetic-candle determinism and OHLC bounds |
| `test_analytics.py` | Reference values for SMA/EMA/RSI/Bollinger/drawdown/beta/correlation/portfolio risk |
| `test_sessions.py` | Trading-hours check, session stats, LLM context lines |
| `test_api.py` | `/api/market/*` status codes and response shapes |
| `test_stream.py` | SSE endpoint: preamble, payload shape, change detection, disconnect handling |

## Demo

A Rich terminal demo is available at `backend/market_data_demo.py`:

```bash
cd backend
uv run market_data_demo.py
```

Displays a live-updating dashboard with the 10 default MOEX tickers, sparklines, color-coded
direction arrows, and an event log for notable price moves, in rubles. Runs 60 seconds or until Ctrl+C.

## Usage for Downstream Code

```python
from app.market import PriceCache, create_market_data_source, create_candle_service, sync_tracking

# Startup
cache = PriceCache()
source = create_market_data_source(cache)  # Reads MOEX_ENABLED
candles = create_candle_service(cache, source)
await source.start(["SBER", "GAZP", "LKOH", ...])

# Read prices
update = cache.get("SBER")          # PriceUpdate or None
price = cache.get_price("SBER")     # float or None
all_prices = cache.get_all()        # dict[str, PriceUpdate]

# Dynamic watchlist (PLAN §6): call after any watchlist/position change
await sync_tracking(source, "LKOH", in_watchlist=True, has_position=False)

# Shutdown
await source.stop()
```
