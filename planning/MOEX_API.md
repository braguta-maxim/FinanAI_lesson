# MOEX ISS API Research

Research notes on the Moscow Exchange's **ISS** (Informational & Statistical Server) API for retrieving stock
prices, as a possible market data source alongside or instead of Massive (`archive/pre-moex/MASSIVE_API.md`). Everything below
was verified with live requests against `https://iss.moex.com` on 2026-09-29 (not just read from docs) —
example commands are included so the results can be reproduced.

## 1. Access tiers — and why this beats Massive's free tier

| Tier | Cost | Data recency | Auth |
|---|---|---|---|
| Anonymous | Free | ~15 min delayed | None — no key, no registration, no signup |
| Subscription | Paid (contract with MOEX) | Real-time | Session cookie from a login endpoint |

**No API key is required for the free tier at all** — every example in this document is an unauthenticated
`GET` request. This is a meaningfully better starting point than Massive: `archive/pre-moex/MASSIVE_API.md` §1 found that
Massive's free Basic tier is end-of-day only (yesterday's closing price, frozen all session) and that a
*live-ish* price needs a paid Starter plan ($29/mo). MOEX's free tier gives a **continuously updating** price,
just ~15 minutes behind the real market — good enough to "watch prices stream" (PLAN.md §2) without spending
anything.

### The 15-minute delay, confirmed empirically

Polling `.../securities/SBER.json` three times, 5 seconds apart:

```bash
curl -s "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/SBER.json?iss.meta=off&iss.only=marketdata"
```

| Poll | `TIME` (last trade) | `SYSTIME` (server clock) | Gap |
|---|---|---|---|
| 1 | 08:25:24 | 08:40:25 | 15:01 |
| 2 | 08:25:29 | 08:40:30 | 15:01 |
| 3 | 08:25:36 | 08:40:36 | 15:00 |

`TIME` advances in step with `SYSTIME` (both moved forward ~5-7s between polls, matching the actual wait), at
a constant ~15-minute offset. This is a live, moving feed — not a static daily snapshot — just delayed, which
matches MOEX's own statement that "delayed data (15 minutes) is provided for free" while real-time requires a
subscription.

## 2. The single request that replaces Massive's snapshot endpoint

```
GET /iss/engines/stock/markets/shares/boards/TQBR/securities.json
```

Called with no ticker filter, this returned **all 506 securities on the TQBR board** (regular equities, the
board essentially every liquid Russian stock trades on) in one response — the same shape of capability as
Massive's `get_snapshot_all`, but free and keyless:

```bash
curl -s "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?iss.meta=off&iss.only=marketdata"
```

The response has an ISS-typical `{"columns": [...], "data": [[...], ...]}` shape — an array of column names
paired with an array of row-arrays, not an array of objects. Relevant `marketdata` columns (there are ~50;
these are the ones this project needs):

| Column | Meaning |
|---|---|
| `SECID` | Ticker, e.g. `"SBER"` |
| `LAST` | Last trade price (the field to use as "current price") |
| `TIME` | Time of that last trade, `HH:MM:SS`, exchange-local (Moscow) time |
| `SYSTIME` | Server timestamp of the response itself, `YYYY-MM-DD HH:MM:SS` |
| `OPEN`, `LOW`, `HIGH` | Today's session bar so far |
| `LASTCHANGE`, `LASTCHANGEPRCNT` | Change vs. the previous trade (not previous close) |
| `TRADINGSTATUS` | `"T"` observed for an actively trading security |

A ticker not filtered would show up with a `null` `LAST` if it exists on the board but hasn't traded yet
today (illiquid names, pre-open). To restrict the payload to just the watchlist, pass `securities` as a
comma-separated filter:

```bash
curl -s "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?securities=SBER,GAZP,LKOH&iss.meta=off&iss.only=marketdata"
```

(Narrowing isn't necessary for cost/rate-limit reasons the way it is with Massive's per-call pricing — it's
just a smaller payload to parse.)

## 3. Single-ticker request

```
GET /iss/engines/stock/markets/shares/boards/TQBR/securities/{TICKER}.json
```

Returns the same `securities` + `marketdata` blocks scoped to one ticker. Verified live:

```bash
curl -s "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/SBER.json?iss.meta=off"
# marketdata.data[0]: [..., "LAST": 273.28, "TIME": "08:25:11", "SYSTIME": "2026-09-29 08:40:11", "TRADINGSTATUS": "T", ...]
```

## 4. Unknown / invalid ticker: empty data, not an error

```bash
curl -s -o /dev/null -w "%{http_code}" "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities/NOTATICKER.json"
# 200
```

An unknown symbol returns **HTTP 200** with `"data": []` in both the `securities` and `marketdata` blocks —
never a 404 or an error payload. Any client must treat "empty `data` array" as "no such ticker" / "no price
available", the same conceptual case PLAN.md §6 already handles for Massive (`current_price: null`).

## 5. Historical daily bars (candles) — for charts and an EOD fallback

```
GET /iss/engines/stock/markets/shares/boards/TQBR/securities/{TICKER}/candles.json?from=YYYY-MM-DD&till=YYYY-MM-DD&interval=24
```

