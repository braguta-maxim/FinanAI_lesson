# Market Data Backend (MOEX) — детальный дизайн

Проект реализации подсистемы рыночных данных FinAlly для **российского рынка (MOEX)**: источники данных
(симулятор и MOEX ISS), кэш цен, SSE, REST API рыночных данных, исторические свечи и аналитика.

Документ **заменяет** `planning/archive/MARKET_DATA_DESIGN.md` (US-акции, Massive/Polygon) и опирается на:

| Документ | Что берём |
|---|---|
| `PLAN.md` §3, §5–§8 | контракт: `MOEX_ENABLED`, тикеры, SSE-формат, порядок старта, правила трейдов |
| `MOEX_API.md` | эндпоинты ISS, формат `columns/data`, задержка 15 мин, пустой `data` для неизвестного тикера |
| `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`, `MARKET_DATA_SUMMARY.md` | уже написанный код `backend/app/market/` (интерфейс, кэш, GBM, SSE) |
| `REVIEW.md`, `CODE_REVIEW.md` | исправленные ранее ошибки (гонка в `remove_ticker`, роутер SSE в фабрике и т.д.) — не повторяем |
| `MASSIVE_API.md` | только как история: Massive **удаляется** из кода |

> **Состояние кода.** Реализованный `backend/app/market/` всё ещё Massive/US (`massive_client.py`, тикеры
> AAPL…, цены в $). Этот документ описывает, что оставить, что изменить и что добавить. Документы
> `MARKET_INTERFACE.md` / `MARKET_SIMULATOR.md` / `MARKET_DATA_SUMMARY.md` и `backend/CLAUDE.md` после
> реализации надо обновить под MOEX (см. §15).

---

## Содержание

