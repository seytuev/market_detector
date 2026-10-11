"""Снимок графика Telegram: один момент для слоёв, подписи и повторной доставки.

Режим события не пересчитывает картинку по более поздним свечам. Цены
берутся из записи события. Алгоритм детекции здесь не меняется.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from .outbox import fingerprint

# Запас цены и пустое место справа — параметры проверки, не торговые правила.
PRICE_MARGIN = 0.08
RIGHT_PAD_RATIO = 0.08

KIND_RU = {
    "bos": "BOS",
    "sms": "SMS",
    "touch": "Касание",
    "sweep_confirmed": "Снятие",
    "sweep_failed": "Уровень пройден",
    "cancellation": "Отмена",
    "entries_ready": "Зоны готовы",
    "range_ready": "Диапазон",
}

_LEVEL_KEYS = ("break_level", "level", "price", "close_price")


def layers_for_kind(kind: str, payload: Optional[dict] = None) -> tuple[str, ...]:
    """Слои автоматического графика. Ручной запрос по-прежнему передаёт свою маску."""
    payload = payload or {}
    rng = payload.get("range") if isinstance(payload.get("range"), dict) else {}
    has_range = rng.get("lower") is not None and rng.get("upper") is not None
    if kind in ("bos", "sms", "cancellation"):
        return ("bos", "range") if has_range else ("bos",)
    if kind in ("touch", "sweep_confirmed", "sweep_failed"):
        return ("entries",)
    if kind in ("entries_ready", "range_ready"):
        return ("range", "entries")
    return ("htf", "bos", "range", "entries")


def event_level(payload: Optional[dict]) -> Optional[float]:
    """Цена из записи события. Нет ключа — None, уровень не выдумывается."""
    payload = payload or {}
    for key in _LEVEL_KEYS:
        value = payload.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _canonical(value: Any) -> Any:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_canonical(v) for v in value]
    return value


def chart_signature(ev) -> str:
    """Семантика картинки. Время файла и косметический дрейф цены сюда не входят."""
    payload = getattr(ev, "payload", None) or {}
    entries = []
    for entry in payload.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        entries.append({
            "type": entry.get("type"),
            "lower": entry.get("lower"),
            "upper": entry.get("upper"),
            "overlap": entry.get("overlap"),
            "outside_premium": entry.get("outside_premium"),
        })
    rng = payload.get("range") if isinstance(payload.get("range"), dict) else {}
    body = _canonical({
        "kind": ev.kind,
        "observation_id": ev.observation_id,
        "scenario_id": ev.scenario_id,
        "occurred_at": ev.occurred_at,
        "level": event_level(payload),
        "reason": payload.get("reason"),
        "outcome": payload.get("outcome"),
        "entry_zone_id": payload.get("entry_zone_id"),
        "range": {k: rng.get(k) for k in ("lower", "upper", "mid", "version")},
        "entries": entries,
    })
    return fingerprint(body)


def right_pad(n: int) -> int:
    """Пустые позиции справа: около 8% ширины и не больше 10%."""
    if n <= 0:
        return 1
    pad = max(1, int(round(n * RIGHT_PAD_RATIO)))
    return min(pad, max(1, int(n * 0.10)))


def in_working_band(
    price: float, low: float, high: float, margin: float = PRICE_MARGIN,
) -> bool:
    span = (high - low) or 1.0
    return (low - span * margin) <= price <= (high + span * margin)


def zone_on_scale(lower: float, upper: float, low: float, high: float) -> bool:
    """Обе границы в рабочем окне H1. Одной близкой границы недостаточно."""
    return in_working_band(lower, low, high) and in_working_band(upper, low, high)


def distance_pct(price: float, lower: float, upper: float) -> Optional[float]:
    if not price:
        return None
    if lower <= price <= upper:
        return 0.0
    nearest = lower if abs(price - lower) <= abs(price - upper) else upper
    return abs(price - nearest) / abs(price) * 100.0


def write_chart_meta(png_path: Path, signature: str, event_id: int, as_of: int) -> None:
    png_path.with_suffix(".json").write_text(
        json.dumps(
            {"signature": signature, "event_id": event_id, "as_of": int(as_of)},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def chart_meta_matches(png_path: Path, signature: str) -> bool:
    meta = png_path.with_suffix(".json")
    if not png_path.exists() or not meta.exists():
        return False
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("signature") == signature


def find_saved_chart(directory: Path, signature: str) -> Optional[str]:
    """Уже нарисованная картинка с той же семантикой. Запись события не меняется."""
    if not directory.exists():
        return None
    for meta in directory.glob("ltf_event_*.json"):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("signature") != signature:
            continue
        png = meta.with_suffix(".png")
        if png.exists():
            return str(png)
    return None
