# Market Data Backend (MOEX) — Code Review

**Date:** 2026-09-29
**Scope:** `backend/app/market/` (16 source files, 1730 lines) and `backend/tests/market/` (18 test
files + fixtures), against `planning/MARKET_DATA_DESIGN.md`, `planning/PLAN.md` §5-§8, and
`planning/MOEX_API.md`.
**Method:** full re-read of every source file, plus targeted live reproductions for every finding
below (not just static reading) — each bug in §3 has a runnable snippet that demonstrates it.

---

## Status: all 8 Verdict items fixed

234 tests pass (was 217), coverage is 98% (was 96%), ruff is clean. Every "Should fix" and "Nice to
have" item in §5 is done, on branch `fix/market-data-review-findings`:

1. **§3.1 fixed** — `MoexDataSource.add_ticker`/`start`/`remove_ticker` now normalize
   (`.strip().upper()`). Re-verified live against `iss.moex.com` with deliberately un-normalized
   input (`["sber", " gazp "]`) — both tickers now price correctly.
2. **§3.2 fixed** — and corrected in the process: the fix originally sketched in §3.2 ("iterate
   subtracting a day") turns out not to work either (one subtraction already lands on Sunday,
   which is `<= sys_dt`, so the loop would stop one day too early — the same bug, not fixed). The
   actual fix walks back to the nearest weekday (`_previous_business_day`), verified against both
   the original midnight-crossing case and a new Friday→Monday weekend case.
3. **§3.3 fixed** — `test_poll_loop_actually_fires_again_over_time` lets `_poll_loop` run for
   multiple real cycles and asserts the client is called again on its own.
4. **§3.4/§3.5 fixed** — added tests for `MoexCandleProvider` (both methods), `MoexClient.
   get_index_candles`/`get_instruments`, and `analyze()`'s benchmark/beta wiring specifically
   (checked against an independently-computed beta, pinning down the column order).
5. **§3.9/§3.10 fixed** — `/api/market/correlations` now skips a ticker with no data instead of
   failing the whole request (an `UpstreamError` still fails the whole request — a partial matrix
   during a real outage would be misleading), and returns `"skipped"`/`"truncated"` fields so
   neither is silent anymore.
6. **§3.6/§3.7 fixed** — `benchmark()` clamps `days` the same way `get()` does; a transient
   empty-but-successful response now falls back to stale cached data the same way an
   `UpstreamError` already did.
7. **§3.8 addressed** — `PriceCache`'s default `history_size` raised from 3,600 to 43,200 (about
   6 hours at the simulator's default tick rate instead of 30 minutes), and both `cache.py` and
   `sessions.py` now document that the window is tick-count-based, not wall-clock-based. This
   narrows the gap; a session left running past the new window still has the same caveat, now
   documented rather than silent.
8. **§4.2/§4.3 fixed** — added a test pinning down Cholesky success for the full 10-ticker default
   watchlist; the PSD precondition now raises a `ValueError` with a clear message (pointing at
   `seed_prices.py`) instead of a bare `numpy.linalg.LinAlgError`; the misleading comment in
   `seed_prices.py` is corrected to state the constraint this decomposition actually needs.

Two coverage gaps noted in §1's table but not elevated to their own Verdict item were also closed
while in the area: `MoexDataSource.stop()` closing a self-owned client, and the bare
`except Exception` branch in `_poll`.

**Left as documented, not fixed:** the exchange-holiday gap in `_previous_business_day` (§3.2's fix
handles ordinary weekends correctly; a holiday adjacent to a weekend would still be off by however
many holiday days there are — fixing that needs a real trading calendar, out of scope here); the
`tickers.py::sync_tracking` coverage gap (its caller, the trade/watchlist service, doesn't exist
yet); and §4.4 (the `isinstance` dispatch in `create_candle_service`), which was a design note, not
a defect.

---

## 1. Test Results Summary

**217 tests, all passing.** `uv run pytest -q` → `217 passed in 2.64s`. `uv run ruff check .` →
clean.

**Coverage: 96%** (908 statements, 35 missed).

