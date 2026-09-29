"""One parametrized contract test run against both MarketDataSource implementations.

This is the test that would have caught the ticker-normalization asymmetry noted in
MARKET_INTERFACE.md §7 if it had ever become a real bug instead of a documented decision.
"""

import httpx
import pytest

from app.market.cache import PriceCache
from app.market.iss import MoexClient
from app.market.moex_client import MoexDataSource
from app.market.simulator import SimulatorDataSource


def _board_response(tickers: list[str]) -> dict:
    return {
        "marketdata": {"columns": ["SECID", "LAST"], "data": [[t, 100.0] for t in tickers]},
        "securities": {"columns": ["SECID"], "data": [[t] for t in tickers]},
    }


def make_simulator(cache: PriceCache) -> SimulatorDataSource:
    return SimulatorDataSource(price_cache=cache, update_interval=100)


def make_moex(cache: PriceCache) -> MoexDataSource:
    def handler(request: httpx.Request) -> httpx.Response:
        tickers = request.url.params["securities"].split(",")
        return httpx.Response(200, json=_board_response(tickers))

    client = MoexClient(transport=httpx.MockTransport(handler), retries=0)
    return MoexDataSource(cache, client=client, poll_interval=100)


@pytest.mark.parametrize("make_source", [make_simulator, make_moex], ids=["simulator", "moex"])
class TestMarketDataSourceContract:
    async def test_start_populates_cache_for_all_tickers(self, make_source):
        cache = PriceCache()
        source = make_source(cache)

        await source.start(["SBER", "GAZP"])
        assert cache.get_price("SBER") is not None
        assert cache.get_price("GAZP") is not None
        assert set(source.get_tickers()) == {"SBER", "GAZP"}

        await source.stop()

    async def test_add_ticker_is_idempotent(self, make_source):
        cache = PriceCache()
        source = make_source(cache)
        await source.start(["SBER"])

        await source.add_ticker("SBER")
        assert source.get_tickers().count("SBER") == 1

        await source.stop()

    async def test_add_ticker_seeds_a_price(self, make_source):
        cache = PriceCache()
        source = make_source(cache)
        await source.start([])

        await source.add_ticker("LKOH")
        assert "LKOH" in source.get_tickers()
        assert cache.get_price("LKOH") is not None

        await source.stop()

    async def test_remove_ticker_clears_the_cache(self, make_source):
        cache = PriceCache()
        source = make_source(cache)
        await source.start(["SBER"])

        await source.remove_ticker("SBER")
        assert "SBER" not in source.get_tickers()
        assert cache.get_price("SBER") is None

        await source.stop()

    async def test_remove_nonexistent_ticker_is_a_noop(self, make_source):
        cache = PriceCache()
        source = make_source(cache)
        await source.start(["SBER"])

        await source.remove_ticker("NOPE")  # must not raise
        assert set(source.get_tickers()) == {"SBER"}

        await source.stop()

    async def test_stop_is_idempotent(self, make_source):
        cache = PriceCache()
        source = make_source(cache)
        await source.start(["SBER"])

        await source.stop()
        await source.stop()  # must not raise

    async def test_status_reports_health(self, make_source):
        cache = PriceCache()
        source = make_source(cache)
        await source.start(["SBER"])

        status = source.status()
        assert status.healthy is True
        assert status.delay_seconds >= 0

        await source.stop()
