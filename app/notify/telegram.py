"""Доставка в Telegram (§9) и лог-заглушка для dev/тестов.

Секреты приходят только из Settings/ENV (§11 п.8); при отсутствии токена
используется LogSender / build_application возвращает None.
"""
from __future__ import annotations

import logging
from typing import Optional

from ..config import Settings
from ..db import Database
from ..bot.access import make_owner_guard
from ..models import (
    AlertState,
    Event,
    EventKind,
    Instrument,
    Review,
    Zone,
    now_ms,
)
# Единый источник формулировок — app/texts_ru.py (веб-UI берёт их же
# через /api/labels, чтобы тексты не расходились)
from ..texts_ru import (
    KIND_RU as _KIND_RU,
    TYPE_RU as _TYPE_RU,
)
from .formatting import fmt_pct_ru, fmt_price_ru, fmt_time_msk
from .presenter import NormalizedKind, htf_snapshot
from .queue import MessagePayload

log = logging.getLogger("htf.notify")

# Длительность кнопки «Отложить» (§9). Отдельная от 120 ч подавления повторов:
# это ручная пауза пользователя, а не автоматическое правило §8.
SNOOZE_HOURS = 24

# «Отключить» — фактически навсегда, до ручного включения на сайте (§9).
# NULL в muted_until означал бы «не заглушено», поэтому ставим далёкую дату.
MUTE_FOREVER_MS = 4_102_444_800_000  # 2100-01-01 00:00:00 UTC


def _fmt_price(p: float) -> str:
    """Цена в едином ru-формате (ТЗ 07.10.2026 §5.1): пробелы-тысячи,
    запятая-десятичная, точность по инструменту."""
    return fmt_price_ru(p)


def _fmt_time(ms: int) -> str:
    """Время события — Europe/Moscow (ТЗ 07.10.2026 §6)."""
    return fmt_time_msk(ms)


def _fmt_source(ins: Optional[Instrument]) -> str:
    """Источник данных (площадка/рынок/символ), не внешний график (§9)."""
    if ins is None:
        return "источник неизвестен"
    return f"{ins.venue} {ins.market_type} / {ins.symbol}"


def tradingview_url(ins: Optional[Instrument]) -> Optional[str]:
    """Ссылка TradingView на инструмент — дополняет собственный график (§10)."""
    if ins is None:
        return None
    return f"https://www.tradingview.com/chart/?symbol={ins.venue.upper()}:{ins.symbol}"


def _headline(snap, event: Event) -> str:
    """Строка-утверждение из нормализованного вида события (ТЗ 07.10.2026
    §4–§5): противоречия исключены самим типом — APPROACH только вне зоны,
    LIQUIDITY_TAKEN никогда не «зона входа»."""
    kind, raw = snap.kind, event.kind
    if kind == NormalizedKind.APPROACH:
        return "Цена приближается к зоне."
    if raw == EventKind.TOUCH:
        return "Цена коснулась зоны."
    if raw == EventKind.ALREADY_IN_ZONE:
        return "Цена уже в зоне."
    if raw == EventKind.JUMP_THROUGH:
        return "Зафиксирован проход зоны насквозь."
    if kind == NormalizedKind.DEPTH_50:
        return "Цена достигла середины зоны."
    if kind == NormalizedKind.DEPTH_90:
        return "Цена достигла 90% глубины зоны."
    if kind == NormalizedKind.ZONE_INVALIDATED:
        return {
            EventKind.FVG_FILLED: "FVG полностью заполнен — зона неактуальна.",
            EventKind.OB_INVALIDATED:
                "Зона инвалидирована закрытием за границей.",
            EventKind.BREAKER_ARCHIVED: "Breaker пробит и архивирован.",
            EventKind.PRB_ARCHIVED: "PRB пробит и архивирован.",
        }.get(raw, "Зона больше не актуальна.")
    if kind == NormalizedKind.LIQUIDITY_TAKEN:
        return f"Ликвидность {snap.type_ru} снята."
    return _KIND_RU.get(raw, raw.value) + "."


