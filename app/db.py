"""Тонкий слой SQLite: миграции + репозитории. Источник истины для подавления,
визитов и состояний — перезапуск процесса ничего не сбрасывает (§8, §13.12).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Optional

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
        self.migrate()

    def migrate(self) -> None:
        cur = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if not cur.fetchone():
            self.conn.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
            self.conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
            )
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
                    "ALTER TABLE instrument ADD COLUMN ltf_analyze INTEGER NOT NULL DEFAULT 0"
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
            self.conn.commit()

    def get_meta(self, key: str) -> Optional[str]:
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?,?) "
            "ON CONFLICT (key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

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
            """INSERT INTO instrument (asset, venue, market_type, symbol, quote_asset, precision, enabled)
               VALUES (?,?,?,?,?,?,?)
               ON CONFLICT (venue, market_type, symbol)
               DO UPDATE SET asset=excluded.asset, quote_asset=excluded.quote_asset,
                             precision=excluded.precision, enabled=excluded.enabled""",
            (ins.asset, ins.venue, ins.market_type, ins.symbol, ins.quote_asset,
             ins.precision, int(ins.enabled)),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT id FROM instrument WHERE venue=? AND market_type=? AND symbol=?",
            (ins.venue, ins.market_type, ins.symbol),
        ).fetchone()
        return int(row["id"])

    def get_instruments(self, enabled_only: bool = False) -> list[Instrument]:
        q = "SELECT * FROM instrument"
        if enabled_only:
            q += " WHERE enabled=1"
        return [self._to_instrument(r) for r in self.conn.execute(q).fetchall()]

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
        self.conn.commit()

    def set_instrument_ltf_analyze(self, instrument_id: int, analyze: bool) -> None:
        self.conn.execute(
            "UPDATE instrument SET ltf_analyze=? WHERE id=?",
            (int(analyze), instrument_id),
        )
        self.conn.commit()

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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
        self.invalidate_zone_cache()

    def invalidate_zone_cache(self) -> None:
        """Сброс кэша get_zones — вызывать после любой записи в zone,
        идущей мимо insert_zone/update_zone (прямые SQL-апдейты)."""
        with self._zone_cache_lock:
            self._zone_cache_version += 1
            self._zone_cache.clear()

    # ---------- кэш горячих LTF-чтений (replay: per-candle N+1) ----------

    def _bump_ltf_cache(self) -> None:
        """Инвалидация кэша LTF-чтений — вызывается из каждого метода записи
        в ltf_* таблицы (insert/update/upsert/delete)."""
        with self._ltf_cache_lock:
            self._ltf_cache_version += 1
            self._ltf_cache.clear()

    def _ltf_cached(self, key: tuple, loader) -> Any:
        """Выборка через кэш версии _ltf_cache_version. Списки отдаются
        поверхностной копией — сортировка/append вызывающего кэш не портит."""
        if not self.ltf_cache_enabled:
            return loader()
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
        self.conn.commit()
        return int(cur.lastrowid) if cur.rowcount == 1 else None

    def update_inner_level(self, level_id: int, **fields: Any) -> None:
        if "evidence" in fields and isinstance(fields["evidence"], dict):
            fields["evidence"] = json.dumps(fields["evidence"], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE inner_level SET {cols} WHERE id=?", (*fields.values(), level_id)
        )
        self.conn.commit()

    def get_inner_level_by_key(
        self, parent_ob_id: int, timeframe: str, kind: str, price: float, pivot_time: int
    ) -> Optional["InnerLevel"]:
        r = self.conn.execute(
            """SELECT * FROM inner_level
               WHERE parent_ob_id=? AND timeframe=? AND kind=? AND price=? AND pivot_time=?""",
            (parent_ob_id, timeframe, kind, price, pivot_time),
        ).fetchone()
        return self._to_inner_level(r) if r else None

    def list_inner_levels(
        self,
        parent_ob_id: Optional[int] = None,
        instrument_id: Optional[int] = None,
        statuses: Optional[tuple[str, ...]] = None,
    ) -> list["InnerLevel"]:
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
        self.conn.commit()

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
        cur = self.conn.execute(
            """INSERT INTO visit (zone_id, cycle_id, entered_at, exited_at, max_depth,
                                  observed, extreme, d_raw)
               VALUES (?,?,?,?,?,?,?,?)""",
            (v.zone_id, v.cycle_id, v.entered_at, v.exited_at, v.max_depth,
             int(v.observed), v.extreme, v.d_raw),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def close_visit(self, visit_id: int, exited_at: int, max_depth: float,
                    exit_kind: Optional[str] = None,
                    extreme: Optional[float] = None, d_raw: Optional[float] = None) -> None:
        self.conn.execute(
            """UPDATE visit SET exited_at=?, max_depth=?, exit_kind=?,
                 extreme=COALESCE(?, extreme), d_raw=COALESCE(?, d_raw)
               WHERE id=?""",
            (exited_at, max_depth, exit_kind, extreme, d_raw, visit_id),
        )
        self.conn.commit()

    def update_visit_depth(self, visit_id: int, max_depth: float,
                           extreme: Optional[float] = None,
                           d_raw: Optional[float] = None) -> None:
        self.conn.execute(
            "UPDATE visit SET max_depth=?, extreme=COALESCE(?, extreme), "
            "d_raw=COALESCE(?, d_raw) WHERE id=?",
            (max_depth, extreme, d_raw, visit_id),
        )
        self.conn.commit()

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
        q = "SELECT * FROM visit WHERE zone_id=?"
        args: list[Any] = [zone_id]
        if cycle_id is not None:
            q += " AND cycle_id=?"
            args.append(cycle_id)
        q += " ORDER BY entered_at"
        return [self._to_visit(r) for r in self.conn.execute(q, args).fetchall()]

    def get_zone_max_depth(self, zone_id: int, cycle_id: int) -> Optional[float]:
        """Максимальная глубина теста за жизненный цикл (§15.7: контракт для
        будущего LTF Screener — глубина хранится отдельно от валидности)."""
        r = self.conn.execute(
            "SELECT MAX(max_depth) AS d FROM visit WHERE zone_id=? AND cycle_id=?",
            (zone_id, cycle_id),
        ).fetchone()
        return r["d"] if r and r["d"] is not None else None

    def open_visit_for(self, zone_id: int, cycle_id: int) -> Optional[Visit]:
        r = self.conn.execute(
            """SELECT * FROM visit WHERE zone_id=? AND cycle_id=? AND exited_at IS NULL
               ORDER BY entered_at DESC LIMIT 1""",
            (zone_id, cycle_id),
        ).fetchone()
        if not r:
            return None
        return self._to_visit(r)

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
        self.conn.commit()
        # rowcount надёжен в отличие от lastrowid после проигнорированной вставки
        return int(cur.lastrowid) if cur.rowcount == 1 else None

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

    def has_event(
        self, zone_id: int, cycle_id: int, kind: EventKind, occurred_at: int
    ) -> bool:
        """Точечная проверка дедуп-ключа (UNIQUE zone/cycle/kind/occurred_at)."""
        row = self.conn.execute(
            """SELECT 1 FROM event
               WHERE zone_id=? AND cycle_id=? AND kind=? AND occurred_at=?""",
            (zone_id, cycle_id, kind.value, occurred_at),
        ).fetchone()
        return row is not None

    def event_keys(self, zone_id: int, cycle_id: int) -> list[tuple[EventKind, int]]:
        """(kind, occurred_at) всех событий цикла зоны — для дедупа при эмите.

        Читает только ключи (покрывается UNIQUE-индексом), без выборки полных
        строк с LIMIT-окном, которое сужало дедуп на зонах с >500 событиями.
        """
        rows = self.conn.execute(
            "SELECT kind, occurred_at FROM event WHERE zone_id=? AND cycle_id=?",
            (zone_id, cycle_id),
        ).fetchall()
        return [(EventKind(r["kind"]), r["occurred_at"]) for r in rows]

    # ---------- deliveries ----------

    def record_delivery(self, d: Delivery) -> Optional[int]:
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO delivery
               (event_ids, destination, status, idempotency_key, delivered_at, error)
               VALUES (?,?,?,?,?,?)""",
            (json.dumps(d.event_ids), d.destination, d.status, d.idempotency_key,
             d.delivered_at, d.error),
        )
        self.conn.commit()
        # rowcount надёжен в отличие от lastrowid после проигнорированной вставки
        return int(cur.lastrowid) if cur.rowcount == 1 else None

    def update_delivery(self, delivery_id: int, status: str,
                        delivered_at: Optional[int] = None, error: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE delivery SET status=?, delivered_at=?, error=? WHERE id=?",
            (status, delivered_at, error, delivery_id),
        )
        self.conn.commit()

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
        self.conn.commit()

    def set_mute(self, zone_id: int, cycle_id: int, muted_until: Optional[int],
                 user: str = "owner") -> None:
        self.conn.execute(
            "UPDATE alert_state SET muted_until=? WHERE zone_id=? AND cycle_id=? AND user=?",
            (muted_until, zone_id, cycle_id, user),
        )
        self.conn.commit()

    # ---------- reviews / notes ----------

    def add_review(self, rev: Review) -> int:
        cur = self.conn.execute(
            """INSERT INTO review (zone_id, decision, author, text, boundary_version, created_at)
               VALUES (?,?,?,?,?,?)""",
            (rev.zone_id, rev.decision, rev.author, rev.text, rev.boundary_version,
             rev.created_at),
        )
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
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
        self.conn.commit()
        self._bump_ltf_cache()

    # ---------- LTF: observations ----------

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
        self.conn.commit()
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
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (s.observation_id, s.direction.value, s.trigger, s.stage, s.state,
             s.trigger_event_id, s.cancellation_reason, s.cancelled_at,
             s.created_at, s.updated_at),
        )
        self.conn.commit()
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

    @staticmethod
    def _to_ltf_scenario(r: sqlite3.Row) -> LtfScenario:
        return LtfScenario(
            id=r["id"], observation_id=r["observation_id"],
            direction=Direction(r["direction"]), trigger=r["trigger"],
            stage=r["stage"], state=r["state"],
            trigger_event_id=r["trigger_event_id"],
            cancellation_reason=r["cancellation_reason"],
            cancelled_at=r["cancelled_at"],
            created_at=r["created_at"], updated_at=r["updated_at"],
        )

    # ---------- LTF: pivots ----------

    def insert_ltf_pivot(self, p: LtfPivot) -> int:
        cur = self.conn.execute(
            """INSERT INTO ltf_pivot
               (instrument_id, price, kind, pivot_at, confirmed_at, role,
                role_assigned_at, "left", "right", candle_open_time, state)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (p.instrument_id, p.price, p.kind, p.pivot_at, p.confirmed_at, p.role,
             p.role_assigned_at, p.left, p.right, p.candle_open_time, p.state),
        )
        self.conn.commit()
        self._bump_ltf_cache()
        return int(cur.lastrowid)

    def get_ltf_pivot(self, pivot_id: int) -> Optional[LtfPivot]:
        def load() -> Optional[LtfPivot]:
            r = self.conn.execute(
                "SELECT * FROM ltf_pivot WHERE id=?", (pivot_id,)
            ).fetchone()
            return self._to_ltf_pivot(r) if r else None
        return self._ltf_cached(("ltf_pivot", pivot_id), load)

    def list_ltf_pivots(
        self, instrument_id: int, since_ms: Optional[int] = None
    ) -> list[LtfPivot]:
        def load() -> list[LtfPivot]:
            q = "SELECT * FROM ltf_pivot WHERE instrument_id=?"
            args: list[Any] = [instrument_id]
            if since_ms is not None:
                q += " AND pivot_at>=?"
                args.append(since_ms)
            q += " ORDER BY pivot_at, id"
            return [self._to_ltf_pivot(r) for r in self.conn.execute(q, args).fetchall()]
        return self._ltf_cached(("ltf_pivots", instrument_id, since_ms), load)

    def delete_ltf_pivots(self, instrument_id: int) -> int:
        """Удалить все pivots инструмента (resync при смене l/r). Журнал ролей
        удаляется первым — он ссылается на ltf_pivot по FK. Якоря диапазонов и
        движений (anchor_*_pivot_id, start/end_pivot_id) — обычные int без FK;
        после удаления читаются через get_ltf_pivot как None."""
        self.conn.execute(
            """DELETE FROM ltf_pivot_role_log WHERE pivot_id IN
               (SELECT id FROM ltf_pivot WHERE instrument_id=?)""",
            (instrument_id,),
        )
        cur = self.conn.execute(
            "DELETE FROM ltf_pivot WHERE instrument_id=?", (instrument_id,)
        )
        self.conn.commit()
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
        self.conn.commit()
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

    @staticmethod
    def _to_ltf_pivot(r: sqlite3.Row) -> LtfPivot:
        return LtfPivot(
            id=r["id"], instrument_id=r["instrument_id"], price=r["price"],
            kind=r["kind"], pivot_at=r["pivot_at"], confirmed_at=r["confirmed_at"],
            role=r["role"], role_assigned_at=r["role_assigned_at"],
            left=r["left"], right=r["right"],
            candle_open_time=r["candle_open_time"], state=r["state"],
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
        self.conn.commit()
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
        self.conn.commit()
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
                anchor_high_pivot_id, available_at, prev_version_id)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rng.scenario_id, rng.version, rng.lower, rng.upper, rng.mid,
             rng.anchor_low_pivot_id, rng.anchor_high_pivot_id, rng.available_at,
             rng.prev_version_id),
        )
        self.conn.commit()
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
        """Подходящие зоны (reason ok) активных сценариев на текущей версии
        диапазона — один агрегирующий запрос для всех инструментов
        (eligible_count списка активов без N+1, §4.2/§14). reason='' —
        строки до миграции: пригодность выводится из state (fallback
        eligibility.entry_reason: fresh+eligible → ok)."""
        q = """
            SELECT o.instrument_id AS instrument_id,
                   se.scenario_id AS scenario_id,
                   z.id AS entry_zone_id, z.lower AS lower, z.upper AS upper
            FROM ltf_scenario_entry se
            JOIN ltf_scenario s ON s.id = se.scenario_id
            JOIN ltf_observation o ON o.id = s.observation_id
            JOIN ltf_entry_zone z ON z.id = se.entry_zone_id
            WHERE s.state IN ('range_pending', 'monitoring_entries')
              AND se.state = 'fresh' AND se.eligible = 1
              AND (se.reason = 'ok' OR se.reason = '')
              AND se.range_version = COALESCE((
                  SELECT MAX(r.version) FROM ltf_range r
                  WHERE r.scenario_id = se.scenario_id
              ), 0)
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

    @staticmethod
    def _to_ltf_range(r: sqlite3.Row) -> LtfRange:
        return LtfRange(
            id=r["id"], scenario_id=r["scenario_id"], version=r["version"],
            lower=r["lower"], upper=r["upper"], mid=r["mid"],
            anchor_low_pivot_id=r["anchor_low_pivot_id"],
            anchor_high_pivot_id=r["anchor_high_pivot_id"],
            available_at=r["available_at"], prev_version_id=r["prev_version_id"],
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
        self.conn.commit()
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
        self.conn.commit()
        self._bump_ltf_cache()
        # после upsert lastrowid ненадёжен — читаем строку по уникальному ключу
        r = self.conn.execute(
            """SELECT * FROM ltf_scenario_entry
               WHERE scenario_id=? AND entry_zone_id=? AND range_version=?""",
            (se.scenario_id, se.entry_zone_id, se.range_version),
        ).fetchone()
        return self._to_ltf_scenario_entry(r)

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
        self.conn.commit()
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
                detected_at, dedupe_key, delivered, delayed)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (e.observation_id, e.scenario_id, e.kind,
             json.dumps(e.payload, ensure_ascii=False), e.occurred_at,
             e.detected_at, e.dedupe_key, int(e.delivered), int(e.delayed)),
        )
        self.conn.commit()
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

    def mark_ltf_event_delivered(self, event_id: int) -> None:
        self.conn.execute(
            "UPDATE ltf_event SET delivered=1 WHERE id=?", (event_id,)
        )
        self.conn.commit()
        self._bump_ltf_cache()

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
        self.conn.commit()
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
        self.conn.commit()
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