| Module | Coverage | Missing |
|---|---|---|
| cache.py, errors.py, factory.py, models.py, seed_prices.py, sessions.py, `__init__.py` | 100% | |
| simulator.py | 99% | L151: `_add_ticker_internal`'s duplicate-add guard |
| api.py | 98% | L106-107: `NoDataError` branch of `/correlations` |
| analytics.py | 96% | L35, L180-183 — see §3.5 |
| history.py | 96% | L39, L42 — see §3.4 |
| stream.py | 97% | L37 (one log line) |
| iss.py | 93% | L192, L222 — see §3.4; L56-57 (malformed-time branch); L81, L92, L118, L207 (untaken branches in already-tested functions) |
| interface.py | 94% | L67: the ABC's own default `status()` body (only subclass overrides are exercised) |
| moex_client.py | 89% | L61, L90-93, L118 — see §3.1/§3.3 |
| tickers.py | 79% | L32-35: `sync_tracking()`'s entire body |

`tickers.py`'s gap is expected: `sync_tracking()` is called by the trade/watchlist service, which
doesn't exist yet (PLAN.md's own top-level `CLAUDE.md` scopes this review to the market data
component only). The rest of §1's gaps are real and are discussed in §3 and §4.2 — coverage
percentage alone overstates how well-tested this module is, because entire classes
(`MoexCandleProvider`) and entire code paths (the recurring poll loop actually firing on a timer)
happen to be small in line count but large in what they're responsible for.

---

## 2. Architecture Assessment

The subsystem cleanly extends the pre-MOEX design (`MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`)
rather than replacing its shape:

```
MarketDataSource (ABC, + non-abstract status())
├── SimulatorDataSource  →  GBM simulator (default, no network)
└── MoexDataSource       →  MOEX ISS REST poller (MOEX_ENABLED=true, no API key)
        │
        ▼
   PriceCache (thread-safe, 4-decimal rounding, per-ticker tick history)
        │
        ├──→ SSE stream (/api/stream/prices)
        ├──→ Market data REST API (/api/market/*)
        └──→ (future) portfolio valuation, trade execution

CandleService (TTL cache + de-dup + stale-if-error)
├── MoexCandleProvider       →  real candles via MoexDataSource's shared httpx client
└── SyntheticCandleProvider  →  GBM history anchored to the live cached price
```

**Strengths, confirmed by reading and by the reproductions in §3:**
- The MOEX ISS `columns`/`data` parsing (`iss.py`) is isolated from HTTP concerns (`MoexClient`),
  so every parsing edge case is testable without a mock server — and mostly is (§1).
- The price fallback chain (`LAST` → `LCURRENTPRICE` → `LCLOSEPRICE` → `PREVPRICE`) is exactly
  what `MARKET_DATA_DESIGN.md` §16.1 asked to verify, and it does the right thing on the fixture
  that models an untraded-today ticker.
- CODE_REVIEW.md's H3 (a ticker removed mid-poll must not be resurrected) is carried forward
  correctly into `MoexDataSource._poll` and has a dedicated, passing regression test.
- `dt` is now derived from `update_interval * time_scale` instead of being a hardcoded constant —
  this actually fixes a documented latent issue from `MARKET_SIMULATOR.md` §6 (the old `dt` and
  `update_interval` could silently drift apart).
- The Cholesky-PSD argument for the sector correlation matrix still holds with three groups of
  different correlation values, not just the old two-group case (verified by hand in this review:
  the matrix decomposes as a sum of non-negative-scaled outer products plus a positive diagonal
  correction, which only requires every `GROUP_CORR[g] < 1`, not the stronger "`>= CROSS_GROUP_CORR`"
  the code comment claims — see §4.3).
- Cholesky was empirically re-checked against the full 10-ticker default watchlist (`SBER, GAZP,
  LKOH, GMKN, ROSN, NVTK, MTSS, TATN, PLZL, VTBR` together) and succeeds — this exact scenario was
  flagged as an untested gap in the pre-MOEX `archive/MARKET_DATA_REVIEW.md` and still has no
  dedicated test today (§4.2).

---

## 3. Issues Found

### 3.1 A non-normalized ticker silently never gets a price from MOEX (Severity: Medium-High)

