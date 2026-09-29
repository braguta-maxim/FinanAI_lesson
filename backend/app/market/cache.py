"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from collections import deque
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Thread-safe in-memory cache of the latest price for each ticker.

    Writers: SimulatorDataSource or MoexDataSource (one at a time).
    Readers: SSE streaming endpoint, portfolio valuation, trade execution.
    """

    def __init__(self, history_size: int = 43_200) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._history: dict[str, deque[tuple[float, float]]] = {}  # ticker -> (timestamp, price)
        self._history_size = history_size
        self._lock = Lock()
        self._version: int = 0  # Monotonically increasing; bumped on every update

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price for a ticker. Returns the created PriceUpdate.

        Automatically computes direction and change from the previous price.
        If this is the first update for the ticker, previous_price == price (direction='flat').
        Price is rounded to 4 decimal places: MOEX prices can have 4 decimals, and rounding to
        cents would distort cheap stocks.
        """
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
            prev = self._prices.get(ticker)
            price = round(price, 4)
            previous_price = prev.price if prev else price

            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=previous_price,
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._history.setdefault(ticker, deque(maxlen=self._history_size)).append((ts, price))
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        """Get the latest price for a single ticker, or None if unknown."""
        with self._lock:
            return self._prices.get(ticker)

    def get_all(self) -> dict[str, PriceUpdate]:
        """Snapshot of all current prices. Returns a shallow copy."""
        with self._lock:
            return dict(self._prices)

    def get_price(self, ticker: str) -> float | None:
        """Convenience: get just the price float, or None."""
        update = self.get(ticker)
        return update.price if update else None

    def get_history(self, ticker: str) -> list[tuple[float, float]]:
        """[(timestamp, price), ...] oldest first, since this cache was created. Empty if unknown.

        This is a *tick count* window, not a time window (CODE_REVIEW §3.8): once `history_size`
        ticks have been recorded, the oldest ones are evicted regardless of how much wall-clock
        time they span. The default (43,200) covers about 6 hours at the simulator's default
        500ms tick interval, or about 180 hours (a week of MOEX sessions) at MOEX's 15s poll
        interval. A source ticking faster than 500ms, or a demo session left running past that
        window, will see "since start" in `sessions.py` quietly mean "since the window began,"
        not literally since the cache was created.
        """
        with self._lock:
            return list(self._history.get(ticker, ()))

    def remove(self, ticker: str) -> None:
        """Remove a ticker from the cache (e.g., when removed from watchlist)."""
        with self._lock:
            self._history.pop(ticker, None)
            if self._prices.pop(ticker, None) is not None:
                self._version += 1

    @property
    def version(self) -> int:
        """Current version counter. Useful for SSE change detection."""
        return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: str) -> bool:
        with self._lock:
            return ticker in self._prices
