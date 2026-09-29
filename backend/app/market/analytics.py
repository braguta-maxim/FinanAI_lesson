"""Market data analytics. Pure functions over numpy — no I/O, no FastAPI knowledge."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .errors import NoDataError
from .models import Candle

TRADING_DAYS = 250  # trading days per year on MOEX (for annualizing volatility)
BENCH = "IMOEX"  # benchmark key when aligning series


def log_returns(x: np.ndarray) -> np.ndarray:
    return np.diff(np.log(x))


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """Simple moving average. The first n-1 values are NaN."""
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """Exponential moving average, seeded with the SMA of the first n values."""
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    alpha = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi(x: np.ndarray, n: int = 14) -> np.ndarray:
    """Wilder's RSI. The first value is at index n."""
    out = np.full(len(x), np.nan)
    if len(x) <= n:
        return out
    d = np.diff(x)
    gain, loss = np.where(d > 0, d, 0.0), np.where(d < 0, -d, 0.0)
    avg_gain, avg_loss = gain[:n].mean(), loss[:n].mean()
    out[n] = _rsi_value(avg_gain, avg_loss)
    for i in range(n, len(d)):
        avg_gain = (avg_gain * (n - 1) + gain[i]) / n
        avg_loss = (avg_loss * (n - 1) + loss[i]) / n
        out[i + 1] = _rsi_value(avg_gain, avg_loss)
    return out