def _reason(snap, event: Event, approach_pct: float) -> str:
    """Строка «Событие: …» из того же снимка (§5.2/§5.3)."""
    kind, raw = snap.kind, event.kind
    if kind == NormalizedKind.APPROACH:
        return f"приближение; порог {fmt_pct_ru(approach_pct * 100, decimals=0)}"
    if raw == EventKind.TOUCH:
        return "касание ближайшей границы"
    if kind == NormalizedKind.DEPTH_50:
        return "достижение середины (50%)"
    if kind == NormalizedKind.DEPTH_90:
        return "достижение 90% глубины"
    if kind == NormalizedKind.LIQUIDITY_TAKEN:
        return "первое пересечение уровня"
    return _KIND_RU.get(raw, raw.value)


def _render_event_details(view, group_members: Optional[list[Zone]] = None,
                        approach_pct: float = 0.02) -> str:
    """Один блок сообщения (ТЗ 07.10.2026 §5): строка бренда «LevelFrame ·
    символ · площадка рынок» (ребрендинг §8), заголовок «символ · тип ТФ ·
    направление», отдельные строки «Диапазон/Середина/Цена события/Событие/
    Статус/Время события». Без «Обратите внимание», без строки «Источник» —
    метаданные источника остаются внутри снимка и в карточке «Подробнее».
    Для зоны из визуальной группы (§10) — состав объединения."""
    event: Event = view.event
    zone: Optional[Zone] = view.zone
    snap = htf_snapshot(view)

    lines: list[str] = []
    # Ребрендинг (LevelFrame_Rebrand §8): первая строка — бренд и источник
    if snap.venue and snap.market_type:
        lines.append(
            f"LevelFrame · {snap.symbol} · {snap.venue} {snap.market_type}"
        )
    else:
        lines.append(f"LevelFrame · {snap.symbol}")
    if zone is not None:
        head = f"{snap.symbol} · {snap.type_ru} {snap.timeframe}"
        if not snap.is_level:
            head += f" · {snap.direction_ru}"
    else:
        head = f"Событие по {snap.symbol}"
    lines.append(head)
    lines.append(_headline(snap, event))

    if zone is not None:
        if snap.is_level:
            # SSL/BSL: уровень, без выдуманной ширины и середины (§5.1)
            lines.append(f"Уровень: {_fmt_price(snap.lower)}")
        else:
            lines.append(
                f"Диапазон: {_fmt_price(snap.lower)}–{_fmt_price(snap.upper)}"
            )
            lines.append(f"Середина: {_fmt_price(snap.mid)}")

    if snap.event_price is not None:
        lines.append(f"Цена события: {_fmt_price(snap.event_price)}")
    if snap.kind == NormalizedKind.APPROACH and snap.distance_pct is not None:
        lines.append(f"До границы: {fmt_pct_ru(snap.distance_pct * 100)}")

    reason = _reason(snap, event, approach_pct)
    if event.depth > 0 and snap.kind in (
        NormalizedKind.DEPTH_50, NormalizedKind.DEPTH_90,
    ):
        reason += f" (фактическая глубина {fmt_pct_ru(event.depth * 100, decimals=0)})"
    lines.append(f"Событие: {reason}")

    if event.kind == EventKind.FVG_WEAKENED:
        # согласованная модель: 50% FVG — сила снижена на 80%; это не
        # торговая вероятность (HTF-спека §3)
        lines.append(
            "FVG ослаблен: сила снижена на 80% (параметр модели, "
            "не вероятность сделки)."
        )

    if zone is not None:
        if snap.status_ru == "снята" or event.kind == EventKind.LEVEL_TAKEN:
            lines.append(
                "Статус: снята; повторные входы по этому уровню отключены"
            )
        else:
            lines.append(f"Статус: {snap.status_ru}")
    lines.append(f"Время события: {_fmt_time(event.occurred_at)}")

    # §10: зона входит в визуальную группу — перечисляем остальных участников
    if zone is not None and group_members:
        parts = []
        for z in sorted(group_members, key=lambda m: m.id or 0):
            z_type = _TYPE_RU.get(z.type.value, z.type.value)
            if z.is_level:
                parts.append(f"{z_type} {z.timeframe} ({_fmt_price(z.lower)})")
            else:
                parts.append(
                    f"{z_type} {z.timeframe} "
                    f"({_fmt_price(z.lower)}–{_fmt_price(z.upper)})"
                )
        lines.append("Визуально объединена с: " + "; ".join(parts) + ".")

    # §11: восстановленное событие — пометка задержки с исходным временем
    if event.delayed:
        lines.append(
            "⚠ Восстановленное событие (доставлено с задержкой): "
            f"исходное время {_fmt_time(event.occurred_at)}, "
            f"обнаружено {_fmt_time(event.detected_at)}."
        )
    return "\n".join(lines)


