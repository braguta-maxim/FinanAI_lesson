"""Tests for market data analytics. Pure numpy functions -- reference values, not fixtures."""

import json

import numpy as np
import pytest

from app.market.analytics import (
    align_closes,
    analyze,
    annualized_volatility,
    beta,
    bollinger,
    correlation_matrix,
    ema,
    log_returns,
    max_drawdown,
    portfolio_risk,
    rsi,
    sma,
)
from app.market.errors import NoDataError
from app.market.models import Candle


def make_candles(closes: list[float]) -> list[Candle]:
    return [Candle(time=i, open=c, high=c, low=c, close=c, volume=1.0) for i, c in enumerate(closes)]


class TestSmaEma:
    def test_sma(self):
        x = np.arange(1.0, 11.0)  # 1..10
        result = sma(x, 3)
        assert result[-1] == pytest.approx(9.0)
        assert np.isnan(result[1])

    def test_sma_too_short_is_all_nan(self):
        assert np.all(np.isnan(sma(np.array([1.0, 2.0]), 5)))

    def test_ema_seeded_with_sma(self):
        x = np.arange(1.0, 11.0)
        result = ema(x, 3)
        assert result[2] == pytest.approx(2.0)  # seed = SMA of the first three


class TestRsi:
    def test_all_gains_is_100(self):
        assert rsi(np.arange(1.0, 40.0), 14)[-1] == 100.0

    def test_all_losses_is_0(self):
        assert rsi(np.arange(40.0, 1.0, -1.0), 14)[-1] == pytest.approx(0.0)

    def test_constant_series_is_50(self):
        assert rsi(np.full(30, 5.0), 14)[-1] == 50.0

    def test_too_short_is_all_nan(self):
        assert np.all(np.isnan(rsi(np.arange(1.0, 10.0), 14)))


class TestBollinger:
    def test_middle_band_is_sma(self):
        x = np.arange(1.0, 31.0)
        lo, mid, hi = bollinger(x, 20)
        assert mid[-1] == pytest.approx(sma(x, 20)[-1])
        assert lo[-1] < mid[-1] < hi[-1]


class TestVolatilityAndDrawdown:
    def test_annualized_volatility_needs_two_points(self):
        assert annualized_volatility(np.array([0.01])) is None

    def test_max_drawdown(self):
        assert max_drawdown(np.array([100.0, 120.0, 90.0, 110.0])) == pytest.approx(-0.25)

    def test_max_drawdown_no_drop_is_zero(self):
        assert max_drawdown(np.array([100.0, 110.0, 120.0])) == pytest.approx(0.0)


class TestBeta:
    def test_beta_of_series_with_itself_is_one(self):
        rng = np.random.default_rng(0)
        r = rng.normal(0, 0.01, 100)
        assert beta(r, r) == pytest.approx(1.0)

    def test_beta_needs_matching_lengths(self):
        assert beta(np.array([0.01, 0.02]), np.array([0.01])) is None

    def test_beta_zero_variance_benchmark_is_none(self):
        assert beta(np.array([0.01, 0.02, 0.03]), np.zeros(3)) is None


