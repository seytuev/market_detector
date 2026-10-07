"""Фоновый процесс HTF Zones (§11 п.4).

Работает независимо от открытой страницы: догружает свечи, гоняет детектор
по закрытым свечам, отслеживает касания по текущей цене, передаёт события
в доставку и WebSocket. Состояние — только в SQLite: после перезапуска
воркер продолжает с последней известной свечи и догружает пропуски,
восстановленные события помечаются delayed (§11).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .adapters.base import AdapterError, MarketDataAdapter, TIMEFRAME_MS
from .config import DetectorConfig, Settings
from .db import Database
from .engine import Scanner
from .engine.replay import migrate_display_fields
from dataclasses import replace

from .models import Candle, Event, EventKind, Instrument, ZoneStatus, ZoneType, now_ms
from .notify.queue import EventDispatcher

log = logging.getLogger(__name__)

# Стартовые активы §1: BTC, ETH, SOL, спот. Символ проверяется по каталогу
# источника; несуществующий инструмент — явная ошибка, без подмены (§1).
SEED = [
    ("binance", "BTCUSDT"),
    ("binance", "ETHUSDT"),
    ("binance", "SOLUSDT"),
]
DEFAULT_TIMEFRAMES = ("D1", "W1")  # запасной вариант, если scan_timeframes пуст
                                   # (§1: H1/H4 убраны по решению пользователя)
STALE_FACTOR = 2  # данные старше N интервалов — проблема (§11)

# F02/A02: изоляция ошибок быстрого цикла котировок — после QUOTE_FAIL_K
# подряд идущих ошибок по инструменту включается пауза 2**min(fails-K, CAP)
# циклов; счётчики сбрасываются первым успешным опросом
QUOTE_FAIL_K = 3
QUOTE_BACKOFF_CAP = 5

# §4 LTF-спеки: наблюдение открывается при фактическом достижении
# подтверждённой HTF-зоны D1/W1 разрешённого типа (настройка
# htf_context_types, ТЗ «LTF Current Setup» §16.1: OB — согласованный
# триггер; FVG — предлагаемый режим, PRB/Breaker/BSL/SSL триггерами
# не являются). FVG_WEAKENED — аналог DEPTH_50 для FVG: касание 50%
# НЕ прекращает FVG (статус WEAKENED — качественная пометка общего
# движка, зона валидна), поэтому такое событие тоже открывает наблюдение.
LTF_START_KINDS = {
    EventKind.TOUCH, EventKind.ALREADY_IN_ZONE,
    EventKind.DEPTH_50, EventKind.DEPTH_90,
    EventKind.FVG_WEAKENED,
}
LTF_PARENT_TIMEFRAMES = ("D1", "W1")
# статусы валидного HTF-родителя (общий движок): ACTIVE и WEAKENED
# (последний бывает только у FVG после 50% — для OB фильтр не меняется)
LTF_PARENT_VALID_STATUSES = (ZoneStatus.ACTIVE, ZoneStatus.WEAKENED)
# состояния наблюдения, при которых LTF продолжает обработку и восстановление
LTF_OPEN_STATES = ("waiting_structure", "active", "paused_data")

_LTF_CONTEXT_ZONE_TYPES = {"OB": ZoneType.OB, "FVG": ZoneType.FVG}


class Worker:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        cfg: DetectorConfig,
        adapters: dict[str, MarketDataAdapter],
        dispatcher: EventDispatcher,
        broadcast=None,  # callable(dict) — WebSocket-рассылка сайта
        ltf_engine=None,      # LtfEngine (этап B2); None — LTF выключен
        ltf_dispatcher=None,  # async callable(list[LtfEvent]) — доставка LTF (этап D)
        alt_runner=None,      # AltRunner (Altcoins D1); None — модуль выключен
        alt_dispatcher=None,  # AltDispatcher — доставка alt_event в Telegram
    ):
        self.db = db
        self.settings = settings
        self.cfg = cfg
        self.adapters = adapters
        self.dispatcher = dispatcher
        self.broadcast = broadcast
        self.ltf_engine = ltf_engine
        self.ltf_dispatcher = ltf_dispatcher
        self.alt_runner = alt_runner
        self.alt_dispatcher = alt_dispatcher
        self.scanner = Scanner(db, cfg)
        # включённые таймфреймы поиска — настройка scan_timeframes (§1)
        self.scan_tfs = tuple(
            t.strip()
            for t in str(getattr(cfg, "scan_timeframes", "")).split(",")
            if t.strip() in TIMEFRAME_MS
        ) or DEFAULT_TIMEFRAMES
        self._stale: dict[tuple[int, str], bool] = {}
        # F02: счётчики ошибок цикла котировок по инструменту — подряд
        # идущие ошибки и остаток паузы (в циклах опроса)
        self._quote_fails: dict[int, int] = {}
        self._quote_skip: dict[int, int] = {}
        self._stop = asyncio.Event()

    # ---------- подготовка ----------

    async def _load_forming_candles(self) -> None:
        """Подтянуть текущий незакрытый бар HTF, чтобы график показывал сегодня."""
        now = now_ms()
        for ins in self.db.get_instruments(enabled_only=True):
            adapter = self.adapters.get(ins.venue)
            if adapter is None:
                continue
            for tf in self.scan_tfs:
                interval = TIMEFRAME_MS[tf]
                start = now - interval
                try:
                    candles = await adapter.klines(
                        ins.symbol, tf, start, now, include_forming=True
                    )
                except AdapterError as exc:
                    log.warning("%s %s: текущий бар не загружен: %s",
                                ins.symbol, tf, exc)
                    continue
                for c in candles:
                    c.instrument_id = ins.id
                    if c.closed:
                        last = self.db.last_candle(ins.id, tf)
                        if last is not None and c.open_time <= last.open_time:
                            continue
                    self.db.insert_candles([c])

    async def seed_instruments(self) -> None:
        """Заводит стартовые инструменты по проверенному каталогу источника (§1)."""
        for venue, symbol in SEED:
            adapter = self.adapters.get(venue)
            if adapter is None:
                continue
            existing = [
                i for i in self.db.get_instruments()
                if i.venue == venue and i.symbol == symbol
            ]
            if existing:
                continue
            catalog = await adapter.catalog()
            match = next((c for c in catalog if c.symbol == symbol), None)
            if match is None:
                log.error("инструмент %s:%s не найден в каталоге источника", venue, symbol)
                await self._service_message(
                    f"Инструмент {venue}:{symbol} не найден в каталоге {venue} — "
                    f"проверьте идентификатор, подмена не выполняется."
                )
                continue
            iid = self.db.upsert_instrument(match)
            log.info("инструмент подключён: %s %s (id=%s)", venue, symbol, iid)

    async def backfill(self, ins: Instrument) -> None:
        """Догрузка истории и replay пропусков (§11).

        Полный replay запускается только когда появились свечи раньше
        прежнего хвоста (первичная загрузка или восстановление после сбоя);
        replay идемпотентен, восстановленные события получают исходное
        occurred_at и delayed=True.
        """
        adapter = self.adapters[ins.venue]
        need_replay = False
        for tf in self.scan_tfs:
            last = self.db.last_candle(ins.id, tf)
            lookback_start = now_ms() - self.cfg.lookback_days_for(tf) * 86_400_000
            history_key = f"history_from:{ins.id}:{tf}"
            raw_from = self.db.get_meta(history_key)
            history_from = int(raw_from) if raw_from else None
            if last is not None:
                # окно ушло глубже уже загруженного хвоста — догружаем раннюю
                # историю назад от первой сохранённой свечи (один раз на окно,
                # граница запрошенной истории — в meta)
                first = self.db.first_candle(ins.id, tf)
                if (
                    first is not None
                    and first.open_time > lookback_start
                    and (history_from is None or lookback_start < history_from)
                ):
                    candles = await adapter.klines(
                        ins.symbol, tf, lookback_start, first.open_time
                    )
                    fresh = [c for c in candles if c.open_time < first.open_time]
                    for c in fresh:
                        c.instrument_id = ins.id
                    if fresh:
                        self.db.insert_candles(fresh)
                        need_replay = True
                        log.info(
                            "%s %s: догружена ранняя история %s — %d свечей",
                            ins.symbol, ins.venue, tf, len(fresh),
                        )
                    self.db.set_meta(history_key, str(lookback_start))
            start = lookback_start
            if last is not None:
                start = max(lookback_start, last.open_time + TIMEFRAME_MS[tf])
            if start >= now_ms() - TIMEFRAME_MS[tf]:
                continue
            candles = await adapter.klines(ins.symbol, tf, start, now_ms())
            fresh = [
                c for c in candles
                if last is None or c.open_time > last.open_time
            ]
            if last is None:
                # полная первичная загрузка покрывает окно целиком
                self.db.set_meta(history_key, str(lookback_start))
            if not fresh:
                continue
            for c in fresh:
                c.instrument_id = ins.id
            self.db.insert_candles(fresh)
            need_replay = True
            log.info("%s %s: догружено %d свечей %s", ins.symbol, ins.venue, len(fresh), tf)
        # replay нужен и при незавершённом прошлом запуске: у ТФ есть свечи,
        # но нет ни одной зоны этого ТФ — структура по нему не построена.
        # Флаг replay_done защищает от вечных повторов для ТФ, где зон
        # законно нет (идемпотентный replay всё равно ничего бы не нашёл).
        replay_done = bool(self.db.get_meta(f"replay_done:{ins.id}"))
        if not need_replay and not replay_done:
            zones = self.db.get_zones(ins.id)
            for tf in self.scan_tfs:
                if self.db.last_candle(ins.id, tf) and not any(
                    z.timeframe == tf for z in zones
                ):
                    need_replay = True
                    break
        if need_replay:
            # replay CPU-bound и длинный (тысячи H1-свечей) — в отдельный поток,
            # чтобы не блокировать event loop и HTTP (доступ к sqlite
            # сериализован замком в app.db)
            events = await asyncio.to_thread(
                self.scanner.replay_instrument, ins.id, None, set(self.scan_tfs)
            )
            self.db.set_meta(f"replay_done:{ins.id}", str(now_ms()))
            log.info(
                "replay %s: свечей обработано, новых событий %d (восстановленных %d)",
                ins.symbol, len(events), sum(1 for e in events if e.delayed),
            )
            # §8: исторические (delayed) события не доставляем — только факт
            # «цена уже в актуальной зоне» на момент подключения инструмента
            fresh = [
                e for e in events
                if e.kind == EventKind.ALREADY_IN_ZONE and not e.delayed
            ]
            if fresh:
                await self.dispatcher.dispatch(fresh)

    # ---------- основной цикл ----------

    async def poll_once(self) -> None:
        for ins in self.db.get_instruments(enabled_only=True):
            try:
                await self._poll_instrument(ins)
            except AdapterError as exc:
                log.warning("%s: источник недоступен: %s", ins.symbol, exc)
                await self._mark_stale(ins, str(exc))
            except Exception:
                log.exception("ошибка опроса %s", ins.symbol)
        await self.dispatcher.retry_pending()

    async def _poll_instrument(self, ins: Instrument) -> None:
        adapter = self.adapters[ins.venue]
        events: list[Event] = []
        for tf in self.scan_tfs:
            last = self.db.last_candle(ins.id, tf)
            start = (
                last.open_time + TIMEFRAME_MS[tf]
                if last is not None
                else now_ms() - self.cfg.lookback_days_for(tf) * 86_400_000
            )
            candles = await adapter.klines(
                ins.symbol, tf, start, now_ms(), include_forming=True
            )
            fresh_closed = False
            for c in candles:
                c.instrument_id = ins.id
                if c.closed:
                    if last is None or c.open_time > last.open_time:
                        self.db.insert_candles([c])
                        events += await asyncio.to_thread(
                            self.scanner.on_closed_candle, c
                        )
                        fresh_closed = True
                else:
                    # текущий незакрытый бар D1/W1 — на график, не в детектор
                    self.db.insert_candles([c])
            if (fresh_closed or any(not c.closed for c in candles)) and self.broadcast:
                self.broadcast({
                    "type": "candle", "instrument_id": ins.id, "timeframe": tf,
                })
            await self._check_freshness(ins, tf)

        # F02: при включённом быстром цикле котировку обновляет он
        # (quote_loop) — HTF-цикл цену не запрашивает; выключен
        # (quote_poll_seconds = 0) — прежний путь, котировка вместе с HTF
        if self.settings.quote_poll_seconds <= 0:
            events += await self._poll_quote(ins)
        await self._dispatch_events(events)
        await self._ltf_on_poll(ins, events)

    async def _poll_quote(self, ins: Instrument) -> list[Event]:
        """Котировка инструмента: last_price → meta → on_price → WS-цена.

        Общий хвост HTF-цикла (quote_poll_seconds = 0) и быстрого цикла
        котировок (F02). События возвращает — доставку и запуск
        LTF-наблюдений выполняет вызывающий."""
        adapter = self.adapters[ins.venue]
        price, ts = await adapter.last_price(ins.symbol)
        # последняя котировка — в meta: read model LTF (§14) считает положение
        # цены относительно HTF-зон серверно по свежей котировке
        self.db.set_quote(ins.id, price, ts)
        events = await asyncio.to_thread(
            self.scanner.on_price, ins.id, price, ts, True, set(self.scan_tfs)
        )
        if self.broadcast:
            payload = {
                "type": "price", "instrument_id": ins.id, "price": price, "time": ts,
                "candles": {},
            }
            for tf in self.scan_tfs:
                bar = self._apply_price_to_forming(ins.id, tf, price)
                if bar is not None:
                    payload["candles"][tf] = {
                        "time": bar.open_time // 1000,
                        "open": bar.open, "high": bar.high,
                        "low": bar.low, "close": bar.close,
                    }
            self.broadcast(payload)
        return events

    async def _dispatch_events(self, events: list[Event]) -> None:
        """Доставка событий детектора и их WS-рассылка (одинаково во всех
        циклах — HTF, LTF, котировки)."""
        if not events:
            return
        await self.dispatcher.dispatch(events)
        if self.broadcast:
            for e in events:
                self.broadcast({
                    "type": "event", "zone_id": e.zone_id, "kind": e.kind.value,
                    "price": e.price, "occurred_at": e.occurred_at,
                })

    def _apply_price_to_forming(
        self, instrument_id: int, timeframe: str, price: float
    ) -> Candle | None:
        """Подтянуть high/low/close текущей незакрытой свечи. Детектор не трогаем."""
        bar = self.db.last_candle(instrument_id, timeframe, closed_only=False)
        if bar is None or bar.closed:
            return None
        updated = replace(
            bar,
            high=max(bar.high, price),
            low=min(bar.low, price),
            close=price,
        )
        self.db.insert_candles([updated])
        return updated

    # ---------- быстрый цикл котировок (F02/A02) ----------

    def _quote_on_success(self, instrument_id: int) -> None:
        """Успешный опрос сбрасывает счётчики ошибок и паузу инструмента."""
        self._quote_fails.pop(instrument_id, None)
        self._quote_skip.pop(instrument_id, None)

    def _quote_on_failure(self, ins: Instrument) -> None:
        """Ошибка опроса: после QUOTE_FAIL_K подряд — экспоненциальная
        пауза 2**min(fails-K, QUOTE_BACKOFF_CAP) циклов."""
        fails = self._quote_fails.get(ins.id, 0) + 1
        self._quote_fails[ins.id] = fails
        if fails >= QUOTE_FAIL_K:
            pause = 2 ** min(fails - QUOTE_FAIL_K, QUOTE_BACKOFF_CAP)
            self._quote_skip[ins.id] = pause
            log.warning(
                "%s: котировка недоступна %d раз подряд — пауза %d циклов",
                ins.symbol, fails, pause,
            )

    async def _quote_poll_once(self) -> None:
        """Один проход цикла котировок: цена каждого инструмента независимо —
        ошибка одного не останавливает остальных, проблемный уходит в паузу."""
        for ins in self.db.get_instruments(enabled_only=True):
            left = self._quote_skip.get(ins.id, 0)
            if left > 0:
                self._quote_skip[ins.id] = left - 1
                continue
            try:
                events = await self._poll_quote(ins)
            except AdapterError as exc:
                log.warning("%s: котировка недоступна: %s", ins.symbol, exc)
                self._quote_on_failure(ins)
                continue
            except Exception:
                log.exception("ошибка опроса котировки %s", ins.symbol)
                self._quote_on_failure(ins)
                continue
            self._quote_on_success(ins.id)
            await self._dispatch_events(events)
            # касание по тику запускает LTF-наблюдение, не дожидаясь HTF-цикла
            await self._ltf_on_poll(ins, events)

    async def quote_loop(self) -> None:
        """F02/A02: быстрый цикл котировок — короткий заход цены в зону
        между HTF-опросами (poll_seconds) больше не теряется. Интервал
        читается каждый цикл — живое изменение настройки подхватывается."""
        while not self._stop.is_set():
            try:
                await self._quote_poll_once()
            except Exception:
                log.exception("ошибка цикла котировок")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), self.settings.quote_poll_seconds
                )
            except asyncio.TimeoutError:
                pass

    # ---------- LTF Confirmations (LTF-спека §4, §13) ----------

    @property
    def _ltf_active(self) -> bool:
        return self.ltf_engine is not None and self.cfg.ltf_enabled

    def _ltf_context_zone_types(self) -> set[ZoneType]:
        """Типы HTF-зон D1/W1, запускающие наблюдение (§16.1).

        Источник — настройка htf_context_types (по умолчанию только OB;
        FVG — предлагаемый режим). Тест и рыночная актуальность зоны —
        из общего движка, специальных правил для FVG здесь нет."""
        return {
            _LTF_CONTEXT_ZONE_TYPES[t]
            for t in self.cfg.htf_context_type_set()
            if t in _LTF_CONTEXT_ZONE_TYPES
        }

    def _ltf_is_context_zone(self, zone) -> bool:
        return (
            zone.type in self._ltf_context_zone_types()
            and zone.timeframe in LTF_PARENT_TIMEFRAMES
        )

    async def _ltf_open_observation(
        self, ins: Instrument, zone, occurred_at: int
    ) -> None:
        """Открыть наблюдение за зоной и подгрузить H1-контекст (§4).

        Идемпотентно по UNIQUE(zone_id, cycle_id) (§4, приёмка п.20);
        подгрузка истории — один раз на наблюдение (meta-флаг seeded)."""
        obs = await asyncio.to_thread(
            self.ltf_engine.on_htf_zone_touched, ins.id, zone, occurred_at
        )
        if not self.db.get_meta(f"ltf:h1:seeded:{obs.id}"):
            # §4: подгрузить историю H1 до касания — предшествующая
            # структура как контекст; события replay помечаются delayed
            await self._ltf_load_h1_history(ins, occurred_at)
            await asyncio.to_thread(self.ltf_engine.replay_observation, obs.id)
            self.db.set_meta(f"ltf:h1:seeded:{obs.id}", "1")
            log.info("LTF: наблюдение %s открыто по %s %s (зона %s)",
                     obs.id, ins.symbol, zone.timeframe, zone.id)

    async def _ltf_open_marked_zones(self, ins: Instrument) -> None:
        """Галочка «Анализировать»: наблюдения на все подтверждённые HTF-зоны
        разрешённых типов D1/W1 инструмента без ожидания касания.
        Закрытые наблюдения не воскрешаются."""
        zones = await asyncio.to_thread(
            self.db.get_zones, instrument_id=ins.id,
            statuses=list(LTF_PARENT_VALID_STATUSES),
        )
        for zone in zones:
            if not self._ltf_is_context_zone(zone):
                continue
            # ТЗ 06.10.2026 §13 (T21): невалидный/неподтверждённый HTF OB не
            # порождает новые LTF-сценарии
            if not zone.is_currently_relevant():
                continue
            existing = await asyncio.to_thread(
                self.db.get_ltf_observation_by_zone, zone.id, zone.cycle_id
            )
            if existing is not None:
                continue  # открытое уже анализируется, закрытое не трогаем
            await self._ltf_open_observation(ins, zone, self._first_htf_reach_ms(zone))

    async def _ltf_open_reached_parents(self, ins: Instrument) -> None:
        """Открыть наблюдение, если HTF-зона D1/W1 разрешённого типа уже
        достигнута.

        Исторические TOUCH/ALREADY_IN_ZONE/DEPTH не приходят повторно в
        текущий poll — без этого LTF остаётся пустым на живой HTF-базе.
        Цена внутри валидной зоны считается достижением."""
        last = (
            self.db.last_candle(ins.id, "D1")
            or self.db.last_candle(ins.id, "H1")
        )
        price = last.close if last is not None else None
        zones = await asyncio.to_thread(
            self.db.get_zones, instrument_id=ins.id,
            statuses=list(LTF_PARENT_VALID_STATUSES),
        )
        for zone in zones:
            if not self._ltf_is_context_zone(zone):
                continue
            # ТЗ 06.10.2026 §13 (T21): невалидный/неподтверждённый HTF OB не
            # порождает новые LTF-сценарии
            if not zone.is_currently_relevant():
                continue
            existing = await asyncio.to_thread(
                self.db.get_ltf_observation_by_zone, zone.id, zone.cycle_id
            )
            if existing is not None:
                continue
            inside = (
                price is not None and zone.lower <= price <= zone.upper
            )
            events = await asyncio.to_thread(
                self.db.get_events, zone_id=zone.id, limit=100
            )
            start_events = [e for e in events if e.kind in LTF_START_KINDS]
            reached = inside or bool(start_events)
            if reached:
                if start_events:
                    occurred = min(e.occurred_at for e in start_events)
                else:
                    occurred = self._first_htf_reach_ms(zone)
                await self._ltf_open_observation(ins, zone, occurred)

    def _first_htf_reach_ms(self, zone) -> int:
        """Время первого касания диапазона HTF-зоны по закрытым свечам.

        Не «сейчас»: иначе LTF пропускает уже случившийся BOS/SMS после входа
        в зону и навсегда остаётся в «ожидании слома».

        Свеча формирования зоны СЧИТАЕТСЯ касанием намеренно (см. финальный
        отчёт ТЗ «LTF Current Setup», п.9.8): activated_at задаёт окно
        opening-скана detect_breaks (since_ms) и фильтр свечей движка; сдвиг
        за confirmed_at меняет, какие сломы открывают сценарий и какие
        промежуточные сценарии порождает replay, — то есть торговую семантику.
        Поэтому «оптимизация» стартовой точки не применяется: цена вопроса —
        replay от display_from (месяцы H1 для старых зон), что снимается
        кэшированием LTF-чтений и replay один раз на инструмент."""
        start = zone.display_from or zone.formed_at or 0
        for tf in (zone.timeframe, "H1"):
            candles = self.db.get_candles(zone.instrument_id, tf, start_ms=start)
            for c in candles:
                if not c.closed:
                    continue
                if c.low <= zone.upper and c.high >= zone.lower:
                    return c.open_time
        return now_ms()

    async def _realign_waiting_observations(self, ins: Instrument) -> None:
        """Если наблюдение открыли слишком поздно — вернуть activated_at
        к фактическому входу в зону и переиграть H1."""
        for obs in self.db.list_ltf_observations(instrument_id=ins.id):
            if obs.state not in LTF_OPEN_STATES:
                continue
            if self.db.get_active_ltf_scenario(obs.id) is not None:
                continue
            zone = self.db.get_zone(obs.zone_id)
            if zone is None:
                continue
            reach = self._first_htf_reach_ms(zone)
            if reach >= obs.activated_at:
                continue
            self.db.update_ltf_observation(obs.id, activated_at=reach)
            log.info(
                "LTF: наблюдение %s сдвинуто к фактическому входу %s",
                obs.id, reach,
            )
            await asyncio.to_thread(self.ltf_engine.replay_observation, obs.id)

    async def _ltf_on_poll(self, ins: Instrument, events: list[Event]) -> None:
        """Запуск наблюдений от HTF-событий и контроль валидности родителей.

        Пятидневное подавление повторных HTF-сообщений (notify-слой) сюда не
        доходит и наблюдение не блокирует (§4).
        """
        if not self._ltf_active:
            return
        for e in events:
            if e.kind not in LTF_START_KINDS:
                continue
            zone = self.db.get_zone(e.zone_id)
            if (
                zone is None
                or not self._ltf_is_context_zone(zone)
                # только валидный родитель (WEAKENED FVG валиден — общий
                # движок не прекращает FVG по касанию 50%)
                or zone.status not in LTF_PARENT_VALID_STATUSES
                # ТЗ 06.10.2026 §13 (T21): единый canonical state
                or not zone.is_currently_relevant()
            ):
                continue
            await self._ltf_open_observation(ins, zone, e.occurred_at)
        # §4: инвалидация родителя закрывает сценарий (HTF_INVALIDATED);
        # тест 90% и запрет Breaker наблюдение не закрывают (внутри метода)
        for obs in self.db.list_ltf_observations(instrument_id=ins.id):
            if obs.state not in LTF_OPEN_STATES:
                continue
            zone = self.db.get_zone(obs.zone_id)
            if zone is not None:
                await asyncio.to_thread(self.ltf_engine.check_parent_validity, zone)

    async def _ltf_load_h1_history(self, ins: Instrument, since_ms: int) -> None:
        adapter = self.adapters[ins.venue]
        start = max(0, since_ms - self.cfg.ltf_history_days * 86_400_000)
        candles = await adapter.klines(ins.symbol, "H1", start, now_ms())
        for c in candles:
            c.instrument_id = ins.id
        if candles:
            self.db.insert_candles(candles)

    async def ltf_poll_once(self) -> None:
        """Один проход LTF-цикла: общий поток закрытий H1 (§13), без
        отдельного запроса на каждую карточку."""
        if not self._ltf_active:
            return
        for ins in self.db.get_instruments(enabled_only=True):
            # уже случившееся достижение HTF-зоны — открыть даже без галочки
            await self._ltf_open_reached_parents(ins)
            await self._realign_waiting_observations(ins)
            if ins.ltf_analyze:
                # «Анализировать»: наблюдения открываются без касания HTF-зоны
                await self._ltf_open_marked_zones(ins)
            # H1 грузим независимо от наличия наблюдений: график LTF и
            # data_quality читают таблицу candle — на свежей БД (деплой, давно
            # без касаний HTF-зон) наблюдений ещё нет, и без этой загрузки
            # график остаётся пустым до первого случайного касания зоны
            adapter = self.adapters[ins.venue]
            try:
                last = self.db.last_candle(ins.id, "H1")
                start = (
                    last.open_time + TIMEFRAME_MS["H1"]
                    if last is not None
                    else now_ms() - self.cfg.ltf_history_days * 86_400_000
                )
                candles = await adapter.klines(
                    ins.symbol, "H1", start, now_ms(), include_forming=True
                )
            except AdapterError as exc:
                log.warning("LTF %s: источник недоступен: %s", ins.symbol, exc)
                continue
            incoming = []
            for c in candles:
                c.instrument_id = ins.id
                if c.closed and (last is None or c.open_time > last.open_time):
                    incoming.append(c)
                elif not c.closed:
                    incoming.append(c)
            if incoming:
                self.db.insert_candles(incoming)
                if self.broadcast:
                    self.broadcast({
                        "type": "candle", "instrument_id": ins.id,
                        "timeframe": "H1",
                    })
            observations = [
                o for o in self.db.list_ltf_observations(instrument_id=ins.id)
                if o.state in LTF_OPEN_STATES
            ]
            if not observations:
                continue
            result = await asyncio.to_thread(
                self.ltf_engine.process_h1_close, ins.id
            )
            if result.events:
                # точка подключения доставки LTF (этап D): одна строка —
                # ltf_dispatcher передаётся в Worker из main.py
                if self.ltf_dispatcher is not None:
                    await self.ltf_dispatcher(result.events)
                if self.broadcast:
                    self.broadcast({
                        "type": "ltf", "instrument_id": ins.id,
                        "events": len(result.events),
                    })
        # автоархивация наблюдений без активности (раз в LTF-цикл, один
        # индексированный запрос + работа только по найденным кандидатам)
        if self._ltf_active:
            archived = await asyncio.to_thread(
                self.ltf_engine.archive_stale_observations
            )
            if archived:
                log.info("LTF: архивированы неактивные наблюдения: %s", archived)
        # ретрай недоставленных LTF-сообщений (отказ Telegram не отменяет
        # факт касания — повторяется только доставка, §9)
        retry = getattr(self.ltf_dispatcher, "retry_pending", None)
        if self._ltf_active and retry is not None:
            await retry()
        # досылка графиков к доставленным событиям (§7 ТЗ 07.10.2026)
        retry_charts = getattr(self.ltf_dispatcher, "retry_charts", None)
        if self._ltf_active and retry_charts is not None:
            await retry_charts()

    async def ltf_loop(self) -> None:
        """Фоновый цикл LTF: закрытая страница анализ не останавливает (§12)."""
        while not self._stop.is_set():
            if self._ltf_active:
                try:
                    await self.ltf_poll_once()
                except Exception:
                    log.exception("ошибка LTF-цикла")
            try:
                await asyncio.wait_for(self._stop.wait(), self.cfg.ltf_poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _ltf_restore_all(self) -> None:
        """§13: восстановление после перезапуска — догрузить H1 и replay
        наблюдений; массовые исторические события не доставляются (delayed)."""
        if not self._ltf_active:
            return
        for ins in self.db.get_instruments(enabled_only=True):
            observations = [
                o for o in self.db.list_ltf_observations(instrument_id=ins.id)
                if o.state in LTF_OPEN_STATES
            ]
            if not observations:
                # без наблюдений replay не нужен, но H1-окно для графика LTF
                # догружаем один раз — иначе на свежей БД график пуст до
                # первого касания HTF-зоны
                if self.db.last_candle(ins.id, "H1") is None:
                    try:
                        await self._ltf_load_h1_history(ins, now_ms())
                    except AdapterError as exc:
                        log.warning(
                            "LTF %s: догрузка H1 пропущена: %s", ins.symbol, exc)
                continue
            self._set_replaying(ins.id, True)
            try:
                last = self.db.last_candle(ins.id, "H1")
                start = (
                    min(o.activated_at for o in observations)
                    - self.cfg.ltf_history_days * 86_400_000
                )
                if last is not None:
                    start = max(start, last.open_time + TIMEFRAME_MS["H1"])
                if start < now_ms() - TIMEFRAME_MS["H1"]:
                    candles = await self.adapters[ins.venue].klines(
                        ins.symbol, "H1", start, now_ms()
                    )
                    for c in candles:
                        c.instrument_id = ins.id
                    if candles:
                        self.db.insert_candles(candles)
                # Один replay на инструмент, а не на каждое наблюдение:
                # replay_observation за один проход обрабатывает ВСЕ открытые
                # наблюдения инструмента (работа зависит только от
                # instrument_id) и идемпотентен — 12 наблюдений на одном
                # инструменте раньше давали 12 одинаковых прогонов всей
                # H1-истории (restore шёл часами)
                await asyncio.to_thread(
                    self.ltf_engine.replay_observation, observations[0].id
                )
                log.info("LTF %s: восстановлено наблюдений: %d",
                         ins.symbol, len(observations))
            except AdapterError as exc:
                log.warning("LTF %s: восстановление пропущено: %s", ins.symbol, exc)
            except Exception:
                log.exception("LTF: ошибка восстановления %s", ins.symbol)
            finally:
                self._set_replaying(ins.id, False)

    # ---------- Altcoins D1 accumulation (ТЗ 07.10.2026 §4, §18) ----------

    async def alt_loop(self) -> None:
        """Фоновый цикл дневного job альткоинов: раз в poll_interval_seconds
        проверяет расписание (should_run — штатный слот МСК + разовый
        catch-up пропущенных суток). После успешного прогона: доставка
        новых событий в Telegram (§18), разовая сводка первичной загрузки
        и WS-обновление страницы без ручной перезагрузки."""
        while not self._stop.is_set():
            cfg = self.settings.alt_config
            if self.alt_runner is not None and cfg.job_enabled:
                try:
                    if self.alt_runner.should_run(datetime.now(timezone.utc)):
                        result = await self.alt_runner.run_daily("schedule")
                        if result.get("status") == "ok":
                            if self.broadcast:
                                self.broadcast({
                                    "type": "alt",
                                    "run_id": result.get("run_id"),
                                    "processed": result.get("processed", 0),
                                    "errors": result.get("errors", 0),
                                })
                            if self.alt_dispatcher is not None:
                                # §18: live/catchup события прогона — в Telegram;
                                # первичная загрузка — одна сводка, не спам
                                await self.alt_dispatcher.dispatch_pending()
                                summary = result.get("summary") or {}
                                if summary.get("backfill_event_ids"):
                                    await (
                                        self.alt_dispatcher
                                        .notify_backfill_summary(summary)
                                    )
                except Exception:
                    log.exception("ошибка ALT-цикла")
                if self.alt_dispatcher is not None:
                    # ретрай недоставленных (отказ Telegram не отменяет факт
                    # события — повторяется только доставка, как у LTF)
                    try:
                        await self.alt_dispatcher.retry_pending()
                    except Exception:
                        log.exception("ALT: ошибка ретрая доставки")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), cfg.poll_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    async def _check_freshness(self, ins: Instrument, tf: str) -> None:
        """Устаревшие данные — показать и уведомить (§11).

        D02: порог — настройка stale_<tf>_intervals; флаг дублируется в meta
        (stale:{id}:{tf}), чтобы API показывал состояние источника и после
        перезапуска процесса."""
        last = self.db.last_candle(ins.id, tf)
        stale = (
            last is None
            or now_ms() - last.close_time > self._stale_limit_ms(tf)
        )
        self.db.set_meta(f"stale:{ins.id}:{tf}", "1" if stale else "0")
        key = (ins.id, tf)
        was = self._stale.get(key, False)
        if stale and not was:
            await self._service_message(
                f"Проблема данных: {ins.venue}:{ins.symbol} {tf} — последняя закрытая "
                f"свеча устарела, сигналы по этому инструменту могут запаздывать."
            )
        elif not stale and was:
            await self._service_message(
                f"Данные восстановлены: {ins.venue}:{ins.symbol} {tf} — "
                f"свечи снова поступают."
            )
        self._stale[key] = stale

    def _stale_limit_ms(self, tf: str) -> int:
        """D02: порог устаревания ТФ — отдельной настройкой в интервалах."""
        intervals = {
            "H1": self.cfg.stale_h1_intervals,
            "D1": self.cfg.stale_d1_intervals,
            "W1": self.cfg.stale_w1_intervals,
        }.get(tf, STALE_FACTOR)
        return intervals * TIMEFRAME_MS[tf]

    def _set_replaying(self, instrument_id: int, on: bool) -> None:
        """D02: маркер восстановления/replay в meta — API помечает снимок
        replaying, пока идёт догрузка истории инструмента."""
        self.db.set_meta(f"replaying:{instrument_id}", "1" if on else "0")

    async def _mark_stale(self, ins: Instrument, reason: str) -> None:
        """Источник недоступен целиком: одно сообщение на инструмент,
        флаги stale — на все ТФ (восстановление отслеживается по каждому)."""
        if any(not self._stale.get((ins.id, tf), False) for tf in self.scan_tfs):
            await self._service_message(
                f"Источник {ins.venue}:{ins.symbol} недоступен: {reason}"
            )
        for tf in self.scan_tfs:
            self._stale[(ins.id, tf)] = True
            self.db.set_meta(f"stale:{ins.id}:{tf}", "1")

    async def _service_message(self, text: str) -> None:
        """Сервисное уведомление владельцу (не рыночное событие, §11) —
        общий транспорт диспетчера: журнал delivery, LogSender без токена."""
        await self.dispatcher.notify_service(text)

    async def run(self) -> None:
        # ТЗ 07.10.2026 §13: миграция снятых/зеркальных уровней SSL/BSL —
        # идемпотентна (флаг в meta), с бэкапом; выполняется один раз до
        # прогрева кэшей чтения, чтобы снятые уровни не попали в выборки
        try:
            from .services.taken_levels_migration import run as _migrate_taken

            rep = _migrate_taken(self.settings.db_path)
            if any(rep["counters"].values()):
                log.info("миграция taken_levels: %s", rep["counters"])
                self.db.invalidate_zone_cache()
                self.db._bump_ltf_cache()
        except Exception:
            log.exception("миграция taken_levels не удалась (не критично для старта)")
        await self.seed_instruments()
        # текущий незакрытый D1/W1 — на график сразу, не ждать backfill/migrate
        await self._load_forming_candles()
        # §15.1.3: довычисление display-полей для зон, созданных до этих
        # правил (идемпотентно)
        mig = migrate_display_fields(self.db)
        if any(mig.values()):
            log.info("миграция display-полей: %s", mig)
        for ins in self.db.get_instruments(enabled_only=True):
            self._set_replaying(ins.id, True)
            try:
                await self.backfill(ins)
            except Exception:
                log.exception("backfill %s", ins.symbol)
            finally:
                self._set_replaying(ins.id, False)
        # §13 LTF: восстановить наблюдения/сценарии/диапазоны после перезапуска
        await self._ltf_restore_all()
        log.info("воркер запущен, опрос каждые %s с", self.settings.poll_seconds)
        loops = [self._main_loop(), self.ltf_loop()]
        if self.alt_runner is not None:
            # дневной job «Altcoins D1 accumulation» — независимый цикл (§4)
            loops.append(self.alt_loop())
        if self.settings.quote_poll_seconds > 0:
            # F02: котировки — отдельным быстрым циклом
            loops.append(self.quote_loop())
        await asyncio.gather(*loops)

    async def _main_loop(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            try:
                await asyncio.wait_for(self._stop.wait(), self.settings.poll_seconds)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