def _render_event_block(view, group_members=None, approach_pct=0.02) -> str:
    from .compact import direction_icon
    snap, event = htf_snapshot(view), view.event
    direction = view.zone.direction.value if view.zone else ""
    icon = "⚪" if snap.status_ru in {"снята", "архивирована"} else direction_icon(direction)
    title = f"{icon} {snap.symbol} · {snap.type_ru} {snap.timeframe}"
    if not snap.is_level:
        title += f" · {snap.direction_ru}"
    lines = [title, "🎯 " + _headline(snap, event)]
    if view.zone:
        lines.append(f"Уровень: {_fmt_price(snap.lower)}" if snap.is_level else
                     f"Зона: {_fmt_price(snap.lower)}–{_fmt_price(snap.upper)}")
    if snap.event_price is not None:
        lines.append(f"Цена события: {_fmt_price(snap.event_price)}")
    if snap.kind == NormalizedKind.APPROACH and snap.distance_pct is not None:
        lines.append(f"До границы: {fmt_pct_ru(snap.distance_pct * 100)}")
    if event.kind == EventKind.FVG_WEAKENED:
        lines.append("⚠️ FVG ослаблен")
    if event.kind == EventKind.LEVEL_TAKEN:
        lines.append("Повторные входы по уровню отключены")
    if event.delayed:
        lines.append("⚠️ Историческое событие · доставлено с задержкой")
    lines.append(f"🕒 {_fmt_time(event.occurred_at)}")
    return "\n".join(lines)


def render_text(payload: MessagePayload) -> str:
    """Текст сообщения. Пакет не скрывает второй актив (§9):
    каждое событие — отдельный блок со своим объектом и причиной."""
    blocks = [
        _render_event_block(v, payload.group_members.get(v.event.zone_id),
                            approach_pct=payload.approach_pct)
        for v in payload.views
    ]
    if len(blocks) > 1:
        header = f"📌 Событий: {len(blocks)}"
        return header + "\n\n" + "\n\n".join(blocks)
    return blocks[0] if blocks else ""


# ---------- отправители ----------


