"""Tests for CandleService and its two providers (MOEX / synthetic)."""

import asyncio
import time
from datetime import date

import httpx
import pytest

from app.market.cache import PriceCache
from app.market.errors import NoDataError, UpstreamError
from app.market.history import MAX_DAYS, CandleService, MoexCandleProvider, SyntheticCandleProvider
from app.market.iss import MoexClient
from app.market.models import Candle, Interval


class FakeProvider:
    """A CandleProvider stub that counts calls and can be made to fail or return fixed data."""

    def __init__(self, candles: list[Candle] | None = None) -> None:
        self.calls = 0
        self._candles = candles if candles is not None else [Candle(1, 1, 1, 1, 1, 1)]
        self.fail_next = False

    async def candles(self, ticker, interval, start, end):
        self.calls += 1
        if self.fail_next:
            raise UpstreamError("boom")
        return self._candles

    async def index_candles(self, interval, start, end):
        return await self.candles("IMOEX", interval, start, end)


class TestCandleServiceCaching:
    async def test_second_call_within_ttl_hits_cache(self):
        provider = FakeProvider()
        service = CandleService(provider)

        await service.get("SBER", Interval.D1, 30)
        await service.get("SBER", Interval.D1, 30)

        assert provider.calls == 1

    async def test_different_keys_are_not_cached_together(self):
        provider = FakeProvider()
        service = CandleService(provider)

        await service.get("SBER", Interval.D1, 30)
        await service.get("GAZP", Interval.D1, 30)

        assert provider.calls == 2

    async def test_concurrent_identical_requests_are_deduplicated(self):
        provider = FakeProvider()
        service = CandleService(provider)

        results = await asyncio.gather(*(service.get("SBER", Interval.D1, 30) for _ in range(5)))

        assert provider.calls == 1
        assert all(r == results[0] for r in results)

    async def test_stale_data_served_when_upstream_fails_after_a_success(self):
        provider = FakeProvider()
        service = CandleService(provider)
        first = await service.get("SBER", Interval.D1, 30)

        provider.fail_next = True
        second = await service.get("SBER", Interval.D1, 30)

        assert second == first  # stale-if-error: no exception, old data returned

    async def test_stale_data_served_when_a_transient_empty_response_follows_a_success(self):
        """CODE_REVIEW §3.7: an empty-but-successful response shouldn't 404 away good stale data
        the way an UpstreamError already correctly doesn't."""
        provider = FakeProvider()
        service = CandleService(provider)
        key = ("SBER", Interval.D1, 30)
        first = await service.get(*key)

        service._cache[key] = (time.monotonic() - 1, service._cache[key][1])  # force TTL expiry
        provider._candles = []  # ISS returns nothing this time, without erroring

        second = await service.get(*key)

        assert second == first

    async def test_raises_upstream_error_with_no_cached_data_yet(self):
        provider = FakeProvider()
        provider.fail_next = True
        service = CandleService(provider)

        with pytest.raises(UpstreamError):
            await service.get("SBER", Interval.D1, 30)

    async def test_empty_result_raises_no_data_error(self):
        provider = FakeProvider(candles=[])
        service = CandleService(provider)

        with pytest.raises(NoDataError):
            await service.get("ZZZZZ", Interval.D1, 30)

    async def test_days_clamped_to_max_days_for_interval(self):
        provider = FakeProvider()
        service = CandleService(provider)

        # Interval.M1's MAX_DAYS is 5; a request for 30 days is silently clamped, not rejected.
        result = await service.get("SBER", Interval.M1, 30)
        assert result == provider._candles


class TestCandleServiceBenchmark:
    async def test_days_are_clamped_the_same_way_get_clamps_them(self):
        """CODE_REVIEW §3.6: benchmark() used to send an unbounded date range to the provider."""
        seen_ranges = []

        class RecordingProvider(FakeProvider):
            async def index_candles(self, interval, start, end):
                seen_ranges.append((end - start).days)
                return self._candles

        service = CandleService(RecordingProvider())
        await service.benchmark(100_000)

        assert seen_ranges == [MAX_DAYS[Interval.D1]]

    async def test_benchmark_returns_data_on_success(self):
        provider = FakeProvider()
        service = CandleService(provider)
        assert await service.benchmark(90) == provider._candles

    async def test_benchmark_returns_empty_list_on_failure_not_an_exception(self):
        provider = FakeProvider()
        provider.fail_next = True
        service = CandleService(provider)
        assert await service.benchmark(90) == []

    async def test_benchmark_returns_empty_list_when_no_data(self):
        provider = FakeProvider(candles=[])
        service = CandleService(provider)
        assert await service.benchmark(90) == []


