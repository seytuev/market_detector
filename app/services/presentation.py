"""Снимок сообщений: коды каталога и факты, без доменных правил на клиенте.

assemble() чистый: тесты трёх вариантов BTC не требуют живого рынка.
build_presentation() читает ту же машину BOS/SMS, которая регистрирует слом.
"""
from __future__ import annotations

from typing import Any, Optional

from ..engine.depth import max_depth_in_interval, zone_depth
from ..engine.ltf.breaks import expected_structure_conditions
from ..engine.ltf.pivots import PivotCandidate
from ..models import Direction, EventKind, ZoneStatus, ZoneType
from ..notify.formatting import format_level, fmt_time_msk
from .htf_parent import context_type_name, parent_decision
from .market_copy import SCHEMA_VERSION, render
from .reconcile import fvg_contradiction, fvg_has_fill_event, read_reconcile

BASIS_RU = {
    "manual": "ручной выбор",
    "price_inside": "цена внутри зоны",
    "nearest": "близость к цене — это навигация, не оценка качества",
    "last_scenario": "последний действующий сценарий",
    "last_contact": "последний контакт с зоной",
}

_END_KNOWN = (
    ("fvg_filled", "полное заполнение"),
    ("close_beyond", "закрытие свечи своего таймфрейма за дальней границей"),
    ("breaker_broken", "поломка breaker"),
    ("converted_to_breaker", "переход в breaker"),
    ("prb_broken", "поломка PRB"),
    ("swept", "снятие уровня"),
    ("worked_90", "глубина 90%"),
)

# Пока статус родителя или расчёт не позволяют вывод, уровень BOS/SMS не показываем.
_HOLD_STRUCTURE = frozenset({"C18", "Q02", "Q03", "Q04", "Q05", "Q06"})


def _px(value: Optional[float]) -> dict[str, Any]:
    return format_level(value)


def _level_text(fmt: dict[str, Any]) -> str:
    """Условие показывает точную цену, если компакт её меняет."""
    if not fmt or fmt.get("value") is None:
        return "—"
    if fmt.get("approximate"):
        return fmt["exact"]
    return fmt["text"]


def _bound_text(fmt: dict[str, Any]) -> str:
    if not fmt or fmt.get("value") is None:
        return "—"
    if fmt.get("approximate"):
        return "≈" + fmt["compact"]
    return fmt["text"]


def _arrow(direction: Optional[str]) -> str:
    if direction == "bull":
        return "↑"
    if direction == "bear":
        return "↓"
    return ""


def _side_ru(side: Optional[str]) -> str:
    return "выше" if side == "above" else "ниже"


def _age_ru(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} с"
    minutes = s // 60
    if minutes < 60:
        return f"{minutes} мин"
    return f"{minutes // 60} ч"


def _when(ms: Optional[int]) -> str:
    if not ms:
        return "—"
    return fmt_time_msk(int(ms))


def _instrument_line(ins: Optional[dict[str, Any]]) -> str:
    ins = ins or {}
    symbol = str(ins.get("symbol") or "")
    if symbol.endswith("USDT") and len(symbol) > 4:
        pair = f"{symbol[:-4]} / USDT"
    elif symbol:
        pair = symbol
    else:
        pair = "—"
    parts = [pair]
    if ins.get("venue"):
        parts.append(str(ins["venue"]))
    if ins.get("market_type"):
        parts.append(str(ins["market_type"]))
    return " · ".join(parts)


def _scope_name(zone: dict[str, Any]) -> str:
    tf = zone.get("timeframe")
    kind = str(zone.get("type") or "").lower()
    if kind == "fvg" and tf == "W1":
        return "недельной FVG"
    if kind == "fvg" and tf == "D1":
        return "дневной FVG"
    if kind == "fvg":
        return f"FVG {tf or ''}".strip()
    return "зоны"


def _job_text(state: Optional[dict[str, Any]]) -> str:
    state = state or {"status": "queued"}
    status = state.get("status") or "queued"
    if status == "queued":
        return "расчёт в очереди. Запустить сверку"
    if status == "running":
        return "сверка идёт"
    if status == "error":
        return f"ошибка: {state.get('error') or 'сбой'}. Повторить расчёт"
    outcome = state.get("outcome")
    if outcome == "fill_recorded":
        return "заполнение записано"
    if outcome == "fill_not_proven":
        return "свечи не доказали полное заполнение. Повторить расчёт"
    return "сверка завершена. Повторить расчёт"