class TelegramSender:
    """Отправка через python-telegram-bot (async, v21+)."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        db: Database,
        site_base_url: str = "http://127.0.0.1:8000",
    ):
        from telegram import Bot

        self._bot = Bot(token=token)
        self.chat_id = chat_id
        self.db = db
        self.site_base_url = site_base_url

    async def send_card(self, card, packet_id, *, quiet=False):
        from .compact import notification_html
        from .navigation import card_keyboard
        kwargs = dict(chat_id=self.chat_id, reply_markup=card_keyboard(packet_id, card.targets),
                      disable_notification=quiet, parse_mode="HTML")
        if card.image_path:
            with open(card.image_path, "rb") as fh:
                msg = await self._bot.send_photo(photo=fh, caption=notification_html(card.text, 1000), **kwargs)
        else:
            msg = await self._bot.send_message(text=notification_html(card.text, 4000),
                                                disable_web_page_preview=True, **kwargs)
        return msg.message_id

    async def edit_card(self, message_id, card, packet_id, *, photo=False):
        from .compact import notification_html
        from .navigation import card_keyboard
        from telegram.error import BadRequest
        kwargs = dict(chat_id=self.chat_id, message_id=message_id,
                      reply_markup=card_keyboard(packet_id, card.targets), parse_mode="HTML")
        try:
            if photo:
                await self._bot.edit_message_caption(caption=notification_html(card.text, 1000), **kwargs)
            else:
                await self._bot.edit_message_text(text=notification_html(card.text, 4000), **kwargs)
        except BadRequest as exc:
            if "message is not modified" not in str(exc).lower():
                raise

    def build_keyboard(self, payload: MessagePayload):
        """Inline-кнопки §9 + единый набор ТЗ бота п.10: «График» (рендер
        в чате), «Подробнее» (карточка зоны), «Открыть рабочее место» (URL),
        «Заглушить» (= «Отключить», htf:mute), «🔄 Обновить» (новым
        сообщением); под касанием — «Показать LTF».
        Действия привязаны к зоне первого события пакета."""
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        if not payload.views:
            return None
        view = payload.views[0]
        zone_id = view.event.zone_id
        cycle_id = view.event.cycle_id
        kind = view.event.kind.value

        buttons = [
            [
                InlineKeyboardButton("📊 График", callback_data=f"nav:chartz:{zone_id}"),
                InlineKeyboardButton("Подробнее", callback_data=f"nav:zone:{zone_id}"),
            ],
        ]
        # §12 ТЗ 07.10.2026: localhost/127.0.0.1 в кнопках не отправляем —
        # URL «Открыть рабочее место» только при публичном адресе сервиса
        from urllib.parse import urlparse

        host = urlparse(self.site_base_url).hostname or ""
        if host not in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
            chart_url = f"{self.site_base_url}/?zone={zone_id}"
            buttons.append(
                [InlineKeyboardButton("Открыть рабочее место", url=chart_url)]
            )
        if kind == "touch" and view.zone is not None:
            buttons.append([
                InlineKeyboardButton(
                    "Показать LTF",
                    callback_data=f"nav:ltf:{view.zone.instrument_id}",
                )
            ])
        tv = tradingview_url(view.instrument)
        if tv:
            buttons.append([InlineKeyboardButton("TradingView", url=tv)])
        buttons.append([
            InlineKeyboardButton(
                "Изучаю", callback_data=f"htf:ack:{zone_id}:{cycle_id}:{kind}"
            ),
            InlineKeyboardButton(
                "Отложить", callback_data=f"htf:snooze:{zone_id}:{cycle_id}:{kind}"
            ),
        ])
        buttons.append([
            InlineKeyboardButton(
                "Отключить", callback_data=f"htf:mute:{zone_id}:{cycle_id}:{kind}"
            ),
            InlineKeyboardButton(
                "Добавить заметку",
                callback_data=f"htf:note:{zone_id}:{cycle_id}:{kind}",
            ),
        ])
        if view.zone is not None:
            # «Обновить» — текущее состояние НОВЫМ сообщением, исторический
            # текст сигнала не переписывается (ТЗ п.10)
            buttons.append([
                InlineKeyboardButton(
                    "🔄 Обновить",
                    callback_data=f"nav:refresh:{view.zone.instrument_id}",
                )
            ])
        return InlineKeyboardMarkup(buttons)

    async def send(self, payload: MessagePayload) -> None:
        text = render_text(payload)
        keyboard = self.build_keyboard(payload)
        # Если сгенерирован снимок графика (§11 п.7) — фото с подписью, иначе текст
        if payload.image_path:
            # Лимит подписи к фото — 1024 символа: длинный пакет шлём
            # отдельным текстовым сообщением после снимка
            caption = text if len(text) <= 1024 else None
            with open(payload.image_path, "rb") as fh:
                await self._bot.send_photo(
                    chat_id=self.chat_id, photo=fh, caption=caption,
                    reply_markup=keyboard,
                )
            if caption is None:
                await self._bot.send_message(chat_id=self.chat_id, text=text)
        else:
            await self._bot.send_message(
                chat_id=self.chat_id, text=text, reply_markup=keyboard,
            )

    async def send_text(self, text: str) -> None:
        """Сервисное сообщение без кнопок и снимка (§11)."""
        await self._bot.send_message(chat_id=self.chat_id, text=text)

    async def send_ltf(self, text: str, reply_markup=None) -> None:
        """LTF-сигнал с inline-кнопками навигации (ТЗ бота п.10).
        Превью ссылки отключено: собственный график важнее превью
        TradingView (ТЗ 07.10.2026 §7)."""
        await self._bot.send_message(
            chat_id=self.chat_id, text=text, reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

    async def send_ltf_photo(self, image_path: str, caption: str,
                             reply_markup=None) -> None:
        """LTF-событие с собственным графиком H1 (§7 ТЗ 07.10.2026)."""
        with open(image_path, "rb") as fh:
            await self._bot.send_photo(
                chat_id=self.chat_id, photo=fh, caption=caption,
                reply_markup=reply_markup,
            )

    async def send_alt(self, text: str) -> None:
        """Событие «Altcoins D1 accumulation» (§18 ТЗ 07.10.2026): текст со
        ссылкой на график сетапа; превью ссылки отключено, как у LTF."""
        await self._bot.send_message(
            chat_id=self.chat_id, text=text, disable_web_page_preview=True,
        )


class LogSender:
    """Заглушка без токена: рендерит текст в лог и считается успешной
    доставкой. Для dev-режима и тестов (self.sent — журнал отправок,
    self.sent_ltf — LTF-сообщения с разметкой кнопок)."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self._log = logger or log
        self.sent: list[MessagePayload] = []
        self.sent_ltf: list[tuple[str, object]] = []
        self.sent_ltf_photos: list[tuple[str, str, object]] = []
        self.sent_alt: list[str] = []
        self.cards: list = []
        self.edits: list = []

    async def send_card(self, card, packet_id, *, quiet=False):
        self.cards.append((card, packet_id, quiet))
        self._log.info("NOTIFY %s:\n%s", packet_id, card.text)
        return len(self.cards)

    async def edit_card(self, message_id, card, packet_id, *, photo=False):
        self.edits.append((message_id, card, packet_id))

    async def send(self, payload: MessagePayload) -> None:
        text = render_text(payload)
        self.sent.append(payload)
        self._log.info("NOTIFY (log-delivery):\n%s", text)

    async def send_text(self, text: str) -> None:
        self._log.warning("SERVICE (log-delivery): %s", text)

    async def send_ltf(self, text: str, reply_markup=None) -> None:
        self.sent_ltf.append((text, reply_markup))
        self._log.info("LTF NOTIFY (log-delivery):\n%s", text)

    async def send_ltf_photo(self, image_path: str, caption: str,
                             reply_markup=None) -> None:
        self.sent_ltf_photos.append((image_path, caption, reply_markup))
        self._log.info("LTF PHOTO (log-delivery): %s\n%s", image_path, caption)

    async def send_alt(self, text: str) -> None:
        self.sent_alt.append(text)
        self._log.info("ALT NOTIFY (log-delivery):\n%s", text)


