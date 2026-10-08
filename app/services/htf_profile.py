"""Явная миграция сохранённого профиля HTF-контекста OB → OB,FVG (F06).

Кодовый default DetectorConfig остаётся «OB»: тесты, которые собирают
конфиг без файла, не меняются. Меняется только сохранённое значение,
которое после нормализации является ровно одним токеном OB. Любой другой
явный профиль записывается как пропуск и больше не переписывается.
Отсутствующий файл и отсутствующий ключ — не профиль: файл не создаём.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from ..models import now_ms

log = logging.getLogger(__name__)

META_KEY = "htf_profile:ob_to_ob_fvg:v1"


def settings_path(settings) -> Path:
    parent = Path(settings.db_path).parent
    if str(parent) in ("", "."):
        parent = Path("data")
    return parent / "settings.json"


def _tokens(raw: str) -> list[str]:
    return [part.strip().upper() for part in str(raw).split(",") if part.strip()]


def migrate_saved_htf_profile(db, settings) -> dict:
    """Один раз. Повторный вызов читает флаг и ничего не пишет в файл."""
    recorded = db.get_meta(META_KEY)
    if recorded:
        try:
            previous = json.loads(recorded)
        except json.JSONDecodeError:
            previous = {"action": "skipped", "reason": "flag_unreadable"}
        previous["repeated"] = True
        return previous

    path = settings_path(settings)
    if not path.exists():
        return {"action": "skipped", "reason": "no_file"}

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("профиль HTF не мигрирован: %s", exc)
        return {"action": "skipped", "reason": "unreadable", "error": str(exc)}

    detector = payload.get("detector", payload)
    if not isinstance(detector, dict) or "htf_context_types" not in detector:
        return {"action": "skipped", "reason": "key_absent"}

    saved = detector.get("htf_context_types")
    tokens = _tokens(saved if saved is not None else "")
    if tokens != ["OB"]:
        report = {
            "action": "skipped",
            "reason": "explicit_profile",
            "saved": saved,
            "at": now_ms(),
        }
        db.set_meta(META_KEY, json.dumps(report, ensure_ascii=False))
        log.info("профиль HTF оставлен как есть: %s", saved)
        return report

    detector["htf_context_types"] = "OB,FVG"
    if "detector" in payload and isinstance(payload.get("detector"), dict):
        payload["detector"] = detector
    else:
        payload = {"detector": detector}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)
    report = {
        "action": "migrated",
        "old": "OB",
        "new": "OB,FVG",
        "added": ["FVG"],
        "at": now_ms(),
    }
    db.set_meta(META_KEY, json.dumps(report, ensure_ascii=False))
    # Файл читается до миграции. Без этой записи процесс, который только
    # что мигрировал профиль, продолжает анализ со старым OB в памяти.
    detector_cfg = getattr(settings, "detector", None)
    if detector_cfg is not None and hasattr(detector_cfg, "htf_context_types"):
        detector_cfg.htf_context_types = "OB,FVG"
    log.info("профиль HTF мигрирован OB → OB,FVG")
    return report
