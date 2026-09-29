"""Read-only REST API for market data."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Query

from .analytics import align_closes, analyze, correlation_matrix
from .cache import PriceCache
from .errors import InvalidTickerError, NoDataError, UpstreamError
from .history import MAX_DAYS, CandleService
from .interface import MarketDataSource
from .models import Candle, Interval
from .sessions import is_main_session, session_stats
from .tickers import normalize_ticker

MAX_CORR_TICKERS = 15

InstrumentLookup = Callable[[str], Awaitable[object | None]]


def create_market_router(
    cache: PriceCache,
    source: MarketDataSource,
    candles: CandleService,
    instruments: InstrumentLookup,
) -> APIRouter:
    """Factory (not a global router) -- like create_stream_router: each app build gets its own cache."""
    router = APIRouter(prefix="/api/market", tags=["market"])

    def ticker_or_400(raw: str) -> str:
        try:
            return normalize_ticker(raw)
        except InvalidTickerError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    async def load(ticker: str, interval: Interval, days: int) -> list[Candle]:
        if days > MAX_DAYS[interval]:
            raise HTTPException(400, f"days for interval {interval.value} must be <= {MAX_DAYS[interval]}")
        try:
            return await candles.get(ticker, interval, days)
        except NoDataError as e:
            raise HTTPException(404, f"No market data for {ticker}") from e
        except UpstreamError as e:
            raise HTTPException(503, "MOEX ISS is temporarily unavailable") from e

    @router.get("/status")
    async def status() -> dict:
        return {
            **source.status().to_dict(),
            "market_open_hint": is_main_session(),
            "tracked": source.get_tickers(),
        }

    @router.get("/quotes")
    async def quotes() -> dict:
        return {t: u.to_dict() for t, u in cache.get_all().items()}

    @router.get("/quotes/{ticker}")
    async def quote(ticker: str) -> dict:
        t = ticker_or_400(ticker)
        update = cache.get(t)
        if update is None:
            raise HTTPException(404, f"No price available for {t} yet")
        return {**update.to_dict(), "session": session_stats(cache.get_history(t))}

    @router.get("/instruments/{ticker}")
    async def instrument(ticker: str) -> dict:
        t = ticker_or_400(ticker)
        try:
            info = await instruments(t)
        except UpstreamError as e:
            raise HTTPException(503, "MOEX ISS is temporarily unavailable") from e
        if info is None:
            raise HTTPException(404, f"Unknown instrument {t}")
        return info.to_dict()

    @router.get("/history/{ticker}")
    async def history(ticker: str, interval: Interval = Interval.D1, days: int = Query(90, ge=1, le=730)) -> dict:
        t = ticker_or_400(ticker)
        bars = await load(t, interval, days)
        return {"ticker": t, "interval": interval.value, "candles": [c.to_dict() for c in bars]}

    @router.get("/analytics/{ticker}")
    async def ticker_analytics(ticker: str, days: int = Query(180, ge=30, le=730)) -> dict:
        t = ticker_or_400(ticker)
        bars = await load(t, Interval.D1, days)
        bench = await candles.benchmark(days)
        try:
            return analyze(t, bars, bench).to_dict()
        except NoDataError as e:
            raise HTTPException(404, str(e)) from e

    @router.get("/correlations")
    async def correlations(tickers: str | None = None, days: int = Query(120, ge=30, le=730)) -> dict:
        raw = tickers.split(",") if tickers else source.get_tickers()
        names = list(dict.fromkeys(ticker_or_400(x) for x in raw))[:MAX_CORR_TICKERS]
        if len(names) < 2:
            raise HTTPException(400, "Provide at least 2 tickers")
        series = await asyncio.gather(*(load(t, Interval.D1, days) for t in names))
        used, closes = align_closes(dict(zip(names, series)))
        try:
            matrix = correlation_matrix(closes)
        except NoDataError as e:
            raise HTTPException(404, str(e)) from e
        return {
            "tickers": used,
            "observations": int(closes.shape[0]) - 1,
            "matrix": [[round(float(v), 4) for v in row] for row in matrix],
        }

    return router