# ---------- приложение бота и обработчики кнопок ----------


def _parse_callback(data: str) -> Optional[tuple[str, int, int, str]]:
    """Формат: htf:<action>:<zone_id>:<cycle_id>:<kind>."""
    parts = (data or "").split(":", 4)
    if len(parts) != 5 or parts[0] != "htf":
        return None
    try:
        return parts[1], int(parts[2]), int(parts[3]), parts[4]
    except ValueError:
        return None


def ensure_mute(db: Database, zone_id: int, cycle_id: int, kind: str, until: int) -> None:
    """Mute всей зоны/цикла. Строки alert_state под некоторые виды событий
    могут ещё не существовать — создаём опорную строку, не сбрасывая
    last_delivered_at уже существующей. Общая логика для htf:-кнопок
    уведомлений и карточек бота (app/bot)."""
    prev = db.get_alert_state(zone_id, cycle_id, kind)
    db.set_alert_state(
        AlertState(
            zone_id=zone_id,
            cycle_id=cycle_id,
            event_kind=kind,
            last_delivered_at=prev.last_delivered_at if prev else 0,
            muted_until=until,
            acknowledged=prev.acknowledged if prev else False,
        )
    )
    db.set_mute(zone_id, cycle_id, until)


def build_application(settings: Settings, db: Database):
    """Application с обработчиками кнопок и заметок. None, если токена нет.

    Доступ ограничен чатом владельца (§11 п.8: приватный сервис).
    """
    if not settings.telegram_token:
        return None

    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        MessageHandler,
        filters,
    )

    from ..bot.handlers import register_bot_handlers

    app = Application.builder().token(settings.telegram_token).build()

    # Единая проверка владельца — app/bot/access.py (используется и новыми
    # хендлерами команд, и старыми кнопками/заметками)
    _is_owner = make_owner_guard(settings)

    async def on_callback(update, context) -> None:
        if not _is_owner(update):
            return
        query = update.callback_query
        parsed = _parse_callback(query.data)
        if parsed is None:
            await query.answer()
            return
        action, zone_id, cycle_id, kind = parsed
        if action == "ack":
            # «Изучаю»: acknowledged=True по всем строкам зоны/цикла (§9)
            db.conn.execute(
                "UPDATE alert_state SET acknowledged=1 "
                "WHERE zone_id=? AND cycle_id=?",
                (zone_id, cycle_id),
            )
            db.conn.commit()
            await query.answer("Отмечено: изучаю.")
        elif action == "snooze":
            until = now_ms() + SNOOZE_HOURS * 3_600_000
            ensure_mute(db, zone_id, cycle_id, kind, until)
            await query.answer(f"Отложено на {SNOOZE_HOURS} ч.")
        elif action == "mute":
            ensure_mute(db, zone_id, cycle_id, kind, MUTE_FOREVER_MS)
            await query.answer("Уведомления по зоне отключены.")
        elif action == "note":
            context.user_data["pending_note_zone"] = zone_id
            await query.answer("Отправьте текст заметки одним сообщением.")
        else:
            await query.answer()

    async def on_text(update, context) -> None:
        if not _is_owner(update):
            return
        zone_id = context.user_data.pop("pending_note_zone", None)
        if zone_id is None:
            # нет pending-заметки — текст может быть кнопкой reply-меню
            if handle_menu_text is not None:
                await handle_menu_text(update, context)
            return
        db.add_review(
            Review(
                id=None,
                zone_id=zone_id,
                decision="note",
                text=update.message.text or "",
                created_at=now_ms(),
            )
        )
        await update.message.reply_text("Заметка сохранена.")

    # Старые хендлеры регистрируются первыми (индексы handlers[0][0..1]
    # используются существующими тестами); конфликтов с новыми нет:
    # MessageHandler исключает команды (~filters.COMMAND), а паттерны
    # callback ^htf: и ^nav: не пересекаются
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^htf:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    from .navigation import register_notification_handlers
    register_notification_handlers(app, db, settings)
    handle_menu_text = register_bot_handlers(app, settings, db)
    return app
