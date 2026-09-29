"""Integration tests for SimulatorDataSource."""

import asyncio

import pytest

from app.market.cache import PriceCache
from app.market.simulator import SimulatorDataSource


@pytest.mark.asyncio
class TestSimulatorDataSource:
    """Integration tests for the SimulatorDataSource."""

    async def test_start_populates_cache(self):
        """Test that start() immediately populates the cache."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["SBER", "GAZP"])

        # Cache should have seed prices immediately (before first loop tick)
        assert cache.get("SBER") is not None
        assert cache.get("GAZP") is not None

        await source.stop()

    async def test_prices_update_over_time(self):
        """Test that prices are updated periodically."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)
        await source.start(["SBER"])

        initial_version = cache.version
        await asyncio.sleep(0.3)  # Several update cycles

        # Version should have incremented (prices updated)
        assert cache.version > initial_version

        await source.stop()

    async def test_stop_is_clean(self):
        """Test that stop() is clean and idempotent."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["SBER"])
        await source.stop()
        # Double stop should not raise
        await source.stop()

    async def test_add_ticker(self):
        """Test adding a ticker dynamically."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["SBER"])

        await source.add_ticker("LKOH")
        assert "LKOH" in source.get_tickers()
        assert cache.get("LKOH") is not None

        await source.stop()

    async def test_remove_ticker(self):
        """Test removing a ticker."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["SBER", "LKOH"])

        await source.remove_ticker("LKOH")
        assert "LKOH" not in source.get_tickers()
        assert cache.get("LKOH") is None

        await source.stop()

    async def test_get_tickers(self):
        """Test getting the list of active tickers."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["SBER", "GAZP"])

        tickers = source.get_tickers()
        assert set(tickers) == {"SBER", "GAZP"}

        await source.stop()

    async def test_empty_start(self):
        """Test starting with no tickers."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start([])

        assert len(cache) == 0
        assert source.get_tickers() == []

        await source.stop()

    async def test_exception_resilience(self):
        """Test that simulator continues running after errors."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.05)

        await source.start(["SBER"])
        version_before = cache.version

        # The first step raises; the loop must survive and keep updating the cache
        real_step = source._sim.step
        calls = {"n": 0}

        def flaky_step():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return real_step()

        source._sim.step = flaky_step
        await asyncio.sleep(0.3)

        assert calls["n"] > 1
        assert cache.version > version_before

        await source.stop()

    async def test_custom_update_interval(self):
        """Test using a custom update interval."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.01)
        await source.start(["SBER"])

        initial_version = cache.version
        await asyncio.sleep(0.05)  # Should get ~5 updates

        # Should have multiple updates with fast interval
        assert cache.version > initial_version + 2

        await source.stop()

    async def test_status_is_healthy(self):
        """Test that the simulator reports itself as always healthy, with no delay."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache, update_interval=0.1)
        await source.start(["SBER"])

        status = source.status()
        assert status.source == "simulator"
        assert status.healthy is True
        assert status.delay_seconds == 0
        assert status.last_success is not None

        await source.stop()

    async def test_time_scale_speeds_up_the_market(self):
        """A higher time_scale should make the simulator's internal dt larger."""
        cache = PriceCache()
        fast = SimulatorDataSource(price_cache=cache, update_interval=0.1, time_scale=100.0)
        await fast.start(["SBER"])

        assert fast._sim._dt == pytest.approx(0.1 * 100.0 / (250 * 9 * 3600))

        await fast.stop()
