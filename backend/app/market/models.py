"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }


class Interval(str, Enum):
    """Candle interval. The value is what our REST API accepts."""

    M1 = "1m"
    M10 = "10m"
    H1 = "1h"
    D1 = "1d"

    @property
    def moex_code(self) -> int:
        """The ISS `interval` parameter: 1, 10, 60 minutes, or 24 (day)."""
        return {"1m": 1, "10m": 10, "1h": 60, "1d": 24}[self.value]

    @property
    def is_daily(self) -> bool:
        return self is Interval.D1


@dataclass(frozen=True, slots=True)
class Candle:
    """One OHLCV bar. `time` is Unix seconds UTC (midnight UTC of the trading date for daily bars)."""

    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True, slots=True)
class InstrumentInfo:
    """Reference data for one ticker (name, lot size, price precision)."""

    ticker: str
    name: str
    lot_size: int | None = None
    decimals: int | None = None
    min_step: float | None = None
    prev_close: float | None = None

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "name": self.name,
            "lot_size": self.lot_size,
            "decimals": self.decimals,
            "min_step": self.min_step,
            "prev_close": self.prev_close,
        }


@dataclass(frozen=True, slots=True)
class SourceStatus:
    """Health/status of a market data source, for `/api/market/status`."""

    source: str
    healthy: bool
    delay_seconds: int
    last_success: float | None = None
    last_error: str | None = None
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "healthy": self.healthy,
            "delay_seconds": self.delay_seconds,
            "last_success": self.last_success,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
        }
