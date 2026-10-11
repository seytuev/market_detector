"""Рендер LTF-графика для Telegram-бота (п.7 ТЗ бота, ТЗ 07.10.2026 §8–§10).

Свечи H1/D1/W1 + включаемые слои поверх read model
app/services/overview.py::observation_chart_layers. Стиль — общий с
app/notify/chartimg.py (тёмная тема снимков зон). Заголовок — в
зарезервированной полосе без обрезки, подписи цен — в правой колонке без
наложений, время — МСК, биржа/тип рынка не выводятся (§5.1).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..db import Database
from ..models import now_ms
from ..notify.chart_series import screenshot_candles
from ..notify.chartimg import candles_to_df, make_dark_style
from ..notify.chart_snapshot import (
    KIND_RU,
    PRICE_MARGIN,
    distance_pct,
    event_level,
    in_working_band,
    right_pad,
    zone_on_scale,
)
from ..notify.chartlabels import (
    FIG_DPI,
    FIG_SIZE,
    add_legend,
    apply_layout,
    contain_labels,
    layout_price_labels,
    set_footer,
    set_header,
)
from ..notify.formatting import fmt_price_ru, fmt_time_msk
from ..services.overview import observation_chart_layers
from ..texts_ru import (
    DIRECTION_RU,
    LTF_CANCELLATION_RU,
    LTF_SCENARIO_STATE_RU,
    LTF_TYPE_RU,
)

# Биты слоёв для компактного кодирования в callback
# (nav:chart…:<iid>:<tf>:<days>:<mask>)
LAYER_BITS = {"htf": 1, "bos": 2, "range": 4, "entries": 8}
LAYER_LABELS = {
    "htf": "HTF-контекст",
    "bos": "BOS/SMS",
    "range": "Диапазон",
    "entries": "Entry Zones",
}
DEFAULT_MASK = 0
for _bit in LAYER_BITS.values():
    DEFAULT_MASK |= _bit  # 15 — все слои

# Свечей на графике для старших ТФ (period_days на них не действует)
_TF_CANDLE_LIMIT = {"D1": 120, "W1": 52}

_COLOR_HTF = "#fbbf24"     # старший контекст
_COLOR_BOS = "#fbbf24"     # уровни слома структуры
_COLOR_RANGE = "#60a5fa"   # диапазон Premium/Discount
_COLOR_LONG = "#34d399"    # допуск бычьего сценария
_COLOR_SHORT = "#f87171"   # допуск медвежьего сценария
_ZONE_STATUS_RU = {
    "active": "активна",
    "candidate": "кандидат",
    "weakened": "ослаблена",
    "filled": "заполнена",
    "taken": "снята",
    "archived": "архив",
    "rejected": "отклонена",
}


def layers_from_mask(mask: int) -> tuple[str, ...]:
    """Битовая маска callback → набор имён слоёв для render_ltf_chart."""
    return tuple(name for name, bit in LAYER_BITS.items() if mask & bit)


def _chart_candles(
    db: Database, instrument_id: int, tf: str, period_days: int, now: int,
    end_ms: Optional[int] = None,
) -> list:
    """Окно снимка. Текущая ситуация включает незакрытый бар периода now;
    график события (end_ms) — только закрытые свечи до правой границы."""
    if tf == "H1":
        since = now - period_days * 86_400_000
        return screenshot_candles(
            db, instrument_id, "H1", start_ms=since, end_ms=end_ms, now=now,
        )
    limit = _TF_CANDLE_LIMIT.get(tf)
    if limit is None:
        return []
    return screenshot_candles(
        db, instrument_id, tf, end_ms=end_ms, limit=limit, now=now,
    )


def render_ltf_chart(
    db: Database,
    observation_id: int,
    out_path,
    *,
    tf: str = "H1",
    period_days: int = 7,
    layers: tuple[str, ...] = ("htf", "bos", "range", "entries"),
    source_label: str = "",
    settings=None,
    now: Optional[int] = None,
    end_ms: Optional[int] = None,
    event=None,
) -> Optional[str]:
    """PNG графика выбранного контекста: свечи + слои htf/bos/range/entries.

    Подтверждённый уровень BOS/SMS — сплошная линия, ожидаемый — пунктир.
    now — момент снимка: хендлер передаёт тот же now, что использовал для
    caption, чтобы цена/сценарий/время на картинке и в подписи совпадали.
    end_ms — правая граница периода (режим «график события», §11.1 ТЗ
    07.10.2026: картинка по снимку, на котором событие произошло);
    None — до текущего незакрытого бара, если его период ещё идёт
    («текущая ситуация»); иначе до последней закрытой свечи.
    None — наблюдение не найдено или свечей нет (хендлер отвечает текстом).
    source_label оставлен для совместимости — на изображение источник не
    выводится (ТЗ 07.10.2026 §5.1).
    event — запись LtfEvent. Картинка объясняется этим событием и не
    подменяет его сценарий более поздним. Цены события берутся из payload.
    """
    import mplfinance as mpf
    from matplotlib import pyplot as plt
    from matplotlib.patches import Rectangle

    obs = db.get_ltf_observation(observation_id)
    if obs is None:
        return None
    ins = db.get_instrument(obs.instrument_id)
    if event is not None and end_ms is None:
        end_ms = int(event.occurred_at)
    if event is not None and now is None:
        now = int(event.occurred_at)
    now = now if now is not None else now_ms()
    requested_since = now - period_days * 86_400_000 if tf == "H1" else None
    candles = _chart_candles(db, obs.instrument_id, tf, period_days, now,
                             end_ms=end_ms)
    if not candles:
        return None
    fetched_first = candles[0].open_time
    setup = None
    sliced_to_cause = False
    moment = end_ms if end_ms is not None else now
    if settings is not None and tf == "H1":
        from ..services.h1_setup import project_setup
        setup = project_setup(
            db, settings.detector, obs.instrument_id, moment,
            mode="event" if end_ms is not None else "current",
        )
        frame = setup.get("frame_from") if setup else None
        if frame:
            sliced = [c for c in candles if c.open_time >= int(frame)]
            if len(sliced) >= 8:
                candles = sliced
                sliced_to_cause = True
    layer_set = set(layers)
    info = None
    if settings is not None:
        layer_kwargs = {}
        if end_ms is not None or event is not None:
            layer_kwargs["as_of"] = int(moment)
            known = int(moment)
            if event is not None and getattr(event, "detected_at", None):
                known = max(known, int(event.detected_at))
            layer_kwargs["known_at"] = known
            if event is not None and getattr(event, "scenario_id", None) is not None:
                layer_kwargs["scenario_id"] = event.scenario_id
        info = observation_chart_layers(
            db, settings, observation_id, **layer_kwargs,
        )
    payload = (getattr(event, "payload", None) or {}) if event is not None else {}

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    df = candles_to_df(candles)
    fig, axes = mpf.plot(
        df,
        type="candle",
        style=make_dark_style(),
        returnfig=True,
        figsize=FIG_SIZE,
        datetime_format="%d.%m %H:%M",
        xrotation=0,
        tight_layout=False,
    )
    ax = axes[0]
    ax.set_ylabel("Цена")

    # Внутренняя ось X mplfinance — позиции свечей 0..n-1 (как в chartimg)
    n = len(candles)
    pad_right = right_pad(n)
    xr = n - 0.5 + pad_right

    def x_since(ts: int) -> float:
        return max(sum(1 for c in candles if c.open_time < ts) - 0.5, -0.5)

    extra_levels: list[float] = []
    labels: list[tuple[float, str, str]] = []
    legend: list[tuple[str, str]] = []
    legend_dashed: list[str] = []

    # рабочий масштаб — по свечам; далёкий HTF-контекст НЕ должен сжимать
    # свечи до нечитаемости (§9.2 ТЗ 07.10.2026): тогда зону не рисуем,
    # а её значения уходят в подзаголовок
    c_hi = max(c.high for c in candles)
    c_lo = min(c.low for c in candles)

    # ---- слой HTF-контекста: прямоугольник только если обе границы
    # в рабочем окне H1. Иначе компактная карточка, ось не растягивается.
    pz = (info or {}).get("parent_zone")
    htf_far_note: Optional[str] = None
    last_close = candles[-1].close
    if pz:
        lo, hi = float(pz["lower"]), float(pz["upper"])
        on_scale = zone_on_scale(lo, hi, c_lo, c_hi)
        if on_scale and "htf" in layer_set:
            x0 = x_since(pz.get("display_from") or pz["formed_at"])
            ax.add_patch(Rectangle(
                (x0, lo), n - 0.5 - x0, hi - lo,
                facecolor=_COLOR_HTF, alpha=0.15,
                edgecolor=_COLOR_HTF, linewidth=1.0, zorder=1,
            ))
            for price in (hi, lo):
                ax.hlines(price, x0, xr, color=_COLOR_HTF, linewidth=1.0,
                          zorder=4)
            mid = (lo + hi) / 2
            ax.hlines(mid, x0, xr, color=_COLOR_HTF,
                      linewidth=0.9, linestyles="--", zorder=4)
            htf_name = f"HTF {pz['type'].upper()} {pz['timeframe']}"
            labels += [
                (hi, f"{htf_name} {fmt_price_ru(hi)}", _COLOR_HTF, False),
                (mid, f"HTF {pz['timeframe']} 50% {fmt_price_ru(mid)}",
                 _COLOR_HTF, False),
                (lo, f"{htf_name} {fmt_price_ru(lo)}", _COLOR_HTF, False),
            ]
            extra_levels += [lo, hi]
            legend.append(("HTF-контекст", _COLOR_HTF))
        elif not on_scale:
            dist = distance_pct(last_close, lo, hi)
            pz_dir = DIRECTION_RU.get(pz.get("direction", ""), "")
            status = _ZONE_STATUS_RU.get(pz.get("status") or "", pz.get("status") or "")
            dist_txt = (
                "цена внутри зоны" if dist == 0
                else (
                    f"до границы {f'{dist:.1f}'.replace('.', ',')}%"
                    if dist is not None else ""
                )
            )
            htf_far_note = (
                f"HTF {pz['type'].upper()} {pz['timeframe']} вне кадра (вне окна): "
                f"{fmt_price_ru(lo)}–{fmt_price_ru(hi)}"
            )
            extra = " · ".join(
                bit for bit in (pz_dir, status, dist_txt) if bit
            )
            if extra:
                htf_far_note += f" · {extra}"

    offscale: list[str] = []

    def _on_scale(price: float) -> bool:
        return in_working_band(float(price), c_lo, c_hi)

    def _remember(price: float) -> None:
        if _on_scale(price):
            extra_levels.append(float(price))

    # ---- слой BOS/SMS: подтверждённый сплошной, ожидаемый пунктиром ----
    if "bos" in layer_set and info:
        breaks = [
            ev for ev in info["structure_events"]
            if str(ev.get("kind") or "").lower() in ("bos", "sms")
            and ev.get("break_level") is not None
        ]
        last_idx = len(breaks) - 1
        for i, ev in enumerate(breaks):
            level = ev["break_level"]
            label = str(ev.get("kind") or "").upper()
            if not _on_scale(level):
                offscale.append(f"{label} {fmt_price_ru(level)}")
                continue
            x0 = x_since(ev.get("break_candle_open_time") or ev["occurred_at"])
            # §9.4: актуальный слом — отрезок вправо; старые не тянутся
            # бесконечно (короткий хвост от свечи слома)
            x_end = xr if i == last_idx else min(x0 + 6, xr)
            ax.hlines(level, x0, x_end, color=_COLOR_BOS, linewidth=1.2,
                      zorder=4)
            if i == last_idx:
                labels.append((
                    level, f"{label} {fmt_price_ru(level)}", _COLOR_BOS, False,
                ))
                # §9.4: для подтверждения — время закрытия H1 по МСК у свечи
                ax.annotate(
                    f"закрытие H1 {fmt_time_msk(ev['occurred_at'])}",
                    xy=(x0, level), xytext=(x0 + 0.5, level),
                    fontsize=6.5, color=_COLOR_BOS, va="bottom", zorder=5,
                )
            _remember(level)
        expected = info.get("expected") or {}
        for key in ("bos", "sms"):
            exp = expected.get(key)
            if not exp or exp.get("level") is None:
                continue
            # Пунктир — ожидаемое условие, не состоявшийся факт и не прогноз.
            if not _on_scale(exp["level"]):
                offscale.append(
                    f"ожид. {key.upper()} {fmt_price_ru(exp['level'])}"
                )
                continue
            ax.hlines(exp["level"], -0.5, xr, color=_COLOR_BOS,
                      linewidth=1.0, linestyles="--", zorder=4)
            labels.append((
                exp["level"], f"ожид. {key.upper()}", _COLOR_BOS, False,
            ))
            _remember(exp["level"])
        if breaks or any(expected.get(key) for key in ("bos", "sms")):
            legend.append(("BOS/SMS", _COLOR_BOS))
            if any(expected.get(key) for key in ("bos", "sms")):
                legend_dashed.append("BOS/SMS")

    def _draw_pd(lower, upper, mid, *, provisional: bool, origin_at, from_event: bool):
        if not _on_scale(lower) or not _on_scale(upper):
            offscale.append(
                f"PD {fmt_price_ru(lower)}–{fmt_price_ru(upper)}"
                f" · PD 50% {fmt_price_ru(mid)}"
            )
            return
        style = "--" if provisional else "-"
        x0 = x_since(origin_at) if origin_at else -0.5
        for price in (lower, upper):
            ax.hlines(price, x0, xr, color=_COLOR_RANGE, linewidth=1.0,
                      linestyles=style, zorder=3)
        ax.hlines(mid, x0, xr, color=_COLOR_RANGE, linewidth=0.9,
                  linestyles="--", zorder=3)
        if from_event:
            high_tag, low_tag = "H снимка", "L снимка"
        else:
            bull = setup.get("direction") == "bull" if setup else True
            high_tag = "H потенц." if bull and provisional else ("H подтв." if bull else "H")
            low_tag = "L потенц." if (not bull) and provisional else ("L подтв." if not bull else "L")
        labels.append((upper, high_tag, _COLOR_RANGE, False))
        labels.append((mid, "PD 50%", _COLOR_RANGE, False))
        labels.append((lower, low_tag, _COLOR_RANGE, False))
        _remember(lower)
        _remember(upper)
        legend_name = "PD снимка" if from_event else (
            "PD H1 " + ("потенц." if provisional else "подтв.")
        )
        legend.append((legend_name, _COLOR_RANGE))
        if provisional:
            legend_dashed.append(legend_name)

    # ---- слой диапазона. Для события — границы из записи, не поздний пересчёт.
    payload_range = payload.get("range") if isinstance(payload.get("range"), dict) else None
    drew_range = False
    if (
        "range" in layer_set and event is not None and payload_range
        and payload_range.get("lower") is not None
        and payload_range.get("upper") is not None
    ):
        lo_r, hi_r = float(payload_range["lower"]), float(payload_range["upper"])
        mid_r = payload_range.get("mid")
        mid_r = float(mid_r) if mid_r is not None else (lo_r + hi_r) / 2
        _draw_pd(lo_r, hi_r, mid_r, provisional=bool(payload.get("range_pending")),
                 origin_at=None, from_event=True)
        drew_range = True
    elif "range" in layer_set and setup and setup.get("lower") is not None:
        provisional = setup.get("range_status") != "confirmed"
        origin_at = (setup.get("origin_anchor") or {}).get("at")
        _draw_pd(
            float(setup["lower"]), float(setup["upper"]), float(setup["eq"]),
            provisional=provisional, origin_at=origin_at, from_event=False,
        )
        drew_range = True
    elif "range" in layer_set and info and setup is None:
        cur_range = next((r for r in info["ranges"] if r.get("current")), None)
        if cur_range and cur_range.get("lower") is not None:
            _draw_pd(
                float(cur_range["lower"]), float(cur_range["upper"]),
                float(cur_range["mid"]),
                provisional=False, origin_at=None, from_event=False,
            )
            drew_range = True
    if not drew_range and "range" in layer_set and event is not None:
        offscale.append("PD в записи события нет")

    def _draw_zone(e, *, primary: bool = False):
        lower, upper = e.get("lower"), e.get("upper")
        if lower is None or upper is None:
            return
        lower, upper = float(lower), float(upper)
        kind = str(e.get("type") or "")
        title = LTF_TYPE_RU.get(kind, kind or "Зона")
        if kind in ("SSL", "BSL"):
            title = f"{kind} ликвидность"
            legend_name = "Ликвидность"
        elif e.get("eligible_now") or (
            event is not None and event.kind in ("entries_ready", "range_ready")
        ):
            title = f"{title} · допуск"
            legend_name = "Допуск"
        else:
            legend_name = "Зона"
        direction = e.get("direction")
        direction = getattr(direction, "value", direction)
        if direction not in ("bull", "bear") and event is not None:
            direction = payload.get("direction")
        color = _COLOR_LONG if direction == "bull" else _COLOR_SHORT
        if not zone_on_scale(lower, upper, c_lo, c_hi):
            if lower == upper:
                offscale.append(f"{title} {fmt_price_ru(lower)}")
            else:
                offscale.append(
                    f"{title} {fmt_price_ru(lower)}–{fmt_price_ru(upper)}"
                )
            return
        x_at = e.get("formed_at") or e.get("candle_open_time")
        if x_at is None and event is not None:
            x_at = event.occurred_at
        x0 = x_since(int(x_at)) if x_at else -0.5
        height = max(upper - lower, 1e-9)
        ax.add_patch(Rectangle(
            (x0, lower), max(n - 0.5 - x0, 0.2), height,
            facecolor=color, alpha=0.25, edgecolor=color, linewidth=0.8, zorder=2,
        ))
        if lower == upper:
            text = f"{title} {fmt_price_ru(lower)}"
            anchor = lower
        else:
            text = f"{title} {fmt_price_ru(lower)}–{fmt_price_ru(upper)}"
            anchor = (lower + upper) / 2
        labels.append((anchor, text, color, primary))
        _remember(lower)
        _remember(upper)
        if (legend_name, color) not in legend:
            legend.append((legend_name, color))

    # ---- зоны и уровни ликвидности. Запись события важнее текущих строк.
    payload_zones = []
    if event is not None and payload.get("entries"):
        payload_zones = [z for z in payload["entries"] if isinstance(z, dict)]
    elif event is not None and payload.get("lower") is not None and payload.get("upper") is not None:
        payload_zones = [payload]
    if "entries" in layer_set and payload_zones:
        for zone_row in payload_zones:
            _draw_zone(zone_row, primary=event.kind in (
                "touch", "sweep_confirmed", "sweep_failed",
            ))
    elif "entries" in layer_set and info:
        for zone_row in info["entries"]:
            _draw_zone(zone_row)

    if event is not None:
        idx = 0
        for i, candle in enumerate(candles):
            if candle.open_time <= int(event.occurred_at):
                idx = i
        ax.axvline(idx, color="#e6e9ee", lw=0.7, alpha=0.55, zorder=3)
        main = event_level(payload)
        if main is None and event.kind == "cancellation" and event.scenario_id:
            sc_row = db.get_ltf_scenario(event.scenario_id)
            if sc_row is not None and sc_row.reverse_break_level_price is not None:
                main = float(sc_row.reverse_break_level_price)
        if main is not None:
            kind_name = KIND_RU.get(event.kind, event.kind)
            near_event = abs(main - last_close) <= ((c_hi - c_lo) or 1.0) * 3
            if near_event:
                labels.append((
                    main, f"{kind_name} {fmt_price_ru(main)}", _COLOR_BOS, True,
                ))
                ax.hlines(main, max(idx - 0.2, -0.5), xr, color=_COLOR_BOS,
                          linewidth=1.4, zorder=4)
                ax.plot([idx], [main], marker="o", color=_COLOR_BOS, ms=5, zorder=6)
                extra_levels.append(main)
            else:
                offscale.append(f"{kind_name} {fmt_price_ru(main)}")
        elif event.kind == "cancellation":
            offscale.append("уровень отмены в записи не сохранён")

    forming = end_ms is None and not candles[-1].closed
    price_name = f"Цена {fmt_price_ru(last_close)}"
    if forming:
        price_name += " · не закрыта"
    labels.append((last_close, price_name, "#e6e9ee", True))

    ax.set_xlim(-0.5, xr)
    hi = max(c.high for c in candles)
    lo = min(c.low for c in candles)
    if extra_levels:
        hi = max(hi, max(extra_levels))
        lo = min(lo, min(extra_levels))
    span = (hi - lo) or 1.0
    margin = span * PRICE_MARGIN
    ax.set_ylim(lo - margin, hi + margin)

    symbol = ins.symbol if ins is not None else "?"
    if event is not None:
        title = f"{symbol} · {tf} · {KIND_RU.get(event.kind, event.kind)}"
    else:
        title = f"{symbol} · {tf}"
        if tf == "H1":
            title += f", обзор до {period_days} дн."
    if event is not None:
        when = fmt_time_msk(int(event.occurred_at))
        sc_row = (
            db.get_ltf_scenario(event.scenario_id)
            if getattr(event, "scenario_id", None) else None
        )
        if event.kind == "cancellation":
            reason = LTF_CANCELLATION_RU.get(
                payload.get("reason"),
                payload.get("reason") or "причина в записи не указана",
            )
            state_line = f"{when} · сценарий отменён · {reason}"
        elif (
            sc_row is not None and sc_row.cancelled_at
            and sc_row.cancelled_at > int(event.occurred_at)
        ):
            state_line = f"{when} · на момент события сценарий ещё шёл"
        elif sc_row is not None:
            state_line = (
                f"{when} · {LTF_SCENARIO_STATE_RU.get(sc_row.state, sc_row.state)}"
            )
        else:
            state_line = when
        detected = getattr(event, "detected_at", None)
        if detected and int(detected) > int(event.occurred_at) + 3_600_000:
            state_line += f" · записано {fmt_time_msk(int(detected))}"
    else:
        state_line = f"снимок {fmt_time_msk(now)}"
    window_bits = [f"{n} свечей {tf}"]
    if sliced_to_cause:
        window_bits.append("окно по опоре движения")
    if (
        tf == "H1" and requested_since is not None
        and fetched_first > requested_since + 3_600_000
    ):
        window_bits.append(f"история короче {period_days} дн.")
    elif tf == "H1" and not sliced_to_cause:
        window_bits.append(f"обзор до {period_days} дн.")
    window = (
        " · ".join(window_bits)
        + f"\nокно {fmt_time_msk(candles[0].open_time)}–"
        f"{fmt_time_msk(candles[-1].close_time)}"
    )
    subtitle = f"LevelFrame · {state_line}\n{window}"
    if htf_far_note:
        subtitle += f"\n{htf_far_note}"
    if event is None and setup and setup.get("history"):
        subtitle += "\nисторический снимок"
    if event is None and setup and setup.get("pd_label"):
        subtitle += f"\n{setup.get('label')} · {setup.get('pd_label')}"
    if event is not None and (info or {}).get("limitations"):
        subtitle += "\n" + "; ".join(info["limitations"][:2])
    header_bottom = set_header(fig, title, subtitle)
    apply_layout(fig, ax, header_bottom, footer_lines=4)
    artists = layout_price_labels(fig, ax, labels, fontsize=12)
    dropped = contain_labels(fig, artists)
    card_lines = offscale + dropped
    if card_lines:
        import textwrap
        wrapped: list[str] = []
        for line in card_lines[:8]:
            wrapped.extend(textwrap.wrap(line, 42) or [line])
        ax.text(
            0.012, 0.985, "\n".join(wrapped[:12]),
            transform=ax.transAxes, va="top", ha="left", fontsize=8,
            color="#c5ccd6", zorder=7,
            bbox=dict(boxstyle="round,pad=0.28", fc="#1b212b",
                      ec="#3a4250", alpha=0.92),
        )
    footer = f"Свечи {tf}, время открытия — МСК"
    if event is not None:
        footer = "Цены события — из записи. " + footer
    set_footer(fig, footer)
    if legend:
        add_legend(fig, legend, dashed=tuple(legend_dashed))

    fig.savefig(out, dpi=FIG_DPI)
    plt.close(fig)
    return str(out)


def bot_reversal_view(db, instrument_id: int, as_of: int, cfg=None):
    """Проекция разворота для подписи бота. Та же функция, что у графика и API."""
    from ..services.htf_context import reversal_projection
    return reversal_projection(db, instrument_id, as_of, cfg)
