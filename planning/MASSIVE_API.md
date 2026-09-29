# Massive API Research

Research notes on the [Massive](https://massive.com) API (formerly Polygon.io, renamed October 2025) for
retrieving stock prices, to inform the unified market data interface in `MARKET_INTERFACE.md`. Verified against
the installed `massive` Python client (v2.2.0, `backend/.venv/lib/.../site-packages/massive/`) and the vendor's
public docs as of 2026-09-28.

## 1. Plans and what they actually include

Massive sells access per asset class (Stocks, Options, Indices, Forex, Crypto, Futures). This document covers
**Stocks** only, since that is all FinAlly needs.

| Plan | Price | Data recency | Rate limit | Snapshot endpoints | Last Trade / Last Quote |
|---|---|---|---|---|---|
| **Basic** (free) | $0 | End-of-day only | 5 calls/min | Not included | Not included |
| **Starter** | $29/mo | 15-min delayed | Unlimited | Included | Not included |
| **Developer** | $79/mo | 15-min delayed | Unlimited | Included | Included |
| **Advanced** | $199/mo | Real-time | Unlimited | Included | Included |

Sources: [Pricing](https://massive.com/pricing), [Full Market Snapshot](https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot),
[Last Trade](https://massive.com/docs/rest/stocks/trades-quotes/last-trade),
[Previous Day Bar](https://massive.com/docs/rest/stocks/aggregates/previous-day-bar),
[rate limit FAQ](https://massive.com/knowledge-base/article/what-is-the-request-limit-for-massives-restful-apis).

### This is the critical finding for this project

**PLAN.md §6 assumes the free key polls the snapshot endpoint at 5 calls/min. That does not work: the
snapshot endpoints (`get_snapshot_all`, `get_snapshot_ticker`) require a paid Starter plan or higher.** A
free Basic key gets a 403 on every snapshot call. Only end-of-day aggregate endpoints (previous close, daily
open/close, custom bars) are available on Basic, and they return yesterday's closing price all day — not a
live price. Basic is not useful for a "live-updating" trading terminal at all.

**Recommendation**, carried into `MARKET_INTERFACE.md`: treat `MASSIVE_API_KEY` as requiring at least the
**Starter** plan. Document this requirement next to the env var. Do not attempt to build an EOD-only fallback
path for Basic keys — it would show a frozen price all session, which contradicts the product's core promise
("watch prices stream") and is not worth the complexity for a capstone project. If a Basic key is used, the
snapshot call fails with `BadResponse` on the first poll; treat that as a startup configuration error (log
clearly and fall back to the simulator) rather than a silent per-ticker gap.

## 2. Client library

```bash
uv add massive
```

```python
from massive import RESTClient

client = RESTClient(api_key="...")  # or MASSIVE_API_KEY env var, picked up automatically
```

`RESTClient` is synchronous (built on `urllib3`). Every call in this document is a blocking network call and
must be wrapped in `asyncio.to_thread(...)` when used from an async app — see `MARKET_INTERFACE.md` §4.

## 3. Full Market Snapshot (the endpoint this project uses)

**Requires Starter or higher.**

```
GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,GOOGL,MSFT
```

```python
from massive.rest.models import SnapshotMarketType

snapshots = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "GOOGL", "MSFT"],  # omit for the whole market (10,000+ tickers)
)
for snap in snapshots:
    print(snap.ticker, snap.last_trade.price if snap.last_trade else None)
```

One call returns a `list[TickerSnapshot]`, one entry per requested ticker — this is what makes it fit a
5-tickers-to-50-tickers watchlist in a single request regardless of plan tier's rate limit. Fields on
`TickerSnapshot` (from `massive.rest.models.snapshot`, verified against the installed package source):

| Field | Type | Notes |
|---|---|---|
| `ticker` | `str` | |
| `last_trade` | `LastTrade \| None` | "Only returned if your current plan includes trades" (vendor docs) — confirmed separately that the standalone Last Trade endpoint requires Developer+, so this field is `None` on Starter. Reading `.price` on `None` raises `AttributeError`. |
| `day` | `Agg \| None` | Today's running OHLCV bar ("the most recent daily bar for this ticker", per the vendor docs). `day.close` is populated on Starter (it's aggregate data, not tick data) — `last_trade` is not. **Not independently confirmed**: whether `day.close` updates continuously intraday (standard Polygon-family behavior, and consistent with the endpoint's stated purpose "get the most up-to-date market data") or only at bar close is not spelled out in Massive's current docs. Verify with a real Starter-tier key before relying on it — poll twice a few seconds apart during market hours and confirm `day.close` moves. If it turns out static, fall back to `min.close` (most recent minute bar), which is unambiguously live. |
| `prev_day` | `Agg \| None` | Yesterday's OHLCV bar. Use `prev_day.close` for a same-day % change baseline if ever needed; FinAlly computes session change from its own first-seen SSE price instead (PLAN.md §10), so this is not required. |
| `min` | `MinuteSnapshot \| None` | Most recent minute bar. |
| `updated` | `int \| None` | Nanosecond Unix timestamp of the snapshot itself — the right field for "when was this priced", independent of tier. |
| `todays_change`, `todays_change_percent` | `float \| None` | Vendor-computed change vs. previous close. Not used by FinAlly (see above). |
| `fair_market_value` | `float \| None` | Business-tier only; `None` otherwise. |

**Timestamp gotcha (confirmed against the installed model source, `massive/rest/models/trades.py`):**
`LastTrade` has no `.timestamp` attribute. The nanosecond field is `sip_timestamp` (or
`participant_timestamp`), not `timestamp`. Code that reads `snap.last_trade.timestamp` raises
`AttributeError` on every call. Prefer `snap.updated` (nanoseconds, always present) or just stamp the cache
with local poll time — see the "Current price" recommendation above; `MARKET_INTERFACE.md` uses local poll
time throughout, which sidesteps this field entirely.

**Empty-response gotcha:** "Snapshot data is cleared daily around 3:30 AM EST and begins repopulating as
exchanges report data, which can start as early as 4:00 AM EST." A ticker can legitimately have
`day=None`/`last_trade=None`/no entry at all outside this window (holidays, pre-market on a new symbol).
Treat a missing snapshot entry the same as "no price yet", per PLAN.md §6 (`current_price: null`).

**Rate limiting:** the endpoint accepts a comma-separated ticker list up to a documented maximum, so the
whole watchlist ∪ positions set is one call. At Starter's unlimited-calls tier, PLAN.md's 15s poll interval
is a deliberate choice to be a good API citizen, not a rate-limit requirement — it can be tightened (e.g. 5s)
without hitting a wall once past the Basic tier.

## 4. Error handling

The `massive` client raises two exception types (from `massive/exceptions.py`):

```python
from massive.exceptions import AuthError, BadResponse

try:
    snapshots = client.get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=tickers)
except AuthError:
    # Empty or invalid API key — raised at RESTClient construction or first call
    ...
except BadResponse as e:
    # Non-200 response (403 = plan doesn't include this endpoint, 429 = rate limited, etc.)
    # str(e) is the raw response body
    ...
```

Neither exception carries a structured status code — `BadResponse`'s message is the raw response body text.
`MARKET_INTERFACE.md` treats both as "poll failed, log and retry next interval", which matches the existing
`massive_client.py` implementation's blanket `except Exception`.

## 5. Other endpoints considered and rejected

These were evaluated as alternatives to the snapshot endpoint and rejected for FinAlly's use case:

- **`get_last_trade(ticker)`** — one ticker per call, requires Developer+ (no cheaper than snapshot, but N
  calls instead of 1 for an N-ticker watchlist). Rejected: worse rate-limit shape for no plan-tier benefit.
- **`get_previous_close_agg(ticker)` / `list_aggs(...)`** — available on Basic, but end-of-day only (see §1).
  Rejected as the live-data path for the reason in §1; not worth a separate degraded code path.
- **`list_universal_snapshots(...)`** — a newer, asset-type-agnostic snapshot endpoint covering stocks,
  options, indices, forex and crypto in one call. Functionally similar to `get_snapshot_all` for stocks, but
  its response model (`UniversalSnapshot`) is a superset shape designed for mixed asset types, which adds
  complexity FinAlly doesn't need (single asset class: stocks). Rejected in favor of the narrower, purpose-fit
  `get_snapshot_all`.
- **WebSocket streaming** — Massive offers a real-time WebSocket API (`massive.websocket`), which would
  remove polling entirely. Rejected because PLAN.md §3 deliberately chose SSE (server push to the browser)
  over a second bidirectional connection (Massive's own WS, server-side) to keep the market-data layer "one
  interface, either polls" — see `MARKET_INTERFACE.md` §1 for why polling keeps both sources symmetric.

## 6. Code example: end-to-end poll of a watchlist

```python
import asyncio
from massive import RESTClient
from massive.exceptions import AuthError, BadResponse
from massive.rest.models import SnapshotMarketType

async def poll_once(client: RESTClient, tickers: list[str]) -> dict[str, float]:
    """One poll cycle: fetch snapshots, return {ticker: price} for tickers priced this cycle."""
    def _fetch():
        return client.get_snapshot_all(market_type=SnapshotMarketType.STOCKS, tickers=tickers)

    try:
        snapshots = await asyncio.to_thread(_fetch)
    except (AuthError, BadResponse) as e:
        print(f"poll failed: {e}")
        return {}

    prices: dict[str, float] = {}
    for snap in snapshots:
        if snap.day and snap.day.close is not None:
            prices[snap.ticker] = snap.day.close
    return prices
```