1. [Цели и принципы](#1-цели-и-принципы)
2. [Структура файлов](#2-структура-файлов)
3. [Модели и ошибки](#3-модели-и-ошибки)
4. [Ценовой кэш](#4-ценовой-кэш)
5. [Интерфейс источника](#5-интерфейс-источника)
6. [Клиент MOEX ISS](#6-клиент-moex-iss)
7. [`MoexDataSource`](#7-moexdatasource)
8. [Симулятор для MOEX](#8-симулятор-для-moex)
9. [Фабрики и согласование тикеров](#9-фабрики-и-согласование-тикеров)
10. [История: свечи](#10-история-свечи)
11. [Аналитика](#11-аналитика)
12. [REST API рыночных данных](#12-rest-api-рыночных-данных)
13. [Интеграция в FastAPI](#13-интеграция-в-fastapi)
14. [Тесты](#14-тесты)
15. [Конфигурация, зависимости, порядок работ](#15-конфигурация-зависимости-порядок-работ)
16. [Непроверенное и отклонения от PLAN.md](#16-непроверенное-и-отклонения-от-planmd)

---

## 1. Цели и принципы

1. **Один интерфейс, два источника.** `SimulatorDataSource` и `MoexDataSource` реализуют `MarketDataSource`
   и пишут в один `PriceCache`. Всё остальное (SSE, портфель, трейды, LLM) читает только кэш.
2. **Опрос, а не push.** MOEX ISS опрашивается REST-запросом раз в 15 с (PLAN §6); бесплатный тариф даёт цены
   с задержкой ~15 минут, но **непрерывно обновляемые**.
3. **Живые цены — из кэша, история — из отдельного сервиса.** Свечи (для графика и аналитики) идут через
   `CandleService` с TTL-кэшем и двумя провайдерами: `MoexCandleProvider` (ISS) и `SyntheticCandleProvider`
   (режим симулятора — приложение должно работать без сети).
4. **Аналитика — чистые функции на numpy** (без pandas, `numpy` уже в зависимостях). Никакого I/O внутри —
   легко тестировать на известных значениях.
5. **Устойчивость.** Ошибка опроса — лог + повтор с backoff, а не падение задачи; невалидный тикер — «нет
   цены», а не исключение (PLAN §6).
6. **Деньги — рубли, простые числа.** Валюту (`₽`) рисует фронтенд (PLAN §10). Время — Unix-секунды UTC.

Что вне рамок: торговые правила и портфель (`portfolio/`), схема БД, LLM. Здесь только рыночные данные и то,
что от них считается.

---

## 2. Структура файлов

```
backend/app/market/
  __init__.py          # реэкспорт публичного API (обновить)
  errors.py            # NEW  MarketDataError, InvalidTickerError, UpstreamError, NoDataError
  tickers.py           # NEW  normalize_ticker(), sync_tracking()
  models.py            # +Candle, InstrumentInfo, SourceStatus, Interval
  cache.py             # +история тиков, округление до 4 знаков
  interface.py         # +status() (не abstract)
  seed_prices.py       # CHANGED  MOEX-тикеры, группы, параметры
  simulator.py         # CHANGED  dt выводится из update_interval, группы MOEX, status()
  iss.py               # NEW  разбор ответов ISS + MoexClient (httpx)
  moex_client.py       # NEW  MoexDataSource  (заменяет massive_client.py)
  factory.py           # CHANGED  MOEX_ENABLED; create_candle_service()
  stream.py            # без изменений (SSE)
  sessions.py          # NEW  is_main_session(), session_stats(), market_context_lines()
  history.py           # NEW  CandleService, MoexCandleProvider, SyntheticCandleProvider
  analytics.py         # NEW  индикаторы, риск, корреляции (numpy)
  api.py               # NEW  create_market_router() — /api/market/*
backend/tests/market/
  fixtures/            # записанные ответы ISS (json)
  test_iss.py  test_moex_source.py  test_history.py  test_analytics.py  test_api.py  test_contract.py
```

Удалить: `massive_client.py`, `tests/market/test_massive.py`, зависимость `massive`.

---

## 3. Модели и ошибки

### 3.1 `errors.py`

```python
"""Исключения подсистемы рыночных данных. API-слой сам переводит их в HTTP-коды."""


class MarketDataError(Exception):
    """Базовое исключение."""


class InvalidTickerError(MarketDataError, ValueError):
    """Тикер не 1–5 латинских букв."""


class UpstreamError(MarketDataError):
    """MOEX ISS недоступен / вернул ошибку (после ретраев)."""


class NoDataError(MarketDataError):
    """Данных по инструменту нет (неизвестный тикер, пустой ответ ISS)."""
```

### 3.2 `models.py` — добавления (`PriceUpdate` не меняется)

```python
from enum import Enum


class Interval(str, Enum):
    """Интервал свечей. Значение — то, что принимает наш API."""
    M1 = "1m"
    M10 = "10m"
    H1 = "1h"
    D1 = "1d"

    @property
    def moex_code(self) -> int:
        """Параметр `interval` ISS: 1, 10, 60 минут или 24 (день)."""
        return {"1m": 1, "10m": 10, "1h": 60, "1d": 24}[self.value]

    @property
    def is_daily(self) -> bool:
        return self is Interval.D1


@dataclass(frozen=True, slots=True)
class Candle:
    time: int          # Unix-секунды UTC, начало свечи (для дневных — 00:00 UTC даты торгов)
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        return {"time": self.time, "open": self.open, "high": self.high,
                "low": self.low, "close": self.close, "volume": self.volume}


@dataclass(frozen=True, slots=True)
class InstrumentInfo:
    ticker: str
    name: str                    # SHORTNAME, напр. "Сбербанк"
    lot_size: int | None = None  # справочно: приложение торгует дробными акциями
    decimals: int | None = None
    min_step: float | None = None
    prev_close: float | None = None

    def to_dict(self) -> dict:
        return {"ticker": self.ticker, "name": self.name, "lot_size": self.lot_size,
                "decimals": self.decimals, "min_step": self.min_step, "prev_close": self.prev_close}


@dataclass(frozen=True, slots=True)
class SourceStatus:
    source: str                        # "simulator" | "moex"
    healthy: bool
    delay_seconds: int                 # 0 у симулятора, ~900 у MOEX
    last_success: float | None = None  # Unix-секунды
    last_error: str | None = None
    consecutive_failures: int = 0

    def to_dict(self) -> dict:
        return {"source": self.source, "healthy": self.healthy, "delay_seconds": self.delay_seconds,
                "last_success": self.last_success, "last_error": self.last_error,
                "consecutive_failures": self.consecutive_failures}
```

Свечи `time` для дневных баров — **полночь UTC даты торгов**, а не полночь МСК: иначе Lightweight Charts
(рисует в UTC) покажет предыдущее число. Для внутридневных — обычный Unix-момент начала свечи.

### 3.3 `tickers.py` — единая нормализация и синхронизация «отслеживаемых»

PLAN §8 отдаёт нормализацию сервису трейдов/вотчлиста. Чтобы у сервиса и API рыночных данных не было двух
регулярок, функция живёт здесь и используется обоими.

```python
"""Нормализация тикеров и правило «отслеживаемые = watchlist ∪ позиции» (PLAN §6)."""
import re

from .errors import InvalidTickerError
from .interface import MarketDataSource

_TICKER_RE = re.compile(r"[A-Z]{1,5}")


def normalize_ticker(raw: str) -> str:
    """strip + upper + проверка «1–5 латинских букв» (SBERP, TATNP подходят; BRK.B — нет)."""
    ticker = (raw or "").strip().upper()
    if not _TICKER_RE.fullmatch(ticker):
        raise InvalidTickerError(f"Invalid ticker '{raw}': expected 1-5 letters")
    return ticker


async def sync_tracking(
    source: MarketDataSource, ticker: str, *, in_watchlist: bool, has_position: bool
) -> None:
    """Единственное место, где решается, следит ли источник за тикером.

    Вызывать после любого изменения вотчлиста или позиции (ручного, через чат, через трейд).
    """
    if in_watchlist or has_position:
        await source.add_ticker(ticker)      # no-op, если уже отслеживается
    else:
        await source.remove_ticker(ticker)   # no-op, если не отслеживается
```

---

## 4. Ценовой кэш

Изменения относительно текущего `cache.py`:

1. **Округление до 4 знаков** вместо 2. Цены MOEX бывают с 4 знаками после запятой; округление до копеек
   исказило бы дешёвые бумаги. (Симулятор продолжает округлять свои тики до копеек — §8.)
2. **История тиков** — кольцевой буфер на тикер (последние N точек). Питает `session_stats()` и LLM-контекст,
   ничего не стоит: 10 тикеров × 3600 точек ≈ 36k кортежей.

```python
from collections import deque


class PriceCache:
    def __init__(self, history_size: int = 3600) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._history: dict[str, deque[tuple[float, float]]] = {}   # ticker -> (timestamp, price)
        self._history_size = history_size
        self._lock = Lock()
        self._version = 0

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        with self._lock:
            ts = timestamp if timestamp is not None else time.time()
            prev = self._prices.get(ticker)
            price = round(price, 4)
            update = PriceUpdate(
                ticker=ticker,
                price=price,
                previous_price=prev.price if prev else price,
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._history.setdefault(ticker, deque(maxlen=self._history_size)).append((ts, price))
            self._version += 1
            return update

    def get_history(self, ticker: str) -> list[tuple[float, float]]:
        """[(timestamp, price), ...] от старых к новым. Пусто, если тикера нет."""
        with self._lock:
            return list(self._history.get(ticker, ()))

    def remove(self, ticker: str) -> None:
        with self._lock:
            self._history.pop(ticker, None)
            if self._prices.pop(ticker, None) is not None:
                self._version += 1
```

Остальное (`get`, `get_all`, `get_price`, `version`, `__len__`, `__contains__`) — как есть.
Обновить тесты `test_cache.py`, которые ожидают округление до 2 знаков.

---

## 5. Интерфейс источника

Добавляется один **не-абстрактный** метод — состояние источника для `/api/market/status`. Существующие
реализации и тесты не ломаются.

```python
# interface.py
from .models import SourceStatus


class MarketDataSource(ABC):
    ...  # start / stop / add_ticker / remove_ticker / get_tickers — без изменений

    def status(self) -> SourceStatus:
        """Состояние источника. По умолчанию — «здоров, без задержки»."""
        return SourceStatus(source=type(self).__name__, healthy=True, delay_seconds=0)
```

Контракт (из `MARKET_INTERFACE.md` §2, остаётся в силе): `start()` вызывается один раз с
`watchlist ∪ positions`; `add/remove_ticker` идемпотентны; `remove_ticker` сам чистит кэш; `stop()`
безопасен при повторном вызове; **тикеры приходят уже нормализованными** (`normalize_ticker`).

---

## 6. Клиент MOEX ISS

Файл `iss.py`. Два слоя: **чистые парсеры** (легко тестировать на JSON-фикстурах) и **`MoexClient`**
(HTTP, ретраи, пагинация). Клиентская библиотека (`aiomoex`) не нужна: эндпоинты — обычный `GET` + JSON,
а `httpx` уже нужен для тестов FastAPI.

### 6.1 Формат ответа ISS

ISS отдаёт **колонки и массив строк**, а не объекты:

```json
{"marketdata": {"columns": ["SECID", "LAST", "TIME", "SYSTIME"],
                "data": [["SBER", 273.28, "08:25:11", "2026-09-29 08:40:11"]]}}
```

Имена колонок в `marketdata`/`securities` — ЗАГЛАВНЫМИ, в блоке `candles` — строчными
(`open, close, high, low, value, volume, begin, end`, см. `MOEX_API.md` §5). Время в ответах — московское
(МСК, UTC+3, без перехода на летнее время).

### 6.2 Разбор ответа

```python
"""MOEX ISS: разбор ответов и HTTP-клиент."""
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
INDEX_PATH = "engines/stock/markets/index/boards/SNDX/securities"   # IMOEX; проверить, см. §16
MSK = ZoneInfo("Europe/Moscow")


def parse_block(payload: dict, name: str) -> list[dict]:
    """{'columns': [...], 'data': [[...]]} -> [{col: value}, ...]. Нет блока / пустой data -> []."""
    block = payload.get(name) or {}
    columns = block.get("columns") or []
    return [dict(zip(columns, row)) for row in block.get("data") or []]


def _num(value) -> float | None:
    """Число > 0 или None. null, 0 и не-числа (ISS иногда отдаёт '') считаются «нет цены»."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value > 0 else None


@dataclass(frozen=True, slots=True)
class MoexQuote:
    ticker: str
    price: float
    price_field: str              # откуда взята цена: LAST / LCURRENTPRICE / LCLOSEPRICE / PREVPRICE
    trade_time: float | None      # Unix-секунды последней сделки
    delay_seconds: int | None     # SYSTIME - TIME (~900 на бесплатном тарифе)


def _trade_time(md: dict) -> tuple[float | None, int | None]:
    """TIME ('HH:MM:SS') и SYSTIME ('YYYY-MM-DD HH:MM:SS') — часы одного пояса, разница от пояса не зависит."""
    systime, tm = md.get("SYSTIME"), md.get("TIME")
    if not systime or not tm:
        return None, None
    try:
        sys_dt = datetime.strptime(systime, "%Y-%m-%d %H:%M:%S")
        trade_dt = datetime.combine(sys_dt.date(), datetime.strptime(tm, "%H:%M:%S").time())
    except ValueError:
        return None, None
    if trade_dt > sys_dt:                     # сделка «вчера», SYSTIME уже после полуночи
        trade_dt -= timedelta(days=1)
    return trade_dt.replace(tzinfo=MSK).timestamp(), int((sys_dt - trade_dt).total_seconds())


# Цена берётся по цепочке: последняя сделка -> цена основной сессии -> официальное закрытие -> закрытие вчера.
# Три последних нужны, когда бумага валидна, но сегодня ещё не торговалась (пре-маркет, выходной, неликвид).
_MARKETDATA_PRICE_FIELDS = ("LAST", "LCURRENTPRICE", "LCLOSEPRICE")


def extract_quotes(payload: dict) -> dict[str, MoexQuote]:
    """Ответ `.../TQBR/securities.json?iss.only=marketdata,securities` -> {ticker: MoexQuote}.

    Неизвестный тикер даёт пустой `data` (HTTP 200) и просто отсутствует в результате — «цены пока нет».
    Фолбэк на PREVPRICE применяется только к тикерам, присутствующим в блоке `securities`, т.е. валидным.
    """
    securities = {r["SECID"]: r for r in parse_block(payload, "securities") if r.get("SECID")}
    quotes: dict[str, MoexQuote] = {}
    for md in parse_block(payload, "marketdata"):
        ticker = md.get("SECID")
        if not ticker:
            continue
        price, field = None, ""
        for candidate in _MARKETDATA_PRICE_FIELDS:
            if (price := _num(md.get(candidate))) is not None:
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
        if ticker := row.get("SECID"):
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
    if daily:                                        # дата торгов -> полночь UTC (см. §3.2)
        return int(dt.replace(tzinfo=timezone.utc).timestamp())
    return int(dt.replace(tzinfo=MSK).timestamp())


def extract_candles(payload: dict, interval: Interval) -> list[Candle]:
    return [
        Candle(
            time=_begin_to_unix(r["begin"], interval.is_daily),
            open=r["open"], high=r["high"], low=r["low"], close=r["close"],
            volume=r.get("volume") or 0.0,
        )
        for r in parse_block(payload, "candles")
        if r.get("close") is not None and r.get("begin")
    ]
```

### 6.3 `MoexClient`

```python
_RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_PAGES = 20            # предохранитель от бесконечной пагинации


class MoexClient:
    """Тонкий async-клиент ISS. Все сетевые сбои превращаются в UpstreamError."""

    def __init__(
        self,
        *,
        base_url: str = ISS_BASE,
        timeout: float = 10.0,
        retries: int = 2,
        transport: httpx.AsyncBaseTransport | None = None,   # подмена в тестах: httpx.MockTransport
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
                    resp.raise_for_status()          # прочие 4xx — не ретраим
                    return resp.json()
            except httpx.HTTPStatusError as e:
                raise UpstreamError(f"MOEX ISS HTTP {e.response.status_code}") from e
            except (httpx.TransportError, ValueError) as e:   # сеть/таймаут; ValueError — битый JSON
                last = e
            if attempt < self._retries:
                await asyncio.sleep(0.5 * 2**attempt)
        raise UpstreamError(f"MOEX ISS unavailable: {last}") from last

    # --- живые котировки и справочник (один запрос на пачку тикеров) ---

    async def _board(self, tickers: list[str]) -> dict:
        return await self._get_json(
            f"{TQBR}/securities.json",
            {"securities": ",".join(tickers), "iss.meta": "off", "iss.only": "marketdata,securities"},
        )

    async def get_quotes(self, tickers: list[str]) -> dict[str, MoexQuote]:
        return extract_quotes(await self._board(tickers))

    async def get_instruments(self, tickers: list[str]) -> dict[str, InstrumentInfo]:
        return extract_instruments(await self._board(tickers))

    # --- свечи ---

    async def _candles(self, path: str, interval: Interval, start: date, end: date | None) -> list[Candle]:
        out: list[Candle] = []
        for _ in range(MAX_PAGES):                    # ISS отдаёт свечи страницами — идём, пока не пусто
            params = {"interval": interval.moex_code, "from": start.isoformat(),
                      "start": len(out), "iss.meta": "off", "iss.only": "candles"}
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
```

Примечания:

- **Rate limit.** Официального лимита нет (`MOEX_API.md` §6). Живой опрос — 1 запрос/15 с; свечи кэшируются
  (§10), так что фоновая нагрузка на ISS минимальна. Никаких параллельных «веерных» запросов на каждый тикер.
- **Пагинация свечей** идёт по `start=<уже получено>`; размер страницы не хардкодим, останавливаемся на
  первой пустой странице.
- Длина URL: 10–50 тикеров в `securities=` безопасно; `MoexDataSource` режет на пачки (§7).

---

## 7. `MoexDataSource`

Файл `moex_client.py` (заменяет `massive_client.py`). Тот же жизненный цикл, что у Massive-версии
(блокирующий первый опрос в `start()` — PLAN §7), плюс три улучшения.

| Улучшение | Зачем |
|---|---|
| `add_ticker` сразу запрашивает цену этого тикера | иначе первая же сделка по новому тикеру падает с «No price available… try again» (PLAN §8, шаг 5) — до 15 с ожидания |
| экспоненциальный backoff при сбоях | не долбить ISS во время его проблем; `min(15·2^n, 120)` с |
| `status()` | `/api/market/status`, индикация деградации |

```python
"""MarketDataSource поверх MOEX ISS."""
from __future__ import annotations

import asyncio
import logging
import time

from .cache import PriceCache
from .errors import UpstreamError
from .interface import MarketDataSource
from .iss import MoexClient
from .models import SourceStatus

logger = logging.getLogger(__name__)


class MoexDataSource(MarketDataSource):
    """Опрашивает TQBR раз в `poll_interval` секунд одним запросом на пачку тикеров."""

    def __init__(
        self,
        price_cache: PriceCache,
        client: MoexClient | None = None,
        poll_interval: float = 15.0,
        batch_size: int = 50,
    ) -> None:
        self._cache = price_cache
        self._client = client or MoexClient()
        self._owns_client = client is None
        self._interval = poll_interval
        self._batch = batch_size
        self._tickers: list[str] = []
        self._task: asyncio.Task | None = None
        # состояние для status()
        self._failures = 0
        self._last_ok: float | None = None
        self._last_error: str | None = None
        self._delay = 900

    @property
    def client(self) -> MoexClient:
        """Общий клиент: CandleService использует его же (одна пула соединений)."""
        return self._client

    async def start(self, tickers: list[str]) -> None:
        self._tickers = list(dict.fromkeys(tickers))
        await self._poll(self._tickers)          # блокирующий первый опрос: цены есть до стартового снапшота
        self._task = asyncio.create_task(self._poll_loop(), name="moex-poller")
        logger.info("MOEX poller started: %d tickers, %.0fs interval", len(self._tickers), self._interval)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        if self._owns_client:
            await self._client.aclose()

    async def add_ticker(self, ticker: str) -> None:
        if ticker in self._tickers:
            return
        self._tickers.append(ticker)
        await self._poll([ticker])               # не ждём следующего цикла (до 15 с)

    async def remove_ticker(self, ticker: str) -> None:
        if ticker in self._tickers:
            self._tickers.remove(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    def status(self) -> SourceStatus:
        return SourceStatus(
            source="moex",
            healthy=self._last_ok is not None and self._failures < 3,
            delay_seconds=self._delay,
            last_success=self._last_ok,
            last_error=self._last_error,
            consecutive_failures=self._failures,
        )

    # --- внутреннее ---

    async def _poll_loop(self) -> None:
        while True:
            pause = self._interval if self._failures == 0 else min(self._interval * 2**self._failures, 120)
            await asyncio.sleep(pause)
            await self._poll(list(self._tickers))

    async def _poll(self, tickers: list[str]) -> None:
        """Один цикл опроса. Никогда не бросает наружу: ошибка = лог + backoff."""
        if not tickers:
            return
        ok = True
        for i in range(0, len(tickers), self._batch):
            chunk = tickers[i : i + self._batch]
            try:
                quotes = await self._client.get_quotes(chunk)
            except UpstreamError as e:
                ok, self._last_error = False, str(e)
                logger.warning("MOEX poll failed: %s", e)
                continue
            except Exception as e:                                   # noqa: BLE001 — задача не должна умирать
                ok, self._last_error = False, repr(e)
                logger.exception("MOEX poll crashed")
                continue

            for ticker, quote in quotes.items():
                if ticker not in self._tickers:                      # remove_ticker во время запроса (CODE_REVIEW H3)
                    continue
                self._cache.update(ticker, quote.price)              # метка времени — момент опроса
                if quote.delay_seconds is not None:
                    self._delay = quote.delay_seconds
            missing = [t for t in chunk if t not in quotes]
            if missing:
                logger.debug("MOEX: no price yet for %s (invalid symbol or not traded)", missing)

        if ok:
            self._failures, self._last_ok, self._last_error = 0, time.time(), None
        else:
            self._failures += 1
```

Поведение:

- **Неизвестный тикер** (пустой `data`) → цены нет, в кэше его нет, `PriceCache.get_price()` → `None`, трейд по нему
  отклоняется (PLAN §6, §8 шаг 5). Фронтенд показывает «—».
- **`cache.update` штампует временем опроса**, а не временем сделки: сделка на MOEX — 15-минутной давности, а
  SSE и график ждут «когда мы это узнали». Реальная задержка отдаётся в `status().delay_seconds`.
- **Повторяющаяся цена.** Если цена не изменилась, `update()` всё равно бампит `version` → SSE шлёт событие раз в
  15 с с `direction: "flat"`. Это нормально: клиент видит, что связь жива.
- **Вне торговой сессии** `LAST` остаётся последней сделкой, а для «неторгованных» бумаг цепочка фолбэков даёт
  вчерашнее закрытие, так что приложение не «пустое» ночью и в выходные. (Поведение `LAST` в выходные не
  проверено — см. §16; фолбэк покрывает оба варианта.)

---

## 8. Симулятор для MOEX

Симулятор остаётся источником по умолчанию (PLAN §6). Структура `GBMSimulator` / `SimulatorDataSource` не
меняется — адаптируются данные и три места в коде.

### 8.1 `seed_prices.py`

Цены ориентировочные (SBER/GAZP/LKOH — из PLAN §6; остальные — порядок величины). **Перед реализацией сверить
с `PREVPRICE` из ISS** — на корректность симулятора это не влияет, но реалистичность демо выше.

```python
"""Стартовые цены (₽) и параметры GBM для дефолтного вотчлиста MOEX."""

SEED_PRICES: dict[str, float] = {
    "SBER": 273.0,
    "GAZP": 97.0,
    "LKOH": 5315.0,
    "GMKN": 130.0,
    "ROSN": 430.0,
    "NVTK": 900.0,
    "MTSS": 220.0,
    "TATN": 600.0,
    "PLZL": 2000.0,
    "VTBR": 80.0,
}

# sigma — годовая волатильность, mu — годовой дрейф. Российские бумаги заметно волатильнее US blue chips.
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
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.30, "mu": 0.08}

# Сектора -> корреляция внутри сектора. Правило: каждая внутригрупповая корреляция >= CROSS_GROUP_CORR,
# иначе матрица может перестать быть положительно полуопределённой и Cholesky упадёт.
TICKER_GROUP: dict[str, str] = {
    "SBER": "banks", "SBERP": "banks", "VTBR": "banks",
    "GAZP": "oil_gas", "LKOH": "oil_gas", "ROSN": "oil_gas", "NVTK": "oil_gas",
    "TATN": "oil_gas", "TATNP": "oil_gas",
    "GMKN": "metals", "PLZL": "metals",
}
GROUP_CORR: dict[str, float] = {"banks": 0.6, "oil_gas": 0.6, "metals": 0.5}
CROSS_GROUP_CORR = 0.3          # между секторами, для телекома (MTSS) и неизвестных тикеров

UNKNOWN_PRICE_RANGE = (50.0, 2000.0)   # PLAN §6: неизвестный тикер стартует со случайной цены в этом диапазоне
```

### 8.2 Изменения в `simulator.py`

```python
# было: DEFAULT_DT = 0.5 / (252 * 6.5 * 3600) — константа, не связанная с update_interval (MARKET_SIMULATOR §6)
# стало: у MOEX основная сессия ~09:50–18:50 = 9 ч, ~250 торговых дней
TRADING_SECONDS_PER_YEAR = 250 * 9 * 3600


class GBMSimulator:
    def __init__(self, tickers, update_interval: float = 0.5, time_scale: float = 1.0,
                 event_probability: float = 0.001) -> None:
        # dt выводится из интервала тика -> одна ручка вместо двух рассинхронизируемых.
        # time_scale > 1 «ускоряет рынок» для демо (реальный 500-мс тик даёт движение <0.01 ₽/тик).
        self._dt = update_interval * time_scale / TRADING_SECONDS_PER_YEAR
        ...

    def _add_ticker_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = SEED_PRICES.get(ticker) or random.uniform(*UNKNOWN_PRICE_RANGE)
        self._params[ticker] = TICKER_PARAMS.get(ticker, dict(DEFAULT_PARAMS))

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        g1, g2 = TICKER_GROUP.get(t1), TICKER_GROUP.get(t2)
        if g1 is not None and g1 == g2:
            return GROUP_CORR[g1]
        return CROSS_GROUP_CORR


class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache, update_interval=0.5, time_scale=1.0, event_probability=0.001): ...

    def status(self) -> SourceStatus:
        return SourceStatus(source="simulator", healthy=True, delay_seconds=0,
                            last_success=time.time())
```

`time_scale` — не env-переменная (PLAN §5 её не содержит): параметр конструктора, по умолчанию 1.0.
Тик по-прежнему округляется до копеек; внутреннее состояние `_prices` не округляется, поэтому квантование
цены не накапливает смещение (просто часть тиков «flat»).

Правки тестов симулятора: тикеры и группы (`AAPL` → `SBER` и т.д.), корреляция «два банка» / «две нефтегазовые»
≈ 0.6, `dt == update_interval / TRADING_SECONDS_PER_YEAR`.

---

## 9. Фабрики и согласование тикеров

```python
# factory.py
"""Выбор источника по MOEX_ENABLED (PLAN §5)."""
import logging
import os

from .cache import PriceCache
from .history import CandleService, MoexCandleProvider, SyntheticCandleProvider
from .interface import MarketDataSource
from .moex_client import MoexDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def moex_enabled() -> bool:
    """Только "true" (без учёта регистра/пробелов) включает MOEX; пусто/отсутствует/что угодно ещё — симулятор."""
    return os.environ.get("MOEX_ENABLED", "").strip().lower() == "true"


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    if moex_enabled():
        logger.info("Market data source: MOEX ISS (real prices, ~15 min delayed)")
        return MoexDataSource(price_cache)
    logger.info("Market data source: GBM simulator")
    return SimulatorDataSource(price_cache)


def create_candle_service(price_cache: PriceCache, source: MarketDataSource) -> CandleService:
    """Свечи из ISS, если работает MoexDataSource (использует его HTTP-клиент); иначе синтетические."""
    if isinstance(source, MoexDataSource):
        return CandleService(MoexCandleProvider(source.client))
    return CandleService(SyntheticCandleProvider(price_cache))
```

Публичный импорт-пакет (`__init__.py`) дополняется:
`from app.market import PriceCache, PriceUpdate, MarketDataSource, create_market_data_source, create_candle_service, create_stream_router, create_market_router, normalize_ticker, sync_tracking`.

**Отслеживаемые тикеры** (PLAN §6): при старте `source.start(watchlist ∪ positions)`; дальше любые изменения
вотчлиста/позиций проходят через `sync_tracking()` (§3.3) — в том числе автодобавление при сделке по
неотслеживаемому тикеру и закрытие позиции.

---

## 10. История: свечи

Нужна для: основного графика (стартовая история вместо «пустого» графика после загрузки страницы), аналитики
(§11) и LLM. Живые цены остаются в кэше — история их не заменяет.

### 10.1 Провайдеры и `CandleService`

```python
"""Исторические свечи: провайдеры (MOEX / синтетический) и кэширующий сервис."""
from __future__ import annotations

import asyncio
import math
import time
import zlib
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

import numpy as np

from .cache import PriceCache
from .errors import NoDataError, UpstreamError
from .iss import MoexClient
from .models import Candle, Interval
from .seed_prices import DEFAULT_PARAMS, SEED_PRICES, TICKER_PARAMS
from .simulator import TRADING_SECONDS_PER_YEAR

# Максимум глубины по интервалу (в календарных днях): защита от огромных выборок.
MAX_DAYS = {Interval.M1: 5, Interval.M10: 30, Interval.H1: 180, Interval.D1: 730}
# Сколько живёт кэш свечей (MOEX всё равно на 15 мин позади).
TTL_SECONDS = {Interval.M1: 60.0, Interval.M10: 120.0, Interval.H1: 300.0, Interval.D1: 900.0}
BENCHMARK = "IMOEX"


class CandleProvider(Protocol):
    async def candles(self, ticker: str, interval: Interval, start: date, end: date) -> list[Candle]: ...
    async def index_candles(self, interval: Interval, start: date, end: date) -> list[Candle]: ...


class MoexCandleProvider:
    def __init__(self, client: MoexClient) -> None:
        self._client = client

    async def candles(self, ticker, interval, start, end):
        return await self._client.get_candles(ticker, interval, start, end)

    async def index_candles(self, interval, start, end):
        return await self._client.get_index_candles(BENCHMARK, interval, start, end)


class CandleService:
    """TTL-кэш + дедупликация параллельных запросов + «stale-if-error»."""

    def __init__(self, provider: CandleProvider) -> None:
        self._provider = provider
        self._cache: dict[tuple, tuple[float, list[Candle]]] = {}      # key -> (expires, candles)
        self._locks: defaultdict[tuple, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def get(self, ticker: str, interval: Interval, days: int) -> list[Candle]:
        """Свечи за последние `days` календарных дней. NoDataError — если ISS ничего не знает о тикере."""
        days = min(days, MAX_DAYS[interval])
        return await self._cached((ticker, interval, days), lambda s, e: self._provider.candles(ticker, interval, s, e), interval, days, required=True)

    async def benchmark(self, days: int) -> list[Candle]:
        """Дневные свечи IMOEX; при любой проблеме — [] (бенчмарк опционален)."""
        try:
            return await self._cached((BENCHMARK, Interval.D1, days), lambda s, e: self._provider.index_candles(Interval.D1, s, e), Interval.D1, days, required=False)
        except (UpstreamError, NoDataError):
            return []

    async def _cached(self, key, fetch, interval, days, *, required) -> list[Candle]:
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        async with self._locks[key]:                                   # одинаковые запросы ждут первый
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            end = date.today()
            try:
                data = await fetch(end - timedelta(days=days), end)
            except UpstreamError:
                if hit:                                                # ISS лёг — отдаём устаревшее
                    return hit[1]
                raise
            if not data and required:
                raise NoDataError(f"No market data for {key[0]}")
            self._cache[key] = (time.monotonic() + TTL_SECONDS[interval], data)
            return data
```

Границы запроса — календарные: `from = today − days`, `till = today`. Пустой ответ ISS для тикера = неизвестный
инструмент → `NoDataError` (в кэш не кладём).

### 10.2 Синтетические свечи (режим симулятора)

Без сети история всё равно нужна: график не должен быть пустым, а аналитика — работать. Генерируем
геометрическое броуновское движение **назад от текущей цены**: путь детерминирован тикером (`crc32` как seed),
последняя цена совпадает с текущей ценой из кэша — история «стыкуется» с живым потоком.

```python
BAR_SECONDS = {Interval.M1: 60, Interval.M10: 600, Interval.H1: 3600, Interval.D1: 9 * 3600}
BARS_PER_DAY = {Interval.M1: 540, Interval.M10: 54, Interval.H1: 9, Interval.D1: 1}
SUBSTEPS = 8   # под-шагов на свечу, чтобы получить честные high/low


class SyntheticCandleProvider:
    def __init__(self, cache: PriceCache) -> None:
        self._cache = cache

    async def candles(self, ticker, interval, start, end):
        trading_days = max(1, round((end - start).days * 5 / 7))
        n = trading_days * BARS_PER_DAY[interval]
        params = TICKER_PARAMS.get(ticker, DEFAULT_PARAMS)
        sigma, mu = params["sigma"], params["mu"]
        step = BAR_SECONDS[interval] / TRADING_SECONDS_PER_YEAR / SUBSTEPS

        rng = np.random.default_rng(zlib.crc32(ticker.encode()))
        z = rng.standard_normal((n, SUBSTEPS))
        log_path = np.cumsum(((mu - 0.5 * sigma**2) * step + sigma * math.sqrt(step) * z).ravel())
        anchor = self._cache.get_price(ticker) or SEED_PRICES.get(ticker) or 100.0
        prices = (anchor * np.exp(log_path - log_path[-1])).reshape(n, SUBSTEPS)   # последняя точка = anchor

        opens = np.concatenate(([prices[0, 0]], prices[:-1, -1]))
        closes = prices[:, -1]
        highs = np.maximum(prices.max(axis=1), opens)
        lows = np.minimum(prices.min(axis=1), opens)
        volumes = rng.integers(10_000, 500_000, size=n)

        times = self._bar_times(interval, n)
        return [Candle(t, round(o, 4), round(h, 4), round(l, 4), round(c, 4), float(v))
                for t, o, h, l, c, v in zip(times, opens, highs, lows, closes, volumes)]

    async def index_candles(self, interval, start, end):
        return []          # бенчмарка в режиме симулятора нет -> beta = None

    @staticmethod
    def _bar_times(interval: Interval, n: int) -> list[int]:
        """n меток времени по возрастанию, последняя — «сейчас». Дневные пропускают выходные."""
        if interval.is_daily:
            day, out = datetime.now(timezone.utc).date(), []
            while len(out) < n:
                if day.weekday() < 5:
                    out.append(int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp()))
                day -= timedelta(days=1)
            return out[::-1]
        step = BAR_SECONDS[interval]
        last = int(time.time()) // step * step
        return [last - (n - 1 - i) * step for i in range(n)]
```

Ограничения (документируем, не чиним): внутридневные синтетические свечи идут подряд, без ночных пауз; история
«сдвигается» вместе с текущей ценой (форма пути фиксирована, масштаб — нет).

---

## 11. Аналитика

Файл `analytics.py` — **чистые функции над `numpy`**, без I/O и без знаний о FastAPI. Используются:

- REST-эндпоинтами `/api/market/analytics|correlations` (§12),
- LLM-контекстом (компактные строки; §11.4),
- модулем портфеля (`portfolio_risk`, §11.3) — концентрация и риск текущих позиций.

### 11.1 Индикаторы

```python
"""Аналитика рыночных данных. Чистые функции на numpy."""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .errors import NoDataError
from .models import Candle

TRADING_DAYS = 250        # торговых дней в году на MOEX (для годовой волатильности)
BENCH = "IMOEX"           # ключ бенчмарка при выравнивании рядов


def log_returns(x: np.ndarray) -> np.ndarray:
    return np.diff(np.log(x))


def sma(x: np.ndarray, n: int) -> np.ndarray:
    """Простая скользящая средняя. Первые n-1 значений — NaN."""
    out = np.full(len(x), np.nan)
    if n <= 0 or len(x) < n:
        return out
    c = np.cumsum(np.insert(x, 0, 0.0))
    out[n - 1 :] = (c[n:] - c[:-n]) / n
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """Экспоненциальная средняя, затравка — SMA первых n значений."""
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
    """RSI по Уайлдеру. Первое значение — на индексе n."""
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
    """(нижняя, средняя, верхняя) полосы Боллинджера; std — выборочное по окну."""
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
    """Максимальная просадка от пика, доля ≤ 0 (−0.12 = −12 %)."""
    peak = np.maximum.accumulate(x)
    return float((x / peak - 1.0).min())


def beta(asset_returns: np.ndarray, bench_returns: np.ndarray) -> float | None:
    if len(asset_returns) < 2 or len(asset_returns) != len(bench_returns):
        return None
    var = np.var(bench_returns, ddof=1)
    return None if var == 0 else float(np.cov(asset_returns, bench_returns)[0, 1] / var)
```

### 11.2 Выравнивание рядов и корреляции

Свечи разных тикеров могут не совпадать по датам (не торговались, приостановка) — считаем только по
**пересечению меток времени**.

```python
def align_closes(series: dict[str, list[Candle]]) -> tuple[list[str], np.ndarray]:
    """{ticker: свечи} -> (тикеры, матрица цен закрытия [наблюдения x тикеры]) по общим меткам времени."""
    names = [t for t, c in series.items() if c]
    if not names:
        return [], np.empty((0, 0))
    common = set.intersection(*(set(c.time for c in series[t]) for t in names))
    times = sorted(common)
    by_time = {t: {c.time: c.close for c in series[t]} for t in names}
    return names, np.array([[by_time[t][ts] for t in names] for ts in times], dtype=float)


def correlation_matrix(closes: np.ndarray) -> np.ndarray:
    """Корреляции лог-доходностей; closes — [наблюдения x тикеры]. Нужно ≥ 3 наблюдений цены."""
    if closes.ndim != 2 or closes.shape[0] < 3 or closes.shape[1] < 2:
        raise NoDataError("Not enough overlapping history to compute correlations")
    returns = np.diff(np.log(closes), axis=0)
    corr = np.corrcoef(returns, rowvar=False)
    return np.nan_to_num(corr, nan=0.0)          # постоянный ряд (нулевая дисперсия) -> 0
```

### 11.3 Аналитика по тикеру и по портфелю

```python
def _f(x, nd: int = 4) -> float | None:
    """NaN/None -> None (JSONResponse не сериализует NaN), иначе округление."""
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
    volatility_annual: float | None      # доля: 0.28 = 28 % годовых
    max_drawdown: float                  # доля ≤ 0
    return_1d: float | None
    return_5d: float | None
    return_20d: float | None
    beta_imoex: float | None
    trend: str                           # "up" | "down" | "sideways"

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker, "bars": self.bars, "last": self.last,
            "sma20": _f(self.sma20), "sma50": _f(self.sma50), "ema12": _f(self.ema12),
            "rsi14": _f(self.rsi14, 2), "bollinger": {k: _f(v) for k, v in self.bollinger.items()},
            "volatility_annual": _f(self.volatility_annual), "max_drawdown": _f(self.max_drawdown),
            "return_1d": _f(self.return_1d), "return_5d": _f(self.return_5d),
            "return_20d": _f(self.return_20d), "beta_imoex": _f(self.beta_imoex, 2), "trend": self.trend,
        }


def _last(a: np.ndarray) -> float | None:
    return None if len(a) == 0 or math.isnan(a[-1]) else float(a[-1])


def analyze(ticker: str, candles: list[Candle], benchmark: list[Candle] | None = None) -> TickerAnalytics:
    """Дневные свечи -> набор индикаторов. Нужно минимум 2 свечи; короткая история даёт None в длинных индикаторах."""
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
        ticker=ticker, bars=len(close), last=float(close[-1]),
        sma20=s20, sma50=s50, ema12=_last(ema(close, 12)), rsi14=_last(rsi(close, 14)),
        bollinger={"lower": _last(lo), "middle": _last(mid), "upper": _last(hi)},
        volatility_annual=annualized_volatility(log_returns(close)),
        max_drawdown=max_drawdown(close),
        return_1d=_ret(close, 1), return_5d=_ret(close, 5), return_20d=_ret(close, 20),
        beta_imoex=b, trend=trend,
    )



def portfolio_risk(weights: dict[str, float], closes: np.ndarray, tickers: list[str]) -> dict:
    """Концентрация и риск портфеля.

    weights — рыночная стоимость позиций по тикерам (₽, любая шкала: нормируется внутри);
    closes  — [наблюдения x тикеры] в порядке `tickers` (из align_closes).
    Возвращает: HHI, «эффективное число позиций», годовую волатильность портфеля, вклад каждой бумаги в риск.
    """
    w = np.array([weights.get(t, 0.0) for t in tickers], dtype=float)
    total = w.sum()
    if total <= 0:
        raise NoDataError("Empty portfolio")
    w /= total
    hhi = float(np.sum(w**2))
    out = {"weights": {t: round(float(x), 4) for t, x in zip(tickers, w)},
           "hhi": round(hhi, 4), "effective_positions": round(1.0 / hhi, 2),
           "volatility_annual": None, "risk_contribution": None}

    if closes.shape[0] >= 3:
        r = np.diff(np.log(closes), axis=0)
        cov = np.atleast_2d(np.cov(r, rowvar=False)) * TRADING_DAYS
        var = float(w @ cov @ w)
        if var > 0:
            out["volatility_annual"] = round(math.sqrt(var), 4)
            contrib = w * (cov @ w) / var                 # доли, сумма = 1
            out["risk_contribution"] = {t: round(float(x), 4) for t, x in zip(tickers, contrib)}
    return out
```

### 11.4 Живая аналитика из кэша и контекст для LLM (`sessions.py`)

Аналитика по тикам, накопленным в `PriceCache` с момента старта сервера, — без запросов к ISS.

```python
"""Торговые часы и «сессионная» статистика по тикам кэша."""
from __future__ import annotations

from datetime import datetime, time

from .cache import PriceCache
from .iss import MSK

# Основная сессия акций MOEX, приблизительно. Праздники не учитываются -> это подсказка для UI, не факт.
MAIN_SESSION = (time(9, 50), time(18, 50))


def is_main_session(now: datetime | None = None) -> bool:
    now = (now or datetime.now(MSK)).astimezone(MSK)
    return now.weekday() < 5 and MAIN_SESSION[0] <= now.time() < MAIN_SESSION[1]


def session_stats(history: list[tuple[float, float]]) -> dict | None:
    """Статистика по тикам с момента старта сервера: первая/последняя/макс/мин цена и изменение."""
    if not history:
        return None
    prices = [p for _, p in history]
    first, last = prices[0], prices[-1]
    return {
        "first_price": first, "last_price": last, "high": max(prices), "low": min(prices),
        "change": round(last - first, 4),
        "change_percent": round((last - first) / first * 100, 4) if first else 0.0,
        "ticks": len(prices), "since": history[0][0],
    }


def market_context_lines(cache: PriceCache, tickers: list[str]) -> list[str]:
    """Компактные строки для LLM-контекста (PLAN §9, шаг 1): «SBER ₽273.50 (+0.12% с начала сессии)»."""
    lines = []
    for t in tickers:
        update = cache.get(t)
        if update is None:
            lines.append(f"{t} — (цены пока нет)")
            continue
        stats = session_stats(cache.get_history(t))
        pct = f" ({stats['change_percent']:+.2f}% с запуска)" if stats else ""
        lines.append(f"{t} ₽{update.price:,.2f}{pct}")
    return lines
```

---

## 12. REST API рыночных данных

PLAN §8 в разделе Market Data содержит только SSE. Ниже — **дополнительные** read-only эндпоинты под префиксом
`/api/market` (PLAN.md надо дополнить — §16). SSE `GET /api/stream/prices` не меняется.

| Метод | Путь | Описание |
|---|---|---|
| GET | `/api/market/status` | источник, задержка, здоровье, отслеживаемые тикеры, идёт ли основная сессия |
| GET | `/api/market/quotes` | последние цены всех отслеживаемых тикеров (то же, что в SSE, но разово) |
| GET | `/api/market/quotes/{ticker}` | цена + статистика с запуска |
| GET | `/api/market/instruments/{ticker}` | название, лот, точность, закрытие вчера |
| GET | `/api/market/history/{ticker}?interval=1d&days=90` | свечи для графика |
| GET | `/api/market/analytics/{ticker}?days=180` | индикаторы, волатильность, просадка, бета |
| GET | `/api/market/correlations?tickers=SBER,GAZP&days=120` | матрица корреляций |

Коды: `400` — невалидный тикер / параметры бизнес-правил; `404` — нет данных по тикеру; `503` — ISS недоступен
и в кэше нет устаревших данных; `422` — некорректный тип/диапазон query-параметра (стандартный FastAPI).
Тело ошибки — `{"detail": "..."}` (PLAN §8). Денежные поля — числа в рублях. Все эндпоинты доступны для
**любого** валидного тикера, а не только отслеживаемого, и **не** добавляют его в вотчлист.

### 12.1 Примеры ответов

`GET /api/market/status`
```json
{"source": "moex", "healthy": true, "delay_seconds": 900, "last_success": 1790000000.1,
 "last_error": null, "consecutive_failures": 0, "market_open_hint": true,
 "tracked": ["SBER", "GAZP", "LKOH"]}
```

`GET /api/market/quotes/SBER`
```json
{"ticker": "SBER", "price": 273.5, "previous_price": 273.32, "timestamp": 1790000000.123,
 "change": 0.18, "change_percent": 0.0658, "direction": "up",
 "session": {"first_price": 272.9, "last_price": 273.5, "high": 273.9, "low": 272.4,
             "change": 0.6, "change_percent": 0.2199, "ticks": 84, "since": 1789999000.0}}
```

`GET /api/market/history/SBER?interval=1d&days=30`
```json
{"ticker": "SBER", "interval": "1d",
 "candles": [{"time": 1788220800, "open": 276.03, "high": 277.77, "low": 271.06, "close": 272.21, "volume": 26826095}]}
```

`GET /api/market/analytics/SBER`
```json
{"ticker": "SBER", "bars": 120, "last": 273.5, "sma20": 270.12, "sma50": 266.4, "ema12": 271.9,
 "rsi14": 58.3, "bollinger": {"lower": 262.1, "middle": 270.12, "upper": 278.1},
 "volatility_annual": 0.2734, "max_drawdown": -0.1187, "return_1d": 0.0066, "return_5d": 0.0121,
 "return_20d": 0.0348, "beta_imoex": 1.04, "trend": "up"}
```

`GET /api/market/correlations?tickers=SBER,GAZP,LKOH`
```json
{"tickers": ["SBER", "GAZP", "LKOH"], "observations": 119,
 "matrix": [[1.0, 0.62, 0.58], [0.62, 1.0, 0.71], [0.58, 0.71, 1.0]]}
```

Доходности и волатильность — **доли** (0.0348 = 3.48 %), `rsi14` — 0…100. Фронтенд форматирует сам.

### 12.2 `api.py`

```python
"""Read-only REST API рыночных данных."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from .analytics import align_closes, analyze, correlation_matrix
from .cache import PriceCache
from .errors import InvalidTickerError, NoDataError, UpstreamError
from .history import MAX_DAYS, CandleService
from .interface import MarketDataSource
from .models import Candle, Interval
from .sessions import is_main_session, session_stats
from .tickers import normalize_ticker

MAX_CORR_TICKERS = 15


def create_market_router(
    cache: PriceCache, source: MarketDataSource, candles: CandleService, instruments
) -> APIRouter:
    """Фабрика (а не глобальный router) — как create_stream_router: каждая сборка приложения получает свой кэш."""
    router = APIRouter(prefix="/api/market", tags=["market"])

    def ticker_or_400(raw: str) -> str:
        try:
            return normalize_ticker(raw)
        except InvalidTickerError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    async def load(ticker: str, interval: Interval, days: int) -> list[Candle]:
        if days > MAX_DAYS[interval]:
            raise HTTPException(400, f"days for interval {interval.value} must be <= {MAX_DAYS[interval]}")
        try:
            return await candles.get(ticker, interval, days)
        except NoDataError as e:
            raise HTTPException(404, f"No market data for {ticker}") from e
        except UpstreamError as e:
            raise HTTPException(503, "MOEX ISS is temporarily unavailable") from e

    @router.get("/status")
    async def status() -> dict:
        return {**source.status().to_dict(), "market_open_hint": is_main_session(),
                "tracked": source.get_tickers()}

    @router.get("/quotes")
    async def quotes() -> dict:
        return {t: u.to_dict() for t, u in cache.get_all().items()}

    @router.get("/quotes/{ticker}")
    async def quote(ticker: str) -> dict:
        t = ticker_or_400(ticker)
        update = cache.get(t)
        if update is None:
            raise HTTPException(404, f"No price available for {t} yet")
        return {**update.to_dict(), "session": session_stats(cache.get_history(t))}

    @router.get("/instruments/{ticker}")
    async def instrument(ticker: str) -> dict:
        t = ticker_or_400(ticker)
        try:
            info = await instruments(t)
        except UpstreamError as e:
            raise HTTPException(503, "MOEX ISS is temporarily unavailable") from e
        if info is None:
            raise HTTPException(404, f"Unknown instrument {t}")
        return info.to_dict()

    @router.get("/history/{ticker}")
    async def history(ticker: str, interval: Interval = Interval.D1, days: int = Query(90, ge=1, le=730)) -> dict:
        t = ticker_or_400(ticker)
        bars = await load(t, interval, days)
        return {"ticker": t, "interval": interval.value, "candles": [c.to_dict() for c in bars]}

    @router.get("/analytics/{ticker}")
    async def ticker_analytics(ticker: str, days: int = Query(180, ge=30, le=730)) -> dict:
        t = ticker_or_400(ticker)
        bars = await load(t, Interval.D1, days)
        bench = await candles.benchmark(days)
        try:
            return analyze(t, bars, bench).to_dict()
        except NoDataError as e:
            raise HTTPException(404, str(e)) from e

    @router.get("/correlations")
    async def correlations(tickers: str | None = None, days: int = Query(120, ge=30, le=730)) -> dict:
        raw = tickers.split(",") if tickers else source.get_tickers()
        names = list(dict.fromkeys(ticker_or_400(x) for x in raw))[:MAX_CORR_TICKERS]
        if len(names) < 2:
            raise HTTPException(400, "Provide at least 2 tickers")
        series = await asyncio.gather(*(load(t, Interval.D1, days) for t in names))
        used, closes = align_closes(dict(zip(names, series)))
        try:
            matrix = correlation_matrix(closes)
        except NoDataError as e:
            raise HTTPException(404, str(e)) from e
        return {"tickers": used, "observations": int(closes.shape[0]) - 1,
                "matrix": [[round(float(v), 4) for v in row] for row in matrix]}

    return router
```

`instruments` — async-функция `ticker -> InstrumentInfo | None`, собирается в `create_app` (§13): для MOEX —
`client.get_instruments([t])` с небольшим `dict`-кэшем (справочник почти не меняется), для симулятора — словарь
названий по умолчанию (`{"SBER": "Сбербанк", "GAZP": "Газпром", "LKOH": "Лукойл", ...}`, остальное — сам
тикер как имя).

---

## 13. Интеграция в FastAPI

Кэш и источник **создаются в `create_app()`** (роутерам они нужны при регистрации), а **запускаются в
lifespan** в порядке PLAN §7.

```python
# backend/app/main.py (фрагмент)
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.market import (PriceCache, create_candle_service, create_market_data_source,
                        create_market_router, create_stream_router)


def create_app() -> FastAPI:
    cache = PriceCache()
    source = create_market_data_source(cache)
    candles = create_candle_service(cache, source)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db()                                                   # 1. схема + сид
        tracked = load_watchlist() | load_position_tickers()        # 2. watchlist ∪ positions
        await source.start(sorted(tracked))                         # 3. MOEX: один блокирующий опрос
        record_portfolio_snapshot(cache)                            # 4. стартовый снапшот (цены уже есть)
        snapshot_task = asyncio.create_task(snapshot_loop(cache))   # 5. раз в 30 с
        yield
        snapshot_task.cancel()
        await source.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.price_cache, app.state.market_source = cache, source   # для trade/chat-сервисов

    app.include_router(create_stream_router(cache))                          # GET /api/stream/prices
    app.include_router(create_market_router(cache, source, candles, instrument_lookup))
    # ... остальные /api-роутеры (portfolio, watchlist, chat, health) ...
    app.mount("/", StaticFiles(directory="static", html=True), name="static")   # строго после /api
    return app
```

**Как потребляют рыночные данные соседние модули:**

```python
# trade-сервис: цена для исполнения и проверка «есть ли цена» (PLAN §8, шаги 4–6)
await sync_tracking(source, ticker, in_watchlist=True, has_position=False)   # авто-добавление, если не отслеживается
price = cache.get_price(ticker)
if price is None:
    raise HTTPException(400, f"No price available for {ticker} yet, try again shortly")

# оценка позиции без цены — по avg_cost (PLAN §8): p = cache.get_price(t) ; value = qty * (p if p is not None else avg_cost)

# LLM-контекст (PLAN §9, шаг 1)
lines = market_context_lines(cache, watchlist_tickers)
```

При MOEX цена нового валидного тикера появляется **в рамках самого `add_ticker`** (§7), поэтому типичная сделка
по неотслеживаемому тикеру проходит с первого раза; ошибка «try again shortly» остаётся только для невалидных
символов и сбоев ISS.

---

## 14. Тесты

Стек — как сейчас: `pytest`, `pytest-asyncio` (`asyncio_mode = "auto"`), `httpx`. Сеть в юнит-тестах **не
используется**: ISS подменяется `httpx.MockTransport` и записанными JSON-фикстурами.

| Файл | Что проверяет |
|---|---|
| `test_iss.py` | `parse_block`; `extract_quotes` (LAST, фолбэк-цепочка, неизвестный тикер = пусто, `delay_seconds ≈ 900`, переход через полночь); `extract_candles` (МСК→UTC, дневные = полночь UTC); `MoexClient`: ретраи на 503/429, 4xx без ретраев, битый JSON → `UpstreamError`, пагинация свечей |
| `test_moex_source.py` | `start` блокирующе наполняет кэш; невалидный тикер не попадает в кэш; `add_ticker` сразу даёт цену; `remove_ticker` во время запроса не воскрешает тикер (гонка H3); сбой ISS → задача жива, `status().healthy` падает, backoff растёт |
| `test_history.py` | TTL-кэш и дедупликация параллельных запросов; stale-if-error; `NoDataError` для пустого ответа; синтетика: детерминизм, последняя цена = цена кэша, `low ≤ open,close ≤ high`, выходные пропущены |
| `test_analytics.py` | эталонные значения SMA/EMA/RSI; постоянный ряд; `max_drawdown`; корреляция ряда с самим собой = 1; `portfolio_risk` (сумма вкладов = 1); NaN → `None` в `to_dict` |
| `test_api.py` | коды и формы ответов `/api/market/*` (в т.ч. 400 для `BRK.B`, 404, 503); ни один эндпоинт не добавляет тикер в вотчлист |
| `test_contract.py` | один параметризованный набор на оба источника (`Simulator`, `Moex` с `MockTransport`): `start → add → remove → get_tickers → stop`; идемпотентность, повторный `stop` |
| `test_cache.py` / `test_simulator.py` | обновить: округление до 4 знаков, история тиков, тикеры и группы MOEX, `dt` |

Фрагменты:

```python
# tests/market/fixtures/moex_board.json — форма записанного ответа (создать реальным curl из MOEX_API.md §2)
# {"marketdata": {"columns": ["SECID","LAST","TIME","SYSTIME"], "data": [["SBER",273.28,"08:25:11","2026-09-29 08:40:11"], ["ZZZZ",null,null,null]]},
#  "securities": {"columns": ["SECID","SHORTNAME","PREVPRICE"], "data": [["SBER","Сбербанк",272.9], ["ZZZZ","Тест",100.5]]}}

def test_extract_quotes_fallback_to_prevprice():
    payload = json.loads((FIXTURES / "moex_board.json").read_text())
    quotes = extract_quotes(payload)
    assert quotes["SBER"].price == 273.28 and quotes["SBER"].price_field == "LAST"
    assert quotes["SBER"].delay_seconds == 900
    assert quotes["ZZZZ"].price == 100.5 and quotes["ZZZZ"].price_field == "PREVPRICE"   # валиден, но не торговался
    assert "NOPE" not in quotes


def test_extract_quotes_unknown_ticker_is_empty():
    assert extract_quotes({"marketdata": {"columns": ["SECID", "LAST"], "data": []},
                           "securities": {"columns": ["SECID"], "data": []}}) == {}
```

```python
# MoexClient на MockTransport: ретраи
async def test_client_retries_on_503_then_succeeds():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"marketdata": {"columns": ["SECID", "LAST"], "data": [["SBER", 273.5]]},
                                         "securities": {"columns": ["SECID"], "data": [["SBER"]]}})

    client = MoexClient(transport=httpx.MockTransport(handler), retries=2)
    assert (await client.get_quotes(["SBER"]))["SBER"].price == 273.5
    assert calls["n"] == 2
    await client.aclose()


async def test_source_survives_upstream_failure():
    cache = PriceCache()
    client = MoexClient(transport=httpx.MockTransport(lambda r: httpx.Response(500)), retries=0)
    source = MoexDataSource(cache, client=client, poll_interval=0.01)
    await source.start(["SBER"])                       # не бросает
    assert cache.get_price("SBER") is None and source.status().healthy is False
    await source.stop()
```

```python
# analytics: эталонные значения
def test_sma_and_ema():
    x = np.arange(1.0, 11.0)                           # 1..10
    assert sma(x, 3)[-1] == pytest.approx(9.0)
    assert np.isnan(sma(x, 3)[1])
    assert ema(x, 3)[2] == pytest.approx(2.0)          # затравка = SMA первых трёх

def test_rsi_bounds_and_monotonic():
    assert rsi(np.arange(1.0, 40.0), 14)[-1] == 100.0  # только рост
    assert rsi(np.arange(40.0, 1.0, -1.0), 14)[-1] == pytest.approx(0.0)
    assert rsi(np.full(30, 5.0), 14)[-1] == 50.0       # константа

def test_max_drawdown():
    assert max_drawdown(np.array([100.0, 120.0, 90.0, 110.0])) == pytest.approx(-0.25)

def test_portfolio_risk_contributions_sum_to_one():
    rng = np.random.default_rng(1)
    closes = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, (60, 3)), axis=0))
    res = portfolio_risk({"A": 1000, "B": 500, "C": 500}, closes, ["A", "B", "C"])
    assert sum(res["risk_contribution"].values()) == pytest.approx(1.0, abs=1e-3)
    assert res["effective_positions"] < 3

def test_to_dict_has_no_nan():
    a = analyze("X", [Candle(i, 10, 11, 9, 10 + i % 3, 1) for i in range(1, 21)])   # 20 баров: SMA50 ещё нет
    d = a.to_dict()
    assert d["sma50"] is None and d["rsi14"] is not None
    json.dumps(d, allow_nan=False)                     # не бросает
```

```python
# API: TestClient + подставной CandleService
def test_history_400_for_bad_ticker(client):
    assert client.get("/api/market/history/BRK.B").status_code == 400

def test_history_404_for_unknown(client_with_empty_provider):
    assert client_with_empty_provider.get("/api/market/history/ZZZZZ").status_code == 404
```

Опциональный «живой» тест против настоящего ISS: `@pytest.mark.live`, пропускается, если не задан
`MOEX_LIVE=1` (не запускается в CI). E2E (Playwright, PLAN §12) идут на симуляторе; SSE-сценарии не меняются.

---

## 15. Конфигурация, зависимости, порядок работ

### Конфигурация

Новых переменных окружения **нет** — только те, что в PLAN §5 (`MOEX_ENABLED`, `LLM_MOCK`, `OPENROUTER_API_KEY`,
`DB_PATH`). Всё остальное — параметры конструкторов с разумными дефолтами:

| Параметр | Значение по умолчанию | Где |
|---|---|---|
| `poll_interval` | 15 с | `MoexDataSource` |
| `batch_size` | 50 тикеров/запрос | `MoexDataSource` |
| backoff | `min(15·2ⁿ, 120)` с | `MoexDataSource._poll_loop` |
| `retries` / timeout ISS | 2 / 10 с | `MoexClient` |
| `update_interval`, `time_scale` | 0.5 с, 1.0 | `SimulatorDataSource` |
| `history_size` тиков на тикер | 3600 | `PriceCache` |
| TTL свечей | 1m: 60 с · 10m: 120 с · 1h: 300 с · 1d: 900 с | `history.TTL_SECONDS` |
| макс. глубина | 1m: 5 · 10m: 30 · 1h: 180 · 1d: 730 дней | `history.MAX_DAYS` |

### `pyproject.toml`

```toml
dependencies = [
    "fastapi>=0.115.0",
    "uvicorn[standard]>=0.32.0",
    "numpy>=2.0.0",
    "httpx>=0.27.0",          # NEW: клиент MOEX ISS (и TestClient/MockTransport в тестах)
]                             # убрать: massive; rich перенести в [dev] — нужен только market_data_demo.py
```

### Порядок реализации

1. **Чистка и данные**: удалить Massive; `errors.py`, `tickers.py`, новые `models.py`; `seed_prices.py` под MOEX;
   правки `simulator.py`, `cache.py`, `interface.py`, `factory.py`; обновить существующие тесты (73 → зелёные).
2. **ISS**: `iss.py` + фикстуры + `test_iss.py`; `moex_client.py` + `test_moex_source.py` + `test_contract.py`.
   Перед этим выполнить `curl`-чек-лист из §16 и сохранить реальные ответы в `fixtures/`.
3. **История и аналитика**: `history.py`, `analytics.py`, `sessions.py` + тесты (чистые функции — можно
   параллельно с шагом 2).
4. **API**: `api.py`, подключение в `create_app`, `test_api.py`.
5. **Документация**: обновить `MARKET_DATA_SUMMARY.md`, `MARKET_INTERFACE.md`, `MARKET_SIMULATOR.md`,
   `backend/CLAUDE.md`, `backend/README.md`, `market_data_demo.py` (тикеры MOEX); добавить §12 в PLAN.md (§16).

---

## 16. Непроверенное и отклонения от PLAN.md

### 16.1 Что не проверено вживую

При написании этого документа доступ к `iss.moex.com` из окружения был закрыт (прокси вернул 403), поэтому
**всё ниже взято из `MOEX_API.md` и документации ISS по памяти** и должно быть подтверждено `curl`-ом
на шаге 2 порядка работ. Код устроен так, что отсутствие колонки/пустая страница не ломает работу
(`dict.get`, цепочка фолбэков, `MAX_PAGES`).

| Допущение | Как проверить |
|---|---|
| Колонки `LCURRENTPRICE`, `LCLOSEPRICE` (marketdata) и `PREVPRICE`, `SHORTNAME`, `LOTSIZE`, `DECIMALS`, `MINSTEP` (securities) существуют на TQBR | `curl "https://iss.moex.com/iss/engines/stock/markets/shares/boards/TQBR/securities.json?securities=SBER&iss.meta=off&iss.only=marketdata,securities"` |
| Фильтр `securities=` работает и для блока `securities`, а не только `marketdata` | тот же запрос: в `securities.data` должна быть одна строка |
| `SYSTIME`/`TIME` — одного пояса (МСК); в `candles` `begin` — МСК | сравнить с текущим временем МСК |
| `LAST` в выходные/ночью: остаётся последней сделкой или `null` | запрос в субботу; при `null` работает фолбэк на `PREVPRICE` |
| Индекс IMOEX: путь `engines/stock/markets/index/boards/SNDX/securities/IMOEX/candles.json` | `curl` с `interval=24&from=2026-09-01`; иначе `beta_imoex` будет `null` (остальное не ломается) |
| Свечи отдаются страницами (ожидается до ~500 строк), пагинация — параметр `start` | `interval=1&from=<5 дней назад>`: проверить размер первой страницы и работу `start=` |
| Основная сессия ≈ 09:50–18:50 МСК, будни | справочно (`is_main_session` — только подсказка UI) |
| Стартовые цены в `seed_prices.py` (кроме SBER/GAZP/LKOH из PLAN) | сверить с `PREVPRICE` |

### 16.2 Отклонения от PLAN.md (нужно согласовать и внести в PLAN)

| # | Отклонение | Обоснование |
|---|---|---|
| 1 | **Новые эндпоинты `/api/market/*`** (§12); PLAN §8 знает только SSE | история для графика, аналитика, статус источника — «аналитика» в задаче; всё read-only и опционально для фронтенда |
| 2 | **`add_ticker` в MOEX сразу запрашивает цену** (PLAN §6/§8: «цена придёт при следующем опросе, до 15 с») | сделка по новому тикеру проходит с первой попытки; поведение для невалидных тикеров то же |
| 3 | **Фолбэк цены на закрытие/`PREVPRICE`** для валидных, но не торговавшихся бумаг | приложение не «пустое» вне сессии; невалидные тикеры по-прежнему без цены. Если нужна строгая семантика «только LAST» — убрать два последних элемента цепочки в `extract_quotes` |
| 4 | **Округление кэша до 4 знаков** вместо 2 | цены MOEX бывают с 4 знаками |
| 5 | `SourceStatus`/`status()`, история тиков в `PriceCache` | статус и «сессионная» статистика; обратно совместимо |
| 6 | `GBMSimulator` принимает `update_interval`/`time_scale` вместо `dt` | устраняет рассинхронизацию `dt` и интервала тика (MARKET_SIMULATOR §6) |

После согласования — добавить в PLAN.md §8 раздел «Market Data» с таблицей из §12 и убрать упоминание Massive из
документации `planning/` и `backend/`.
