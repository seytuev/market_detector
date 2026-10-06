"""Доставка в Telegram (§9) и лог-заглушка для dev/тестов.

Секреты приходят только из Settings/ENV (§11 п.8); при отсутствии токена
используется LogSender / build_application возвращает None.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
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
    DIRECTION_RU as _DIRECTION_RU,
    KIND_RU as _KIND_RU,
    STATUS_RU as _STATUS_RU,
    TYPE_RU as _TYPE_RU,
)
from .queue import MessagePayload

log = logging.getLogger("htf.notify")

# Длительность кнопки «Отложить» (§9). Отдельная от 120 ч подавления повторов:
# это ручная пауза пользователя, а не автоматическое правило §8.
SNOOZE_HOURS = 24

# «Отключить» — фактически навсегда, до ручного включения на сайте (§9).
# NULL в muted_until означал бы «не заглушено», поэтому ставим далёкую дату.
MUTE_FOREVER_MS = 4_102_444_800_000  # 2100-01-01 00:00:00 UTC


def _fmt_price(p: float) -> str:
    """Цена с точностью инструмента: без лишних нулей, но без потери знаков."""
    if p != p:  # NaN
        return "?"
    if abs(p) >= 100:
        return f"{p:,.2f}"
    if abs(p) >= 1:
        return f"{p:.4f}".rstrip("0").rstrip(".")
    return f"{p:.8f}".rstrip("0").rstrip(".")


def _fmt_time(ms: int) -> str:
    """Время события с явным часовым поясом (§9)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


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


def _render_event_block(view, group_members: Optional[list[Zone]] = None) -> str:
    """Один блок сообщения по §9: актив, тип/направление/ТФ, границы L–U,
    середина M, цена, причина, источник, время с поясом, статус зоны.
    Для зоны из визуальной группы (§10) — состав объединения."""
    event: Event = view.event
    zone: Optional[Zone] = view.zone
    ins: Optional[Instrument] = view.instrument

    asset = ins.asset if ins else f"зона #{event.zone_id}"
    if zone is not None:
        type_ru = _TYPE_RU.get(zone.type.value, zone.type.value)
        dir_ru = _DIRECTION_RU.get(zone.direction.value, zone.direction.value)
        head = f"Цена на {asset} пришла в {type_ru} {zone.timeframe} ({dir_ru})."
    else:
        head = f"Событие по {asset}."
    lines = [head + " Обратите внимание."]

    if zone is not None:
        if zone.is_level:
            lines.append(f"Уровень: {_fmt_price(zone.lower)}.")
        else:
            lines.append(
                f"Диапазон: {_fmt_price(zone.lower)}–{_fmt_price(zone.upper)}. "
                f"Середина: {_fmt_price(zone.mid)}."
            )

    reason = _KIND_RU.get(event.kind, event.kind.value)
    if event.depth > 0:
        reason += f" (глубина {event.depth:.0%})"
    lines.append(f"Цена: {_fmt_price(event.price)}. Событие: {reason}.")
    lines.append(f"Источник: {_fmt_source(ins)}. Время: {_fmt_time(event.occurred_at)}.")

    if zone is not None:
        status_ru = _STATUS_RU.get(zone.status.value, zone.status.value)
        lines.append(f"Статус зоны: {status_ru}.")

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


def render_text(payload: MessagePayload) -> str:
    """Текст сообщения. Пакет не скрывает второй актив (§9):
    каждое событие — отдельный блок со своим объектом и причиной."""
    blocks = [
        _render_event_block(v, payload.group_members.get(v.event.zone_id))
        for v in payload.views
    ]
    if len(blocks) > 1:
        header = f"Несколько сигналов ({len(blocks)}):"
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

    def build_keyboard(self, payload: MessagePayload):
        """Inline-кнопки §9 + единый набор ТЗ бота п.10: «График» (рендер
        в чате), «Подробнее» (карточка зоны), «Открыть приложение» (URL),
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

        chart_url = f"{self.site_base_url}/?zone={zone_id}"
        buttons = [
            [
                InlineKeyboardButton("📊 График", callback_data=f"nav:chartz:{zone_id}"),
                InlineKeyboardButton("Подробнее", callback_data=f"nav:zone:{zone_id}"),
            ],
            [InlineKeyboardButton("Открыть приложение", url=chart_url)],
        ]
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
        """LTF-сигнал с inline-кнопками навигации (ТЗ бота п.10)."""
        await self._bot.send_message(
            chat_id=self.chat_id, text=text, reply_markup=reply_markup,
        )


class LogSender:
    """Заглушка без токена: рендерит текст в лог и считается успешной
    доставкой. Для dev-режима и тестов (self.sent — журнал отправок,
    self.sent_ltf — LTF-сообщения с разметкой кнопок)."""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self._log = logger or log
        self.sent: list[MessagePayload] = []
        self.sent_ltf: list[tuple[str, object]] = []

    async def send(self, payload: MessagePayload) -> None:
        text = render_text(payload)
        self.sent.append(payload)
        self._log.info("NOTIFY (log-delivery):\n%s", text)

    async def send_text(self, text: str) -> None:
        self._log.warning("SERVICE (log-delivery): %s", text)

    async def send_ltf(self, text: str, reply_markup=None) -> None:
        self.sent_ltf.append((text, reply_markup))
        self._log.info("LTF NOTIFY (log-delivery):\n%s", text)


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
    handle_menu_text = register_bot_handlers(app, settings, db)
    return app
