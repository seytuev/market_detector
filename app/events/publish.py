"""Публикации и пакеты движка (§9 ТЗ): существенные изменения, окно
объединения 60 секунд без продления, сводка после простоя, доставки.

Простой повтор вычисления без пересечения порога не создаёт публикацию.
Транспорт Telegram — поздняя волна (W3-E): здесь только шов wire() и
записи доставки; pending не означает отправлено.
"""
from __future__ import annotations

import hashlib

from . import store
from .rules import ENGINE_RULE_VERSION

_WIRED = False
_SETTINGS = None

# Качества источников: ухудшение и восстановление публикуются по одному разу (§9).
_BAD_QUALITY = {"stale", "unavailable", "auth_error", "capability_error"}
_GOOD_QUALITY = {"ok", "partial"}

_TRANSITION_KIND = {
    "appeared": "fact_appeared",
    "confirmed": "fact_confirmed",
    "invalidated": "fact_invalidated",
    "expired": "fact_expired",
    "reinterpreted": "fact_reinterpreted",
    "late_record": "fact_late_record",
}

_DIR_WORD = {
    "long": "LONG", "short": "SHORT", "neutral": "Нейтрально",
    "mixed": "Противоречивый контекст", "unknown": "Направление неизвестно",
}


def wire(sender=None, settings=None) -> None:
    """Шов подключения отправителя (расширяет W3-E). Пока только фиксирует
    факт подключения: реальной отправки здесь нет, app.notify не импортируется."""
    global _WIRED, _SETTINGS
    _SETTINGS = settings
    _WIRED = True


def is_wired() -> bool:
    return _WIRED


def _pub_key(*, rule_id: str, scope: str, episode_or_fact: str,
             kind: str, occurrence: str) -> str:
    """Идемпотентность (§9): правило/версия, scope, факт, тип и occurrence."""
    raw = f"{rule_id}|{ENGINE_RULE_VERSION}|{scope}|{episode_or_fact}|{kind}|{occurrence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _bundle_for(db, settings, now_ms: int) -> int:
    """Открытый пакет объединения; closes_at никогда не продлевается (§9)."""
    bundle = store.open_bundle(db)
    if bundle is not None:
        return bundle["id"]
    return store.create_bundle(
        db, opened_at=now_ms,
        closes_at=now_ms + int(settings.events_group_seconds) * 1000,
    )


def _day_occurrence(curr: dict) -> str:
    day = (curr.get("time_context") or {}).get("day_start_ms")
    return str(day if day is not None else curr.get("evaluated_at"))