`MoexDataSource.add_ticker`/`start` do not normalize their input (no `.strip().upper()`), unlike
the retired `MassiveDataSource`, which did this defensively. `MARKET_INTERFACE.md` §7 explicitly
noted that defensive normalization "protects direct callers such as tests or the demo script" —
that protection is now gone from the MOEX path, and the failure mode is worse than before: it is
**silent**, not an error.

Reproduction (`backend/`, run with `uv run python -c "..."`):
```python
import asyncio, httpx
from app.market.cache import PriceCache
from app.market.iss import MoexClient
from app.market.moex_client import MoexDataSource

def handler(request):  # MOEX always echoes the canonical uppercase SECID
    return httpx.Response(200, json={
        "marketdata": {"columns": ["SECID", "LAST"], "data": [["SBER", 273.5]]},
        "securities": {"columns": ["SECID"], "data": [["SBER"]]},
    })

async def main():
    client = MoexClient(transport=httpx.MockTransport(handler), retries=0)
    cache = PriceCache()
    source = MoexDataSource(cache, client=client, poll_interval=100)
    await source.start(["sber"])            # lowercase, as if a caller forgot to normalize
    print(cache.get_price("sber"))           # None
    print(cache.get_price("SBER"))           # also None
    await source.stop()

asyncio.run(main())
```
Both print `None`. What happens: `_poll` requests `securities=sber`, ISS returns the quote keyed
by its canonical `SECID` (`"SBER"`), and `moex_client.py`'s guard `if ticker not in self._tickers:
continue` (the H3 fix) silently drops it, because `"SBER" not in ["sber"]`. There is no log line
for this branch at all — not even at DEBUG. The ticker is tracked forever (`get_tickers()` returns
`["sber"]`) but will never receive a price, with zero observability into why.

PLAN.md §8 places normalization in the trade/watchlist service, so this should never be reachable
through the built application *once that service exists*. It is exploitable today by anything that
calls `MoexDataSource` directly (the demo script pattern, a future admin script, a test that
forgets to normalize) and, unlike the old Massive client, gives no signal that anything went wrong.

**Fix:** normalize in `add_ticker`/`start` the same way the retired `MassiveDataSource` did — it
costs nothing and turns a silent permanent failure into a normal, comparable ticker.

### 3.2 `_trade_time` miscalculates the delay across a gap longer than 24 hours (Severity: Medium)

`iss.py`'s `_trade_time` assumes a trade can be "yesterday" relative to `SYSTIME` by at most one
day (`if trade_dt > sys_dt: trade_dt -= timedelta(days=1)`). Across a weekend or a holiday, MOEX's
`LAST`/`TIME` can be several days old — a real, expected condition for a market that's closed
Saturday/Sunday, not an edge case.

Reproduction:
```python
from app.market.iss import extract_quotes
import datetime