class TestAlignClosesAndCorrelation:
    def test_align_closes_intersects_timestamps(self):
        series = {
            "A": [Candle(1, 1, 1, 1, 10, 1), Candle(2, 1, 1, 1, 11, 1), Candle(3, 1, 1, 1, 12, 1)],
            "B": [Candle(2, 1, 1, 1, 20, 1), Candle(3, 1, 1, 1, 21, 1)],
        }
        names, closes = align_closes(series)
        assert names == ["A", "B"]
        assert closes.shape == (2, 2)  # only timestamps 2 and 3 are shared
        assert list(closes[:, 0]) == [11, 12]

    def test_align_closes_empty_input(self):
        names, closes = align_closes({})
        assert names == [] and closes.shape == (0, 0)

    def test_correlation_of_series_with_itself_is_one(self):
        rng = np.random.default_rng(1)
        closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (60, 1)), axis=0))
        matrix = correlation_matrix(np.hstack([closes, closes]))
        assert matrix[0, 1] == pytest.approx(1.0, abs=1e-9)

    def test_correlation_needs_at_least_two_tickers(self):
        closes = np.ones((10, 1))
        with pytest.raises(NoDataError):
            correlation_matrix(closes)

    def test_correlation_needs_at_least_three_observations(self):
        closes = np.ones((2, 2))
        with pytest.raises(NoDataError):
            correlation_matrix(closes)

    def test_constant_series_correlation_is_zero_not_nan(self):
        closes = np.ones((10, 2))
        matrix = correlation_matrix(closes)
        assert not np.isnan(matrix).any()
        assert matrix[0, 1] == 0.0


class TestAnalyze:
    def test_needs_at_least_two_candles(self):
        with pytest.raises(NoDataError):
            analyze("SBER", make_candles([100.0]))

    def test_short_history_gives_none_in_long_indicators(self):
        result = analyze("SBER", make_candles([100.0 + i % 3 for i in range(20)]))
        assert result.sma50 is None
        assert result.rsi14 is not None

    def test_to_dict_has_no_nan(self):
        result = analyze("SBER", make_candles([100.0 + i % 3 for i in range(20)]))
        d = result.to_dict()
        assert d["sma50"] is None
        json.dumps(d, allow_nan=False)  # must not raise

    def test_trend_up_when_price_above_both_smas(self):
        closes = [100.0 + i * 0.5 for i in range(60)]  # steady uptrend
        result = analyze("SBER", make_candles(closes))
        assert result.trend == "up"

    def test_beta_is_none_without_benchmark(self):
        result = analyze("SBER", make_candles([100.0 + i % 3 for i in range(20)]))
        assert result.beta_imoex is None

    def test_beta_via_analyze_matches_independent_calculation(self):
        """CODE_REVIEW §3.5: analyze()'s own benchmark/beta wiring had 0% coverage, so a column
        swap in its align_closes({ticker: ..., BENCH: ...}) call would have gone unnoticed."""
        rng = np.random.default_rng(7)
        n = 40
        bench_returns = rng.normal(0, 0.01, n)
        # The asset is bench * 1.5 plus independent noise -- a known, non-trivial beta.
        asset_returns = 1.5 * bench_returns + rng.normal(0, 0.002, n)

        bench_closes = 100 * np.exp(np.cumsum(bench_returns))
        asset_closes = 50 * np.exp(np.cumsum(asset_returns))

        bench_candles = make_candles(list(bench_closes))
        asset_candles = make_candles(list(asset_closes))

        result = analyze("SBER", asset_candles, benchmark=bench_candles)

        expected = beta(np.diff(np.log(asset_closes)), np.diff(np.log(bench_closes)))
        assert result.beta_imoex == pytest.approx(expected)
        assert result.beta_imoex == pytest.approx(1.5, abs=0.15)


class TestPortfolioRisk:
    def test_risk_contributions_sum_to_one(self):
        rng = np.random.default_rng(1)
        closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (60, 3)), axis=0))
        result = portfolio_risk({"A": 1000, "B": 500, "C": 500}, closes, ["A", "B", "C"])
        assert sum(result["risk_contribution"].values()) == pytest.approx(1.0, abs=1e-3)
        assert result["effective_positions"] < 3

    def test_empty_portfolio_raises(self):
        with pytest.raises(NoDataError):
            portfolio_risk({}, np.empty((0, 0)), [])

    def test_single_position_is_fully_concentrated(self):
        result = portfolio_risk({"A": 1000}, np.ones((5, 1)), ["A"])
        assert result["hhi"] == 1.0
        assert result["effective_positions"] == 1.0

    def test_log_returns_length(self):
        assert len(log_returns(np.array([100.0, 110.0, 121.0]))) == 2