def process_cycle(db, settings, *, prev: dict | None, curr: dict,
                  transitions: list[dict], now_ms: int) -> list[dict]:
    """Обнаружение существенных изменений и запись в открытый пакет.

    Возвращает созданные публикации. Повтор того же изменения не создаёт
    вторую строку (pub_key), повтор без пересечения порога — вообще ничего.
    """
    changes: list[dict] = []

    if prev is not None:
        # Смена итогового направления и направлений групп
        if prev.get("direction") != curr.get("direction"):
            changes.append({
                "rule_id": "MARKET_DIRECTION", "kind": "direction",
                "episode_or_fact": f"{prev.get('direction')}>{curr.get('direction')}",
                "occurrence": _day_occurrence(curr),
                "text": (
                    f"Направление изменилось: {_DIR_WORD.get(prev.get('direction'))} → "
                    f"{_DIR_WORD.get(curr.get('direction'))}"
                ),
                "fields": {"from": prev.get("direction"), "to": curr.get("direction")},
            })
        for group, key in (("events", "events_direction"), ("time", "time_direction")):
            if prev.get(key) != curr.get(key):
                label = "События" if group == "events" else "Время"
                changes.append({
                    "rule_id": "MARKET_DIRECTION", "kind": "direction",
                    "episode_or_fact": f"{group}:{prev.get(key)}>{curr.get(key)}",
                    "occurrence": _day_occurrence(curr),
                    "text": (
                        f"{label}: {_DIR_WORD.get(prev.get(key))} → "
                        f"{_DIR_WORD.get(curr.get(key))}"
                    ),
                    "fields": {"group": group, "from": prev.get(key), "to": curr.get(key)},
                })
        if bool(prev.get("contradictory")) != bool(curr.get("contradictory")):
            on = bool(curr.get("contradictory"))
            changes.append({
                "rule_id": "MARKET_DIRECTION", "kind": "conflict",
                "episode_or_fact": f"contradictory>{on}",
                "occurrence": _day_occurrence(curr),
                "text": "Контекст стал противоречивым" if on else "Противоречие снято",
                "fields": {"contradictory": on},
            })
        # Временная граница — самостоятельный переход (§9), даже без смены итога
        prev_day = (prev.get("time_context") or {}).get("weekday")
        curr_day = (curr.get("time_context") or {}).get("weekday")
        if prev_day is not None and curr_day is not None and prev_day != curr_day:
            changes.append({
                "rule_id": "TIME_BOUNDARY", "kind": "time_boundary",
                "episode_or_fact": "",
                "occurrence": _day_occurrence(curr),
                "text": "Временной фактор обновился",
                "fields": {"from_weekday": prev_day, "to_weekday": curr_day},
            })
        # Покрытие через порог достаточности в любую сторону
        prev_cov, curr_cov = prev.get("coverage"), curr.get("coverage")
        if prev_cov and curr_cov and prev_cov.get("sufficient") != curr_cov.get("sufficient"):
            changes.append({
                "rule_id": "BASKET_COVERAGE", "kind": "coverage",
                "episode_or_fact": f"{prev_cov.get('sufficient')}>{curr_cov.get('sufficient')}",
                "occurrence": _day_occurrence(curr),
                "text": (
                    "Покрытие корзины стало достаточным" if curr_cov.get("sufficient")
                    else "Покрытие корзины упало ниже порога достаточности"
                ),
                "fields": {"coverage": curr_cov},
            })
        # Опубликованная статистика: сдвиг ≥ change_pp п.п. или потеря/появление
        prev_st, curr_st = prev.get("statistics"), curr.get("statistics")
        if isinstance(prev_st, dict) and isinstance(curr_st, dict):
            flip = prev_st.get("available") != curr_st.get("available")
            shift = (
                prev_st.get("available") and curr_st.get("available")
                and prev_st.get("value") is not None and curr_st.get("value") is not None
                and abs(curr_st["value"] - prev_st["value"]) * 100
                >= float(settings.events_stats_change_pp)
            )
            if flip or shift:
                changes.append({
                    "rule_id": "BASKET_STATS", "kind": "stats",
                    "episode_or_fact": "basket_day_return",
                    "occurrence": _day_occurrence(curr),
                    "text": f"Статистика обновилась: {curr_st.get('text')}",
                    "fields": {
                        "from_value": prev_st.get("value"),
                        "to_value": curr_st.get("value"),
                    },
                })

    # Переходы фактов памяти (появление/подтверждение/инвалидация/истечение/…)
    for tr in transitions or []:
        kind = _TRANSITION_KIND.get(tr.get("transition"))
        if kind is None:
            continue
        fact = store.get_fact(db, tr["fact_key"]) or {}
        changes.append({
            "rule_id": fact.get("rule_id") or "UNKNOWN",
            "kind": kind,
            "episode_or_fact": tr["fact_key"],
            "occurrence": str(tr.get("at_ms")),
            "window_ms": (fact.get("payload") or {}).get("window_ms"),
            "text": _transition_text(tr, fact),
            "fields": {
                "fact_key": tr["fact_key"],
                "transition": tr.get("transition"),
                "reason": tr.get("reason"),
                "direction": fact.get("direction"),
            },
        })

    # Источники: одна публикация на ухудшение и одна на восстановление (§9)
    changes.extend(_source_changes(db, curr))

    created = []
    for change in changes:
        bundle_id = _bundle_for(db, settings, now_ms)
        pub_key = _pub_key(
            rule_id=change["rule_id"], scope="crypto_market",
            episode_or_fact=change["episode_or_fact"], kind=change["kind"],
            occurrence=change["occurrence"],
        )
        payload = {"text": change["text"], **change.get("fields", {})}
        inserted = store.add_publication(
            db, pub_key=pub_key, bundle_id=bundle_id, kind=change["kind"],
            assessment_id=curr.get("assessment_id"),
            window_ms=change.get("window_ms"), payload=payload, created_at=now_ms,
        )
        if inserted:
            created.append({"pub_key": pub_key, "bundle_id": bundle_id,
                            "kind": change["kind"], "payload": payload})
    return created