class TestMoexCandleProvider:
    """CODE_REVIEW §3.4: this class had 0% coverage -- it's the only thing connecting
    CandleService to the real MOEX client for both per-ticker and benchmark candles."""

    async def test_candles_delegates_to_the_client(self):
        calls = {"n": 0}
        one_row = {"candles": {"columns": ["open", "close", "high", "low", "volume", "begin"],
                                "data": [[1.0, 2.0, 3.0, 0.5, 10.0, "2026-09-01 00:00:00"]]}}
        empty = {"candles": {"columns": ["open", "close", "high", "low", "volume", "begin"], "data": []}}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=one_row if calls["n"] == 1 else empty)

        client = MoexClient(transport=httpx.MockTransport(handler))
        provider = MoexCandleProvider(client)

        candles = await provider.candles("SBER", Interval.D1, date(2026, 9, 1), date(2026, 9, 2))

        assert len(candles) == 1
        assert candles[0].close == 2.0
        await client.aclose()

    async def test_index_candles_requests_imoex(self):
        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(200, json={"candles": {"columns": ["open", "close", "high", "low", "volume", "begin"], "data": []}})

        client = MoexClient(transport=httpx.MockTransport(handler))
        provider = MoexCandleProvider(client)

        result = await provider.index_candles(Interval.D1, date(2026, 9, 1), date(2026, 9, 2))

        assert result == []
        assert any("IMOEX" in url for url in seen)
        await client.aclose()


class TestSyntheticCandleProvider:
    async def test_deterministic_for_the_same_ticker(self):
        cache = PriceCache()
        provider = SyntheticCandleProvider(cache)

        a = await provider.candles("SBER", Interval.D1, date(2026, 8, 1), date(2026, 9, 1))
        b = await provider.candles("SBER", Interval.D1, date(2026, 8, 1), date(2026, 9, 1))

        assert [c.close for c in a] == [c.close for c in b]

    async def test_different_tickers_get_different_paths(self):
        cache = PriceCache()
        provider = SyntheticCandleProvider(cache)

        sber = await provider.candles("SBER", Interval.D1, date(2026, 8, 1), date(2026, 9, 1))
        gazp = await provider.candles("GAZP", Interval.D1, date(2026, 8, 1), date(2026, 9, 1))

        assert [c.close for c in sber] != [c.close for c in gazp]

    async def test_last_close_matches_cached_price(self):
        cache = PriceCache()
        cache.update("SBER", 273.5)
        provider = SyntheticCandleProvider(cache)

        candles = await provider.candles("SBER", Interval.D1, date(2026, 8, 1), date(2026, 9, 1))

        assert candles[-1].close == pytest.approx(273.5)

    async def test_falls_back_to_seed_price_without_a_cached_price(self):
        cache = PriceCache()  # SBER never updated
        provider = SyntheticCandleProvider(cache)

        candles = await provider.candles("SBER", Interval.D1, date(2026, 8, 1), date(2026, 9, 1))

        assert candles[-1].close == pytest.approx(273.0)  # SEED_PRICES["SBER"]

    async def test_ohlc_bounds_are_honest(self):
        cache = PriceCache()
        provider = SyntheticCandleProvider(cache)

        candles = await provider.candles("SBER", Interval.H1, date(2026, 9, 1), date(2026, 9, 8))

        for c in candles:
            assert c.low <= c.open <= c.high
            assert c.low <= c.close <= c.high

    async def test_daily_bars_skip_weekends(self):
        cache = PriceCache()
        provider = SyntheticCandleProvider(cache)

        candles = await provider.candles("SBER", Interval.D1, date(2026, 9, 1), date(2026, 9, 15))

        from datetime import datetime, timezone

        for c in candles:
            weekday = datetime.fromtimestamp(c.time, tz=timezone.utc).weekday()
            assert weekday < 5

    async def test_index_candles_are_empty(self):
        """No benchmark in simulator mode -> beta_imoex stays None, but nothing breaks."""
        cache = PriceCache()
        provider = SyntheticCandleProvider(cache)
        assert await provider.index_candles(Interval.D1, date(2026, 8, 1), date(2026, 9, 1)) == []
