"""Ежедневный job модуля «Altcoins D1 accumulation» (ТЗ 07.10.2026 §3, §4,
§15, §16, §18).

Один прогон в сутки (по умолчанию 03:15 Europe/Moscow, после закрытия
UTC-дня — настройки job_hour_msk/job_minute_msk):

1. Вселенная: refresh_universe (снапшот CMC). Ошибка CMC без ни одного
   подтверждённого снапшота → run со статусом "no_universe" (DATA_PENDING,
   топ-300 не выдумывается); ошибка со снапшотом → продолжаем по last_good
   с summary["universe_stale"]=True.
2. Источник на актив: ровно один alt_instrument_source (venue/symbol/quote,
   source_version=1). Выбор пары — venue_preference (binance → bybit),
   внутри площадки quote_preference (USDT → USDC); каталоги кэшируются на
   прогон. История одного source_id никогда не смешивает площадки (§4).
3. История D1: первичная загрузка — пагинация всей доступной истории от
   start_ms=0 (страницы klines до пустого ответа); дальше — инкремент от
   last_closed_ms. Только ЗАКРЫТЫЕ свечи (include_forming=False + фильтр
   close boundary <= now — формирующаяся D1 не хранится, §4). Пропуски дней
   не синтезируются — фиксируются gaps в meta (§4).
4. Движок: AltEngine.process_asset_history по полной сохранённой серии
   (replay идемпотентен — дедуп по origin_key/UNIQUE-ключам alt_event,
   ретраи внутри run не множат рыночные события). Движок/БД синхронные —
   вызов в asyncio.to_thread, чтобы не блокировать event loop.
5. Антиспам (§18): первичная загрузка актива (processing_mode="backfill",
   meta-флаг alt:loaded:{asset}:{source}) НЕ рассылает месяцы старых
   событий — новые alt_event помечаются delivered без отправки, их id
   попадают в summary прогона (сводку сформирует notify-слой). Дальше —
   "live"/"catchup": события остаются pending для диспетчера; пропущенные
   дни восстанавливаются один раз с исходным event_time (его ставит
   движок от закрытия свечи, не от времени доставки).
6. Непрерывность (§3): актив, выпавший из выборки, с НЕтерминальным
   сетапом продолжает обрабатываться с universe_eligible=0; без активного
   сетапа — не обрабатывается (новых сетапов вне вселенной нет).
7. Учёт: один alt_run на исполнение (started/finished/as_of/status/
   processed/errors/summary_json с причинами пропуска/ошибки каждого
   инструмента). Блокировка повторного запуска: активный "running" моложе
   run_lock_stale_hours → "skipped_locked"; старше — "interrupted"
   (восстановление после падения процесса) и новый прогон.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

from ..adapters.base import AdapterError, MarketDataAdapter
from ..config import AltConfig, Settings
from ..db import Database
from ..models import close_boundary_ms, now_ms
from ..models_alt import (
    AltAsset,
    AltCandle,
    AltInstrumentSource,
    AltRun,
    AltState,
)
from .engine import AltEngine
from .universe import refresh_universe

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
MSK = ZoneInfo("Europe/Moscow")

# Терминальные состояния сетапа (§15): завершённый не воскресает и не
# удерживает актив в ежедневной обработке после выхода из вселенной
TERMINAL_STATES = (
    AltState.CANCELLED.value,
    AltState.EXPIRED_NO_RETEST.value,
    AltState.TARGETS_COMPLETED.value,
)

# Статусы прогона, считающиеся «успешным завершением» для расписания:
# no_universe — штатный DATA_PENDING (нет подтверждённого снапшота CMC),
# не повод перезапускать job каждый poll
SCHEDULE_OK_STATUSES = ("ok", "no_universe")


def _csv(value: str) -> list[str]:
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _is_terminal_setup(setup: Any) -> bool:
    return setup.terminated_ms is not None or setup.state in TERMINAL_STATES


def _detect_gaps(stored: Sequence[AltCandle]) -> list[dict[str, int]]:
    """Пропущенные D1 между соседними сохранёнными свечами (§4: пропуски
    фиксируются, но НЕ заполняются придуманными OHLC)."""
    gaps: list[dict[str, int]] = []
    for a, b in zip(stored, stored[1:]):
        missing = int((b.open_time - a.open_time) // DAY_MS - 1)
        if missing > 0:
            gaps.append({
                "from_open_time": a.open_time,
                "to_open_time": b.open_time,
                "missing_days": missing,
            })
    return gaps


class AltRunner:
    """Ежедневный runner модуля альткоинов.

    broadcast — необязательный callable(dict) (WS-хаб сайта): после
    успешного прогона страница получает обновление без ручной перезагрузки
    (§18). Без cmc_api_key runner конструируется, но run_daily завершается
    статусом "no_universe" — спроектированный путь DATA_PENDING.
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        adapters: dict[str, MarketDataAdapter],
        cmc_adapter: Any,
        broadcast: Any = None,
    ):
        self.db = db
        self.settings = settings
        self.adapters = adapters
        self.cmc_adapter = cmc_adapter
        self.broadcast = broadcast

    @property
    def cfg(self) -> AltConfig:
        return self.settings.alt_config

    # ------------------------------------------------------------------
    # Расписание (время МСК на экране/в настройках, внутри — UTC ms, §4)
    # ------------------------------------------------------------------

    def _slot_msk(self, day: datetime) -> datetime:
        """Слот запуска job_hour:job_minute МСК в день `day` (datetime в MSK)."""
        return day.replace(
            hour=self.cfg.job_hour_msk, minute=self.cfg.job_minute_msk,
            second=0, microsecond=0,
        )

    def _last_due_slot(self, now: datetime) -> datetime:
        """Последний наступивший слот расписания (datetime в MSK)."""
        now_msk = now.astimezone(MSK)
        slot = self._slot_msk(now_msk)
        if now_msk < slot:
            slot -= timedelta(days=1)
        return slot

    def next_run_time(self, now: datetime) -> datetime:
        """Следующий штатный запуск (datetime в Europe/Moscow, §16)."""
        now_msk = now.astimezone(MSK)
        slot = self._slot_msk(now_msk)
        if slot <= now_msk:
            slot += timedelta(days=1)
        return slot

    def should_run(self, now: datetime) -> bool:
        """Пора ли запускать job.

        True, если последний УСПЕШНЫЙ прогон (ok/no_universe) старше
        последнего наступившего слота — это и штатное расписание, и
        разовый catch-up пропущенных суток (§18: догоняем один раз).
        Любая попытка в текущем слоте (в т.ч. error) блокирует повтор до
        следующего слота — цикл опроса не превращается в спам ретраями.
        """
        slot_ms = int(self._last_due_slot(now).timestamp() * 1000)
        last_any = self.db.get_latest_alt_run()
        if last_any is not None and last_any.started_ms >= slot_ms:
            return False
        last_ok = self.db.get_latest_alt_run(statuses=SCHEDULE_OK_STATUSES)
        if last_ok is None:
            return True  # первый запуск — сразу (первичная загрузка)
        return last_ok.started_ms < slot_ms

    # ------------------------------------------------------------------
    # Прогон
    # ------------------------------------------------------------------

    async def run_daily(self, trigger: str = "schedule") -> dict[str, Any]:
        """Один дневной прогон с блокировкой повторного запуска (§4 Railway:
        один ежедневный job; ретраи внутри run не множат события — дедуп
        движка по стабильным source_event_id)."""
        cfg = self.cfg
        now = now_ms()
        lock_ms = int(cfg.run_lock_stale_hours * 3_600_000)
        running = self.db.get_running_alt_run()
        if running is not None:
            if now - running.started_ms <= lock_ms:
                log.info("alt: прогон %s ещё выполняется — пропуск", running.id)
                return {
                    "status": "skipped_locked",
                    "locked_by_run_id": running.id,
                }
            # Процесс умер посреди прогона: зависший "running" → interrupted
            self.db.update_alt_run(
                running.id, status="interrupted", finished_ms=now
            )
            log.warning(
                "alt: прогон %s завис дольше %s ч — помечен interrupted",
                running.id, cfg.run_lock_stale_hours,
            )

        as_of = (now // DAY_MS) * DAY_MS  # граница последних закрытых суток UTC
        run = self.db.insert_alt_run(AltRun(
            id=None, started_ms=now, as_of_ms=as_of, status="running"
        ))
        try:
            return await self._run_body(run, trigger, now)
        except Exception as exc:
            log.exception("alt: дневной прогон %s упал", run.id)
            self.db.update_alt_run(
                run.id, status="error", finished_ms=now_ms(),
                summary_json=json.dumps(
                    {"error": f"{type(exc).__name__}: {exc}"},
                    ensure_ascii=False,
                ),
            )
            raise

    async def _run_body(
        self, run: AltRun, trigger: str, now: int
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "trigger": trigger,
            "universe_stale": False,
            "venue_errors": {},
            "per_asset": [],
            "backfill_event_ids": [],
        }

        # --- (a) вселенная ---
        uni = await refresh_universe(self.db, self.cmc_adapter, self.cfg, now)
        if uni.get("empty"):
            # Нет ни одного подтверждённого снапшота CMC — анализ недоступен
            # (DATA_PENDING), список не выдумывается (§3)
            summary["error"] = uni.get("error")
            self.db.update_alt_run(
                run.id, status="no_universe", finished_ms=now_ms(),
                summary_json=json.dumps(summary, ensure_ascii=False),
            )
            return {
                "status": "no_universe", "run_id": run.id,
                "error": uni.get("error"), "summary": summary,
            }
        summary["universe_stale"] = bool(uni.get("stale"))
        if uni.get("stale"):
            summary["universe_error"] = uni.get("error")
        summary["universe_snapshot_id"] = uni.get("snapshot_id")

        # --- (b) набор активов: выборка + непрерывность наблюдения (§3) ---
        targets = self._target_assets(uni["included_cmc_ids"])

        catalogs: dict[str, Optional[set[str]]] = {}
        processed = errors = 0
        for asset, in_universe in targets:
            entry: dict[str, Any] = {
                "asset_id": asset.id, "symbol": asset.symbol,
                "cmc_rank": asset.cmc_rank, "in_universe": in_universe,
            }
            try:
                src, reason = await self._ensure_source(
                    asset, catalogs, summary["venue_errors"]
                )
                if src is None:
                    entry.update(status="skipped", reason=reason)
                    summary["per_asset"].append(entry)
                    continue
                entry["venue"] = src.venue
                entry["pair"] = src.symbol

                load = await self._load_history(src)
                entry["candles_added"] = load["added"]
                entry["candles_stored"] = load["stored"]
                entry["history_scope"] = src.history_scope
                if load["gaps"]:
                    entry["gaps"] = load["gaps"]

                mode, marked = await self._process_asset(
                    asset, src, load["new_days"], now
                )
                entry["mode"] = mode
                entry["events_marked_delivered"] = len(marked)
                summary["backfill_event_ids"].extend(marked)

                self._update_universe_flags(asset, in_universe, now)
                entry["status"] = "processed"
                processed += 1
            except AdapterError as exc:
                entry.update(status="error", reason=str(exc))
                errors += 1
            except Exception as exc:
                log.exception("alt: ошибка обработки %s", asset.symbol)
                entry.update(
                    status="error", reason=f"{type(exc).__name__}: {exc}"
                )
                errors += 1
            summary["per_asset"].append(entry)

        summary["processed"] = processed
        summary["errors"] = errors
        summary["skipped"] = sum(
            1 for e in summary["per_asset"] if e["status"] == "skipped"
        )
        self.db.update_alt_run(
            run.id, status="ok", finished_ms=now_ms(), processed=processed,
            errors=errors,
            universe_snapshot_id=uni.get("snapshot_id"),
            summary_json=json.dumps(summary, ensure_ascii=False),
        )
        return {
            "status": "ok", "run_id": run.id, "processed": processed,
            "errors": errors, "as_of_ms": run.as_of_ms, "summary": summary,
        }

    def _target_assets(
        self, included_cmc_ids: Sequence[int]
    ) -> list[tuple[AltAsset, bool]]:
        """(asset, in_universe): текущая выборка + выпавшие активы с
        нетерминальными сетапами (§3: наблюдение до завершения сетапа,
        «вне текущей выборки»). Выпавший актив БЕЗ активного сетапа сюда
        не попадает — новых сетапов вне вселенной не открываем."""
        targets: list[tuple[AltAsset, bool]] = []
        seen: set[int] = set()
        for cmc_id in included_cmc_ids:
            asset = self.db.get_alt_asset_by_cmc_id(cmc_id)
            if asset is None or not asset.enabled:
                continue
            targets.append((asset, True))
            seen.add(asset.id)
        for asset in self.db.list_alt_assets(enabled_only=True):
            if asset.id in seen:
                continue
            setups = self.db.list_alt_setups(asset.id)
            if any(not _is_terminal_setup(s) for s in setups):
                targets.append((asset, False))
        return targets

    # ------------------------------------------------------------------
    # (b) выбор пары / источника
    # ------------------------------------------------------------------

    async def _catalog_symbols(
        self,
        venue: str,
        cache: dict[str, Optional[set[str]]],
        venue_errors: dict[str, str],
    ) -> Optional[set[str]]:
        """Символы спотового каталога площадки, кэш на прогон. Ошибка
        каталога — None (площадка в этом прогоне пропускается, причина —
        в venue_errors)."""
        if venue in cache:
            return cache[venue]
        adapter = self.adapters.get(venue)
        if adapter is None:
            cache[venue] = None
            return None
        try:
            catalog = await adapter.catalog()
            cache[venue] = {i.symbol for i in catalog}
        except AdapterError as exc:
            log.warning("alt: каталог %s недоступен: %s", venue, exc)
            venue_errors[venue] = str(exc)
            cache[venue] = None
        return cache[venue]

    async def _ensure_source(
        self,
        asset: AltAsset,
        catalogs: dict[str, Optional[set[str]]],
        venue_errors: dict[str, str],
    ) -> tuple[Optional[AltInstrumentSource], Optional[str]]:
        """Ровно один источник на актив: существующий переиспользуется
        (история одного source_id не смешивает площадки, §4); новый —
        по venue_preference (binance → bybit), внутри площадки по
        quote_preference (USDT → USDC), source_version=1."""
        existing = self.db.get_alt_instrument_source(asset.id)
        if existing is not None:
            if existing.venue not in self.adapters:
                return None, f"venue_unavailable:{existing.venue}"
            return existing, None
        quotes = [q.upper() for q in _csv(self.cfg.quote_preference)]
        base = asset.symbol.upper()
        for venue in _csv(self.cfg.venue_preference):
            if venue not in self.adapters:
                continue
            symbols = await self._catalog_symbols(venue, catalogs, venue_errors)
            if symbols is None:
                continue
            for quote in quotes:
                symbol = f"{base}{quote}"
                if symbol in symbols:
                    src = self.db.upsert_alt_instrument_source(
                        AltInstrumentSource(
                            id=None, asset_id=asset.id, venue=venue,
                            symbol=symbol, quote=quote, source_version=1,
                        )
                    )
                    return src, None
        return None, "no_spot_pair"

    # ------------------------------------------------------------------
    # (c) загрузка истории D1
    # ------------------------------------------------------------------

    async def _fetch_d1_closed(
        self, adapter: MarketDataAdapter, symbol: str, start_ms: int, end_ms: int
    ) -> list[Any]:
        """Пагинация klines до пустого ответа; на выходе — только ЗАКРЫТЫЕ
        D1 по возрастанию (§4: незакрытая свеча не участвует, сортировка
        не зависит от порядка ответа API)."""
        out: dict[int, Any] = {}
        cursor = max(0, start_ms)
        while cursor <= end_ms:
            page = await adapter.klines(
                symbol, "D1", cursor, end_ms, include_forming=False
            )
            fresh = [
                c for c in page
                if cursor <= c.open_time <= end_ms and c.open_time not in out
            ]
            for c in fresh:
                out[c.open_time] = c
            if not fresh:
                break
            cursor = max(c.open_time for c in fresh) + 1
        now = now_ms()
        closed = [
            c for c in out.values()
            if getattr(c, "closed", True)
            and close_boundary_ms(c.open_time, "D1") <= now
        ]
        closed.sort(key=lambda c: c.open_time)
        return closed

    async def _load_history(self, src: AltInstrumentSource) -> dict[str, Any]:
        """Первичная загрузка — вся доступная история от 0; дальше —
        инкремент от last_closed_ms (последняя закрытая свеча перечитывается,
        INSERT OR IGNORE дедуплицирует). Границы истории и gaps — в строке
        источника / meta; свечи не синтезируются (§4)."""
        adapter = self.adapters[src.venue]
        now = now_ms()
        prev_last = src.last_closed_ms
        start = prev_last if prev_last else 0
        closed = await self._fetch_d1_closed(adapter, src.symbol, start, now)
        added = self.db.insert_alt_candles([
            AltCandle(
                source_id=src.id, open_time=c.open_time, open=c.open,
                high=c.high, low=c.low, close=c.close,
                volume=float(getattr(c, "volume", 0.0) or 0.0),
            )
            for c in closed
        ])
        stored = self.db.get_alt_candles(src.id)
        gaps = _detect_gaps(stored)
        if stored:
            if not src.earliest_available_ms:
                src.earliest_available_ms = stored[0].open_time
            src.last_closed_ms = stored[-1].open_time
            if not prev_last:
                # Первичная пагинация от 0 завершена без ошибок — подтверждена
                # вся доступная у площадки история (§4)
                src.history_scope = "full"
            self.db.upsert_alt_instrument_source(src)
        self.db.set_meta(
            f"alt:gaps:{src.asset_id}:{src.id}", json.dumps(gaps)
        )
        new_days = (
            sum(1 for c in stored if c.open_time > prev_last)
            if prev_last else 0
        )
        return {
            "added": added, "stored": len(stored),
            "gaps": gaps, "new_days": new_days,
        }

    # ------------------------------------------------------------------
    # (d–f) движок, антиспам, флаги вселенной
    # ------------------------------------------------------------------

    async def _process_asset(
        self, asset: AltAsset, src: AltInstrumentSource, new_days: int, now: int
    ) -> tuple[str, list[int]]:
        """Replay движка по полной сохранённой серии (идемпотентен — это
        штатный режим) + антиспам первичной загрузки (§18).

        Возвращает (processing_mode, ids событий, помеченных delivered
        без отправки): backfill — первая загрузка актива; catchup —
        восстановление пропущенных дней; live — обычный дневной шаг.
        """
        mode_key = f"alt:loaded:{asset.id}:{src.id}"
        backfill = self.db.get_meta(mode_key) is None
        mode = "backfill" if backfill else ("catchup" if new_days > 1 else "live")

        candles = self.db.get_alt_candles(src.id)
        engine = AltEngine(self.db, self.cfg)
        await asyncio.to_thread(
            engine.process_asset_history, asset.id, src.id, candles, now
        )

        marked: list[int] = []
        if backfill:
            # §18: первичная загрузка/replay не рассылает месяцы старых
            # событий — помечаем delivered без отправки; id — в summary
            # прогона (per-run сводку сформирует notify-слой)
            setup_ids = {s.id for s in self.db.list_alt_setups(asset.id)}
            for e in self.db.pending_alt_events(limit=100_000):
                if e.setup_id in setup_ids:
                    self.db.mark_alt_event_delivered(e.id)
                    marked.append(e.id)
            self.db.set_meta(
                mode_key, json.dumps({"at_ms": now, "candles": len(candles)})
            )
        return mode, marked

    def _update_universe_flags(
        self, asset: AltAsset, in_universe: bool, now: int
    ) -> None:
        """universe_eligible на активных сетапах (§3): выпавший из выборки
        актив дожимает сетап с флагом «вне текущей выборки»."""
        for s in self.db.list_alt_setups(asset.id):
            if _is_terminal_setup(s):
                continue
            if bool(s.universe_eligible) != in_universe:
                self.db.update_alt_setup(
                    s.id, universe_eligible=in_universe, updated_ms=now
                )
