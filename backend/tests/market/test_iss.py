"""Tests for MOEX ISS response parsing and the HTTP client. No network: httpx.MockTransport and
recorded JSON fixtures stand in for iss.moex.com."""

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from app.market.errors import UpstreamError
from app.market.iss import (
    MSK,
    MoexClient,
    extract_candles,
    extract_instruments,
    extract_quotes,
    parse_block,
)
from app.market.models import Interval

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


class TestParseBlock:
    def test_columns_and_data_become_dicts(self):
        payload = {"marketdata": {"columns": ["SECID", "LAST"], "data": [["SBER", 273.28]]}}
        assert parse_block(payload, "marketdata") == [{"SECID": "SBER", "LAST": 273.28}]

    def test_missing_block_is_empty(self):
        assert parse_block({}, "marketdata") == []

    def test_empty_data_is_empty(self):
        payload = {"marketdata": {"columns": ["SECID"], "data": []}}
        assert parse_block(payload, "marketdata") == []


class TestExtractQuotes:
    def test_last_price_used_when_present(self):
        quotes = extract_quotes(load("moex_board.json"))
        assert quotes["SBER"].price == 273.28
        assert quotes["SBER"].price_field == "LAST"

    def test_delay_seconds_is_time_minus_systime(self):
        quotes = extract_quotes(load("moex_board.json"))
        assert quotes["SBER"].delay_seconds == 900  # exactly 15 minutes, per MOEX_API.md's measurement

    def test_falls_back_to_prevprice_when_untraded(self):
        """A valid ticker with no marketdata price (all fields null) falls back to securities.PREVPRICE."""
        quotes = extract_quotes(load("moex_board.json"))
        assert quotes["ZZZZ"].price == 100.5
        assert quotes["ZZZZ"].price_field == "PREVPRICE"
        assert quotes["ZZZZ"].trade_time is None

    def test_unknown_ticker_is_absent_not_an_error(self):
        payload = {
            "marketdata": {"columns": ["SECID", "LAST"], "data": []},
            "securities": {"columns": ["SECID"], "data": []},
        }
        assert extract_quotes(payload) == {}

    def test_fallback_chain_prefers_last_over_lcurrentprice(self):
        payload = {
            "marketdata": {
                "columns": ["SECID", "LAST", "LCURRENTPRICE", "LCLOSEPRICE"],
                "data": [["SBER", 273.28, 270.0, 269.0]],
            },
            "securities": {"columns": ["SECID"], "data": [["SBER"]]},
        }
        quote = extract_quotes(payload)["SBER"]
        assert quote.price == 273.28 and quote.price_field == "LAST"

    def test_fallback_chain_skips_null_last(self):
        payload = {
            "marketdata": {
                "columns": ["SECID", "LAST", "LCURRENTPRICE", "LCLOSEPRICE"],
                "data": [["SBER", None, 270.0, 269.0]],
            },
            "securities": {"columns": ["SECID"], "data": [["SBER"]]},
        }
        quote = extract_quotes(payload)["SBER"]
        assert quote.price == 270.0 and quote.price_field == "LCURRENTPRICE"

    def test_zero_price_is_treated_as_missing(self):
        """ISS sometimes returns 0 instead of null; 0 is not a valid price."""
        payload = {
            "marketdata": {"columns": ["SECID", "LAST"], "data": [["SBER", 0]]},
            "securities": {"columns": ["SECID", "PREVPRICE"], "data": [["SBER", 272.9]]},
        }
        quote = extract_quotes(payload)["SBER"]
        assert quote.price == 272.9 and quote.price_field == "PREVPRICE"

    def test_trade_time_crosses_midnight(self):
        """A trade near midnight, reported after SYSTIME has rolled to the next day."""
        payload = {
            "marketdata": {
                "columns": ["SECID", "LAST", "TIME", "SYSTIME"],
                "data": [["SBER", 273.0, "23:59:00", "2026-09-30 00:05:00"]],
            },
            "securities": {"columns": ["SECID"], "data": [["SBER"]]},
        }
        quote = extract_quotes(payload)["SBER"]
        assert quote.delay_seconds == 360  # 6 minutes
        expected = datetime(2026, 9, 29, 23, 59, 0).replace(tzinfo=MSK)
        assert quote.trade_time == expected.timestamp()

    def test_trade_time_crosses_a_weekend(self):
        """A Monday-morning poll whose last trade was Friday's close (CODE_REVIEW §3.2):
        a naive one-calendar-day rollback would land on Sunday, not Friday."""
        payload = {
            "marketdata": {
                "columns": ["SECID", "LAST", "TIME", "SYSTIME"],
                "data": [["SBER", 273.0, "18:50:00", "2026-10-05 10:00:00"]],  # Mon 2026-10-05
            },
            "securities": {"columns": ["SECID"], "data": [["SBER"]]},
        }
        quote = extract_quotes(payload)["SBER"]
        expected = datetime(2026, 10, 2, 18, 50, 0).replace(tzinfo=MSK)  # the preceding Friday
        assert quote.trade_time == expected.timestamp()
        assert quote.delay_seconds == pytest.approx(63 * 3600 + 10 * 60, abs=1)


