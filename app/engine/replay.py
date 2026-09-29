"""Обвязка replay (§11): воспроизведение истории из БД и статистика.

Загрузка истории через адаптер сюда не входит (это обязанность воркера).
Восстановленные при догрузке пропусков события получают исходный occurred_at
и delayed=True — это делает Scanner.replay_instrument (detected_at — текущее
время, occurred_at — граница исторической свечи, §11).
"""
from __future__ import annotations

from typing import Optional

from ..config import DetectorConfig
from ..db import Database
from ..models import EventKind, TIMEFRAME_MINUTES, ZoneStatus, ZoneType
from .lifecycle import is_ob_like
from .scanner import Scanner

# Терминальные события, завершающие рисунок зоны (§15.1.3)
_TERMINAL_EVENTS = {
    EventKind.FVG_FILLED: "fvg_filled (§3)",
    EventKind.LEVEL_TAKEN: "swept (§7)",
    EventKind.DEPTH_90: "worked_90 (§6)",
    EventKind.BREAKER_ARCHIVED: "breaker_broken (§6)",
    EventKind.PRB_ARCHIVED: "prb_broken (§5)",
    EventKind.JUMP_THROUGH: "jumped_through (§6)",
}


def migrate_display_fields(db: Database) -> dict:
    """Разовая/идемпотентная миграция существующих зон под §15.1.3.

    - display_until/end_reason вычисляются из уже записанных терминальных
      событий (зоны, завершившиеся до появления полей в движке);
    - OB, конвертированный в Breaker до введения сегментов, получает
      display_until = момент создания Breaker-преемника (§15.1.4);
    - FVG получают display_from = средняя свеча тройки (§15.1.7).

    Пишет типизированные колонки zone; бэкфилл колонок из evidence JSON
    старых БД выполняет db.migrate().

    ТЗ «Единый движок» (22.09.2026): прежняя постобработка §9.8 (архивация
    H1-зон по первому касанию) удалена вместе с самой веткой lifecycle;
    DEPTH_90 завершает рисунок только для PRB/ручных не-OB (для OB это
    событие глубины, а не завершение). Восстановление зон, завершённых
    по старым правилам, — tools/restore_unified_zones.py.
    """
    stats = {"display_until": 0, "display_from": 0}

    for z in db.get_zones():
        # display_from для FVG — средняя свеча исходной тройки (§15.1.7)
        if (
            z.type == ZoneType.FVG
            and z.display_from is None
            and len(z.source_candles) >= 3
        ):
            db.update_zone(z.id, display_from=z.source_candles[1])
            stats["display_from"] += 1

        if z.display_until is None:
            # терминальное событие самой зоны; DEPTH_90 терминально только
            # для типов со статусом WORKED по 90% (PRB, ручные без правила)
            terminal = None
            for e in db.get_events(z.id):
                if e.cycle_id != z.cycle_id:
                    continue
                if e.kind == EventKind.DEPTH_90 and is_ob_like(z):
                    continue
                if e.kind in _TERMINAL_EVENTS:
                    if terminal is None or e.occurred_at < terminal.occurred_at:
                        terminal = e
            if terminal is not None:
                fields: dict = {"display_until": terminal.occurred_at}
                if z.end_reason is None:
                    fields["end_reason"] = _TERMINAL_EVENTS[terminal.kind]
                db.update_zone(z.id, **fields)
                stats["display_until"] += 1
            elif z.status == ZoneStatus.CONVERTED:
                # сегмент OB заканчивается конверсией: ищем Breaker-преемника
                succ = db.find_relations(predecessor_ob_id=z.id)
                if succ:
                    br_zone = db.get_zone(succ[0].zone_id)
                    if br_zone is not None:
                        fields = {"display_until": br_zone.formed_at}
                        if z.end_reason is None:
                            fields["end_reason"] = "converted_to_breaker (§6/§15.6)"
                        db.update_zone(z.id, **fields)
                        stats["display_until"] += 1

    # §15.3/R12: починка связей OB↔FVG, испорченных старой перезаписью —
    # evidence.confirming_fvg_formed_at (записан при первом подтверждении)
    # указывает истинный FVG; relation выравниваем по нему
    repaired = 0
    for z in db.get_zones():
        if z.type not in (ZoneType.OB, ZoneType.PRB):
            continue
        formed = z.evidence.get("confirming_fvg_formed_at")
        if formed is None:
            continue
        rel = db.get_relation(z.id)
        fvg = next(
            (c for c in db.get_zones(z.instrument_id, types=[ZoneType.FVG])
             if c.formed_at == formed and c.timeframe == z.timeframe),
            None,
        )
        if fvg is None:
            continue
        mismatch = rel is None or rel.confirming_fvg_id != fvg.id
        time_bad = z.confirmed_at is not None and z.confirmed_at != fvg.confirmed_at
        if not mismatch and not time_bad:
            continue
        # намеренная починка исторической связи — не через set_relation
        # (там first-write-wins), а прямым UPDATE
        db.conn.execute(
            """INSERT INTO zone_relation (zone_id, confirming_fvg_id) VALUES (?,?)
               ON CONFLICT (zone_id) DO UPDATE SET confirming_fvg_id=excluded.confirming_fvg_id""",
            (z.id, fvg.id),
        )
        if time_bad:
            db.conn.execute(
                "UPDATE zone SET confirmed_at=? WHERE id=?", (fvg.confirmed_at, z.id)
            )
            db.invalidate_zone_cache()  # прямой SQL мимо update_zone
        db.conn.commit()
        repaired += 1
    stats["relations_repaired"] = repaired
    return stats


def replay_from_db(db: Database, cfg: DetectorConfig, instrument_id: int,
                   timeframes: Optional[set] = None) -> dict:
    """Прогоняет replay инструмента и возвращает статистику.

    timeframes — необязательное ограничение ТФ (воркер применяет настройку
    scan_timeframes); None — все ТФ из БД.
    """
    scanner = Scanner(db, cfg)
    events = scanner.replay_instrument(instrument_id, timeframes=timeframes)
    tfs = timeframes or set(TIMEFRAME_MINUTES)
    zones = db.get_zones(instrument_id)
    zones_by_type: dict[str, int] = {}
    for z in zones:
        zones_by_type[z.type.value] = zones_by_type.get(z.type.value, 0) + 1
    n_candles = sum(
        len(db.get_candles(instrument_id, tf)) for tf in tfs
    )
    return {
        "instrument_id": instrument_id,
        "candles": n_candles,
        "zones": len(zones),
        "zones_by_type": zones_by_type,
        "events_created": len(events),
        # §11: восстановленные события помечены задержкой, время — исходное
        "delayed_events": sum(1 for e in events if e.delayed),
    }
