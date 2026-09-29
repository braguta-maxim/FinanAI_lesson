"""Exceptions for the market data subsystem. The API layer translates these to HTTP codes."""


class MarketDataError(Exception):
    """Base exception for the market data subsystem."""


class InvalidTickerError(MarketDataError, ValueError):
    """Ticker is not 1-5 Latin letters."""


class UpstreamError(MarketDataError):
    """MOEX ISS is unreachable or returned an error, after retries."""


class NoDataError(MarketDataError):
    """No data is available for the instrument (unknown ticker, empty ISS response)."""
