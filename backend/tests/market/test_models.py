"""Tests for the market data models."""

import pytest

from app.market.models import Candle, InstrumentInfo, Interval, PriceUpdate, SourceStatus


class TestPriceUpdate:
    """Unit tests for the PriceUpdate model."""

    def test_price_update_creation(self):
        """Test basic PriceUpdate creation."""
        update = PriceUpdate(ticker="SBER", price=190.50, previous_price=190.00, timestamp=1234567890.0)
        assert update.ticker == "SBER"
        assert update.price == 190.50
        assert update.previous_price == 190.00
        assert update.timestamp == 1234567890.0

    def test_change_calculation(self):
        """Test price change calculation."""
        update = PriceUpdate(ticker="SBER", price=190.50, previous_price=190.00, timestamp=1234567890.0)
        assert update.change == 0.50

    def test_change_negative(self):
        """Test negative price change."""
        update = PriceUpdate(ticker="SBER", price=189.50, previous_price=190.00, timestamp=1234567890.0)
        assert update.change == -0.50

    def test_change_percent_up(self):
        """Test percentage change calculation (up)."""
        update = PriceUpdate(ticker="SBER", price=190.00, previous_price=100.00, timestamp=1234567890.0)
        assert update.change_percent == 90.0

    def test_change_percent_down(self):
        """Test percentage change calculation (down)."""
        update = PriceUpdate(ticker="SBER", price=100.00, previous_price=200.00, timestamp=1234567890.0)
        assert update.change_percent == -50.0

    def test_change_percent_zero_previous(self):
        """Test percentage change with zero previous price."""
        update = PriceUpdate(ticker="SBER", price=100.00, previous_price=0.00, timestamp=1234567890.0)
        assert update.change_percent == 0.0

    def test_direction_up(self):
        """Test direction calculation (up)."""
        update = PriceUpdate(ticker="SBER", price=191.00, previous_price=190.00, timestamp=1234567890.0)
        assert update.direction == "up"

    def test_direction_down(self):
        """Test direction calculation (down)."""
        update = PriceUpdate(ticker="SBER", price=189.00, previous_price=190.00, timestamp=1234567890.0)
        assert update.direction == "down"

    def test_direction_flat(self):
        """Test direction calculation (flat)."""
        update = PriceUpdate(ticker="SBER", price=190.00, previous_price=190.00, timestamp=1234567890.0)
        assert update.direction == "flat"

    def test_to_dict(self):
        """Test serialization to dictionary."""
        update = PriceUpdate(ticker="SBER", price=190.50, previous_price=190.00, timestamp=1234567890.0)
        result = update.to_dict()

        assert result["ticker"] == "SBER"
        assert result["price"] == 190.50
        assert result["previous_price"] == 190.00
        assert result["timestamp"] == 1234567890.0
        assert result["change"] == 0.50
        assert result["change_percent"] == 0.2632  # (0.50 / 190.00) * 100
        assert result["direction"] == "up"

    def test_immutability(self):
        """Test that PriceUpdate is immutable."""
        update = PriceUpdate(ticker="SBER", price=190.50, previous_price=190.00, timestamp=1234567890.0)

        with pytest.raises(AttributeError):
            update.price = 200.00  # Should raise error


class TestInterval:
    """Unit tests for the Interval enum."""

    def test_moex_code_mapping(self):
        assert Interval.M1.moex_code == 1
        assert Interval.M10.moex_code == 10
        assert Interval.H1.moex_code == 60
        assert Interval.D1.moex_code == 24

    def test_is_daily(self):
        assert Interval.D1.is_daily is True
        assert Interval.H1.is_daily is False

    def test_value_is_api_string(self):
        assert Interval.D1.value == "1d"
        assert Interval("1d") is Interval.D1


class TestCandle:
    """Unit tests for the Candle dataclass."""

    def test_to_dict(self):
        candle = Candle(time=1700000000, open=100.0, high=105.0, low=99.0, close=103.0, volume=12345.0)
        assert candle.to_dict() == {
            "time": 1700000000,
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 103.0,
            "volume": 12345.0,
        }

    def test_immutability(self):
        candle = Candle(time=1700000000, open=100.0, high=105.0, low=99.0, close=103.0, volume=12345.0)
        with pytest.raises(AttributeError):
            candle.close = 200.0


class TestInstrumentInfo:
    """Unit tests for the InstrumentInfo dataclass."""

    def test_to_dict_with_all_fields(self):
        info = InstrumentInfo(ticker="SBER", name="Sberbank", lot_size=10, decimals=2, min_step=0.01, prev_close=272.9)
        assert info.to_dict() == {
            "ticker": "SBER",
            "name": "Sberbank",
            "lot_size": 10,
            "decimals": 2,
            "min_step": 0.01,
            "prev_close": 272.9,
        }

    def test_optional_fields_default_to_none(self):
        info = InstrumentInfo(ticker="ZZZZ", name="ZZZZ")
        assert info.lot_size is None
        assert info.prev_close is None


class TestSourceStatus:
    """Unit tests for the SourceStatus dataclass."""

    def test_to_dict(self):
        status = SourceStatus(
            source="moex", healthy=True, delay_seconds=900, last_success=123.0, last_error=None, consecutive_failures=0
        )
        assert status.to_dict() == {
            "source": "moex",
            "healthy": True,
            "delay_seconds": 900,
            "last_success": 123.0,
            "last_error": None,
            "consecutive_failures": 0,
        }

    def test_defaults(self):
        status = SourceStatus(source="simulator", healthy=True, delay_seconds=0)
        assert status.last_success is None
        assert status.last_error is None
        assert status.consecutive_failures == 0
