"""Тонкий слой SQLite: миграции + репозитории. Источник истины для подавления,
визитов и состояний — перезапуск процесса ничего не сбрасывает (§8, §13.12).
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .models import (
    AlertState,
    BoundaryCorrection,
    Candle,
    Delivery,
    Direction,
    Event,
    EventKind,
    InnerLevel,
    Instrument,
    Review,
    ReviewAssessment,
    Visit,
    Zone,
    ZoneRelation,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from .models_ltf import (
    LtfEntryZone,
    LtfEvent,
    LtfLiquidityTest,
    LtfMovement,
    LtfObservation,
    LtfPivot,
    LtfRange,
    LtfReview,
    LtfReviewAssessment,
    LtfScenario,
    LtfScenarioEntry,
    LtfStructureEvent,
)
from .models_alt import (
    AltAsset,
    AltCandle,
    AltEntryOpportunity,
    AltEvent,
    AltFrozenRange,
    AltInstrumentSource,
    AltManipulationEpisode,
    AltRangeCandidate,
    AltRangeEpisode,
    AltRun,
    AltSetup,
    AltStructureEvent,
    AltSweepEpisode,
)

SCHEMA_VERSION = 1
_SCHEMA_PATH = Path(__file__).with_name("schema.sql")


class _LockedConnection:
    """Сериализует доступ к sqlite3-соединению из разных потоков.

    replay движка работает в отдельном потоке (asyncio.to_thread), а
    HTTP-обработчики и доставка — в основном. check_same_thread=False лишь
    разрешает такое использование, но гонки по курсору/транзакции не убирает
    (OperationalError: not an error) — нужен замок.
    """

    def __init__(self, conn: sqlite3.Connection):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_lock", threading.RLock())

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(object.__getattribute__(self, "_conn"), name, value)

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(*args, **kwargs)

    def executemany(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executemany(*args, **kwargs)

    def executescript(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.executescript(*args, **kwargs)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Database:
    def __init__(self, path: str = ":memory:", *, ltf_cache: bool = True):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(path, check_same_thread=False, timeout=15.0)
        raw.row_factory = sqlite3.Row
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("PRAGMA busy_timeout = 15000")
        self.conn = _LockedConnection(raw)
        self.path = path
        # Кэш горячих LTF-чтений: replay движка на каждой H1-свече повторяет
        # одни и те же выборки (list_ltf_scenario_entries, list_ltf_pivots,
        # list_ltf_events и др.) — per-candle N+1 превращал startup-restore в
        # часы. Инвалидация — версией: _bump_ltf_cache() вызывается из каждого
        # метода записи в ltf_* таблицы, поэтому читатель всегда видит данные
        # не старше последней записи ЭТОГО процесса (как и _zone_cache).
        # Списки возвращаются копией; объекты разделяются — не мутировать без
        # последующего update_* (он инвалидирует кэш). ltf_cache=False —
        # полный байпас (бенчмарк/проверка идентичности replay).
        self.ltf_cache_enabled = ltf_cache
        self._ltf_cache_lock = threading.RLock()
        self._ltf_cache_version = 0
        self._ltf_cache: dict[tuple, tuple[int, Any]] = {}
        # Кэш выборок get_zones: сканер при реплее дёргает её несколько раз
        # на свечу, каждый раз десериализуя все зоны. Инвалидация — любой
        # записью в zone (insert_zone/update_zone/invalidate_zone_cache).
        # Возвращаемые Zone разделяются между вызовами — не мутировать.
        self._zone_cache_lock = threading.RLock()
        self._zone_cache_version = 0
        self._zone_cache: dict[tuple, tuple[int, list[Zone]]] = {}
        # Десериализация Zone (json evidence и пр.) — дорогая на больших БД;
        # кэш объектов по id с отпечатком сырых полей строки: повторные
        # выборки неизменных зон не тратят время на _to_zone. Инвалидация по
        # отпечатку — любое изменение строки даёт новый fingerprint.
        self._zone_obj_cache: dict[int, tuple[int, Zone]] = {}
        # Кэши горячих чтений HTF-движка (per-candle N+1 на replay, см. LTF
        # прецедент выше): визиты/уровни/события перечитывались на каждой
        # свече для каждой зоны. Инвалидация — версией, bump из методов записи
        # (open/close/update_visit, insert/update_inner_level, insert_event).
        # Списки возвращаются копией; объекты разделяются — не мутировать без
        # последующего update_* (он инвалидирует кэш).
        self._read_cache_lock = threading.RLock()
        self._visit_versions: dict[int, int] = {}
        self._visits_cache: dict[tuple, tuple[int, Any]] = {}
        self._inner_versions: dict[int, int] = {}
        self._inner_global_version = 0
        self._inner_cache: dict[tuple, tuple[int, Any]] = {}
        self._event_versions: dict[tuple[int, int], int] = {}
        self._event_cache: dict[tuple, tuple[int, Any]] = {}
        # batch-режим записи (replay): per-write commit'ы и инкременты
        # state_seq откладываются до выхода из batch_writes — один commit
        # на весь прогон вместо тысяч fsync. Живой путь (одна свеча) не в
        # батче — семантика per-write commit сохраняется.
        self._batch_lock = threading.RLock()
        self._batch_depth = 0
        self._read_depth = 0
        self._state_seq_dirty = False
        # Межпроцессная инвалидация: кэши этого процесса сбрасываются,
        # если чужой писатель сдвинул state_seq. Проверка не чаще 0.5 с
        # и не внутри batch_writes. :memory: — один процесс, проверки нет.
        self._epoch_lock = threading.Lock()
        self._epoch_checked_at = 0.0
        self._seen_epoch: Optional[int] = None
        self.migrate()
        if path != ":memory:":
            self._seen_epoch = self.get_state_seq()

    def _commit(self) -> None:
        """commit с учётом batch-режима: внутри batch_writes отложен."""
        if self._batch_depth == 0:
            self.conn.commit()

    @contextlib.contextmanager
    def batch_writes(self):
        """Массовая запись (replay инструмента): per-write commit'ы и
        инкременты state_seq откладываются — один commit и не более одного
        инкремента state_seq на весь батч. Инвалидация читающих кэшей при
        этом остаётся немедленной (записи и чтения replay перемежаются).

        Безопасность: соединение одно (_LockedConnection), транзакция sqlite
        одна на батч; исключение откатывает батч целиком и сбрасывает
        in-memory кэши (могли наполниться данными откаченных строк). Вложенные
        батчи учитываются счётчиком — commit только на внешнем выходе.
        """
        with self._batch_lock:
            self._batch_depth += 1
        try:
            yield
        except BaseException:
            with self._batch_lock:
                self._batch_depth -= 1
                if self._batch_depth == 0:
                    self._state_seq_dirty = False
                    self.conn._conn.rollback()
                    self._drop_read_caches()
            raise
        with self._batch_lock:
            self._batch_depth -= 1
            if self._batch_depth == 0:
                if self._state_seq_dirty:
                    self._state_seq_dirty = False
                    self._write_state_seq()
                    self._seen_epoch = self.get_state_seq()
                self.conn.commit()

    def _note_external_epoch(self) -> None:
        """Сбросить кэши, если state_seq изменил другой процесс."""
        if self.path == ":memory:" or self._batch_depth:
            return
        now = time.monotonic()
        with self._epoch_lock:
            if now - self._epoch_checked_at < 0.5:
                return
            self._epoch_checked_at = now
            seen = self._seen_epoch
        try:
            seq = self.get_state_seq()
        except Exception:
            return
        if seen is None or seq == seen:
            with self._epoch_lock:
                if self._seen_epoch is None:
                    self._seen_epoch = seq
            return
        with self._epoch_lock:
            self._seen_epoch = seq
        self._drop_read_caches()

    def _drop_read_caches(self) -> None:
        """Полный сброс in-memory кэшей чтения (после rollback батча)."""
        with self._zone_cache_lock:
            self._zone_cache_version += 1
            self._zone_cache.clear()
        self._zone_obj_cache.clear()
        with self._ltf_cache_lock:
            self._ltf_cache_version += 1
            self._ltf_cache.clear()
        with self._read_cache_lock:
            self._visits_cache.clear()
            self._inner_cache.clear()
            self._event_cache.clear()

    def migrate(self) -> None:
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if not cur.fetchone():
            self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            self.conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
            from .events.schema import apply_events_schema
            apply_events_schema(self.conn)
            self.conn.commit()
        else:
            # идемпотентные дополнения схемы для существующих БД
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            cols = {
                r["name"] for r in self.conn.execute("PRAGMA table_info(visit)").fetchall()
            }
            if "exit_kind" not in cols:
                self.conn.execute("ALTER TABLE visit ADD COLUMN exit_kind TEXT")
            # ТЗ «Единый движок» §4: экстремум и исходная глубина захода
            if "extreme" not in cols:
                self.conn.execute("ALTER TABLE visit ADD COLUMN extreme REAL")
            if "d_raw" not in cols:
                self.conn.execute("ALTER TABLE visit ADD COLUMN d_raw REAL")
            ins_table = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='instrument'"
            ).fetchone()
            ins_cols = (
                {
                    r["name"]
                    for r in self.conn.execute("PRAGMA table_info(instrument)").fetchall()
                }
                if ins_table else set()
            )
            if ins_table and "ltf_analyze" not in ins_cols:
                self.conn.execute(
                    "ALTER TABLE instrument ADD COLUMN ltf_analyze INTEGER NOT NULL DEFAULT 1"
                )
            # LTF-анализ включён по умолчанию: одноразово включаем для всех
            # уже заведённых инструментов (ручное снятие галочки дальше сохраняется)
            if ins_table and not self.conn.execute(
                "SELECT 1 FROM meta WHERE key='ltf_analyze_default_on'"
            ).fetchone():
                self.conn.execute("UPDATE instrument SET ltf_analyze=1")
                self.conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('ltf_analyze_default_on', '1')"
                )
            # §15: раздельная оценка ревью и версионированные правки границ.
            # CREATE TABLE IF NOT EXISTS — без потерь для существующих данных.
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS review_assessment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id INTEGER NOT NULL REFERENCES zone(id),
                    review_id INTEGER NOT NULL REFERENCES review(id),
                    review_decision TEXT NOT NULL,
                    geometry_verdict TEXT NOT NULL,
                    lifecycle_verdict TEXT,
                    reason_code TEXT NOT NULL DEFAULT '',
                    evidence_source TEXT NOT NULL DEFAULT 'manual_ui',
                    assessed_as_of INTEGER NOT NULL DEFAULT 0,
                    reviewed_at INTEGER NOT NULL DEFAULT 0,
                    requires_clarification INTEGER NOT NULL DEFAULT 0
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_review_assessment_zone "
                "ON review_assessment (zone_id)"
            )
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS boundary_correction (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id INTEGER NOT NULL REFERENCES zone(id),
                    boundary_version INTEGER NOT NULL,
                    original_lower REAL NOT NULL,
                    original_upper REAL NOT NULL,
                    corrected_lower REAL NOT NULL,
                    corrected_upper REAL NOT NULL,
                    anchor_candle_open_time INTEGER,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL DEFAULT 0
                )"""
            )
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_boundary_correction_zone "
                "ON boundary_correction (zone_id)"
            )
            # §15.1.3/§15.6: display-поля и состояние Breaker-машины —
            # типизированные колонки (раньше жили ключами в evidence JSON)
            zone_cols = {
                r["name"] for r in self.conn.execute("PRAGMA table_info(zone)").fetchall()
            }
            for col, ddl in (
                ("display_from", "INTEGER"),
                ("display_until", "INTEGER"),
                ("end_reason", "TEXT"),
                ("breakout_close_at", "INTEGER"),
                ("breaker_forbidden", "INTEGER NOT NULL DEFAULT 0"),
                ("breakout_expired", "INTEGER NOT NULL DEFAULT 0"),
                # §15.7: статус не проверен по полной истории — нужен replay
                ("needs_replay", "INTEGER NOT NULL DEFAULT 0"),
                # ТЗ «Единый движок» (22.09.2026) §3/§7
                ("market_validity", "TEXT NOT NULL DEFAULT 'active'"),
                ("max_test_depth", "REAL NOT NULL DEFAULT 0"),
                ("has_tests", "INTEGER NOT NULL DEFAULT 0"),
                ("entry_eligible", "INTEGER NOT NULL DEFAULT 1"),
                ("anchor_time", "INTEGER"),
                ("zone_type", "TEXT"),
                ("test_extreme", "REAL"),
            ):
                if col not in zone_cols:
                    self.conn.execute(f"ALTER TABLE zone ADD COLUMN {col} {ddl}")
            # бэкфилл из evidence старых строк (json_extract вернёт NULL — ок)
            self.conn.execute(
                """UPDATE zone SET
                     display_from = COALESCE(display_from,
                         json_extract(evidence, '$.display_from')),
                     display_until = COALESCE(display_until,
                         json_extract(evidence, '$.display_until')),
                     end_reason = COALESCE(end_reason,
                         json_extract(evidence, '$.end_reason')),
                     breakout_close_at = COALESCE(breakout_close_at,
                         json_extract(evidence, '$.breakout_close_at')),
                     breaker_forbidden = MAX(breaker_forbidden,
                         COALESCE(json_extract(evidence, '$.breaker_forbidden'), 0)),
                     breakout_expired = MAX(breakout_expired,
                         COALESCE(json_extract(evidence, '$.breakout_expired'), 0))"""
            )
            # LTF Confirmations (§12 LTF-спеки): вся схема написана через
            # CREATE ... IF NOT EXISTS, поэтому файл безопасно применить целиком —
            # существующие таблицы/индексы не затрагиваются, добавляются только
            # недостающие (включая ltf_* в старых БД).
            self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            # ТЗ «Единый движок» §3: история глубины тестов Entry Zone —
            notification_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(notification_member)")}
            if "reason" not in notification_cols:
                self.conn.execute("ALTER TABLE notification_member ADD COLUMN reason TEXT")
            # CREATE IF NOT EXISTS не добавляет колонки в существующие таблицы
            ltf_ez_cols = {
                r["name"]
                for r in self.conn.execute(
                    "PRAGMA table_info(ltf_entry_zone)"
                ).fetchall()
            }
            for col, ddl in (
                ("max_test_depth", "REAL NOT NULL DEFAULT 0"),
                ("test_extreme", "REAL"),
            ):
                if col not in ltf_ez_cols:
                    self.conn.execute(
                        f"ALTER TABLE ltf_entry_zone ADD COLUMN {col} {ddl}"
                    )
            # ТЗ «LTF Current Setup» §10: код пригодности привязки
            ltf_se_cols = {
                r["name"]
                for r in self.conn.execute(
                    "PRAGMA table_info(ltf_scenario_entry)"
                ).fetchall()
            }
            if "reason" not in ltf_se_cols:
                self.conn.execute(
                    "ALTER TABLE ltf_scenario_entry "
                    "ADD COLUMN reason TEXT NOT NULL DEFAULT ''"
                )
            # L03: версии расчётов и мягкая замена опор (supersede вместо
            # удаления — исторические ссылки сценариев остаются разрешимыми)
            self.conn.execute(
                """CREATE TABLE IF NOT EXISTS calc_version (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    params TEXT NOT NULL DEFAULT '{}',
                    rule_version TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                )"""
            )
            ltf_p_cols = {
                r["name"]
                for r in self.conn.execute(
                    "PRAGMA table_info(ltf_pivot)"
                ).fetchall()
            }
            for col, ddl in (
                ("calc_version_id", "INTEGER"),
                ("superseded_by", "INTEGER"),
            ):
                if col not in ltf_p_cols:
                    self.conn.execute(
                        f"ALTER TABLE ltf_pivot ADD COLUMN {col} {ddl}"
                    )
            # F01/A01: происхождение LTF-событий (live/catchup/replay) и лаг
            # обнаружения; старые строки получают 'unknown'/0
            ltf_ev_cols = {
                r["name"]
                for r in self.conn.execute(
                    "PRAGMA table_info(ltf_event)"
                ).fetchall()
            }
            for col, ddl in (
                ("processing_mode", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("detection_lag_ms", "INTEGER NOT NULL DEFAULT 0"),
                # ТЗ 07.10.2026 §7: состояние графика события отдельно от
                # состояния события (текст ≠ успешная доставка картинки)
                ("chart_state", "TEXT NOT NULL DEFAULT 'none'"),
                ("chart_attempts", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if col not in ltf_ev_cols:
                    self.conn.execute(
                        f"ALTER TABLE ltf_event ADD COLUMN {col} {ddl}"
                    )
            # §5/§12 (этап 2): причинная цепочка сценария — триггерное
            # событие, движение, структурная эпоха, уровень отмены с
            # происхождением и курсор обработки; старые строки — NULL/эпоха 1
            ltf_sc_cols = {
                r["name"]
                for r in self.conn.execute(
                    "PRAGMA table_info(ltf_scenario)"
                ).fetchall()
            }
            for col, ddl in (
                ("origin_break_event_id", "INTEGER"),
                ("origin_movement_id", "INTEGER"),
                ("structural_epoch_id", "INTEGER NOT NULL DEFAULT 1"),
                ("reverse_break_level_price", "REAL"),
                ("reverse_break_pivot_id", "INTEGER"),
                ("reverse_break_confirmed_at", "INTEGER"),
                ("last_processed_close", "INTEGER"),
            ):
                if col not in ltf_sc_cols:
                    self.conn.execute(
                        f"ALTER TABLE ltf_scenario ADD COLUMN {col} {ddl}"
                    )
            ltf_rng_cols = {
                r["name"]
                for r in self.conn.execute(
                    "PRAGMA table_info(ltf_range)"
                ).fetchall()
            }
            for col, ddl in (
                ("kind", "TEXT NOT NULL DEFAULT 'continuation'"),
                ("anchor_policy", "TEXT"),
                ("structural_epoch_id", "INTEGER NOT NULL DEFAULT 1"),
            ):
                if col not in ltf_rng_cols:
                    self.conn.execute(
                        f"ALTER TABLE ltf_range ADD COLUMN {col} {ddl}"
                    )
            from .events.schema import apply_events_schema
            apply_events_schema(self.conn)
            self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def meta_prefix(self, prefix: str) -> dict[str, str]:
        """Все meta-ключи с этим префиксом. Префикс — литерал, не шаблон."""
        like = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        return {
            r["key"]: r["value"]
            for r in self.conn.execute(
                "SELECT key, value FROM meta WHERE key LIKE ? ESCAPE '\\'",
                (like,),
            ).fetchall()
        }

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._commit()

    # ---------- meta: последние котировки (read model LTF, ТЗ §14) ----------

    def set_quote(self, instrument_id: int, price: float, ts_ms: int) -> None:
        """Последняя котировка инструмента (цена и время последней сделки);
        пишет воркер на live-тике. Положение цены относительно HTF-зон
        сервер считает только по свежей котировке (§6/§14)."""
        self.set_meta(
            f"quote:{instrument_id}",
            json.dumps({"price": price, "ts": ts_ms}),
        )

    def get_quote(self, instrument_id: int) -> Optional[tuple[float, int]]:
        raw = self.get_meta(f"quote:{instrument_id}")
        if not raw:
            return None
        try:
            d = json.loads(raw)
            return float(d["price"]), int(d["ts"])
        except (ValueError, KeyError, TypeError):
            return None

    def get_all_quotes(self) -> dict[int, tuple[float, int]]:
        """Все котировки одним запросом (список инструментов без N+1)."""
        out: dict[int, tuple[float, int]] = {}
        for r in self.conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'quote:%'"
        ).fetchall():
            try:
                d = json.loads(r["value"])
                out[int(r["key"].split(":", 1)[1])] = (
                    float(d["price"]), int(d["ts"])
                )
            except (ValueError, KeyError, TypeError):
                continue
        return out

    def close(self) -> None:
        self.conn.close()

    # ---------- instruments ----------

    def upsert_instrument(self, ins: Instrument) -> int:
        self.conn.execute(
            """INSERT INTO instrument (asset, venue, market_type, symbol, quote_asset, precision, enabled, ltf_analyze)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT (venue, market_type, symbol)
               DO UPDATE SET asset=excluded.asset, quote_asset=excluded.quote_asset,
                             precision=excluded.precision, enabled=excluded.enabled""",
            (ins.asset, ins.venue, ins.market_type, ins.symbol, ins.quote_asset,
             ins.precision, int(ins.enabled), int(ins.ltf_analyze)),
        )
        self._commit()
        row = self.conn.execute(
            "SELECT id FROM instrument WHERE venue=? AND market_type=? AND symbol=?",
            (ins.venue, ins.market_type, ins.symbol),
        ).fetchone()
        return int(row["id"])

    def get_instruments(
        self, enabled_only: bool = False, include_retired: bool = False,
    ) -> list[Instrument]:
        q = "SELECT * FROM instrument"
        if enabled_only:
            q += " WHERE enabled=1"
        rows = [self._to_instrument(r) for r in self.conn.execute(q).fetchall()]
        if include_retired:
            return rows
        return [i for i in rows if "#retired-" not in (i.symbol or "")]

    def get_instrument(self, instrument_id: int) -> Optional[Instrument]:
        r = self.conn.execute(
            "SELECT * FROM instrument WHERE id=?", (instrument_id,)
        ).fetchone()
        return self._to_instrument(r) if r else None

    @staticmethod
    def _to_instrument(r: sqlite3.Row) -> Instrument:
        return Instrument(
            id=r["id"], asset=r["asset"], venue=r["venue"], market_type=r["market_type"],
            symbol=r["symbol"], quote_asset=r["quote_asset"], precision=r["precision"],
            enabled=bool(r["enabled"]), ltf_analyze=bool(r["ltf_analyze"]),
        )

    def set_instrument_enabled(self, instrument_id: int, enabled: bool) -> None:
        self.conn.execute(
            "UPDATE instrument SET enabled=? WHERE id=?", (int(enabled), instrument_id)
        )
        self._commit()

    def set_instrument_active(self, instrument_id: int, active: bool) -> None:
        """Один выключатель актива: опрос и расчёт вместе.

        ``enabled`` останавливает свечи, котировки, LTF и события.
        ``ltf_analyze`` останавливает открытие наблюдений. История
        зон и сценариев не удаляется.
        """
        flag = int(bool(active))
        self.conn.execute(
            "UPDATE instrument SET enabled=?, ltf_analyze=? WHERE id=?",
            (flag, flag, instrument_id),
        )
        self._commit()

    def rename_instrument_symbol(self, instrument_id: int, symbol: str) -> None:
        """Обновляет внешний идентификатор инструмента при миграции адаптера."""
        self.conn.execute(
            "UPDATE instrument SET symbol=? WHERE id=?", (symbol, instrument_id)
        )
        self._commit()

    def delete_empty_instrument(self, instrument_id: int) -> bool:
        """Удаляет дубликат инструмента только если у него нет рыночных данных."""
        if any(self._instrument_data_counts(instrument_id)):
            return False
        self.conn.execute("DELETE FROM instrument WHERE id=?", (instrument_id,))
        self._commit()
        return True

    def _instrument_data_counts(self, instrument_id: int) -> tuple[int, int, int, int]:
        """Свечи, зоны, наблюдения и зоны входа. Пустая четвёрка — строку можно убрать."""
        row = self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM candle WHERE instrument_id=?), "
            "(SELECT COUNT(*) FROM zone WHERE instrument_id=?), "
            "(SELECT COUNT(*) FROM ltf_observation WHERE instrument_id=?), "
            "(SELECT COUNT(*) FROM ltf_entry_zone WHERE instrument_id=?)",
            (instrument_id, instrument_id, instrument_id, instrument_id),
        ).fetchone()
        return tuple(int(v or 0) for v in row)

    def collapse_hyperliquid_hype(self) -> None:
        """Один публичный инструмент Hyperliquid HYPE.

        Технический alias @207 и повторный HYPE — один и тот же спот.
        Остаётся строка с большей историей (при равенстве — уже названная
        HYPE). Её id не меняется. Пустой дубль удаляется. Дубль с данными
        выключается и переименовывается в ``…#retired-<id>``, чтобы не
        светиться в списке и не качать тот же рынок второй раз.
        """
        rows = [
            i for i in self.get_instruments(include_retired=True)
            if i.venue == "hyperliquid" and i.symbol in ("HYPE", "@207")
        ]
        grouped: dict[str, list] = {}
        for ins in rows:
            grouped.setdefault(ins.market_type, []).append(ins)
        for contenders in grouped.values():
            self._collapse_hype_group(contenders)

    def _collapse_hype_group(self, contenders: list) -> None:
        if not contenders:
            return
        if len(contenders) == 1:
            only = contenders[0]
            if only.symbol == "@207":
                self.rename_instrument_symbol(only.id, "HYPE")
            return
        def score(ins) -> tuple:
            candles, zones, obs, entries = self._instrument_data_counts(ins.id)
            return (candles, zones, obs, entries, 1 if ins.symbol == "HYPE" else 0, -ins.id)
        winner = max(contenders, key=score)
        any_enabled = any(i.enabled for i in contenders)
        for loser in contenders:
            if loser.id == winner.id:
                continue
            if not any(self._instrument_data_counts(loser.id)):
                self.delete_empty_instrument(loser.id)
                continue
            self.set_instrument_enabled(loser.id, False)
            retired = f"{loser.symbol}#retired-{loser.id}"
            if loser.symbol != retired:
                self.rename_instrument_symbol(loser.id, retired)
        if winner.symbol != "HYPE":
            self.rename_instrument_symbol(winner.id, "HYPE")
        if any_enabled and not winner.enabled:
            self.set_instrument_enabled(winner.id, True)

    def set_instrument_ltf_analyze(self, instrument_id: int, analyze: bool) -> None:
        self.conn.execute(
            "UPDATE instrument SET ltf_analyze=? WHERE id=?",
            (int(analyze), instrument_id),
        )
        self._commit()

    # ---------- candles ----------

    def insert_candles(self, candles: Iterable[Candle]) -> int:
        rows = [
            (c.instrument_id, c.timeframe, c.open_time, c.close_time, c.open, c.high,
             c.low, c.close, int(c.closed), c.source)
            for c in candles
        ]
        cur = self.conn.executemany(
            """INSERT INTO candle (instrument_id, timeframe, open_time, close_time,
                                  open, high, low, close, closed, source)
               VALUES (?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (instrument_id, timeframe, open_time)
               DO UPDATE SET close_time=excluded.close_time, open=excluded.open,
                             high=excluded.high, low=excluded.low, close=excluded.close,
                             closed=excluded.closed, source=excluded.source""",
            rows,
        )
        self._commit()
        self.bump_state_seq()
        return cur.rowcount

    def get_candles(
        self,
        instrument_id: int,
        timeframe: str,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
        closed_only: bool = True,
    ) -> list[Candle]:
        q = "SELECT * FROM candle WHERE instrument_id=? AND timeframe=?"
        args: list[Any] = [instrument_id, timeframe]
        if closed_only:
            q += " AND closed=1"
        if start_ms is not None:
            q += " AND open_time>=?"
            args.append(start_ms)
        if end_ms is not None:
            q += " AND open_time<=?"
            args.append(end_ms)
        q += " ORDER BY open_time"
        return [self._to_candle(r) for r in self.conn.execute(q, args).fetchall()]

    def last_candle(
        self, instrument_id: int, timeframe: str, closed_only: bool = True
    ) -> Optional[Candle]:
        q = "SELECT * FROM candle WHERE instrument_id=? AND timeframe=?"
        args: list[Any] = [instrument_id, timeframe]
        if closed_only:
            q += " AND closed=1"
        q += " ORDER BY open_time DESC LIMIT 1"
        r = self.conn.execute(q, args).fetchone()
        return self._to_candle(r) if r else None

    def first_candle(
        self, instrument_id: int, timeframe: str, closed_only: bool = True
    ) -> Optional[Candle]:
        q = "SELECT * FROM candle WHERE instrument_id=? AND timeframe=?"
        args: list[Any] = [instrument_id, timeframe]
        if closed_only:
            q += " AND closed=1"
        q += " ORDER BY open_time ASC LIMIT 1"
        r = self.conn.execute(q, args).fetchone()
        return self._to_candle(r) if r else None

    @staticmethod
    def _to_candle(r: sqlite3.Row) -> Candle:
        return Candle(
            instrument_id=r["instrument_id"], timeframe=r["timeframe"],
            open_time=r["open_time"], close_time=r["close_time"], open=r["open"],
            high=r["high"], low=r["low"], close=r["close"], closed=bool(r["closed"]),
            source=r["source"],
        )

    # ---------- zones ----------

    def insert_zone(self, z: Zone) -> Optional[int]:
        """INSERT OR IGNORE по дедуп-индексу; возвращает id или None, если уже есть."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO zone
               (instrument_id, type, direction, timeframe, lower, upper, formed_at,
                confirmed_at, status, cycle_id, source, rule_version, evidence, created_at,
                display_from, display_until, end_reason,
                breakout_close_at, breaker_forbidden, breakout_expired, needs_replay,
                market_validity, max_test_depth, has_tests, entry_eligible,
                anchor_time, zone_type, test_extreme)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (z.instrument_id, z.type.value, z.direction.value, z.timeframe, z.lower,
             z.upper, z.formed_at, z.confirmed_at, z.status.value, z.cycle_id, z.source,
             z.rule_version, z.evidence_json(), z.created_at,
             z.display_from, z.display_until, z.end_reason,
             z.breakout_close_at, int(z.breaker_forbidden), int(z.breakout_expired),
             int(z.needs_replay), z.market_validity, z.max_test_depth,
             int(z.has_tests), int(z.entry_eligible), z.anchor_time, z.zone_type,
             z.test_extreme),
        )
        self._commit()
        # INSERT OR IGNORE: rowcount == 0 при дубликате; lastrowid sqlite3 при этом
        # может вернуть id прежней вставки — на него полагаться нельзя.
        if cur.rowcount == 1:
            self.invalidate_zone_cache()
            return int(cur.lastrowid)
        return None

    def find_zone_id(self, z: Zone) -> Optional[int]:
        """Точечный поиск по дедуп-ключу (ux_zone_dedup) — без полного скана
        зон инструмента (replay дёргает её на каждый дубликат)."""
        row = self.conn.execute(
            """SELECT id FROM zone
               WHERE instrument_id=? AND type=? AND direction=? AND timeframe=?
                 AND lower=? AND upper=? AND formed_at=? AND cycle_id=?""",
            (z.instrument_id, z.type.value, z.direction.value, z.timeframe,
             z.lower, z.upper, z.formed_at, z.cycle_id),
        ).fetchone()
        return int(row["id"]) if row else None

    def update_zone(self, zone_id: int, **fields: Any) -> None:
        if "status" in fields and isinstance(fields["status"], ZoneStatus):
            fields["status"] = fields["status"].value
        if "evidence" in fields and isinstance(fields["evidence"], dict):
            fields["evidence"] = json.dumps(fields["evidence"], ensure_ascii=False)
        for b in ("breaker_forbidden", "breakout_expired", "needs_replay",
                  "has_tests", "entry_eligible"):
            if b in fields:
                fields[b] = int(bool(fields[b]))
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE zone SET {cols} WHERE id=?", (*fields.values(), zone_id)
        )
        self._commit()
        self.invalidate_zone_cache()

    def invalidate_zone_cache(self) -> None:
        """Сброс кэша get_zones — вызывать после любой записи в zone,
        идущей мимо insert_zone/update_zone (прямые SQL-апдейты)."""
        with self._zone_cache_lock:
            self._zone_cache_version += 1
            self._zone_cache.clear()
        self.bump_state_seq()

    # ---------- версия состояния (D01) ----------

    def _write_state_seq(self) -> None:
        self.conn.execute(
            """INSERT INTO meta (key, value) VALUES ('state_seq', '1')
               ON CONFLICT (key) DO UPDATE SET
               value = CAST(CAST(value AS INTEGER) + 1 AS TEXT)"""
        )

    def bump_state_seq(self) -> None:
        """Монотонный счётчик версии состояния (D01): инкремент при любой
        записи предметного состояния (зоны, события, свечи, ltf_*). WS и
        снимки отдают его как state_version; по возрастанию клиент видит
        изменение, а после reconnect перечитывает полный снимок. В batch-
        режиме (replay) инкремент откладывается до конца батча."""
        if self._batch_depth > 0:
            self._state_seq_dirty = True
            return
        self._write_state_seq()
        self._commit()
        self._seen_epoch = self.get_state_seq()

    def get_state_seq(self) -> int:
        r = self.conn.execute(
            "SELECT value FROM meta WHERE key='state_seq'"
        ).fetchone()
        return int(r["value"]) if r else 0

    @contextlib.contextmanager
    def read_tx(self):
        """Согласованное чтение снимка (D01): замок соединения удерживается
        на всю read-транзакцию, поэтому серия запросов видит одну версию
        состояния без вклинивания записей между ними. Во время batch_writes
        BEGIN/ROLLBACK сломали бы отложенную транзакцию батча на этом же
        соединении — читаем без обёртки (видно промежуточное состояние
        replay, как и при per-write commit'ах)."""
        lock = self.conn._lock
        # Чужая запись между execute и commit оставляет транзакцию открытой
        # и отпускает замок. BEGIN в этот зазор даёт
        # «cannot start a transaction within a transaction» и 500 на снимке.
        # Замок при ожидании не держим, чтобы писатель успел сделать commit.
        # Вложенное чтение и batch транзакцию не открывают и не откатывают.
        deadline = time.monotonic() + 5.0
        while True:
            with lock:
                if self._batch_depth > 0 or self._read_depth > 0:
                    self._read_depth += 1
                    try:
                        yield
                    finally:
                        self._read_depth -= 1
                    return
                raw = self.conn._conn
                if not raw.in_transaction:
                    self._read_depth += 1
                    raw.execute("BEGIN DEFERRED")
                    try:
                        yield
                    finally:
                        self._read_depth -= 1
                        try:
                            raw.rollback()
                        except Exception:
                            pass  # транзакцию уже завершил commit вложенной записи
                    return
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError(
                    "cannot start a transaction within a transaction"
                )
            time.sleep(0.01)

    # ---------- кэш горячих LTF-чтений (replay: per-candle N+1) ----------

    def _bump_ltf_cache(self) -> None:
        """Инвалидация кэша LTF-чтений — вызывается из каждого метода записи
        в ltf_* таблицы (insert/update/upsert/delete)."""
        with self._ltf_cache_lock:
            self._ltf_cache_version += 1
            self._ltf_cache.clear()
        self.bump_state_seq()

    def _ltf_cached(self, key: tuple, loader) -> Any:
        """Выборка через кэш версии _ltf_cache_version. Списки отдаются
        поверхностной копией — сортировка/append вызывающего кэш не портит."""
        if not self.ltf_cache_enabled:
            return loader()
        self._note_external_epoch()
        with self._ltf_cache_lock:
            version = self._ltf_cache_version
            hit = self._ltf_cache.get(key)
            if hit is not None and hit[0] == version:
                value = hit[1]
                return list(value) if isinstance(value, list) else value
        value = loader()
        with self._ltf_cache_lock:
            # версия могла измениться, пока шёл запрос — тогда не кэшируем
            if version == self._ltf_cache_version:
                self._ltf_cache[key] = (version, value)
        return list(value) if isinstance(value, list) else value

    # ---------- кэши горячих HTF-чтений (replay: per-candle N+1) ----------

    def _rc_cached(self, store: dict, key: tuple, version: int, loader) -> Any:
        """Выборка через кэш с явной версией (визиты/уровни/события зоны).
        Списки отдаются поверхностной копией; объекты разделяются — не
        мутировать без последующего update_* (он инвалидирует кэш)."""
        with self._read_cache_lock:
            hit = store.get(key)
            if hit is not None and hit[0] == version:
                value = hit[1]
                return list(value) if isinstance(value, list) else value
        value = loader()
        with self._read_cache_lock:
            store[key] = (version, value)
        return list(value) if isinstance(value, list) else value

    def _visit_version(self, zone_id: int) -> int:
        with self._read_cache_lock:
            return self._visit_versions.get(zone_id, 0)

    def _bump_visit_version(self, zone_id: int) -> None:
        with self._read_cache_lock:
            self._visit_versions[zone_id] = self._visit_versions.get(zone_id, 0) + 1

    def _inner_version(self, parent_ob_id: int) -> int:
        with self._read_cache_lock:
            return self._inner_versions.get(parent_ob_id, 0)

    def _bump_inner_version(self, parent_ob_id: int) -> None:
        with self._read_cache_lock:
            self._inner_versions[parent_ob_id] = (
                self._inner_versions.get(parent_ob_id, 0) + 1
            )
            self._inner_global_version += 1

    def _event_version(self, zone_id: int, cycle_id: int) -> int:
        with self._read_cache_lock:
            return self._event_versions.get((zone_id, cycle_id), 0)

    def _bump_event_version(self, zone_id: int, cycle_id: int) -> None:
        with self._read_cache_lock:
            key = (zone_id, cycle_id)
            self._event_versions[key] = self._event_versions.get(key, 0) + 1

    def get_zone(self, zone_id: int) -> Optional[Zone]:
        r = self.conn.execute("SELECT * FROM zone WHERE id=?", (zone_id,)).fetchone()
        return self._to_zone(r) if r else None

    def get_zones(
        self,
        instrument_id: Optional[int] = None,
        statuses: Optional[list[ZoneStatus]] = None,
        types: Optional[list[ZoneType]] = None,
        timeframes: Optional[set] = None,
    ) -> list[Zone]:
        self._note_external_epoch()
        key = (
            instrument_id,
            tuple(s.value for s in statuses) if statuses else None,
            tuple(t.value for t in types) if types else None,
            tuple(sorted(timeframes)) if timeframes else None,
        )
        with self._zone_cache_lock:
            version = self._zone_cache_version
            hit = self._zone_cache.get(key)
            if hit is not None and hit[0] == version:
                return list(hit[1])
        q = "SELECT * FROM zone WHERE 1=1"
        args: list[Any] = []
        if instrument_id is not None:
            q += " AND instrument_id=?"
            args.append(instrument_id)
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += [s.value for s in statuses]
        if types:
            q += f" AND type IN ({','.join('?' * len(types))})"
            args += [t.value for t in types]
        if timeframes:
            q += f" AND timeframe IN ({','.join('?' * len(timeframes))})"
            args += sorted(timeframes)
        q += " ORDER BY formed_at"
        result = [self._zone_from_row(r) for r in self.conn.execute(q, args).fetchall()]
        with self._zone_cache_lock:
            # версия могла измениться, пока шёл запрос — тогда не кэшируем
            if version == self._zone_cache_version:
                self._zone_cache[key] = (version, result)
        return list(result)

    def get_unreviewed_candidates(
        self, instrument_id: Optional[int] = None
    ) -> list[Zone]:
        """Очередь ручной проверки (§10): кандидаты без единого ревью.

        Решения вроде now_irrelevant / no_context статуса зоны не меняют
        (§15.1.1), поэтому «проверенность» определяется по наличию записи
        в review — иначе проверенные зоны возвращались бы в очередь.
        Без кэша: review-записи версию кэша зон не инвалидируют.
        ТЗ 06.10.2026 §3.3 (T07): завершённые/инвалидированные объекты
        (close_beyond и т.п.) очередь проверки и рабочие сигналы не
        засоряют — они доступны в истории."""
        q = ("SELECT * FROM zone WHERE status='candidate' "
             "AND market_validity='active' AND display_until IS NULL "
             "AND NOT EXISTS (SELECT 1 FROM review r WHERE r.zone_id = zone.id)")
        args: list[Any] = []
        if instrument_id is not None:
            q += " AND instrument_id=?"
            args.append(instrument_id)
        q += " ORDER BY formed_at"
        return [self._zone_from_row(r) for r in self.conn.execute(q, args).fetchall()]

    def _zone_from_row(self, r: sqlite3.Row) -> Zone:
        fp = hash(tuple(r))
        cached = self._zone_obj_cache.get(r["id"])
        if cached is not None and cached[0] == fp:
            return cached[1]
        z = self._to_zone(r)
        self._zone_obj_cache[r["id"]] = (fp, z)
        return z

    @staticmethod
    def _to_zone(r: sqlite3.Row) -> Zone:
        ev = json.loads(r["evidence"] or "{}")
        source_candles = ev.pop("source_candles", [])
        keys = r.keys()
        return Zone(
            id=r["id"], instrument_id=r["instrument_id"], type=ZoneType(r["type"]),
            direction=Direction(r["direction"]), timeframe=r["timeframe"],
            lower=r["lower"], upper=r["upper"], formed_at=r["formed_at"],
            confirmed_at=r["confirmed_at"], status=ZoneStatus(r["status"]),
            cycle_id=r["cycle_id"], source=r["source"], rule_version=r["rule_version"],
            source_candles=source_candles, evidence=ev, created_at=r["created_at"],
            display_from=r["display_from"], display_until=r["display_until"],
            end_reason=r["end_reason"], breakout_close_at=r["breakout_close_at"],
            breaker_forbidden=bool(r["breaker_forbidden"]),
            breakout_expired=bool(r["breakout_expired"]),
            needs_replay=bool(r["needs_replay"]) if "needs_replay" in keys else False,
            market_validity=r["market_validity"] if "market_validity" in keys else "active",
            max_test_depth=r["max_test_depth"] if "max_test_depth" in keys else 0.0,
            has_tests=bool(r["has_tests"]) if "has_tests" in keys else False,
            entry_eligible=bool(r["entry_eligible"]) if "entry_eligible" in keys else True,
            anchor_time=r["anchor_time"] if "anchor_time" in keys else None,
            zone_type=r["zone_type"] if "zone_type" in keys else None,
            test_extreme=r["test_extreme"] if "test_extreme" in keys else None,
        )

    # ---------- inner levels (ТЗ «Единый движок» §5) ----------

    def insert_inner_level(self, lv: "InnerLevel") -> Optional[int]:
        """Идемпотентно по UNIQUE(parent_ob_id, timeframe, kind, price, pivot_time)."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO inner_level
               (parent_ob_id, instrument_id, timeframe, kind, price, pivot_time,
                confirmed_at, source_test_id, status, taken_at, evidence, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (lv.parent_ob_id, lv.instrument_id, lv.timeframe, lv.kind, lv.price,
             lv.pivot_time, lv.confirmed_at, lv.source_test_id, lv.status,
             lv.taken_at, json.dumps(lv.evidence, ensure_ascii=False), lv.created_at),
        )
        if cur.rowcount == 1:
            self._bump_inner_version(lv.parent_ob_id)
            self._commit()
            return int(cur.lastrowid)
        self._commit()
        return None

    def update_inner_level(self, level_id: int, **fields: Any) -> None:
        if "evidence" in fields and isinstance(fields["evidence"], dict):
            fields["evidence"] = json.dumps(fields["evidence"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        parent = self.conn.execute(
            "SELECT parent_ob_id FROM inner_level WHERE id=?", (level_id,)
        ).fetchone()
        self.conn.execute(
            f"UPDATE inner_level SET {cols} WHERE id=?", (*fields.values(), level_id)
        )
        if parent is not None:
            self._bump_inner_version(int(parent["parent_ob_id"]))
        self._commit()

    def get_inner_level_by_key(
        self, parent_ob_id: int, timeframe: str, kind: str, price: float, pivot_time: int
    ) -> Optional["InnerLevel"]:
        def load() -> Optional["InnerLevel"]:
            r = self.conn.execute(
                """SELECT * FROM inner_level
                   WHERE parent_ob_id=? AND timeframe=? AND kind=? AND price=? AND pivot_time=?""",
                (parent_ob_id, timeframe, kind, price, pivot_time),
            ).fetchone()
            return self._to_inner_level(r) if r else None
        return self._rc_cached(
            self._inner_cache,
            ("inner_k", parent_ob_id, timeframe, kind, price, pivot_time),
            self._inner_version(parent_ob_id), load,
        )

    def list_inner_levels(
        self,
        parent_ob_id: Optional[int] = None,
        instrument_id: Optional[int] = None,
        statuses: Optional[tuple[str, ...]] = None,
    ) -> list["InnerLevel"]:
        def load() -> list["InnerLevel"]:
            q = "SELECT * FROM inner_level WHERE 1=1"
            args: list[Any] = []
            if parent_ob_id is not None:
                q += " AND parent_ob_id=?"
                args.append(parent_ob_id)
            if instrument_id is not None:
                q += " AND instrument_id=?"
                args.append(instrument_id)
            if statuses:
                q += f" AND status IN ({','.join('?' * len(statuses))})"
                args += list(statuses)
            q += " ORDER BY pivot_time, id"
            return [self._to_inner_level(r) for r in self.conn.execute(q, args).fetchall()]
        if parent_ob_id is not None:
            return self._rc_cached(
                self._inner_cache, ("inner_p", parent_ob_id, statuses),
                self._inner_version(parent_ob_id), load,
            )
        # выборки по инструменту — глобальная версия inner_level (записи редки)
        with self._read_cache_lock:
            version = self._inner_global_version
        return self._rc_cached(
            self._inner_cache, ("inner_i", instrument_id, statuses), version, load,
        )

    @staticmethod
    def _to_inner_level(r: sqlite3.Row) -> "InnerLevel":
        return InnerLevel(
            id=r["id"], parent_ob_id=r["parent_ob_id"], instrument_id=r["instrument_id"],
            timeframe=r["timeframe"], kind=r["kind"], price=r["price"],
            pivot_time=r["pivot_time"], confirmed_at=r["confirmed_at"],
            source_test_id=r["source_test_id"], status=r["status"],
            taken_at=r["taken_at"], evidence=json.loads(r["evidence"] or "{}"),
            created_at=r["created_at"],
        )

    # ---------- relations ----------

    def set_relation(self, rel: ZoneRelation) -> None:
        self.conn.execute(
            """INSERT INTO zone_relation
               (zone_id, parent_ob_id, confirming_fvg_id, predecessor_ob_id, visual_group_id)
               VALUES (?,?,?,?,?)
               ON CONFLICT (zone_id) DO UPDATE SET
                 parent_ob_id=COALESCE(zone_relation.parent_ob_id, excluded.parent_ob_id),
                 confirming_fvg_id=COALESCE(zone_relation.confirming_fvg_id, excluded.confirming_fvg_id),
                 predecessor_ob_id=COALESCE(zone_relation.predecessor_ob_id, excluded.predecessor_ob_id),
                 visual_group_id=COALESCE(zone_relation.visual_group_id, excluded.visual_group_id)""",
            (rel.zone_id, rel.parent_ob_id, rel.confirming_fvg_id,
             rel.predecessor_ob_id, rel.visual_group_id),
        )
        self._commit()

    def get_relation(self, zone_id: int) -> Optional[ZoneRelation]:
        r = self.conn.execute(
            "SELECT * FROM zone_relation WHERE zone_id=?", (zone_id,)
        ).fetchone()
        if not r:
            return None
        return ZoneRelation(
            zone_id=r["zone_id"], parent_ob_id=r["parent_ob_id"],
            confirming_fvg_id=r["confirming_fvg_id"],
            predecessor_ob_id=r["predecessor_ob_id"], visual_group_id=r["visual_group_id"],
        )

    def find_relations(self, **where: Any) -> list[ZoneRelation]:
        q = "SELECT * FROM zone_relation WHERE " + " AND ".join(f"{k}=?" for k in where)
        return [
            ZoneRelation(
                zone_id=r["zone_id"], parent_ob_id=r["parent_ob_id"],
                confirming_fvg_id=r["confirming_fvg_id"],
                predecessor_ob_id=r["predecessor_ob_id"],
                visual_group_id=r["visual_group_id"],
            )
            for r in self.conn.execute(q, tuple(where.values())).fetchall()
        ]

    # ---------- visits ----------

    def open_visit(self, v: Visit) -> int:
        """Открытие захода. Идемпотентно (T22, ТЗ 06.10.2026 §14): повторный
        replay обрабатывает те же свечи — визит с тем же ключом
        (zone_id, cycle_id, entered_at) не дублируется, возвращается id
        существующего."""
        existing = self.conn.execute(
            "SELECT id FROM visit WHERE zone_id=? AND cycle_id=? AND entered_at=?",
            (v.zone_id, v.cycle_id, v.entered_at),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])
        cur = self.conn.execute(
            """INSERT INTO visit (zone_id, cycle_id, entered_at, exited_at, max_depth,
                                  observed, extreme, d_raw)
               VALUES (?,?,?,?,?,?,?,?)""",
            (v.zone_id, v.cycle_id, v.entered_at, v.exited_at, v.max_depth,
             int(v.observed), v.extreme, v.d_raw),
        )
        self._bump_visit_version(v.zone_id)
        self._commit()
        return int(cur.lastrowid)

    def close_visit(self, visit_id: int, exited_at: int, max_depth: float,
                    exit_kind: Optional[str] = None,
                    extreme: Optional[float] = None, d_raw: Optional[float] = None) -> None:
        zone_id = self._visit_zone_id(visit_id)
        self.conn.execute(
            """UPDATE visit SET exited_at=?, max_depth=?, exit_kind=?,
                 extreme=COALESCE(?, extreme), d_raw=COALESCE(?, d_raw)
               WHERE id=?""",
            (exited_at, max_depth, exit_kind, extreme, d_raw, visit_id),
        )
        if zone_id is not None:
            self._bump_visit_version(zone_id)
        self._commit()

    def _visit_zone_id(self, visit_id: int) -> Optional[int]:
        r = self.conn.execute(
            "SELECT zone_id FROM visit WHERE id=?", (visit_id,)
        ).fetchone()
        return int(r["zone_id"]) if r else None

    def update_visit_depth(self, visit_id: int, max_depth: float,
                           extreme: Optional[float] = None,
                           d_raw: Optional[float] = None) -> None:
        zone_id = self._visit_zone_id(visit_id)
        self.conn.execute(
            "UPDATE visit SET max_depth=?, extreme=COALESCE(?, extreme), "
            "d_raw=COALESCE(?, d_raw) WHERE id=?",
            (max_depth, extreme, d_raw, visit_id),
        )
        if zone_id is not None:
            self._bump_visit_version(zone_id)
        self._commit()

    @staticmethod
    def _to_visit(r: sqlite3.Row) -> Visit:
        keys = r.keys()
        return Visit(
            id=r["id"], zone_id=r["zone_id"], cycle_id=r["cycle_id"],
            entered_at=r["entered_at"], exited_at=r["exited_at"],
            max_depth=r["max_depth"], observed=bool(r["observed"]),
            exit_kind=r["exit_kind"] if "exit_kind" in keys else None,
            extreme=r["extreme"] if "extreme" in keys else None,
            d_raw=r["d_raw"] if "d_raw" in keys else None,
        )

    def get_visits(self, zone_id: int, cycle_id: Optional[int] = None) -> list[Visit]:
        def load() -> list[Visit]:
            q = "SELECT * FROM visit WHERE zone_id=?"
            args: list[Any] = [zone_id]
            if cycle_id is not None:
                q += " AND cycle_id=?"
                args.append(cycle_id)
            q += " ORDER BY entered_at"
            return [self._to_visit(r) for r in self.conn.execute(q, args).fetchall()]
        return self._rc_cached(
            self._visits_cache, ("visits", zone_id, cycle_id),
            self._visit_version(zone_id), load,
        )

    def get_zone_max_depth(self, zone_id: int, cycle_id: int) -> Optional[float]:
        """Максимальная глубина теста за жизненный цикл (§15.7: контракт для
        будущего LTF Screener — глубина хранится отдельно от валидности)."""
        r = self.conn.execute(
            "SELECT MAX(max_depth) AS d FROM visit WHERE zone_id=? AND cycle_id=?",
            (zone_id, cycle_id),
        ).fetchone()
        return r["d"] if r and r["d"] is not None else None

    def open_visit_for(self, zone_id: int, cycle_id: int) -> Optional[Visit]:
        def load() -> Optional[Visit]:
            r = self.conn.execute(
                """SELECT * FROM visit WHERE zone_id=? AND cycle_id=? AND exited_at IS NULL
                   ORDER BY entered_at DESC LIMIT 1""",
                (zone_id, cycle_id),
            ).fetchone()
            return self._to_visit(r) if r else None
        return self._rc_cached(
            self._visits_cache, ("open_visit", zone_id, cycle_id),
            self._visit_version(zone_id), load,
        )

    # ---------- events ----------

    def insert_event(self, e: Event) -> Optional[int]:
        """Идемпотентно: UNIQUE(zone, cycle, kind, occurred_at). None = дубликат."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO event
               (zone_id, cycle_id, kind, occurred_at, detected_at, price, depth, delayed, evidence)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (e.zone_id, e.cycle_id, e.kind.value, e.occurred_at, e.detected_at, e.price,
             e.depth, int(e.delayed), json.dumps(e.evidence, ensure_ascii=False)),
        )
        self._commit()
        # rowcount надёжен в отличие от lastrowid после проигнорированной вставки
        if cur.rowcount == 1:
            self._bump_event_version(e.zone_id, e.cycle_id)
            self.bump_state_seq()
            return int(cur.lastrowid)
        return None

    def get_events(
        self,
        zone_id: Optional[int] = None,
        since_ms: Optional[int] = None,
        limit: int = 500,
    ) -> list[Event]:
        q = "SELECT * FROM event WHERE 1=1"
        args: list[Any] = []
        if zone_id is not None:
            q += " AND zone_id=?"
            args.append(zone_id)
        if since_ms is not None:
            q += " AND occurred_at>=?"
            args.append(since_ms)
        q += " ORDER BY occurred_at DESC LIMIT ?"
        args.append(limit)
        return [
            Event(
                id=r["id"], zone_id=r["zone_id"], cycle_id=r["cycle_id"],
                kind=EventKind(r["kind"]), occurred_at=r["occurred_at"],
                detected_at=r["detected_at"], price=r["price"], depth=r["depth"],
                delayed=bool(r["delayed"]), evidence=json.loads(r["evidence"] or "{}"),
            )
            for r in self.conn.execute(q, args).fetchall()
        ]

    def get_event(self, event_id: int) -> Optional[Event]:
        row = self.conn.execute("SELECT * FROM event WHERE id=?", (event_id,)).fetchone()
        if row is None:
            return None
        return Event(id=row["id"], zone_id=row["zone_id"], cycle_id=row["cycle_id"],
                     kind=EventKind(row["kind"]), occurred_at=row["occurred_at"],
                     detected_at=row["detected_at"], price=row["price"], depth=row["depth"],
                     delayed=bool(row["delayed"]), evidence=json.loads(row["evidence"] or "{}"))

    def list_events_for_instrument(
        self, instrument_id: int, since_ms: Optional[int] = None, limit: int = 100
    ) -> list[Event]:
        """HTF-события инструмента (join через зону), свежие первыми —
        история для бота (ТЗ п.11)."""
        q = """SELECT event.* FROM event
               JOIN zone ON event.zone_id = zone.id
               WHERE zone.instrument_id=?"""
        args: list[Any] = [instrument_id]
        if since_ms is not None:
            q += " AND event.occurred_at>=?"
            args.append(since_ms)
        q += " ORDER BY event.occurred_at DESC LIMIT ?"
        args.append(limit)
        return [
            Event(
                id=r["id"], zone_id=r["zone_id"], cycle_id=r["cycle_id"],
                kind=EventKind(r["kind"]), occurred_at=r["occurred_at"],
                detected_at=r["detected_at"], price=r["price"], depth=r["depth"],
                delayed=bool(r["delayed"]), evidence=json.loads(r["evidence"] or "{}"),
            )
            for r in self.conn.execute(q, args).fetchall()
        ]

    def has_event(
        self, zone_id: int, cycle_id: int, kind: EventKind, occurred_at: int
    ) -> bool:
        """Точечная проверка дедуп-ключа (UNIQUE zone/cycle/kind/occurred_at)."""
        def load() -> bool:
            row = self.conn.execute(
                """SELECT 1 FROM event
                   WHERE zone_id=? AND cycle_id=? AND kind=? AND occurred_at=?""",
                (zone_id, cycle_id, kind.value, occurred_at),
            ).fetchone()
            return row is not None
        return self._rc_cached(
            self._event_cache, ("has_event", zone_id, cycle_id, kind, occurred_at),
            self._event_version(zone_id, cycle_id), load,
        )

    def event_keys(self, zone_id: int, cycle_id: int) -> list[tuple[EventKind, int]]:
        """(kind, occurred_at) всех событий цикла зоны — для дедупа при эмите.

        Читает только ключи (покрывается UNIQUE-индексом), без выборки полных
        строк с LIMIT-окном, которое сужало дедуп на зонах с >500 событиями.
        """
        def load() -> list[tuple[EventKind, int]]:
            rows = self.conn.execute(
                "SELECT kind, occurred_at FROM event WHERE zone_id=? AND cycle_id=?",
                (zone_id, cycle_id),
            ).fetchall()
            return [(EventKind(r["kind"]), r["occurred_at"]) for r in rows]
        return self._rc_cached(
            self._event_cache, ("event_keys", zone_id, cycle_id),
            self._event_version(zone_id, cycle_id), load,
        )

    # ---------- deliveries ----------

    def record_delivery(self, d: Delivery) -> Optional[int]:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO delivery
               (event_ids, destination, status, idempotency_key, delivered_at, error)
               VALUES (?,?,?,?,?,?)""",
            (json.dumps(d.event_ids), d.destination, d.status, d.idempotency_key,
             d.delivered_at, d.error),
        )
        self._commit()
        # rowcount надёжен в отличие от lastrowid после проигнорированной вставки
        return int(cur.lastrowid) if cur.rowcount == 1 else None

    def update_delivery(self, delivery_id: int, status: str,
                        delivered_at: Optional[int] = None, error: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE delivery SET status=?, delivered_at=?, error=? WHERE id=?",
            (status, delivered_at, error, delivery_id),
        )
        self._commit()

    def pending_deliveries(self) -> list[Delivery]:
        rows = self.conn.execute(
            "SELECT * FROM delivery WHERE status IN ('pending','failed') ORDER BY id"
        ).fetchall()
        return [
            Delivery(
                id=r["id"], event_ids=json.loads(r["event_ids"]), destination=r["destination"],
                status=r["status"], idempotency_key=r["idempotency_key"],
                delivered_at=r["delivered_at"], error=r["error"],
            )
            for r in rows
        ]

    # ---------- alert state (подавление 120 ч, §8) ----------

    def get_alert_state(self, zone_id: int, cycle_id: int, event_kind: str,
                        user: str = "owner") -> Optional[AlertState]:
        r = self.conn.execute(
            """SELECT * FROM alert_state
               WHERE zone_id=? AND cycle_id=? AND event_kind=? AND user=?""",
            (zone_id, cycle_id, event_kind, user),
        ).fetchone()
        if not r:
            return None
        return AlertState(
            zone_id=r["zone_id"], cycle_id=r["cycle_id"], event_kind=r["event_kind"],
            user=r["user"], last_delivered_at=r["last_delivered_at"],
            muted_until=r["muted_until"], acknowledged=bool(r["acknowledged"]),
        )

    def set_alert_state(self, st: AlertState) -> None:
        self.conn.execute(
            """INSERT INTO alert_state
               (zone_id, cycle_id, event_kind, user, last_delivered_at, muted_until, acknowledged)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (zone_id, cycle_id, event_kind, user) DO UPDATE SET
                 last_delivered_at=excluded.last_delivered_at,
                 muted_until=excluded.muted_until,
                 acknowledged=excluded.acknowledged""",
            (st.zone_id, st.cycle_id, st.event_kind, st.user, st.last_delivered_at,
             st.muted_until, int(st.acknowledged)),
        )
        self._commit()

    def set_mute(self, zone_id: int, cycle_id: int, muted_until: Optional[int],
                 user: str = "owner") -> None:
        self.conn.execute(
            "UPDATE alert_state SET muted_until=? WHERE zone_id=? AND cycle_id=? AND user=?",
            (muted_until, zone_id, cycle_id, user),
        )
        self._commit()

    # ---------- Telegram-бот: watchlist (ТЗ п.8) ----------

    def list_watchlist(self, chat_id: str) -> list[dict[str, Any]]:
        """Строки списка наблюдения владельца бота (порядок — по добавлению)."""
        return [
            {
                "instrument_id": r["instrument_id"],
                "alerts_enabled": bool(r["alerts_enabled"]),
                "added_at": r["added_at"],
            }
            for r in self.conn.execute(
                "SELECT * FROM bot_watchlist WHERE chat_id=? ORDER BY added_at, instrument_id",
                (chat_id,),
            ).fetchall()
        ]

    def watchlist_add(self, chat_id: str, instrument_id: int) -> None:
        self.conn.execute(
            """INSERT OR IGNORE INTO bot_watchlist
               (chat_id, instrument_id, alerts_enabled, added_at)
               VALUES (?,?,1,?)""",
            (chat_id, instrument_id, now_ms()),
        )
        self._commit()

    def watchlist_remove(self, chat_id: str, instrument_id: int) -> None:
        self.conn.execute(
            "DELETE FROM bot_watchlist WHERE chat_id=? AND instrument_id=?",
            (chat_id, instrument_id),
        )
        self._commit()

    def watchlist_set_alerts(
        self, chat_id: str, instrument_id: int, enabled: bool
    ) -> None:
        self.conn.execute(
            "UPDATE bot_watchlist SET alerts_enabled=? "
            "WHERE chat_id=? AND instrument_id=?",
            (int(enabled), chat_id, instrument_id),
        )
        self._commit()

    def watchlist_alerts_disabled(self, chat_id: str, instrument_id: int) -> bool:
        """True только если инструмент в списке с выключенными уведомлениями;
        отсутствие строки — не блокировка (fallback на enabled-инструменты)."""
        r = self.conn.execute(
            "SELECT alerts_enabled FROM bot_watchlist "
            "WHERE chat_id=? AND instrument_id=?",
            (chat_id, instrument_id),
        ).fetchone()
        return r is not None and not r["alerts_enabled"]

    def seed_watchlist(self, chat_id: str) -> int:
        """Первичное наполнение из включённых инструментов (при /start);
        непустой список не трогаем. Возвращает число добавленных."""
        if self.list_watchlist(chat_id):
            return 0
        added = 0
        for ins in self.get_instruments(enabled_only=True):
            self.watchlist_add(chat_id, ins.id)
            added += 1
        return added

    # ---------- Telegram-бот: настройки уведомлений (ТЗ п.9) ----------

    def get_alert_prefs(self, chat_id: str) -> list[dict[str, Any]]:
        return [
            {
                "scope": r["scope"], "scope_ref": r["scope_ref"],
                "grp": r["grp"], "kind": r["kind"],
                "enabled": bool(r["enabled"]),
            }
            for r in self.conn.execute(
                "SELECT * FROM bot_alert_pref WHERE chat_id=?", (chat_id,)
            ).fetchall()
        ]

    def set_alert_pref(
        self, chat_id: str, scope: str, scope_ref: str,
        grp: str, kind: str, enabled: bool,
    ) -> None:
        self.conn.execute(
            """INSERT INTO bot_alert_pref
               (chat_id, scope, scope_ref, grp, kind, enabled)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT (chat_id, scope, scope_ref, grp, kind)
               DO UPDATE SET enabled=excluded.enabled""",
            (chat_id, scope, scope_ref, grp, kind, int(enabled)),
        )
        self._commit()

    def alert_pref_enabled(
        self, chat_id: str, scope: str, scope_ref: str, grp: str, kind: str
    ) -> bool:
        """Дефолт — включено: строка появляется при первом переключении."""
        r = self.conn.execute(
            """SELECT enabled FROM bot_alert_pref
               WHERE chat_id=? AND scope=? AND scope_ref=? AND grp=? AND kind=?""",
            (chat_id, scope, scope_ref, grp, kind),
        ).fetchone()
        return bool(r["enabled"]) if r else True

    # ---------- Telegram-бот: мьютинг доставки (ТЗ п.9) ----------

    def set_mute_scope(
        self, chat_id: str, scope: str, scope_ref: str, until: int
    ) -> None:
        self.conn.execute(
            """INSERT INTO bot_mute (chat_id, scope, scope_ref, until)
               VALUES (?,?,?,?)
               ON CONFLICT (chat_id, scope, scope_ref)
               DO UPDATE SET until=excluded.until""",
            (chat_id, scope, scope_ref, until),
        )
        self._commit()

    def clear_mute_scope(self, chat_id: str, scope: str, scope_ref: str) -> None:
        self.conn.execute(
            "DELETE FROM bot_mute WHERE chat_id=? AND scope=? AND scope_ref=?",
            (chat_id, scope, scope_ref),
        )
        self._commit()

    def get_mutes(self, chat_id: str, now: int) -> list[dict[str, Any]]:
        """Активные (не истёкшие) мьюты владельца."""
        return [
            {"scope": r["scope"], "scope_ref": r["scope_ref"], "until": r["until"]}
            for r in self.conn.execute(
                "SELECT * FROM bot_mute WHERE chat_id=? AND until>?",
                (chat_id, now),
            ).fetchall()
        ]

    # ---------- reviews / notes ----------

    def add_review(self, rev: Review) -> int:
        cur = self.conn.execute(
            """INSERT INTO review (zone_id, decision, author, text, boundary_version, created_at)
               VALUES (?,?,?,?,?,?)""",
            (rev.zone_id, rev.decision, rev.author, rev.text, rev.boundary_version,
             rev.created_at),
        )
        self._commit()
        return int(cur.lastrowid)

    def get_reviews(self, zone_id: Optional[int] = None) -> list[Review]:
        q = "SELECT * FROM review"
        args: tuple = ()
        if zone_id is not None:
            q += " WHERE zone_id=?"
            args = (zone_id,)
        q += " ORDER BY created_at"
        return [
            Review(
                id=r["id"], zone_id=r["zone_id"], decision=r["decision"],
                author=r["author"], text=r["text"], boundary_version=r["boundary_version"],
                created_at=r["created_at"],
            )
            for r in self.conn.execute(q, args).fetchall()
        ]

    # ---------- review assessments (§15.1.1, §15.3) ----------

    def add_assessment(self, a: ReviewAssessment) -> int:
        cur = self.conn.execute(
            """INSERT INTO review_assessment
               (zone_id, review_id, review_decision, geometry_verdict, lifecycle_verdict,
                reason_code, evidence_source, assessed_as_of, reviewed_at,
                requires_clarification)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (a.zone_id, a.review_id, a.review_decision, a.geometry_verdict,
             a.lifecycle_verdict, a.reason_code, a.evidence_source, a.assessed_as_of,
             a.reviewed_at, int(a.requires_clarification)),
        )
        self._commit()
        return int(cur.lastrowid)

    def get_assessments(self, zone_id: int) -> list[ReviewAssessment]:
        return [
            ReviewAssessment(
                id=r["id"], zone_id=r["zone_id"], review_id=r["review_id"],
                review_decision=r["review_decision"],
                geometry_verdict=r["geometry_verdict"],
                lifecycle_verdict=r["lifecycle_verdict"],
                reason_code=r["reason_code"], evidence_source=r["evidence_source"],
                assessed_as_of=r["assessed_as_of"], reviewed_at=r["reviewed_at"],
                requires_clarification=bool(r["requires_clarification"]),
            )
            for r in self.conn.execute(
                "SELECT * FROM review_assessment WHERE zone_id=? ORDER BY reviewed_at",
                (zone_id,),
            ).fetchall()
        ]

    # ---------- boundary corrections (§15.2) ----------

    def add_boundary_correction(self, c: BoundaryCorrection) -> int:
        cur = self.conn.execute(
            """INSERT INTO boundary_correction
               (zone_id, boundary_version, original_lower, original_upper,
                corrected_lower, corrected_upper, anchor_candle_open_time,
                reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (c.zone_id, c.boundary_version, c.original_lower, c.original_upper,
             c.corrected_lower, c.corrected_upper, c.anchor_candle_open_time,
             c.reason, c.created_at),
        )
        self._commit()
        return int(cur.lastrowid)

    def get_boundary_corrections(self, zone_id: int) -> list[BoundaryCorrection]:
        return [
            BoundaryCorrection(
                id=r["id"], zone_id=r["zone_id"], boundary_version=r["boundary_version"],
                original_lower=r["original_lower"], original_upper=r["original_upper"],
                corrected_lower=r["corrected_lower"], corrected_upper=r["corrected_upper"],
                anchor_candle_open_time=r["anchor_candle_open_time"],
                reason=r["reason"], created_at=r["created_at"],
            )
            for r in self.conn.execute(
                "SELECT * FROM boundary_correction WHERE zone_id=? ORDER BY created_at",
                (zone_id,),
            ).fetchall()
        ]

    # ==========================================================================
    # LTF Confirmations (LTF_Confirmations_Window_Spec_v0.2.md, §12)
    # ==========================================================================

    def _update_ltf_row(self, table: str, row_id: int, fields: dict[str, Any]) -> None:
        """UPDATE по id для ltf_*: bool→int, Direction→str, dict/list→JSON."""
        norm: dict[str, Any] = {}
        for k, v in fields.items():
            if isinstance(v, bool):
                norm[k] = int(v)
            elif isinstance(v, Direction):
                norm[k] = v.value
            elif isinstance(v, (dict, list)):
                norm[k] = json.dumps(v, ensure_ascii=False)
            else:
                norm[k] = v
        cols = ", ".join(f'"{k}"=?' for k in norm)
        self.conn.execute(f"UPDATE {table} SET {cols} WHERE id=?", (*norm.values(), row_id))
        self._commit()
        self._bump_ltf_cache()

    # ---------- LTF: observations ----------

    def list_htf_ideas(self, instrument_id: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM htf_idea WHERE instrument_id=? ORDER BY id", (instrument_id,)
        ).fetchall()
        return [{**json.loads(r["payload"]), "id": r["id"],
                 "manual_closed_at": r["manual_closed_at"]} for r in rows]

    def save_htf_idea(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        row = self.conn.execute("SELECT payload FROM htf_idea WHERE scenario_id=?",
                                (payload["scenario_id"],)).fetchone()
        if row and row["payload"] == encoded:
            return
        self.conn.execute(
            """INSERT INTO htf_idea(instrument_id,scenario_id,rule_version,payload)
               VALUES(?,?,?,?) ON CONFLICT(scenario_id) DO UPDATE SET
               rule_version=excluded.rule_version,payload=excluded.payload""",
            (payload["instrument_id"], payload["scenario_id"], payload["rule_version"], encoded),
        )
        self._commit()
        self._bump_ltf_cache()

    def close_htf_idea(self, idea_id: int, closed_at: int) -> None:
        self.conn.execute("UPDATE htf_idea SET manual_closed_at=COALESCE(manual_closed_at,?) WHERE id=?",
                          (closed_at, idea_id))
        self._commit()
        self._bump_ltf_cache()

    def insert_ltf_observation(self, o: LtfObservation) -> LtfObservation:
        """Идемпотентно по UNIQUE(zone_id, cycle_id): повторное HTF-событие
        достижения зоны не создаёт второе наблюдение — возвращается имеющееся."""
        existing = self.get_ltf_observation_by_zone(o.zone_id, o.cycle_id)
        if existing is not None:
            return existing
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO ltf_observation
               (instrument_id, zone_id, zone_version, cycle_id, direction, state,
                activated_at, data_quality, created_at, updated_at, evidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (o.instrument_id, o.zone_id, o.zone_version, o.cycle_id,
             o.direction.value, o.state, o.activated_at, o.data_quality,
             o.created_at, o.updated_at, json.dumps(o.evidence, ensure_ascii=False)),
        )
        self._commit()
        self._bump_ltf_cache()
        if cur.rowcount == 1:
            o.id = int(cur.lastrowid)
            return o
        # дубликат/гонка: lastrowid после INSERT OR IGNORE ненадёжен — читаем по ключу
        found = self.get_ltf_observation_by_zone(o.zone_id, o.cycle_id)
        if found is None:  # pragma: no cover — защита от несогласованности схемы
            raise RuntimeError("ltf_observation: вставка проигнорирована, строка не найдена")
        return found

    def get_ltf_observation(self, observation_id: int) -> Optional[LtfObservation]:
        def load() -> Optional[LtfObservation]:
            r = self.conn.execute(
                "SELECT * FROM ltf_observation WHERE id=?", (observation_id,)
            ).fetchone()
            return self._to_ltf_observation(r) if r else None
        return self._ltf_cached(("ltf_obs", observation_id), load)

    def get_ltf_observation_by_zone(
        self, zone_id: int, cycle_id: int
    ) -> Optional[LtfObservation]:
        def load() -> Optional[LtfObservation]:
            r = self.conn.execute(
                "SELECT * FROM ltf_observation WHERE zone_id=? AND cycle_id=?",
                (zone_id, cycle_id),
            ).fetchone()
            return self._to_ltf_observation(r) if r else None
        return self._ltf_cached(("ltf_obs_zone", zone_id, cycle_id), load)

    def list_stale_ltf_observations(
        self, states: tuple[str, ...], updated_before_ms: int
    ) -> list[LtfObservation]:
        """Наблюдения в заданных состояниях без обновлений с updated_before_ms
        (автоархивация по неактивности; индекс ix_ltf_observation_state)."""
        marks = ",".join("?" for _ in states)
        q = (
            f"SELECT * FROM ltf_observation WHERE state IN ({marks})"
            " AND updated_at < ? ORDER BY id"
        )
        return [
            self._to_ltf_observation(r)
            for r in self.conn.execute(q, (*states, updated_before_ms)).fetchall()
        ]

    def update_ltf_observation(self, observation_id: int, **fields: Any) -> None:
        self._update_ltf_row("ltf_observation", observation_id, fields)

    def list_ltf_observations(
        self,
        state: Optional[str] = None,
        instrument_id: Optional[int] = None,
    ) -> list[LtfObservation]:
        def load() -> list[LtfObservation]:
            q = "SELECT * FROM ltf_observation WHERE 1=1"
            args: list[Any] = []
            if state is not None:
                q += " AND state=?"
                args.append(state)
            if instrument_id is not None:
                q += " AND instrument_id=?"
                args.append(instrument_id)
            q += " ORDER BY updated_at DESC, id DESC"
            return [
                self._to_ltf_observation(r)
                for r in self.conn.execute(q, args).fetchall()
            ]
        return self._ltf_cached(("ltf_obs_list", state, instrument_id), load)

    @staticmethod
    def _to_ltf_observation(r: sqlite3.Row) -> LtfObservation:
        return LtfObservation(
            id=r["id"], instrument_id=r["instrument_id"], zone_id=r["zone_id"],
            zone_version=r["zone_version"], cycle_id=r["cycle_id"],
            direction=Direction(r["direction"]), state=r["state"],
            activated_at=r["activated_at"], data_quality=r["data_quality"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            evidence=json.loads(r["evidence"] or "{}"),
        )

    # ---------- LTF: scenarios ----------

    def insert_ltf_scenario(self, s: LtfScenario) -> LtfScenario:
        cur = self.conn.execute(
            """INSERT INTO ltf_scenario
               (observation_id, direction, "trigger", stage, state,
                trigger_event_id, cancellation_reason, cancelled_at,
                origin_break_event_id, origin_movement_id, structural_epoch_id,
                reverse_break_level_price, reverse_break_pivot_id,
                reverse_break_confirmed_at, last_processed_close,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s.observation_id, s.direction.value, s.trigger, s.stage, s.state,
             s.trigger_event_id, s.cancellation_reason, s.cancelled_at,
             s.origin_break_event_id, s.origin_movement_id, s.structural_epoch_id,
             s.reverse_break_level_price, s.reverse_break_pivot_id,
             s.reverse_break_confirmed_at, s.last_processed_close,
             s.created_at, s.updated_at),
        )
        self._commit()
        self._bump_ltf_cache()
        s.id = int(cur.lastrowid)
        return s

    def get_ltf_scenario(self, scenario_id: int) -> Optional[LtfScenario]:
        def load() -> Optional[LtfScenario]:
            r = self.conn.execute(
                "SELECT * FROM ltf_scenario WHERE id=?", (scenario_id,)
            ).fetchone()
            return self._to_ltf_scenario(r) if r else None
        return self._ltf_cached(("ltf_sc", scenario_id), load)

    def update_ltf_scenario(self, scenario_id: int, **fields: Any) -> None:
        self._update_ltf_row("ltf_scenario", scenario_id, fields)

    def list_ltf_scenarios(
        self,
        observation_id: Optional[int] = None,
        state: Optional[str] = None,
    ) -> list[LtfScenario]:
        def load() -> list[LtfScenario]:
            q = "SELECT * FROM ltf_scenario WHERE 1=1"
            args: list[Any] = []
            if observation_id is not None:
                q += " AND observation_id=?"
                args.append(observation_id)
            if state is not None:
                q += " AND state=?"
                args.append(state)
            q += " ORDER BY id"
            return [self._to_ltf_scenario(r) for r in self.conn.execute(q, args).fetchall()]
        return self._ltf_cached(("ltf_sc_list", observation_id, state), load)

    def get_active_ltf_scenario(self, observation_id: int) -> Optional[LtfScenario]:
        """Живой сценарий наблюдения: ждёт диапазон или отслеживает входы."""
        def load() -> Optional[LtfScenario]:
            r = self.conn.execute(
                """SELECT * FROM ltf_scenario
                   WHERE observation_id=? AND state IN ('range_pending','monitoring_entries')
                   ORDER BY id DESC LIMIT 1""",
                (observation_id,),
            ).fetchone()
            return self._to_ltf_scenario(r) if r else None
        return self._ltf_cached(("ltf_sc_active", observation_id), load)

    def list_ltf_scenarios_for_instrument(self, instrument_id: int) -> list[LtfScenario]:
        """Сценарии инструмента в порядке наблюдений, затем id сценария."""
        def load() -> list[LtfScenario]:
            rows = self.conn.execute(
                """SELECT s.* FROM ltf_scenario s
                   JOIN ltf_observation o ON o.id = s.observation_id
                   WHERE o.instrument_id=?
                   ORDER BY o.updated_at DESC, o.id DESC, s.id""",
                (instrument_id,),
            ).fetchall()
            return [self._to_ltf_scenario(r) for r in rows]
        return self._ltf_cached(("ltf_sc_ins", instrument_id), load)

    def ltf_projection_bundle(self, instrument_id: int) -> dict[str, Any]:
        """Дети сценариев инструмента одним проходом на таблицу.

        Порядок внутри списков совпадает с list_ltf_*: события структуры
        по occurred_at, движения по start_at, привязки по added_at,
        диапазоны по version, тесты по touch_at. Журнал ltf_event — не
        больше 1000 последних на сценарий, свежие первыми: context_flags
        оставляет последнюю запись htf_fvg50 из этого окна.
        """
        def load() -> dict[str, Any]:
            scenarios = self.list_ltf_scenarios_for_instrument(instrument_id)
            by_obs: dict[int, list[LtfScenario]] = {}
            for sc in scenarios:
                by_obs.setdefault(sc.observation_id, []).append(sc)
            structure_events = self._bundle_by_scenario(
                instrument_id,
                """SELECT e.* FROM ltf_structure_event e
                   JOIN ltf_scenario s ON s.id = e.scenario_id
                   JOIN ltf_observation o ON o.id = s.observation_id
                   WHERE o.instrument_id=?
                   ORDER BY e.occurred_at, e.id""",
                self._to_ltf_structure_event,
            )
            movements = self._bundle_by_scenario(
                instrument_id,
                """SELECT m.* FROM ltf_movement m
                   JOIN ltf_scenario s ON s.id = m.scenario_id
                   JOIN ltf_observation o ON o.id = s.observation_id
                   WHERE o.instrument_id=?
                   ORDER BY m.start_at, m.id""",
                self._to_ltf_movement,
            )
            entries = self._bundle_by_scenario(
                instrument_id,
                """SELECT e.* FROM ltf_scenario_entry e
                   JOIN ltf_scenario s ON s.id = e.scenario_id
                   JOIN ltf_observation o ON o.id = s.observation_id
                   WHERE o.instrument_id=?
                   ORDER BY e.added_at, e.id""",
                self._to_ltf_scenario_entry,
            )
            ranges = self._bundle_by_scenario(
                instrument_id,
                """SELECT r.* FROM ltf_range r
                   JOIN ltf_scenario s ON s.id = r.scenario_id
                   JOIN ltf_observation o ON o.id = s.observation_id
                   WHERE o.instrument_id=?
                   ORDER BY r.version""",
                self._to_ltf_range,
            )
            tests = self._bundle_by_scenario(
                instrument_id,
                """SELECT t.* FROM ltf_liquidity_test t
                   JOIN ltf_scenario s ON s.id = t.scenario_id
                   JOIN ltf_observation o ON o.id = s.observation_id
                   WHERE o.instrument_id=?
                   ORDER BY t.touch_at, t.id""",
                self._to_ltf_liquidity_test,
            )
            ltf_events = self._bundle_by_scenario(
                instrument_id,
                """SELECT * FROM (
                     SELECT e.*, ROW_NUMBER() OVER (
                       PARTITION BY e.scenario_id
                       ORDER BY e.occurred_at DESC, e.id DESC
                     ) AS _rn
                     FROM ltf_event e
                     JOIN ltf_scenario s ON s.id = e.scenario_id
                     JOIN ltf_observation o ON o.id = s.observation_id
                     WHERE o.instrument_id=?
                   ) ranked
                   WHERE _rn <= 1000
                   ORDER BY occurred_at DESC, id DESC""",
                self._to_ltf_event,
            )
            movements_by_id = {
                m.id: m for rows in movements.values() for m in rows if m.id is not None
            }
            prefix = "htf_idea:discovered:"
            discovered: dict[int, str] = {}
            for key, value in self.meta_prefix(prefix).items():
                tail = key[len(prefix):]
                if tail.isdigit():
                    discovered[int(tail)] = value
            return {
                "scenarios_by_obs": by_obs,
                "structure_events": structure_events,
                "movements": movements,
                "movements_by_id": movements_by_id,
                "entries": entries,
                "ranges": ranges,
                "tests": tests,
                "ltf_events": ltf_events,
                "discovered": discovered,
            }
        return self._ltf_cached(("ltf_proj", instrument_id), load)

    def _bundle_by_scenario(self, instrument_id: int, sql: str, convert) -> dict[int, list]:
        grouped: dict[int, list] = {}
        for row in self.conn.execute(sql, (instrument_id,)).fetchall():
            item = convert(row)
            if item.scenario_id is None:
                continue
            grouped.setdefault(item.scenario_id, []).append(item)
        return grouped

    @staticmethod
    def _to_ltf_scenario(r: sqlite3.Row) -> LtfScenario:
        return LtfScenario(
            id=r["id"], observation_id=r["observation_id"],
            direction=Direction(r["direction"]), trigger=r["trigger"],
            stage=r["stage"], state=r["state"],
            trigger_event_id=r["trigger_event_id"],
            cancellation_reason=r["cancellation_reason"],
            cancelled_at=r["cancelled_at"],
            origin_break_event_id=r["origin_break_event_id"],
            origin_movement_id=r["origin_movement_id"],
            structural_epoch_id=r["structural_epoch_id"],
            reverse_break_level_price=r["reverse_break_level_price"],
            reverse_break_pivot_id=r["reverse_break_pivot_id"],
            reverse_break_confirmed_at=r["reverse_break_confirmed_at"],
            last_processed_close=r["last_processed_close"],
            created_at=r["created_at"], updated_at=r["updated_at"],
        )

    # ---------- LTF: pivots ----------

    def insert_ltf_pivot(self, p: LtfPivot) -> int:
        cur = self.conn.execute(
            """INSERT INTO ltf_pivot
               (instrument_id, price, kind, pivot_at, confirmed_at, role,
                role_assigned_at, "left", "right", candle_open_time, state,
                calc_version_id, superseded_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (p.instrument_id, p.price, p.kind, p.pivot_at, p.confirmed_at, p.role,
             p.role_assigned_at, p.left, p.right, p.candle_open_time, p.state,
             p.calc_version_id, p.superseded_by),
        )
        self._commit()
        self._bump_ltf_cache()
        return int(cur.lastrowid)

    def get_or_create_calc_version(
        self, kind: str, params: dict[str, Any], rule_version: str,
        created_at: int,
    ) -> tuple[int, bool]:
        """Текущая версия расчёта kind с теми же params/rule_version или
        новая строка (L03): изменение параметров создаёт новую версию,
        предыдущая сохраняется. Возвращает (id, created)."""
        blob = json.dumps(params, sort_keys=True)
        r = self.conn.execute(
            "SELECT id, params, rule_version FROM calc_version "
            "WHERE kind=? ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        if (
            r is not None and r["params"] == blob
            and r["rule_version"] == rule_version
        ):
            return int(r["id"]), False
        cur = self.conn.execute(
            "INSERT INTO calc_version (kind, params, rule_version, created_at)"
            " VALUES (?,?,?,?)",
            (kind, blob, rule_version, created_at),
        )
        self._commit()
        return int(cur.lastrowid), True

    def get_calc_version(self, calc_version_id: int) -> Optional[dict[str, Any]]:
        r = self.conn.execute(
            "SELECT * FROM calc_version WHERE id=?", (calc_version_id,)
        ).fetchone()
        if r is None:
            return None
        return {
            "id": r["id"], "kind": r["kind"],
            "params": json.loads(r["params"] or "{}"),
            "rule_version": r["rule_version"], "created_at": r["created_at"],
        }

    def supersede_ltf_pivots(
        self, instrument_id: int, calc_version_id: int
    ) -> int:
        """Пометить действующие pivots инструмента заменёнными версией
        calc_version_id (L03): строки и журнал ролей сохраняются, ссылки
        исторических сценариев/движений остаются разрешимыми; из текущей
        выдачи (list_ltf_pivots) они исключаются."""
        cur = self.conn.execute(
            "UPDATE ltf_pivot SET superseded_by=? "
            "WHERE instrument_id=? AND superseded_by IS NULL",
            (calc_version_id, instrument_id),
        )
        self._commit()
        self._bump_ltf_cache()
        return cur.rowcount

    def get_ltf_pivot(self, pivot_id: int) -> Optional[LtfPivot]:
        def load() -> Optional[LtfPivot]:
            r = self.conn.execute(
                "SELECT * FROM ltf_pivot WHERE id=?", (pivot_id,)
            ).fetchone()
            return self._to_ltf_pivot(r) if r else None
        return self._ltf_cached(("ltf_pivot", pivot_id), load)

    def list_ltf_pivots(
        self, instrument_id: int, since_ms: Optional[int] = None,
        include_superseded: bool = False,
    ) -> list[LtfPivot]:
        """Действующие pivots инструмента; include_superseded=True — включая
        заменённые версиями пересчёта (L03, исторические опоры)."""
        def load() -> list[LtfPivot]:
            q = "SELECT * FROM ltf_pivot WHERE instrument_id=?"
            args: list[Any] = [instrument_id]
            if since_ms is not None:
                q += " AND pivot_at>=?"
                args.append(since_ms)
            if not include_superseded:
                q += " AND (superseded_by IS NULL)"
            q += " ORDER BY pivot_at, id"
            return [self._to_ltf_pivot(r) for r in self.conn.execute(q, args).fetchall()]
        return self._ltf_cached(
            ("ltf_pivots", instrument_id, since_ms, include_superseded), load
        )

    def delete_ltf_pivots(self, instrument_id: int) -> int:
        """Удалить все pivots инструмента. Журнал ролей удаляется первым —
        он ссылается на ltf_pivot по FK. ВНИМАНИЕ (L03): для перестройки по
        новым l/r используется supersede_ltf_pivots — удаление разрывает
        исторические ссылки сценариев (anchor_*_pivot_id, start/end_pivot_id
        читаются как None). Оставлено для явной очистки в тестах."""
        self.conn.execute(
            """DELETE FROM ltf_pivot_role_log WHERE pivot_id IN
               (SELECT id FROM ltf_pivot WHERE instrument_id=?)""",
            (instrument_id,),
        )
        cur = self.conn.execute(
            "DELETE FROM ltf_pivot WHERE instrument_id=?", (instrument_id,)
        )
        self._commit()
        self._bump_ltf_cache()
        return cur.rowcount

    def update_ltf_pivot_role(self, pivot_id: int, new_role: str, changed_at: int) -> None:
        """Пересмотр структурной роли (§5.2): история не стирается — каждое
        изменение дополнительно пишется в ltf_pivot_role_log. Если роль не
        изменилась относительно сохранённой, не пишет ни UPDATE, ни строку лога."""
        cur = self.get_ltf_pivot(pivot_id)
        old_role = cur.role if cur else None
        if old_role == new_role:
            return
        self.conn.execute(
            "UPDATE ltf_pivot SET role=?, role_assigned_at=? WHERE id=?",
            (new_role, changed_at, pivot_id),
        )
        self.conn.execute(
            """INSERT INTO ltf_pivot_role_log (pivot_id, old_role, new_role, changed_at)
               VALUES (?,?,?,?)""",
            (pivot_id, old_role, new_role, changed_at),
        )
        self._commit()
        self._bump_ltf_cache()

    def list_ltf_pivot_role_log(self, pivot_id: int) -> list[dict[str, Any]]:
        return [
            {"id": r["id"], "pivot_id": r["pivot_id"], "old_role": r["old_role"],
             "new_role": r["new_role"], "changed_at": r["changed_at"]}
            for r in self.conn.execute(
                "SELECT * FROM ltf_pivot_role_log WHERE pivot_id=? ORDER BY id",
                (pivot_id,),
            ).fetchall()
        ]

    def list_ltf_pivot_role_logs(
        self, instrument_id: int, pivot_ids: Optional[list[int]] = None,
    ) -> dict[int, list[dict[str, Any]]]:
        """Журнал ролей опор инструмента, по pivot_id и порядку id.

        Нужен снимку H1 на историческом as_of: роль берётся известная тогда,
        а не финальная из будущего. ``pivot_ids`` ограничивает чтение опорами,
        чья роль назначена позже as_of. Чтение, без записи.
        """
        if pivot_ids is not None and not pivot_ids:
            return {}
        grouped: dict[int, list[dict[str, Any]]] = {}
        chunks: list[Optional[list[int]]]
        if pivot_ids is None:
            chunks = [None]
        else:
            chunks = [pivot_ids[i:i + 400] for i in range(0, len(pivot_ids), 400)]
        for chunk in chunks:
            q = (
                """SELECT l.id, l.pivot_id, l.old_role, l.new_role, l.changed_at
                   FROM ltf_pivot_role_log l
                   JOIN ltf_pivot p ON p.id = l.pivot_id
                   WHERE p.instrument_id=?"""
            )
            args: list[Any] = [instrument_id]
            if chunk:
                q += " AND l.pivot_id IN (" + ",".join("?" * len(chunk)) + ")"
                args.extend(chunk)
            q += " ORDER BY l.pivot_id, l.id"
            for r in self.conn.execute(q, args).fetchall():
                grouped.setdefault(int(r["pivot_id"]), []).append({
                    "id": r["id"], "pivot_id": r["pivot_id"],
                    "old_role": r["old_role"], "new_role": r["new_role"],
                    "changed_at": r["changed_at"],
                })
        return grouped

    @staticmethod
    def _to_ltf_pivot(r: sqlite3.Row) -> LtfPivot:
        keys = r.keys()
        return LtfPivot(
            id=r["id"], instrument_id=r["instrument_id"], price=r["price"],
            kind=r["kind"], pivot_at=r["pivot_at"], confirmed_at=r["confirmed_at"],
            role=r["role"], role_assigned_at=r["role_assigned_at"],
            left=r["left"], right=r["right"],
            candle_open_time=r["candle_open_time"], state=r["state"],
            calc_version_id=(
                r["calc_version_id"] if "calc_version_id" in keys else None
            ),
            superseded_by=(
                r["superseded_by"] if "superseded_by" in keys else None
            ),
        )

    # ---------- LTF: structure events (BOS/SMS, §6) ----------

    def insert_ltf_structure_event(self, e: LtfStructureEvent) -> LtfStructureEvent:
        """Идемпотентно по UNIQUE(scenario_id, level_key, stage) — §6: один
        слом на уровень/этап, повторный проход движка не дублирует событие."""
        existing = self._get_ltf_structure_event_by_key(
            e.scenario_id, e.level_key, e.stage
        )
        if existing is not None:
            return existing
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO ltf_structure_event
               (scenario_id, kind, stage, direction, break_level,
                break_candle_open_time, occurred_at, detected_at, ref_pivot_ids,
                accompanying, level_key, evidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (e.scenario_id, e.kind, e.stage, e.direction.value, e.break_level,
             e.break_candle_open_time, e.occurred_at, e.detected_at,
             json.dumps(e.ref_pivot_ids), int(e.accompanying), e.level_key,
             json.dumps(e.evidence, ensure_ascii=False)),
        )
        self._commit()
        self._bump_ltf_cache()
        if cur.rowcount == 1:
            e.id = int(cur.lastrowid)
            return e
        found = self._get_ltf_structure_event_by_key(e.scenario_id, e.level_key, e.stage)
        if found is None:  # pragma: no cover
            raise RuntimeError("ltf_structure_event: вставка проигнорирована, строка не найдена")
        return found

    def _get_ltf_structure_event_by_key(
        self, scenario_id: int, level_key: str, stage: str
    ) -> Optional[LtfStructureEvent]:
        r = self.conn.execute(
            """SELECT * FROM ltf_structure_event
               WHERE scenario_id=? AND level_key=? AND stage=?""",
            (scenario_id, level_key, stage),
        ).fetchone()
        return self._to_ltf_structure_event(r) if r else None

    def has_ltf_structure_event(self, scenario_id: int, level_key: str, stage: str) -> bool:
        def load() -> bool:
            row = self.conn.execute(
                """SELECT 1 FROM ltf_structure_event
                   WHERE scenario_id=? AND level_key=? AND stage=?""",
                (scenario_id, level_key, stage),
            ).fetchone()
            return row is not None
        return self._ltf_cached(("ltf_has_se", scenario_id, level_key, stage), load)

    def list_ltf_structure_events(self, scenario_id: int) -> list[LtfStructureEvent]:
        def load() -> list[LtfStructureEvent]:
            return [
                self._to_ltf_structure_event(r)
                for r in self.conn.execute(
                    "SELECT * FROM ltf_structure_event WHERE scenario_id=? "
                    "ORDER BY occurred_at, id",
                    (scenario_id,),
                ).fetchall()
            ]
        return self._ltf_cached(("ltf_se_list", scenario_id), load)

    def list_ltf_structure_links(self, instrument_id: int) -> list[tuple]:
        """(level_key, kind, stage, occurred_at, scenario_id) одним запросом.

        Порядок сценариев совпадает с обходом наблюдений в _scenario_index.
        """
        rows = self.conn.execute(
            """SELECT e.level_key AS level_key, e.kind AS kind, e.stage AS stage,
                      e.occurred_at AS occurred_at, s.id AS scenario_id
               FROM ltf_structure_event e
               JOIN ltf_scenario s ON s.id = e.scenario_id
               JOIN ltf_observation o ON o.id = s.observation_id
               WHERE o.instrument_id=?
               ORDER BY o.updated_at DESC, o.id DESC, s.id, e.occurred_at, e.id""",
            (instrument_id,),
        ).fetchall()
        return [
            (r["level_key"], r["kind"], r["stage"], r["occurred_at"], int(r["scenario_id"]))
            for r in rows
        ]

    @staticmethod
    def _to_ltf_structure_event(r: sqlite3.Row) -> LtfStructureEvent:
        return LtfStructureEvent(
            id=r["id"], scenario_id=r["scenario_id"], kind=r["kind"],
            stage=r["stage"], direction=Direction(r["direction"]),
            break_level=r["break_level"],
            break_candle_open_time=r["break_candle_open_time"],
            occurred_at=r["occurred_at"], detected_at=r["detected_at"],
            level_key=r["level_key"],
            ref_pivot_ids=json.loads(r["ref_pivot_ids"] or "[]"),
            accompanying=bool(r["accompanying"]),
            evidence=json.loads(r["evidence"] or "{}"),
        )

    # ---------- LTF: movements (§8.1) ----------

    def insert_ltf_movement(self, m: LtfMovement) -> int:
        cur = self.conn.execute(
            """INSERT INTO ltf_movement
               (scenario_id, start_pivot_id, end_pivot_id, start_at, end_at,
                break_event_id, confirmed_at, source_candle_ids, provenance_status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (m.scenario_id, m.start_pivot_id, m.end_pivot_id, m.start_at, m.end_at,
             m.break_event_id, m.confirmed_at, json.dumps(m.source_candle_ids),
             m.provenance_status),
        )
        self._commit()
        self._bump_ltf_cache()
        return int(cur.lastrowid)

    def get_ltf_movement(self, movement_id: int) -> Optional[LtfMovement]:
        r = self.conn.execute(
            "SELECT * FROM ltf_movement WHERE id=?", (movement_id,)
        ).fetchone()
        return self._to_ltf_movement(r) if r else None

    def list_ltf_movements(self, scenario_id: int) -> list[LtfMovement]:
        def load() -> list[LtfMovement]:
            return [
                self._to_ltf_movement(r)
                for r in self.conn.execute(
                    "SELECT * FROM ltf_movement WHERE scenario_id=? ORDER BY start_at, id",
                    (scenario_id,),
                ).fetchall()
            ]
        return self._ltf_cached(("ltf_mv_list", scenario_id), load)

    @staticmethod
    def _to_ltf_movement(r: sqlite3.Row) -> LtfMovement:
        return LtfMovement(
            id=r["id"], scenario_id=r["scenario_id"],
            start_pivot_id=r["start_pivot_id"], end_pivot_id=r["end_pivot_id"],
            start_at=r["start_at"], end_at=r["end_at"],
            break_event_id=r["break_event_id"], confirmed_at=r["confirmed_at"],
            source_candle_ids=json.loads(r["source_candle_ids"] or "[]"),
            provenance_status=r["provenance_status"],
        )

    # ---------- LTF: ranges (Premium/Discount, §7) ----------

    def insert_ltf_range(self, rng: LtfRange) -> int:
        """Новая версия диапазона; UNIQUE(scenario_id, version) не даёт
        переписать уже опубликованную геометрию задним числом."""
        cur = self.conn.execute(
            """INSERT INTO ltf_range
               (scenario_id, version, lower, upper, mid, anchor_low_pivot_id,
                anchor_high_pivot_id, available_at, prev_version_id,
                kind, anchor_policy, structural_epoch_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rng.scenario_id, rng.version, rng.lower, rng.upper, rng.mid,
             rng.anchor_low_pivot_id, rng.anchor_high_pivot_id, rng.available_at,
             rng.prev_version_id, rng.kind, rng.anchor_policy,
             rng.structural_epoch_id),
        )
        self._commit()
        self._bump_ltf_cache()
        return int(cur.lastrowid)

    def get_current_ltf_range(self, scenario_id: int) -> Optional[LtfRange]:
        def load() -> Optional[LtfRange]:
            r = self.conn.execute(
                """SELECT * FROM ltf_range WHERE scenario_id=?
                   ORDER BY version DESC LIMIT 1""",
                (scenario_id,),
            ).fetchone()
            return self._to_ltf_range(r) if r else None
        return self._ltf_cached(("ltf_rng_cur", scenario_id), load)

    def list_ltf_ranges(self, scenario_id: int) -> list[LtfRange]:
        def load() -> list[LtfRange]:
            return [
                self._to_ltf_range(r)
                for r in self.conn.execute(
                    "SELECT * FROM ltf_range WHERE scenario_id=? ORDER BY version",
                    (scenario_id,),
                ).fetchall()
            ]
        return self._ltf_cached(("ltf_rng_list", scenario_id), load)

    # ---------- LTF: агрегаты read model (ТЗ «LTF Current Setup» §14) ----------

    def get_ltf_range_versions(self) -> dict[int, int]:
        """scenario_id → max(version) диапазона, одним запросом (без N+1)."""
        return {
            int(r["scenario_id"]): int(r["v"])
            for r in self.conn.execute(
                "SELECT scenario_id, MAX(version) AS v FROM ltf_range "
                "GROUP BY scenario_id"
            ).fetchall()
        }

    def list_ltf_eligible_zones(self) -> list[dict[str, Any]]:
        """Подходящие зоны активных сценариев на текущей версии диапазона —
        один агрегирующий запрос для всех инструментов (eligible_count
        списка активов без N+1, §4.2/§14). reason='' — строки до миграции:
        пригодность выводится из state (fallback eligibility.entry_reason:
        fresh/tested+eligible → ok). §18 (L01): контекстно допущенные FVG вне
        Premium входят, когда у сценария полный контекст — оба факта
        context_update (counter_sweep и htf_fvg50); правило зеркалит
        eligibility.evaluate_final (допуск по обычному правилу — состояния
        fresh и tested с reason ok, повторно допущенная tested-зона не
        выпадает из счётчика)."""
        q = """
            SELECT o.instrument_id AS instrument_id,
                   se.scenario_id AS scenario_id,
                   z.id AS entry_zone_id, z.lower AS lower, z.upper AS upper
            FROM ltf_scenario_entry se
            JOIN ltf_scenario s ON s.id = se.scenario_id
            JOIN ltf_observation o ON o.id = s.observation_id
            JOIN ltf_entry_zone z ON z.id = se.entry_zone_id
            WHERE s.state IN ('range_pending', 'monitoring_entries')
              AND se.range_version = COALESCE((
                  SELECT MAX(r.version) FROM ltf_range r
                  WHERE r.scenario_id = se.scenario_id
              ), 0)
              AND (
                  (se.state IN ('fresh', 'tested') AND se.eligible = 1
                   AND (se.reason = 'ok' OR se.reason = ''))
                  OR (se.state = 'out_of_range' AND se.reason = 'outside_pd'
                      AND z.type = 'FVG'
                      AND EXISTS (
                          SELECT 1 FROM ltf_event e1
                          WHERE e1.scenario_id = se.scenario_id
                            AND e1.kind = 'context_update'
                            AND json_extract(e1.payload, '$.fact')
                                = 'counter_sweep')
                      AND EXISTS (
                          SELECT 1 FROM ltf_event e2
                          WHERE e2.scenario_id = se.scenario_id
                            AND e2.kind = 'context_update'
                            AND json_extract(e2.payload, '$.fact')
                                = 'htf_fvg50'))
              )
        """
        return [dict(r) for r in self.conn.execute(q).fetchall()]

    def get_ltf_last_event_at(self) -> dict[int, int]:
        """instrument_id → occurred_at последнего события LTF (агрегат, §4.2)."""
        return {
            int(r["instrument_id"]): int(r["t"])
            for r in self.conn.execute(
                "SELECT o.instrument_id AS instrument_id, "
                "MAX(e.occurred_at) AS t "
                "FROM ltf_event e "
                "JOIN ltf_observation o ON o.id = e.observation_id "
                "GROUP BY o.instrument_id"
            ).fetchall()
        }

    def count_candidate_zones(self) -> dict[int, int]:
        """instrument_id → число зон на ручной проверке (кандидаты без ревью) —
        один агрегатный запрос для приоритета внимания списка активов (L06,
        без N+1). Фильтр совпадает с очередью /api/candidates."""
        return {
            int(r["instrument_id"]): int(r["n"])
            for r in self.conn.execute(
                "SELECT instrument_id, COUNT(*) AS n FROM zone "
                "WHERE status='candidate' "
                "AND market_validity='active' AND display_until IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM review r WHERE r.zone_id = zone.id) "
                "GROUP BY instrument_id"
            ).fetchall()
        }

    @staticmethod
    def _to_ltf_range(r: sqlite3.Row) -> LtfRange:
        return LtfRange(
            id=r["id"], scenario_id=r["scenario_id"], version=r["version"],
            lower=r["lower"], upper=r["upper"], mid=r["mid"],
            anchor_low_pivot_id=r["anchor_low_pivot_id"],
            anchor_high_pivot_id=r["anchor_high_pivot_id"],
            available_at=r["available_at"], prev_version_id=r["prev_version_id"],
            kind=r["kind"], anchor_policy=r["anchor_policy"],
            structural_epoch_id=r["structural_epoch_id"],
        )

    # ---------- LTF: entry zones (§8) ----------

    def insert_ltf_entry_zone(self, z: LtfEntryZone) -> LtfEntryZone:
        """Идемпотентно по UNIQUE(instrument, type, direction, границы,
        formed_at, movement_id): повторное обнаружение той же зоны тем же
        движением не создаёт дубликат."""
        existing = self._get_ltf_entry_zone_by_key(z)
        if existing is not None:
            return existing
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO ltf_entry_zone
               (instrument_id, type, direction, lower, upper, formed_at,
                confirmed_at, movement_id, first_test_at, validity,
                max_test_depth, test_extreme, source, rule_version, evidence)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (z.instrument_id, z.type, z.direction.value, z.lower, z.upper,
             z.formed_at, z.confirmed_at, z.movement_id, z.first_test_at,
             z.validity, z.max_test_depth, z.test_extreme, z.source,
             z.rule_version, z.evidence_json()),
        )
        self._commit()
        self._bump_ltf_cache()
        if cur.rowcount == 1:
            z.id = int(cur.lastrowid)
            return z
        found = self._get_ltf_entry_zone_by_key(z)
        if found is None:  # pragma: no cover
            raise RuntimeError("ltf_entry_zone: вставка проигнорирована, строка не найдена")
        return found

    def _get_ltf_entry_zone_by_key(self, z: LtfEntryZone) -> Optional[LtfEntryZone]:
        r = self.conn.execute(
            """SELECT * FROM ltf_entry_zone
               WHERE instrument_id=? AND type=? AND direction=? AND lower=?
                 AND upper=? AND formed_at=? AND movement_id=?""",
            (z.instrument_id, z.type, z.direction.value, z.lower, z.upper,
             z.formed_at, z.movement_id),
        ).fetchone()
        return self._to_ltf_entry_zone(r) if r else None

    def get_ltf_entry_zone(self, entry_zone_id: int) -> Optional[LtfEntryZone]:
        def load() -> Optional[LtfEntryZone]:
            r = self.conn.execute(
                "SELECT * FROM ltf_entry_zone WHERE id=?", (entry_zone_id,)
            ).fetchone()
            return self._to_ltf_entry_zone(r) if r else None
        return self._ltf_cached(("ltf_ez", entry_zone_id), load)

    def update_ltf_entry_zone(self, entry_zone_id: int, **fields: Any) -> None:
        self._update_ltf_row("ltf_entry_zone", entry_zone_id, fields)

    def list_ltf_entry_zones(
        self,
        movement_id: Optional[int] = None,
        instrument_id: Optional[int] = None,
    ) -> list[LtfEntryZone]:
        q = "SELECT * FROM ltf_entry_zone WHERE 1=1"
        args: list[Any] = []
        if movement_id is not None:
            q += " AND movement_id=?"
            args.append(movement_id)
        if instrument_id is not None:
            q += " AND instrument_id=?"
            args.append(instrument_id)
        q += " ORDER BY formed_at, id"
        return [self._to_ltf_entry_zone(r) for r in self.conn.execute(q, args).fetchall()]

    @staticmethod
    def _to_ltf_entry_zone(r: sqlite3.Row) -> LtfEntryZone:
        keys = r.keys()
        return LtfEntryZone(
            id=r["id"], instrument_id=r["instrument_id"], type=r["type"],
            direction=Direction(r["direction"]), lower=r["lower"], upper=r["upper"],
            formed_at=r["formed_at"], confirmed_at=r["confirmed_at"],
            movement_id=r["movement_id"], first_test_at=r["first_test_at"],
            validity=r["validity"],
            max_test_depth=r["max_test_depth"] if "max_test_depth" in keys else 0.0,
            test_extreme=r["test_extreme"] if "test_extreme" in keys else None,
            source=r["source"],
            rule_version=r["rule_version"],
            evidence=json.loads(r["evidence"] or "{}"),
        )

    # ---------- LTF: scenario entries (§7, §8.5) ----------

    def upsert_ltf_scenario_entry(self, se: LtfScenarioEntry) -> LtfScenarioEntry:
        """INSERT/UPDATE по UNIQUE(scenario_id, entry_zone_id, range_version):
        пересчёт состояния на той же версии диапазона не плодит строки."""
        self.conn.execute(
            """INSERT INTO ltf_scenario_entry
               (scenario_id, entry_zone_id, range_version, eligible, overlap,
                state, reason, added_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT (scenario_id, entry_zone_id, range_version)
               DO UPDATE SET eligible=excluded.eligible, overlap=excluded.overlap,
                             state=excluded.state, reason=excluded.reason,
                             updated_at=excluded.updated_at""",
            (se.scenario_id, se.entry_zone_id, se.range_version, int(se.eligible),
             se.overlap, se.state, se.reason, se.added_at, se.updated_at),
        )
        self._commit()
        self._bump_ltf_cache()
        # после upsert lastrowid ненадёжен — читаем строку по уникальному ключу
        r = self.conn.execute(
            """SELECT * FROM ltf_scenario_entry
               WHERE scenario_id=? AND entry_zone_id=? AND range_version=?""",
            (se.scenario_id, se.entry_zone_id, se.range_version),
        ).fetchone()
        return self._to_ltf_scenario_entry(r)

    def set_ltf_scenario_entry_reason(self, entry_id: int, reason: str) -> None:
        """Записать reason привязки. Используется ремонтом терминальных
        уровней; обычный пересчёт по-прежнему идёт через upsert."""
        self._update_ltf_row(
            "ltf_scenario_entry", entry_id,
            {"reason": reason, "updated_at": now_ms()},
        )

    def list_all_ltf_scenario_entries(self) -> list[LtfScenarioEntry]:
        rows = self.conn.execute(
            "SELECT * FROM ltf_scenario_entry ORDER BY id"
        ).fetchall()
        return [self._to_ltf_scenario_entry(r) for r in rows]

    def get_ltf_scenario_entry(self, entry_id: int) -> Optional[LtfScenarioEntry]:
        r = self.conn.execute(
            "SELECT * FROM ltf_scenario_entry WHERE id=?", (entry_id,)
        ).fetchone()
        return self._to_ltf_scenario_entry(r) if r else None

    def list_ltf_scenario_entries(
        self, scenario_id: int, state: Optional[str] = None
    ) -> list[LtfScenarioEntry]:
        def load() -> list[LtfScenarioEntry]:
            q = "SELECT * FROM ltf_scenario_entry WHERE scenario_id=?"
            args: list[Any] = [scenario_id]
            if state is not None:
                q += " AND state=?"
                args.append(state)
            q += " ORDER BY added_at, id"
            return [
                self._to_ltf_scenario_entry(r)
                for r in self.conn.execute(q, args).fetchall()
            ]
        return self._ltf_cached(("ltf_sce", scenario_id, state), load)

    def list_ltf_scenario_entries_by_zone(
        self, entry_zone_id: int
    ) -> list[LtfScenarioEntry]:
        """Все привязки зоны к сценариям (все версии диапазона) — контекст
        для экспорта разметки."""
        return [
            self._to_ltf_scenario_entry(r)
            for r in self.conn.execute(
                "SELECT * FROM ltf_scenario_entry WHERE entry_zone_id=? "
                "ORDER BY added_at, id",
                (entry_zone_id,),
            ).fetchall()
        ]

    @staticmethod
    def _to_ltf_scenario_entry(r: sqlite3.Row) -> LtfScenarioEntry:
        keys = r.keys()
        return LtfScenarioEntry(
            id=r["id"], scenario_id=r["scenario_id"], entry_zone_id=r["entry_zone_id"],
            range_version=r["range_version"], eligible=bool(r["eligible"]),
            overlap=r["overlap"], state=r["state"],
            reason=r["reason"] if "reason" in keys else "",
            added_at=r["added_at"], updated_at=r["updated_at"],
        )

    # ---------- LTF: liquidity tests (§10) ----------

    def insert_ltf_liquidity_test(self, t: LtfLiquidityTest) -> int:
        cur = self.conn.execute(
            """INSERT INTO ltf_liquidity_test
               (entry_zone_id, scenario_id, level, touch_at, candle_open_time,
                state, close_price, sweep_at, resolved_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (t.entry_zone_id, t.scenario_id, t.level, t.touch_at,
             t.candle_open_time, t.state, t.close_price, t.sweep_at, t.resolved_at),
        )
        self._commit()
        self._bump_ltf_cache()
        return int(cur.lastrowid)

    def update_ltf_liquidity_test(self, test_id: int, **fields: Any) -> None:
        self._update_ltf_row("ltf_liquidity_test", test_id, fields)

    def list_ltf_liquidity_tests(
        self,
        state: Optional[str] = None,
        scenario_id: Optional[int] = None,
    ) -> list[LtfLiquidityTest]:
        def load() -> list[LtfLiquidityTest]:
            q = "SELECT * FROM ltf_liquidity_test WHERE 1=1"
            args: list[Any] = []
            if state is not None:
                q += " AND state=?"
                args.append(state)
            if scenario_id is not None:
                q += " AND scenario_id=?"
                args.append(scenario_id)
            q += " ORDER BY touch_at, id"
            return [
                self._to_ltf_liquidity_test(r)
                for r in self.conn.execute(q, args).fetchall()
            ]
        return self._ltf_cached(("ltf_lt", state, scenario_id), load)

    @staticmethod
    def _to_ltf_liquidity_test(r: sqlite3.Row) -> LtfLiquidityTest:
        return LtfLiquidityTest(
            id=r["id"], entry_zone_id=r["entry_zone_id"], scenario_id=r["scenario_id"],
            level=r["level"], touch_at=r["touch_at"],
            candle_open_time=r["candle_open_time"], state=r["state"],
            close_price=r["close_price"], sweep_at=r["sweep_at"],
            resolved_at=r["resolved_at"],
        )

    # ---------- LTF: events (журнал, §11; дедупликация §11.5) ----------

    def insert_ltf_event(self, e: LtfEvent) -> tuple[LtfEvent, bool]:
        """Идемпотентно по UNIQUE(dedupe_key). Возвращает (event, created):
        created=False — событие с таким ключом уже было, повтор не создан."""
        existing = self._get_ltf_event_by_dedupe(e.dedupe_key)
        if existing is not None:
            return existing, False
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO ltf_event
               (observation_id, scenario_id, kind, payload, occurred_at,
                detected_at, dedupe_key, delivered, delayed,
                processing_mode, detection_lag_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (e.observation_id, e.scenario_id, e.kind,
             json.dumps(e.payload, ensure_ascii=False), e.occurred_at,
             e.detected_at, e.dedupe_key, int(e.delivered), int(e.delayed),
             e.processing_mode, e.detection_lag_ms),
        )
        self._commit()
        self._bump_ltf_cache()
        if cur.rowcount == 1:
            e.id = int(cur.lastrowid)
            return e, True
        found = self._get_ltf_event_by_dedupe(e.dedupe_key)
        if found is None:  # pragma: no cover
            raise RuntimeError("ltf_event: вставка проигнорирована, строка не найдена")
        return found, False

    def _get_ltf_event_by_dedupe(self, dedupe_key: str) -> Optional[LtfEvent]:
        r = self.conn.execute(
            "SELECT * FROM ltf_event WHERE dedupe_key=?", (dedupe_key,)
        ).fetchone()
        return self._to_ltf_event(r) if r else None

    def get_ltf_event(self, event_id: int) -> Optional[LtfEvent]:
        r = self.conn.execute(
            "SELECT * FROM ltf_event WHERE id=?", (event_id,)
        ).fetchone()
        return self._to_ltf_event(r) if r else None

    def list_ltf_events(
        self,
        observation_id: Optional[int] = None,
        scenario_id: Optional[int] = None,
        limit: int = 500,
    ) -> list[LtfEvent]:
        def load() -> list[LtfEvent]:
            q = "SELECT * FROM ltf_event WHERE 1=1"
            args: list[Any] = []
            if observation_id is not None:
                q += " AND observation_id=?"
                args.append(observation_id)
            if scenario_id is not None:
                q += " AND scenario_id=?"
                args.append(scenario_id)
            q += " ORDER BY occurred_at DESC, id DESC LIMIT ?"
            args.append(limit)
            return [self._to_ltf_event(r) for r in self.conn.execute(q, args).fetchall()]
        return self._ltf_cached(
            ("ltf_ev", observation_id, scenario_id, limit), load)

    def list_ltf_events_for_instrument(
        self, instrument_id: int, since_ms: Optional[int] = None, limit: int = 100
    ) -> list[LtfEvent]:
        """LTF-события инструмента (join через наблюдение), свежие первыми —
        история для бота (ТЗ п.11). Без кэша: выборка редкая (команда бота)."""
        q = """SELECT ltf_event.* FROM ltf_event
               JOIN ltf_observation o ON ltf_event.observation_id = o.id
               WHERE o.instrument_id=?"""
        args: list[Any] = [instrument_id]
        if since_ms is not None:
            q += " AND ltf_event.occurred_at>=?"
            args.append(since_ms)
        q += " ORDER BY ltf_event.occurred_at DESC, ltf_event.id DESC LIMIT ?"
        args.append(limit)
        return [self._to_ltf_event(r) for r in self.conn.execute(q, args).fetchall()]

    def mark_ltf_event_delivered(self, event_id: int) -> None:
        self.conn.execute(
            "UPDATE ltf_event SET delivered=1 WHERE id=?", (event_id,)
        )
        self._commit()
        self._bump_ltf_cache()

    def update_ltf_event_chart(self, event_id: int, state: str,
                               bump_attempts: bool = False) -> None:
        """Состояние графика события (ТЗ 07.10.2026 §7): отдельно от
        состояния самого события — ошибка картинки не меняет BOS/зону."""
        if bump_attempts:
            self.conn.execute(
                "UPDATE ltf_event SET chart_state=?, "
                "chart_attempts=chart_attempts+1 WHERE id=?",
                (state, event_id),
            )
        else:
            self.conn.execute(
                "UPDATE ltf_event SET chart_state=? WHERE id=?",
                (state, event_id),
            )
        self._commit()
        self._bump_ltf_cache()

    def pending_ltf_charts(self, limit: int = 50) -> list[LtfEvent]:
        """События с доставленным текстом, но недоставленным графиком —
        повторная генерация без повторного рыночного уведомления (§7)."""
        return [
            self._to_ltf_event(r)
            for r in self.conn.execute(
                "SELECT * FROM ltf_event WHERE delivered=1 "
                "AND chart_state='failed' AND chart_attempts<5 "
                "ORDER BY occurred_at, id LIMIT ?",
                (limit,),
            ).fetchall()
        ]

    def pending_ltf_events(self, limit: int = 200) -> list[LtfEvent]:
        """Недоставленные текущие (не delayed) события — ретрай доставки (§11).

        delayed (восстановленные replay) сюда не попадают: политика их
        Telegram-доставки отдельна и не согласована (§13) — не отправляем."""
        return [
            self._to_ltf_event(r)
            for r in self.conn.execute(
                "SELECT * FROM ltf_event WHERE delivered=0 AND delayed=0 "
                "ORDER BY occurred_at, id LIMIT ?",
                (limit,),
            ).fetchall()
        ]

    @staticmethod
    def _to_ltf_event(r: sqlite3.Row) -> LtfEvent:
        return LtfEvent(
            id=r["id"], observation_id=r["observation_id"],
            scenario_id=r["scenario_id"], kind=r["kind"],
            payload=json.loads(r["payload"] or "{}"),
            occurred_at=r["occurred_at"], detected_at=r["detected_at"],
            dedupe_key=r["dedupe_key"], delivered=bool(r["delivered"]),
            delayed=bool(r["delayed"]),
            processing_mode=r["processing_mode"],
            detection_lag_ms=r["detection_lag_ms"],
            chart_state=r["chart_state"] if "chart_state" in r.keys() else "none",
            chart_attempts=(
                r["chart_attempts"] if "chart_attempts" in r.keys() else 0
            ),
        )

    # ---------- LTF: reviews / assessments (разметка Entry Zones) ----------

    def add_ltf_review(self, rev: LtfReview) -> int:
        cur = self.conn.execute(
            """INSERT INTO ltf_review (entry_zone_id, scenario_id, decision,
                                      author, text, created_at)
               VALUES (?,?,?,?,?,?)""",
            (rev.entry_zone_id, rev.scenario_id, rev.decision, rev.author,
             rev.text, rev.created_at),
        )
        self._commit()
        return int(cur.lastrowid)

    def get_ltf_reviews(self, entry_zone_id: int) -> list[LtfReview]:
        return [
            LtfReview(
                id=r["id"], entry_zone_id=r["entry_zone_id"],
                scenario_id=r["scenario_id"], decision=r["decision"],
                author=r["author"], text=r["text"], created_at=r["created_at"],
            )
            for r in self.conn.execute(
                "SELECT * FROM ltf_review WHERE entry_zone_id=? ORDER BY created_at",
                (entry_zone_id,),
            ).fetchall()
        ]

    def add_ltf_assessment(self, a: LtfReviewAssessment) -> int:
        cur = self.conn.execute(
            """INSERT INTO ltf_review_assessment
               (entry_zone_id, review_id, review_decision, geometry_verdict,
                lifecycle_verdict, reason_code, evidence_source, assessed_as_of,
                reviewed_at, requires_clarification, corrected_lower,
                corrected_upper)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (a.entry_zone_id, a.review_id, a.review_decision, a.geometry_verdict,
             a.lifecycle_verdict, a.reason_code, a.evidence_source,
             a.assessed_as_of, a.reviewed_at, int(a.requires_clarification),
             a.corrected_lower, a.corrected_upper),
        )
        self._commit()
        return int(cur.lastrowid)

    def get_ltf_assessments(self, entry_zone_id: int) -> list[LtfReviewAssessment]:
        return [
            LtfReviewAssessment(
                id=r["id"], entry_zone_id=r["entry_zone_id"],
                review_id=r["review_id"], review_decision=r["review_decision"],
                geometry_verdict=r["geometry_verdict"],
                lifecycle_verdict=r["lifecycle_verdict"],
                reason_code=r["reason_code"], evidence_source=r["evidence_source"],
                assessed_as_of=r["assessed_as_of"], reviewed_at=r["reviewed_at"],
                requires_clarification=bool(r["requires_clarification"]),
                corrected_lower=r["corrected_lower"],
                corrected_upper=r["corrected_upper"],
            )
            for r in self.conn.execute(
                "SELECT * FROM ltf_review_assessment WHERE entry_zone_id=? "
                "ORDER BY reviewed_at",
                (entry_zone_id,),
            ).fetchall()
        ]

    # ---------- ALT: вселенная и источники («Altcoins D1 accumulation») ----------

    def upsert_alt_asset(self, a: AltAsset) -> AltAsset:
        """Идемпотентно по UNIQUE(cmc_id): повторная загрузка снапшота
        вселенной обновляет ранг/маппинг, не создавая вторую строку."""
        cur = self.conn.execute(
            """INSERT INTO alt_asset
               (cmc_id, canonical_asset_id, symbol, name, cmc_rank,
                exclusion_category, mapping_status, mapping_reason, enabled,
                created_ms, updated_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT (cmc_id) DO UPDATE SET
                 canonical_asset_id=excluded.canonical_asset_id,
                 symbol=excluded.symbol, name=excluded.name,
                 cmc_rank=excluded.cmc_rank,
                 exclusion_category=excluded.exclusion_category,
                 mapping_status=excluded.mapping_status,
                 mapping_reason=excluded.mapping_reason,
                 enabled=excluded.enabled, updated_ms=excluded.updated_ms""",
            (a.cmc_id, a.canonical_asset_id, a.symbol, a.name, a.cmc_rank,
             a.exclusion_category, a.mapping_status, a.mapping_reason,
             int(a.enabled), a.created_ms, a.updated_ms),
        )
        self._commit()
        if a.id is None:
            r = self.conn.execute(
                "SELECT id FROM alt_asset WHERE cmc_id=?", (a.cmc_id,)
            ).fetchone()
            a.id = int(r["id"])
        return a

    def get_alt_asset(self, asset_id: int) -> Optional[AltAsset]:
        r = self.conn.execute(
            "SELECT * FROM alt_asset WHERE id=?", (asset_id,)
        ).fetchone()
        return self._to_alt_asset(r) if r else None

    def get_alt_asset_by_cmc_id(self, cmc_id: int) -> Optional[AltAsset]:
        r = self.conn.execute(
            "SELECT * FROM alt_asset WHERE cmc_id=?", (cmc_id,)
        ).fetchone()
        return self._to_alt_asset(r) if r else None

    def list_alt_assets(self, enabled_only: bool = False) -> list[AltAsset]:
        """Все известные активы модуля (в т.ч. выпавшие из текущей выборки —
        политика непрерывности наблюдения разруливается вызывающим, §3 ТЗ)."""
        q = "SELECT * FROM alt_asset"
        if enabled_only:
            q += " WHERE enabled=1"
        q += " ORDER BY cmc_rank, id"
        return [self._to_alt_asset(r) for r in self.conn.execute(q).fetchall()]

    @staticmethod
    def _to_alt_asset(r: sqlite3.Row) -> AltAsset:
        return AltAsset(
            id=r["id"], cmc_id=r["cmc_id"],
            canonical_asset_id=r["canonical_asset_id"], symbol=r["symbol"],
            name=r["name"], cmc_rank=r["cmc_rank"],
            exclusion_category=r["exclusion_category"],
            mapping_status=r["mapping_status"], mapping_reason=r["mapping_reason"],
            enabled=bool(r["enabled"]),
            created_ms=r["created_ms"], updated_ms=r["updated_ms"],
        )

    def insert_alt_universe_snapshot(
        self, taken_ms: int, payload_json: str, source: str = "cmc", stale: bool = False
    ) -> int:
        cur = self.conn.execute(
            """INSERT INTO alt_universe_snapshot (taken_ms, payload_json, source, stale)
               VALUES (?,?,?,?)""",
            (taken_ms, payload_json, source, int(stale)),
        )
        self._commit()
        return int(cur.lastrowid)

    def get_latest_alt_universe_snapshot(self) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM alt_universe_snapshot ORDER BY taken_ms DESC, id DESC LIMIT 1"
        ).fetchone()

    def upsert_alt_instrument_source(self, s: AltInstrumentSource) -> AltInstrumentSource:
        """Идемпотентно по UNIQUE(asset_id, venue, symbol, quote, source_version)."""
        self.conn.execute(
            """INSERT INTO alt_instrument_source
               (asset_id, venue, symbol, quote, earliest_available_ms,
                last_closed_ms, history_scope, source_version)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT (asset_id, venue, symbol, quote, source_version)
               DO UPDATE SET
                 earliest_available_ms=excluded.earliest_available_ms,
                 last_closed_ms=excluded.last_closed_ms,
                 history_scope=excluded.history_scope""",
            (s.asset_id, s.venue, s.symbol, s.quote, s.earliest_available_ms,
             s.last_closed_ms, s.history_scope, s.source_version),
        )
        self._commit()
        if s.id is None:
            r = self.conn.execute(
                """SELECT id FROM alt_instrument_source
                   WHERE asset_id=? AND venue=? AND symbol=? AND quote=?
                     AND source_version=?""",
                (s.asset_id, s.venue, s.symbol, s.quote, s.source_version),
            ).fetchone()
            s.id = int(r["id"])
        return s

    def get_alt_instrument_source(self, asset_id: int) -> Optional[AltInstrumentSource]:
        r = self.conn.execute(
            "SELECT * FROM alt_instrument_source WHERE asset_id=? "
            "ORDER BY source_version DESC, id DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
        if not r:
            return None
        return AltInstrumentSource(
            id=r["id"], asset_id=r["asset_id"], venue=r["venue"],
            symbol=r["symbol"], quote=r["quote"],
            earliest_available_ms=r["earliest_available_ms"],
            last_closed_ms=r["last_closed_ms"],
            history_scope=r["history_scope"], source_version=r["source_version"],
        )

    # ---------- ALT: свечи D1 ----------

    def insert_alt_candles(self, candles: Iterable[AltCandle]) -> int:
        """INSERT OR IGNORE: повторная загрузка истории не переписывает свечи."""
        rows = [
            (c.source_id, c.open_time, c.open, c.high, c.low, c.close, c.volume)
            for c in candles
        ]
        cur = self.conn.executemany(
            """INSERT OR IGNORE INTO alt_candle
               (source_id, open_time, open, high, low, close, volume)
               VALUES (?,?,?,?,?,?,?)""",
            rows,
        )
        self._commit()
        return cur.rowcount

    def get_alt_candles(
        self,
        source_id: int,
        start_ms: Optional[int] = None,
        end_ms: Optional[int] = None,
    ) -> list[AltCandle]:
        q = "SELECT * FROM alt_candle WHERE source_id=?"
        args: list[Any] = [source_id]
        if start_ms is not None:
            q += " AND open_time>=?"
            args.append(start_ms)
        if end_ms is not None:
            q += " AND open_time<=?"
            args.append(end_ms)
        q += " ORDER BY open_time"
        return [
            AltCandle(
                source_id=r["source_id"], open_time=r["open_time"], open=r["open"],
                high=r["high"], low=r["low"], close=r["close"], volume=r["volume"],
            )
            for r in self.conn.execute(q, args).fetchall()
        ]

    # ---------- ALT: диапазоны ----------

    def insert_alt_range_candidate(self, c: AltRangeCandidate) -> AltRangeCandidate:
        cur = self.conn.execute(
            """INSERT INTO alt_range_candidate
               (asset_id, origin_key, start_anchor_open_time,
                rebound_anchor_open_time, lower, upper, width, mid, n_days,
                version, state, metrics_json, first_seen_ms, updated_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c.asset_id, c.origin_key, c.start_anchor_open_time,
             c.rebound_anchor_open_time, c.lower, c.upper, c.width, c.mid,
             c.n_days, c.version, c.state, c.metrics_json,
             c.first_seen_ms, c.updated_ms),
        )
        self._commit()
        c.id = int(cur.lastrowid)
        return c

    def get_alt_range_candidate(self, candidate_id: int) -> Optional[AltRangeCandidate]:
        r = self.conn.execute(
            "SELECT * FROM alt_range_candidate WHERE id=?", (candidate_id,)
        ).fetchone()
        return self._to_alt_range_candidate(r) if r else None

    def find_alt_range_candidate_by_origin(
        self, asset_id: int, origin_key: str
    ) -> Optional[AltRangeCandidate]:
        """Идемпотентность replay: одна пара якорей = один диапазон (§15 ТЗ)."""
        r = self.conn.execute(
            "SELECT * FROM alt_range_candidate WHERE asset_id=? AND origin_key=? "
            "ORDER BY id LIMIT 1",
            (asset_id, origin_key),
        ).fetchone()
        return self._to_alt_range_candidate(r) if r else None

    def update_alt_range_candidate(self, candidate_id: int, **fields: Any) -> None:
        """Обновление живого кандидата (версии/границы/возраст до freeze)."""
        manual = self.active_alt_range_revision("candidate", candidate_id)
        if manual is not None and manual.source_kind == "manual":
            # Ежедневный auto-replay вправе обновить состояние/метрики, но
            # закреплённую пользователем геометрию не перезаписывает.
            for key in ("lower", "upper", "mid", "width",
                        "start_anchor_open_time", "version"):
                fields.pop(key, None)
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE alt_range_candidate SET {cols} WHERE id=?",
            (*fields.values(), candidate_id),
        )
        self._commit()

    def list_alt_range_candidates(self, asset_id: int) -> list[AltRangeCandidate]:
        """Все кандидаты диапазонов актива (для read model окна, §16 ТЗ)."""
        return [
            self._to_alt_range_candidate(r)
            for r in self.conn.execute(
                "SELECT * FROM alt_range_candidate WHERE asset_id=? ORDER BY id",
                (asset_id,),
            ).fetchall()
        ]

    @staticmethod
    def _to_alt_range_candidate(r: sqlite3.Row) -> AltRangeCandidate:
        return AltRangeCandidate(
            id=r["id"], asset_id=r["asset_id"], origin_key=r["origin_key"],
            start_anchor_open_time=r["start_anchor_open_time"],
            rebound_anchor_open_time=r["rebound_anchor_open_time"],
            lower=r["lower"], upper=r["upper"], width=r["width"], mid=r["mid"],
            n_days=r["n_days"], version=r["version"], state=r["state"],
            metrics_json=r["metrics_json"], first_seen_ms=r["first_seen_ms"],
            updated_ms=r["updated_ms"],
        )

    def insert_alt_frozen_range(self, f: AltFrozenRange) -> AltFrozenRange:
        cur = self.conn.execute(
            """INSERT INTO alt_frozen_range
               (range_id, lower, upper, width, mid, start_anchor_open_time,
                rebound_anchor_open_time, included_candles, mature_at_ms,
                classifier_version, range_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (f.range_id, f.lower, f.upper, f.width, f.mid,
             f.start_anchor_open_time, f.rebound_anchor_open_time,
             f.included_candles, f.mature_at_ms, f.classifier_version,
             f.range_version),
        )
        self._commit()
        f.id = int(cur.lastrowid)
        return f

    def get_alt_frozen_range(self, frozen_id: int) -> Optional[AltFrozenRange]:
        r = self.conn.execute(
            "SELECT * FROM alt_frozen_range WHERE id=?", (frozen_id,)
        ).fetchone()
        return self._to_alt_frozen_range(r) if r else None

    def get_alt_frozen_range_by_range(
        self, range_id: int
    ) -> Optional[AltFrozenRange]:
        """Заморозка конкретного кандидата — идемпотентность freeze при replay."""
        r = self.conn.execute(
            "SELECT * FROM alt_frozen_range WHERE range_id=? ORDER BY id LIMIT 1",
            (range_id,),
        ).fetchone()
        return self._to_alt_frozen_range(r) if r else None

    @staticmethod
    def _to_alt_frozen_range(r: sqlite3.Row) -> AltFrozenRange:
        return AltFrozenRange(
            id=r["id"], range_id=r["range_id"], lower=r["lower"],
            upper=r["upper"], width=r["width"], mid=r["mid"],
            start_anchor_open_time=r["start_anchor_open_time"],
            rebound_anchor_open_time=r["rebound_anchor_open_time"],
            included_candles=r["included_candles"], mature_at_ms=r["mature_at_ms"],
            classifier_version=r["classifier_version"],
            range_version=r["range_version"],
        )

    # ---------- ALT: сетапы ----------

    def insert_alt_setup(self, s: AltSetup) -> tuple[AltSetup, bool]:
        """Идемпотентно по UNIQUE(asset_id, range_id): повторная заморозка
        диапазона не создаёт второй сетап. Возвращает (setup, created)."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO alt_setup
               (asset_id, source_id, range_id, state, flags_json,
                confirmation_event_id, targets_json, cancel_price, cancel_mode,
                cancel_reachable, breakout_close, breakout_closed_at,
                retest_deadline_ms, entry_a_id, entry_b_id, universe_eligible,
                created_ms, updated_ms, terminated_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (s.asset_id, s.source_id, s.range_id, s.state, s.flags_json,
             s.confirmation_event_id, s.targets_json, s.cancel_price,
             s.cancel_mode, int(s.cancel_reachable), s.breakout_close,
             s.breakout_closed_at, s.retest_deadline_ms, s.entry_a_id,
             s.entry_b_id, int(s.universe_eligible), s.created_ms,
             s.updated_ms, s.terminated_ms),
        )
        self._commit()
        if cur.rowcount == 1:
            s.id = int(cur.lastrowid)
            return s, True
        r = self.conn.execute(
            "SELECT * FROM alt_setup WHERE asset_id=? AND range_id=?",
            (s.asset_id, s.range_id),
        ).fetchone()
        if r is None:  # pragma: no cover — защита от несогласованности схемы
            raise RuntimeError("alt_setup: вставка проигнорирована, строка не найдена")
        return self._to_alt_setup(r), False

    def get_alt_setup(self, setup_id: int) -> Optional[AltSetup]:
        r = self.conn.execute(
            "SELECT * FROM alt_setup WHERE id=?", (setup_id,)
        ).fetchone()
        return self._to_alt_setup(r) if r else None

    def list_alt_setups(self, asset_id: int) -> list[AltSetup]:
        """Все сетапы актива (история + активный) — для проверки terminal-
        состояний при replay (завершённый сетап не воскресает, §15 ТЗ)."""
        return [
            self._to_alt_setup(r)
            for r in self.conn.execute(
                "SELECT * FROM alt_setup WHERE asset_id=? ORDER BY id",
                (asset_id,),
            ).fetchall()
        ]

    def update_alt_setup(self, setup_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        args = [
            int(v) if isinstance(v, bool) else v for v in fields.values()
        ]
        self.conn.execute(
            f"UPDATE alt_setup SET {cols} WHERE id=?", (*args, setup_id)
        )
        self._commit()

    @staticmethod
    def _to_alt_setup(r: sqlite3.Row) -> AltSetup:
        return AltSetup(
            id=r["id"], asset_id=r["asset_id"], source_id=r["source_id"],
            range_id=r["range_id"], state=r["state"], flags_json=r["flags_json"],
            confirmation_event_id=r["confirmation_event_id"],
            targets_json=r["targets_json"], cancel_price=r["cancel_price"],
            cancel_mode=r["cancel_mode"],
            cancel_reachable=bool(r["cancel_reachable"]),
            breakout_close=r["breakout_close"],
            breakout_closed_at=r["breakout_closed_at"],
            retest_deadline_ms=r["retest_deadline_ms"],
            entry_a_id=r["entry_a_id"], entry_b_id=r["entry_b_id"],
            universe_eligible=bool(r["universe_eligible"]),
            created_ms=r["created_ms"], updated_ms=r["updated_ms"],
            terminated_ms=r["terminated_ms"],
        )

    # ---------- ALT: структура, манипуляции, входы ----------

    def insert_alt_structure_event(self, e: AltStructureEvent) -> int:
        cur = self.conn.execute(
            """INSERT INTO alt_structure_event
               (setup_id, kind, level_price, close_price, candle_open_time,
                anchors_json)
               VALUES (?,?,?,?,?,?)""",
            (e.setup_id, e.kind, e.level_price, e.close_price,
             e.candle_open_time, e.anchors_json),
        )
        self._commit()
        return int(cur.lastrowid)

    def find_alt_structure_event(
        self, setup_id: int, kind: str, candle_open_time: int
    ) -> Optional[int]:
        """Дедупликация при replay: та же свеча + тип = то же событие (§15)."""
        r = self.conn.execute(
            "SELECT id FROM alt_structure_event "
            "WHERE setup_id=? AND kind=? AND candle_open_time=?",
            (setup_id, kind, candle_open_time),
        ).fetchone()
        return int(r["id"]) if r else None

    def list_alt_structure_events(self, setup_id: int) -> list[AltStructureEvent]:
        return [
            AltStructureEvent(
                id=r["id"], setup_id=r["setup_id"], kind=r["kind"],
                level_price=r["level_price"], close_price=r["close_price"],
                candle_open_time=r["candle_open_time"],
                anchors_json=r["anchors_json"],
            )
            for r in self.conn.execute(
                "SELECT * FROM alt_structure_event WHERE setup_id=? "
                "ORDER BY candle_open_time, id",
                (setup_id,),
            ).fetchall()
        ]

    def insert_alt_manipulation_episode(self, m: AltManipulationEpisode) -> int:
        cur = self.conn.execute(
            """INSERT INTO alt_manipulation_episode
               (setup_id, started_candle_open_time, min_price,
                ended_candle_open_time, days_below)
               VALUES (?,?,?,?,?)""",
            (m.setup_id, m.started_candle_open_time, m.min_price,
             m.ended_candle_open_time, m.days_below),
        )
        self._commit()
        return int(cur.lastrowid)

    def find_alt_manipulation_episode(
        self, setup_id: int, started_candle_open_time: int
    ) -> Optional[AltManipulationEpisode]:
        """Дедупликация при replay: эпизод определяется свечой начала (§9)."""
        r = self.conn.execute(
            "SELECT * FROM alt_manipulation_episode "
            "WHERE setup_id=? AND started_candle_open_time=?",
            (setup_id, started_candle_open_time),
        ).fetchone()
        if not r:
            return None
        return AltManipulationEpisode(
            id=r["id"], setup_id=r["setup_id"],
            started_candle_open_time=r["started_candle_open_time"],
            min_price=r["min_price"],
            ended_candle_open_time=r["ended_candle_open_time"],
            days_below=r["days_below"],
        )

    def update_alt_manipulation_episode(self, episode_id: int, **fields: Any) -> None:
        """Ход эпизода: min_price/days_below/ended_candle_open_time (§9)."""
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE alt_manipulation_episode SET {cols} WHERE id=?",
            (*fields.values(), episode_id),
        )
        self._commit()

    def list_alt_manipulation_episodes(self, setup_id: int) -> list[AltManipulationEpisode]:
        return [
            AltManipulationEpisode(
                id=r["id"], setup_id=r["setup_id"],
                started_candle_open_time=r["started_candle_open_time"],
                min_price=r["min_price"],
                ended_candle_open_time=r["ended_candle_open_time"],
                days_below=r["days_below"],
            )
            for r in self.conn.execute(
                "SELECT * FROM alt_manipulation_episode WHERE setup_id=? "
                "ORDER BY started_candle_open_time, id",
                (setup_id,),
            ).fetchall()
        ]

    def insert_alt_entry_opportunity(self, o: AltEntryOpportunity) -> AltEntryOpportunity:
        cur = self.conn.execute(
            """INSERT INTO alt_entry_opportunity
               (setup_id, kind, event_time_ms, price, zone_json, bases_json)
               VALUES (?,?,?,?,?,?)""",
            (o.setup_id, o.kind, o.event_time_ms, o.price, o.zone_json,
             o.bases_json),
        )
        self._commit()
        o.id = int(cur.lastrowid)
        return o

    def find_alt_entry_opportunity(
        self, setup_id: int, kind: str
    ) -> Optional[AltEntryOpportunity]:
        """Антиспам v1 (§11): один первый вход A и один первый B на сетап."""
        r = self.conn.execute(
            "SELECT * FROM alt_entry_opportunity WHERE setup_id=? AND kind=? "
            "ORDER BY id LIMIT 1",
            (setup_id, kind),
        ).fetchone()
        if not r:
            return None
        return AltEntryOpportunity(
            id=r["id"], setup_id=r["setup_id"], kind=r["kind"],
            event_time_ms=r["event_time_ms"], price=r["price"],
            zone_json=r["zone_json"], bases_json=r["bases_json"],
        )

    def list_alt_entry_opportunities(self, setup_id: int) -> list[AltEntryOpportunity]:
        return [
            AltEntryOpportunity(
                id=r["id"], setup_id=r["setup_id"], kind=r["kind"],
                event_time_ms=r["event_time_ms"], price=r["price"],
                zone_json=r["zone_json"], bases_json=r["bases_json"],
            )
            for r in self.conn.execute(
                "SELECT * FROM alt_entry_opportunity WHERE setup_id=? "
                "ORDER BY event_time_ms, id",
                (setup_id,),
            ).fetchall()
        ]

    # ---------- ALT: прогоны джобы ----------

    def insert_alt_run(self, run: AltRun) -> AltRun:
        cur = self.conn.execute(
            """INSERT INTO alt_run
               (started_ms, finished_ms, as_of_ms, status, processed, errors,
                universe_snapshot_id, summary_json)
               VALUES (?,?,?,?,?,?,?,?)""",
            (run.started_ms, run.finished_ms, run.as_of_ms, run.status,
             run.processed, run.errors, run.universe_snapshot_id,
             run.summary_json),
        )
        self._commit()
        run.id = int(cur.lastrowid)
        return run

    def update_alt_run(self, run_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE alt_run SET {cols} WHERE id=?", (*fields.values(), run_id)
        )
        self._commit()

    def get_alt_run(self, run_id: int) -> Optional[AltRun]:
        r = self.conn.execute(
            "SELECT * FROM alt_run WHERE id=?", (run_id,)
        ).fetchone()
        return self._to_alt_run(r) if r else None

    def get_running_alt_run(self) -> Optional[AltRun]:
        """Активный прогон джобы — блокировка повторного запуска (§4 ТЗ)."""
        r = self.conn.execute(
            "SELECT * FROM alt_run WHERE status='running' "
            "ORDER BY started_ms DESC, id DESC LIMIT 1"
        ).fetchone()
        return self._to_alt_run(r) if r else None

    def get_latest_alt_run(
        self, statuses: Optional[Sequence[str]] = None
    ) -> Optional[AltRun]:
        """Последний прогон (опционально — фильтр статусов) для расписания
        и отображения «последний run» на экране (§16 ТЗ)."""
        q = "SELECT * FROM alt_run"
        args: list[Any] = []
        if statuses:
            q += f" WHERE status IN ({','.join('?' for _ in statuses)})"
            args.extend(statuses)
        q += " ORDER BY started_ms DESC, id DESC LIMIT 1"
        r = self.conn.execute(q, args).fetchone()
        return self._to_alt_run(r) if r else None

    @staticmethod
    def _to_alt_run(r: sqlite3.Row) -> AltRun:
        return AltRun(
            id=r["id"], started_ms=r["started_ms"], finished_ms=r["finished_ms"],
            as_of_ms=r["as_of_ms"], status=r["status"], processed=r["processed"],
            errors=r["errors"], universe_snapshot_id=r["universe_snapshot_id"],
            summary_json=r["summary_json"],
        )

    # ---------- ALT: outbox событий ----------

    def insert_alt_event(self, e: AltEvent) -> tuple[AltEvent, bool]:
        """Идемпотентно по UNIQUE(setup_id, event_type, source_event_id).
        Возвращает (event, created): created=False — дубликат, повтор не создан."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO alt_event
               (setup_id, event_type, source_event_id, payload_json,
                event_time_ms, detected_at_ms, run_id, delivered, created_ms)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (e.setup_id, e.event_type, e.source_event_id, e.payload_json,
             e.event_time_ms, e.detected_at_ms, e.run_id, int(e.delivered),
             e.created_ms),
        )
        self._commit()
        if cur.rowcount == 1:
            e.id = int(cur.lastrowid)
            return e, True
        r = self.conn.execute(
            """SELECT * FROM alt_event
               WHERE setup_id=? AND event_type=? AND source_event_id=?""",
            (e.setup_id, e.event_type, e.source_event_id),
        ).fetchone()
        if r is None:  # pragma: no cover
            raise RuntimeError("alt_event: вставка проигнорирована, строка не найдена")
        return self._to_alt_event(r), False

    def pending_alt_events(self, limit: int = 200) -> list[AltEvent]:
        """Недоставленные события — ретрай доставки (аналог pending_ltf_events)."""
        return [
            self._to_alt_event(r)
            for r in self.conn.execute(
                "SELECT * FROM alt_event WHERE delivered=0 "
                "ORDER BY event_time_ms, id LIMIT ?",
                (limit,),
            ).fetchall()
        ]

    def mark_alt_event_delivered(self, event_id: int) -> None:
        self.conn.execute(
            "UPDATE alt_event SET delivered=1 WHERE id=?", (event_id,)
        )
        self._commit()

    def get_alt_event(self, event_id: int) -> Optional[AltEvent]:
        r = self.conn.execute(
            "SELECT * FROM alt_event WHERE id=?", (event_id,)
        ).fetchone()
        return self._to_alt_event(r) if r else None

    def list_alt_events(self, setup_id: int) -> list[AltEvent]:
        """Все события сетапа (read model «Почему найдено», §16 ТЗ)."""
        return [
            self._to_alt_event(r)
            for r in self.conn.execute(
                "SELECT * FROM alt_event WHERE setup_id=? "
                "ORDER BY event_time_ms, id",
                (setup_id,),
            ).fetchall()
        ]

    @staticmethod
    def _to_alt_event(r: sqlite3.Row) -> AltEvent:
        return AltEvent(
            id=r["id"], setup_id=r["setup_id"], event_type=r["event_type"],
            source_event_id=r["source_event_id"], payload_json=r["payload_json"],
            event_time_ms=r["event_time_ms"], detected_at_ms=r["detected_at_ms"],
            run_id=r["run_id"], delivered=bool(r["delivered"]),
            created_ms=r["created_ms"],
        )

    # ---------- ALT: эпизоды диапазонов v2 (R-01/R-07/R-09) ----------

    def insert_alt_range_episode(self, e: AltRangeEpisode) -> tuple[AltRangeEpisode, bool]:
        """Идемпотентно по UNIQUE(origin_key): повторный прогон не дублирует
        эпизод (R-10). Возвращает (episode, created)."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO alt_range_episode
               (asset_id, source_id, origin_key, rules_version, state,
                anchor_start_open_time, base_start_open_time,
                base_end_open_time, base_end_reason, base_end_confirmed_at_ms,
                accompaniment_end_open_time, lower, upper, mid, width,
                wick_low, wick_high, quality_json, selection_rank_reason,
                detected_at_ms, created_ms, updated_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (e.asset_id, e.source_id, e.origin_key, e.rules_version, e.state,
             e.anchor_start_open_time, e.base_start_open_time,
             e.base_end_open_time, e.base_end_reason, e.base_end_confirmed_at_ms,
             e.accompaniment_end_open_time, e.lower, e.upper, e.mid, e.width,
             e.wick_low, e.wick_high, e.quality_json, e.selection_rank_reason,
             e.detected_at_ms, e.created_ms, e.updated_ms),
        )
        self._commit()
        if cur.rowcount == 1:
            e.id = int(cur.lastrowid)
            return e, True
        r = self.conn.execute(
            "SELECT * FROM alt_range_episode WHERE origin_key=?",
            (e.origin_key,),
        ).fetchone()
        if r is None:  # pragma: no cover — защита от несогласованности схемы
            raise RuntimeError("alt_range_episode: вставка проигнорирована, строка не найдена")
        return self._to_alt_range_episode(r), False

    def get_alt_range_episode(self, episode_id: int) -> Optional[AltRangeEpisode]:
        r = self.conn.execute(
            "SELECT * FROM alt_range_episode WHERE id=?", (episode_id,)
        ).fetchone()
        return self._to_alt_range_episode(r) if r else None

    def get_alt_range_episode_by_origin(self, origin_key: str) -> Optional[AltRangeEpisode]:
        r = self.conn.execute(
            "SELECT * FROM alt_range_episode WHERE origin_key=?", (origin_key,)
        ).fetchone()
        return self._to_alt_range_episode(r) if r else None

    def list_alt_range_episodes(self, asset_id: int) -> list[AltRangeEpisode]:
        """Все эпизоды v2 актива от старых к новым — «История диапазонов» (R-08)."""
        return [
            self._to_alt_range_episode(r)
            for r in self.conn.execute(
                "SELECT * FROM alt_range_episode WHERE asset_id=? "
                "ORDER BY anchor_start_open_time, id",
                (asset_id,),
            ).fetchall()
        ]

    def update_alt_range_episode(self, episode_id: int, **fields: Any) -> None:
        """Ход эпизода: state, геометрия до mature, поля конца заливки
        (base_end_*) и сопровождения (R-07), quality/selection (R-04/R-08)."""
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE alt_range_episode SET {cols} WHERE id=?",
            (*fields.values(), episode_id),
        )
        self._commit()

    # ---------- ALT: ручные ревизии effective-range ----------

    def active_alt_range_revision(self, subject_kind: str, subject_id: int):
        from .models_alt import AltRangeRevision
        r = self.conn.execute(
            "SELECT * FROM alt_range_revision WHERE subject_kind=? AND subject_id=? "
            "AND active=1 ORDER BY revision DESC LIMIT 1",
            (subject_kind, subject_id),
        ).fetchone()
        return self._to_alt_range_revision(r) if r else None

    def list_alt_range_revisions(self, subject_kind: str, subject_id: int):
        return [self._to_alt_range_revision(r) for r in self.conn.execute(
            "SELECT * FROM alt_range_revision WHERE subject_kind=? AND subject_id=? "
            "ORDER BY revision DESC", (subject_kind, subject_id),
        ).fetchall()]

    def insert_alt_range_revision(self, revision):
        """CAS + idempotency. Caller validates geometry and builds derived_json."""
        existing = self.conn.execute(
            "SELECT * FROM alt_range_revision WHERE subject_kind=? AND subject_id=? "
            "AND idempotency_key=?",
            (revision.subject_kind, revision.subject_id, revision.idempotency_key),
        ).fetchone()
        if existing:
            return self._to_alt_range_revision(existing), False
        current = self.active_alt_range_revision(revision.subject_kind, revision.subject_id)
        current_no = current.revision if current else 0
        if current_no != revision.expected_previous_revision:
            raise ValueError(f"revision_conflict:{current_no}")
        revision.revision = current_no + 1
        self.conn.execute(
            "UPDATE alt_range_revision SET active=0 WHERE subject_kind=? AND subject_id=?",
            (revision.subject_kind, revision.subject_id),
        )
        cur = self.conn.execute(
            """INSERT INTO alt_range_revision
               (subject_kind,subject_id,revision,source_kind,lower,upper,mid,width,
                base_start_open_time,base_end_open_time,derived_json,reason,
                expected_previous_revision,idempotency_key,active,created_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
            (revision.subject_kind, revision.subject_id, revision.revision,
             revision.source_kind, revision.lower, revision.upper, revision.mid,
             revision.width, revision.base_start_open_time,
             revision.base_end_open_time, revision.derived_json, revision.reason,
             revision.expected_previous_revision, revision.idempotency_key,
             revision.created_ms),
        )
        revision.id = int(cur.lastrowid)
        revision.active = True
        # События старой геометрии, которые ещё не доставлялись, не должны уйти.
        if revision.subject_kind == "setup":
            rows = self.conn.execute(
                "SELECT id,payload_json FROM alt_event WHERE setup_id=? AND delivered=0",
                (revision.subject_id,),
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, ValueError):
                    payload = {}
                payload["superseded_by_range_revision"] = revision.revision
                self.conn.execute(
                    "UPDATE alt_event SET delivered=1,payload_json=? WHERE id=?",
                    (json.dumps(payload, ensure_ascii=False), row["id"]),
                )
        self._commit()
        self.bump_state_seq()
        return revision, True

    @staticmethod
    def _to_alt_range_revision(r):
        from .models_alt import AltRangeRevision
        return AltRangeRevision(
            id=r["id"], subject_kind=r["subject_kind"], subject_id=r["subject_id"],
            revision=r["revision"], source_kind=r["source_kind"], lower=r["lower"],
            upper=r["upper"], mid=r["mid"], width=r["width"],
            base_start_open_time=r["base_start_open_time"],
            base_end_open_time=r["base_end_open_time"], derived_json=r["derived_json"],
            reason=r["reason"], expected_previous_revision=r["expected_previous_revision"],
            idempotency_key=r["idempotency_key"], active=bool(r["active"]),
            created_ms=r["created_ms"],
        )

    @staticmethod
    def _to_alt_range_episode(r: sqlite3.Row) -> AltRangeEpisode:
        return AltRangeEpisode(
            id=r["id"], asset_id=r["asset_id"], source_id=r["source_id"],
            origin_key=r["origin_key"], rules_version=r["rules_version"],
            state=r["state"],
            anchor_start_open_time=r["anchor_start_open_time"],
            base_start_open_time=r["base_start_open_time"],
            base_end_open_time=r["base_end_open_time"],
            base_end_reason=r["base_end_reason"],
            base_end_confirmed_at_ms=r["base_end_confirmed_at_ms"],
            accompaniment_end_open_time=r["accompaniment_end_open_time"],
            lower=r["lower"], upper=r["upper"], mid=r["mid"], width=r["width"],
            wick_low=r["wick_low"], wick_high=r["wick_high"],
            quality_json=r["quality_json"],
            selection_rank_reason=r["selection_rank_reason"],
            detected_at_ms=r["detected_at_ms"],
            created_ms=r["created_ms"], updated_ms=r["updated_ms"],
        )

    # ---------- ALT: выносы вниз v2 (R-05) ----------

    def insert_alt_sweep_episode(self, s: AltSweepEpisode) -> AltSweepEpisode:
        cur = self.conn.execute(
            """INSERT INTO alt_sweep_episode
               (episode_id, start_open_time, min_price, min_open_time,
                end_open_time, return_confirmed, return_confirmed_at_ms,
                state, created_ms, updated_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (s.episode_id, s.start_open_time, s.min_price, s.min_open_time,
             s.end_open_time, int(s.return_confirmed), s.return_confirmed_at_ms,
             s.state, s.created_ms, s.updated_ms),
        )
        self._commit()
        s.id = int(cur.lastrowid)
        return s

    def update_alt_sweep_episode(self, sweep_id: int, **fields: Any) -> None:
        """Ход выноса: min_price/min_open_time, конец и подтверждение возврата."""
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        args = [
            int(v) if isinstance(v, bool) else v for v in fields.values()
        ]
        self.conn.execute(
            f"UPDATE alt_sweep_episode SET {cols} WHERE id=?", (*args, sweep_id)
        )
        self._commit()

    def get_alt_sweep_episode(self, sweep_id: int) -> Optional[AltSweepEpisode]:
        r = self.conn.execute(
            "SELECT * FROM alt_sweep_episode WHERE id=?", (sweep_id,)
        ).fetchone()
        return self._to_alt_sweep_episode(r) if r else None

    def list_alt_sweep_episodes(self, episode_id: int) -> list[AltSweepEpisode]:
        return [
            self._to_alt_sweep_episode(r)
            for r in self.conn.execute(
                "SELECT * FROM alt_sweep_episode WHERE episode_id=? "
                "ORDER BY start_open_time, id",
                (episode_id,),
            ).fetchall()
        ]

    @staticmethod
    def _to_alt_sweep_episode(r: sqlite3.Row) -> AltSweepEpisode:
        return AltSweepEpisode(
            id=r["id"], episode_id=r["episode_id"],
            start_open_time=r["start_open_time"], min_price=r["min_price"],
            min_open_time=r["min_open_time"], end_open_time=r["end_open_time"],
            return_confirmed=bool(r["return_confirmed"]),
            return_confirmed_at_ms=r["return_confirmed_at_ms"],
            state=r["state"], created_ms=r["created_ms"],
            updated_ms=r["updated_ms"],
        )
