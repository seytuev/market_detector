"""Хендлеры команд и навигации бота.

register_bot_handlers(app, settings, db) вешает CommandHandler'ы и
CallbackQueryHandler(^nav:) и возвращает handle_menu_text — обработчик
кнопок reply-меню, который app/notify/telegram.py вызывает из своего
on_text, когда у пользователя нет pending-заметки. Так текстовых
MessageHandler'ов остаётся один, и конфликта «заметка vs кнопка меню»
нет (PTB в одной группе выполняет только первый подошедший хендлер).

Схема nav-callback:
  nav:now:refresh            — обновить /now
  nav:asset:<iid>            — карточка актива
  nav:htf:<iid>[:<filt>]     — HTF-зоны (filt: d1|w1|all|inside|near)
  nav:zone:<zone_id>         — карточка зоны
  nav:zt:<zone_id>           — история тестов зоны
  nav:zm:<zone_id>           — заглушить уведомления по зоне
  nav:ltf:<iid>              — LTF-сценарий
  nav:entries:<iid>          — подходящие зоны входа
  nav:why:<iid>              — «почему этот сценарий»
  nav:ctx:<iid>              — список контекстов
  nav:ctxsel:<iid>:<obs_id>  — выбрать контекст (как POST select-context)
  nav:chart:<iid>[:<tf>[:<days>]] — диалог графика (ТФ → период → слои)
  nav:charttg:<iid>:<tf>:<days>:<mask>:<bit> — переключить слой (mask^bit)
  nav:chartgo:<iid>:<tf>:<days>:<mask> — рендер и отправка фото
  nav:chartlay:<iid>:<tf>:<days>:<mask> — слои отдельным сообщением (из фото)
  nav:chartz:<zone_id>       — график по зоне (observation или снимок зоны)
  nav:alerts|hist            — заглушки следующих шагов
  mask слоёв: 1=htf, 2=bos, 4=range, 8=entries (app/bot/charts.py LAYER_BITS)
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from ..db import Database
from ..models import now_ms
from ..notify.chartimg import render_zone_chart
from ..notify.telegram import MUTE_FOREVER_MS, _fmt_time, ensure_mute
from ..services.overview import _ACTIVE_STATES, instrument_current
from ..texts_ru import BOT_HELP, BOT_START
from .access import make_owner_guard
from .cards import (
    _ACTIVE_ZONE_STATUSES,
    _clip,
    render_asset,
    render_chart_caption,
    render_contexts,
    render_entries,
    render_history,
    render_htf,
    render_ltf,
    render_now,
    render_status,
    render_watchlist,
    render_why,
    render_zone,
    render_zone_tests,
    split_long,
)
from .charts import (
    DEFAULT_MASK, layers_from_mask, render_ltf_chart, screenshot_candles,
)
from .keyboards import (
    MENU_ALERTS,
    MENU_HIST,
    MENU_HTF,
    MENU_LTF,
    MENU_NOW,
    MENU_OPEN_APP,
    MENU_PICK,
    MENU_STATUS,
    alert_kinds_inline,
    alerts_groups_inline,
    alerts_instrument_groups_inline,
    asset_inline,
    chart_layers_inline,
    chart_period_inline,
    chart_result_inline,
    chart_tf_inline,
    contexts_inline,
    history_period_inline,
    history_result_inline,
    history_type_inline,
    htf_inline,
    instruments_inline,
    ltf_back_inline,
    ltf_inline,
    main_menu_keyboard,
    now_inline,
    watchlist_add_inline,
    watchlist_inline,
    watchlist_remove_confirm_inline,
    zone_inline,
    zone_tests_inline,
)

log = logging.getLogger("htf.bot")


def _resolve_instruments(db: Database, query: str) -> list:
    """Инструменты по symbol/asset (регистр не важен): сначала точное
    совпадение, затем вхождение подстроки. Несколько совпадений (разные
    биржи/рынки) — повод для inline-выбора."""
    q = query.strip().lower()
    instruments = db.get_instruments()
    exact = [
        i for i in instruments
        if i.symbol.lower() == q or i.asset.lower() == q
    ]
    if exact:
        return exact
    return [
        i for i in instruments
        if q in i.symbol.lower() or q in i.asset.lower()
    ]


def _htf_zones(db: Database, instrument_id: int, filt: str) -> list:
    zones = db.get_zones(
        instrument_id=instrument_id, statuses=_ACTIVE_ZONE_STATUSES
    )
    # ТЗ 06.10.2026 §13 (T21): единый canonical state актуальности
    zones = [z for z in zones if z.is_currently_relevant()]
    if filt in ("d1", "w1"):
        zones = [z for z in zones if z.timeframe == filt.upper()]
    return sorted(zones, key=lambda z: (z.timeframe, z.lower))


def _chat_id(update) -> str:
    """chat_id владельца — ключ watchlist/prefs/mutes (модель «один владелец»)."""
    chat = update.effective_chat
    return str(chat.id) if chat is not None else ""


def _parse_duration_ms(token: str) -> int | None:
    """Длительность мьюта: Nh (часы) или Nd (дни). None — мусор."""
    token = (token or "").strip().lower()
    try:
        if token.endswith("h"):
            return int(token[:-1]) * 3_600_000
        if token.endswith("d"):
            return int(token[:-1]) * 86_400_000
    except ValueError:
        return None
    return None


def register_bot_handlers(app, settings, db: Database):
    """Команды и nav-callback. Возвращает обработчик текстов кнопок
    reply-меню (для единого MessageHandler в telegram.py)."""
    from telegram.ext import CallbackQueryHandler, CommandHandler

    is_owner = make_owner_guard(settings)

    async def _reply(message, text: str, markup=None) -> None:
        """Ответ с разбивкой длинного текста; клавиатура — на последней части."""
        parts = split_long(text)
        for part in parts[:-1]:
            await message.reply_text(part)
        await message.reply_text(parts[-1], reply_markup=markup)

    async def _cmd_with_instrument(update, context, prefix, render_fn, markup_fn) -> None:
        """Команда с аргументом-инструментом: без аргумента — выбор кнопками;
        неоднозначность — inline-выбор из совпавших."""
        if not is_owner(update):
            return
        args = getattr(context, "args", None) or []
        if not args:
            await update.message.reply_text(
                "Выберите актив:", reply_markup=instruments_inline(db, prefix)
            )
            return
        query = " ".join(args)
        matches = _resolve_instruments(db, query)
        if not matches:
            await update.message.reply_text(f"Инструмент «{query}» не найден.")
            return
        if len(matches) > 1:
            await update.message.reply_text(
                "Несколько совпадений — выберите инструмент:",
                reply_markup=instruments_inline(db, prefix, matches),
            )
            return
        iid = matches[0].id
        await _reply(update.message, render_fn(iid), markup_fn(iid))

    # ------------------------------ команды ------------------------------

    async def cmd_start(update, context) -> None:
        if not is_owner(update):
            # молчание + лог: чужим не сообщаем даже о существовании бота
            chat = update.effective_chat
            log.warning(
                "бот: /start из чужого чата %s — проигнорировано",
                chat.id if chat else None,
            )
            return
        # ТЗ п.8: первичное наполнение watchlist из включённых инструментов
        db.seed_watchlist(_chat_id(update))
        await update.message.reply_text(
            BOT_START, reply_markup=main_menu_keyboard()
        )

    async def cmd_help(update, context) -> None:
        if not is_owner(update):
            return
        await update.message.reply_text(BOT_HELP)

    async def cmd_now(update, context) -> None:
        if not is_owner(update):
            return
        await update.message.reply_text(
            render_now(db, settings, _chat_id(update)),
            reply_markup=now_inline(db),
        )

    async def cmd_asset(update, context) -> None:
        await _cmd_with_instrument(
            update, context, "nav:asset",
            lambda iid: render_asset(db, settings, iid),
            lambda iid: asset_inline(db, settings, iid),
        )

    async def cmd_htf(update, context) -> None:
        await _cmd_with_instrument(
            update, context, "nav:htf",
            lambda iid: render_htf(db, settings, iid),
            lambda iid: htf_inline(db, iid, _htf_zones(db, iid, "all")),
        )

    async def cmd_ltf(update, context) -> None:
        await _cmd_with_instrument(
            update, context, "nav:ltf",
            lambda iid: render_ltf(db, settings, iid),
            lambda iid: ltf_inline(db, iid),
        )

    # ------------------------------ /chart ------------------------------

    def _chart_out(name: str) -> str:
        # снимки — рядом с БД, как у EventDispatcher (§11 п.7)
        return str(Path(settings.db_path).parent / "charts" / name)

    def _auto_days(observation_id: int | None, now: int) -> tuple[int, bool]:
        """Период по умолчанию (ТЗ 07.10.2026 §8): 3 дня; если HTF-касание
        (activated_at наблюдения) лежит раньше — автоматически 7/14 дней.
        (days, outside): outside=True — начало движения всё равно вне окна."""
        if observation_id is None:
            return 3, False
        obs = db.get_ltf_observation(observation_id)
        if obs is None or not obs.activated_at:
            return 3, False
        elapsed_days = (now - obs.activated_at) / 86_400_000
        if elapsed_days <= 3:
            return 3, False
        if elapsed_days <= 7:
            return 7, False
        return 14, elapsed_days > 14

    async def _send_chart(
        message, instrument_id: int, tf: str, days: int, mask: int,
        observation_id: int | None = None,
    ) -> None:
        """Один снимок: instrument_current вызывается один раз, тот же now
        идёт и в рендер картинки, и в caption — цена/сценарий/время совпадают.
        Один график выбранного контекста, без серий изображений."""
        now = now_ms()
        cur = instrument_current(db, settings, instrument_id)
        if cur is None:
            await message.reply_text("Инструмент не найден.")
            return
        obs_id = observation_id or cur["selected_context_id"]
        if obs_id is None:
            await message.reply_text(
                "Нет активного контекста — недостаточно данных для графика."
            )
            return
        ins = cur["instrument"]
        source = f"{ins['venue']} {ins['market_type']} / {ins['symbol']}"
        out = _chart_out(f"ltf_bot_{obs_id}_{tf}_{days}_{mask}_{now}.png")
        path = await asyncio.to_thread(
            render_ltf_chart,
            db, obs_id, out,
            tf=tf, period_days=days, layers=layers_from_mask(mask),
            source_label=source, settings=settings, now=now,
        )
        if path is None:
            await message.reply_text("Недостаточно данных для графика.")
            return
        caption = render_chart_caption(cur, now, tf, days)
        if observation_id is not None:
            # контекст конкретного сообщения (§8): отменённый сценарий не
            # воскрешается — показываем факт отмены (§11.2)
            obs = db.get_ltf_observation(observation_id)
            if obs is not None and obs.state not in _ACTIVE_STATES:
                caption += (
                    "\n⚠ Контекст этого сообщения уже неактивен — график "
                    "показан по состоянию на текущий снимок; актуальный "
                    "сценарий — кнопкой «Зоны входа»."
                )
        _, outside = _auto_days(obs_id, now)
        if tf == "H1" and outside:
            caption += "\nНачало движения вне окна — полное движение доступно в приложении."
        with open(path, "rb") as fh:
            await message.reply_photo(
                photo=fh, caption=caption,
                reply_markup=chart_result_inline(
                    instrument_id, tf, days, mask,
                    settings=settings, instrument=ins,
                ),
            )

    async def _send_zone_snapshot(message, zone) -> None:
        """Fallback nav:chartz без LTF-наблюдения: снимок самой HTF-зоны
        (render_zone_chart — тот же рендер, что в уведомлениях)."""
        ins = db.get_instrument(zone.instrument_id)
        source = (
            f"{ins.venue} {ins.market_type} / {ins.symbol}"
            if ins is not None else "источник неизвестен"
        )
        candles = screenshot_candles(
            db, zone.instrument_id, zone.timeframe, limit=120,
        )
        if not candles:
            await message.reply_text("Недостаточно данных для графика.")
            return
        out = _chart_out(f"zone_{zone.id}_{now_ms()}.png")
        path = await asyncio.to_thread(
            render_zone_chart, candles, zone, out, source
        )
        with open(path, "rb") as fh:
            await message.reply_photo(photo=fh, reply_markup=zone_inline(zone))

    async def cmd_chart(update, context) -> None:
        if not is_owner(update):
            return
        args = getattr(context, "args", None) or []
        if not args:
            await update.message.reply_text(
                "График: выберите актив",
                reply_markup=instruments_inline(db, "nav:chart"),
            )
            return
        matches = _resolve_instruments(db, args[0])
        if not matches:
            await update.message.reply_text(f"Инструмент «{args[0]}» не найден.")
            return
        if len(matches) > 1:
            await update.message.reply_text(
                "Несколько совпадений — выберите инструмент:",
                reply_markup=instruments_inline(db, "nav:chart", matches),
            )
            return
        iid = matches[0].id
        tf = args[1].upper() if len(args) > 1 else ""
        if tf not in ("H1", "D1", "W1"):
            await update.message.reply_text(
                "Таймфрейм:", reply_markup=chart_tf_inline(iid)
            )
            return
        # /chart BTC H1 — сразу дефолт: 7 дней (для старших ТФ — авто), все слои
        days = 7 if tf == "H1" else 0
        await _send_chart(update.message, iid, tf, days, DEFAULT_MASK)

    # -------------------- watchlist / alerts / mute (ТЗ п.8, п.9) --------------------

    async def _show_watchlist(message, chat_id: str) -> None:
        await message.reply_text(
            render_watchlist(db, chat_id),
            reply_markup=watchlist_inline(db, chat_id),
        )

    async def cmd_watchlist(update, context) -> None:
        if not is_owner(update):
            return
        await _show_watchlist(update.message, _chat_id(update))

    async def cmd_add(update, context) -> None:
        if not is_owner(update):
            return
        args = getattr(context, "args", None) or []
        if not args:
            await update.message.reply_text("Формат: /add BTC")
            return
        matches = _resolve_instruments(db, args[0])
        if not matches:
            await update.message.reply_text(f"Инструмент «{args[0]}» не найден.")
            return
        if len(matches) > 1:
            await update.message.reply_text(
                "Несколько совпадений — выберите инструмент:",
                reply_markup=instruments_inline(db, "nav:waddok", matches),
            )
            return
        ins = matches[0]
        db.watchlist_add(_chat_id(update), ins.id)
        await _show_watchlist(update.message, _chat_id(update))

    async def cmd_remove(update, context) -> None:
        if not is_owner(update):
            return
        args = getattr(context, "args", None) or []
        if not args:
            await update.message.reply_text("Формат: /remove BTC")
            return
        matches = _resolve_instruments(db, args[0])
        if not matches:
            await update.message.reply_text(f"Инструмент «{args[0]}» не найден.")
            return
        if len(matches) > 1:
            await update.message.reply_text(
                "Несколько совпадений — выберите инструмент:",
                reply_markup=instruments_inline(db, "nav:wrmok", matches),
            )
            return
        ins = matches[0]
        # трогаем только bot_watchlist — зоны/события/история не затрагиваются
        db.watchlist_remove(_chat_id(update), ins.id)
        await _show_watchlist(update.message, _chat_id(update))

    async def cmd_alerts(update, context) -> None:
        if not is_owner(update):
            return
        await update.message.reply_text(
            "Группы уведомлений (глобально):",
            reply_markup=alerts_groups_inline(),
        )

    async def cmd_mute(update, context) -> None:
        if not is_owner(update):
            return
        chat_id = _chat_id(update)
        args = getattr(context, "args", None) or []
        if not args:
            mutes = db.get_mutes(chat_id, now_ms())
            lines = ["Активные заглушения:" if mutes
                     else "Активных заглушений нет."]
            for m in mutes:
                lines.append(
                    f"• {m['scope']} {m['scope_ref']} — до {_fmt_time(m['until'])}"
                )
            lines.append("Формат: /mute BTC [8h|2d], /mute all [8h]")
            await update.message.reply_text("\n".join(lines))
            return
        dur_token = args[1] if len(args) > 1 else "8h"  # дефолт — 8 часов
        ms = _parse_duration_ms(dur_token)
        if ms is None:
            await update.message.reply_text(
                "Длительность: Nh или Nd (например 8h, 2d)."
            )
            return
        until = now_ms() + ms
        if args[0].lower() == "all":
            db.set_mute_scope(chat_id, "all", "", until)
            await update.message.reply_text(
                f"Все торговые уведомления заглушены до {_fmt_time(until)}. "
                "Сервисные продолжают приходить."
            )
            return
        matches = _resolve_instruments(db, args[0])
        if not matches:
            await update.message.reply_text(f"Инструмент «{args[0]}» не найден.")
            return
        if len(matches) > 1:
            await update.message.reply_text(
                "Несколько совпадений — выберите инструмент:",
                reply_markup=instruments_inline(db, f"nav:mutei:{dur_token}", matches),
            )
            return
        ins = matches[0]
        db.set_mute_scope(chat_id, "instrument", str(ins.id), until)
        await update.message.reply_text(
            f"Уведомления по {ins.symbol} · {ins.venue} · {ins.market_type} "
            f"заглушены до {_fmt_time(until)}."
        )

    async def cmd_unmute(update, context) -> None:
        if not is_owner(update):
            return
        chat_id = _chat_id(update)
        args = getattr(context, "args", None) or []
        if not args:
            await update.message.reply_text("Формат: /unmute BTC | /unmute all")
            return
        if args[0].lower() == "all":
            db.clear_mute_scope(chat_id, "all", "")
            await update.message.reply_text("Все торговые уведомления включены.")
            return
        matches = _resolve_instruments(db, args[0])
        if not matches:
            await update.message.reply_text(f"Инструмент «{args[0]}» не найден.")
            return
        if len(matches) > 1:
            await update.message.reply_text(
                "Несколько совпадений — выберите инструмент:",
                reply_markup=instruments_inline(db, "nav:unmutei", matches),
            )
            return
        ins = matches[0]
        db.clear_mute_scope(chat_id, "instrument", str(ins.id))
        await update.message.reply_text(
            f"Уведомления по {ins.symbol} · {ins.venue} · {ins.market_type} "
            "включены."
        )

    # -------------------- history / status (ТЗ п.11) --------------------

    async def cmd_history(update, context) -> None:
        if not is_owner(update):
            return
        args = getattr(context, "args", None) or []
        if not args:
            await update.message.reply_text(
                "История: выберите актив",
                reply_markup=instruments_inline(db, "nav:hist"),
            )
            return
        matches = _resolve_instruments(db, args[0])
        if not matches:
            await update.message.reply_text(f"Инструмент «{args[0]}» не найден.")
            return
        if len(matches) > 1:
            await update.message.reply_text(
                "Несколько совпадений — выберите инструмент:",
                reply_markup=instruments_inline(db, "nav:hist", matches),
            )
            return
        await update.message.reply_text(
            "Период:", reply_markup=history_period_inline(matches[0].id)
        )

    async def cmd_status(update, context) -> None:
        if not is_owner(update):
            return
        await _reply(update.message, render_status(db, settings))

    # ------------------------------ nav-callback ------------------------------

    async def on_nav_callback(update, context) -> None:
        if not is_owner(update):
            return
        query = update.callback_query
        data = query.data or ""
        parts = data.split(":")

        async def edit(text: str, markup=None) -> None:
            # edit — одно сообщение: длинный текст режем до первой порции
            await query.edit_message_text(_clip(text), reply_markup=markup)

        try:
            if data == "nav:now:refresh":
                await edit(
                    render_now(db, settings, _chat_id(update)), now_inline(db)
                )
                await query.answer()
                return
            kind = parts[1] if len(parts) > 1 else ""
            # ---------- watchlist (ТЗ п.8) ----------
            if kind == "wlist":
                chat_id = _chat_id(update)
                await edit(
                    render_watchlist(db, chat_id),
                    watchlist_inline(db, chat_id),
                )
                await query.answer()
            elif kind == "wal":
                chat_id = _chat_id(update)
                iid = int(parts[2])
                cur = next(
                    (w for w in db.list_watchlist(chat_id)
                     if w["instrument_id"] == iid),
                    None,
                )
                if cur is not None:
                    db.watchlist_set_alerts(chat_id, iid,
                                            not cur["alerts_enabled"])
                await edit(
                    render_watchlist(db, chat_id),
                    watchlist_inline(db, chat_id),
                )
                await query.answer()
            elif kind == "wrm":
                iid = int(parts[2])
                ins = db.get_instrument(iid)
                label = (
                    f"{ins.symbol} · {ins.venue} · {ins.market_type}"
                    if ins is not None else f"#{iid}"
                )
                await edit(
                    f"Удалить {label} из списка наблюдения? "
                    "Зоны и история не затрагиваются.",
                    watchlist_remove_confirm_inline(iid),
                )
                await query.answer()
            elif kind == "wrmok":
                chat_id = _chat_id(update)
                db.watchlist_remove(chat_id, int(parts[2]))
                await edit(
                    render_watchlist(db, chat_id),
                    watchlist_inline(db, chat_id),
                )
                await query.answer("Удалено из списка.")
            elif kind == "wadd":
                chat_id = _chat_id(update)
                await edit(
                    "Добавить в список наблюдения:",
                    watchlist_add_inline(db, chat_id),
                )
                await query.answer()
            elif kind == "waddok":
                chat_id = _chat_id(update)
                db.watchlist_add(chat_id, int(parts[2]))
                await edit(
                    render_watchlist(db, chat_id),
                    watchlist_inline(db, chat_id),
                )
                await query.answer("Добавлено в список.")
            # ---------- настройки уведомлений (ТЗ п.9) ----------
            elif kind == "alerts" and len(parts) == 2:
                await edit(
                    "Группы уведомлений (глобально):", alerts_groups_inline()
                )
                await query.answer()
            elif kind == "alerts":
                # «Уведомления» из карточки актива — группы по этому активу
                iid = int(parts[2])
                ins = db.get_instrument(iid)
                label = ins.symbol if ins is not None else f"#{iid}"
                await edit(
                    f"Уведомления по {label} — выберите группу:",
                    alerts_instrument_groups_inline(iid),
                )
                await query.answer()
            elif kind == "algrp":
                grp = parts[2]
                await edit(
                    f"Группа «{grp}» (глобально):",
                    alert_kinds_inline(db, _chat_id(update), grp),
                )
                await query.answer()
            elif kind == "altg":
                chat_id = _chat_id(update)
                grp, ev_kind = parts[2], parts[3]
                cur = db.alert_pref_enabled(chat_id, "global", "", grp, ev_kind)
                db.set_alert_pref(chat_id, "global", "", grp, ev_kind, not cur)
                await edit(
                    f"Группа «{grp}» (глобально):",
                    alert_kinds_inline(db, chat_id, grp),
                )
                await query.answer()
            elif kind == "alins":
                grp = parts[2]
                await edit(
                    f"Группа «{grp}» по активу — выберите:",
                    instruments_inline(db, f"nav:alinsi:{grp}"),
                )
                await query.answer()
            elif kind == "alinsi":
                grp, iid = parts[2], parts[3]
                await edit(
                    f"Группа «{grp}» по активу:",
                    alert_kinds_inline(
                        db, _chat_id(update), grp,
                        scope="instrument", scope_ref=iid,
                        back=f"nav:alins:{grp}",
                    ),
                )
                await query.answer()
            elif kind == "altgi":
                chat_id = _chat_id(update)
                grp, ev_kind, iid = parts[2], parts[3], parts[4]
                cur = db.alert_pref_enabled(
                    chat_id, "instrument", iid, grp, ev_kind
                )
                db.set_alert_pref(
                    chat_id, "instrument", iid, grp, ev_kind, not cur
                )
                await edit(
                    f"Группа «{grp}» по активу:",
                    alert_kinds_inline(
                        db, chat_id, grp,
                        scope="instrument", scope_ref=iid,
                        back=f"nav:alins:{grp}",
                    ),
                )
                await query.answer()
            elif kind == "alz":
                # «Уведомления зоны» — уровень контекста (scope=context)
                zid = parts[2]
                await edit(
                    "Уведомления зоны (HTF):",
                    alert_kinds_inline(
                        db, _chat_id(update), "htf",
                        scope="context", scope_ref=zid,
                        back=f"nav:zone:{zid}",
                    ),
                )
                await query.answer()
            elif kind == "altgc":
                chat_id = _chat_id(update)
                ev_kind, zid = parts[2], parts[3]
                cur = db.alert_pref_enabled(
                    chat_id, "context", zid, "htf", ev_kind
                )
                db.set_alert_pref(
                    chat_id, "context", zid, "htf", ev_kind, not cur
                )
                await edit(
                    "Уведомления зоны (HTF):",
                    alert_kinds_inline(
                        db, chat_id, "htf",
                        scope="context", scope_ref=zid,
                        back=f"nav:zone:{zid}",
                    ),
                )
                await query.answer()
            # ---------- мьютинг (ТЗ п.9) ----------
            elif kind == "mutei":
                chat_id = _chat_id(update)
                dur_token, iid = parts[2], parts[3]
                ms = _parse_duration_ms(dur_token)
                ins = db.get_instrument(int(iid))
                if ms is None or ins is None:
                    await query.answer("Некорректные параметры.")
                    return
                until = now_ms() + ms
                db.set_mute_scope(chat_id, "instrument", str(ins.id), until)
                await edit(
                    f"Уведомления по {ins.symbol} · {ins.venue} · "
                    f"{ins.market_type} заглушены до {_fmt_time(until)}."
                )
                await query.answer()
            elif kind == "unmutei":
                chat_id = _chat_id(update)
                iid = int(parts[2])
                ins = db.get_instrument(iid)
                db.clear_mute_scope(chat_id, "instrument", str(iid))
                label = ins.symbol if ins is not None else f"#{iid}"
                await edit(f"Уведомления по {label} включены.")
                await query.answer()
            elif kind == "asset":
                iid = int(parts[2])
                await edit(
                    render_asset(db, settings, iid),
                    asset_inline(db, settings, iid),
                )
                await query.answer()
            elif kind == "htf":
                iid = int(parts[2])
                filt = parts[3] if len(parts) > 3 else "all"
                await edit(
                    render_htf(db, settings, iid, filt),
                    htf_inline(db, iid, _htf_zones(db, iid, filt), filt),
                )
                await query.answer()
            elif kind == "zone":
                zid = int(parts[2])
                z = db.get_zone(zid)
                if z is None:
                    await query.answer("Зона не найдена.")
                    return
                await edit(render_zone(db, settings, zid), zone_inline(z))
                await query.answer()
            elif kind == "zt":
                zid = int(parts[2])
                z = db.get_zone(zid)
                if z is None:
                    await query.answer("Зона не найдена.")
                    return
                await edit(render_zone_tests(db, zid), zone_tests_inline(z))
                await query.answer()
            elif kind == "zm":
                zid = int(parts[2])
                z = db.get_zone(zid)
                if z is None:
                    await query.answer("Зона не найдена.")
                    return
                # общая с htf:-кнопками логика mute (app/notify/telegram.py)
                ensure_mute(db, z.id, z.cycle_id, "touch", MUTE_FOREVER_MS)
                await query.answer("Уведомления по зоне отключены.")
            elif kind == "ltf":
                iid = int(parts[2])
                await edit(render_ltf(db, settings, iid), ltf_inline(db, iid))
                await query.answer()
            elif kind == "entries":
                iid = int(parts[2])
                await edit(
                    render_entries(db, settings, iid), ltf_back_inline(iid)
                )
                await query.answer()
            elif kind == "why":
                iid = int(parts[2])
                await edit(render_why(db, settings, iid), ltf_back_inline(iid))
                await query.answer()
            elif kind == "ctx":
                iid = int(parts[2])
                cur = instrument_current(db, settings, iid)
                if cur is None:
                    await query.answer("Инструмент не найден.")
                    return
                await edit(
                    render_contexts(db, settings, iid),
                    contexts_inline(iid, cur["contexts"]),
                )
                await query.answer()
            elif kind == "ctxsel":
                iid, obs_id = int(parts[2]), int(parts[3])
                obs = db.get_ltf_observation(obs_id)
                if (obs is None or obs.instrument_id != iid
                        or obs.state not in _ACTIVE_STATES):
                    await query.answer("Контекст недоступен.")
                    return
                # как POST select-context: ручной выбор держится в meta
                db.set_meta(f"ltf:selected_context:{iid}", str(obs_id))
                await edit(render_ltf(db, settings, iid), ltf_inline(db, iid))
                await query.answer("Контекст выбран.")
            elif kind == "refresh":
                # «🔄 Обновить» под сигналом: текущая карточка НОВЫМ
                # сообщением — исторический текст сигнала не редактируется
                iid = int(parts[2])
                await query.message.reply_text(
                    render_asset(db, settings, iid),
                    reply_markup=asset_inline(db, settings, iid),
                )
                await query.answer()
            elif kind == "chart":
                iid = int(parts[2])
                if len(parts) == 3:
                    await edit("Таймфрейм:", chart_tf_inline(iid))
                else:
                    # ТЗ 07.10.2026 §8: открытие графика за одно нажатие —
                    # сразу рендер с периодом/слоями по умолчанию; период и
                    # слои меняются кнопками ПОСЛЕ выдачи графика
                    tf = parts[3]
                    days, _ = (
                        _auto_days(None, now_ms()) if tf == "H1" else (0, False)
                    )
                    await _send_chart(query.message, iid, tf, days, DEFAULT_MASK)
                await query.answer()
            elif kind == "charto":
                # «График» из конкретного LTF-сообщения: контекст ЭТОГО
                # события (observation), а не «первый активный сценарий» (§8)
                iid, obs_id = int(parts[2]), int(parts[3])
                obs = db.get_ltf_observation(obs_id)
                if obs is None or obs.instrument_id != iid:
                    await query.answer("Контекст недоступен.")
                    return
                days, _ = _auto_days(obs_id, now_ms())
                await _send_chart(
                    query.message, iid, "H1", days, DEFAULT_MASK,
                    observation_id=obs_id,
                )
                await query.answer()
            elif kind == "chartlay":
                # из фото-сообщения текст не редактируется — слои новым сообщением
                iid, tf, days, mask = (
                    int(parts[2]), parts[3], int(parts[4]), int(parts[5])
                )
                await query.message.reply_text(
                    "Слои:", reply_markup=chart_layers_inline(iid, tf, days, mask)
                )
                await query.answer()
            elif kind == "charttg":
                iid, tf, days, mask, bit = (
                    int(parts[2]), parts[3], int(parts[4]),
                    int(parts[5]), int(parts[6]),
                )
                await edit(
                    "Слои:", chart_layers_inline(iid, tf, days, mask ^ bit)
                )
                await query.answer()
            elif kind == "chartgo":
                iid, tf, days, mask = (
                    int(parts[2]), parts[3], int(parts[4]), int(parts[5])
                )
                await _send_chart(query.message, iid, tf, days, mask)
                await query.answer()
            elif kind == "chartz":
                zid = int(parts[2])
                z = db.get_zone(zid)
                if z is None:
                    await query.answer("Зона не найдена.")
                    return
                # график H1 по observation этой зоны; без наблюдения —
                # снимок самой зоны (render_zone_chart)
                obs = db.get_ltf_observation_by_zone(z.id, z.cycle_id)
                if obs is not None:
                    await _send_chart(
                        query.message, z.instrument_id, "H1", 7, DEFAULT_MASK,
                        observation_id=obs.id,
                    )
                else:
                    await _send_zone_snapshot(query.message, z)
                await query.answer()
            elif kind == "hist":
                # фильтры истории: актив → период → тип события (ТЗ п.11)
                iid = int(parts[2])
                await edit("Период:", history_period_inline(iid))
                await query.answer()
            elif kind == "histp":
                iid, hours = int(parts[2]), int(parts[3])
                await edit("Тип события:", history_type_inline(iid, hours))
                await query.answer()
            elif kind == "histt":
                iid, hours, htype = int(parts[2]), int(parts[3]), parts[4]
                await edit(
                    render_history(db, settings, iid, hours, htype),
                    history_result_inline(iid),
                )
                await query.answer()
            else:
                await query.answer()
        except (IndexError, ValueError):
            await query.answer()

    # ------------------------- кнопки reply-меню -------------------------

    async def handle_menu_text(update, context) -> None:
        """Кнопки reply-меню (обычный текст, не команда). Owner уже проверен
        вызывающим on_text; pending-заметка имеет приоритет над меню."""
        text = (update.message.text or "").strip()
        if text == MENU_NOW:
            await update.message.reply_text(
                render_now(db, settings, _chat_id(update)),
                reply_markup=now_inline(db),
            )
        elif text == MENU_PICK:
            # watchlist владельца (ТЗ п.8); пустой список — все инструменты
            wl_ids = [
                w["instrument_id"] for w in db.list_watchlist(_chat_id(update))
            ]
            instruments = (
                [db.get_instrument(i) for i in wl_ids] if wl_ids else None
            )
            if instruments is not None:
                instruments = [i for i in instruments if i is not None]
            await update.message.reply_text(
                "Выберите актив:",
                reply_markup=instruments_inline(
                    db, "nav:asset", instruments or None
                ),
            )
        elif text == MENU_HTF:
            await update.message.reply_text(
                "HTF-зоны: выберите актив",
                reply_markup=instruments_inline(db, "nav:htf"),
            )
        elif text == MENU_LTF:
            await update.message.reply_text(
                "LTF-сценарии: выберите актив",
                reply_markup=instruments_inline(db, "nav:ltf"),
            )
        elif text == MENU_OPEN_APP:
            await update.message.reply_text(
                f"Рабочее место: {settings.effective_base_url()}"
            )
        elif text == MENU_ALERTS:
            await update.message.reply_text(
                "Группы уведомлений (глобально):",
                reply_markup=alerts_groups_inline(),
            )
        elif text == MENU_HIST:
            await update.message.reply_text(
                "История: выберите актив",
                reply_markup=instruments_inline(db, "nav:hist"),
            )
        elif text == MENU_STATUS:
            await _reply(update.message, render_status(db, settings))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("now", cmd_now))
    app.add_handler(CommandHandler("asset", cmd_asset))
    app.add_handler(CommandHandler("htf", cmd_htf))
    app.add_handler(CommandHandler("ltf", cmd_ltf))
    app.add_handler(CommandHandler("chart", cmd_chart))
    app.add_handler(CommandHandler("watchlist", cmd_watchlist))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("alerts", cmd_alerts))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_nav_callback, pattern=r"^nav:"))
    return handle_menu_text
