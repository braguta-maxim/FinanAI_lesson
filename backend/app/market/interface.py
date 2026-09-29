"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import SourceStatus


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["SBER", "GAZP", ...])
        # ... app runs ...
        await source.add_ticker("LKOH")
        await source.remove_ticker("GAZP")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once. Calling start() twice is undefined behavior.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), the source will not write
        to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include this ticker.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""

    def status(self) -> SourceStatus:
        """Current health of the source, for `/api/market/status`.

        Not abstract, so existing implementations that predate this method still satisfy the
        interface. The default is "healthy, no delay".
        """
        return SourceStatus(source=type(self).__name__, healthy=True, delay_seconds=0)
