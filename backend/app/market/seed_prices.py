"""Seed prices (rubles) and GBM parameters for the default MOEX watchlist."""

# Realistic starting prices for the default watchlist, checked against MOEX ISS PREVPRICE
# on 2026-09-29 (see planning/MOEX_API.md).
SEED_PRICES: dict[str, float] = {
    "SBER": 273.0,
    "GAZP": 97.0,
    "LKOH": 5315.0,
    "GMKN": 118.0,
    "ROSN": 354.0,
    "NVTK": 1032.0,
    "MTSS": 175.0,
    "TATN": 630.0,
    "PLZL": 971.0,
    "VTBR": 53.0,
}

# Per-ticker GBM parameters.
# sigma: annualized volatility (higher = more price movement)
# mu: annualized drift / expected return
# Russian equities are noticeably more volatile than US blue chips.
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "SBER": {"sigma": 0.28, "mu": 0.10},
    "GAZP": {"sigma": 0.32, "mu": 0.05},
    "LKOH": {"sigma": 0.28, "mu": 0.10},
    "GMKN": {"sigma": 0.33, "mu": 0.06},
    "ROSN": {"sigma": 0.30, "mu": 0.08},
    "NVTK": {"sigma": 0.30, "mu": 0.06},
    "MTSS": {"sigma": 0.25, "mu": 0.08},
    "TATN": {"sigma": 0.29, "mu": 0.08},
    "PLZL": {"sigma": 0.35, "mu": 0.10},
    "VTBR": {"sigma": 0.35, "mu": 0.06},
}

# Default parameters for tickers not in the list above (dynamically added).
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.30, "mu": 0.08}

# Sector -> intra-sector correlation. Rule: every intra-group correlation must be
# >= CROSS_GROUP_CORR, otherwise the correlation matrix can stop being positive
# semi-definite and the Cholesky decomposition raises.
TICKER_GROUP: dict[str, str] = {
    "SBER": "banks",
    "SBERP": "banks",
    "VTBR": "banks",
    "GAZP": "oil_gas",
    "LKOH": "oil_gas",
    "ROSN": "oil_gas",
    "NVTK": "oil_gas",
    "TATN": "oil_gas",
    "TATNP": "oil_gas",
    "GMKN": "metals",
    "PLZL": "metals",
}
GROUP_CORR: dict[str, float] = {"banks": 0.6, "oil_gas": 0.6, "metals": 0.5}
CROSS_GROUP_CORR = 0.3  # between sectors, and for telecom (MTSS) and unknown tickers

# PLAN §6: an unknown ticker starts at a random price in this range.
UNKNOWN_PRICE_RANGE = (50.0, 2000.0)
