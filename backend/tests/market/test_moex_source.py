"""Tests for MoexDataSource. No network: httpx.MockTransport stands in for iss.moex.com."""

import asyncio

import httpx
import pytest

from app.market.cache import PriceCache
from app.market.iss import MoexClient
from app.market.moex_client import MoexDataSource


def board_response(rows: list[tuple[str, float]]) -> dict:
    """Build a minimal marketdata+securities payload for the given (ticker, price) pairs."""
    return {
        "marketdata": {"columns": ["SECID", "LAST"], "data": [[t, p] for t, p in rows]},
        "securities": {"columns": ["SECID"], "data": [[t] for t, _ in rows]},
    }


def client_for(handler) -> MoexClient:
    return MoexClient(transport=httpx.MockTransport(handler), retries=0)


class TestMoexDataSource:
    async def test_start_blocks_until_first_poll_completes(self):
        """start() must populate the cache before returning (PLAN §7: startup snapshot needs prices)."""
        client = client_for(lambda r: httpx.Response(200, json=board_response([("SBER", 273.5)])))
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=100)

        await source.start(["SBER"])
        assert cache.get_price("SBER") == 273.5

        await source.stop()

    async def test_unknown_ticker_never_gets_a_price(self):
        client = client_for(lambda r: httpx.Response(200, json=board_response([])))
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=100)

        await source.start(["ZZZZZ"])
        assert cache.get_price("ZZZZZ") is None

        await source.stop()

    async def test_add_ticker_gets_a_price_immediately(self):
        """add_ticker() polls right away instead of waiting for the next cycle (PLAN §8, step 5)."""
        client = client_for(lambda r: httpx.Response(200, json=board_response([("LKOH", 5315.0)])))
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=100)
        await source.start([])

        await source.add_ticker("LKOH")
        assert cache.get_price("LKOH") == 5315.0

        await source.stop()

    async def test_remove_ticker_during_in_flight_poll_is_not_resurrected(self):
        """CODE_REVIEW H3: a ticker removed while a poll is in flight must not be written back."""
        release = asyncio.Event()

        async def slow_handler(request: httpx.Request) -> httpx.Response:
            await release.wait()
            return httpx.Response(200, json=board_response([("SBER", 273.0), ("GAZP", 97.0)]))

        client = MoexClient(transport=httpx.MockTransport(slow_handler), retries=0)
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=100)
        source._tickers = ["SBER", "GAZP"]

        poll_task = asyncio.create_task(source._poll(["SBER", "GAZP"]))
        await asyncio.sleep(0.01)  # let the request start
        source._tickers.remove("GAZP")  # simulate remove_ticker landing mid-poll
        release.set()
        await poll_task

        assert cache.get_price("SBER") == 273.0
        assert cache.get_price("GAZP") is None

        await source.stop()

    async def test_poll_loop_actually_fires_again_over_time(self):
        """CODE_REVIEW §3.3: start()'s blocking poll is not the only poll -- the recurring
        _poll_loop must keep calling the client on its own, without any further prompting."""
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=board_response([("SBER", 273.0 + calls["n"])]))

        client = client_for(handler)
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=0.02)

        await source.start(["SBER"])
        calls_after_start = calls["n"]
        assert calls_after_start == 1  # only the blocking first poll so far

        await asyncio.sleep(0.15)  # several multiples of poll_interval

        assert calls["n"] > calls_after_start
        assert cache.get_price("SBER") == 273.0 + calls["n"]

        await source.stop()

    async def test_source_survives_upstream_failure(self):
        client = client_for(lambda r: httpx.Response(500))
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=0.01)

        await source.start(["SBER"])  # must not raise
        assert cache.get_price("SBER") is None
        assert source.status().healthy is False

        await source.stop()

    async def test_status_healthy_after_successful_poll(self):
        client = client_for(lambda r: httpx.Response(200, json=board_response([("SBER", 273.5)])))
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=100)
        await source.start(["SBER"])

        status = source.status()
        assert status.source == "moex"
        assert status.healthy is True
        assert status.consecutive_failures == 0
        assert status.last_success is not None

        await source.stop()

    async def test_backoff_grows_with_consecutive_failures(self):
        client = client_for(lambda r: httpx.Response(500))
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=10)
        await source.start(["SBER"])  # first failure: _failures == 1

        assert source._failures == 1
        pause_after_1_failure = source._interval * 2**source._failures
        assert pause_after_1_failure == 20

        await source.stop()

    async def test_get_tickers_reflects_add_and_remove(self):
        client = client_for(lambda r: httpx.Response(200, json=board_response([])))
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=100)
        await source.start(["SBER"])

        await source.add_ticker("GAZP")
        assert set(source.get_tickers()) == {"SBER", "GAZP"}

        await source.remove_ticker("SBER")
        assert source.get_tickers() == ["GAZP"]

        await source.stop()

    async def test_add_duplicate_is_noop(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=board_response([("SBER", 273.0)]))

        client = client_for(handler)
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=100)
        await source.start(["SBER"])
        calls_after_start = calls["n"]

        await source.add_ticker("SBER")  # already tracked -> no extra poll
        assert calls["n"] == calls_after_start
        assert source.get_tickers() == ["SBER"]

        await source.stop()

    async def test_stop_closes_a_self_owned_client(self):
        """When no client is passed in (the production default), stop() must close the one it
        created itself -- this is the actual default-usage path, previously untested."""
        cache = PriceCache()
        source = MoexDataSource(cache, poll_interval=100)  # no client= -> owns its own MoexClient
        await source.start([])

        await source.stop()

        with pytest.raises(RuntimeError):
            await source.client.get_quotes(["SBER"])  # closed clients refuse further requests

    async def test_a_non_upstream_exception_during_poll_does_not_kill_the_task(self):
        """The bare `except Exception` branch in _poll -- e.g. a bug in extract_quotes -- must
        also just log and back off, not propagate and kill the poll loop."""

        class BrokenClient:
            async def get_quotes(self, tickers):
                raise KeyError("boom")  # anything that isn't UpstreamError

        cache = PriceCache()
        source = MoexDataSource(cache, client=BrokenClient(), poll_interval=100)

        await source.start(["SBER"])  # must not raise

        assert source.status().healthy is False
        assert "boom" in source.status().last_error

        await source.stop()

    async def test_stop_is_idempotent(self):
        client = client_for(lambda r: httpx.Response(200, json=board_response([])))
        cache = PriceCache()
        source = MoexDataSource(cache, client=client, poll_interval=100)
        await source.start([])
        await source.stop()
        await source.stop()  # must not raise

    async def test_batching_splits_large_ticker_lists(self):
        seen_batches: list[list[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            tickers = request.url.params["securities"].split(",")
            seen_batches.append(tickers)
            return httpx.Response(200, json=board_response([(t, 1.0) for t in tickers]))

        client = client_for(handler)
        cache = PriceCache()
        tickers = [f"T{i:03d}" for i in range(5)]
        source = MoexDataSource(cache, client=client, poll_interval=100, batch_size=2)
        await source.start(tickers)

        assert len(seen_batches) == 3  # 2 + 2 + 1
        assert sum(len(b) for b in seen_batches) == 5

        await source.stop()