`interval=24` is daily bars (other values: `1`/`10`/`60` minutes, `7` weekly, `31` monthly). Verified live for
SBER over September 2026 — returns `open, close, high, low, value, volume, begin, end` per day, e.g.:

```json
{"open": 276.03, "close": 272.21, "high": 277.77, "low": 271.06, "volume": 26826095,
 "begin": "2026-09-01 00:00:00", "end": "2026-09-01 23:59:59"}
```

There is also a dedicated **history** endpoint for official end-of-day closes on a specific past date
(paginated at 100 rows/page — use `start=100`, `start=200`, ... for the rest of the board):

```bash
curl -s "https://iss.moex.com/iss/history/engines/stock/markets/shares/boards/TQBR/securities.json?date=2026-09-25&iss.only=history&history.columns=SECID,CLOSE,TRADEDATE"
```

Returns nothing for a non-trading day (verified: `date=2026-09-26`, a Saturday, returned an empty array;
`date=2026-09-25`, a Friday, returned 100 rows) — a market-holidays/weekends check is free: an empty response
means no session that day, no separate holiday calendar lookup needed for a "was there trading" check.

## 6. Rate limits

**No official published number was found**, and it is not documented alongside the reference at
`iss.moex.com/iss/reference/`. A widely-repeated "~10 requests/second" figure appears in web search summaries
without a traceable source — treat that as unverified, not a fact to design around. What is directly
observed: a handful of requests a few seconds apart (§1, §5 above) hit no throttling or errors. Recommended
approach for this project: poll at the same conservative cadence already used for Massive (15s, per PLAN.md
§6) or a bit faster, and treat any non-200 response as "poll failed, log and retry next interval" — the same
defensive pattern `massive_client.py` already uses — rather than assuming a specific limit.

## 7. Client libraries

| Package | Style | Wraps | Notes |
|---|---|---|---|
| [`apimoex`](https://pypi.org/project/apimoex/) (v1.5.0) | Sync | `requests` | Thin: builds ISS URLs, parses the `columns`/`data` shape into rows |
| [`aiomoex`](https://pypi.org/project/aiomoex/) (v2.2.0) | Async | `aiohttp` | Same API surface, natively `async`/`await` |

Both are thin enough that using one is a convenience, not a requirement — the raw endpoints are plain
`GET` + JSON, reachable with any HTTP client. `aiomoex` is the better fit for this project specifically: unlike
`massive_client.py`, which wraps a synchronous `RESTClient` in `asyncio.to_thread(...)` because the `massive`
package has no async client, `aiomoex` calls fit directly into the existing `async def _poll_once` pattern
with no thread hop needed.

## 8. Error handling

Neither `apimoex` nor `aiomoex` defines custom exception types (verified: no `exceptions` module in either
package, unlike `massive`). A non-200 HTTP status or a malformed JSON body raises the underlying HTTP client's
own exception (`requests.exceptions.RequestException` / `aiohttp.ClientError`), which a `MoexDataSource` should
catch broadly around each poll, matching the "log and retry next interval" pattern in §6.

## 9. Scope beyond stocks (for context, not used here)

The ISS root (`/iss/index.json`) lists eleven engines beyond `stock`: `currency` (FX), `futures`, `commodity`,
`state` (government bond placements), `money`, and others. FinAlly only needs `stock` → `shares` → board
`TQBR` (the main equities board); this is noted so a future agent doesn't need to re-discover that MOEX covers
more than equities.

## 10. Code example: end-to-end poll of a watchlist

```python
import aiohttp

ISS_BASE = "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json"

async def poll_once(session: aiohttp.ClientSession, tickers: list[str]) -> dict[str, float]:
    """One poll cycle: fetch marketdata for the given tickers, return {ticker: price}."""
    params = {
        "securities": ",".join(tickers),
        "iss.meta": "off",
        "iss.only": "marketdata",
        "marketdata.columns": "SECID,LAST",
    }
    try:
        async with session.get(ISS_BASE, params=params, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            resp.raise_for_status()
            body = await resp.json()
    except aiohttp.ClientError as e:
        print(f"poll failed: {e}")
        return {}

    rows = body["marketdata"]["data"]
    columns = body["marketdata"]["columns"]
    secid_i, last_i = columns.index("SECID"), columns.index("LAST")
    return {row[secid_i]: row[last_i] for row in rows if row[last_i] is not None}
```

## 11. Summary: MOEX vs. Massive for this project

| | Massive free tier | Massive Starter+ | MOEX anonymous |
|---|---|---|---|
| Cost | $0 | $29+/mo | $0 |
| Key required | Yes (still gated) | Yes | **No** |
| Data recency | End-of-day only | 15-min delayed / real-time | 15-min delayed |
| Multi-ticker in one call | N/A (blocked) | Yes | Yes |
| Market covered | US equities | US equities | Russian equities (MOEX) |

MOEX's free tier is strictly more useful than Massive's free tier for a live-updating demo, at the cost of
covering a different market (Russian stocks, not US ones). It cannot serve as a drop-in *replacement* for
Massive if the product's identity is "watch AAPL/GOOGL/TSLA stream" — that requires switching the default
watchlist and currency to match. See `archive/pre-moex/MARKET_INTERFACE.md` for how a `MoexDataSource` fits the existing
`MarketDataSource` interface, and the open decision on how it coexists with the US-market path.
