"""MOEX ISS: response parsing and HTTP client."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

from .errors import UpstreamError
from .models import Candle, InstrumentInfo, Interval

logger = logging.getLogger(__name__)

ISS_BASE = "https://iss.moex.com/iss"
TQBR = "engines/stock/markets/shares/boards/TQBR"
INDEX_PATH = "engines/stock/markets/index/boards/SNDX/securities"  # IMOEX
MSK = ZoneInfo("Europe/Moscow")


def parse_block(payload: dict, name: str) -> list[dict]:
    """{'columns': [...], 'data': [[...]]} -> [{col: value}, ...]. Missing block / empty data -> []."""
    block = payload.get(name) or {}
    columns = block.get("columns") or []
    return [dict(zip(columns, row)) for row in block.get("data") or []]


def _num(value) -> float | None:
    """A number > 0, or None. null, 0, and non-numbers (ISS sometimes returns '') mean "no price"."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


@dataclass(frozen=True, slots=True)
class MoexQuote:
    ticker: str
    price: float
    price_field: str  # where the price came from: LAST / LCURRENTPRICE / LCLOSEPRICE / PREVPRICE
    trade_time: float | None  # Unix seconds of the last trade
    delay_seconds: int | None  # SYSTIME - TIME (~900 on the free tier)


def _trade_time(md: dict) -> tuple[float | None, int | None]:
    """TIME ('HH:MM:SS') and SYSTIME ('YYYY-MM-DD HH:MM:SS') are the same timezone, so their
    difference does not depend on which timezone that is."""
    systime, tm = md.get("SYSTIME"), md.get("TIME")
    if not systime or not tm:
        return None, None
    try:
        sys_dt = datetime.strptime(systime, "%Y-%m-%d %H:%M:%S")
        trade_dt = datetime.combine(sys_dt.date(), datetime.strptime(tm, "%H:%M:%S").time())
    except ValueError:
        return None, None
    if trade_dt > sys_dt:  # the trade was "yesterday"; SYSTIME is already past midnight
        trade_dt -= timedelta(days=1)
    return trade_dt.replace(tzinfo=MSK).timestamp(), int((sys_dt - trade_dt).total_seconds())


# Price is taken from a fallback chain: last trade -> main-session current price -> official
# close -> yesterday's close. The last three matter when a ticker is valid but hasn't traded
# today yet (pre-market, a holiday, an illiquid name).
_MARKETDATA_PRICE_FIELDS = ("LAST", "LCURRENTPRICE", "LCLOSEPRICE")


def extract_quotes(payload: dict) -> dict[str, MoexQuote]:
    """`.../TQBR/securities.json?iss.only=marketdata,securities` response -> {ticker: MoexQuote}.

    An unknown ticker gives an empty `data` array (HTTP 200) and is simply absent from the
    result — "no price yet". The PREVPRICE fallback applies only to tickers present in the
    `securities` block, i.e. valid ones.
    """
    securities = {r["SECID"]: r for r in parse_block(payload, "securities") if r.get("SECID")}
    quotes: dict[str, MoexQuote] = {}
    for md in parse_block(payload, "marketdata"):
        ticker = md.get("SECID")
        if not ticker:
            continue
        price, field = None, ""
        for candidate in _MARKETDATA_PRICE_FIELDS:
            price = _num(md.get(candidate))
            if price is not None:
                field = candidate
                break
        if price is None and ticker in securities:
            price = _num(securities[ticker].get("PREVPRICE"))
            field = "PREVPRICE"
        if price is None:
            continue
        trade_time, delay = _trade_time(md)
        quotes[ticker] = MoexQuote(ticker, price, field, trade_time, delay)
    return quotes


def extract_instruments(payload: dict) -> dict[str, InstrumentInfo]:
    out = {}
    for row in parse_block(payload, "securities"):
        ticker = row.get("SECID")
        if ticker:
            out[ticker] = InstrumentInfo(
                ticker=ticker,
                name=row.get("SHORTNAME") or ticker,
                lot_size=row.get("LOTSIZE"),
                decimals=row.get("DECIMALS"),
                min_step=row.get("MINSTEP"),
                prev_close=_num(row.get("PREVPRICE")),
            )
    return out


def _begin_to_unix(begin: str, daily: bool) -> int:
    dt = datetime.strptime(begin, "%Y-%m-%d %H:%M:%S")
    if daily:  # trading date -> midnight UTC (Lightweight Charts draws in UTC)
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    return int(dt.replace(tzinfo=MSK).timestamp())


def extract_candles(payload: dict, interval: Interval) -> list[Candle]:
    return [
        Candle(
            time=_begin_to_unix(r["begin"], interval.is_daily),
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r.get("volume") or 0.0,
        )
        for r in parse_block(payload, "candles")
        if r.get("close") is not None and r.get("begin")
    ]


_RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_PAGES = 20  # guard against unbounded pagination


class MoexClient:
    """Thin async ISS client. Every network failure becomes an UpstreamError."""

    def __init__(
        self,
        *,
        base_url: str = ISS_BASE,
        timeout: float = 10.0,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,  # swapped out in tests: httpx.MockTransport
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": "FinAlly/0.1 (educational project)"},
        )
        self._retries = retries

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get_json(self, path: str, params: dict) -> dict:
        last: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                resp = await self._http.get(path, params=params)
                if resp.status_code in _RETRY_STATUS:
                    last = UpstreamError(f"MOEX ISS HTTP {resp.status_code}")
                else:
                    resp.raise_for_status()  # other 4xx are not retried
                    return resp.json()
            except httpx.HTTPStatusError as e:
                raise UpstreamError(f"MOEX ISS HTTP {e.response.status_code}") from e
            except (httpx.TransportError, ValueError) as e:  # network/timeout; ValueError = bad JSON
                last = e
            if attempt < self._retries:
                await asyncio.sleep(0.5 * 2**attempt)
        raise UpstreamError(f"MOEX ISS unavailable: {last}") from last

    # --- live quotes and reference data (one request per batch of tickers) ---

    async def _board(self, tickers: list[str]) -> dict:
        return await self._get_json(
            f"{TQBR}/securities.json",
            {"securities": ",".join(tickers), "iss.meta": "off", "iss.only": "marketdata,securities"},
        )

    async def get_quotes(self, tickers: list[str]) -> dict[str, MoexQuote]:
        return extract_quotes(await self._board(tickers))

    async def get_instruments(self, tickers: list[str]) -> dict[str, InstrumentInfo]:
        return extract_instruments(await self._board(tickers))

    # --- candles ---

    async def _candles(self, path: str, interval: Interval, start: date, end: date | None) -> list[Candle]:
        out: list[Candle] = []
        for _ in range(MAX_PAGES):  # ISS pages candles; keep going until a page is empty
            params = {
                "interval": interval.moex_code,
                "from": start.isoformat(),
                "start": len(out),
                "iss.meta": "off",
                "iss.only": "candles",
            }
            if end:
                params["till"] = end.isoformat()
            page = extract_candles(await self._get_json(path, params), interval)
            if not page:
                break
            out.extend(page)
        return out

    async def get_candles(
        self, ticker: str, interval: Interval, start: date, end: date | None = None
    ) -> list[Candle]:
        return await self._candles(f"{TQBR}/securities/{ticker}/candles.json", interval, start, end)

    async def get_index_candles(
        self, index: str, interval: Interval, start: date, end: date | None = None
    ) -> list[Candle]:
        return await self._candles(f"{INDEX_PATH}/{index}/candles.json", interval, start, end)
