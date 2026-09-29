"""Historical candles: providers (MOEX / synthetic) and a caching service."""

from __future__ import annotations

import asyncio
import math
import time
import zlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import numpy as np

from .cache import PriceCache
from .errors import NoDataError, UpstreamError
from .iss import MoexClient
from .models import Candle, Interval
from .seed_prices import DEFAULT_PARAMS, SEED_PRICES, TICKER_PARAMS
from .simulator import TRADING_SECONDS_PER_YEAR

# Maximum depth per interval (calendar days): guards against huge samples.
MAX_DAYS = {Interval.M1: 5, Interval.M10: 30, Interval.H1: 180, Interval.D1: 730}
# How long the candle cache lives (MOEX is 15 minutes behind anyway).
TTL_SECONDS = {Interval.M1: 60.0, Interval.M10: 120.0, Interval.H1: 300.0, Interval.D1: 900.0}
BENCHMARK = "IMOEX"


class CandleProvider(Protocol):
    async def candles(self, ticker: str, interval: Interval, start: date, end: date) -> list[Candle]: ...
    async def index_candles(self, interval: Interval, start: date, end: date) -> list[Candle]: ...


class MoexCandleProvider:
    def __init__(self, client: MoexClient) -> None:
        self._client = client

    async def candles(self, ticker, interval, start, end):
        return await self._client.get_candles(ticker, interval, start, end)

    async def index_candles(self, interval, start, end):
        return await self._client.get_index_candles(BENCHMARK, interval, start, end)


class CandleService:
    """TTL cache + de-duplication of concurrent requests + "stale if error"."""

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider
        self._cache: dict[tuple, tuple[float, list[Candle]]] = {}  # key -> (expires, candles)
        self._locks: defaultdict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get(self, ticker: str, interval: Interval, days: int) -> list[Candle]:
        """Candles for the last `days` calendar days. Raises NoDataError if ISS knows nothing about the ticker."""
        days = min(days, MAX_DAYS[interval])
        return await self._cached(
            (ticker, interval, days),
            lambda s, e: self._provider.candles(ticker, interval, s, e),
            interval,
            required=True,
        )

    async def benchmark(self, days: int) -> list[Candle]:
        """Daily IMOEX candles; [] on any problem (the benchmark is optional)."""
        days = min(days, MAX_DAYS[Interval.D1])  # same clamp get() applies, for the same reason
        try:
            return await self._cached(
                (BENCHMARK, Interval.D1, days),
                lambda s, e: self._provider.index_candles(Interval.D1, s, e),
                Interval.D1,
                required=False,
            )
        except (UpstreamError, NoDataError):
            return []

    async def _cached(self, key, fetch, interval, *, required) -> list[Candle]:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        async with self._locks[key]:  # identical requests wait for the first one
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            end = date.today()
            days = key[2]
            try:
                data = await fetch(end - timedelta(days=days), end)
            except UpstreamError:
                if hit:  # ISS is down — serve stale data
                    return hit[1]
                raise
            if not data:
                # A transient empty response for a ticker that had good data a moment ago is
                # more likely an ISS hiccup than the ticker vanishing -- serve the stale entry
                # instead of erroring, the same way an UpstreamError does above.
                if hit:
                    return hit[1]
                if required:
                    raise NoDataError(f"No market data for {key[0]}")
                return data
            self._cache[key] = (time.monotonic() + TTL_SECONDS[interval], data)
            return data


# --- synthetic candles (simulator mode) ---
#
# The app needs history even without network: the chart must not be empty and analytics must
# work. We generate a geometric Brownian motion path *backwards* from the current price: the
# path is deterministic in the ticker (crc32 as the RNG seed), and the last price matches the
# current cached price, so history "connects" to the live stream.

BAR_SECONDS = {Interval.M1: 60, Interval.M10: 600, Interval.H1: 3600, Interval.D1: 9 * 3600}
BARS_PER_DAY = {Interval.M1: 540, Interval.M10: 54, Interval.H1: 9, Interval.D1: 1}
SUBSTEPS = 8  # sub-steps per bar, to get honest high/low


class SyntheticCandleProvider:
    def __init__(self, cache: PriceCache) -> None:
        self._cache = cache

    async def candles(self, ticker, interval, start, end):
        trading_days = max(1, round((end - start).days * 5 / 7))
        n = trading_days * BARS_PER_DAY[interval]
        params = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
        sigma, mu = params["sigma"], params["mu"]
        step = BAR_SECONDS[interval] / TRADING_SECONDS_PER_YEAR / SUBSTEPS

        rng = np.random.default_rng(zlib.crc32(ticker.encode()))
        z = rng.standard_normal((n, SUBSTEPS))
        log_path = np.cumsum(((mu - 0.5 * sigma**2) * step + sigma * math.sqrt(step) * z).ravel())
        anchor = self._cache.get_price(ticker) or SEED_PRICES.get(ticker) or 100.0
        prices = (anchor * np.exp(log_path - log_path[-1])).reshape(n, SUBSTEPS)  # last point = anchor

        opens = np.concatenate(([prices[0, 0]], prices[:-1, -1]))
        closes = prices[:, -1]
        highs = np.maximum(prices.max(axis=1), opens)
        lows = np.minimum(prices.min(axis=1), opens)
        volumes = rng.integers(10_000, 500_000, size=n)

        times = self._bar_times(interval, n)
        return [
            Candle(t, round(float(o), 4), round(float(h), 4), round(float(low), 4), round(float(c), 4), float(v))
            for t, o, h, low, c, v in zip(times, opens, highs, lows, closes, volumes)
        ]

    async def index_candles(self, interval, start, end):
        return []  # no benchmark in simulator mode -> beta is None

    @staticmethod
    def _bar_times(interval: Interval, n: int) -> list[int]:
        """n ascending timestamps, the last one being "now". Daily bars skip weekends."""
        if interval.is_daily:
            day, out = datetime.now(timezone.utc).date(), []
            while len(out) < n:
                if day.weekday() < 5:
                    out.append(int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()))
                day -= timedelta(days=1)
            return out[::-1]
        step = BAR_SECONDS[interval]
        last = int(time.time()) // step * step
        return [last - (n - 1 - i) * step for i in range(n)]
