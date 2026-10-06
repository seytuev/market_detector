"""Текстовые карточки бота поверх read model app/services/overview.py."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from ..db import Database
from ..models import Zone, ZoneStatus, now_ms
from ..notify.telegram import _fmt_price, _fmt_time
from ..services.overview import (
    STAGE_DATA_PENDING,
    STAGE_IN_ENTRY,
    STAGE_NO_ZONES,
    STAGE_RETRACEMENT,
    STAGE_WAIT_BOS,
    STAGE_WAIT_HTF,
    STAGE_WAIT_RANGE,
    instrument_current,
    instruments_overview,
    service_status,
)
from ..texts_ru import (
    DIRECTION_RU,
    KIND_RU,
    LTF_CANCELLATION_RU,
    LTF_EVENT_KIND_RU,
    LTF_SCENARIO_STATE_RU,
    STATUS_RU,
    TYPE_RU,
)
from ..web.ltf_api import _expected_levels

# Порядок этапов в /now: самые «горячие» сверху. Этап возврата в ответе
# динамический («Возврат в Premium/Discount») — распознаётся по префиксу.
STAGE_RANK = {
    STAGE_IN_ENTRY: 0,
    STAGE_RETRACEMENT: 1,
    STAGE_WAIT_BOS: 2,
    STAGE_WAIT_RANGE: 3,
}
_RETRACEMENT_PREFIX = STAGE_RETRACEMENT.split("/")[0]  # «Возврат в »
_RANK_OTHER = 4

# Лимит текста Telegram — 4096; держим запас (как TELEGRAM_TEXT_LIMIT
# в app/notify/ltf_templates.py)
TEXT_LIMIT = 4000

# «Активные» HTF-зоны для /htf (ослабленная FVG всё ещё рыночно значима)
_ACTIVE_ZONE_STATUSES = [ZoneStatus.ACTIVE, ZoneStatus.WEAKENED]

# Следующий ожидаемый шаг по этапу (§4.2)
_NEXT_STEP = {
    STAGE_WAIT_HTF: "ждём касания HTF-зоны",
    STAGE_WAIT_BOS: "ждём слома структуры BOS/SMS на H1",
    STAGE_WAIT_RANGE: "ждём диапазона Premium/Discount",
    STAGE_IN_ENTRY: "цена в зоне входа — оцените сценарий",
    STAGE_NO_ZONES: "подходящих зон входа нет",
    STAGE_DATA_PENDING: "ждём данные (котировки/свечи)",
}

_POSITION_RU = {"inside": "внутри", "above": "выше", "below": "ниже"}


def stage_rank(stage: str) -> int:
    if stage.startswith(_RETRACEMENT_PREFIX):
        return STAGE_RANK[STAGE_RETRACEMENT]
    return STAGE_RANK.get(stage, _RANK_OTHER)


def split_long(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Разбивка длинного сообщения по границам строк (как _split в
    ltf_templates): ни одна строка не теряется."""
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    """Первая порция текста — для edit_message_text (одно сообщение)."""
    return split_long(text, limit)[0]