payload = {  # Friday close (18:50), next poll happens Monday morning (10:00)
    "marketdata": {"columns": ["SECID", "LAST", "TIME", "SYSTIME"],
                   "data": [["SBER", 273.0, "18:50:00", "2026-10-05 10:00:00"]]},
    "securities": {"columns": ["SECID"], "data": [["SBER"]]},
}
q = extract_quotes(payload)["SBER"]
print(datetime.datetime.fromtimestamp(q.trade_time, tz=datetime.timezone.utc))  # 2026-10-04 15:50 UTC (Sunday!)
print(q.delay_seconds)  # 54600 = 15h10m
```
The real gap is about 63 hours (Friday 18:50 → Monday 10:00); the code reports `trade_time` as
Sunday and a 15-hour delay. This doesn't corrupt the displayed **price** (that comes from the
separate fallback-chain logic, unaffected), but it does corrupt `delay_seconds`, which feeds
`MoexDataSource._delay` and therefore `/api/market/status` and `/api/market/quotes/{ticker}` —
both PLAN.md-visible fields whose entire purpose is honest transparency about data freshness.
After any weekend, `status.delay_seconds` understates the real staleness by roughly the number of
whole days in the gap.

**Fix:** iterate subtracting a day while `trade_dt > sys_dt` (handles any gap length), or compute
the delay from a real trading-calendar-aware "last session close" rather than a fixed one-day
lookback.

### 3.3 The recurring poll loop has zero test coverage (Severity: Medium, testing gap)

Every test that constructs a `MoexDataSource` passes `poll_interval=100` (or similar) specifically
so the periodic loop never fires during the test — the only poll exercised is the one-shot,
blocking poll inside `start()` (and the one inside `add_ticker()`). `_poll_loop`'s `while True:`
body (`moex_client.py:90-93`) has 0% coverage. This is the actual "keep polling MOEX every 15
seconds" behavior — the single most defining feature of the class — and no test proves it works.

I verified by hand that it does work correctly:
```python
# poll_interval=0.05, mock server returns an incrementing price on each call
# after start():                calls=1, price=274.0
# after asyncio.sleep(0.25):    calls=5, price=278.0
```
So there is no bug here today — but this is exactly the kind of gap that lets a future refactor
(e.g., changing `_poll_loop`'s sleep/poll order, or a typo in the backoff formula) ship silently
broken, since the test suite would stay green.

**Fix:** add one test with a short `poll_interval` that awaits past one full cycle and asserts a
second poll happened (mirrors `test_prices_update_over_time` in `test_simulator_source.py`, which
already does exactly this for the simulator).

### 3.4 The IMOEX benchmark path is untested end-to-end (Severity: Medium)

Three methods that only matter for the `beta_imoex` feature have no test at any level:
- `MoexClient.get_index_candles` (`iss.py:222`)
- `MoexCandleProvider.candles` and `MoexCandleProvider.index_candles` (`history.py:39, 42`) — this
  class has **0% coverage on both of its methods**; `test_factory.py` only checks that
  `create_candle_service` *instantiates* a `MoexCandleProvider`, never that it works.

Combined with §3.5, this means the entire "compare a stock to the IMOEX index" feature — from the
REST client through the provider wrapper through the beta calculation — has no automated proof
that the pieces are actually wired together correctly, only that each piece works in isolation
(`extract_candles` is well-tested; `beta()` is well-tested; the glue between them via a real client
is not).

**Fix:** add one `test_moex_candle_provider.py`-style test with `httpx.MockTransport` covering both
`MoexCandleProvider` methods and `MoexClient.get_index_candles`/`get_instruments`.

### 3.5 `analyze()`'s benchmark integration path is untested, and a column-swap would go unnoticed (Severity: Medium)

`analytics.py:180-183` — the branch inside `analyze()` that actually computes `beta_imoex` from a
real benchmark — has 0% coverage. Every `test_analyze_*` test calls `analyze()` with `benchmark=None`
or an empty benchmark, and `beta()` itself is unit-tested directly with hand-built arrays, but
never through `analyze()`'s own plumbing:

```python
_, m = align_closes({ticker: candles, BENCH: benchmark})
if m.shape[0] >= 30:
    r = np.diff(np.log(m), axis=0)
    b = beta(r[:, 0], r[:, 1])
