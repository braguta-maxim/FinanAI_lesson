"""MarketDataSource backed by the MOEX ISS API."""

from __future__ import annotations

import asyncio
import logging
import time

from .cache import PriceCache
from .errors import UpstreamError
from .interface import MarketDataSource
from .iss import MoexClient
from .models import SourceStatus

logger = logging.getLogger(__name__)


class MoexDataSource(MarketDataSource):
    """Polls the TQBR board every `poll_interval` seconds, one request per batch of tickers."""

    def __init__(
        self,
        price_cache: PriceCache,
        client: MoexClient | None = None,
        poll_interval: float = 15.0,
        batch_size: int = 50,
    ) -> None:
        self._cache = price_cache
        self._client = client or MoexClient()
        self._owns_client = client is None
        self._interval = poll_interval
        self._batch = batch_size
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        # status() bookkeeping
        self._failures = 0
        self._last_ok: float | None = None
        self._last_error: str | None = None
        self._delay = 900

    @property
    def client(self) -> MoexClient:
        """The shared client: CandleService reuses it (one connection pool)."""
        return self._client

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(dict.fromkeys(tickers))
        await self._poll(self._tickers)  # blocking first poll: prices exist before the startup snapshot
        self._task = asyncio.create_task(self._poll_loop(), name="moex-poller")
        logger.info("MOEX poller started: %d tickers, %.0fs interval", len(self._tickers), self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        if self._owns_client:
            await self._client.aclose()

    async def add_ticker(self, ticker: str) -> None:
        if ticker in self._tickers:
            return
        self._tickers.append(ticker)
        await self._poll([ticker])  # don't wait for the next cycle (up to 15s)

    async def remove_ticker(self, ticker: str) -> None:
        if ticker in self._tickers:
            self._tickers.remove(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def status(self) -> SourceStatus:
        return SourceStatus(
            source="moex",
            healthy=self._last_ok is not None and self._failures < 3,
            delay_seconds=self._delay,
            last_success=self._last_ok,
            last_error=self._last_error,
            consecutive_failures=self._failures,
        )

    # --- internal ---

    async def _poll_loop(self) -> None:
        while True:
            pause = self._interval if self._failures == 0 else min(self._interval * 2**self._failures, 120)
            await asyncio.sleep(pause)
            await self._poll(list(self._tickers))

    async def _poll(self, tickers: list[str]) -> None:
        """One poll cycle. Never raises out: a failure means log + backoff."""
        if not tickers:
            return
        ok = True
        for i in range(0, len(tickers), self._batch):
            chunk = tickers[i : i + self._batch]
            try:
                quotes = await self._client.get_quotes(chunk)
            except UpstreamError as e:
                ok, self._last_error = False, str(e)
                logger.warning("MOEX poll failed: %s", e)
                continue
            except Exception as e:  # noqa: BLE001 — the task must not die
                ok, self._last_error = False, repr(e)
                logger.exception("MOEX poll crashed")
                continue

            for ticker, quote in quotes.items():
                if ticker not in self._tickers:  # removed mid-request (CODE_REVIEW H3)
                    continue
                self._cache.update(ticker, quote.price)  # timestamp = poll time
                if quote.delay_seconds is not None:
                    self._delay = quote.delay_seconds
            missing = [t for t in chunk if t not in quotes]
            if missing:
                logger.debug("MOEX: no price yet for %s (invalid symbol or not traded)", missing)

        if ok:
            self._failures, self._last_ok, self._last_error = 0, time.time(), None
        else:
            self._failures += 1
