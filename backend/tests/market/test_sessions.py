"""Tests for trading-hours and session-statistics helpers."""

from datetime import datetime

from app.market.cache import PriceCache
from app.market.iss import MSK
from app.market.sessions import is_main_session, market_context_lines, session_stats


class TestIsMainSession:
    def test_weekday_during_session(self):
        # Tuesday, 12:00 MSK
        now = datetime(2026, 9, 29, 12, 0, tzinfo=MSK)
        assert is_main_session(now) is True

    def test_weekday_before_open(self):
        now = datetime(2026, 9, 29, 9, 0, tzinfo=MSK)
        assert is_main_session(now) is False

    def test_weekday_after_close(self):
        now = datetime(2026, 9, 29, 19, 0, tzinfo=MSK)
        assert is_main_session(now) is False

    def test_saturday_is_never_a_session(self):
        now = datetime(2026, 9, 26, 12, 0, tzinfo=MSK)  # a Saturday
        assert is_main_session(now) is False


class TestSessionStats:
    def test_empty_history_is_none(self):
        assert session_stats([]) is None

    def test_computes_first_last_high_low_and_change(self):
        history = [(1.0, 100.0), (2.0, 105.0), (3.0, 98.0), (4.0, 102.0)]
        stats = session_stats(history)
        assert stats == {
            "first_price": 100.0,
            "last_price": 102.0,
            "high": 105.0,
            "low": 98.0,
            "change": 2.0,
            "change_percent": 2.0,
            "ticks": 4,
            "since": 1.0,
        }

    def test_zero_first_price_does_not_divide_by_zero(self):
        stats = session_stats([(1.0, 0.0), (2.0, 5.0)])
        assert stats["change_percent"] == 0.0


class TestMarketContextLines:
    def test_no_price_yet(self):
        cache = PriceCache()
        lines = market_context_lines(cache, ["SBER"])
        assert lines == ["SBER — (no price yet)"]

    def test_includes_price_and_session_change(self):
        cache = PriceCache()
        cache.update("SBER", 273.0, timestamp=1.0)
        cache.update("SBER", 274.0, timestamp=2.0)

        lines = market_context_lines(cache, ["SBER"])

        assert len(lines) == 1
        assert "SBER" in lines[0]
        assert "274.00" in lines[0]
        assert "%" in lines[0]