```

This relies on `align_closes` preserving dict insertion order so that column 0 is the ticker and
column 1 is the benchmark. It does today (Python dicts preserve insertion order, and
`{ticker: candles, BENCH: benchmark}` inserts `ticker` first) — but if that ordering assumption
ever broke (e.g., `align_closes` were refactored to sort ticker names alphabetically for some
other reason, which would silently reorder `SBER` after `IMOEX`), `beta_imoex` would silently
become its own reciprocal-ish inverse relationship instead of erroring, and nothing would catch it.

**Fix:** add a test that calls `analyze(ticker, candles, benchmark)` with >=30 overlapping bars and
asserts `beta_imoex` against a value computed independently (e.g., via `np.polyfit`), so the
column-order assumption is pinned down by name, not just by the unit test of `beta()` alone.

### 3.6 `CandleService.benchmark()` doesn't clamp `days` against `MAX_DAYS` (Severity: Low)

`get()` clamps: `days = min(days, MAX_DAYS[interval])`. `benchmark()` does not — it passes whatever
`days` it's given straight into the cache key and the date range. Today this is harmless because
the only caller (`api.py`'s `/analytics/{ticker}`) already bounds `days` to `<= 730` via
`Query(180, ge=30, le=730)`, but `CandleService` is a general-purpose class and this asymmetry
means calling `service.benchmark(100_000)` directly would build a request for a 100,000-day date
range with no guard, unlike the equivalent `get()` call.

### 3.7 Stale-if-error only covers `UpstreamError`, not a transient empty response (Severity: Low)

`CandleService._cached` serves stale cached data when the fetch raises `UpstreamError`, but if the
fetch *succeeds* and returns an empty list for a ticker that has valid, TTL-expired cached data
from moments ago, `if not data and required: raise NoDataError(...)` fires — the good stale data
sitting right there in `self._cache[key]` is not used as a fallback, and the caller gets an
avoidable 404 instead. Narrow window (requires the TTL to have just expired and ISS to return an
empty page for a previously-good ticker), but the "stale if error" design intent doesn't fully
cover this case.

### 3.8 Default `PriceCache` tick history is a 30-minute window for the simulator, not "since start" (Severity: Low-Medium, doc/behavior mismatch)

`PriceCache(history_size=3600)` is the default everywhere (`create_market_data_source` never
overrides it). `sessions.py`'s `session_stats`/`market_context_lines` docstrings, and their
consumption in the (future) LLM context per PLAN §9, are framed as "since the server started" /
"since start". For `MoexDataSource` (15s polls), 3600 ticks ≈ 15 hours — a reasonable full-session
window. For `SimulatorDataSource` (the *default* source, ticking every 0.5s), 3600 ticks ≈ **30
minutes**:
```python
>>> 3600 * 0.5 / 60
30.0
```
Any demo session left running past 30 minutes will have `market_context_lines()` silently report
change "since start" that is actually "since ~30 minutes ago", because the oldest ticks have been
evicted from the deque. This is a real behavior gap, not just a naming quibble — the LLM context
description in PLAN §9 and the `sessions.py` docstrings both say "since start" without qualification.

**Fix:** either scale `history_size` by the same `update_interval` the source actually uses (so
the time window is constant regardless of tick rate), or document explicitly that "since start"
means "within the last `history_size * interval` seconds," not literally since server startup.

### 3.9 `/api/market/correlations` silently truncates beyond 15 tickers (Severity: Low)

`names = list(dict.fromkeys(...))[:MAX_CORR_TICKERS]` drops any tickers past the 15th with no
indication in the response — a caller passing 20 tickers gets a 200 with a `"tickers"` array of 15
and no way to tell, from the response alone, that 5 were dropped rather than simply not requested.

### 3.10 `/api/market/correlations` fails entirely if any one requested ticker has no history (Severity: Low, design choice worth confirming)

Verified by reproduction: requesting correlations for 3 tickers where one has no candle data
returns `404 {"detail": "No market data for GAZP"}` for the *whole* request, rather than computing
the matrix over the other two. `asyncio.gather` without `return_exceptions=True` means the first
`load()` failure aborts the batch. This may be the intended "fail closed" behavior, but
`MARKET_DATA_DESIGN.md` §12 doesn't specify it either way, so it's worth an explicit product
decision rather than an implicit one.

---

## 4. Design Observations

### 4.1 Things done well

- **The price fallback chain and the "empty `data` = no such ticker" contract** are exactly what
  `MOEX_API.md`'s live research found, carried through faithfully into code and tests.
- **`normalize_ticker`/`sync_tracking` centralize a rule PLAN §8 states in prose** — this is a real
  improvement over the pre-MOEX state, where normalization was duplicated (and inconsistently
  applied) between `MassiveDataSource` and callers.
- **`SyntheticCandleProvider`'s determinism** (seeded by `crc32(ticker)`, anchored to the live
  cached price) is a genuinely good solution to "history must exist with zero network" — verified
  it produces different, but each individually deterministic, paths per ticker.
- **The batch-size split in `MoexDataSource._poll`** (`batch_size=50`) is untested for the exact
  batching behavior only in one test (`test_batching_splits_large_ticker_lists`), but that one test
  is solid: it captures the exact `securities=` query params sent per request.

### 4.2 Recurring gap from the pre-MOEX review

The archived `archive/MARKET_DATA_REVIEW.md` flagged "no test for `GBMSimulator` with all 10
default tickers" as a nice-to-have. That gap is still present today — verified by hand (see §2)
that it works, but there is still no test pinning it down for the *current* 10 MOEX tickers and
sector groups, which is exactly the kind of change (new tickers, new groups, new correlation
values) that could silently break the PSD property the whole Cholesky step depends on.

### 4.3 The `GROUP_CORR >= CROSS_GROUP_CORR` comment overstates the actual constraint

`seed_prices.py`'s comment says every intra-group correlation "must be >= CROSS_GROUP_CORR,
otherwise the correlation matrix can stop being positive semi-definite." Working through the
decomposition by hand: the actual requirement for the sum-of-PSD-parts argument to go through is
only that every `GROUP_CORR[g] < 1` (so the diagonal correction term stays non-negative) — the
`>= CROSS_GROUP_CORR` relationship isn't load-bearing for *this specific* decomposition, though
it's not wrong to keep as a simplifying convention. Not a bug (today's values satisfy both the
weaker real constraint and the stated stronger one), just a comment that claims more than it proves.
Neither constraint has a runtime assertion — a future edit to `GROUP_CORR` that violated the real
constraint (`>= 1`) would only be caught by `np.linalg.cholesky` raising at runtime, with no
earlier, clearer error.

### 4.4 `create_candle_service`'s `isinstance` dispatch

`isinstance(source, MoexDataSource)` couples `factory.py` to a concrete subclass rather than to the
`MarketDataSource` interface. Fine with exactly two implementations; would need revisiting if a
third source is ever added (it would silently fall back to `SyntheticCandleProvider` rather than
failing loudly).

### 4.5 What remains unverified from `MARKET_DATA_DESIGN.md` §16.1

One item from the design document's own "not verified live" checklist still isn't verified: MOEX's
`LAST` field's behavior outside trading hours / on a weekend (does it hold the last trade, or go
`null`?). This can't be checked live from within this session (today is a Tuesday during trading
hours). The fallback chain (`LAST` → `LCURRENTPRICE` → `LCLOSEPRICE` → `PREVPRICE`) is written to
tolerate either answer, but the specific behavior is still an assumption, not a measurement.

---

## 5. Verdict

The implementation is solid and matches `MARKET_DATA_DESIGN.md` closely; 217 tests pass, ruff is
clean, and every one of the design document's "verify live" items from §16.1 that could be checked
this session was checked against the real `iss.moex.com` (documented in `MOEX_API.md`) rather than
assumed. The two real, reproducible bugs found (§3.1, §3.2) are both in edge cases outside the
common path — a caller skipping normalization, and a weekend/holiday gap — and neither corrupts
the price shown to a user, only secondary metadata (silently-unpriced ticker, and an inaccurate
delay figure).

**Should fix:**
1. Normalize tickers defensively in `MoexDataSource` (§3.1) — cheap, and turns a silent failure
   into a visible one even before the trade/watchlist service exists to prevent it upstream.
2. Fix the multi-day gap in `_trade_time` (§3.2).
3. Add a test that actually lets `_poll_loop` fire more than once (§3.3) — this is the single
   highest-value test missing from the suite given what the class is for.
4. Add coverage for `MoexCandleProvider`, `MoexClient.get_index_candles`, and `analyze()`'s
   benchmark/beta integration path (§3.4, §3.5) — three different layers of the same untested
   feature (comparing a stock to IMOEX).

**Nice to have:**
5. Decide and document the intended behavior for `/api/market/correlations` when one of several
   requested tickers has no data (§3.10), and surface truncation past 15 tickers (§3.9).
6. Make `CandleService.benchmark()`'s day clamping consistent with `get()` (§3.6), and extend
   stale-if-error to cover an empty-but-successful response the same way it covers `UpstreamError`
   (§3.7).
7. Either scale `PriceCache`'s tick-history window with the source's actual tick interval, or
   document that "since start" means "within the history window," not literally since startup (§3.8).
8. Add the all-10-default-tickers Cholesky test that both this review and the pre-MOEX review
   flagged as missing (§4.2), and add a runtime assertion (or at least a clearer error message)
   for the correlation-matrix PSD precondition (§4.3).
