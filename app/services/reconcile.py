"""Сверка жизненного цикла зоны с уже записанными свечами.

GET состояние не меняет. Запись заполнения делает только POST и только
существующим track_zone на первой свече, которая достигает полной глубины.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..engine.depth import max_depth_in_interval
from ..engine.lifecycle import track_zone
from ..models import EventKind, ZoneType, now_ms

_META = "reconcile:{zone_id}"


def _key(zone_id: int) -> str:
    return _META.format(zone_id=zone_id)


def read_reconcile(db, zone_id: int) -> dict[str, Any]:
    """Статус задачи. Пустая meta для ещё не запущенной сверки — queued."""
    raw = db.get_meta(_key(zone_id))
    if not raw:
        return {"status": "queued"}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "error", "error": "статус сверки повреждён"}
    if not isinstance(data, dict) or not data.get("status"):
        return {"status": "queued"}
    return data


def _write(db, zone_id: int, payload: dict[str, Any]) -> None:
    db.set_meta(_key(zone_id), json.dumps(payload, ensure_ascii=False))


def proving_candle(db, zone):
    """Первая закрытая свеча своего ТФ после подтверждения с глубиной 1."""
    start = zone.confirmed_at if zone.confirmed_at is not None else (zone.formed_at or 0)
    candles = db.get_candles(zone.instrument_id, zone.timeframe, start_ms=start or 0)
    for candle in candles:
        if not candle.closed:
            continue
        if zone.confirmed_at is not None and candle.close_time < zone.confirmed_at:
            continue
        if max_depth_in_interval(zone, candle.low, candle.high) >= 1.0:
            return candle
    return None


def fvg_has_fill_event(db, zone) -> bool:
    if zone is None or zone.id is None:
        return False
    return any(
        kind == EventKind.FVG_FILLED
        for kind, _ts in db.event_keys(zone.id, zone.cycle_id)
    )


def fvg_contradiction(db, zone, price: Optional[float], quote_ok: bool) -> bool:
    """Активная FVG без события заполнения, но движение уже дошло до дальней границы.

    Одна цена без свечи не создаёт время заполнения — только признак сверки.
    """
    if zone is None or zone.type != ZoneType.FVG:
        return False
    if zone.display_until is not None:
        return False
    if fvg_has_fill_event(db, zone):
        return False
    from ..engine.depth import zone_depth

    if quote_ok and price is not None and zone_depth(zone, price) >= 1.0:
        return True
    return proving_candle(db, zone) is not None


def reconcile_zone(db, settings, zone_id: int) -> dict[str, Any]:
    """queued → running → resolved | error. Повторный вызов запускает расчёт заново."""
    zone = db.get_zone(zone_id)
    if zone is None:
        return {"status": "error", "error": "зона не найдена", "missing": True}
    _write(db, zone_id, {"status": "running", "at": now_ms(), "zone_id": zone_id})
    try:
        candle = proving_candle(db, zone)
        if candle is None:
            result = {
                "status": "resolved",
                "outcome": "fill_not_proven",
                "at": now_ms(),
                "zone_id": zone_id,
            }
        else:
            track_zone(
                db, settings.detector, zone,
                candle.low, candle.high, candle.close_time,
                False, None, now_ms(), silent=False,
            )
            fresh = db.get_zone(zone_id)
            reason = (fresh.end_reason or "") if fresh is not None else ""
            if "fvg_filled" in reason:
                result = {
                    "status": "resolved",
                    "outcome": "fill_recorded",
                    "at": fresh.display_until,
                    "end_reason": reason,
                    "zone_id": zone_id,
                }
            else:
                result = {
                    "status": "resolved",
                    "outcome": "fill_not_proven",
                    "at": now_ms(),
                    "zone_id": zone_id,
                }
    except Exception as exc:
        result = {
            "status": "error",
            "error": str(exc)[:200],
            "at": now_ms(),
            "zone_id": zone_id,
        }
    _write(db, zone_id, result)
    return result
