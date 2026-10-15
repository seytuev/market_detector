"""Фоновый процесс HTF Zones (§11 п.4).

Работает независимо от открытой страницы: догружает свечи, гоняет детектор
по закрытым свечам, отслеживает касания по текущей цене, передаёт события
в доставку и WebSocket. Состояние — только в SQLite: после перезапуска
воркер продолжает с последней известной свечи и догружает пропуски,
восстановленные события помечаются delayed (§11).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from threading import RLock
from datetime import datetime, timezone
from typing import Optional

from .adapters.base import AdapterError, MarketDataAdapter, QuoteTick, TIMEFRAME_MS
from .config import DetectorConfig, Settings
from .db import Database
from .engine import Scanner
from .engine.replay import migrate_display_fields
from dataclasses import asdict, replace

from .models import (
    Candle, Event, EventKind, Instrument, ZoneStatus, ZoneType,
    bar_period_contains, now_ms,
)
from .services.htf_parent import (
    PARENT_QUERY_STATUSES,
    context_type_name,
    eligible_htf_parent,
)
from .notify.queue import EventDispatcher
from .realtime.binance_ws import BinanceRealtime
from .services.calc_queue import CalcQueue
from .services.data_sync import DataSync, instrument_owned
from .services.market_queue import MarketQueue
from .services.metrics import get_registry
from .services.runtime import Readiness

log = logging.getLogger(__name__)

# Стартовые активы: BTC, ETH, SOL, ZEC на Binance Spot и HYPE на Hyperliquid Spot.
# Символ проверяется по каталогу источника
# источника; несуществующий инструмент — явная ошибка, без подмены (§1).
SEED = [
    ("binance", "BTCUSDT"),
    ("binance", "ETHUSDT"),
    ("binance", "SOLUSDT"),
    ("binance", "ZECUSDT"),
    # Hyperliquid: HYPE/USDT0 (API alias @207).
    ("hyperliquid", "HYPE"),
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
# htf_context_types). Правила v1 §3: источниками являются также уровни
# BSL/SSL (контакт — LEVEL_TAKEN: касание/пересечение без требования
# подтверждённого sweep; направление ожидания BSL→bear, SSL→bull) и
# пересечение всей зоны между наблюдениями (JUMP_THROUGH, §10.1: ожидание
# допустимо, точный момент контакта неизвестен).
# FVG_WEAKENED — аналог DEPTH_50 для FVG: касание 50%
# НЕ прекращает FVG (статус WEAKENED — качественная пометка общего
# движка, зона валидна), поэтому такое событие тоже открывает наблюдение.
LTF_START_KINDS = {
    EventKind.TOUCH, EventKind.ALREADY_IN_ZONE,
    EventKind.DEPTH_50, EventKind.DEPTH_90,
    EventKind.FVG_WEAKENED,
    EventKind.LEVEL_TAKEN, EventKind.JUMP_THROUGH,
}
LTF_PARENT_TIMEFRAMES = ("D1", "W1")
# Допуск родителя — eligible_htf_parent (F01). Этот набор только не даёт
# выборке отрезать подтверждённый candidate; уже него быть нельзя.
LTF_PARENT_VALID_STATUSES = PARENT_QUERY_STATUSES
# состояния наблюдения, при которых LTF продолжает обработку и восстановление
LTF_OPEN_STATES = ("waiting_structure", "active", "paused_data")

_LTF_CONTEXT_ZONE_TYPES = {
    "OB": ZoneType.OB, "FVG": ZoneType.FVG,
    "BSL": ZoneType.BSL, "SSL": ZoneType.SSL,
}


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
        self._ltf_engines = {}
        self._replay_depth = {}
        self._replay_lock = RLock()
        self._restoring_instruments = set()
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
        self.data_sync = DataSync(self)
        # этап 2 (§8): тяжёлая обработка тиков — per-instrument очередь;
        # _poll_quote делает только быстрый ingest
        self.market_queue = MarketQueue(self)
        # этап 4 (§9): очередь расчётов закрытых свечей — приоритеты,
        # коалесцинг, fairness-старение, durable calc_task (AC18). Тиковый
        # on_price сюда НЕ входит: его sub-second путь уже изолирован в
        # market_queue, вторая очередь добавила бы задержку без выигрыша.
        self.calc_queue = CalcQueue(
            self.db, self._run_calc_task,
            concurrency=max(1, getattr(settings, "calculation_concurrency", 1)),
        )
        get_registry().register("calc_skipped_unchanged", "counter", ("timeframe",))
        # этап 3 (§7): realtime-поток Binance по WebSocket — за флагом
        # realtime_enabled (по умолчанию выключен, поведение прежнее, AC21).
        # Возможность источника (realtime_supported) отделена от флага.
        self.realtime: Optional[BinanceRealtime] = None
        binance = self.adapters.get("binance")
        if (
            getattr(settings, "realtime_enabled", False)
            and binance is not None
            and getattr(binance, "realtime_supported", lambda: False)()
        ):
            self.realtime = BinanceRealtime(
                base_url=settings.binance_ws_url,
                on_trade=self._ws_on_trade,
                on_kline=self._ws_on_kline,
                on_reconnect=self._ws_on_reconnect,
                idle_timeout=settings.ws_idle_timeout,
                lifetime=settings.ws_lifetime_s,
                reconnect_base=settings.ws_reconnect_base_s,
                reconnect_cap=settings.ws_reconnect_cap_s,
            )
        self._ws_background: set[asyncio.Task] = set()
        # этап 5 (§11): фазы готовности web → realtime → history → calc
        # (читается /api/ready; ready=true начиная с realtime)
        self.readiness = Readiness()
        # фоновая догрузка истории и повтор сетевого каталога источника
        self._backfill_task: Optional[asyncio.Task] = None
        self._catalog_retry_task: Optional[asyncio.Task] = None
        self._seed_pending_venues: set[str] = set()
        self.seed_catalog_retry_seconds = 60.0
        self.seed_catalog_max_attempts = 5

    async def _calculate(self, fn, *args, calculation_instrument=None, **kwargs):
        if getattr(fn, '__self__', None) is self.scanner and args:
            self.scanner._bar_checkpoint = self._calculation_checkpoint
            if fn.__name__ in ('replay_instrument', 'process_candle_revisions', 'on_price'):
                calculation_instrument = args[0]
            elif fn.__name__ == 'on_closed_candle':
                calculation_instrument = args[0].instrument_id
        if self.ltf_engine is not None and getattr(fn, '__self__', None) is self.ltf_engine:
            name = fn.__name__
            if name in ('process_h1_close', 'on_htf_zone_touched') and args:
                calculation_instrument = args[0]
            elif name == 'replay_observation' and args:
                obs = self.db.get_ltf_observation(args[0])
                calculation_instrument = obs.instrument_id if obs else None
            elif name == 'check_parent_validity' and args:
                calculation_instrument = args[0].instrument_id
            if calculation_instrument is not None:
                from .engine.ltf.engine import LtfEngine
                engine = self._ltf_engines.get(calculation_instrument)
                if engine is None:
                    engine = self.ltf_engine if not self._ltf_engines else LtfEngine(
                        self.db, self.cfg, scan_cursors=self.ltf_engine.scan_cursors)
                    self._ltf_engines[calculation_instrument] = engine
                engine.cfg = self.cfg
                engine._bar_checkpoint = self._calculation_checkpoint
                fn = getattr(engine, name)
        from functools import wraps
        @wraps(fn)
        def atomic_calculation():
            from .services.runtime import CALC_OWNER, assert_owner
            claim = getattr(self, "_calc_claim", None)
            # Без внешнего DEFERRED-батча: единый батч на весь расчёт либо
            # держит писателя с самого начала (IMMEDIATE — тогда заблокированный
            # расчёт останавливает ingest котировок и watchdog job, AC02/§15),
            # либо падает с SQLITE_BUSY_SNAPSHOT при апгрейде после чужого
            # commit'а. Движок батчит свои записи сам (replay_instrument и
            # методы записи — _transactional), а курсоры обработки делают
            # крах середины расчёта восстановимым (AC18). Проверки владельца
            # и enabled-гард не требуют транзакции.
            if claim: assert_owner(self.db, CALC_OWNER, claim)
            name = getattr(fn,"__name__","")
            iid = None
            if args and name in {"process_h1_close","on_htf_zone_touched"}:
                iid = args[0]
            elif args and name in {"on_closed_candle","check_parent_validity"}:
                iid = args[0].instrument_id
            if iid is not None:
                instrument = self.db.get_instrument(iid)
                if instrument is None or not instrument.enabled:
                    if name == "process_h1_close":
                        from .engine.ltf.engine import LtfTickResult
                        return LtfTickResult()
                    return None if name == "on_htf_zone_touched" else []
            previous_scope = getattr(self.db._local, 'calculation_instrument', None)
            self.db._local.calculation_instrument = calculation_instrument
            try:
                result = fn(*args, **kwargs)
            finally:
                self.db._local.calculation_instrument = previous_scope
            if claim: assert_owner(self.db, CALC_OWNER, claim, check_expiry=False)
            return result
        return await self.data_sync.calculate(atomic_calculation, instrument_id=calculation_instrument)

    def _calculation_checkpoint(self):
        if self._stop.is_set():
            raise InterruptedError('worker_stopping')
        claim = getattr(self, '_calc_claim', None)
        if claim:
            from .services.runtime import CALC_OWNER, assert_owner
            assert_owner(self.db, CALC_OWNER, claim)


    # ---------- подготовка ----------

    async def seed_instruments(self) -> None:
        """Заводит стартовые инструменты по проверенному каталогу источника (§1).

        §11 (этап 5): временная сетевая ошибка каталога не блокирует уже
        известные валидные инструменты из БД — такой источник пропускается
        (метрика seed_catalog_fallback), а каталог повторяется фоновой
        задачей. Каталог ПОЛУЧЕН, но символа нет — прежняя явная ошибка,
        без подмены."""
        # Удалённый из стартового списка Binance-инструмент не должен
        # оставаться активным после обновления существующей базы.
        for old in self.db.get_instruments():
            if old.venue == "binance" and old.symbol == "HYPEUSDT":
                await self.data_sync._write(self.db.set_instrument_enabled, old.id, False)
        # @207 и повторный HYPE — один и тот же спот. Пустой дубль
        # удаляется, дубль с историей скрывается. Публичное имя одно.
        await self.data_sync._write(self.db.collapse_hyperliquid_hype)
        catalogs: dict[str, Optional[list]] = {}
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
            if venue not in catalogs:
                try:
                    catalogs[venue] = await self.data_sync.request(venue, adapter.catalog)
                except Exception as exc:
                    catalogs[venue] = None
                    log.warning(
                        "каталог %s недоступен (%s) — seed отложен; известные "
                        "валидные инструменты продолжают работу", venue, exc)
                    get_registry().counter("seed_catalog_fallback", labels={"venue": venue})
            catalog = catalogs[venue]
            if catalog is None:
                self._seed_pending_venues.add(venue)
                continue
            self._seed_pending_venues.discard(venue)
            match = next((c for c in catalog if c.symbol == symbol), None)
            if match is None:
                log.error("инструмент %s:%s не найден в каталоге источника", venue, symbol)
                await self._service_message(
                    f"Инструмент {venue}:{symbol} не найден в каталоге {venue} — "
                    f"проверьте идентификатор, подмена не выполняется."
                )
                continue
            iid = await self.data_sync._write(self.db.upsert_instrument, match)
            log.info("инструмент подключён: %s %s (id=%s)", venue, symbol, iid)
        if self._seed_pending_venues:
            self._schedule_catalog_retry()

    def _schedule_catalog_retry(self) -> None:
        """Фоновый повтор каталога после сетевой ошибки (§11), без блокировки старта."""
        task = self._catalog_retry_task
        if task is not None and not task.done():
            return
        self._catalog_retry_task = asyncio.create_task(self._catalog_retry_loop())

    async def _catalog_retry_loop(self) -> None:
        """Повторяет seed для источников с упавшим каталогом; успешно
        досеянные инструменты сразу проходят backfill (чекпоинты делают
        повторные проходы no-op)."""
        try:
            for _ in range(self.seed_catalog_max_attempts):
                if not self._seed_pending_venues:
                    return
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), self.seed_catalog_retry_seconds)
                    return
                except asyncio.TimeoutError:
                    pass
                known = {i.id for i in self.db.get_instruments()}
                await self.seed_instruments()
                for ins in self.db.get_instruments(enabled_only=True):
                    if ins.id in known:
                        continue
                    self._set_replaying(ins.id, True)
                    try:
                        await self.backfill(ins)
                    except Exception:
                        log.exception("backfill %s после retry каталога", ins.symbol)
                    finally:
                        self._set_replaying(ins.id, False)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("retry каталога источников завершился сбоем")

    # ---------- backfill с чекпоинтами (этап 5, §11) ----------

    def _backfill_checkpoint(self, ins: Instrument, tf: str) -> dict:
        """Чекпоинт канала backfill: done_through_ms — глубина покрытого
        окна истории; completed — структура по окну построена (replay/хвост
        завершены). Рестарт продолжает с чекпоинта, перекрывающийся участок
        сверяется идемпотентной вставкой (детект ревизий), а не перезапуском."""
        raw = self.db.get_meta(f"backfill:checkpoint:{ins.id}:{tf}")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def _save_backfill_checkpoint(self, ins: Instrument, tf: str, *,
                                  done_through_ms: int, completed: bool) -> None:
        prev = self._backfill_checkpoint(ins, tf) if completed else {}
        self.db.set_meta(
            f"backfill:checkpoint:{ins.id}:{tf}",
            json.dumps({
                "done_through_ms": int(done_through_ms),
                "completed": bool(completed),
                "updated_at": now_ms(),
            }),
        )
        if completed and not prev.get("completed"):
            # этап 6a (§13): переход канала в completed — версия истории;
            # повторные сохранения уже завершённого чекпоинта версию не двигают
            self.db.bump_domain(f"seq:history:{ins.id}")

    async def _backfill_tail(self, ins: Instrument, tf: str, adapter,
                             *, progress=None) -> dict:
        """Свежий хвост канала РАНЬШЕ полной истории (§11): котировки и
        сканер сразу видят текущую структуру. Свежий канал не перекачивается
        (повторный backfill — без сети). Первичная загрузка (last is None)
        покрывает окно lookback целиком; её структуру строит полный replay
        в финале (need_replay), а не per-tf replay здесь."""
        result = {"first_load": False, "fresh": [], "before": {}, "need_replay": False}
        last = self.db.last_candle(ins.id, tf)
        lookback_start = now_ms() - self.cfg.lookback_days_for(tf) * 86_400_000
        cp = self._backfill_checkpoint(ins, tf)
        # §11: рестарт от незавершённого чекпоинта — перекрывающийся участок
        # хвоста запрашивается повторно и сверяется (идемпотентная вставка +
        # детект ревизий в insert_candles), а не принимается на доверии
        resume_overlap = bool(cp) and not cp.get("completed") and last is not None
        start = lookback_start
        if last is not None:
            start = max(lookback_start, last.open_time + TIMEFRAME_MS[tf])
            if resume_overlap:
                start = max(lookback_start, last.open_time - TIMEFRAME_MS[tf])
        if start >= now_ms() - TIMEFRAME_MS[tf] and not resume_overlap:
            return result
        if progress:
            progress("source")
        try:
            candles = await self.data_sync.request(
                ins.venue, adapter.klines, ins.symbol, tf, start, now_ms())
        except AdapterError as exc:
            self.data_sync._note_invalid_symbol(ins, exc)
            raise
        # §12 (этап 7): before-снапшот нужен для классификации хвоста
        # (перекрытие резюме — 2 бара, max-агрегаты — последний бар);
        # ограничение в SQL по образцу _ws_closed_kline (limit=512)
        rows = self.db.get_candles(ins.id, tf, limit=512)
        before = {c.open_time: c for c in rows}
        fresh = [
            c for c in candles
            if last is None or c.open_time > last.open_time
        ]
        # перекрытие при резюме: уже записанные свечи вставляются повторно —
        # вставка идемпотентна, расхождение OHLC журналируется как ревизия
        overlap = [
            c for c in candles
            if resume_overlap and c.closed and last is not None
            and c.open_time <= last.open_time
        ]
        if last is None:
            # полная первичная загрузка покрывает окно целиком
            await self.data_sync._write(
                self.db.set_meta, f"history_from:{ins.id}:{tf}", str(lookback_start))
        # Курсор обработки канала фиксируется до вставки хвоста (как в
        # sync_candles): рестарт между записью и расчётом не пропускает хвост.
        processed_key = f"data_sync:processed:{ins.id}:{tf}"
        if self.db.get_meta(processed_key) is None:
            await self.data_sync._write(
                self.db.set_meta,
                processed_key, str(max((c.close_time for c in rows), default=0)))
        for c in (*fresh, *overlap):
            c.instrument_id = ins.id
        # Контракт storage (этап стабилизации): записи из asyncio-кода, которые
        # могут столкнуться с потоковым расчётом, идут через to_thread —
        # потоковое соединение ждёт busy_timeout вместо мгновенного busy
        if fresh or overlap:
            await self.data_sync._write(self.db.insert_candles, [*fresh, *overlap])
        if fresh:
            log.info("%s %s: догружено %d свечей %s", ins.symbol, ins.venue, len(fresh), tf)
        done_through = min(
            x for x in (cp.get("done_through_ms"), lookback_start)
            if x is not None
        )
        await self.data_sync._write(
            self._save_backfill_checkpoint,
            ins, tf, done_through_ms=done_through,
            completed=bool(cp.get("completed")))
        result.update(
            first_load=last is None,
            fresh=fresh if last is not None else [],
            before=before,
            need_replay=last is None,
        )
        return result

    async def _backfill_history(self, ins: Instrument, tf: str, adapter,
                                *, progress=None) -> Optional[int]:
        """Ранняя история назад от первой сохранённой свечи (окно lookback
        ушло глубже хвоста — один раз на окно, граница запрошенной истории —
        в meta history_from + чекпоинт). Возвращает earliest open_time
        вставленной истории; None — догрузка не потребовалась."""
        if self.db.last_candle(ins.id, tf) is None:
            return None
        lookback_start = now_ms() - self.cfg.lookback_days_for(tf) * 86_400_000
        first = self.db.first_candle(ins.id, tf)
        history_key = f"history_from:{ins.id}:{tf}"
        raw_from = self.db.get_meta(history_key)
        history_from = int(raw_from) if raw_from else None
        if not (
            first is not None
            and first.open_time > lookback_start
            and (history_from is None or lookback_start < history_from)
        ):
            return None
        if progress:
            progress("source")
        try:
            candles = await self.data_sync.request(
                ins.venue, adapter.klines, ins.symbol, tf, lookback_start, first.open_time)
        except AdapterError as exc:
            self.data_sync._note_invalid_symbol(ins, exc)
            raise
        fresh = [c for c in candles if c.open_time < first.open_time]
        for c in fresh:
            c.instrument_id = ins.id
        if fresh:
            await self.data_sync._write(self.db.insert_candles, fresh)
            log.info(
                "%s %s: догружена ранняя история %s — %d свечей",
                ins.symbol, ins.venue, tf, len(fresh),
            )
        await self.data_sync._write(self.db.set_meta, history_key, str(lookback_start))
        # окно покрыто, но структура за расширенную историю ещё не построена —
        # completed взведёт финал после replay
        await self.data_sync._write(
            self._save_backfill_checkpoint,
            ins, tf, done_through_ms=lookback_start, completed=False)
        return min((c.open_time for c in fresh), default=None)

    async def _submit_backfill_replay(self, ins: Instrument):
        """Полный replay окна инструмента через calc_queue (kind="backfill",
        приоритет 4, §9 — live-задания идут раньше); при выключенной очереди
        — прежний прямой вызов. Replay CPU-bound и длинный — внутри
        _calculate он уходит в отдельный поток, event loop и HTTP не ждут."""
        if not self.calc_queue_enabled:
            return await self._calculate(
                self.scanner.replay_instrument, ins.id, None, set(self.scan_tfs))
        accepted = self.calc_queue.enqueue(
            ins.id, self.scan_tfs[0], "backfill",
            input_version=self.db.get_state_seq(),
            payload={"mode": "replay", "timeframes": sorted(self.scan_tfs)},
        )
        if not accepted:
            raise RuntimeError("calc_queue_overloaded")
        return await self.calc_queue.wait_done(ins.id, self.scan_tfs[0], "backfill")

    async def _submit_backfill_tail(self, ins: Instrument, tf: str,
                                    fresh: list[Candle], before: dict,
                                    *, progress=None):
        """Инкрементальная обработка свежего хвоста общим live-путём канала
        (ключ live_* — коалесцируется с boundary/live-заданиями того же
        канала, AC05/AC06 не ломаются). Как и прежний _apply_tail, backfill
        события хвоста не доставляет (§13: восстановленные события delayed) —
        результат возвращается вызывающему без рассылки."""
        if progress:
            progress("calculation")
        if not self.calc_queue_enabled:
            return await self._process_synced_channel(ins, tf, fresh, before)
        return await self._submit_synced_channel(ins, tf, fresh, before, reason="backfill")

    async def backfill(self, ins: Instrument, *, progress=None) -> None:
        """Догрузка истории и replay пропусков (§11; этап 5 — фазы и чекпоинты).

        Фазы: свежий хвост каждого ТФ раньше полной истории (AC01), затем
        догрузка ранней истории назад, затем один расчёт на инструмент.
        Полный replay — только первичная загрузка, догрузка ранней истории,
        ТФ со свечами без единой зоны или канал с незавершённым чекпоинтом
        (прошлый запуск прерван до построения структуры). Новые свечи в
        хвосте (обычный рестарт после деплоя) применяются инкрементально,
        как живой опрос, и не пересобирают уже записанную структуру. Replay
        идемпотентен; восстановленные события получают исходное occurred_at
        и delayed=True.
        """
        adapter = self.adapters[ins.venue]
        need_replay = False
        tails: dict[str, tuple[list[Candle], dict]] = {}
        for tf in self.scan_tfs:
            r = await self._backfill_tail(ins, tf, adapter, progress=progress)
            need_replay = need_replay or r["need_replay"]
            if r["fresh"]:
                tails[tf] = (r["fresh"], r["before"])
        for tf in self.scan_tfs:
            if await self._backfill_history(ins, tf, adapter, progress=progress) is not None:
                need_replay = True
        # replay нужен и при незавершённом прошлом запуске: чекпоинт канала
        # без completed — структура по окну не была достроена. Для баз,
        # обслуживавшихся до чекпоинтов, эвристика «свечи без зон ТФ» под
        # защитой replay_done сохраняется.
        replay_done = bool(self.db.get_meta(f"replay_done:{ins.id}"))
        incomplete = any(
            self.db.last_candle(ins.id, tf) is not None
            and not self._backfill_checkpoint(ins, tf).get("completed")
            for tf in self.scan_tfs
        )
        if not need_replay and not incomplete and not replay_done:
            zones = self.db.get_zones(ins.id)
            for tf in self.scan_tfs:
                if self.db.last_candle(ins.id, tf) and not any(
                    z.timeframe == tf for z in zones
                ):
                    need_replay = True
                    break
        need_replay = need_replay or incomplete
        if need_replay:
            if progress:
                progress("calculation")
            events = await self._submit_backfill_replay(ins)
            await self.data_sync._write(
                self.db.set_meta, f"replay_done:{ins.id}", str(now_ms()))
            log.info(
                "replay %s: свечей обработано, новых событий %d (восстановленных %d)",
                ins.symbol, len(events), sum(1 for e in events if e.delayed),
            )
            # §8: исторические (delayed) события не доставляем — только факт
            # «цена уже в актуальной зоне» на момент подключения инструмента
            fresh_events = [
                e for e in events
                if e.kind == EventKind.ALREADY_IN_ZONE and not e.delayed
            ]
            if fresh_events:
                await self.dispatcher.dispatch(fresh_events)
        elif tails:
            for tf, (fresh, before) in tails.items():
                await self._submit_backfill_tail(ins, tf, fresh, before, progress=progress)
            await self.data_sync._write(
                self.db.set_meta, f"replay_done:{ins.id}", str(now_ms()))
            log.info(
                "%s: хвост %d свечей без полного replay",
                ins.symbol, sum(len(t[0]) for t in tails.values()),
            )
        # Каналы, обслуженные этим проходом (или уже имевшие свечи), помечаются
        # завершёнными: окно покрыто, структура построена (replay выше) либо
        # хвост применён инкрементально. Пустой канал без чекпоинта остаётся
        # pending — его увидит прогресс /api/ready.
        for tf in self.scan_tfs:
            cp = self._backfill_checkpoint(ins, tf)
            if not cp and self.db.last_candle(ins.id, tf) is None:
                continue
            await self.data_sync._write(
                self._save_backfill_checkpoint,
                ins, tf,
                done_through_ms=cp.get(
                    "done_through_ms",
                    now_ms() - self.cfg.lookback_days_for(tf) * 86_400_000,
                ),
                completed=True,
            )

    def _needs_history_recovery(self, ins: Instrument) -> bool:
        """Этап C ручного job (§15): незавершённый чекпоинт backfill или окно
        lookback ушло глубже покрытого — каналу нужно восстановление истории.
        База, обслуженная версией до чекпоинтов (replay_done + history_from
        актуальны), восстановления не требует. Инструмент, отвергнутый
        источником (invalid symbol), не восстанавливается до снятия метки."""
        if ins.id in self.data_sync._invalid_instruments:
            return False
        for tf in self.scan_tfs:
            lookback_start = now_ms() - self.cfg.lookback_days_for(tf) * 86_400_000
            cp = self._backfill_checkpoint(ins, tf)
            if not cp:
                if self.db.get_meta(f"replay_done:{ins.id}"):
                    raw_from = self.db.get_meta(f"history_from:{ins.id}:{tf}")
                    if raw_from and lookback_start >= int(raw_from):
                        continue
                return True
            if not cp.get("completed"):
                return True
            if int(cp.get("done_through_ms") or 0) > lookback_start:
                return True
        return False

    async def _backfill_all(self) -> None:
        """Фоновая догрузка истории при старте (этап 5, §11, AC01): идёт
        параллельно с живыми циклами (котировки/WS не блокируются), по
        инструментам — конкурентно (сеть ограничена per-venue семафором §10).
        Фазы готовности: history — проход завершён; calc — очередь расчётов
        дочитана после него."""
        try:
            instruments = self.db.get_instruments(enabled_only=True)

            async def _one(ins):
                if ins.id in self.data_sync._invalid_instruments:
                    return  # источник отверг инструмент — до job/рестарта (§10)
                await self.data_sync._write(self._set_replaying, ins.id, True)
                try:
                    await self.backfill(ins)
                except Exception:
                    log.exception("backfill %s", ins.symbol)
                finally:
                    await self.data_sync._write(self._set_replaying, ins.id, False)

            await asyncio.gather(*(_one(i) for i in instruments))
            self.readiness.reach("history")
            if self._restore_task is not None:
                await asyncio.shield(self._restore_task)
            await self._wait_calc_idle()
            self.readiness.reach("calc")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("фоновый backfill завершился сбоем")

    async def _wait_calc_idle(self, timeout: float = 600.0) -> None:
        """Дочитка очереди расчётов после backfill (фаза calc). Ограничена
        таймаутом: живой поток live-заданий не должен держать фазу вечно."""
        if not self.calc_queue_enabled:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.calc_queue.depth() == 0 and not self.calc_queue.busy():
                return
            await asyncio.sleep(0.5)

    # ---------- основной цикл ----------

    async def poll_once(self) -> None:
        for ins in self.db.get_instruments(enabled_only=True):
            try:
                await self._poll_instrument(ins)
            except AdapterError as exc:
                log.warning("%s: источник недоступен: %s", ins.symbol, exc)
                await self._mark_stale(ins, str(exc), timeframes=["quote"])
            except Exception:
                log.exception("ошибка опроса %s", ins.symbol)
        await self.dispatcher.retry_pending()

    @instrument_owned
    async def _poll_instrument(self, ins: Instrument) -> None:
        for tf in self.scan_tfs:
            try:
                await self.data_sync.sync_candles(ins, tf)
            except Exception as exc:
                log.warning("%s %s: %s", ins.symbol, tf, exc)
                # sync_candles records failures for this channel. A failed W1
                # request must not invalidate an H1 channel that already
                # recovered (or confuse calculation failures with outages).
                if (isinstance(exc, (AdapterError, asyncio.TimeoutError))
                        and self.data_sync.channel_status(ins, tf).get("error_stage") != "processing"):
                    await self._mark_stale(ins, str(exc), timeframes=[tf])

        # F02: при включённом быстром цикле котировку обновляет он
        # (quote_loop) — HTF-цикл цену не запрашивает; выключен
        # (quote_poll_seconds = 0) — прежний путь, котировка вместе с HTF.
        # Этап 2: котировка ставится в market_queue; события, их доставка
        # и LTF-запуск выполняет потребитель очереди.
        if self.settings.quote_poll_seconds <= 0:
            await self._poll_quote(ins)

    async def _poll_quote(self, ins: Instrument, *, source: str = "rest") -> Optional[QuoteTick]:
        """Котировка инструмента: last_price → быстрый ingest (§8, этап 2).

        Замок инструмента на время сети НЕ держится: persist атомарен и
        монотонно защищён (set_quote_if_fresh), тяжёлая обработка — в
        per-instrument FIFO market_queue. События детектора, их доставка и
        запуск LTF-наблюдений выполняет потребитель очереди. Возвращает
        принятый тик; None — дубль или устаревший тик (состояние не
        двинулось). source — метка источника тика для метрик (rest/ws;
        ws — этап 3)."""
        adapter = self.adapters[ins.venue]
        if ins.id in self.data_sync._invalid_instruments:
            # источник однозначно отверг инструмент — без повторов в цикле (§10)
            return None
        result = dict(self.data_sync.channel_status(ins, "quote"))
        result.update(last_attempt_at=now_ms(), error=None)
        _save = self.data_sync._write
        await _save(self.data_sync._save_channel, ins, "quote", result)
        try:
            tick = await self.data_sync.request(ins.venue, adapter.last_price, ins.symbol)
        except Exception as exc:
            self.data_sync._note_invalid_symbol(ins, exc)
            result.update(error=str(exc)[:300] or type(exc).__name__, last_error_at=now_ms())
            await _save(self.data_sync._save_channel, ins, "quote", result)
            raise
        result.update(last_success_at=now_ms(), error=None)
        await _save(self.data_sync._save_channel, ins, "quote", result)
        await _save(self.dispatcher.services.recovered, ins, "quote")
        await _save(self.db.set_meta, f"stale:{ins.id}:quote", "0")
        return await self._ingest_quote(ins, tick, source)

    async def _ingest_quote(self, ins: Instrument, tick: QuoteTick, source: str) -> Optional[QuoteTick]:
        from .validation import validate_quote
        validate_quote(tick)
        import hashlib
        def accept():
            from .services.runtime import CALC_OWNER, assert_owner
            with self.db.batch_writes():
                current = self.db.get_instrument(ins.id)
                if self._stop.is_set() or current is None or not current.enabled:
                    return None
                claim = getattr(self, "_calc_claim", None)
                if claim: assert_owner(self.db, CALC_OWNER, claim)
                key = [source,tick.source_seq] if tick.source_seq is not None else [source,tick.event_at if tick.event_at is not None else tick.received_at,tick.price]
                dedup = hashlib.sha256(json.dumps(key,separators=(",", ":")).encode()).hexdigest()
                if self.db.conn.execute("SELECT 1 FROM quote_spool WHERE instrument_id=? AND dedup_key=?",(ins.id,dedup)).fetchone():
                    return None
                now = now_ms()
                if not self.db.set_quote_if_fresh(ins.id,tick.price,tick.monotonic_at,source=source,
                    price_kind=tick.price_kind,event_at=tick.event_at,received_at=tick.received_at,
                    source_seq=tick.source_seq,age_ms=now-tick.event_at if tick.event_at is not None else None,
                    detection_lag_ms=now-tick.received_at):
                    return None
                row_id=self.db.spool_quote(ins.id,source=source,event_at=tick.event_at,received_at=tick.received_at,
                    price=tick.price,price_kind=tick.price_kind,source_seq=tick.source_seq)
                candles={}
                for tf in dict.fromkeys((*self.scan_tfs,"H1")):
                    bar=self._apply_price_to_forming(ins.id,tf,tick.price)
                    if bar:
                        candles[tf]={"time":bar.open_time//1000,"open":bar.open,"high":bar.high,"low":bar.low,"close":bar.close}
                return row_id,candles
        result=await asyncio.to_thread(accept)
        if result is None: return None
        row_id,candles=result
        await self.data_sync.mark_instrument_success(ins)
        self.market_queue.enqueue(ins,tick,source,persisted_id=row_id)
        if self.broadcast:
            self.broadcast({"type":"price","instrument_id":ins.id,"price":tick.price,"time":tick.monotonic_at,"candles":candles})
        metrics=get_registry()
        metrics.counter("quote_ticks",labels={"venue":ins.venue,"source":source})
        if tick.event_at is not None:
            metrics.gauge("quote_source_age_s",(tick.received_at-tick.event_at)/1000,{"venue":ins.venue})
        metrics.histogram("quote_receive_to_persist",max(0,(now_ms()-tick.received_at)/1000),{"venue":ins.venue})
        metrics.histogram("quote_receive_to_broadcast",max(0,(now_ms()-tick.received_at)/1000),{"venue":ins.venue})
        return tick


    async def _dispatch_events(self, events: list[Event]) -> None:
        """Доставка событий детектора и их WS-рассылка (одинаково во всех
        циклах — HTF, LTF, котировки)."""
        if not events:
            return
        await self.dispatcher.dispatch(events)
        if self.broadcast:
            for e in events:
                zone = self.db.get_zone(e.zone_id)
                self.broadcast({
                    "type": "event", "zone_id": e.zone_id, "kind": e.kind.value,
                    "price": e.price, "occurred_at": e.occurred_at,
                    "instrument_id": zone.instrument_id if zone else None,
                })

    def _apply_price_to_forming(
        self, instrument_id: int, timeframe: str, price: float
    ) -> Candle | None:
        """Подтянуть high/low/close текущей незакрытой свечи. Детектор не трогаем."""
        bar = self.db.last_candle(instrument_id, timeframe, closed_only=False)
        if bar is None or bar.closed:
            return None
        # Застрявший бар прошлого периода не переписываем ценой нового.
        if not bar_period_contains(bar.open_time, timeframe, now_ms()):
            return None
        updated = replace(
            bar,
            high=max(bar.high, price),
            low=min(bar.low, price),
            close=price,
        )
        self.db.insert_candles([updated])
        return updated

    # ---------- realtime WS Binance (этап 3, спека §7) ----------

    async def _ws_on_trade(self, ins: Instrument, tick: QuoteTick) -> None:
        """WS-тик сделки — тот же быстрый путь, что у REST (source="ws")."""
        await self._ingest_quote(ins, tick, "ws")

    async def _ws_on_kline(self, ins: Instrument, tf: str, candle: Candle) -> None:
        """WS-свеча: формирующая (x=false) — только upsert бара, без расчёта;
        закрытая (x=true) — вставка + обработка как у boundary-синхронизации,
        отдельной задачей под замком инструмента (цикл чтения WS не ждёт)."""
        current = self.db.get_instrument(ins.id)
        if self._stop.is_set() or current is None or not current.enabled:
            return
        candle.instrument_id = ins.id
        if not candle.closed:
            await asyncio.to_thread(self.db.insert_candles, [candle])
            return
        task = asyncio.create_task(self._ws_closed_kline(ins, tf, candle))
        self._ws_background.add(task)
        task.add_done_callback(self._ws_background.discard)

    async def _ws_closed_kline(self, ins: Instrument, tf: str, candle: Candle) -> None:
        """Закрытый бар из WS: insert_candles (идемпотентно, дубликат закрытия
        — не ревизия и не повторная обработка) → тот же путь обработки и
        доставки, что у data_sync.sync_candles после вставки. Этап 4: при
        включённой очереди бар ставится в calc_queue без ожидания (цикл
        чтения WS не ждёт расчёт), доставку делает потребитель очереди."""
        try:
            async with self.data_sync.instrument(ins.id):
                before = {c.open_time: c for c in self.db.get_candles(ins.id, tf, limit=512)}
                # как в sync_candles: курсор обработки фиксируется до вставки,
                # чтобы рестарт между записью и расчётом не пропустил хвост
                processed_key = f"data_sync:processed:{ins.id}:{tf}"
                if tf != "H1" and self.db.get_meta(processed_key) is None:
                    self.db.set_meta(
                        processed_key,
                        str(max((c.close_time for c in before.values()), default=0)),
                    )
                self.db.insert_candles([candle])
                if self.calc_queue_enabled:
                    # этап 4: расчёт — через очередь (приоритет/коалесцинг/
                    # durable-строка); ждёт и доставляет этот фоновый обработчик
                    # (цикл чтения WS свободен), порядок как у sync_candles
                    kind = self._calc_task_kind(tf, "ws_kline")
                    self.calc_queue.enqueue(
                        ins.id, tf, kind,
                        target_cursor=candle.open_time,
                        earliest_revised_open=(
                            None if tf == "H1"
                            else self._recovery_earliest(
                                ins, tf, [candle], before,
                                int(self.db.get_meta(processed_key) or 0))
                        ),
                        input_version=self.db.get_state_seq(),
                    )
                    result = await self.calc_queue.wait_done(ins.id, tf, kind)
                else:
                    result = await self._process_synced_channel(ins, tf, [candle], before)
                await self._deliver_synced_channel(ins, tf, result)
                if self.broadcast:
                    self.broadcast({
                        "type": "candle", "instrument_id": ins.id,
                        "timeframe": tf,
                        # §14 (этап 6b): OHLC закрытого бара — клиент обновляет
                        # график инкрементально, без перечтения всех свечей
                        "open_time": candle.open_time, "open": candle.open,
                        "high": candle.high, "low": candle.low,
                        "close": candle.close,
                    })
        except Exception:
            log.exception(
                "realtime: обработка закрытого бара %s %s не удалась",
                ins.symbol, tf,
            )

    def _ws_on_reconnect(self, ins: Instrument, tfs) -> None:
        """AC10: после (пере)подключения — догрузить возможный разрыв по каждому
        подписанному ТФ. insert_candles идемпотентен, дублей нет; задачи не
        блокируют цикл чтения WS."""
        for tf in tfs:
            task = asyncio.create_task(self._ws_recovery_sync(ins, tf))
            self._ws_background.add(task)
            task.add_done_callback(self._ws_background.discard)

    async def _ws_recovery_sync(self, ins: Instrument, tf: str) -> None:
        try:
            await self.data_sync.sync_candles(ins, tf, "ws_recovery")
        except Exception:
            log.warning(
                "realtime: ws_recovery %s %s не удался", ins.symbol, tf,
                exc_info=True,
            )

    def _realtime_instruments(self) -> list[Instrument]:
        return [
            i for i in self.db.get_instruments(enabled_only=True)
            if i.venue == "binance"
        ]

    def _reconcile_realtime(self) -> None:
        """Живое изменение включённых инструментов и scan_timeframes без
        рестарта: сверка желаемых подписок с фактическими раз в HTF-цикл."""
        tfs = tuple(
            t.strip()
            for t in str(getattr(self.cfg, "scan_timeframes", "")).split(",")
            if t.strip() in TIMEFRAME_MS
        ) or DEFAULT_TIMEFRAMES
        if tfs != self.scan_tfs:
            log.info(
                "scan_timeframes изменены на лету: %s → %s", self.scan_tfs, tfs)
            self.scan_tfs = tfs
        if self.realtime is not None:
            self.realtime.update_subscriptions(
                self._realtime_instruments(), self.scan_tfs)

    async def _note_quote_channel_live(self, ins: Instrument) -> None:
        """Канал котировок жив за счёт WS: пометить успех без REST-запроса
        (сами данные пишет _ingest_quote; раз в цикл опроса, не на тик)."""
        result = dict(self.data_sync.channel_status(ins, "quote"))
        result.update(
            last_attempt_at=now_ms(), last_success_at=now_ms(), error=None)
        await self.data_sync._write(self.data_sync._save_channel, ins, "quote", result)
        await self.data_sync._write(self.dispatcher.services.recovered, ins, "quote")
        await self.data_sync._write(self.db.set_meta, f"stale:{ins.id}:quote", "0")

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
        ошибка одного не останавливает остальных, проблемный уходит в паузу.
        Этап 2: здесь только быстрый ingest; события, доставка и LTF —
        в потребителе market_queue."""
        for ins in self.db.get_instruments(enabled_only=True):
            left = self._quote_skip.get(ins.id, 0)
            if left > 0:
                self._quote_skip[ins.id] = left - 1
                continue
            # этап 3: здоровая WS-подписка делает REST-опрос котировки
            # избыточным (счётчики ошибок не трогаем); нездоровая —
            # REST-фолбэк продолжается автоматически, per-instrument
            if self.realtime is not None and self.realtime.is_healthy(ins.id):
                await self._note_quote_channel_live(ins)
                continue
            try:
                await self._poll_quote(ins)
            except AdapterError as exc:
                log.warning("%s: котировка недоступна: %s", ins.symbol, exc)
                self._quote_on_failure(ins)
                continue
            except Exception:
                log.exception("ошибка опроса котировки %s", ins.symbol)
                self._quote_on_failure(ins)
                continue
            self._quote_on_success(ins.id)

    async def quote_loop(self) -> None:
        """F02/A02: быстрый цикл котировок — короткий заход цены в зону
        между HTF-опросами (poll_seconds) больше не теряется. Интервал
        читается каждый цикл — живое изменение настройки подхватывается."""
        metrics = get_registry()
        expected_wake = time.monotonic()
        while not self._stop.is_set():
            try:
                await self._quote_poll_once()
            except Exception:
                log.exception("ошибка цикла котировок")
            try:
                interval = self.settings.quote_poll_seconds
                expected_wake += interval
                await asyncio.wait_for(self._stop.wait(), interval)
            except asyncio.TimeoutError:
                pass
            # дрейф фактического пробуждения от расписания — лаг event loop
            metrics.gauge("event_loop_lag_s", max(0.0, time.monotonic() - expected_wake))

    # ---------- LTF Confirmations (LTF-спека §4, §13) ----------

    @property
    def _ltf_active(self) -> bool:
        return self.ltf_engine is not None and self.cfg.ltf_enabled

    def _ltf_context_zone_types(self) -> set[ZoneType]:
        """Типы HTF-зон D1/W1, запускающие наблюдение (§16.1).

        Источник — настройка htf_context_types (по умолчанию OB и FVG).
        Тест и рыночная актуальность зоны — из общего движка, специальных
        правил касания для FVG здесь нет."""
        return {
            _LTF_CONTEXT_ZONE_TYPES[t]
            for t in self.cfg.htf_context_type_set()
            if t in _LTF_CONTEXT_ZONE_TYPES
        }

    def _ltf_is_context_zone(self, zone) -> bool:
        # Ручной OB/FVG (type=manual, zone_type=ob|fvg) — тот же родитель.
        allowed = {t.value.upper() for t in self._ltf_context_zone_types()}
        return (
            context_type_name(zone) in allowed
            and zone.timeframe in LTF_PARENT_TIMEFRAMES
        )

    async def _ltf_open_observation(
        self, ins: Instrument, zone, occurred_at: int, *,
        interaction: Optional[str] = None, evidence: Optional[dict] = None,
        seed: bool = True,
    ):
        """Открыть наблюдение за зоной и подгрузить H1-контекст (§4).

        Идемпотентно по UNIQUE(zone_id, cycle_id) (§4, приёмка п.20);
        подгрузка истории — один раз на наблюдение (meta-флаг seeded).
        interaction/evidence — основание контакта для источника эпизода
        (contact_basis): рыночный факт или служебная пометка (manual_watch)."""
        obs = await self._calculate(
            self.ltf_engine.on_htf_zone_touched, ins.id, zone, occurred_at,
            interaction, evidence,
        )
        if obs is None:
            # §7 (R16/R18): зона отработана до новой независимой активации —
            # факт записан в zone_activation, наблюдение не открывается
            log.info(
                "LTF: зона %s исторически отработана до активации — "
                "наблюдение не открывается", zone.id,
            )
            return
        if seed:
            await self._ltf_seed_observations(ins, [obs])
        return obs

    async def _ltf_seed_observations(self, ins, observations):
        pending = [o for o in observations if not self.db.get_meta(f"ltf:h1:seeded:{o.id}")]
        if not pending:
            return
        first = min(pending, key=lambda o: o.activated_at)
        # One instrument replay sees every causally available observation.
        # Enabling 30 parent zones must not replay that history 30 times.
        await self._ltf_load_h1_history(ins, first.activated_at)
        await self._calculate(self.ltf_engine.replay_observation, first.id,
                              start_ms=first.activated_at)
        await self._ltf_mark_seeded(pending)

    async def _ltf_mark_seeded(self, observations):
        def publish():
            with self.db.batch_writes(join_repository_writes=True):
                for obs in observations:
                    self.db.set_meta(f"ltf:h1:seeded:{obs.id}", "1")
        await self.data_sync._write(publish)

    async def _ltf_open_marked_zones(self, ins: Instrument) -> None:
        """Галочка «Анализировать»: наблюдения на все подтверждённые HTF-зоны
        разрешённых типов D1/W1 инструмента без ожидания касания.
        Закрытые наблюдения не воскрешаются."""
        zones = await self._calculate(
            self.db.get_zones, instrument_id=ins.id,
            statuses=list(LTF_PARENT_VALID_STATUSES),
            timeframes=set(LTF_PARENT_TIMEFRAMES),
        )
        opened = []
        for zone in zones:
            # F01: единый допуск. Закрытое пользователем наблюдение уже
            # есть в БД и здесь не воскрешается.
            if not eligible_htf_parent(zone, self.cfg, as_of=now_ms()):
                continue
            existing = await self._calculate(
                self.db.get_ltf_observation_by_zone, zone.id, zone.cycle_id
            )
            if existing is not None:
                continue
            obs = await self._ltf_open_observation(
                ins, zone, self._first_htf_reach_ms(zone),
                # §2: включение «Анализировать» — не рыночный контакт;
                # источник эпизода помечается служебным основанием
                evidence={"contact_basis": "manual_watch"}, seed=False,
            )
            if obs is not None:
                opened.append(obs)
        await self._ltf_seed_observations(ins, opened)
        if opened and self.broadcast:
            self.broadcast({
                "type": "ltf", "instrument_id": ins.id,
                "events": 0, "reason": "observation_opened",
            })

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
        zones = await self._calculate(
            self.db.get_zones, instrument_id=ins.id,
            statuses=list(LTF_PARENT_VALID_STATUSES),
            timeframes=set(LTF_PARENT_TIMEFRAMES),
        )
        opened = []
        for zone in zones:
            if not eligible_htf_parent(zone, self.cfg, as_of=now_ms()):
                continue
            existing = await self._calculate(
                self.db.get_ltf_observation_by_zone, zone.id, zone.cycle_id
            )
            if existing is not None:
                continue
            inside = (
                price is not None and zone.lower <= price <= zone.upper
            )
            events = await self._calculate(
                self.db.get_events, zone_id=zone.id, limit=100
            )
            start_events = [e for e in events if e.kind in LTF_START_KINDS]
            reached = inside or bool(start_events)
            if reached:
                if start_events:
                    occurred = min(e.occurred_at for e in start_events)
                else:
                    occurred = self._first_htf_reach_ms(zone)
                obs = await self._ltf_open_observation(ins, zone, occurred, seed=False)
                if obs is not None:
                    opened.append(obs)
        await self._ltf_seed_observations(ins, opened)

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
        pending_key = f'ltf:h1:realign_pending:{ins.id}'
        pending = json.loads(self.db.get_meta(pending_key) or 'null')
        realigned = []
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
            log.info(
                "LTF: наблюдение %s сдвинуто к фактическому входу %s",
                obs.id, reach,
            )
            realigned.append((reach, obs.id))
        if realigned:
            candidates = realigned + ([(pending['start'], pending['observation'])] if pending else [])
            reach, oid = min(candidates)
            pending = {'start': reach, 'observation': oid, 'generation': now_ms()}
            # Publish the new activation and its unfinished replay together.
            # A restart must not mistake the moved activation for finished work.
            def prepare():
                with self.db.batch_writes(join_repository_writes=True):
                    self.db.set_meta(pending_key, json.dumps(pending))
                    for start, observation_id in realigned:
                        self.db.update_ltf_observation(observation_id, activated_at=start)
            await self.data_sync._write(prepare)
        if pending:
            reach, oid = pending['start'], pending['observation']
            replay_key = f"realign:{pending.get('generation', str(oid)+':'+str(reach))}"
            await self._calculate(self.ltf_engine.replay_observation, oid, start_ms=reach,
                                  resume=True, replay_key=replay_key)
            await self.data_sync._write(self.db.set_meta, pending_key, '')

    @instrument_owned
    async def _ltf_on_poll(self, ins: Instrument, events: list[Event]) -> None:
        """Запуск наблюдений от HTF-событий и контроль валидности родителей.

        Пятидневное подавление повторных HTF-сообщений (notify-слой) сюда не
        доходит и наблюдение не блокирует (§4).
        """
        current = self.db.get_instrument(ins.id)
        if current is None or not current.enabled:
            return
        if not self._ltf_active:
            return
        # §4/§5: проход HTF-источника своим ТФ — факт машины возврата
        # контекста (записывается рыночным временем события)
        from .services import htf_context as _htf_ctx
        for e in events:
            if e.kind not in (EventKind.SOURCE_PASSED, EventKind.OB_INVALIDATED):
                continue
            zone = self.db.get_zone(e.zone_id)
            if zone is not None:
                await self._calculate(
                    _htf_ctx.note_source_outcome, self.db, zone, e.occurred_at, self.cfg
                )
        for e in events:
            if e.kind not in LTF_START_KINDS:
                continue
            zone = self.db.get_zone(e.zone_id)
            # LEVEL_TAKEN завершает уровень в момент события (TAKEN +
            # display_until = occurred_at): допуск родителя оценивается
            # строго до контакта — сам контакт и есть начало наблюдения (R09)
            as_of = (
                e.occurred_at - 1
                if e.kind == EventKind.LEVEL_TAKEN else e.occurred_at
            )
            if not eligible_htf_parent(zone, self.cfg, as_of=as_of):
                continue
            contact_evidence = dict(e.evidence or {})
            # §3/Q18 (R34): контакт по реальной котировке имеет доказанное
            # время — та же H1 может подтвердить сценарий; пересечение между
            # наблюдениями точного времени не имеет
            if contact_evidence.get("observed") and not contact_evidence.get(
                "exact_contact_unknown"
            ):
                contact_evidence.setdefault("order_proven", True)
            await self._ltf_open_observation(
                ins, zone, e.occurred_at,
                evidence=contact_evidence,
            )
        # §4: инвалидация родителя закрывает сценарий (HTF_INVALIDATED);
        # тест 90% и запрет Breaker наблюдение не закрывают (внутри метода)
        for obs in self.db.list_ltf_observations(instrument_id=ins.id):
            if obs.state not in LTF_OPEN_STATES:
                continue
            zone = self.db.get_zone(obs.zone_id)
            if zone is not None:
                await self._calculate(self.ltf_engine.check_parent_validity, zone)

    async def _ltf_load_h1_history(self, ins: Instrument, since_ms: int) -> None:
        adapter = self.adapters[ins.venue]
        start = max(0, since_ms - self.cfg.ltf_history_days * 86_400_000)
        candles = await self.data_sync.request(ins.venue, adapter.klines, ins.symbol, "H1", start, now_ms())
        for c in candles:
            c.instrument_id = ins.id
        if candles:
            await self.data_sync._write(self.db.insert_candles, candles)

    async def ltf_poll_once(self) -> None:
        """Один проход LTF-цикла: общий поток закрытий H1 (§13), без
        отдельного запроса на каждую карточку."""
        if not self._ltf_active:
            return
        for ins in self.db.get_instruments(enabled_only=True):
            try:
                await self._poll_ltf_instrument(ins)
            except Exception:
                log.exception("LTF sync %s", ins.symbol)
        # автоархивация наблюдений без активности (раз в LTF-цикл, один
        # индексированный запрос + работа только по найденным кандидатам)
        if self._ltf_active:
            archived = await self._calculate(
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

    @instrument_owned
    async def _poll_ltf_instrument(self, ins: Instrument) -> None:
        await self._ltf_open_reached_parents(ins)
        await self._realign_waiting_observations(ins)
        if ins.ltf_analyze:
            await self._ltf_open_marked_zones(ins)
        await self.data_sync.sync_candles(ins, "H1")

    # ---------- очередь расчётов (этап 4, спека §9) ----------

    @property
    def calc_queue_enabled(self) -> bool:
        return bool(getattr(self.settings, "calc_queue_enabled", False))

    @staticmethod
    def _calc_task_kind(tf: str, reason: str) -> str:
        """Класс приоритета задания по каналу и источнику синхронизации (§9)."""
        if reason == "ws_recovery":
            return "recovery"
        if reason == "manual":
            return "manual_tail"
        return "live_h1" if tf == "H1" else "live_htf"

    @staticmethod
    def _recovery_earliest(ins, tf, incoming, before, cursor: int = 0) -> Optional[int]:
        """Граница replay для HTF-канала (§9, AC06): первая загрузка
        (before пуст), вставка/дыра за обработанным хвостом или РЕВИЗИЯ
        уже обработанной закрытой свечи (close_time <= курсора). Ревизия
        хвоста за курсором (ещё не обработанного) replay не требует —
        инкрементальный проход возьмёт новые значения сам.
        None — обычный инкрементальный хвост; 0 — replay с начала."""
        last_before = max(before, default=0)
        missing = [c for c in incoming if c.closed and c.open_time not in before]
        revised_behind = [
            c for c in incoming
            if c.closed and c.open_time in before and before[c.open_time].closed
            and (c.open, c.high, c.low, c.close) != (
                before[c.open_time].open, before[c.open_time].high,
                before[c.open_time].low, before[c.open_time].close)
            and c.close_time <= cursor
        ]
        holes = any(c.open_time < last_before for c in missing)
        if before and not holes and not revised_behind:
            return None
        return min(
            (c.open_time for c in (*missing, *revised_behind)), default=0)

    def _calc_deps_hash(self, instrument_id: int, tf: str) -> str:
        """Отпечаток зависимостей расчёта канала (AC07, §9).

        Хэшируется: полный DetectorConfig (asdict JSON — консервативно,
        включая включённые типы зон и scan_timeframes; смена настроек через
        POST /api/settings мутирует тот же объект — следующий расчёт видит
        расхождение сам, отдельной проводки к settings:recalc не нужно),
        флаги инструмента (enabled, ltf_analyze — галочка «Анализировать»
        меняет поведение H1-канала), решения review (count + max id по
        review / review_assessment / boundary_correction — глобальный дешёвый
        прокси; сами записи append-only) и состояние HTF-зон инструмента
        (id/cycle/статус/валидность/допуск/границы/времена). Хранится в meta
        calc:deps:{iid}:{tf} по факту успешного расчёта; расхождение на входе
        принуждает к пересчёту. Ложноположительная инвалидация допустима,
        ложноотрицательная — нет."""
        ins = self.db.get_instrument(instrument_id)
        rev = self.db.conn.execute(
            "SELECT (SELECT COUNT(*) FROM review),"
            "       (SELECT COALESCE(MAX(id),0) FROM review),"
            "       (SELECT COUNT(*) FROM review_assessment),"
            "       (SELECT COALESCE(MAX(id),0) FROM review_assessment),"
            "       (SELECT COUNT(*) FROM boundary_correction),"
            "       (SELECT COALESCE(MAX(id),0) FROM boundary_correction)"
        ).fetchone()
        zones = [
            tuple(r)
            for r in self.db.conn.execute(
                "SELECT id, cycle_id, status, market_validity, entry_eligible,"
                " lower, upper, formed_at, confirmed_at, display_until"
                " FROM zone WHERE instrument_id=? ORDER BY id",
                (instrument_id,),
            ).fetchall()
        ]
        payload = {
            "cfg": asdict(self.cfg),
            "instrument": (
                None if ins is None
                else (bool(ins.enabled), bool(ins.ltf_analyze))
            ),
            "reviews": tuple(rev),
            "zones": zones,
        }
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _htf_unchanged(self, instrument_id: int, tf: str, cursor: int) -> bool:
        """AC05: закрытый хвост канала идентичен тому, что потребил расчёт.

        Проверяется по состоянию БД (не по снапшоту синхронизации): новых
        закрытых свечей за курсором нет, журнал ревизий обработан, и OHLC +
        close_time каждой закрытой свечи до курсора совпадают с отпечатком
        htf:ohlc:{iid}:{tf}, записанным ПОСЛЕ последнего успешного расчёта.
        Отпечаток отстаёт от курсора (крах между advance и записью) —
        расхождение → пересчёт, консервативно."""
        rows = self.db.get_candles(instrument_id, tf)
        if any(c.close_time > cursor for c in rows):
            return False
        revisions_done = int(
            self.db.get_meta(f"htf:candle_revision:last:{instrument_id}") or 0)
        if self.db.latest_candle_revision_at(instrument_id, tf) > revisions_done:
            return False
        try:
            fingerprint = json.loads(
                self.db.get_meta(f"htf:ohlc:{instrument_id}:{tf}") or "{}")
        except json.JSONDecodeError:
            return False
        for c in rows:
            if not c.closed:
                continue
            prev = fingerprint.get(str(c.open_time))
            if prev is None or prev != [c.open, c.high, c.low, c.close, c.close_time]:
                return False
        return True

    async def _store_htf_fingerprints(self, ins, tf, cursor_key: str) -> None:
        """Записать отпечаток потреблённого расчётом хвоста и зависимостей.

        Вызывается только после УСПЕШНОГО завершения обработки канала:
        сбой оставляет старые отпечатки (или их отсутствие), и следующий
        прогон не пропускается (AC18). Запись — в потоке: исполнитель очереди
        работает на цикле событий, где транзитный busy от чужого потокового
        расчёта дал бы мгновенный SQLITE_BUSY (контракт storage)."""
        cursor = int(self.db.get_meta(cursor_key) or 0)
        fingerprint = {
            str(c.open_time): [c.open, c.high, c.low, c.close, c.close_time]
            for c in self.db.get_candles(ins.id, tf)
            if c.closed and c.close_time <= cursor
        }
        await self.data_sync._write(
            self.db.set_meta, f"htf:ohlc:{ins.id}:{tf}", json.dumps(fingerprint))
        # зависимости — посчитанные ПОСЛЕ расчёта: зоны, созданные этим же
        # прогоном, не должны инвалидировать следующий
        deps = self._calc_deps_hash(ins.id, tf)
        await self.data_sync._write(
            self.db.set_meta, f"calc:deps:{ins.id}:{tf}", deps)

    async def _submit_synced_channel(self, ins, tf, incoming, before, *, reason):
        """Постановка канала в очередь расчётов и ожидание результата.

        Семантика синхронной обработки sync_candles сохраняется: вызов
        возвращается после финального прогона задания (включая слитые
        повторы), исключение исполнителя поднимается как есть; доставка и
        freshness-метки выполняет сам sync_candles после wait_done, в прежнем
        порядке."""
        kind = self._calc_task_kind(tf, str(reason))
        earliest = None
        if tf != "H1":
            # детект recovery на продюсере: у него настоящие before/incoming
            cursor = int(self.db.get_meta(f"data_sync:processed:{ins.id}:{tf}") or 0)
            earliest = self._recovery_earliest(ins, tf, incoming, before, cursor)
        target = max((c.open_time for c in incoming if c.closed), default=None)
        accepted = self.calc_queue.enqueue(
            ins.id, tf, kind,
            target_cursor=target,
            earliest_revised_open=earliest,
            input_version=self.db.get_state_seq(),
        )
        if not accepted:
            raise RuntimeError("calc_queue_overloaded")
        return await self.calc_queue.wait_done(ins.id, tf, kind)

    async def _run_calc_task(self, task):
        """Исполнитель очереди расчётов (этап 4).

        Только вычисление: доставка и freshness-метки остаются у вызывающего
        после wait_done (порядок «freshness → доставка» синхронного пути не
        меняется, а блокировки инструмента у продюсера и потребителя не
        встречаются — _ltf_on_poll под @instrument_owned ждал бы замок,
        который держит ждущий продюсер). Контекст incoming/before из payload
        не читается — канал обрабатывается из состояния БД (курсоры + свечи
        уже вставлены продюсером), поэтому восстановленное после краха
        задание равнозначно живому; его результат доставки не имеет — как и
        при крахе синхронного пути, подхват делает retry_pending."""
        ins = self.db.get_instrument(task.instrument_id)
        if ins is None or not ins.enabled:
            log.warning(
                "calc_queue: инструмент %s недоступен — задание %s/%s отброшено",
                task.instrument_id, task.timeframe, task.task_kind)
            return None
        if task.task_kind == "backfill":
            return await self._run_backfill_task(task, ins)
        return await self._process_synced_channel(
            ins, task.timeframe, None, None,
            recovery_earliest=task.earliest_revised_open,
        )

    async def _run_backfill_task(self, task, ins):
        """Стартовое/восстановительное задание истории (этап 5, §11).

        В очереди живёт только РАСЧЁТ: выборка истории батчами и чекпоинты
        остаются у фонового оркестратора Worker.backfill — потребитель
        очереди не должен ждать сеть, пока live-задания стоят за ним (§9).
        Полный replay окна по scan_tfs из payload — тот же вызов
        replay_instrument, что у прежнего синхронного backfill; по факту
        прогона курсоры каналов и отпечатки обновляются (последующие live
        sync пойдут инкрементально, AC05-пропуски работают)."""
        tfs = {
            tf for tf in (task.payload.get("timeframes") or [task.timeframe])
            if tf in TIMEFRAME_MS
        } or {task.timeframe}
        events = await self._calculate(
            self.scanner.replay_instrument, ins.id, None, tfs)
        for tf in tfs:
            key = f"data_sync:processed:{ins.id}:{tf}"
            last = self.db.last_candle(ins.id, tf)
            if last is not None:
                await self.data_sync._write(self.db.set_meta, key, str(last.close_time))
            await self._store_htf_fingerprints(ins, tf, key)
        return events

    async def _process_synced_channel(self, ins, tf, incoming, before, *,
                                      recovery_earliest=None):
        """Обработка синхронизированного канала (H1 или HTF).

        recovery_earliest: None + реальные incoming/before — наследуемый
        детект recovery из снимков до/после вставки (прямой вызов при
        выключенной очереди). Путь очереди: incoming/before=None и ЯВНАЯ
        граница от продюсера (None — recovery не нужно): канал
        обрабатывается из состояния БД, поэтому восстановленное после
        краха задание равнозначно живому."""
        if tf == "H1":
            if self.ltf_engine is None:
                raise RuntimeError("h1_processor_unavailable")
            deps_key = f"calc:deps:{ins.id}:H1"
            cursor = int(self.db.get_meta(f"ltf:h1:last_close:{ins.id}") or 0)
            last = self.db.last_candle(ins.id, "H1")
            revisions_pending = (
                self.db.latest_candle_revision_at(ins.id, "H1")
                > int(self.db.get_meta(f"ltf:h1:revision_done:{ins.id}") or 0)
            )
            deps_now = self._calc_deps_hash(ins.id, "H1")
            # AC05: ни новых закрытий, ни ревизий, ни смены зависимостей —
            # ни движок, ни обходы наблюдений не запускаются
            if (last is not None and last.close_time <= cursor
                    and not revisions_pending
                    and self.db.get_meta(deps_key) == deps_now):
                get_registry().counter(
                    "calc_skipped_unchanged", labels={"timeframe": "H1"})
                return None
            if self._ltf_active:
                await self._ltf_open_reached_parents(ins)
                await self._realign_waiting_observations(ins)
                if ins.ltf_analyze:
                    await self._ltf_open_marked_zones(ins)
            result = await self._calculate(self.ltf_engine.process_h1_close, ins.id)
            observations = [o for o in self.db.list_ltf_observations(instrument_id=ins.id)
                            if o.state in LTF_OPEN_STATES]
            if observations:
                cursor_raw = self.db.get_meta(f"ltf:h1:last_close:{ins.id}")
                if cursor_raw:
                    await self.data_sync._write(
                        self.db.set_meta,
                        f"ltf:h1:scenario_last_close:{ins.id}", cursor_raw)
            await self.data_sync._write(
                self.db.set_meta, deps_key, self._calc_deps_hash(ins.id, "H1"))
            return result
        key = f"data_sync:processed:{ins.id}:{tf}"
        raw_cursor = self.db.get_meta(key)
        cursor = int(raw_cursor or 0)
        if recovery_earliest is None and incoming is not None:
            # Наследуемый детект recovery из снимков до/после вставки.
            last_before = max(before, default=0)
            missing = [c for c in incoming if c.closed and c.open_time not in before]
            recovery = not before or any(c.open_time < last_before for c in missing)
            earliest = min((c.open_time for c in missing), default=None) if recovery else None
        else:
            # Путь очереди: граница вычислена продюсером до постановки.
            recovery = recovery_earliest is not None
            earliest = recovery_earliest
        deps_key = f"calc:deps:{ins.id}:{tf}"
        deps_now = self._calc_deps_hash(ins.id, tf)
        deps_changed = self.db.get_meta(deps_key) != deps_now
        # AC05: закрытый хвост и зависимости не изменились — движок,
        # уведомления и bump версий не запускаются (доставке нечего слать)
        if not recovery and not deps_changed and self._htf_unchanged(ins.id, tf, cursor):
            get_registry().counter("calc_skipped_unchanged", labels={"timeframe": tf})
            return None
        events = []
        processed_new = False
        # Only first load / an internal hole needs historical recovery.
        if recovery:
            events += await self._calculate(self.scanner.replay_instrument, ins.id, earliest, {tf})
            last = self.db.last_candle(ins.id, tf)
            if last:
                await self.data_sync._write(self.db.set_meta, key, str(last.close_time))
        else:
            # Legacy databases had no explicit HTF cursor. Their existing
            # closed history was processed by the startup backfill.
            if raw_cursor is None and before:
                cursor = max(c.close_time for c in before.values())
            # §12 (этап 7): обрабатываются только свечи с close_time > курсора,
            # т.е. open_time > курсор - step + 1 (close_time = open_time+step-1);
            # окно в SQL вместо полного чтения истории, фильтр по close_time
            # сохранён как страж точности
            for candle in self.db.get_candles(
                    ins.id, tf, start_ms=max(0, cursor - TIMEFRAME_MS[tf] + 2)):
                if candle.close_time <= cursor:
                    continue
                events += await self._calculate(self.scanner.on_closed_candle, candle)
                await self.data_sync._write(
                    self.db.set_meta, key, str(candle.close_time))
                processed_new = True
            if not self.db.get_meta(key) and cursor:
                await self.data_sync._write(self.db.set_meta, key, str(cursor))
            if not processed_new and deps_changed:
                # AC07: зависимости изменились при тех же OHLC — принудительный
                # пересчёт свежего хвоста (идемпотентный вызов, дублей зон и
                # событий нет), а не полный replay — границы replay диктуют
                # только курсоры/дыры
                last = self.db.last_candle(ins.id, tf)
                if last is not None:
                    events += await self._calculate(self.scanner.on_closed_candle, last)
        events += await self._calculate(self.scanner.process_candle_revisions, ins.id, now_ms())
        await self._store_htf_fingerprints(ins, tf, key)
        return events

    async def _deliver_synced_channel(self, ins, tf, result):
        if result is None:
            # AC05: пропуск неизменившегося канала — ничего не изменилось,
            # доставлять и перепроверять родителей нечего
            return
        if tf != "H1":
            await self._dispatch_events(result)
            await self._ltf_on_poll(ins, result)
            return
        if result.events and self._ltf_active and self.ltf_dispatcher is not None:
            await self.ltf_dispatcher(result.events)
        if (result.events or result.processed) and self.broadcast:
            self.broadcast({"type": "ltf", "instrument_id": ins.id,
                            "events": len(result.events), "structure": bool(result.processed)})

    async def ltf_loop(self) -> None:
        """Фоновый цикл LTF: закрытая страница анализ не останавливает (§12)."""
        while not self._stop.is_set():
            if self._ltf_active:
                try:
                    await self.ltf_poll_once()
                except Exception as exc:
                    log.exception("ошибка LTF-цикла")
                    try:
                        self.db.set_meta("runtime:last_error", str(exc)[:500])
                    except Exception:
                        log.exception("не удалось записать runtime:last_error")
            try:
                await asyncio.wait_for(self._stop.wait(), self.cfg.ltf_poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def _ltf_restore_all(self) -> None:
        """§13: восстановление после перезапуска — догрузить H1 и replay
        наблюдений; массовые исторические события не доставляются (delayed)."""
        if not self._ltf_active:
            return
        async def restore(ins):
            self._restoring_instruments.add(ins.id)
            try:
                async with self.data_sync.instrument(ins.id):
                    await self._ltf_restore_instrument(ins)
            finally:
                self._restoring_instruments.discard(ins.id)
        await asyncio.gather(*(restore(ins) for ins in self.db.get_instruments(enabled_only=True)))

    async def _ltf_restore_instrument(self, ins):
        observations = [
            o for o in self.db.list_ltf_observations(instrument_id=ins.id)
            if o.state in LTF_OPEN_STATES
        ]
        if not observations:
            # H1-окно и структура нужны графику и без наблюдения (F11).
            # Сбой одного инструмента (сеть, транзитный конфликт записи)
            # не должен обрывать запуск воркера (§11).
            try:
                if self.db.last_candle(ins.id, "H1") is None:
                    try:
                        await self._ltf_load_h1_history(ins, now_ms())
                    except AdapterError as exc:
                        log.warning(
                            "LTF %s: догрузка H1 пропущена: %s", ins.symbol, exc)
                if self.db.last_candle(ins.id, "H1") is not None:
                    await self._calculate(
                        self.ltf_engine.process_h1_close, ins.id
                    )
            except Exception:
                log.exception("LTF %s: стартовая обработка H1 не удалась", ins.symbol)
            return
        await self.data_sync._write(self._set_replaying, ins.id, True)
        try:
            cursor_raw = self.db.get_meta(f"ltf:h1:last_close:{ins.id}")
            last = self.db.last_candle(ins.id, "H1")
            # Курсор уже на последней закрытой H1 — полный прогон
            # десятков тысяч свечей не нужен. Сравнение до вставки хвоста.
            has_checkpoint = cursor_raw is not None and int(cursor_raw) > 0
            start = (
                min(o.activated_at for o in observations)
                - self.cfg.ltf_history_days * 86_400_000
            )
            if last is not None:
                start = max(start, last.open_time + TIMEFRAME_MS["H1"])
            if start < now_ms() - TIMEFRAME_MS["H1"]:
                # §10: вся сеть — через DataSync.request (семафор venue,
                # Retry-After, таймаут), restore не обходит общий лимит
                candles = await self.data_sync.request(
                    ins.venue, self.adapters[ins.venue].klines,
                    ins.symbol, "H1", start, now_ms(),
                )
                for c in candles:
                    c.instrument_id = ins.id
                if candles:
                    await self.data_sync._write(self.db.insert_candles, candles)
            if has_checkpoint:
                # Хвост, если он вставился, проходит инкрементально.
                # Из restore не доставляем: retry заберёт только
                # не-delayed события внутри grace.
                await self._calculate(
                    self.ltf_engine.process_h1_close, ins.id
                )
                log.info(
                    "LTF %s: H1 восстановлен от сохранённого курсора, полный replay пропущен (%d)",
                    ins.symbol, len(observations),
                )
            else:
                # Один replay на инструмент, а не на каждое наблюдение.
                # Без сохранённого курсора историю восстанавливаем delayed.
                await self._calculate(
                    self.ltf_engine.replay_observation, observations[0].id
                )
                log.info("LTF %s: восстановлено наблюдений: %d",
                         ins.symbol, len(observations))
            # Restore processes all open observations. Publish the same
            # scenario checkpoint as normal channel processing only after success.
            last = self.db.last_candle(ins.id, 'H1')
            if last is not None and int(self.db.get_meta(f'ltf:h1:last_close:{ins.id}') or 0) < last.close_time:
                # Boundary intake can append a new closed bar while the
                # historical replay is working on its immutable old sample.
                await self._calculate(self.ltf_engine.process_h1_close, ins.id)
            cursor_raw = self.db.get_meta(f"ltf:h1:last_close:{ins.id}")
            if cursor_raw:
                if self.db.get_meta(f'ltf:h1:realign_pending:{ins.id}'):
                    await self._realign_waiting_observations(ins)
                await self.data_sync._write(
                    self.db.set_meta,
                    f"ltf:h1:scenario_last_close:{ins.id}", cursor_raw,
                )
                await self._ltf_mark_seeded(observations)
        except AdapterError as exc:
            log.warning("LTF %s: восстановление пропущено: %s", ins.symbol, exc)
        except Exception:
            log.exception("LTF: ошибка восстановления %s", ins.symbol)
        finally:
            await self.data_sync._write(self._set_replaying, ins.id, False)

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
                                if cfg.engine_version == "v3":
                                    await self.alt_dispatcher.dispatch_v3()
                                else:
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

    async def alt_live_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.alt_runner is not None and self.settings.alt_config.job_enabled:
                    await self.alt_runner.poll_live()
                    if self.alt_dispatcher is not None and self.settings.alt_config.engine_version == "v3":
                        await self.alt_dispatcher.dispatch_v3()
            except Exception:
                log.exception("ALT W1: live observation failed")
            try:
                await asyncio.wait_for(self._stop.wait(), max(1, self.settings.alt_config.touch_poll_seconds))
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
        # Записи — через поток (контракт storage: async-писатель не должен
        # ловить мгновенный SQLITE_BUSY, пока потоковый расчёт держит
        # блокировку писателя)
        await self.data_sync._write(
            self.db.set_meta, f"stale:{ins.id}:{tf}", "1" if stale else "0")
        key = (ins.id, tf)
        was = self._stale.get(key, False)
        if stale and not was:
            await self.data_sync._write(
                self.dispatcher.services.failed, ins, [tf], "stale")
        elif not stale:
            # Persistent incidents survive process restart; in-memory `was`
            # alone cannot decide whether a recovery is owed.
            await self.data_sync._write(
                self.dispatcher.services.recovered, ins, tf)
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
        with self._replay_lock:
            depth = max(0, self._replay_depth.get(instrument_id, 0) + (1 if on else -1))
            key = f"replaying:{instrument_id}"
            value = "1" if depth else "0"
            with self.db.batch_writes(join_repository_writes=True):
                if self.db.get_meta(key) != value:
                    self.db.set_meta(key, value)
                    self.db.bump_state_seq()
            self._replay_depth[instrument_id] = depth

    async def _mark_stale(self, ins: Instrument, reason: str, *, timeframes=None) -> None:
        """Record an outage only for the affected streams; recovery is per stream.

        Without an explicit list, all scanning timeframes are affected.
        """
        log.warning("Source %s:%s unavailable: %s", ins.venue, ins.symbol, reason)
        # записи — в потоке (контракт storage: busy у цикла событий мгновенный)
        affected = self.scan_tfs if timeframes is None else timeframes
        await self.data_sync._write(self.dispatcher.services.failed, ins, affected, "network")
        for tf in affected:
            self._stale[(ins.id, tf)] = True
            await self.data_sync._write(self.db.set_meta, f"stale:{ins.id}:{tf}", "1")

    async def _service_message(self, text: str) -> None:
        """Сервисное уведомление владельцу (не рыночное событие, §11) —
        общий транспорт диспетчера: журнал delivery, LogSender без токена."""
        await self.dispatcher.notify_service(text)

    async def run(self) -> None:
        from .services.runtime import CALC_OWNER, wait_for_owner
        claim = await wait_for_owner(self.db, CALC_OWNER, {
            "pid": os.getpid(), "role": "calc", "instance_id": getattr(self,"instance_id",None),
        }, self._stop)
        if claim is None:
            return
        self.readiness.reach("web")
        self._calc_claim = claim
        from .services.runtime import lease_heartbeat
        self._lease_task = asyncio.create_task(lease_heartbeat(self.db, CALC_OWNER, self._calc_claim, self._stop))
        self.data_sync.recover_interrupted()
        # ТЗ 07.10.2026 §13: миграция снятых/зеркальных уровней SSL/BSL —
        # идемпотентна (флаг в meta), с бэкапом; выполняется один раз до
        # прогрева кэшей чтения, чтобы снятые уровни не попали в выборки
        try:
            from .services.taken_levels_migration import run as _migrate_taken

            rep = {"counters": {}} if self.db.is_postgres else _migrate_taken(self.settings.db_path)
            if any(rep["counters"].values()):
                log.info("миграция taken_levels: %s", rep["counters"])
                self.db.invalidate_zone_cache()
                self.db._bump_ltf_cache()
        except Exception:
            log.exception("миграция taken_levels не удалась (не критично для старта)")
        await self.seed_instruments()
        # Координатор доступен после захвата расчётного владельца и загрузки
        # инструментов. Начальный backfill может занимать минуты или ждать
        # биржу, но ручная синхронизация уже должна быть доступна в это время.
        self.data_sync.ready = True
        # §15.1.3: довычисление display-полей для зон, созданных до этих
        # правил (идемпотентно)
        mig = migrate_display_fields(self.db)
        if any(mig.values()):
            log.info("миграция display-полей: %s", mig)
        # §11 (этап 5, AC01): сначала живой контур — очереди, цикл котировок
        # и WS поднимаются ДО истории; сохранённый снимок из БД виден сразу,
        # котировки текут, пока backfill идёт фоном. Потребители
        # market_queue обрабатывают восстановленный spool по структуре из БД
        # (после рестарта она актуальна; на пустой БД касаниям просто нет зон).
        self.market_queue.start()
        if self.realtime is not None:
            # этап 3: WS-подписки сразу; recovery-догрузки (AC10) ложатся на
            # структуру из БД, расхождения доберёт backfill/replay
            self.realtime.start(self._realtime_instruments(), self.scan_tfs)
        quote_task = None
        if self.settings.quote_poll_seconds > 0:
            # F02: котировки — отдельным быстрым циклом, до истории
            quote_task = asyncio.create_task(self.quote_loop())
        self.readiness.reach("realtime")
        # Start intake/coordination immediately. Restore owns each instrument's
        # mutex; independent engines allow other instruments to stay live.
        if self.calc_queue_enabled:
            # этап 4: durable-очередь расчётов — строки calc_task, пережившие
            # рестарт (pending/running/failed), поднимаются в обработку после
            # LTF-restore (порядок H1 выше) и до фонового backfill
            self.calc_queue.concurrency = max(self.calc_queue.concurrency,
                min(4, len(self.db.get_instruments(enabled_only=True))))
            self.calc_queue.start()
        self._restore_task = asyncio.create_task(self._ltf_restore_all())
        # История — фоном (§11): свежий хвост раньше полной истории,
        # чекпоинты per (instrument, tf), replay — через calc_queue
        # (kind="backfill", приоритет ниже live).
        self._backfill_task = asyncio.create_task(self._backfill_all())
        log.info("воркер запущен, опрос каждые %s с", self.settings.poll_seconds)
        loops = [self._main_loop(), self.ltf_loop(), self.data_sync.boundary_loop(), self.runtime_control()]
        if self.alt_runner is not None:
            # дневной job «Altcoins D1 accumulation» — независимый цикл (§4)
            loops.append(self.alt_loop())
            loops.append(self.alt_live_loop())
            loops.append(self.alt_runner.live_stream(self._stop))
        loops.append(self.events_loop())
        if quote_task is not None:
            loops.append(quote_task)
        try:
            await asyncio.gather(*loops)
        finally:
            self.data_sync.ready = False
            self._restore_task.cancel()
            await asyncio.gather(self._restore_task, return_exceptions=True)
            if self._backfill_task is not None:
                # остановка: недогруженные чекпоинты остаются incomplete —
                # следующий старт продолжит с них, перекрытие сверится
                self._backfill_task.cancel()
                await asyncio.gather(self._backfill_task, return_exceptions=True)
            if self._catalog_retry_task is not None:
                self._catalog_retry_task.cancel()
                await asyncio.gather(self._catalog_retry_task, return_exceptions=True)

    async def events_loop(self) -> None:
        """Coinglass и оценка событий. Сбой этого цикла не останавливает H1."""
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await asyncio.to_thread(self._events_once)
            except Exception:
                log.exception("events: цикл не удался, расчёт H1 не затронут")
            # The configured period is between cycle starts. Sleeping a full
            # minute after a 40-second calculation made even successful market
            # assessments cross the two-period freshness limit before renewal.
            delay = max(0.0, self.settings.events_poll_seconds - (time.monotonic() - started))
            try:
                await asyncio.wait_for(
                    self._stop.wait(), delay,
                )
            except asyncio.TimeoutError:
                pass

    def _events_once(self) -> None:
        from .events.runner import run_cycle
        run_cycle(self.db, self.settings)

    async def _main_loop(self) -> None:
        while not self._stop.is_set():
            await self.poll_once()
            if self.realtime is not None:
                # этап 3: живое включение/выключение инструментов и смена
                # scan_timeframes подхватываются сверкой подписок раз в цикл
                try:
                    self._reconcile_realtime()
                except Exception:
                    log.exception("realtime: ошибка сверки подписок")
            try:
                await asyncio.wait_for(self._stop.wait(), self.settings.poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def runtime_control(self):
        from .services.runtime import CALC_OWNER, renew_owner
        renewed = 0.0
        while not self._stop.is_set():
            self._reconcile_realtime()
            if time.monotonic()-renewed >= 20:
                await asyncio.to_thread(renew_owner,self.db,CALC_OWNER,self._calc_claim)
                await asyncio.to_thread(self.db.set_meta,"runtime:calc_heartbeat",str(now_ms()))
                renewed=time.monotonic()
            try: await asyncio.wait_for(self._stop.wait(),1)
            except asyncio.TimeoutError: pass

    def stop(self) -> None:
        self.data_sync.ready = False
        self._stop.set()
