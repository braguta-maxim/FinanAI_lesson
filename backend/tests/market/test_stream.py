"""Tests for the SSE streaming router."""

import asyncio
import json

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router

EVENT_FIELDS = {
    "ticker",
    "price",
    "previous_price",
    "timestamp",
    "change",
    "change_percent",
    "direction",
}


class StubRequest:
    """Request stub whose is_disconnected() reports False until the given call number."""

    client = None

    def __init__(self, disconnect_on_call: int, on_call=None) -> None:
        self._disconnect_on_call = disconnect_on_call
        self._on_call = on_call
        self._calls = 0

    async def is_disconnected(self) -> bool:
        self._calls += 1
        if self._on_call:
            self._on_call(self._calls)
        return self._calls >= self._disconnect_on_call


async def _collect(cache: PriceCache, request: StubRequest) -> list[str]:
    return [event async for event in _generate_events(cache, request, interval=0)]


def _payload(event: str) -> dict:
    assert event.startswith("data: ") and event.endswith("\n\n")
    return json.loads(event.removeprefix("data: "))


def test_each_call_creates_an_independent_router():
    """Each factory call must return its own router with exactly one /prices route."""
    router_a = create_stream_router(PriceCache())
    router_b = create_stream_router(PriceCache())

    assert router_a is not router_b
    assert len(router_a.routes) == 1
    assert len(router_b.routes) == 1
    assert router_a.routes[0].path == "/api/stream/prices"


async def test_starts_with_retry_directive():
    """The first event tells the browser how fast to reconnect."""
    events = await _collect(PriceCache(), StubRequest(disconnect_on_call=1))

    assert events == ["retry: 1000\n\n"]


async def test_payload_is_dict_keyed_by_ticker():
    """One data event carries all tickers, each with the seven documented fields."""
    cache = PriceCache()
    cache.update("SBER", 273.50)
    cache.update("GAZP", 97.25)

    events = await _collect(cache, StubRequest(disconnect_on_call=2))

    assert len(events) == 2
    data = _payload(events[1])
    assert set(data) == {"SBER", "GAZP"}
    assert set(data["SBER"]) == EVENT_FIELDS
    assert data["SBER"]["price"] == 273.50


async def test_empty_cache_sends_no_data_event():
    """Nothing is pushed while no ticker has a price."""
    events = await _collect(PriceCache(), StubRequest(disconnect_on_call=3))

    assert events == ["retry: 1000\n\n"]


async def test_no_duplicate_event_when_nothing_changed():
    """Two loop iterations over an unchanged cache produce a single data event."""
    cache = PriceCache()
    cache.update("SBER", 273.50)

    events = await _collect(cache, StubRequest(disconnect_on_call=3))

    assert len(events) == 2


async def test_new_event_after_price_change():
    """A price update between iterations produces another data event."""
    cache = PriceCache()
    cache.update("SBER", 273.50)

    def update_on_second_call(call: int) -> None:
        if call == 2:
            cache.update("SBER", 274.00)

    events = await _collect(cache, StubRequest(disconnect_on_call=3, on_call=update_on_second_call))

    assert len(events) == 3
    assert _payload(events[2])["SBER"]["price"] == 274.00


async def test_cancellation_propagates():
    """Cancelling a consumer of the stream must not be swallowed by the generator."""
    cache = PriceCache()
    cache.update("SBER", 273.50)

    async def consume() -> None:
        async for _ in _generate_events(cache, StubRequest(disconnect_on_call=10**9), interval=0.01):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
