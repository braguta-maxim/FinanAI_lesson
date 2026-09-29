"""Market data subsystem for FinAlly.

Public API:
    PriceUpdate                - Immutable price snapshot dataclass
    PriceCache                 - Thread-safe in-memory price store
    MarketDataSource           - Abstract interface for data providers
    Candle, InstrumentInfo,
    SourceStatus, Interval     - Supporting data models
    create_market_data_source  - Factory that selects simulator or MOEX
    create_candle_service      - Factory for historical-candle access
    create_stream_router       - FastAPI router factory for the SSE endpoint
    create_market_router       - FastAPI router factory for /api/market/*
    normalize_ticker           - Shared ticker validation (strip/upper/1-5 letters)
    sync_tracking               - Keep a source's tracked tickers in sync with watchlist/positions
"""

from .api import create_market_router
from .cache import PriceCache
from .factory import create_candle_service, create_market_data_source
from .interface import MarketDataSource
from .models import Candle, InstrumentInfo, Interval, PriceUpdate, SourceStatus
from .stream import create_stream_router
from .tickers import normalize_ticker, sync_tracking

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "Candle",
    "InstrumentInfo",
    "SourceStatus",
    "Interval",
    "create_market_data_source",
    "create_candle_service",
    "create_stream_router",
    "create_market_router",
    "normalize_ticker",
    "sync_tracking",
]