def _reconcile_action(zone_id: int, state: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Кнопка сверки. Идущий расчёт и уже записанное заполнение кнопки не требуют."""
    status = (state or {}).get("status") or "queued"
    outcome = (state or {}).get("outcome")
    if status == "running" or outcome == "fill_recorded":
        return None
    label = "Запустить сверку" if status == "queued" else "Повторить расчёт"
    return {
        "kind": "reconcile",
        "zone_id": zone_id,
        "label": label,
        "href": f"/api/zones/{zone_id}/reconcile",
    }


def _presentation_actions(facts: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    seen: set[Any] = set()
    inc = facts.get("inconsistent") or {}
    candidates = []
    if isinstance(inc, dict) and inc.get("action"):
        candidates.append(inc["action"])
    for row in facts.get("other_zones") or []:
        if isinstance(row, dict) and row.get("action"):
            candidates.append(row["action"])
    for action in candidates:
        zone_id = action.get("zone_id")
        if zone_id is None or zone_id in seen:
            continue
        seen.add(zone_id)
        href = action.get("href") or f"/api/zones/{zone_id}/reconcile"
        found.append({
            "kind": action.get("kind") or "reconcile",
            "zone_id": zone_id,
            "label": action.get("label") or "Запустить сверку",
            "href": href,
        })
    return found


def _relation(price: Optional[float], lower: Optional[float], upper: Optional[float],
              quote_ok: bool) -> str:
    if not quote_ok or price is None or lower is None or upper is None:
        return "unknown"
    if price == lower or price == upper:
        return "boundary"
    if lower < price < upper:
        return "inside"
    if price > upper:
        return "above"
    return "below"


def _location_sentence(relation: str, price_text: str) -> str:
    if relation == "unknown":
        return "Текущее положение цены неизвестно"
    if relation == "inside":
        return f"Цена {price_text} внутри зоны"
    if relation == "above":
        return f"Цена {price_text} выше зоны"
    if relation == "below":
        return f"Цена {price_text} ниже зоны"
    if relation == "boundary":
        return f"Цена {price_text} на границе зоны"
    return "Текущее положение цены неизвестно"


def end_reason_text(reason: Optional[str], when: str) -> Optional[str]:
    """Фраза только из записанной причины. Пустая причина — не пробой."""
    text = (reason or "").strip()
    if not text:
        return None
    for prefix, phrase in _END_KNOWN:
        if text.startswith(prefix) or prefix in text:
            return f"{phrase}, {when}"
    return f"записанная причина: {text}, {when}"


def _missing_ob(evidence: Optional[dict[str, Any]]) -> tuple[str, str]:
    """C05 только если в evidence записано отсутствие внешнего FVG."""
    ev = evidence or {}
    relation = ev.get("relation") if isinstance(ev.get("relation"), dict) else {}
    recorded = (
        ev.get("external_fvg") is False
        or ev.get("missing_confirmation") == "external_fvg"
        or "fvg" in str(ev.get("unconfirmed_reason") or "").lower()
        or (
            "confirming_fvg_id" in relation and not relation.get("confirming_fvg_id")
        )
        or (
            "confirming_fvg_formed_at" in ev and not ev.get("confirming_fvg_formed_at")
        )
    )
    if recorded:
        return "C05", ""
    missing = ev.get("unconfirmed_reason") or ev.get("missing")
    if not missing:
        missing = "условие подтверждения не записано"
    return "C06", str(missing)


def condition_texts(conditions: Optional[dict[str, Any]]) -> list[dict[str, Any]]:
    """Тексты BOS и SMS по статусу машины. Уровень 0 не подставляется."""
    if not conditions:
        return []
    out = []
    for cond in (conditions.get("bos"), conditions.get("sms")):
        if not cond:
            continue
        level_fmt = _px(cond.get("level")) if cond.get("level") is not None else None
        level = _level_text(level_fmt) if level_fmt else None
        side = _side_ru(cond.get("side"))
        status = cond.get("status")
        kind = cond.get("kind") or "BOS"
        item = {
            "kind": kind,
            "level": cond.get("level"),
            "level_text": level,
            "side": cond.get("side"),
            "timeframe": cond.get("timeframe") or "H1",
            "requires_close": bool(cond.get("requires_close", True)),
            "strict": bool(cond.get("strict", True)),
            "status": status,
            "missing": cond.get("missing"),
            "opens_scenario": bool(cond.get("opens_scenario")),
            "source": cond.get("source"),
            "pivot": cond.get("pivot"),
            "anchor": cond.get("anchor"),
        }
        if status in ("unavailable", "waiting_prerequisite") and level is None:
            missing = cond.get("missing") or "нет подтверждённой опоры"
            item["text"] = f"Уровень {kind} ещё не определён: {missing}"
        elif status == "waiting_prerequisite":
            item["text"] = (
                f"Уровень {kind}: {level}. Сначала нужен подтверждённый откат; "
                f"одного закрытия {side} {level} пока недостаточно"
            )
        elif status == "ready":
            item["text"] = f"Для {kind}: закрытие H1 строго {side} {level}"
        elif status == "occurred":
            item["text"] = f"{kind} уже подтверждён на закрытии H1 {side} {level}"
        elif status == "superseded":
            item["text"] = f"Уровень {kind} {level} больше не открывает сценарий: опорный уровень уже пробит"
        else:
            item["text"] = cond.get("missing") or "условие не готово"
        out.append(item)
    if conditions.get("either"):
        out.append({
            "kind": "either",
            "status": "ready",
            "opens_scenario": True,
            "text": "Сценарий откроет любое из двух: первичный BOS или первичный SMS",
        })
    return out


def select_headline(facts: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Приоритет F08. Проблема только котировки заголовок стадии не подменяет."""
    inc = facts.get("inconsistent") or None
    if inc and inc.get("blocks"):
        return "C18", {
            "scope": inc.get("scope") or "зоны",
            "gap": inc.get("gap") or "данные о движении расходятся с сохранённым состоянием",
            "job": inc.get("job") or "расчёт в очереди. Запустить сверку",
        }
    engine = facts.get("engine") or {}
    reason = (facts.get("data_state") or {}).get("reason")
    if engine.get("replaying") or reason == "replay_in_progress":
        return "Q06", {"time": facts.get("consistent_at") or "—"}
    if engine.get("ltf_enabled") is False or engine.get("ltf_analyze") is False:
        scope = "глобально" if engine.get("ltf_enabled") is False else "для инструмента"
        return "Q05", {"scope": scope, "time": facts.get("consistent_at") or "—"}
    if reason == "no_h1_candles":
        return "Q02", {"reason": "загрузка свечей H1 ещё не выполнена"}
    if reason == "history_gap":
        return "Q04", {
            "timeframe": "H1",
            "interval": "между соседними свечами",
            "recovery": "ждём непрерывную историю",
        }
    code, params = _stage_headline(facts)
    if reason == "processing_lag" and code in {"H01", "H02", "H03", "C04"}:
        return "Q03", {
            "processed": facts.get("processed_text") or "—",
            "available": facts.get("available_text") or "—",
        }
    return code, params


def _ready_level_text(cond: dict[str, Any]) -> str:
    if cond.get("level") is None:
        return "—"
    return _level_text(_px(cond.get("level")))


def _stage_headline(facts: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if facts.get("scenario_cancelled"):
        info = facts["scenario_cancelled"]
        return "H13", {
            "direction": info.get("direction") or "—",
            "reason": info.get("reason") or "причина записана машиной",
            "time": info.get("time") or "—",
            "condition": info.get("condition") or "условие отмены записано",
        }
    if not facts.get("selected"):
        if facts.get("recent_fill"):
            fill = facts["recent_fill"]
            return "C11", {
                "time": fill.get("time") or "—",
                "next": fill.get("next") or "подтверждённого контекста сейчас нет",
            }
        return _no_context(facts)
    if facts.get("parent_ended"):
        return "H15", {"scenario": facts["parent_ended"].get("scenario") or "сценарий обновляется"}
    scenario = facts.get("scenario") or None
    if scenario:
        if not scenario.get("has_range"):
            if scenario.get("provisional_only"):
                return "H09", {}
            return "H08", {"missing": scenario.get("missing") or "опора диапазона"}
        if scenario.get("price_in_entry"):
            return "H11", {
                "zone": scenario.get("entry_zone") or "зона входа",
                "time": scenario.get("touch_time") or "—",
            }
        if int(scenario.get("eligible") or 0) > 0:
            return "H10", {
                "zone": scenario.get("entry_zone") or "вход",
                "direction": scenario.get("direction_ru") or "—",
                "cancel": scenario.get("cancel") or "условие отмены отдельно",
            }
        return "H12", {
            "reason": scenario.get("no_zone_reason") or "зоны не прошли текущий фильтр",
        }
    touch = facts.get("touch") or None
    if touch and touch.get("klass") == "wick":
        return "H04", {
            "close_side": touch.get("close_side") or "по прежнюю сторону",
            "level": touch.get("level") or "—",
        }
    if touch and touch.get("klass") == "forming":
        return "H05", {}
    if touch and touch.get("klass") == "equal":
        return "H06", {
            "level": touch.get("level") or "—",
            "side": touch.get("side") or "за уровнем",
        }
    conds = facts.get("conditions") or {}
    bos = conds.get("bos") or {}
    sms = conds.get("sms") or {}
    occurred = bos if bos.get("status") == "occurred" else (
        sms if sms.get("status") == "occurred" else None
    )
    if occurred is not None:
        bull = (facts.get("direction") == "bull")
        return "H07", {
            "kind": occurred.get("kind") or "BOS",
            "side": "вверх" if bull else "вниз",
            "close_side": "выше" if bull else "ниже",
            "level": _ready_level_text(occurred),
            "time": facts.get("occurred_time") or "—",
            "next": "сценарий по этому слому ещё не открыт",
        }
    ready = [c for c in (bos, sms) if c.get("status") == "ready" and c.get("level") is not None]
    if ready:
        bull = facts.get("direction") != "bear"
        kind = "BOS или SMS" if len(ready) == 2 else ready[0].get("kind")
        return ("H01" if bull else "H02"), {
            "kind": kind,
            "level": _ready_level_text(ready[0]),
        }
    missing = bos.get("missing") or sms.get("missing") or "нет подтверждённой опоры"
    return "H03", {"missing": missing}


def _no_context(facts: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    code = facts.get("wait_code")
    if code == "context_type_disabled":
        zone = facts.get("disabled_zone") or {}
        return "C07", {
            "type": zone.get("type") or "зона",
            "timeframe": zone.get("timeframe") or "—",
        }
    if code == "awaiting_contact":
        return "C02", {
            "zone": facts.get("await_zone") or "допустимый контекст",
            "side": facts.get("await_side") or "вне зоны",
            "price": facts.get("price_text") or "—",
            "time": facts.get("quote_time") or "—",
        }
    if code == "observation_pending":
        return "C04", {"time": facts.get("contact_time") or "—"}
    if code == "structure_pending":
        return "Q03", {
            "processed": facts.get("processed_text") or "—",
            "available": facts.get("available_text") or "—",
        }
    return "C01", {
        "types": facts.get("allowed_types") or "OB, FVG",
        "timeframes": facts.get("allowed_tfs") or "D1, W1",
    }


def assemble(facts: dict[str, Any]) -> dict[str, Any]:
    """Согласованный блок presentation из уже собранных фактов."""
    code, params = select_headline(facts)
    headline = render(code, params)
    headline["evidence_refs"] = list(facts.get("evidence_refs") or [])
    zone = facts.get("zone")
    price = facts.get("price")
    quote_ok = bool(facts.get("quote_ok"))
    lower = zone.get("lower") if zone else None
    upper = zone.get("upper") if zone else None
    relation = _relation(price, lower, upper, quote_ok)
    price_fmt = _px(price)
    location_text = _location_sentence(relation, price_fmt["text"] if price is not None else "—")
    if facts.get("location_suppressed"):
        relation = "unknown"
        location_text = "Положение цены обновляется"
    context = None
    if zone and facts.get("selected"):
        lo = _bound_text(_px(lower))
        hi = _bound_text(_px(upper))
        direction = zone.get("direction") or facts.get("direction")
        basis = BASIS_RU.get(facts.get("basis") or "", facts.get("basis") or "—")
        context = {
            "zone_id": zone.get("id"),
            "type": zone.get("type"),
            "timeframe": zone.get("timeframe"),
            "direction": direction,
            "arrow": _arrow(direction),
            "state": zone.get("status"),
            "selection_reason": facts.get("basis"),
            "lower": lower,
            "upper": upper,
            "lower_text": lo,
            "upper_text": hi,
            "exact_lower": _px(lower)["exact"],
            "exact_upper": _px(upper)["exact"],
            "text": (
                f"{str(zone.get('type') or '').upper()} {zone.get('timeframe') or ''} "
                f"{_arrow(direction)} {lo}–{hi}. Выбран по основанию: {basis}."
            ).strip(),
        }
    notes = [render(n["code"], n.get("params")) for n in (facts.get("notes") or [])]
    data_issues = []
    ds = facts.get("data_state") or {}
    if ds.get("reason") == "quote_stale" or ds.get("quote_stale"):
        data_issues.append(render("Q01", {
            "age": _age_ru(ds.get("quote_age_s")),
            "price": price_fmt["text"] if price is not None else "—",
            "time": _when(facts.get("quote_at")),
        }))
    if ds.get("reason") == "processing_lag" and code not in {"Q03"}:
        data_issues.append(render("Q03", {
            "processed": facts.get("processed_text") or "—",
            "available": facts.get("available_text") or "—",
        }))
    if ds.get("reason") == "h1_stale":
        data_issues.append(render("Q03", {
            "processed": facts.get("processed_text") or "—",
            "available": "свеча H1 устарела",
        }))
        data_issues[-1]["headline"] = "Свечи H1 устарели"
    conflict = facts.get("conflict")
    conflict_msg = None
    if conflict:
        conflict_msg = render("C16", conflict)
    review = facts.get("review") or {}
    review_msg = None
    if int(review.get("count") or 0) > 0:
        review_msg = render("Q07", {
            "asset": review.get("asset") or "Актив",
            "count": review.get("count"),
        })
    liquidity = []
    for fact in facts.get("liquidity") or []:
        outcome = fact.get("outcome")
        kind = str(fact.get("type") or "уровень").upper()
        level = _level_text(_px(fact.get("level")))
        if outcome == "returned":
            liquidity.append(render("L02", {
                "kind": kind, "level": level,
                "side": fact.get("side") or "обратно",
                "timeframe": fact.get("timeframe") or "—",
                "stage": fact.get("stage") or "—",
            }))
        elif outcome == "failed":
            liquidity.append(render("L03", {
                "kind": kind, "level": level,
                "timeframe": fact.get("timeframe") or "—",
                "side": fact.get("side") or "за уровнем",
            }))
        else:
            follow = fact.get("follow") or "Этот модуль не рассчитывает исход возврата"
            msg = render("L01", {
                "kind": kind,
                "timeframe": fact.get("timeframe") or "",
                "level": level,
                "follow": follow,
            })
            msg["event_id"] = fact.get("event_id")
            msg["occurred_at"] = fact.get("occurred_at")
            liquidity.append(msg)
    other = []
    for row in facts.get("other_zones") or []:
        msg = render(row["code"], row.get("params"))
        if row.get("detail"):
            msg["detail"] = row["detail"]
        msg["zone_id"] = row.get("zone_id")
        msg["blocking"] = bool(row.get("blocking"))
        other.append(msg)
    other.sort(key=lambda row: (not row.get("blocking"), row.get("code") or ""))
    ins = facts.get("instrument") or {}
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": f"{facts.get('instrument_id')}:{facts.get('as_of')}:{facts.get('data_version')}",
        "as_of": facts.get("as_of"),
        "instrument_id": facts.get("instrument_id"),
        "source": facts.get("source") or ins.get("venue"),
        "ruleset_id": facts.get("ruleset_id"),
        "data_version": facts.get("data_version"),
        "instrument_line": _instrument_line(ins),
        "headline": headline,
        "context": context,
        "location": {
            "relation": relation,
            "quote": price,
            "quote_text": price_fmt["text"] if price is not None else None,
            "quote_at": facts.get("quote_at"),
            "quote_version": facts.get("quote_at"),
            "text": location_text,
        },
        "structure": {
            "state": (facts.get("selected") or {}).get("state") if facts.get("selected") else None,
            "processed_through": facts.get("processed_at"),
            "text": "" if code in _HOLD_STRUCTURE else _structure_text(facts, code),
        },
        "next_conditions": [] if code in _HOLD_STRUCTURE else condition_texts(facts.get("conditions")),
        "termination_conditions": _termination(facts),
        "other_zones": other,
        "other_zones_more": max(0, len(other) - 2),
        "liquidity_facts": liquidity[:3],
        "liquidity_more": max(0, len(liquidity) - 3),
        "data_issues": data_issues,
        "notes": notes,
        "conflict": conflict_msg,
        "review": {
            "count": int(review.get("count") or 0),
            "scope": "instrument",
            "asset": review.get("asset"),
            "text": review_msg["headline"] if review_msg else None,
            "detail": review_msg["detail"] if review_msg else None,
            "href": "#review",
        },
        "actions": _presentation_actions(facts),
    }


def _structure_text(facts: dict[str, Any], code: str) -> str:
    headline = render(code, {})["headline"] if False else ""
    del headline
    conds = condition_texts(facts.get("conditions"))
    ready = [c["text"] for c in conds if c.get("status") == "ready" or c.get("kind") == "either"]
    if ready:
        return " ".join(ready)
    pending = [c["text"] for c in conds if c.get("text")]
    if pending:
        return pending[0]
    if code == "H03":
        return render("H03", {"missing": (facts.get("conditions") or {}).get("bos", {}).get("missing") or "нет подтверждённой опоры"})["detail"]
    return ""


def _termination(facts: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    scenario = facts.get("scenario")
    cancel = facts.get("cancel") or {}
    if scenario:
        if cancel.get("level") is None or cancel.get("status") in (None, "undefined"):
            lines.append(render("H14", {
                "missing": "опора обратного слома ещё не подтверждена",
            }))
        else:
            side = _side_ru(cancel.get("side"))
            level = _level_text(_px(cancel.get("level")))
            verb = "Отмена произошла" if cancel.get("status") == "occurred" else "Отменит сценарий"
            lines.append({
                "code": "cancel",
                "headline": f"{verb}: закрытие H1 строго {side} {level}",
                "detail": cancel.get("kind") or "",
                "params": {"level": cancel.get("level"), "side": cancel.get("side")},
            })
    zone = facts.get("zone")
    if zone and facts.get("selected"):
        kind = str(zone.get("type") or "").lower()
        if kind == "fvg":
            lines.append({
                "code": "parent_fvg",
                "headline": "Контекст завершится при полном заполнении",
                "detail": "Закрытие старшего таймфрейма для заполнения FVG не требуется",
                "params": {},
            })
        else:
            bull = zone.get("direction") != "bear"
            edge = zone.get("lower") if bull else zone.get("upper")
            side = "ниже" if bull else "выше"
            lines.append({
                "code": "parent_ob",
                "headline": (
                    f"Контекст завершится при закрытии {zone.get('timeframe') or 'ТФ'} "
                    f"{side} {_level_text(_px(edge))}"
                ),
                "detail": "Выход цены за границу без закрытия свечи этого таймфрейма зону не завершает",
                "params": {},
            })
    return lines


def _zone_fact(zone) -> dict[str, Any]:
    return {
        "id": zone.id,
        "type": context_type_name(zone).lower() if context_type_name(zone) in ("OB", "FVG") else zone.type.value,
        "timeframe": zone.timeframe,
        "direction": zone.direction.value,
        "lower": zone.lower,
        "upper": zone.upper,
        "status": zone.status.value if hasattr(zone.status, "value") else zone.status,
        "end_reason": zone.end_reason,
        "display_until": zone.display_until,
        "source": zone.source,
        "confirmed_at": zone.confirmed_at,
        "max_test_depth": zone.max_test_depth,
    }


def _other_zone_row(zone, price: Optional[float], quote_ok: bool, now: int) -> Optional[dict[str, Any]]:
    when = _when(zone.display_until)
    reason = end_reason_text(zone.end_reason, when)
    inside = (
        quote_ok and price is not None and zone.lower <= price <= zone.upper
    )
    ended = zone.display_until is not None or zone.status not in (
        ZoneStatus.ACTIVE, ZoneStatus.WEAKENED, ZoneStatus.CANDIDATE,
    )
    if zone.type == ZoneType.FVG and zone.display_until is not None and "fvg_filled" in (zone.end_reason or ""):
        return {
            "code": "C11",
            "zone_id": zone.id,
            "blocking": False,
            "params": {
                "time": when,
                "next": "наблюдение по этой зоне закрыто",
            },
        }
    if ended and zone.source == "manual":
        detail = None
        if inside:
            code = "C15"
            params = {"time": when}
            if reason is None:
                detail = (
                    f"Она завершена {when}; прежний контекст не возобновляется. "
                    "Причина завершения не записана"
                )
            else:
                detail = (
                    f"Она завершена {when}; прежний контекст не возобновляется. {reason}"
                )
        elif reason is None:
            code = "C14"
            params = {}
            detail = f"Ручная зона завершена {when}. Причина завершения не записана; зона показана в истории"
        else:
            code = "C13"
            params = {"reason": reason}
            detail = None
        row = {"code": code, "zone_id": zone.id, "blocking": False, "params": params}
        if detail:
            row["detail"] = detail
        if inside or zone.display_until is not None:
            return row
    return None


def _classify_touch(candles, cond: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not cond or cond.get("status") != "ready" or cond.get("level") is None or not candles:
        return None
    level = float(cond["level"])
    bear = cond.get("side") == "below"
    last = max(candles, key=lambda c: c.open_time)
    level_text = _level_text(_px(level))
    if not last.closed:
        beyond = (last.low < level) if bear else (last.high > level)
        if beyond:
            return {"klass": "forming", "level": level_text, "side": _side_ru(cond.get("side"))}
        return None
    if last.close == level:
        return {
            "klass": "equal",
            "level": level_text,
            "side": _side_ru(cond.get("side")),
        }
    if bear:
        wicked = last.low < level and last.close >= level
        close_side = "по прежнюю сторону" if last.close > level else "на уровне"
    else:
        wicked = last.high > level and last.close <= level
        close_side = "по прежнюю сторону" if last.close < level else "на уровне"
    if wicked:
        return {"klass": "wick", "level": level_text, "close_side": close_side, "side": _side_ru(cond.get("side"))}
    return None


def _quote_ok(data_state: dict[str, Any]) -> bool:
    channels = (data_state or {}).get("channels") or {}
    quote = channels.get("quote")
    if quote is not None:
        return quote.get("status") == "ok"
    return (data_state or {}).get("state") == "ok"


def build_presentation(
    db, settings, *,
    instrument: Optional[dict[str, Any]],
    observations,
    selected,
    basis: Optional[str],
    scenario,
    stage: str,
    direction: Optional[str],
    price: Optional[float],
    quote_at: Optional[int],
    data_state: dict[str, Any],
    now: int,
    review_count: int,
    cancel: Optional[dict[str, Any]],
    price_in_entry: bool,
    eligible_count: int,
    has_range: bool,
    provisional_only: bool,
    entry_zone: Optional[str],
    contexts: list,
    ruleset_id: str,
    data_version: Any,
    wait: Optional[dict[str, Any]],
    engine: dict[str, Any],
) -> dict[str, Any]:
    """Факты выбранного инструмента и блок presentation."""
    quote_ok = _quote_ok(data_state)
    policy = settings.detector
    zone = db.get_zone(selected.zone_id) if selected is not None else None
    zone_fact = _zone_fact(zone) if zone is not None else None
    inconsistent = None
    if zone is not None and fvg_contradiction(db, zone, price, quote_ok):
        state = read_reconcile(db, zone.id)
        inconsistent = {
            "blocks": True,
            "zone_id": zone.id,
            "scope": _scope_name(zone_fact or {}),
            "gap": "зона сохранена действующей, но движение требует сверки заполнения",
            "job": _job_text(state),
            "action": _reconcile_action(zone.id, state),
        }
    conditions = None
    candles = []
    if selected is not None:
        since = selected.activated_at - policy.ltf_history_days * 86_400_000
        pivots = [
            PivotCandidate(
                instrument_id=p.instrument_id, price=p.price, kind=p.kind,
                pivot_at=p.pivot_at, candle_open_time=p.candle_open_time,
                confirmed_at=p.confirmed_at or 0, left=p.left, right=p.right,
                state=p.state, pivot_id=p.id, role=p.role,
            )
            for p in db.list_ltf_pivots(selected.instrument_id, since_ms=since)
        ]
        candles = list(db.get_candles(selected.instrument_id, "H1", start_ms=since))
        open_last = db.last_candle(selected.instrument_id, "H1", closed_only=False)
        if open_last is not None and not open_last.closed:
            candles.append(open_last)
        conditions = expected_structure_conditions(
            pivots, [c for c in candles if c.closed], selected.direction, now, since_ms=since,
        )
    touch = None
    if conditions and inconsistent is None:
        ready = None
        for cond in (conditions.get("bos"), conditions.get("sms")):
            if cond and cond.get("status") == "ready":
                ready = cond
                break
        touch = _classify_touch(candles, ready)
    notes = []
    if zone is not None and inconsistent is None:
        depth = zone.max_test_depth
        threshold = getattr(policy, "entry_reuse_max_depth", 0.9)
        kind = context_type_name(zone)
        if kind == "FVG" and depth is not None and 0 < depth < 1:
            notes.append({"code": "C10", "params": {"depth": f"{round(depth * 100)}%"}})
        if kind == "OB" and depth is not None and depth >= threshold and zone.display_until is None:
            notes.append({
                "code": "C12",
                "params": {
                    "depth": f"{round(depth * 100)}%",
                    "threshold": f"{round(threshold * 100)}%",
                },
            })
        if kind == "OB" and zone.display_until is None:
            _note_wick(db, zone, notes)
    other = _collect_other_zones(db, instrument, zone, price, quote_ok, now, policy)
    recent_fill = None
    if selected is None:
        recent_fill = _recent_fill(db, instrument, now)
        if recent_fill and recent_fill.get("zone_id"):
            other = [row for row in other if row.get("zone_id") != recent_fill["zone_id"]]
    conflict = _conflict(contexts, selected, basis)
    asset = (instrument or {}).get("asset") or (instrument or {}).get("symbol") or "Актив"
    processed = (engine or {}).get("structure_last_processed_h1")
    available = (data_state or {}).get("h1_last_close")
    facts = {
        "instrument": instrument,
        "instrument_id": (instrument or {}).get("id"),
        "as_of": now,
        "ruleset_id": ruleset_id,
        "data_version": data_version,
        "source": (instrument or {}).get("venue"),
        "price": price,
        "quote_at": quote_at,
        "quote_ok": quote_ok,
        "data_state": data_state,
        "engine": {
            "ltf_enabled": engine.get("ltf_enabled", True),
            "ltf_analyze": engine.get("ltf_analyze", True),
            "replaying": engine.get("replaying", False),
        },
        "stage": stage,
        "direction": zone.direction.value if zone is not None else direction,
        "basis": basis,
        "selected": (
            {"id": selected.id, "state": selected.state, "zone_id": selected.zone_id}
            if selected is not None else None
        ),
        "zone": zone_fact,
        "scenario": None if scenario is None else {
            "has_range": has_range,
            "provisional_only": provisional_only,
            "price_in_entry": price_in_entry,
            "eligible": eligible_count,
            "entry_zone": entry_zone,
            "direction_ru": "рост" if getattr(scenario, "direction", None) and scenario.direction.value == "bull" else "снижение",
            "cancel": "закрытие H1 за уровнем обратной структуры",
            "no_zone_reason": "подходящие зоны не прошли фильтр",
            "missing": "опора диапазона",
        },
        "conditions": conditions,
        "touch": touch,
        "cancel": cancel,
        "other_zones": other,
        "liquidity": [],
        "review": {"count": review_count, "asset": asset},
        "inconsistent": inconsistent,
        "recent_fill": recent_fill,
        "notes": notes,
        "conflict": conflict,
        "wait_code": (wait or {}).get("code"),
        "allowed_types": ", ".join(sorted(getattr(policy, "htf_context_type_set", lambda: {"OB", "FVG"})())),
        "allowed_tfs": "D1, W1",
        "processed_text": _when(processed),
        "available_text": _when(available),
        "processed_at": processed,
        "consistent_at": _when(processed or quote_at),
        "price_text": _px(price)["text"] if price is not None else "—",
        "quote_time": _when(quote_at),
        "evidence_refs": (
            [{"kind": "zone", "id": zone.id}] if zone is not None else []
        ),
    }
    if inconsistent:
        facts["evidence_refs"].append({"kind": "reconcile", "id": zone.id})
    presentation = assemble(facts)
    presentation["liquidity_facts"] = _liquidity_messages(db, (instrument or {}).get("id"))
    return presentation


def _note_wick(db, zone, notes: list) -> None:
    candles = db.get_candles(zone.instrument_id, zone.timeframe)
    if not candles:
        return
    last = candles[-1]
    bull = zone.direction == Direction.BULL
    if bull and last.low < zone.lower and last.close >= zone.lower:
        beyond = True
        level = zone.lower
        side = "ниже"
    elif not bull and last.high > zone.upper and last.close <= zone.upper:
        beyond = True
        level = zone.upper
        side = "выше"
    else:
        beyond = False
        level = None
        side = ""
    if not beyond:
        return
    if max_depth_in_interval(zone, last.low, last.high) >= 1.0 and (
        (bull and last.close < zone.lower) or (not bull and last.close > zone.upper)
    ):
        return
    notes.append({
        "code": "C09",
        "params": {
            "timeframe": zone.timeframe,
            "side": side,
            "level": _level_text(_px(level)),
        },
    })


def _collect_other_zones(db, instrument, selected_zone, price, quote_ok, now, policy) -> list[dict[str, Any]]:
    if not instrument or instrument.get("id") is None:
        return []
    rows = []
    selected_id = selected_zone.id if selected_zone is not None else None
    zones = db.get_zones(
        instrument_id=instrument["id"],
        timeframes={"D1", "W1"},
        types=[ZoneType.OB, ZoneType.FVG, ZoneType.MANUAL],
    )
    for zone in zones:
        if selected_id is not None and zone.id == selected_id:
            continue
        if fvg_contradiction(db, zone, price, quote_ok):
            state = read_reconcile(db, zone.id)
            fact = _zone_fact(zone)
            rows.append({
                "code": "C18",
                "zone_id": zone.id,
                "blocking": True,
                "action": _reconcile_action(zone.id, state),
                "params": {
                    "scope": _scope_name(fact),
                    "gap": "зона сохранена действующей, но движение требует сверки заполнения",
                    "job": _job_text(state),
                },
            })
            continue
        decision = parent_decision(zone, policy, as_of=now)
        inside = quote_ok and price is not None and zone.lower <= price <= zone.upper
        if decision.reason == "unconfirmed" and inside:
            code, missing = _missing_ob(zone.evidence)
            if code == "C05":
                rows.append({
                    "code": "C05",
                    "zone_id": zone.id,
                    "blocking": False,
                    "params": {"timeframe": zone.timeframe},
                })
            else:
                rows.append({
                    "code": "C06",
                    "zone_id": zone.id,
                    "blocking": False,
                    "params": {"missing": missing},
                })
            continue
        if decision.reason == "type_disabled" and inside:
            rows.append({
                "code": "C07",
                "zone_id": zone.id,
                "blocking": True,
                "params": {"type": context_type_name(zone), "timeframe": zone.timeframe},
            })
            continue
        ended = _other_zone_row(zone, price, quote_ok, now)
        if ended is not None and (inside or zone.source == "manual"):
            rows.append(ended)
    return rows


def _recent_fill(db, instrument, now: int) -> Optional[dict[str, Any]]:
    if not instrument or instrument.get("id") is None:
        return None
    zones = db.get_zones(
        instrument_id=instrument["id"],
        timeframes={"D1", "W1"},
        types=[ZoneType.FVG],
    )
    filled = [
        z for z in zones
        if z.display_until is not None and "fvg_filled" in (z.end_reason or "")
        and (z.display_until <= now)
    ]
    if not filled:
        return None
    zone = max(filled, key=lambda z: z.display_until or 0)
    if not fvg_has_fill_event(db, zone) and "fvg_filled" not in (zone.end_reason or ""):
        return None
    return {
        "time": _when(zone.display_until),
        "next": "подтверждённого контекста сейчас нет",
        "zone_id": zone.id,
    }


def _conflict(contexts, selected, basis: Optional[str]) -> Optional[dict[str, str]]:
    dirs = {}
    for ctx in contexts or []:
        direction = ctx.get("direction")
        if direction:
            dirs.setdefault(direction, ctx)
    if "bull" not in dirs or "bear" not in dirs:
        return None
    chosen = "выбранный контекст"
    other = "альтернативный контекст"
    if selected is not None:
        chosen_dir = selected.direction.value
        chosen = "контекст вверх" if chosen_dir == "bull" else "контекст вниз"
        other_dir = "bear" if chosen_dir == "bull" else "bull"
        other_ctx = dirs.get(other_dir) or {}
        other = "контекст вниз" if other_dir == "bear" else "контекст вверх"
        stage = (other_ctx.get("state") or "активен")
    else:
        stage = "активен"
    return {
        "chosen": chosen,
        "reason": BASIS_RU.get(basis or "", basis or "навигация"),
        "other": other,
        "stage": stage,
    }


def _liquidity_messages(db, instrument_id: Optional[int]) -> list[dict[str, Any]]:
    if instrument_id is None:
        return []
    from .overview import _liquidity_facts

    rows = []
    for fact in _liquidity_facts(db, instrument_id, limit=3):
        msg = render("L01", {
            "kind": str(fact.get("type") or "уровень").upper(),
            "timeframe": fact.get("timeframe") or "",
            "level": _level_text(_px(fact.get("level"))),
            "follow": "Этот модуль не рассчитывает исход возврата",
        })
        msg["event_id"] = fact.get("event_id")
        msg["occurred_at"] = fact.get("occurred_at")
        msg["creates_scenario"] = False
        rows.append(msg)
    return rows


def legacy_expected(conditions: dict[str, Any], direction: str) -> dict[str, Any]:
    """Слот графика и бота: готовый уровень и уровень, который ждёт предпосылку.

    Свершившийся, снятый и неизвестный уровень в слот не попадают.
    """
    def slot(cond: Optional[dict[str, Any]], kind: str) -> Optional[dict[str, Any]]:
        if not cond:
            return None
        status = cond.get("status")
        level = cond.get("level")
        if status in ("occurred", "superseded", "unavailable") or level is None:
            return None
        if kind == "bos":
            return {
                "direction": direction,
                "level": level,
                "ref_pivot": cond.get("pivot"),
                "status": status,
            }
        return {
            "level": level,
            "internal_pivot": cond.get("pivot"),
            "status": status,
            "prerequisite": cond.get("missing"),
        }
    return {
        "bos": slot(conditions.get("bos"), "bos"),
        "sms": slot(conditions.get("sms"), "sms"),
    }
