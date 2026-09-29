"""Factory for creating market data sources, selected by MOEX_ENABLED (PLAN §5)."""

from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .history import CandleService, MoexCandleProvider, SyntheticCandleProvider
from .interface import MarketDataSource
from .moex_client import MoexDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def moex_enabled() -> bool:
    """Only "true" (case/whitespace-insensitive) enables MOEX; unset/empty/anything else -> simulator."""
    return os.environ.get("MOEX_ENABLED", "").strip().lower() == "true"


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MOEX_ENABLED=true -> MoexDataSource (real MOEX prices, no API key needed)
    - Otherwise -> SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    if moex_enabled():
        logger.info("Market data source: MOEX ISS (real prices, ~15 min delayed)")
        return MoexDataSource(price_cache)
    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache)


def create_candle_service(price_cache: PriceCache, source: MarketDataSource) -> CandleService:
    """Candles from ISS if MoexDataSource is running (reuses its HTTP client); otherwise synthetic."""
    if isinstance(source, MoexDataSource):
        return CandleService(MoexCandleProvider(source.client))
    return CandleService(SyntheticCandleProvider(price_cache))
