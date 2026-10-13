"""Realtime-поток Binance Spot по WebSocket (этап 3, спека §7).

Одно соединение на инструмент (combined stream, стримы в URL): сбой одной
подписки не роняет остальные. Сообщения нормализуются в контракты этапа 1/2:
trade → QuoteTick (price_kind="trade", event_at=время СДЕЛКИ T, не E),
kline → Candle (closed по флагу источника k.x, source="binance_ws").

Константы соединения — официальная документация Binance Spot WebSocket
Streams (binance-spot-api-docs/web-socket-streams.md, сверено с актуальной
версией 2026-10):
- combined stream: ``{base}/stream?streams=<s1>/<s2>/...``; событие обёрнуто
  ``{"stream": "<name>", "data": <payload>}``; символы — lowercase;
- одно соединение живёт не более 24 часов («expect to be disconnected at
  the 24 hour mark») → плановая ротация раньше срока (ws_lifetime, 23 ч);
- сервер шлёт ping-фрейм каждые 20 с и отключает соединение, если pong не
  получен в течение минуты — библиотека websockets отвечает на ping
  автоматически (важно: текст «ping каждые 3 минуты» из старых копий
  документации устарел);
- лимиты: ≤5 входящих сообщений/с на соединение (ping/pong/JSON),
  ≤1024 стримов на соединение, ≤300 попыток подключения за 5 мин с IP;
- событие ``serverShutdown`` на стриме ``!serverShutdown`` — сервер
  запрашивает скорейшее переподключение;
- документированного idle-таймаута маркет-стримов НЕТ (kline-стрим шлёт
  обновления каждые ~2 с): ws_idle_timeout=120 с — консервативный
  клиентский детектор мёртвого соединения (проектный выбор, не из доков).

Дедуп при пересечении эпох (плановая ротация/переподключение): ключи
стабильны между эпохами — source="ws", source_seq=trade id; LRU этапа 2
держит 256 ключей на инструмент, пересечение глубже покрывается
монотонностью set_quote_if_fresh. Свечи дедуплицируются upsert'ом
insert_candles и курсором обработки data_sync:processed:*.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional

from websockets.asyncio.client import connect

from ..adapters.base import QuoteTick
from ..adapters.binance import INTERVAL_MAP
from ..models import Candle, Instrument, now_ms
from ..services.metrics import MetricsRegistry, get_registry

log = logging.getLogger(__name__)

# --- лимиты и жизненный цикл соединения (источники — в модульном docstring) ---
DEFAULT_BASE_URL = "wss://stream.binance.com:9443"
MAX_STREAMS_PER_CONN = 1024       # лимит Binance; сейчас соединение на инструмент (≤4 стримов)
SERVER_INBOUND_MSG_PER_S = 5      # мы почти ничего не шлём (подписки — в URL)
CONNECT_ATTEMPTS_PER_5MIN = 300   # лимит IP; backoff 1→60 с далёк от него
PING_INTERVAL_S = 180             # клиентский keepalive поверх серверных ping (20 с)
PING_TIMEOUT_S = 60               # как у сервера: минута ожидания pong

_INTERVAL_TO_TF = {v: k for k, v in INTERVAL_MAP.items()}
_VENUE = "binance"


def backoff_delays(
    base: float = 1.0,
    cap: float = 60.0,
    factor: float = 2.0,
    jitter: float = 0.2,
    rand: Callable[[], float] = random.random,
):
    """Экспоненциальный backoff с джиттером ±jitter: base, base*factor, ...,
    ограничено cap (джиттер применяется после потолка). rand() ∈ [0, 1)."""
    attempt = 0
    while True:
        delay = min(cap, base * (factor ** attempt))
        spread = delay * jitter
        yield delay - spread + rand() * 2 * spread
        attempt += 1


@dataclass
class _LinkState:
    """Состояние соединения одного инструмента. Здоровье соединения — отдельно
    от возраста последней сделки (неликвид может не давать сделок)."""

    instrument: Instrument
    streams: tuple[str, ...]
    tfs: tuple[str, ...]
    connected: bool = False
    task: Optional[asyncio.Task] = None
    last_message_mono: float = 0.0      # monotonic: idle-детектор
    last_valid_mono: float = 0.0        # monotonic: здоровье подписки
    last_message_at: Optional[int] = None       # wall ms — для status()
    last_valid_message_at: Optional[int] = None
    reconnects: int = 0
    last_error: Optional[str] = None


class BinanceRealtime:
    """Менеджер per-instrument WS-подключений Binance Spot.

    Колбэки: on_trade(ins, tick) и on_kline(ins, tf, candle) — async,
    вызываются из цикла чтения строго в порядке приёма (тяжёлую обработку
    колбэк ставит задачей сам); on_reconnect(ins, tfs) — sync/async,
    вызывается на каждом успешном (пере)подключении ДО приёма живых
    сообщений как авторитетных (AC10: догрузка разрыва по REST)."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        on_trade=None,       # async callable(Instrument, QuoteTick)
        on_kline=None,       # async callable(Instrument, str, Candle)
        on_reconnect=None,   # callable(Instrument, tuple[str, ...])
        idle_timeout: float = 120.0,
        lifetime: float = 82_800.0,      # 23 ч — ротация до 24-часового разрыва
        reconnect_base: float = 1.0,
        reconnect_cap: float = 60.0,
        metrics: Optional[MetricsRegistry] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.on_trade = on_trade
        self.on_kline = on_kline
        self.on_reconnect = on_reconnect
        self.idle_timeout = float(idle_timeout)
        self.lifetime = float(lifetime)
        self.reconnect_base = float(reconnect_base)
        self.reconnect_cap = float(reconnect_cap)
        self._metrics = metrics or get_registry()
        for name in ("ws_connections", "ws_reconnects", "ws_messages",
                     "ws_malformed", "clock_skew"):
            self._metrics.register(name, "counter", ("venue",))
        self._metrics.register("ws_connected", "gauge", ())
        self._metrics.register("ws_last_message_age_s", "gauge", ())
        self._links: dict[str, _LinkState] = {}   # ключ — symbol (uppercase)
        self._started = False
        self._stop_event = asyncio.Event()
        self._monitor_task: Optional[asyncio.Task] = None
        self._background: set[asyncio.Task] = set()

    # ---------- жизненный цикл ----------

    def start(self, instruments: list[Instrument], scan_tfs) -> None:
        """Запустить менеджер и текущий набор подписок (идемпотентно).
        Вызывать из работающего event loop."""
        if self._started:
            return
        self._started = True
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        self.update_subscriptions(instruments, scan_tfs)
        log.info("binance realtime: запущен (%d инструментов)", len(self._links))

    async def stop(self) -> None:
        """Отменить задачи и закрыть соединения (идемпотентно)."""
        if not self._started:
            return
        self._started = False
        self._stop_event.set()
        tasks = [link.task for link in self._links.values() if link.task]
        for task in tasks:
            task.cancel()
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            tasks.append(self._monitor_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        for link in self._links.values():
            link.connected = False
            link.task = None
        self._metrics.gauge("ws_connected", 0.0)
        log.info("binance realtime: остановлен")

    def update_subscriptions(self, instruments: list[Instrument], scan_tfs) -> None:
        """Согласовать желаемые подписки с фактическими без рестарта сервиса:
        новый/изменённый инструмент — своё соединение, снятый — остановка."""
        desired: dict[str, tuple[Instrument, tuple[str, ...], tuple[str, ...]]] = {}
        for ins in instruments:
            if ins.venue != _VENUE or not ins.enabled:
                continue
            symbol = ins.symbol.lower()
            streams = [f"{symbol}@trade"]
            tfs = []
            for tf in dict.fromkeys(("H1", *scan_tfs)):
                interval = INTERVAL_MAP.get(tf)
                if interval is None:
                    continue
                tfs.append(tf)
                streams.append(f"{symbol}@kline_{interval}")
            desired[ins.symbol] = (ins, tuple(streams), tuple(tfs))
        for symbol in list(self._links):
            if symbol not in desired:
                self._cancel_link(self._links.pop(symbol))
                log.info("binance realtime: подписка %s снята", symbol)
        for symbol, (ins, streams, tfs) in desired.items():
            if len(streams) > MAX_STREAMS_PER_CONN:
                # недостижимо при схеме «соединение на инструмент»; оставлено
                # как граница для будущего батчинга нескольких инструментов
                log.error("binance realtime: %d стримов > лимита %d, пропуск %s",
                          len(streams), MAX_STREAMS_PER_CONN, symbol)
                continue
            link = self._links.get(symbol)
            if (link is not None and link.streams == streams
                    and link.task is not None and not link.task.done()):
                continue
            if link is not None:
                self._cancel_link(link)
            link = _LinkState(instrument=ins, streams=streams, tfs=tfs)
            self._links[symbol] = link
            if self._started:
                link.task = asyncio.create_task(self._run_link(link))
                log.info("binance realtime: подписка %s → %s", symbol, streams)

    def _cancel_link(self, link: _LinkState) -> None:
        link.connected = False
        if link.task is not None and not link.task.done():
            link.task.cancel()
        link.task = None

    # ---------- здоровье и статус ----------

    def _healthy(self, link: _LinkState, now_mono: float) -> bool:
        return (
            link.connected
            and link.last_valid_mono > 0
            and now_mono - link.last_valid_mono <= 3 * self.idle_timeout
        )

    def is_healthy(self, instrument_id: int) -> bool:
        """True — подписка инструмента жива (соединение + свежие валидные
        сообщения): REST-фолбэк котировок не нужен."""
        if not self._started:
            return False
        now_mono = time.monotonic()
        return any(
            link.instrument.id == instrument_id and self._healthy(link, now_mono)
            for link in self._links.values()
        )

    def status(self) -> dict:
        now_mono = time.monotonic()
        return {
            "enabled": True,
            "base_url": self.base_url,
            "instruments": {
                symbol: {
                    "instrument_id": link.instrument.id,
                    "connected": link.connected,
                    "streams": list(link.streams),
                    "last_message_at": link.last_message_at,
                    "last_valid_message_at": link.last_valid_message_at,
                    "reconnects": link.reconnects,
                    "last_error": link.last_error,
                    "healthy": self._healthy(link, now_mono),
                }
                for symbol, link in sorted(self._links.items())
            },
        }

    async def _monitor_loop(self) -> None:
        """Медленные гауджи: агрегат подключённости и возраст последнего
        валидного сообщения (gaudje не тикают сами — обновляем явно)."""
        while True:
            now = time.monotonic()
            links = list(self._links.values())
            self._metrics.gauge(
                "ws_connected",
                1.0 if links and all(l.connected for l in links) else 0.0,
            )
            ages = [now - l.last_valid_mono for l in links if l.last_valid_mono]
            if ages:
                self._metrics.gauge("ws_last_message_age_s", max(ages))
            await asyncio.sleep(5)

    # ---------- соединение одного инструмента ----------

    async def _run_link(self, link: _LinkState) -> None:
        symbol = link.instrument.symbol
        delays = backoff_delays(self.reconnect_base, self.reconnect_cap)
        first = True
        try:
            while not self._stop_event.is_set():
                url = f"{self.base_url}/stream?streams={'/'.join(link.streams)}"
                try:
                    async with connect(
                        url,
                        ping_interval=PING_INTERVAL_S,
                        ping_timeout=PING_TIMEOUT_S,
                    ) as ws:
                        if first:
                            first = False
                        else:
                            # повторный вход в connect — переподключение
                            # (обрыв, idle-таймаут или плановая ротация)
                            link.reconnects += 1
                            self._metrics.counter("ws_reconnects",
                                                  labels={"venue": _VENUE})
                        link.connected = True
                        link.last_error = None
                        # idle-окно стартует от подключения
                        link.last_message_mono = time.monotonic()
                        self._metrics.counter("ws_connections",
                                              labels={"venue": _VENUE})
                        delays = backoff_delays(self.reconnect_base,
                                                self.reconnect_cap)
                        # AC10: догрузка возможного разрыва до приёма живых
                        # данных как авторитетных (задачи, цикл не ждёт)
                        self._dispatch_reconnect(link)
                        await self._read_loop(link, ws)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    link.last_error = (str(exc) or type(exc).__name__)[:300]
                    log.warning("binance ws %s: %s", symbol, link.last_error)
                finally:
                    link.connected = False
                if self._stop_event.is_set():
                    break
                try:
                    await asyncio.wait_for(self._stop_event.wait(), next(delays))
                    break
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise
        finally:
            link.connected = False

    async def _read_loop(self, link: _LinkState, ws) -> None:
        rotate_at = time.monotonic() + self.lifetime
        while not self._stop_event.is_set():
            now = time.monotonic()
            rotate_left = rotate_at - now
            if rotate_left <= 0:
                log.info("binance ws %s: плановая ротация соединения",
                         link.instrument.symbol)
                return
            idle_left = self.idle_timeout - (now - link.last_message_mono)
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), max(0.05, min(idle_left, rotate_left)))
            except asyncio.TimeoutError:
                if time.monotonic() >= rotate_at:
                    log.info("binance ws %s: плановая ротация соединения",
                             link.instrument.symbol)
                else:
                    link.last_error = f"idle_timeout>{self.idle_timeout:g}s"
                    log.warning("binance ws %s: нет сообщений %ss — reconnect",
                                link.instrument.symbol, self.idle_timeout)
                return
            if await self._handle_raw(link, raw):
                return  # serverShutdown: скорейшее переподключение

    def _dispatch_reconnect(self, link: _LinkState) -> None:
        if self.on_reconnect is None:
            return
        try:
            result = self.on_reconnect(link.instrument, link.tfs)
            if inspect.isawaitable(result):
                self._spawn(result)
        except Exception:
            log.exception("binance realtime: on_reconnect %s упал",
                          link.instrument.symbol)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---------- нормализация сообщений ----------

    def _malformed(self, reason: str) -> None:
        self._metrics.counter("ws_malformed", labels={"venue": _VENUE})
        log.debug("binance ws: сообщение отброшено (%s)", reason)

    async def _handle_raw(self, link: _LinkState, raw) -> bool:
        """Разбор одного фрейма. True — сервер просит переподключение.
        Мусор/неизвестное — счётчик ws_malformed и drop, цикл не падает."""
        self._metrics.counter("ws_messages", labels={"venue": _VENUE})
        link.last_message_mono = time.monotonic()
        link.last_message_at = now_ms()
        try:
            msg = json.loads(raw)
        except (ValueError, TypeError):
            self._malformed("bad_json")
            return False
        if not isinstance(msg, dict):
            self._malformed("not_an_object")
            return False
        if msg.get("stream") == "!serverShutdown":
            log.warning("binance ws %s: serverShutdown — переподключение",
                        link.instrument.symbol)
            return True
        data = msg.get("data")
        if not isinstance(data, dict) or msg.get("stream") not in link.streams:
            self._malformed("unknown_stream_or_shape")
            return False
        etype = data.get("e")
        if etype == "trade":
            tick = self._parse_trade(link, data)
            if tick is None:
                return False
            self._mark_valid(link)
            if self.on_trade is not None:
                try:
                    await self.on_trade(link.instrument, tick)
                except Exception:
                    log.exception("binance realtime: on_trade %s упал",
                                  link.instrument.symbol)
            return False
        if etype == "kline":
            parsed = self._parse_kline(link, data)
            if parsed is None:
                return False
            tf, candle = parsed
            self._mark_valid(link)
            if self.on_kline is not None:
                try:
                    await self.on_kline(link.instrument, tf, candle)
                except Exception:
                    log.exception("binance realtime: on_kline %s упал",
                                  link.instrument.symbol)
            return False
        self._malformed(f"unknown_event:{etype!r}")
        return False

    def _mark_valid(self, link: _LinkState) -> None:
        link.last_valid_mono = time.monotonic()
        link.last_valid_message_at = now_ms()

    @staticmethod
    def _finite_positive(*values: float) -> bool:
        return all(math.isfinite(v) and v > 0 for v in values)

    def _parse_trade(self, link: _LinkState, data: dict) -> Optional[QuoteTick]:
        try:
            if data.get("s") != link.instrument.symbol:
                raise ValueError("symbol_mismatch")
            price = float(data["p"])
            # T — время СДЕЛКИ источника; E (время события) не подменяет его
            event_at = int(data["T"])
            trade_id = int(data["t"])
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            self._malformed(f"trade:{exc}")
            return None
        if not self._finite_positive(price) or event_at <= 0:
            self._malformed("trade:non_finite_or_bad_time")
            return None
        received_at = now_ms()
        if received_at - event_at < 0:
            # §4: часы источника впереди наших — задержка не «отрицательная»,
            # а неизвестная; факт фиксируем, мгновенной доставку не считаем
            self._metrics.counter("clock_skew", labels={"venue": _VENUE})
        return QuoteTick(
            price=price, event_at=event_at, received_at=received_at,
            price_kind="trade", source_seq=str(trade_id),
        )

    def _parse_kline(
        self, link: _LinkState, data: dict
    ) -> Optional[tuple[str, Candle]]:
        try:
            k = data["k"]
            if not isinstance(k, dict):
                raise ValueError("no_kline_payload")
            if data.get("s") != link.instrument.symbol or k.get("s") != link.instrument.symbol:
                raise ValueError("symbol_mismatch")
            tf = _INTERVAL_TO_TF[k["i"]]
            open_time = int(k["t"])
            close_time = int(k["T"])
            o, h, l, c = (float(k[key]) for key in ("o", "h", "l", "c"))
            if type(k["x"]) is not bool:
                raise ValueError("closed_not_boolean")
            closed = k["x"]
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            self._malformed(f"kline:{exc}")
            return None
        if not self._finite_positive(o, h, l, c):
            self._malformed("kline:non_finite_or_non_positive")
            return None
        if not (l <= o <= h and l <= c <= h and l <= h):
            self._malformed("kline:inconsistent_ohlc")
            return None
        if close_time <= open_time:
            self._malformed("kline:bad_times")
            return None
        candle = Candle(
            instrument_id=0,  # проставляет обработчик (воркер)
            timeframe=tf, open_time=open_time, close_time=close_time,
            open=o, high=h, low=l, close=c,
            closed=closed, source="binance_ws", revision=0,
        )
        return tf, candle
