"""Tests for the /api/market/* REST endpoints."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.market.api import create_market_router
from app.market.cache import PriceCache
from app.market.errors import NoDataError, UpstreamError
from app.market.models import Candle, InstrumentInfo
from app.market.simulator import SimulatorDataSource


class FakeCandleService:
    """A CandleService stub: returns fixed candles, or raises on demand."""

    def __init__(self, candles: list[Candle] | None = None, benchmark: list[Candle] | None = None) -> None:
        self._candles = candles if candles is not None else _sample_candles()
        self._benchmark = benchmark if benchmark is not None else []
        self.error: Exception | None = None

    async def get(self, ticker, interval, days):
        if self.error:
            raise self.error
        return self._candles

    async def benchmark(self, days):
        return self._benchmark


def _sample_candles(n: int = 60) -> list[Candle]:
    return [Candle(time=i * 86400, open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=1000.0) for i in range(n)]


def build_app(*, candle_service=None, source=None, instruments=None) -> tuple[FastAPI, PriceCache, object]:
    cache = PriceCache()
    cache.update("SBER", 273.5)
    source = source or SimulatorDataSource(price_cache=cache)
    candles = candle_service or FakeCandleService()
    lookup = instruments or AsyncMock(return_value=InstrumentInfo(ticker="SBER", name="Sberbank"))

    app = FastAPI()
    app.include_router(create_market_router(cache, source, candles, lookup))
    return app, cache, source


@pytest.fixture
def client():
    app, _, source = build_app()
    with TestClient(app) as c:
        yield c


class TestStatus:
    def test_returns_source_status_and_tracked_tickers(self, client):
        resp = client.get("/api/market/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "simulator"
        assert body["healthy"] is True
        assert "market_open_hint" in body
        assert "tracked" in body


class TestQuotes:
    def test_all_quotes(self, client):
        resp = client.get("/api/market/quotes")
        assert resp.status_code == 200
        assert "SBER" in resp.json()

    def test_single_quote_includes_session_stats(self, client):
        resp = client.get("/api/market/quotes/SBER")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "SBER"
        assert "session" in body

    def test_unknown_ticker_is_404(self, client):
        resp = client.get("/api/market/quotes/ZZZZZ")
        assert resp.status_code == 404

    def test_invalid_ticker_is_400(self, client):
        resp = client.get("/api/market/quotes/BRK.B")
        assert resp.status_code == 400

    def test_ticker_is_normalized(self, client):
        resp = client.get("/api/market/quotes/sber")
        assert resp.status_code == 200
        assert resp.json()["ticker"] == "SBER"


class TestInstruments:
    def test_returns_instrument_info(self, client):
        resp = client.get("/api/market/instruments/SBER")
        assert resp.status_code == 200
        assert resp.json()["ticker"] == "SBER"

    def test_unknown_instrument_is_404(self):
        lookup = AsyncMock(return_value=None)
        app, _, _ = build_app(instruments=lookup)
        with TestClient(app) as c:
            resp = c.get("/api/market/instruments/ZZZZZ")
        assert resp.status_code == 404

    def test_upstream_failure_is_503(self):
        lookup = AsyncMock(side_effect=UpstreamError("down"))
        app, _, _ = build_app(instruments=lookup)
        with TestClient(app) as c:
            resp = c.get("/api/market/instruments/SBER")
        assert resp.status_code == 503

    def test_invalid_ticker_is_400(self, client):
        resp = client.get("/api/market/instruments/BRK.B")
        assert resp.status_code == 400


class TestHistory:
    def test_returns_candles(self, client):
        resp = client.get("/api/market/history/SBER?interval=1d&days=30")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "SBER"
        assert body["interval"] == "1d"
        assert len(body["candles"]) == 60  # FakeCandleService.get ignores `days`, returns its fixed list

    def test_400_for_invalid_ticker(self, client):
        assert client.get("/api/market/history/BRK.B").status_code == 400

    def test_400_when_days_exceeds_interval_max(self, client):
        resp = client.get("/api/market/history/SBER?interval=1m&days=30")  # M1's MAX_DAYS is 5
        assert resp.status_code == 400

    def test_404_for_unknown_ticker(self):
        service = FakeCandleService()
        service.error = NoDataError("no data")
        app, _, _ = build_app(candle_service=service)
        with TestClient(app) as c:
            resp = c.get("/api/market/history/ZZZZZ")
        assert resp.status_code == 404

    def test_503_when_upstream_is_down(self):
        service = FakeCandleService()
        service.error = UpstreamError("down")
        app, _, _ = build_app(candle_service=service)
        with TestClient(app) as c:
            resp = c.get("/api/market/history/SBER")
        assert resp.status_code == 503

    def test_422_for_out_of_range_days(self, client):
        resp = client.get("/api/market/history/SBER?days=0")
        assert resp.status_code == 422


class TestAnalytics:
    def test_returns_indicators(self, client):
        resp = client.get("/api/market/analytics/SBER")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ticker"] == "SBER"
        assert "rsi14" in body

    def test_404_for_insufficient_history(self):
        service = FakeCandleService(candles=_sample_candles(1))
        app, _, _ = build_app(candle_service=service)
        with TestClient(app) as c:
            resp = c.get("/api/market/analytics/SBER")
        assert resp.status_code == 404


class TestCorrelations:
    def test_returns_matrix_for_given_tickers(self, client):
        resp = client.get("/api/market/correlations?tickers=SBER,GAZP")
        assert resp.status_code == 200
        body = resp.json()
        assert body["tickers"] == ["SBER", "GAZP"]
        assert len(body["matrix"]) == 2

    def test_defaults_to_tracked_tickers_when_none_given(self):
        """With no `tickers` query param, the endpoint falls back to source.get_tickers()."""
        cache = PriceCache()
        source = SimulatorDataSource(price_cache=cache)
        app, _, _ = build_app(source=source)
        with TestClient(app) as c:
            # The simulator here was never start()-ed, so get_tickers() is [] -> "at least 2" fails.
            resp = c.get("/api/market/correlations")
        assert resp.status_code == 400

    def test_400_for_fewer_than_two_tickers(self, client):
        resp = client.get("/api/market/correlations?tickers=SBER")
        assert resp.status_code == 400

    def test_400_for_invalid_ticker_in_list(self, client):
        resp = client.get("/api/market/correlations?tickers=SBER,BRK.B")
        assert resp.status_code == 400

    def test_duplicate_tickers_are_deduplicated(self, client):
        resp = client.get("/api/market/correlations?tickers=SBER,sber,GAZP")
        assert resp.status_code == 200
        assert resp.json()["tickers"] == ["SBER", "GAZP"]


class TestReadOnlyGuarantee:
    """None of these endpoints may add a ticker to what the source tracks."""

    def test_quote_endpoint_does_not_call_add_ticker(self, client, monkeypatch):
        called = []
        monkeypatch.setattr(SimulatorDataSource, "add_ticker", AsyncMock(side_effect=lambda self, t: called.append(t)))
        client.get("/api/market/quotes/GAZP")  # not tracked, not cached -> 404, but must not track it
        assert called == []
