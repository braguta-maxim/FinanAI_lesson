"""Tests for PriceCache."""

from app.market.cache import PriceCache


class TestPriceCache:
    """Unit tests for the PriceCache."""

    def test_update_and_get(self):
        """Test updating and getting a price."""
        cache = PriceCache()
        update = cache.update("SBER", 273.50)
        assert update.ticker == "SBER"
        assert update.price == 273.50
        assert cache.get("SBER") == update

    def test_first_update_is_flat(self):
        """Test that the first update has flat direction."""
        cache = PriceCache()
        update = cache.update("SBER", 273.50)
        assert update.direction == "flat"
        assert update.previous_price == 273.50

    def test_direction_up(self):
        """Test price update with upward direction."""
        cache = PriceCache()
        cache.update("SBER", 273.00)
        update = cache.update("SBER", 274.00)
        assert update.direction == "up"
        assert update.change == 1.00

    def test_direction_down(self):
        """Test price update with downward direction."""
        cache = PriceCache()
        cache.update("SBER", 273.00)
        update = cache.update("SBER", 272.00)
        assert update.direction == "down"
        assert update.change == -1.00

    def test_remove(self):
        """Test removing a ticker from cache."""
        cache = PriceCache()
        cache.update("SBER", 273.00)
        cache.remove("SBER")
        assert cache.get("SBER") is None

    def test_remove_nonexistent(self):
        """Test removing a ticker that doesn't exist."""
        cache = PriceCache()
        cache.remove("SBER")  # Should not raise

    def test_get_all(self):
        """Test getting all prices."""
        cache = PriceCache()
        cache.update("SBER", 273.00)
        cache.update("GAZP", 97.00)
        all_prices = cache.get_all()
        assert set(all_prices.keys()) == {"SBER", "GAZP"}

    def test_version_increments(self):
        """Test that version counter increments."""
        cache = PriceCache()
        v0 = cache.version
        cache.update("SBER", 273.00)
        assert cache.version == v0 + 1
        cache.update("SBER", 274.00)
        assert cache.version == v0 + 2

    def test_remove_increments_version(self):
        """Test that removing a ticker bumps the version so SSE pushes the change."""
        cache = PriceCache()
        cache.update("SBER", 273.00)
        v0 = cache.version
        cache.remove("SBER")
        assert cache.version == v0 + 1

    def test_remove_nonexistent_keeps_version(self):
        """Test that removing an unknown ticker is not a state change."""
        cache = PriceCache()
        v0 = cache.version
        cache.remove("NOPE")
        assert cache.version == v0

    def test_get_price_convenience(self):
        """Test the convenience get_price method."""
        cache = PriceCache()
        cache.update("SBER", 273.50)
        assert cache.get_price("SBER") == 273.50
        assert cache.get_price("NOPE") is None

    def test_len(self):
        """Test __len__ method."""
        cache = PriceCache()
        assert len(cache) == 0
        cache.update("SBER", 273.00)
        assert len(cache) == 1
        cache.update("GAZP", 97.00)
        assert len(cache) == 2

    def test_contains(self):
        """Test __contains__ method."""
        cache = PriceCache()
        cache.update("SBER", 273.00)
        assert "SBER" in cache
        assert "GAZP" not in cache

    def test_custom_timestamp(self):
        """Test updating with a custom timestamp."""
        cache = PriceCache()
        custom_ts = 1234567890.0
        update = cache.update("SBER", 273.50, timestamp=custom_ts)
        assert update.timestamp == custom_ts

    def test_price_rounding(self):
        """Test that prices are rounded to 4 decimal places (MOEX prices can have 4 decimals)."""
        cache = PriceCache()
        update = cache.update("SBER", 273.123456)
        assert update.price == 273.1235

    def test_history_records_ticks_oldest_first(self):
        """Test that get_history returns (timestamp, price) tuples in insertion order."""
        cache = PriceCache()
        cache.update("SBER", 273.00, timestamp=1.0)
        cache.update("SBER", 274.00, timestamp=2.0)
        assert cache.get_history("SBER") == [(1.0, 273.00), (2.0, 274.00)]

    def test_history_empty_for_unknown_ticker(self):
        """Test that get_history returns [] for a ticker that was never updated."""
        cache = PriceCache()
        assert cache.get_history("NOPE") == []

    def test_history_bounded_by_history_size(self):
        """Test that the history deque drops the oldest tick once history_size is exceeded."""
        cache = PriceCache(history_size=3)
        for i in range(5):
            cache.update("SBER", float(i), timestamp=float(i))
        history = cache.get_history("SBER")
        assert len(history) == 3
        assert history[0] == (2.0, 2.0)  # the two oldest ticks were dropped

    def test_remove_clears_history(self):
        """Test that removing a ticker also clears its tick history."""
        cache = PriceCache()
        cache.update("SBER", 273.00)
        cache.remove("SBER")
        assert cache.get_history("SBER") == []
