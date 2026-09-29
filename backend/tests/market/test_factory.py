"""Tests for market data source factory."""

import os
from unittest.mock import patch

from app.market.cache import PriceCache
from app.market.factory import create_candle_service, create_market_data_source, moex_enabled
from app.market.history import CandleService, MoexCandleProvider, SyntheticCandleProvider
from app.market.moex_client import MoexDataSource
from app.market.simulator import SimulatorDataSource


class TestMoexEnabled:
    """Tests for the moex_enabled() env var check."""

    def test_unset_is_false(self):
        with patch.dict(os.environ, {}, clear=True):
            assert moex_enabled() is False

    def test_empty_is_false(self):
        with patch.dict(os.environ, {"MOEX_ENABLED": ""}, clear=True):
            assert moex_enabled() is False

    def test_whitespace_is_false(self):
        with patch.dict(os.environ, {"MOEX_ENABLED": "   "}, clear=True):
            assert moex_enabled() is False

    def test_other_value_is_false(self):
        with patch.dict(os.environ, {"MOEX_ENABLED": "yes"}, clear=True):
            assert moex_enabled() is False

    def test_true_case_insensitive(self):
        for value in ("true", "True", "TRUE", " true "):
            with patch.dict(os.environ, {"MOEX_ENABLED": value}, clear=True):
                assert moex_enabled() is True


class TestFactory:
    """Tests for create_market_data_source factory."""

    def test_creates_simulator_when_disabled(self):
        cache = PriceCache()
        with patch.dict(os.environ, {}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)

    def test_creates_moex_when_enabled(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MOEX_ENABLED": "true"}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, MoexDataSource)

    def test_simulator_receives_cache(self):
        cache = PriceCache()
        with patch.dict(os.environ, {}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, SimulatorDataSource)
        assert source._cache is cache

    def test_moex_receives_cache(self):
        cache = PriceCache()
        with patch.dict(os.environ, {"MOEX_ENABLED": "true"}, clear=True):
            source = create_market_data_source(cache)
        assert isinstance(source, MoexDataSource)
        assert source._cache is cache


class TestCreateCandleService:
    """Tests for create_candle_service: picks the provider matching the active source."""

    def test_moex_source_gets_moex_provider(self):
        cache = PriceCache()
        source = MoexDataSource(cache)
        service = create_candle_service(cache, source)
        assert isinstance(service, CandleService)
        assert isinstance(service._provider, MoexCandleProvider)

    def test_simulator_source_gets_synthetic_provider(self):
        cache = PriceCache()
        source = SimulatorDataSource(cache)
        service = create_candle_service(cache, source)
        assert isinstance(service, CandleService)
        assert isinstance(service._provider, SyntheticCandleProvider)
