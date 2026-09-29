"""Tests for GBMSimulator."""

import pytest

from app.market.seed_prices import SEED_PRICES
from app.market.simulator import TRADING_SECONDS_PER_YEAR, GBMSimulator

DEFAULT_WATCHLIST = ["SBER", "GAZP", "LKOH", "GMKN", "ROSN", "NVTK", "MTSS", "TATN", "PLZL", "VTBR"]


class TestGBMSimulator:
    """Unit tests for the GBM price simulator."""

    def test_step_returns_all_tickers(self):
        """Test that step() returns prices for all tickers."""
        sim = GBMSimulator(tickers=["SBER", "GAZP"])
        result = sim.step()
        assert set(result.keys()) == {"SBER", "GAZP"}

    def test_prices_are_positive(self):
        """GBM prices can never go negative (exp() is always positive)."""
        sim = GBMSimulator(tickers=["SBER"])
        for _ in range(10_000):
            prices = sim.step()
            assert prices["SBER"] > 0

    def test_initial_prices_match_seeds(self):
        """Test that initial prices match seed prices."""
        sim = GBMSimulator(tickers=["SBER"])
        # Before any step, price should be the seed price
        assert sim.get_price("SBER") == SEED_PRICES["SBER"]

    def test_add_ticker(self):
        """Test adding a ticker dynamically."""
        sim = GBMSimulator(tickers=["SBER"])
        sim.add_ticker("LKOH")
        result = sim.step()
        assert "LKOH" in result

    def test_remove_ticker(self):
        """Test removing a ticker."""
        sim = GBMSimulator(tickers=["SBER", "GAZP"])
        sim.remove_ticker("GAZP")
        result = sim.step()
        assert "GAZP" not in result
        assert "SBER" in result

    def test_add_duplicate_is_noop(self):
        """Test that adding a duplicate ticker is a no-op."""
        sim = GBMSimulator(tickers=["SBER"])
        sim.add_ticker("SBER")
        assert len(sim._tickers) == 1

    def test_remove_nonexistent_is_noop(self):
        """Test that removing a non-existent ticker is a no-op."""
        sim = GBMSimulator(tickers=["SBER"])
        sim.remove_ticker("NOPE")  # Should not raise

    def test_unknown_ticker_gets_random_seed_price(self):
        """Test that unknown tickers get a random price in UNKNOWN_PRICE_RANGE."""
        sim = GBMSimulator(tickers=["ZZZZ"])
        price = sim.get_price("ZZZZ")
        assert price is not None
        assert 50.0 <= price <= 2000.0

    def test_empty_step(self):
        """Test stepping with no tickers."""
        sim = GBMSimulator(tickers=[])
        result = sim.step()
        assert result == {}

    def test_prices_change_over_time(self):
        """After many steps, prices should have drifted from their seeds."""
        sim = GBMSimulator(tickers=["SBER"])
        initial_price = sim.get_price("SBER")

        for _ in range(1000):
            sim.step()

        final_price = sim.get_price("SBER")
        # Price should have changed (extremely unlikely to be exactly the seed)
        assert final_price != initial_price

    def test_random_event_moves_price_2_to_5_percent(self):
        """With event_probability=1.0 every tick is a 2-5% shock on top of the tiny GBM move."""
        sim = GBMSimulator(tickers=["SBER"], event_probability=1.0)
        for _ in range(50):
            before = sim.get_price("SBER")
            after = sim.step()["SBER"]
            assert 0.019 <= abs(after / before - 1) <= 0.051

    def test_no_events_when_probability_zero(self):
        """With event_probability=0 a single tick stays within GBM noise (well under 1%)."""
        sim = GBMSimulator(tickers=["SBER"], event_probability=0.0)
        for _ in range(50):
            before = sim.get_price("SBER")
            after = sim.step()["SBER"]
            assert abs(after / before - 1) < 0.01

    def test_cholesky_rebuilds_on_add(self):
        """Test that Cholesky matrix is rebuilt when tickers are added."""
        sim = GBMSimulator(tickers=["SBER"])
        assert sim._cholesky is None  # Only 1 ticker, no correlation matrix
        sim.add_ticker("GAZP")
        assert sim._cholesky is not None  # Now 2 tickers, matrix exists

    def test_cholesky_none_with_one_ticker(self):
        """Test that Cholesky is None with only one ticker."""
        sim = GBMSimulator(tickers=["SBER"])
        assert sim._cholesky is None

    def test_get_price_returns_none_for_unknown(self):
        """Test that get_price returns None for unknown ticker."""
        sim = GBMSimulator(tickers=["SBER"])
        assert sim.get_price("UNKNOWN") is None

    def test_pairwise_correlation_banks(self):
        """Test that two bank stocks have high correlation."""
        assert GBMSimulator._pairwise_correlation("SBER", "VTBR") == 0.6

    def test_pairwise_correlation_oil_gas(self):
        """Test that two oil & gas stocks have high correlation."""
        assert GBMSimulator._pairwise_correlation("GAZP", "LKOH") == 0.6

    def test_pairwise_correlation_metals(self):
        """Test that two metals/mining stocks have moderate correlation."""
        assert GBMSimulator._pairwise_correlation("GMKN", "PLZL") == 0.5

    def test_pairwise_correlation_telecom_has_no_group(self):
        """Test that MTSS (telecom, no sector group) falls back to cross-group correlation."""
        assert GBMSimulator._pairwise_correlation("MTSS", "SBER") == 0.3
        assert GBMSimulator._pairwise_correlation("MTSS", "GAZP") == 0.3

    def test_pairwise_correlation_cross_sector(self):
        """Test cross-sector correlation (bank vs. oil & gas)."""
        assert GBMSimulator._pairwise_correlation("SBER", "GAZP") == 0.3

    def test_pairwise_correlation_unknown_ticker(self):
        """Test that an unknown ticker (no group) falls back to cross-group correlation."""
        assert GBMSimulator._pairwise_correlation("ZZZZ", "SBER") == 0.3

    def test_dt_derived_from_update_interval(self):
        """dt is update_interval * time_scale / TRADING_SECONDS_PER_YEAR -- one knob, not two."""
        sim = GBMSimulator(tickers=["SBER"], update_interval=0.5, time_scale=1.0)
        assert sim._dt == 0.5 / TRADING_SECONDS_PER_YEAR

    def test_dt_scales_with_time_scale(self):
        """A higher time_scale speeds up the simulated market proportionally."""
        sim = GBMSimulator(tickers=["SBER"], update_interval=0.5, time_scale=10.0)
        assert sim._dt == 5.0 / TRADING_SECONDS_PER_YEAR

    def test_default_dt_is_reasonable(self):
        """Test that the default dt is a reasonable small value."""
        sim = GBMSimulator(tickers=["SBER"])
        assert 0 < sim._dt < 0.0001

    def test_cholesky_succeeds_for_the_full_default_watchlist(self):
        """CODE_REVIEW §4.2: this exact scenario (10 tickers, 3 sector groups) was flagged as an
        untested gap in the pre-MOEX review too -- pin it down so a future edit to the tickers,
        groups, or correlation values can't silently break the PSD property Cholesky relies on."""
        sim = GBMSimulator(tickers=DEFAULT_WATCHLIST)
        assert sim._cholesky is not None
        assert sim._cholesky.shape == (10, 10)
        for _ in range(200):
            prices = sim.step()
            assert all(p > 0 for p in prices.values())

    def test_invalid_correlation_matrix_raises_a_clear_error(self, monkeypatch):
        """CODE_REVIEW §4.3: a bad GROUP_CORR value should fail loudly, not with a bare LinAlgError."""
        import app.market.seed_prices as seed_prices

        # GROUP_CORR is a mutable dict shared by reference with simulator.py's `from ... import`,
        # so mutating it here is visible there too -- no need to patch two names.
        monkeypatch.setitem(seed_prices.GROUP_CORR, "banks", 1.5)  # not a valid correlation; breaks PSD

        with pytest.raises(ValueError, match="not positive semi-definite"):
            GBMSimulator(tickers=["SBER", "VTBR"])  # both in the "banks" group

    def test_prices_rounded_to_two_decimals(self):
        """Test that prices are rounded to 2 decimal places."""
        sim = GBMSimulator(tickers=["SBER"])
        result = sim.step()
        price_str = str(result["SBER"])
        # Check that we have at most 2 decimal places
        if "." in price_str:
            decimal_part = price_str.split(".")[1]
            assert len(decimal_part) <= 2
