"""Шаблоны Telegram-сообщений модуля «Altcoins D1 accumulation»
(ТЗ 07.10.2026 §18; формулировки отмены — §12, входы A/B — §2).

Правила:
- каждое сообщение содержит монету/символ, «· D1», setup_id, event_time (МСК),
  detected_at (МСК), источник (venue/pair), as_of и ссылку на график сетапа
  (§18); ссылка — {effective_base_url}/alt.html?asset={cmc_id}, строится
  только от публичного URL (localhost не отправляем, как кнопку «Открыть
  приложение» у HTF); страница alt.html появляется отдельным этапом;
- события одного сетапа одного run объединяются в одно сообщение с
  сохранением оснований; терминальное событие (CANCELLED/EXPIRED_NO_RETEST/
  TARGETS_COMPLETED) ведёт сообщение, входные события той же пачки —
  контекстными строками (§18);
- отмена объясняет K и режим wick/close как ПРОЕКТНУЮ настройку v1; термин
  «стоп-лосс» нигде не используется (§12); при K<=0 — точная формулировка
  §12 «По выбранной формуле ценовой уровень отмены неположительный»;
- флаг intra_candle_sequence_unknown — оговорка о неизвестной
  внутрисвечной последовательности (§12/§13): факты перечисляются без
  выдуманного порядка;
- цены — fmt_price_ru (до 8 значащих цифр для мелких альткоинов, хвостовые
  нули обрезаются), время — МСК.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from ..models_alt import (
    AltAsset,
    AltEvent,
    AltEventType,
    AltFrozenRange,
    AltInstrumentSource,
    AltRun,
    AltSetup,
)
from .formatting import fmt_pct_ru, fmt_price_ru, fmt_time_msk

DAY_MS = 86_400_000

# Терминальные события (§15): ведут объединённое сообщение пачки (§18)
_TERMINAL_TYPES = {
    AltEventType.CANCELLED.value,
    AltEventType.EXPIRED_NO_RETEST.value,
    AltEventType.TARGETS_COMPLETED.value,
}

# «Входные» события: при наличии терминального в пачке демонтируются до
# контекстных строк — новая возможность не подаётся наравне с завершением
_ENTRY_TYPES = {
    AltEventType.ENTRY_A.value,
    AltEventType.ENTRY_B.value,
    AltEventType.RETEST.value,
}

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "0.0.0.0", "::1"}


@dataclass
class AltContext:
    """Контекст сообщения, обогащённый диспетчером из БД (§18)."""

    asset: Optional[AltAsset]
    source: Optional[AltInstrumentSource]
    setup: Optional[AltSetup]
    frozen: Optional[AltFrozenRange]
    run: Optional[AltRun] = None
    base_url: Optional[str] = None  # effective_base_url; localhost — без ссылки


def _payload(ev: AltEvent) -> dict[str, Any]:
    try:
        return json.loads(ev.payload_json or "{}")
    except ValueError:
        return {}


def _symbol(ctx: AltContext) -> str:
    return ctx.asset.symbol if ctx.asset is not None else "актив ?"


def _range_lu(ctx: AltContext) -> tuple[Optional[float], Optional[float]]:
    if ctx.frozen is not None:
        return ctx.frozen.lower, ctx.frozen.upper
    return None, None


def _fmt(v: Optional[float]) -> str:
    return fmt_price_ru(v) if v is not None else "?"


def _cancel_mode_ru(mode: str) -> str:
    return {
        "wick_on_closed_d1": "тень закрытой D1 (Low ≤ K)",
        "close_on_closed_d1": "закрытие D1 (Close ≤ K)",
    }.get(mode, mode)


def _k_lines(setup: Optional[AltSetup]) -> list[str]:
    """Строки об уровне отмены K (§12): формула, режим как проектная
    настройка v1; K<=0 — не обрезается и не подменяется, текст §12."""
    if setup is None or setup.cancel_price is None:
        return []
    k = fmt_price_ru(setup.cancel_price)
    if not setup.cancel_reachable:
        return [
            f"По выбранной формуле ценовой уровень отмены неположительный "
            f"(K = 2L − U = {k}); ценовая отмена для этого случая не "
            f"применяется — продолжает работать таймер ретеста после выхода."
        ]
    return [
        f"Уровень отмены K = {k} (K = 2L − U; режим подтверждения: "
        f"{_cancel_mode_ru(setup.cancel_mode)} — проектная настройка v1)."
    ]


def _target_lines(targets: list[dict[str, Any]]) -> list[str]:
    """TP1..TP4 списком; «пройдена к моменту подтверждения» — отдельная
    подпись, не будущий потенциал (§13)."""
    lines = []
    for t in targets:
        line = f"TP{t.get('tp', '?')} — {_fmt(t.get('price'))}"
        if t.get("passed_at_confirmation"):
            line += " (пройдена к моменту подтверждения)"
        lines.append(line)
    return lines


def _setup_targets(setup: Optional[AltSetup]) -> list[dict[str, Any]]:
    if setup is None:
        return []
    try:
        raw = json.loads(setup.targets_json or "[]")
    except ValueError:
        return []
    return [t for t in raw if isinstance(t, dict)]


# ---------------------------------------------------------------------------
# Блоки отдельных событий
# ---------------------------------------------------------------------------


def _render_forming_started(ev: AltEvent, ctx: AltContext,
                            p: dict[str, Any]) -> list[str]:
    n = p.get("n_days_at_recognition")
    dd = p.get("drawdown_pct")
    if dd is not None:
        drop = f"после падения {fmt_pct_ru(float(dd))} от биржевого ATH"
    else:
        # старые события без поля — порог допуска модуля известен (§2)
        drop = "после падения более 80% от биржевого ATH"
    head = "Найдена аккумуляция"
    if n is not None:
        head += f" {int(n)} дней"
    lines = [f"{head} {drop}."]
    lower, upper = _range_lu(ctx)
    if lower is None:
        lower, upper = p.get("lower"), p.get("upper")
    if lower is not None and upper is not None:
        lines.append(f"Диапазон {_fmt(lower)}–{_fmt(upper)}. Формируется.")
    else:
        lines.append("Формируется.")
    return lines


def _render_mature_frozen(ev: AltEvent, ctx: AltContext,
                          p: dict[str, Any]) -> list[str]:
    lower = p.get("lower", ctx.frozen.lower if ctx.frozen else None)
    upper = p.get("upper", ctx.frozen.upper if ctx.frozen else None)
    mid = p.get("mid", ctx.frozen.mid if ctx.frozen else None)
    lines = [
        f"Зрелый диапазон зафиксирован: {_fmt(lower)}–{_fmt(upper)} "
        f"(середина {_fmt(mid)})."
    ]
    candles = p.get(
        "included_candles",
        ctx.frozen.included_candles if ctx.frozen else None,
    )
    if candles:
        lines.append(f"Длительность консолидации: {int(candles)} дн.")
    mature_at = ctx.frozen.mature_at_ms if ctx.frozen else None
    if mature_at:
        lines.append(f"Зафиксирован: {fmt_time_msk(mature_at)}.")
    lines.append("После фиксации границы не расширяются.")
    lines.extend(_k_lines(ctx.setup))
    return lines


def _render_manipulation_started(ev: AltEvent, ctx: AltContext,
                                 p: dict[str, Any]) -> list[str]:
    lower = p.get("range_lower")
    if lower is None and ctx.frozen is not None:
        lower = ctx.frozen.lower
    return [
        f"Нижний вынос: минимум {_fmt(p.get('low'))} ниже границы "
        f"{_fmt(lower)}.",
        "Манипуляция началась — границы диапазона не меняются, это не "
        "отмена сетапа.",
    ]


def _render_manipulation_ended(ev: AltEvent, ctx: AltContext,
                               p: dict[str, Any]) -> list[str]:
    lower = ctx.frozen.lower if ctx.frozen is not None else None
    lines = [
        f"Возврат выше {_fmt(lower)}: эпизод манипуляции завершён.",
    ]
    days = p.get("days_below")
    mn = p.get("min_price")
    if days is not None:
        line = f"Дней ниже границы: {int(days)}"
        if mn is not None:
            line += f", минимум эпизода {_fmt(mn)}"
        lines.append(line + ".")
    return lines


def _render_ssl_taken(ev: AltEvent, ctx: AltContext,
                      p: dict[str, Any]) -> list[str]:
    return [
        f"Снятие внутреннего SSL: опора {_fmt(p.get('level_price'))}, "
        f"закрытие D1 {_fmt(p.get('close'))}.",
        "Снятие SSL необязательно для входа — это диагностический факт (§9).",
    ]


def _render_bos_sms(ev: AltEvent, ctx: AltContext,
                    p: dict[str, Any]) -> list[str]:
    kind = "BOS" if ev.event_type == AltEventType.BOS_CONFIRMED.value else "SMS"
    return [
        f"Подтверждён bullish {kind}: уровень слома "
        f"{_fmt(p.get('level_price'))}, закрытие D1 {_fmt(p.get('close'))}.",
    ]


def _render_entry_a(ev: AltEvent, ctx: AltContext,
                    p: dict[str, Any]) -> list[str]:
    lines = [f"Возможность A по закрытию {_fmt(p.get('price'))}."]
    targets = _setup_targets(ctx.setup)
    if targets:
        lines.append("Цели:")
        lines.extend(_target_lines(targets))
    return lines


def _render_breakout(ev: AltEvent, ctx: AltContext,
                     p: dict[str, Any]) -> list[str]:
    upper = p.get("upper")
    mid = ctx.frozen.mid if ctx.frozen is not None else None
    lines = [
        f"Выход выше {_fmt(upper)}: закрытие D1 {_fmt(p.get('close'))}.",
    ]
    deadline = p.get("retest_deadline_ms")
    wait = f"Ждём ретест {_fmt(mid)}–{_fmt(upper)}"
    if deadline:
        wait += f" до {fmt_time_msk(deadline)}"
    lines.append(wait + ".")
    return lines


def _render_retest(ev: AltEvent, ctx: AltContext,
                   p: dict[str, Any]) -> list[str]:
    zone = p.get("zone") or {}
    mid, upper = zone.get("lower"), zone.get("upper")
    if mid is None and ctx.frozen is not None:
        mid, upper = ctx.frozen.mid, ctx.frozen.upper
    if p.get("journal"):
        lines = [
            f"Повторное касание ретеста {_fmt(mid)}–{_fmt(upper)} "
            "(журнал, без нового входа)."
        ]
    else:
        lines = [f"Касание ретеста {_fmt(mid)}–{_fmt(upper)}."]
    if p.get("depth_below_mid") is not None:
        lines.append(
            f"Глубина ниже середины: {_fmt(p['depth_below_mid'])} — "
            "само по себе не условие отмены (§11)."
        )
    candle_ot = p.get("candle_open_time")
    if candle_ot:
        lines.append(f"Дневная свеча {fmt_time_msk(candle_ot)}.")
    return lines


def _render_entry_b(ev: AltEvent, ctx: AltContext,
                    p: dict[str, Any]) -> list[str]:
    zone = p.get("zone") or {}
    mid, upper = zone.get("lower"), zone.get("upper")
    if mid is None and ctx.frozen is not None:
        mid, upper = ctx.frozen.mid, ctx.frozen.upper
    return [
        f"Дополнительная возможность B того же сетапа. "
        f"Область {_fmt(mid)}–{_fmt(upper)}. Цели — первоначальные (§2)."
    ]


def _render_target_hit(ev: AltEvent, ctx: AltContext,
                       p: dict[str, Any]) -> list[str]:
    levels = p.get("levels") or []
    prices = p.get("prices") or {}
    passed = set(p.get("passed_at_confirmation") or [])
    parts = []
    for n in levels:
        price = prices.get(str(n))
        part = f"TP{n} — {_fmt(price)}"
        if n in passed:
            part += " (пройдена к моменту подтверждения)"
        parts.append(part)
    if not parts:
        return ["Достигнуты цели сетапа."]
    head = "Достигнута цель:" if len(parts) == 1 else "Достигнуты цели:"
    return [head] + parts


def _render_cancelled(ev: AltEvent, ctx: AltContext,
                      p: dict[str, Any]) -> list[str]:
    k = p.get("cancel_price")
    if k is None and ctx.setup is not None:
        k = ctx.setup.cancel_price
    mode = p.get(
        "cancel_mode",
        ctx.setup.cancel_mode if ctx.setup else "wick_on_closed_d1",
    )
    return [
        f"Сетап отменён: достигнут уровень отмены K = {_fmt(k)} "
        "(K = 2L − U, нижняя граница минус высота исходного диапазона).",
        f"Режим подтверждения: {_cancel_mode_ru(mode)} — проектная "
        "настройка v1 (§12), не утверждённый пользователем параметр.",
        "Новые входы и целевые уведомления по сетапу прекращены; история "
        "сохраняется.",
    ]


def _render_expired(ev: AltEvent, ctx: AltContext,
                    p: dict[str, Any]) -> list[str]:
    start, deadline = p.get("first_breakout_closed_at"), p.get("deadline")
    days = None
    if start and deadline:
        days = round((deadline - start) / DAY_MS)
    lines = [
        f"Сетап завершён: за {days if days else 14} дней после выхода "
        "ретеста не было.",
    ]
    if start:
        lines.append(f"Выход: {fmt_time_msk(start)}.")
    if deadline:
        lines.append(f"Дедлайн ретеста: {fmt_time_msk(deadline)}.")
    lines.append(
        "Завершение наблюдения — не команда закрывать позицию (§11)."
    )
    return lines


def _render_targets_completed(ev: AltEvent, ctx: AltContext,
                              p: dict[str, Any]) -> list[str]:
    levels = p.get("levels") or []
    n = len(levels) or 4
    return [
        f"Все {n} цели (TP1–TP{n}) достигнуты — аналитический план "
        "уровней завершён.",
        "Завершение плана не означает, что пользователь закрыл позицию (§13).",
    ]


def _render_review_required(ev: AltEvent, ctx: AltContext,
                            p: dict[str, Any]) -> list[str]:
    lines = [
        "Требуется проверка опор: выбор стартовой опоры консолидации "
        "неоднозначен.",
        "Альтернативные опоры показаны на графике сетапа; новые входы до "
        "разбора не выдаются.",
    ]
    alts = p.get("alternative_anchors") or []
    if alts:
        lines.append(
            "Альтернативные опоры (свечи): "
            + ", ".join(fmt_time_msk(a) for a in alts)
            + "."
        )
    return lines


_RENDERERS = {
    AltEventType.FORMING_STARTED.value: _render_forming_started,
    AltEventType.MATURE_FROZEN.value: _render_mature_frozen,
    AltEventType.MANIPULATION_STARTED.value: _render_manipulation_started,
    AltEventType.MANIPULATION_ENDED.value: _render_manipulation_ended,
    AltEventType.SSL_TAKEN.value: _render_ssl_taken,
    AltEventType.BOS_CONFIRMED.value: _render_bos_sms,
    AltEventType.SMS_CONFIRMED.value: _render_bos_sms,
    AltEventType.ENTRY_A.value: _render_entry_a,
    AltEventType.BREAKOUT.value: _render_breakout,
    AltEventType.RETEST.value: _render_retest,
    AltEventType.ENTRY_B.value: _render_entry_b,
    AltEventType.TARGET_HIT.value: _render_target_hit,
    AltEventType.CANCELLED.value: _render_cancelled,
    AltEventType.EXPIRED_NO_RETEST.value: _render_expired,
    AltEventType.TARGETS_COMPLETED.value: _render_targets_completed,
    AltEventType.REVIEW_REQUIRED.value: _render_review_required,
}


def _render_block(ev: AltEvent, ctx: AltContext) -> list[str]:
    renderer = _RENDERERS.get(ev.event_type)
    if renderer is None:
        return [f"Событие {ev.event_type}."]
    return renderer(ev, ctx, _payload(ev))


def _context_line(ev: AltEvent, ctx: AltContext) -> str:
    """Демонтированная входная строка при терминальном лидере пачки (§18):
    факт возможности сохраняется, но не подаётся как новая возможность."""
    p = _payload(ev)
    if ev.event_type == AltEventType.ENTRY_A.value:
        return (
            f"Контекст: на той же свече была возможность A по закрытию "
            f"{_fmt(p.get('price'))} — завершение сетапа имеет приоритет."
        )
    if ev.event_type == AltEventType.ENTRY_B.value:
        return (
            "Контекст: на той же свече была возможность B — завершение "
            "сетапа имеет приоритет."
        )
    if ev.event_type == AltEventType.RETEST.value:
        zone = p.get("zone") or {}
        return (
            f"Контекст: на той же свече было касание ретеста "
            f"{_fmt(zone.get('lower'))}–{_fmt(zone.get('upper'))}."
        )
    block = _render_block(ev, ctx)
    return f"Контекст: {block[0]}"


# ---------------------------------------------------------------------------
# Сборка сообщения
# ---------------------------------------------------------------------------


def _chart_url(ctx: AltContext) -> Optional[str]:
    """Ссылка на график сетапа (§16/§18): только публичный URL приложения,
    ключ — CMC asset id (устойчивый идентификатор, символом не подменяется,
    §3). Страница alt.html реализуется отдельным этапом."""
    if not ctx.base_url or ctx.asset is None:
        return None
    host = urlparse(ctx.base_url).hostname or ""
    if host in _LOCAL_HOSTS:
        return None
    return f"{ctx.base_url}/alt.html?asset={ctx.asset.cmc_id}"


def _footer(events: list[AltEvent], ctx: AltContext) -> list[str]:
    lead = events[0]
    setup_id = ctx.setup.id if ctx.setup is not None else lead.setup_id
    lines = [
        f"Сетап #{setup_id} · D1",
        f"Свеча: {fmt_time_msk(lead.event_time_ms)}",
        f"Обнаружено: {fmt_time_msk(max(e.detected_at_ms for e in events))}",
    ]
    if ctx.source is not None:
        lines.append(
            f"Источник: {ctx.source.venue} spot / {ctx.source.symbol}"
        )
    if ctx.run is not None and ctx.run.as_of_ms:
        lines.append(f"as_of: {fmt_time_msk(ctx.run.as_of_ms)}")
    url = _chart_url(ctx)
    if url:
        lines.append(f"График: {url}")
    if ctx.setup is not None and not ctx.setup.universe_eligible:
        lines.append(
            "Актив вне текущей выборки CMC — наблюдение продолжается до "
            "завершения сетапа (§3)."
        )
    if any(_payload(e).get("intra_candle_sequence_unknown") for e in events):
        lines.append(
            "⚠ Порядок событий внутри дневной свечи неизвестен — "
            "перечислены только наблюдаемые факты, без выдуманной "
            "последовательности."
        )
    return lines


def render_alt_message(events: list[AltEvent], ctx: AltContext) -> str:
    """Одно сообщение на пачку событий одного сетапа одного run (§18).

    Терминальное событие пачки ведёт сообщение; входные события той же
    пачки — контекстными строками. Остальные блоки — в хронологии."""
    if not events:
        return ""
    ordered = sorted(events, key=lambda e: (e.event_time_ms, e.id or 0))
    terminal = [e for e in ordered if e.event_type in _TERMINAL_TYPES]
    blocks: list[list[str]] = []
    if terminal:
        lead = terminal[0]
        ordered = [lead] + [e for e in ordered if e is not lead]
        blocks.append(_render_block(lead, ctx))
        for ev in ordered[1:]:
            if ev.event_type in _ENTRY_TYPES:
                blocks.append([_context_line(ev, ctx)])
            else:
                blocks.append(_render_block(ev, ctx))
    else:
        for ev in ordered:
            blocks.append(_render_block(ev, ctx))

    parts = [f"{_symbol(ctx)} · D1"]
    parts.extend("\n".join(b) for b in blocks)
    parts.append("\n".join(_footer(ordered, ctx)))
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Сводка первичной загрузки (§18: без исторического спама — одна сводка)
# ---------------------------------------------------------------------------

_STATE_RU = {
    "forming": "формируются",
    "mature": "зрелые",
    "active_confirmed": "подтверждены",
    "review_required": "требуют проверки",
}


def render_backfill_summary(
    summary: dict[str, Any],
    setups_by_state: Optional[dict[str, int]] = None,
) -> str:
    """Текст разовой сводки после первичной загрузки (§18): месяцы старых
    событий не рассылаются — только итог текущих найденных сетапов.

    summary — summary_json дневного прогона (per_asset/processed/errors);
    setups_by_state — число сетапов по состояниям (считает диспетчер)."""
    per_asset = summary.get("per_asset") or []
    processed = summary.get("processed", 0)
    errors = summary.get("errors", 0)
    no_pair = sum(
        1 for e in per_asset if e.get("reason") == "no_spot_pair"
    )
    skipped = summary.get(
        "skipped", sum(1 for e in per_asset if e.get("status") == "skipped")
    )
    lines = [
        "Альткоины D1 · первичная загрузка завершена.",
        f"Обработано монет: {processed}; без спотовой пары: {no_pair}; "
        f"пропущено: {skipped}; ошибок: {errors}.",
    ]
    if summary.get("universe_stale"):
        lines.append(
            "⚠ Снимок рейтинга CMC устарел — используется последний "
            "успешный (universe_stale)."
        )
    if setups_by_state is not None:
        total = sum(setups_by_state.values())
        parts = [
            f"{label}: {setups_by_state.get(state, 0)}"
            for state, label in _STATE_RU.items()
            if setups_by_state.get(state)
        ]
        line = f"Найдено сетапов: {total}"
        if parts:
            line += " (" + ", ".join(parts) + ")"
        lines.append(line + ".")
    lines.append(
        "Исторические события не рассылаются — дальше приходят только "
        "новые события по мере их появления (§18)."
    )
    return "\n".join(lines)
