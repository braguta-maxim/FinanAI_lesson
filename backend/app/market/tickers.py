"""Ticker normalization and the "tracked = watchlist ∪ positions" rule (PLAN §6).

PLAN §8 assigns ticker normalization to the trade/watchlist service. This function lives here
so the service and the market data REST API share one regex instead of two.
"""

from __future__ import annotations

import re

from .errors import InvalidTickerError
from .interface import MarketDataSource

_TICKER_RE = re.compile(r"[A-Z]{1,5}")


def normalize_ticker(raw: str) -> str:
    """strip + uppercase + validate "1-5 Latin letters" (SBERP, TATNP pass; BRK.B does not)."""
    ticker = (raw or "").strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise InvalidTickerError(f"Invalid ticker '{raw}': expected 1-5 letters")
    return ticker


async def sync_tracking(
    source: MarketDataSource, ticker: str, *, in_watchlist: bool, has_position: bool
) -> None:
    """The single place that decides whether a source tracks a ticker.

    Call this after any change to the watchlist or a position (manual, via chat, or via a trade).
    """
    if in_watchlist or has_position:
        await source.add_ticker(ticker)  # no-op if already tracked
    else:
        await source.remove_ticker(ticker)  # no-op if not tracked