def _transition_text(tr: dict, fact: dict) -> str:
    transition = tr.get("transition")
    if transition == "expired":
        return "Событие больше не учитывается"
    if transition == "invalidated":
        return "Условие опровергнуто"
    if transition == "appeared":
        summary = (fact.get("payload") or {}).get("summary")
        if summary:
            return f"Новое событие: {summary}"
        return f"Новое событие по правилу {fact.get('rule_id')}"
    if transition == "confirmed":
        return "Предварительное событие подтверждено"
    if transition == "reinterpreted":
        to_dir = (tr.get("payload") or {}).get("to_direction")
        word = _DIR_WORD.get(to_dir, "без направления")
        return f"Интерпретация события изменена: теперь {word}"
    if transition == "late_record":
        return "Поздняя запись: исторический факт сохранён, не текущий сигнал"
    return f"Переход факта: {transition}"


def _source_changes(db, curr: dict) -> list[dict]:
    """Сравнение качества источников с уже опубликованным (notified_quality)."""
    changes = []
    for source, state in store.get_source_states(db).items():
        quality = state.get("quality")
        payload = dict(state.get("payload") or {})
        notified = payload.get("notified_quality")
        if notified == quality:
            continue
        if quality in _BAD_QUALITY and notified != quality:
            changes.append({
                "rule_id": "SRC_DEGRADED", "kind": "source_degraded",
                "episode_or_fact": source,
                "occurrence": str(state.get("updated_at")),
                "text": f"Данные ухудшились: {source} — {quality}",
                "fields": {"source": source, "quality": quality,
                           "detail": state.get("detail")},
            })
        elif quality in _GOOD_QUALITY and notified in _BAD_QUALITY:
            changes.append({
                "rule_id": "SRC_RECOVERED", "kind": "source_recovered",
                "episode_or_fact": source,
                "occurrence": str(state.get("updated_at")),
                "text": f"Данные восстановились: {source} — {quality}",
                "fields": {"source": source, "quality": quality},
            })
        payload["notified_quality"] = quality
        store.set_source_state(
            db, source=source, quality=quality, detail=state.get("detail"),
            at_ms=state.get("updated_at") or curr.get("evaluated_at") or 0,
            payload=payload,
        )
    return changes


def flush_due(db, settings, now_ms: int) -> list[dict]:
    """Публикация пакетов с истёкшим окном объединения и записи доставки.

    Лента — всегда («sent» сразу); Telegram — «pending» только при
    подключённом отправителе и включённой настройке, иначе «unavailable»
    с честной причиной (§9, AC16).
    """
    published = []
    for bundle in store.list_bundles(db, status="open"):
        if bundle["closes_at"] > now_ms:
            continue
        store.set_bundle_status(db, bundle["id"], "published", published_at=now_ms)
        store.upsert_delivery(
            db, bundle_id=bundle["id"], recipient="owner", channel="feed",
            status="sent", at_ms=now_ms, delivered_at=now_ms,
        )
        if is_wired() and getattr(settings, "events_notify_enabled", False):
            store.upsert_delivery(
                db, bundle_id=bundle["id"], recipient="owner", channel="telegram",
                status="pending", at_ms=now_ms,
            )
        else:
            store.upsert_delivery(
                db, bundle_id=bundle["id"], recipient="owner", channel="telegram",
                status="unavailable", at_ms=now_ms, detail="Доставка недоступна",
            )
        published.append(store.get_bundle(db, bundle["id"]))
    return published


