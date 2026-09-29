"""Trading hours and "session" statistics over the cache's tick history."""

from __future__ import annotations

from datetime import datetime, time

from .cache import PriceCache
from .iss import MSK

# MOEX's main equities session, approximately. Holidays are not accounted for -- this is a UI
# hint, not a fact.
MAIN_SESSION = (time(9, 50), time(18, 50))


def is_main_session(now: datetime | None = None) -> bool:
    now = (now or datetime.now(MSK)).astimezone(MSK)
    return now.weekday() < 5 and MAIN_SESSION[0] <= now.time() < MAIN_SESSION[1]


def session_stats(history: list[tuple[float, float]]) -> dict | None:
    """Stats over the ticks in `history`: first/last/high/low price and the change.

    "Since the server started" is only exact while the cache's tick-history window
    (`PriceCache.get_history`'s docstring) hasn't rolled over yet.
    """
    if not history:
        return None
    prices = [p for _, p in history]
    first, last = prices[0], prices[-1]
    return {
        "first_price": first,
        "last_price": last,
        "high": max(prices),
        "low": min(prices),
        "change": round(last - first, 4),
        "change_percent": round((last - first) / first * 100, 4) if first else 0.0,
        "ticks": len(prices),
        "since": history[0][0],
    }


def market_context_lines(cache: PriceCache, tickers: list[str]) -> list[str]:
    """Compact lines for the LLM context (PLAN §9, step 1): "SBER ₽273.50 (+0.12% since start)"."""
    lines = []
    for t in tickers:
        update = cache.get(t)
        if update is None:
            lines.append(f"{t} — (no price yet)")
            continue
        stats = session_stats(cache.get_history(t))
        pct = f" ({stats['change_percent']:+.2f}% since start)" if stats else ""
        lines.append(f"{t} ₽{update.price:,.2f}{pct}")
    return lines