def bollinger(x: np.ndarray, n: int = 20, k: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(lower, middle, upper) Bollinger bands; std is the sample std over the window."""
    mid = sma(x, n)
    std = np.full(len(x), np.nan)
    for i in range(n - 1, len(x)):
        std[i] = x[i - n + 1 : i + 1].std(ddof=1)
    return mid - k * std, mid, mid + k * std


def annualized_volatility(returns: np.ndarray, periods_per_year: int = TRADING_DAYS) -> float | None:
    if len(returns) < 2:
        return None
    return float(np.std(returns, ddof=1) * math.sqrt(periods_per_year))


def max_drawdown(x: np.ndarray) -> float:
    """Maximum drawdown from a peak, a fraction <= 0 (-0.12 = -12%)."""
    peak = np.maximum.accumulate(x)
    return float((x / peak - 1.0).min())


def beta(asset_returns: np.ndarray, bench_returns: np.ndarray) -> float | None:
    if len(asset_returns) < 2 or len(asset_returns) != len(bench_returns):
        return None
    var = np.var(bench_returns, ddof=1)
    return None if var == 0 else float(np.cov(asset_returns, bench_returns)[0, 1] / var)


def align_closes(series: dict[str, list[Candle]]) -> tuple[list[str], np.ndarray]:
    """{ticker: candles} -> (tickers, close-price matrix [observations x tickers]) on shared timestamps."""
    names = [t for t, c in series.items() if c]
    if not names:
        return [], np.empty((0, 0))
    common = set.intersection(*(set(c.time for c in series[t]) for t in names))
    times = sorted(common)
    by_time = {t: {c.time: c.close for c in series[t]} for t in names}
    return names, np.array([[by_time[t][ts] for t in names] for ts in times], dtype=float)


def correlation_matrix(closes: np.ndarray) -> np.ndarray:
    """Correlations of log returns; closes is [observations x tickers]. Needs >= 3 price observations."""
    if closes.ndim != 2 or closes.shape[0] < 3 or closes.shape[1] < 2:
        raise NoDataError("Not enough overlapping history to compute correlations")
    returns = np.diff(np.log(closes), axis=0)
    corr = np.corrcoef(returns, rowvar=False)
    return np.nan_to_num(corr, nan=0.0)  # a constant series (zero variance) -> 0


def _f(x, nd: int = 4) -> float | None:
    """NaN/None -> None (JSONResponse cannot serialize NaN), otherwise rounded."""
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), nd)


def _ret(x: np.ndarray, days: int) -> float | None:
    return float(x[-1] / x[-1 - days] - 1.0) if len(x) > days else None


@dataclass(frozen=True, slots=True)
class TickerAnalytics:
    ticker: str
    bars: int
    last: float
    sma20: float | None
    sma50: float | None
    ema12: float | None
    rsi14: float | None
    bollinger: dict[str, float | None]
    volatility_annual: float | None  # a fraction: 0.28 = 28% annualized
    max_drawdown: float  # a fraction <= 0
    return_1d: float | None
    return_5d: float | None
    return_20d: float | None
    beta_imoex: float | None
    trend: str  # "up" | "down" | "sideways"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "bars": self.bars,
            "last": self.last,
            "sma20": _f(self.sma20),
            "sma50": _f(self.sma50),
            "ema12": _f(self.ema12),
            "rsi14": _f(self.rsi14, 2),
            "bollinger": {k: _f(v) for k, v in self.bollinger.items()},
            "volatility_annual": _f(self.volatility_annual),
            "max_drawdown": _f(self.max_drawdown),
            "return_1d": _f(self.return_1d),
            "return_5d": _f(self.return_5d),
            "return_20d": _f(self.return_20d),
            "beta_imoex": _f(self.beta_imoex, 2),
            "trend": self.trend,
        }


def _last(a: np.ndarray) -> float | None:
    return None if len(a) == 0 or math.isnan(a[-1]) else float(a[-1])


def analyze(ticker: str, candles: list[Candle], benchmark: list[Candle] | None = None) -> TickerAnalytics:
    """Daily candles -> a set of indicators. Needs at least 2 candles; short history gives None in
    the longer indicators."""
    if len(candles) < 2:
        raise NoDataError(f"Not enough history for {ticker}")
    close = np.array([c.close for c in candles], dtype=float)
    lo, mid, hi = bollinger(close, 20)
    s20, s50 = _last(sma(close, 20)), _last(sma(close, 50))

    if s20 is not None and s50 is not None:
        trend = "up" if close[-1] > s20 > s50 else "down" if close[-1] < s20 < s50 else "sideways"
    else:
        trend = "sideways"

    b = None
    if benchmark:
        _, m = align_closes({ticker: candles, BENCH: benchmark})
        if m.shape[0] >= 30:
            r = np.diff(np.log(m), axis=0)
            b = beta(r[:, 0], r[:, 1])

    return TickerAnalytics(
        ticker=ticker,
        bars=len(close),
        last=float(close[-1]),
        sma20=s20,
        sma50=s50,
        ema12=_last(ema(close, 12)),
        rsi14=_last(rsi(close, 14)),
        bollinger={"lower": _last(lo), "middle": _last(mid), "upper": _last(hi)},
        volatility_annual=annualized_volatility(log_returns(close)),
        max_drawdown=max_drawdown(close),
        return_1d=_ret(close, 1),
        return_5d=_ret(close, 5),
        return_20d=_ret(close, 20),
        beta_imoex=b,
        trend=trend,
    )


def portfolio_risk(weights: dict[str, float], closes: np.ndarray, tickers: list[str]) -> dict:
    """Portfolio concentration and risk.

    weights -- market value of positions by ticker (rubles, any scale: normalized internally).
    closes  -- [observations x tickers] in the order of `tickers` (from align_closes).
    Returns: HHI, "effective number of positions", annualized portfolio volatility, and each
    position's contribution to risk.
    """
    w = np.array([weights.get(t, 0.0) for t in tickers], dtype=float)
    total = w.sum()
    if total <= 0:
        raise NoDataError("Empty portfolio")
    w /= total
    hhi = float(np.sum(w**2))
    out = {
        "weights": {t: round(float(x), 4) for t, x in zip(tickers, w)},
        "hhi": round(hhi, 4),
        "effective_positions": round(1.0 / hhi, 2),
        "volatility_annual": None,
        "risk_contribution": None,
    }

    if closes.shape[0] >= 3:
        r = np.diff(np.log(closes), axis=0)
        cov = np.atleast_2d(np.cov(r, rowvar=False)) * TRADING_DAYS
        var = float(w @ cov @ w)
        if var > 0:
            out["volatility_annual"] = round(math.sqrt(var), 4)
            contrib = w * (cov @ w) / var  # fractions, summing to 1
            out["risk_contribution"] = {t: round(float(x), 4) for t, x in zip(tickers, contrib)}
    return out
