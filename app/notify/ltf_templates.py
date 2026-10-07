"""Шаблоны Telegram-сообщений окна LTF (LTF-спека §11, §10, §3.3).

Тексты строятся только из реальных полей события и контекста (инструмент,
родительская HTF-зона, сценарий) — без выдуманных цен. Время и источник
обязательны в каждом сообщении. «Внутри HTF» пишется только при явном
признаке htf_position="inside" в payload; иначе — нейтральное «после
касания» (§3.3: не вводить в заблуждение при сломе после выхода из зоны).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..models import Instrument, Zone
from ..models_ltf import LtfEvent, LtfObservation, LtfScenario
from ..texts_ru import (
    DIRECTION_RU,
    LTF_CANCELLATION_RU,
    LTF_TYPE_RU,
    TYPE_RU,
)
from .formatting import fmt_pct_ru, fmt_time_msk
from .telegram import _fmt_price, _fmt_time, tradingview_url

# Лимит текста Telegram — 4096; держим запас под юникод/разметку
TELEGRAM_TEXT_LIMIT = 4000


@dataclass
class LtfContext:
    """Контекст сообщения, обогащённый диспетчером из БД (§13)."""

    instrument: Optional[Instrument]
    zone: Optional[Zone]                 # родительская HTF-зона
    observation: Optional[LtfObservation]
    scenario: Optional[LtfScenario]


def _symbol(ctx: LtfContext) -> str:
    return ctx.instrument.symbol if ctx.instrument else "инструмент ?"


def _direction_word(direction: str) -> str:
    return {"bull": "Bullish", "bear": "Bearish"}.get(direction, direction)


def _htf_context(ctx: LtfContext) -> str:
    """«FVG D1 · медвежий» — тип/ТФ/направление родителя (§5.4)."""
    z = ctx.zone
    if z is None:
        return "HTF-контекст отсутствует"
    type_ru = TYPE_RU.get(z.type.value, z.type.value)
    dir_ru = DIRECTION_RU.get(z.direction.value, z.direction.value)
    return f"{type_ru} {z.timeframe} · {dir_ru}"


def _htf_part(ctx: LtfContext) -> str:
    """«HTF Orderblock D1 (медвежий)» — для связного предложения."""
    z = ctx.zone
    if z is None:
        return "HTF-зоны"
    type_ru = TYPE_RU.get(z.type.value, z.type.value)
    dir_ru = DIRECTION_RU.get(z.direction.value, z.direction.value)
    return f"HTF {type_ru} {z.timeframe} ({dir_ru})"


def _footer(ev: LtfEvent, ctx: LtfContext) -> str:
    """ТЗ 07.10.2026 §5.1/§6: без строки «Источник» (метаданные — в карточке
    «Подробнее»), время — МСК. Ссылка TradingView живёт кнопкой, чтобы её
    превью не подменяло собственный график (§7)."""
    return f"Время события: {_fmt_time(ev.occurred_at)}"


def _render_entry_line(e: dict[str, Any]) -> str:
    """Одна зона списка (§11.1): уровень K или диапазон L–U с серединой;
    «частично» при частичном попадании в половину (§8.5)."""
    t = LTF_TYPE_RU.get(e.get("type", ""), e.get("type", "?"))
    if e.get("lower") == e.get("upper"):
        line = f"{t} H1: {_fmt_price(e['lower'])}"
    else:
        line = (
            f"{t} H1: {_fmt_price(e['lower'])}–{_fmt_price(e['upper'])}"
            f" (середина {_fmt_price(e['mid'])})"
        )
    if e.get("overlap") == "partial":
        line += " — частично"
    if e.get("outside_premium"):
        line += " — вне Premium (допущено по контексту, §18)"
    return line


def _context_lines(entries: list[dict[str, Any]], ctx: LtfContext) -> list[str]:
    """§18: аннотации контекстного допуска — после списка зон."""
    if not any(e.get("outside_premium") for e in entries):
        return []
    lines = ["⚠️ Зона вне Premium — не критично для этого сценария."]
    if any(e.get("context") for e in entries):
        bear = (
            ctx.scenario is None
            or ctx.scenario.direction.value == "bear"
        )
        lines.append(
            "Контекст: обновление лоя = снятие SSL + тест 50% D1 FVG."
            if bear else
            "Контекст: обновление хая = снятие BSL + тест 50% D1 FVG."
        )
    return lines


def _render_entries_block(entries: list[dict[str, Any]]) -> list[str]:
    """Все зоны списком, по одной строке на зону — без выбора «лучшей»."""
    return [_render_entry_line(e) for e in entries]


def _render_range(rng: Optional[dict[str, Any]]) -> Optional[str]:
    """Диапазон и середина — отдельными строками (ТЗ 07.10.2026 §5.1)."""
    if not rng:
        return None
    return (
        f"Диапазон: {_fmt_price(rng['lower'])}–{_fmt_price(rng['upper'])}\n"
        f"Середина: {_fmt_price(rng['mid'])}"
    )


def _split(head: str, entry_lines: list[str], footer: str) -> list[str]:
    """Длинный список зон режется на несколько сообщений по строкам —
    ни одна зона не пропускается (§11.1)."""
    if not entry_lines:
        return [f"{head}\n\n{footer}"]
    chunks: list[list[str]] = []
    cur: list[str] = []
    cur_len = 0
    for line in entry_lines:
        if cur and cur_len + len(line) + 1 > TELEGRAM_TEXT_LIMIT:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    chunks.append(cur)
    if len(chunks) == 1:
        text = f"{head}\n" + "\n".join(chunks[0]) + f"\n\n{footer}"
        if len(text) <= TELEGRAM_TEXT_LIMIT:
            return [text]
    messages = [f"{head}\n" + "\n".join(chunks[0])]
    for ch in chunks[1:-1]:
        messages.append("\n".join(ch))
    messages.append("\n".join(chunks[-1]) + f"\n\n{footer}")
    return messages


def _render_movement(mv: Optional[dict[str, Any]]) -> Optional[str]:
    """Строка о причинном движении слома (§8.1): цены опор, амплитуда,
    длительность в свечах H1. None — движения в payload нет (старые события,
    нет стартового pivot) — строку не выдумываем."""
    if not mv:
        return None
    start, end = mv.get("start_price"), mv.get("end_price")
    if start is None or end is None:
        return None
    line = f"Движение к слому: {_fmt_price(start)} → {_fmt_price(end)}"
    if start:
        pct = 100 * (end - start) / start
        line += f" ({fmt_pct_ru(pct, signed=True)}"
        if mv.get("candles"):
            line += f" · {mv['candles']} свечей H1"
        line += ")"
    if mv.get("start_at") is not None:
        line += f", от {_fmt_time(mv['start_at'])}"
    line += "."
    if mv.get("provenance_status") == "ambiguous":
        line += " Происхождение неоднозначно (§8.1)."
    return line


def _render_break(ev: LtfEvent, ctx: LtfContext) -> list[str]:
    """§5.4 ТЗ 07.10.2026: BOS/SMS — с готовыми зонами либо с ожиданием.
    Отсутствие зон входа не подменяет факт слома."""
    p = ev.payload
    direction = p.get("direction") or (
        ctx.scenario.direction.value if ctx.scenario else ""
    )
    word = _direction_word(direction)
    kind = str(p.get("kind", ev.kind)).upper()
    stage_ru = "вторичный" if p.get("stage") == "secondary" else "первичный"
    head = (
        f"LevelFrame · {_symbol(ctx)} · H1\n"
        f"Подтверждён {word} {kind} ({stage_ru}).\n"
        f"HTF-контекст: {_htf_context(ctx)}"
    )
    if p.get("break_level") is not None:
        head += f"\nУровень слома: {_fmt_price(p['break_level'])}"
    if p.get("close") is not None:
        head += f"\nЗакрытие H1: {_fmt_price(p['close'])}"
    # occurred_at события — close_time пробойной свечи (engine._emit);
    # это именно время закрытия H1, а не открытия (§6 ТЗ 07.10.2026)
    head += f"\nВремя закрытия H1: {fmt_time_msk(ev.occurred_at)}"
    mv_line = _render_movement(p.get("movement"))
    if mv_line:
        head += f"\n{mv_line}"

    entries = p.get("entries") or []
    if p.get("range_pending"):
        head += "\nОжидаем подтверждения LL/HH тремя свечами."
        return _split(head, [], _footer(ev, ctx))
    rng_line = _render_range(p.get("range"))
    if rng_line:
        head += f"\n{rng_line}"
    if not entries:
        head += "\nПодтверждение есть; подходящих зон входа сейчас нет"
        return _split(head, [], _footer(ev, ctx))
    lines = (["Зоны входа:"] + _render_entries_block(entries)
             + _context_lines(entries, ctx))
    return _split(head, lines, _footer(ev, ctx))


def _render_entries_ready(ev: LtfEvent, ctx: LtfContext) -> list[str]:
    """§11.2: дополнение к существующему сценарию — только новые зоны."""
    p = ev.payload
    head = f"LevelFrame · {_symbol(ctx)} · H1\nНовые Entry Zones по сценарию"
    if ctx.scenario is not None:
        dir_ru = DIRECTION_RU.get(ctx.scenario.direction.value,
                                  ctx.scenario.direction.value)
        head += f" {dir_ru} {ctx.scenario.trigger}"
    head += f" ({_htf_context(ctx)}):"
    rng_line = _render_range(p.get("range"))
    if rng_line:
        head += f"\n{rng_line}"
    entries = p.get("entries") or []
    return _split(head,
                  _render_entries_block(entries) + _context_lines(entries, ctx),
                  _footer(ev, ctx))


def _render_touch(ev: LtfEvent, ctx: LtfContext) -> list[str]:
    """§5.5 ТЗ 07.10.2026: касание допустимой Entry Zone. Сигнал касания
    не равен исполненному ордеру; для уровня — ожидание закрытия H1."""
    p = ev.payload
    t = LTF_TYPE_RU.get(p.get("type", ""), p.get("type", "?"))
    dir_ru = (
        DIRECTION_RU.get(ctx.scenario.direction.value, "")
        if ctx.scenario is not None else ""
    )
    head = f"LevelFrame · {_symbol(ctx)} · H1"
    if dir_ru:
        head += f" · {dir_ru}"
    head += f"\nЦена коснулась Entry Zone: {t} H1."
    head += f"\nHTF-контекст: {_htf_context(ctx)}"
    if ctx.scenario is not None:
        basis = f"{ctx.scenario.trigger}"
        if ctx.scenario.created_at:
            basis += f", {fmt_time_msk(ctx.scenario.created_at)}"
        head += f"\nОснование: {basis}"
    lower, upper = p.get("lower"), p.get("upper")
    if lower is not None and upper is not None:
        if lower == upper:
            head += f"\nУровень: {_fmt_price(lower)}"
        else:
            head += f"\nДиапазон: {_fmt_price(lower)}–{_fmt_price(upper)}"
            head += f"\nСередина: {_fmt_price(p['mid'])}"
    if p.get("price") is not None:
        # цену события берём только из payload, не выдумываем (§11)
        head += f"\nЦена события: {_fmt_price(p['price'])}"
    head += "\nСобытие: касание зоны входа"
    head += "\nСигнал касания не равен исполненному ордеру."
    if p.get("type") in ("BSL", "SSL"):
        # §10/§11.3: до закрытия свечи подтверждение не пишем
        head += "\nОжидаем закрытия текущей H1 для проверки снятия без закрепления."
    if p.get("outside_premium"):
        # §18: контекстный допуск — факт «вне Premium» показываем, не блокируем
        head += "\n⚠️ Зона вне Premium — не критично для этого сценария."
        if p.get("context"):
            bear = (
                ctx.scenario is None
                or ctx.scenario.direction.value == "bear"
            )
            head += (
                "\nКонтекст: обновление лоя = снятие SSL + тест 50% D1 FVG."
                if bear else
                "\nКонтекст: обновление хая = снятие BSL + тест 50% D1 FVG."
            )
    return _split(head, [], _footer(ev, ctx))


def _render_sweep(ev: LtfEvent, ctx: LtfContext) -> list[str]:
    """§3.3 ТЗ 07.10.2026: исход liquidity-теста. Снятый уровень — НЕ
    обратная зона входа; повторные входы по нему отключены."""
    p = ev.payload
    t = LTF_TYPE_RU.get(p.get("type", ""), p.get("type", "уровень"))
    level = _fmt_price(p["level"]) if p.get("level") is not None else "?"
    close = _fmt_price(p["close_price"]) if p.get("close_price") is not None else "?"
    head = f"LevelFrame · {_symbol(ctx)} · H1 · {t}"
    if ev.kind == "sweep_confirmed":
        head += (
            f"\nЛиквидность {t} снята."
            f"\nУровень: {level}"
            f"\nЗакрытие H1: {close}"
            "\nСобытие: первое снятие без закрепления, подтверждено закрытием H1"
            "\nСтатус: снята; повторные входы по этому уровню отключены"
        )
    elif p.get("outcome") == "equal_close":
        # Close = K: нет подтверждения строго по нужную сторону — без
        # выдуманного сигнала (§3.2 ТЗ 07.10.2026)
        head += (
            f"\nУровень: {level}"
            f"\nЗакрытие H1: {close} — ровно на уровне"
            "\nСобытие: закрытие ровно на уровне"
            "\nСтатус: неопределённость; подтверждения снятия нет"
        )
    else:
        # §9 (Этап 5): строгое закрытие за уровнем — финальный исход,
        # уровень пройден без возврата и исключён из выбора навсегда
        head += (
            f"\nЛиквидность {t} снята с закреплением."
            f"\nУровень: {level}"
            f"\nЗакрытие H1: {close}"
            "\nСобытие: закрепление H1 строго за уровнем"
            "\nСтатус: снята; повторные входы по этому уровню отключены"
        )
    return _split(head, [], _footer(ev, ctx))


def _render_cancellation(ev: LtfEvent, ctx: LtfContext) -> list[str]:
    """§11.4: отмена сценария. Про позицию не пишем — данных о ней нет."""
    p = ev.payload
    direction = ctx.scenario.direction.value if ctx.scenario else ""
    dir_ru = DIRECTION_RU.get(direction, direction)
    reason = LTF_CANCELLATION_RU.get(p.get("reason", ""), p.get("reason", "?"))
    head = (
        f"LevelFrame · {_symbol(ctx)}: {dir_ru} LTF-сценарий отменён.\n"
        f"Причина: {reason}."
    )
    if p.get("reason") in ("reverse_bos", "reverse_sms"):
        # родитель ещё валиден — наблюдение ждёт нового подтверждения (§6.5)
        head += (
            f"\n{_htf_part(ctx)} ещё валидна: ожидаем новое подтверждение "
            "по направлению этой HTF-зоны."
        )
    return _split(head, [], _footer(ev, ctx))


def render_ltf_messages(ev: LtfEvent, ctx: LtfContext) -> list[str]:
    """Текст(ы) Telegram-сообщения по событию; пустой список — вид без шаблона."""
    if ev.kind in ("bos", "sms"):
        return _render_break(ev, ctx)
    if ev.kind in ("entries_ready", "range_ready"):
        if ev.kind == "range_ready" and not (ev.payload.get("entries")):
            p = ev.payload
            head = f"LevelFrame · {_symbol(ctx)} · H1\nДиапазон готов ({_htf_context(ctx)})."
            rng_line = _render_range(p.get("range"))
            if rng_line:
                head += f"\n{rng_line}"
            note = p.get("note")
            if note:
                head += f"\n{note}."
            return _split(head, [], _footer(ev, ctx))
        return _render_entries_ready(ev, ctx)
    if ev.kind == "touch":
        return _render_touch(ev, ctx)
    if ev.kind in ("sweep_confirmed", "sweep_failed"):
        return _render_sweep(ev, ctx)
    if ev.kind == "cancellation":
        return _render_cancellation(ev, ctx)
    return []