def startup_digest(db, settings, now_ms: int) -> None:
    """Одна сводка после простоя (§9, AC18): итог, пропущенные переходы,
    отмены и истечения, неизвестные данные. Старые события не переиздаются
    как свежие — только счётчики и ссылки. Перезапуск не создаёт дубль."""
    last = store.latest_assessment(db)
    if last is None:
        return
    gap = now_ms - int(last["evaluated_at"])
    if gap <= int(settings.events_digest_gap_seconds) * 1000:
        return
    guard = db.get_meta("events:last_digest_gap_end")
    if guard is not None and int(guard) == int(last["evaluated_at"]):
        return
    transitions = store.fact_transitions_since(db, int(last["evaluated_at"]))
    counts: dict[str, int] = {}
    for tr in transitions:
        counts[tr["transition"]] = counts.get(tr["transition"], 0) + 1
    payload = {
        "summary": "После простоя: сводка",
        "gap_ms": gap,
        "counts": counts,
        "expired_facts": [t["fact_key"] for t in transitions if t["transition"] == "expired"],
        "invalidated_facts": [t["fact_key"] for t in transitions if t["transition"] == "invalidated"],
        "unknown_dependencies": last.get("unknown_dependencies") or [],
        "direction": last.get("direction"),
        "market_line": last.get("market_line"),
    }
    bundle_id = store.create_bundle(
        db, opened_at=now_ms, closes_at=now_ms, kind="downtime_digest",
    )
    store.add_publication(
        db,
        pub_key=_pub_key(
            rule_id="DOWNTIME_DIGEST", scope="crypto_market",
            episode_or_fact=str(last["evaluated_at"]), kind="downtime_digest",
            occurrence=str(now_ms),
        ),
        bundle_id=bundle_id, kind="downtime_digest",
        assessment_id=last.get("assessment_id"), window_ms=None,
        payload={"text": "После простоя: сводка", **payload}, created_at=now_ms,
    )
    store.set_bundle_status(db, bundle_id, "open", payload=payload)
    flush_due(db, settings, now_ms)
    db.set_meta("events:last_digest_gap_end", str(int(last["evaluated_at"])))


def render_bundle_text(db, bundle_id: int, base_url: str = "") -> str:
    """Текст пакета: итог, изменения, обе причины конфликта, качество, ссылка."""
    bundle = store.get_bundle(db, bundle_id)
    if bundle is None:
        return ""
    pubs = store.bundle_publications(db, bundle_id)
    assessment = None
    for pub in reversed(pubs):
        if pub.get("assessment_id"):
            assessment = store.get_assessment(db, pub["assessment_id"])
            if assessment is not None:
                break
    lines: list[str] = []
    if bundle["kind"] == "downtime_digest":
        summary = (bundle.get("payload") or {}).get("summary")
        lines.append(summary or "После простоя: сводка")
    lines.append(assessment["market_line"] if assessment else "События и время · Крипторынок")
    for pub in pubs:
        text = (pub.get("payload") or {}).get("text")
        if text and text not in lines:
            lines.append(text)
    if assessment is not None:
        if assessment.get("contradictory"):
            for reason in assessment.get("reasons") or []:
                text = reason.get("text")
                if text and text not in lines:
                    lines.append(text)
            note = assessment.get("conflict_note") or "Контекст противоречивый"
            if note not in lines:
                lines.append(note)
        coverage = assessment.get("coverage")
        cov_text = (
            f"{coverage['ok']}/{coverage['total']}" if coverage else "нет данных"
        )
        lines.append(f"Качество: {assessment.get('quality')}; покрытие: {cov_text}")
        lines.append(f"{base_url}/events.html#assessment={assessment['assessment_id']}")
    return "\n".join(lines)