def _fmt_hhmm(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%H:%M")


def _next_step(stage: str) -> str:
    if stage.startswith(_RETRACEMENT_PREFIX):
        return "ждём возврата цены в рабочую половину диапазона"
    return _NEXT_STEP.get(stage, stage)


def _distance_to_boundary(lower: float, upper: float, price: float) -> float:
    """Расстояние до ближайшей границы (внутри — до ближайшей из двух)."""
    if price > upper:
        return price - upper
    if price < lower:
        return lower - price
    return min(price - lower, upper - price)


def _type_ru(type_value: str) -> str:
    return TYPE_RU.get(type_value, type_value)


def _dir_ru(direction: str) -> str:
    return DIRECTION_RU.get(direction, direction)


def render_now(db: Database, settings, chat_id: Optional[str] = None) -> str:
    """Компактный список активов: «СИМВОЛ · биржа — этап; HTF-контекст;
    зон входа: N; обновлено HH:MM». Сортировка по этапу (STAGE_RANK),
    внутри этапа — по символу. Время — max(quote ts, last_event_at).
    chat_id — watchlist владельца бота (ТЗ п.8): непустой список сужает
    выдачу до его активов; пустой/None — все (fallback, как до /start)."""
    rows = instruments_overview(db, settings)
    if chat_id:
        wl_ids = {w["instrument_id"] for w in db.list_watchlist(chat_id)}
        if wl_ids:
            rows = [r for r in rows if r["instrument"]["id"] in wl_ids]
    if not rows:
        return "Пока нет активов на анализе. Добавьте инструменты в приложении."
    quotes = db.get_all_quotes()

    def line(r: dict) -> str:
        ins = r["instrument"]
        parts = [f"{ins['symbol']} · {ins['venue']} — {r['stage']}"]
        ctx = r.get("htf_context")
        if ctx:
            parts.append(f"HTF: {ctx['type']} {ctx['timeframe']}")
        parts.append(f"зон входа: {r['eligible_count']}")
        quote_ts: Optional[int] = None
        quote = quotes.get(ins["id"])
        if quote:
            quote_ts = quote[1]
        marks = [t for t in (quote_ts, r.get("last_event_at")) if t]
        if marks:
            parts.append(f"обновлено {_fmt_hhmm(max(marks))}")
        return "; ".join(parts)

    rows = sorted(
        rows,
        key=lambda r: (stage_rank(r["stage"]), r["instrument"]["symbol"]),
    )
    return "Сейчас по активам:\n" + "\n".join(line(r) for r in rows)


# --------------------------------------------------------------------- #
# /asset — карточка актива
# --------------------------------------------------------------------- #

def _last_structure_event(db: Database, scenario_id: int):
    events = db.list_ltf_structure_events(scenario_id)
    if not events:
        return None
    return max(events, key=lambda e: (e.occurred_at, e.id or 0))


def render_asset(db: Database, settings, instrument_id: int) -> str:
    """Карточка актива: цена и её положение в HTF-зонах, этап и следующий
    шаг, текущий сценарий H1, подходящие зоны входа (до 5). Бычий и
    медвежий HTF-контексты — отдельными блоками, не единым сигналом."""
    cur = instrument_current(db, settings, instrument_id)
    if cur is None:
        return "Инструмент не найден."
    ins = cur["instrument"]
    lines = [f"{ins['symbol']} · {ins['venue']} · {ins['market_type']}"]
    price = cur["price"]
    if price is not None:
        lines.append(
            f"Цена: {_fmt_price(price)} (котировка {_fmt_time(cur['quote_at'])})"
        )
    else:
        lines.append("Цена: нет котировки.")
    lines.append(f"Этап: {cur['stage']}. Следующий шаг: {_next_step(cur['stage'])}.")

    if not cur["contexts"]:
        lines.append("HTF-контекстов пока нет.")
    for ctx in cur["contexts"]:
        head = f"HTF-контекст ({_dir_ru(ctx['direction'])})"
        zone = ctx.get("parent_zone")
        if zone is None:
            lines.append(f"{head}: зона недоступна.")
            continue
        block = (
            f"{head}: {_type_ru(zone['type'])} {zone['timeframe']} "
            f"{_fmt_price(zone['lower'])}–{_fmt_price(zone['upper'])}"
        )
        pos = ctx.get("price_position")
        if pos and price is not None:
            block += f" — цена {_POSITION_RU.get(pos, pos)} зоны"
            dist = _distance_to_boundary(zone["lower"], zone["upper"], price)
            block += f", до границы {_fmt_price(dist)}"
        lines.append(block + ".")

    sc = cur["current_scenario"]
    if sc is not None:
        state_ru = LTF_SCENARIO_STATE_RU.get(sc["state"], sc["state"])
        lines.append(
            f"Сценарий H1: {sc['trigger']}, {_dir_ru(sc['direction'])}, {state_ru}."
        )
        ev = _last_structure_event(db, sc["id"])
        if ev is not None:
            lines.append(
                f"Последнее подтверждённое событие: {ev.kind}, уровень "
                f"{_fmt_price(ev.break_level)} ({_fmt_time(ev.occurred_at)})."
            )
    elif cur.get("scenario_waiting"):
        lines.append("Сценарий H1: ожидание нового сценария.")

    entries = cur["eligible_entries"]
    if entries:
        lines.append("Подходящие зоны входа:")
        for e in entries[:5]:
            lines.append(
                f"• {e['type']} {_fmt_price(e['lower'])}–{_fmt_price(e['upper'])}"
            )
    return "\n".join(lines)


# --------------------------------------------------------------------- #
# /htf — зоны инструмента
# --------------------------------------------------------------------- #

def _group_by_price(
    zones: list[Zone], price: Optional[float]
) -> tuple[list[Zone], list[Zone], list[Zone]]:
    """(внутри, ближайшие сверху, ближайшие снизу) относительно цены."""
    if price is None:
        return [], [], sorted(zones, key=lambda z: (z.timeframe, z.lower))
    inside = [z for z in zones if z.lower <= price <= z.upper]
    above = sorted(
        (z for z in zones if z.lower > price), key=lambda z: z.lower - price
    )
    below = sorted(
        (z for z in zones if z.upper < price), key=lambda z: price - z.upper
    )
    return inside, above, below


def _zone_line(db: Database, z: Zone, price: Optional[float]) -> str:
    parts = [
        f"• {_type_ru(z.type.value)} {z.timeframe} ({_dir_ru(z.direction.value)}): "
        f"{_fmt_price(z.lower)}–{_fmt_price(z.upper)}, 50% {_fmt_price(z.mid)}"
    ]
    parts.append(f"статус: {STATUS_RU.get(z.status.value, z.status.value)}")
    if z.has_tests:
        parts.append(f"макс. глубина теста {z.max_test_depth:.0%}")
        visits = db.get_visits(z.id, z.cycle_id)
        if visits:
            parts.append(f"последний тест {_fmt_time(visits[-1].entered_at)}")
    if price is not None:
        dist = _distance_to_boundary(z.lower, z.upper, price)
        parts.append(f"до границы {_fmt_price(dist)}")
    return "; ".join(parts) + "."


def render_htf(
    db: Database, settings, instrument_id: int, filt: str = "all"
) -> str:
    """Активные HTF-зоны инструмента. Порядок: цена внутри → ближайшие
    сверху → ближайшие снизу. Фильтры: d1 | w1 | all | inside | near."""
    ins = db.get_instrument(instrument_id)
    if ins is None:
        return "Инструмент не найден."
    zones = db.get_zones(
        instrument_id=instrument_id, statuses=_ACTIVE_ZONE_STATUSES
    )
    # ТЗ 06.10.2026 §13 (T21): единый canonical state — invalidated и
    # неподтверждённые не показываем как актуальные даже при плоском status
    zones = [z for z in zones if z.is_currently_relevant()]
    quote = db.get_quote(instrument_id)
    price = quote[0] if quote else None
    if filt in ("d1", "w1"):
        zones = [z for z in zones if z.timeframe == filt.upper()]
    inside, above, below = _group_by_price(zones, price)
    if filt == "inside":
        ordered = inside
    elif filt == "near":
        ordered = sorted(
            zones,
            key=lambda z: (
                _distance_to_boundary(z.lower, z.upper, price)
                if price is not None else 0.0
            ),
        )[:5]
    else:
        ordered = inside + above + below
    head = f"HTF-зоны: {ins.symbol} · {ins.venue} · {ins.market_type}"
    if price is not None:
        head += f"\nЦена: {_fmt_price(price)}"
    if not ordered:
        return head + "\nПодходящих зон нет."
    return "\n".join([head] + [_zone_line(db, z, price) for z in ordered])


def render_zone(db: Database, settings, zone_id: int) -> str:
    """Карточка одной HTF-зоны."""
    z = db.get_zone(zone_id)
    if z is None:
        return "Зона не найдена."
    ins = db.get_instrument(z.instrument_id)
    label = (
        f"{_type_ru(z.type.value)} {z.timeframe} ({_dir_ru(z.direction.value)})"
    )
    if ins is not None:
        label += f" — {ins.symbol} · {ins.venue} · {ins.market_type}"
    lines = [
        label,
        f"Границы: {_fmt_price(z.lower)}–{_fmt_price(z.upper)}, "
        f"50% {_fmt_price(z.mid)}.",
        f"Статус: {STATUS_RU.get(z.status.value, z.status.value)}.",
    ]
    quote = db.get_quote(z.instrument_id)
    if quote:
        price = quote[0]
        if z.lower <= price <= z.upper:
            pos = "внутри"
        else:
            pos = "выше" if price > z.upper else "ниже"
        dist = _distance_to_boundary(z.lower, z.upper, price)
        lines.append(
            f"Цена {_fmt_price(price)} — {pos} зоны, "
            f"до границы {_fmt_price(dist)}."
        )
    if z.has_tests:
        lines.append(f"Максимальная глубина теста: {z.max_test_depth:.0%}.")
        visits = db.get_visits(z.id, z.cycle_id)
        if visits:
            lines.append(f"Последний тест: {_fmt_time(visits[-1].entered_at)}.")
    else:
        lines.append("Тестов ещё не было.")
    return "\n".join(lines)


def render_zone_tests(db: Database, zone_id: int) -> str:
    """История тестов зоны (визиты, последние 5)."""
    z = db.get_zone(zone_id)
    if z is None:
        return "Зона не найдена."
    head = (
        f"История тестов: {_type_ru(z.type.value)} {z.timeframe} "
        f"{_fmt_price(z.lower)}–{_fmt_price(z.upper)}"
    )
    visits = db.get_visits(z.id, z.cycle_id)
    if not visits:
        return head + "\nТестов пока не было."
    lines = [head]
    for v in visits[-5:][::-1]:
        lines.append(
            f"• {_fmt_time(v.entered_at)} — глубина {v.max_depth:.0%}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------- #
# /ltf — сценарий H1
# --------------------------------------------------------------------- #

def _selected_context(cur: dict[str, Any]) -> Optional[dict[str, Any]]:
    return next(
        (c for c in cur["contexts"]
         if c["observation_id"] == cur["selected_context_id"]),
        None,
    )


def _no_entries_reason(cur: dict[str, Any]) -> str:
    ds = cur["data_state"]
    if ds["state"] != "ok":
        return f"данные неактуальны ({ds['state']})"
    if cur["current_scenario"] is None:
        return "сценарий ещё не подтверждён (ожидание BOS/SMS)"
    if cur["range"] is None:
        return "диапазон Premium/Discount ещё не готов"
    excluded = cur["counts"]["excluded"]
    if excluded:
        return f"исключено правилами: {excluded} (причины — в приложении)"
    return cur["stage"]


def render_ltf(db: Database, settings, instrument_id: int) -> str:
    """Сценарий H1 по выбранному контексту: родительская HTF-зона,
    подтверждённый ЛИБО ожидаемый BOS/SMS (подписи различаются), опоры
    диапазона и середина, подходящие зоны входа либо причина их отсутствия."""
    cur = instrument_current(db, settings, instrument_id)
    if cur is None:
        return "Инструмент не найден."
    ins = cur["instrument"]
    lines = [f"LTF: {ins['symbol']} · {ins['venue']} · {ins['market_type']}"]

    ctx = _selected_context(cur)
    if ctx is not None and ctx.get("parent_zone"):
        pz = ctx["parent_zone"]
        lines.append(
            f"HTF-зона контекста: {_type_ru(pz['type'])} {pz['timeframe']} "
            f"({_dir_ru(ctx['direction'])}) "
            f"{_fmt_price(pz['lower'])}–{_fmt_price(pz['upper'])}."
        )
    else:
        lines.append("HTF-контекст не выбран — ждём касания HTF-зоны.")

    sc = cur["current_scenario"]
    if sc is not None and sc.get("break_level") is not None:
        lines.append(
            f"{sc['trigger']} подтверждён: уровень {_fmt_price(sc['break_level'])}."
        )
        if sc.get("break_candle_open_time"):
            lines.append(
                f"Подтверждение: свеча {_fmt_time(sc['break_candle_open_time'])}."
            )
    else:
        expected = {"bos": None, "sms": None}
        if cur["selected_context_id"] is not None:
            obs = db.get_ltf_observation(cur["selected_context_id"])
            if obs is not None:
                expected = _expected_levels(db, obs, settings)
        bos = expected.get("bos")
        if bos:
            side = "ниже" if bos["direction"] == "bear" else "выше"
            lines.append(
                f"Ожидаемый BOS: закрытие H1 {side} {_fmt_price(bos['level'])}."
            )
        elif ctx is not None:
            lines.append("BOS/SMS ещё не подтверждён — якорной структуры нет.")

    rng = cur["range"]
    if rng is not None:
        lines.append(
            f"Диапазон Premium/Discount: {_fmt_price(rng['lower'])}–"
            f"{_fmt_price(rng['upper'])}, 50% {_fmt_price(rng['mid'])}."
        )
        anchors = rng.get("anchors") or {}
        bits = []
        if anchors.get("low"):
            bits.append(f"low {_fmt_price(anchors['low']['price'])}")
        if anchors.get("high"):
            bits.append(f"high {_fmt_price(anchors['high']['price'])}")
        if bits:
            lines.append("Опоры диапазона: " + ", ".join(bits) + ".")

    entries = cur["eligible_entries"]
    if entries:
        lines.append("Подходящие зоны входа:")
        for e in entries[:5]:
            lines.append(
                f"• {e['type']} {_fmt_price(e['lower'])}–{_fmt_price(e['upper'])}"
            )
    else:
        lines.append(f"Подходящих зон входа нет: {_no_entries_reason(cur)}.")
    return "\n".join(lines)


def render_entries(db: Database, settings, instrument_id: int) -> str:
    """Детальный список подходящих зон входа (eligible от движка)."""
    cur = instrument_current(db, settings, instrument_id)
    if cur is None:
        return "Инструмент не найден."
    ins = cur["instrument"]
    head = f"Зоны входа: {ins['symbol']} · {ins['venue']} · {ins['market_type']}"
    entries = cur["eligible_entries"]
    if not entries:
        return head + f"\nПодходящих зон входа нет: {_no_entries_reason(cur)}."
    lines = [head]
    for e in entries:
        row = (
            f"• {e['type']} {_fmt_price(e['lower'])}–{_fmt_price(e['upper'])}, "
            f"50% {_fmt_price(e['mid'])}"
        )
        if e.get("half"):
            row += f"; половина: {e['half']}"
        if e.get("dist_abs") is not None:
            row += f"; дистанция {_fmt_price(e['dist_abs'])} ({e['dist_pct']:.2f}%)"
        lines.append(row)
    return "\n".join(lines)


def render_why(db: Database, settings, instrument_id: int) -> str:
    """Краткое объяснение текущего сценария из данных current: выбранный
    контекст и последнее структурное событие."""
    cur = instrument_current(db, settings, instrument_id)
    if cur is None:
        return "Инструмент не найден."
    ins = cur["instrument"]
    lines = [f"Почему этот сценарий: {ins['symbol']} · {ins['venue']}"]
    ctx = _selected_context(cur)
    if ctx is not None and ctx.get("parent_zone"):
        pz = ctx["parent_zone"]
        lines.append(
            f"Контекст: {_type_ru(pz['type'])} {pz['timeframe']} "
            f"({_dir_ru(ctx['direction'])}) — цена коснулась HTF-зоны, "
            "открыто наблюдение."
        )
    else:
        lines.append("Контекст не выбран: сценария нет, ждём касания HTF-зоны.")
        return "\n".join(lines)
    sc = cur["current_scenario"]
    if sc is None:
        waiting = cur.get("scenario_waiting") or {}
        cancel = waiting.get("last_cancellation")
        if cancel:
            lines.append(
                "Предыдущий сценарий отменён — ожидаем новое подтверждение "
                "по направлению HTF-зоны."
            )
        else:
            lines.append(
                "Структурного слома ещё нет — ожидаем подтверждение BOS/SMS "
                "по направлению HTF-зоны."
            )
        return "\n".join(lines)
    ev = _last_structure_event(db, sc["id"])
    if ev is not None:
        lines.append(
            f"Последнее структурное событие: {ev.kind} — закрытие H1 за "
            f"уровнем {_fmt_price(ev.break_level)} ({_fmt_time(ev.occurred_at)})."
        )
    lines.append(
        f"Сценарий: {sc['trigger']}, {_dir_ru(sc['direction'])}, "
        f"{LTF_SCENARIO_STATE_RU.get(sc['state'], sc['state'])}."
    )
    return "\n".join(lines)


def render_contexts(db: Database, settings, instrument_id: int) -> str:
    """Список активных HTF-контекстов инструмента (выбор — кнопками)."""
    cur = instrument_current(db, settings, instrument_id)
    if cur is None:
        return "Инструмент не найден."
    ins = cur["instrument"]
    if not cur["contexts"]:
        return (
            f"Контексты: {ins['symbol']} · {ins['venue']}\n"
            "Активных контекстов нет."
        )
    lines = [f"Контексты: {ins['symbol']} · {ins['venue']}"]
    for c in cur["contexts"]:
        pz = c.get("parent_zone") or {}
        mark = " ← выбран" if c["observation_id"] == cur["selected_context_id"] else ""
        lines.append(
            f"• {_dir_ru(c['direction'])} {_type_ru(pz.get('type', '?'))} "
            f"{pz.get('timeframe', '?')} "
            f"{_fmt_price(pz['lower'])}–{_fmt_price(pz['upper'])}{mark}"
            if pz else f"• {_dir_ru(c['direction'])}{mark}"
        )
    return "\n".join(lines)


def render_chart_caption(
    cur: dict[str, Any], now: int, tf: str, days: int
) -> str:
    """Подпись к графику из УЖЕ взятого снимка instrument_current (один
    снимок на пару «картинка + подпись»): символ·биржа, цена, этап,
    «BOS подтверждён» vs «ожидаемый BOS», время расчёта = время снимка."""
    ins = cur["instrument"]
    head = f"{ins['symbol']} · {ins['venue']} · {ins['market_type']} — {tf}"
    if tf == "H1":
        head += f", {days} дн."
    lines = [head]
    if cur["price"] is not None:
        lines.append(f"Цена: {_fmt_price(cur['price'])}")
    lines.append(f"Этап: {cur['stage']}")
    sc = cur["current_scenario"]
    if sc is not None and sc.get("break_level") is not None:
        lines.append(
            f"{sc['trigger']} подтверждён: уровень {_fmt_price(sc['break_level'])}"
        )
    elif cur["selected_context_id"] is not None:
        lines.append("BOS/SMS ещё не подтверждён (ожидаемый — пунктиром)")
    lines.append(f"Расчёт: {_fmt_time(now)}")
    return "\n".join(lines)


def render_watchlist(db: Database, chat_id: str) -> str:
    """Список наблюдения владельца бота (ТЗ п.8)."""
    rows = db.list_watchlist(chat_id)
    if not rows:
        return (
            "Список наблюдения пуст. Добавьте актив кнопкой ниже "
            "или командой /add BTC."
        )
    lines = ["Список наблюдения:"]
    for w in rows:
        ins = db.get_instrument(w["instrument_id"])
        if ins is None:
            continue
        state = "вкл" if w["alerts_enabled"] else "выкл"
        lines.append(
            f"• {ins.symbol} · {ins.venue} · {ins.market_type} — "
            f"уведомления {state}"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------- #
# /status — состояние сервиса (ТЗ п.11)
# --------------------------------------------------------------------- #

def render_status(db: Database, settings) -> str:
    """Компактный статус: котировки, обработка H1, HTF-свечи, очередь
    доставки, пропуски данных. Маркеры ✅/⚠️/❌ — отличить «сетапа нет»
    от «данные не обработаны»."""
    st = service_status(db, settings)
    lines = ["Состояние сервиса:"]

    q = st["quotes"]
    if q["ok"]:
        last = _fmt_time(q["last_quote_at"]) if q["last_quote_at"] else "—"
        lines.append(f"✅ Котировки идут (последняя {last}).")
    elif q["stale"]:
        lines.append(f"⚠️ Котировки просрочены: {', '.join(q['stale'])}.")
    else:
        lines.append("❌ Котировок нет.")

    h = st["h1"]
    if h["lagging"]:
        processed = (
            _fmt_time(h["last_processed"]) if h["last_processed"] else "никогда"
        )
        lines.append(
            f"⚠️ H1 не обработаны: {', '.join(h['lagging'])} "
            f"(обработка: {processed})."
        )
    elif h["last_closed"]:
        lines.append(f"✅ H1 обработаны (последняя {_fmt_time(h['last_closed'])}).")
    else:
        lines.append("❌ Свечей H1 нет.")

    if st["htf_stale"]:
        lines.append(f"⚠️ HTF-свечи просрочены: {', '.join(st['htf_stale'])}.")
    else:
        lines.append("✅ HTF-свечи свежие (D1/W1).")

    if st["delivery_pending"]:
        lines.append(
            f"⚠️ Неотправленных уведомлений: {st['delivery_pending']}."
        )
    else:
        lines.append("✅ Очередь доставки пуста.")

    if st["data_gaps"]:
        gaps = ", ".join(f"{g['symbol']} ({g['state']})" for g in st["data_gaps"])
        lines.append(f"⚠️ Пропуски данных: {gaps}.")
    else:
        lines.append("✅ Пропусков данных нет.")
    return "\n".join(lines)


# --------------------------------------------------------------------- #
# /history — история сигналов (ТЗ п.11)
# --------------------------------------------------------------------- #

HISTORY_PERIODS = [(24, "24 часа"), (168, "7 дней"), (720, "30 дней")]
HISTORY_TYPES = [
    ("all", "Все"), ("htf", "HTF"), ("ltf", "LTF"),
    ("entry", "Вход (касания)"), ("cancel", "Отмены"),
]
_HISTORY_LTF_CORE = {
    "bos", "sms", "entries_ready", "range_ready",
    "sweep_confirmed", "sweep_failed",
}
_HISTORY_LIMIT = 15


def _ltf_history_line(ev) -> str:
    label = LTF_EVENT_KIND_RU.get(ev.kind, ev.kind)
    p = ev.payload or {}
    detail = ""
    if ev.kind in ("bos", "sms") and p.get("break_level") is not None:
        detail = f"уровень {_fmt_price(p['break_level'])}"
    elif ev.kind == "touch":
        if p.get("lower") is not None:
            detail = f"{p.get('type', '?')} {_fmt_price(p['lower'])}–{_fmt_price(p['upper'])}"
    elif ev.kind == "cancellation":
        reason = p.get("reason")
        detail = f"причина: {LTF_CANCELLATION_RU.get(reason, reason or '—')}"
    elif ev.kind in ("entries_ready", "range_ready"):
        n = len(p.get("entries") or [])
        detail = f"зон: {n}" if n else "ожидание зон"
    elif ev.kind.startswith("sweep") and p.get("level") is not None:
        detail = f"уровень {_fmt_price(p['level'])}"
    text = f"{_fmt_time(ev.occurred_at)} — {label}"
    if detail:
        text += f": {detail}"
    return text


def render_history(
    db: Database, settings, instrument_id: int,
    hours: int = 24, type: str = "all",
) -> str:
    """События актива за период (HTF + LTF, до 15 свежих): время, вид,
    краткая суть; в конце — «Сейчас: <stage>» (последующее состояние).
    type: htf | ltf | entry | cancel | all."""
    ins = db.get_instrument(instrument_id)
    if ins is None:
        return "Инструмент не найден."
    since = now_ms() - hours * 3_600_000
    items: list[tuple[int, str]] = []

    if type in ("htf", "all"):
        for ev in db.list_events_for_instrument(instrument_id, since, limit=50):
            zone = db.get_zone(ev.zone_id)
            label = KIND_RU.get(ev.kind, ev.kind.value)
            ztxt = (
                f"{_type_ru(zone.type.value)} {zone.timeframe}"
                if zone is not None else f"зона #{ev.zone_id}"
            )
            items.append((
                ev.occurred_at,
                f"{_fmt_time(ev.occurred_at)} — {label}: {ztxt}, "
                f"цена {_fmt_price(ev.price)}",
            ))

    if type == "all":
        wanted = None
    elif type == "ltf":
        wanted = _HISTORY_LTF_CORE
    elif type == "entry":
        wanted = {"touch"}
    elif type == "cancel":
        wanted = {"cancellation"}
    else:  # htf — LTF-события не включаем
        wanted = set()
    if wanted is None or wanted:
        for ev in db.list_ltf_events_for_instrument(
                instrument_id, since, limit=50):
            if wanted is not None and ev.kind not in wanted:
                continue
            items.append((ev.occurred_at, _ltf_history_line(ev)))

    period = dict(HISTORY_PERIODS).get(hours, f"{hours} ч")
    type_label = dict(HISTORY_TYPES).get(type, type)
    header = (
        f"История: {ins.symbol} · {ins.venue} · {ins.market_type} — "
        f"{period}, {type_label}"
    )
    if not items:
        body = header + "\nСобытий за период нет."
    else:
        items.sort(key=lambda it: it[0], reverse=True)
        body = "\n".join(
            [header] + [f"• {t}" for _, t in items[:_HISTORY_LIMIT]]
        )
    cur = instrument_current(db, settings, instrument_id)
    if cur is not None:
        body += f"\nСейчас: {cur['stage']}"
    return body