class TestExtractInstruments:
    def test_fields_are_mapped(self):
        instruments = extract_instruments(load("moex_instrument.json"))
        info = instruments["SBER"]
        assert info.name == "Сбербанк"
        assert info.lot_size == 10
        assert info.decimals == 2
        assert info.min_step == 0.01
        assert info.prev_close == 272.45

    def test_missing_name_falls_back_to_ticker(self):
        payload = {"securities": {"columns": ["SECID"], "data": [["ZZZZ"]]}}
        assert extract_instruments(payload)["ZZZZ"].name == "ZZZZ"


class TestExtractCandles:
    def test_daily_candle_time_is_midnight_utc_of_trading_date(self):
        candles = extract_candles(load("moex_candles_daily.json"), Interval.D1)
        assert len(candles) == 2
        assert candles[0].time == int(datetime(2026, 9, 1, tzinfo=timezone.utc).timestamp())
        assert candles[0].open == 276.03
        assert candles[0].close == 272.21

    def test_rows_without_close_are_dropped(self):
        payload = {
            "candles": {
                "columns": ["open", "close", "high", "low", "volume", "begin"],
                "data": [[1.0, None, 1.0, 1.0, 1.0, "2026-09-01 00:00:00"]],
            }
        }
        assert extract_candles(payload, Interval.D1) == []


class TestMoexClient:
    async def test_retries_on_503_then_succeeds(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(503)
            return httpx.Response(
                200,
                json={
                    "marketdata": {"columns": ["SECID", "LAST"], "data": [["SBER", 273.5]]},
                    "securities": {"columns": ["SECID"], "data": [["SBER"]]},
                },
            )

        client = MoexClient(transport=httpx.MockTransport(handler), retries=2)
        quotes = await client.get_quotes(["SBER"])
        assert quotes["SBER"].price == 273.5
        assert calls["n"] == 2
        await client.aclose()

    async def test_gives_up_after_exhausting_retries(self):
        client = MoexClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)), retries=1)
        with pytest.raises(UpstreamError):
            await client.get_quotes(["SBER"])
        await client.aclose()

    async def test_4xx_is_not_retried(self):
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(400)

        client = MoexClient(transport=httpx.MockTransport(handler), retries=3)
        with pytest.raises(UpstreamError):
            await client.get_quotes(["SBER"])
        assert calls["n"] == 1
        await client.aclose()

    async def test_malformed_json_raises_upstream_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"not json")

        client = MoexClient(transport=httpx.MockTransport(handler), retries=0)
        with pytest.raises(UpstreamError):
            await client.get_quotes(["SBER"])
        await client.aclose()

    async def test_get_instruments_wraps_extract_instruments(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=load("moex_instrument.json"))

        client = MoexClient(transport=httpx.MockTransport(handler), retries=0)
        instruments = await client.get_instruments(["SBER"])
        assert instruments["SBER"].name == "Сбербанк"
        await client.aclose()

    async def test_get_index_candles_hits_the_index_path(self):
        seen_paths = []
        empty = {"candles": {"columns": ["open", "close", "high", "low", "volume", "begin"], "data": []}}

        def handler(request: httpx.Request) -> httpx.Response:
            seen_paths.append(request.url.path)
            body = load("moex_candles_daily.json") if len(seen_paths) == 1 else empty
            return httpx.Response(200, json=body)

        client = MoexClient(transport=httpx.MockTransport(handler))
        candles = await client.get_index_candles("IMOEX", Interval.D1, start=date(2026, 9, 1))
        assert len(candles) == 2
        assert all("index" in p and "IMOEX" in p for p in seen_paths)
        await client.aclose()

    async def test_candles_paginate_until_empty_page(self):
        pages = [
            {"candles": {"columns": ["open", "close", "high", "low", "volume", "begin"], "data": [[1, 1, 1, 1, 1, "2026-09-01 00:00:00"]]}},
            {"candles": {"columns": ["open", "close", "high", "low", "volume", "begin"], "data": [[2, 2, 2, 2, 2, "2026-09-02 00:00:00"]]}},
            {"candles": {"columns": ["open", "close", "high", "low", "volume", "begin"], "data": []}},
        ]
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            page = pages[min(calls["n"], len(pages) - 1)]
            calls["n"] += 1
            return httpx.Response(200, json=page)

        client = MoexClient(transport=httpx.MockTransport(handler))
        candles = await client.get_candles("SBER", Interval.D1, start=date(2026, 9, 1))
        assert len(candles) == 2
        assert calls["n"] == 3  # two pages with data, then one empty page to stop
        await client.aclose()
