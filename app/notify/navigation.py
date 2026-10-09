"""Packet navigation, explicit target selection and reusable detail panels."""
from __future__ import annotations

import json

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup
from telegram.error import BadRequest


def card_keyboard(packet_id, targets=None):
    graph = Button("📊 График", callback_data=f"nf:g:{packet_id}")
    if targets and len(targets) == 1:
        if targets[0].get("chart"):
            graph = Button("📊 График", callback_data=targets[0]["chart"])
        elif targets[0].get("url"):
            graph = Button("📊 График", url=targets[0]["url"])
    return Markup([
        [graph,
         Button("🔎 Подробнее", callback_data=f"nf:d:{packet_id}:0")],
        [Button("⋯ Действия", callback_data=f"nf:a:{packet_id}")],
    ])


async def reusable_panel(db, bot, chat_id, key, text, markup=None):
    # Detail panels are paginated; current-state panels may still be verbose.
    if len(text.encode("utf-16-le")) // 2 > 4000:
        text = text.encode("utf-16-le")[:7800].decode("utf-16-le", errors="ignore") + "\n…"
    cache_key = f"notification:panel:{chat_id}:{key}"
    message_id = db.get_meta(cache_key)
    if message_id:
        try:
            await bot.edit_message_text(chat_id=chat_id, message_id=int(message_id),
                                        text=text, reply_markup=markup, disable_web_page_preview=True)
            return int(message_id)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return int(message_id)
            if not any(term in str(exc).lower() for term in ("message to edit not found", "message can't be edited")):
                raise
    msg = await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup,
                                 disable_web_page_preview=True)
    db.set_meta(cache_key, str(msg.message_id))
    return msg.message_id


async def reusable_photo(db, bot, chat_id, key, path, caption, markup, reply_photo):
    """Keep interactive chart refreshes separate from immutable signal photos."""
    from telegram import InputMediaPhoto
    cache_key = f"notification:chart:{chat_id}:{key}"
    message_id = db.get_meta(cache_key)
    if len(caption.encode("utf-16-le")) // 2 > 1000:
        caption = caption.encode("utf-16-le")[:1960].decode("utf-16-le", errors="ignore") + "\n…"
    with open(path, "rb") as fh:
        if message_id:
            try:
                await bot.edit_message_media(chat_id=chat_id, message_id=int(message_id),
                    media=InputMediaPhoto(fh, caption=caption), reply_markup=markup)
                return
            except BadRequest as exc:
                if "message is not modified" in str(exc).lower():
                    return
                if not any(term in str(exc).lower() for term in ("message to edit not found", "message can't be edited")):
                    raise
                fh.seek(0)
        msg = await reply_photo(photo=fh, caption=caption, reply_markup=markup)
    if msg is not None:
        db.set_meta(cache_key, str(msg.message_id))


def register_notification_handlers(app, db, settings):
    from telegram.ext import CallbackQueryHandler
    from ..bot.access import make_owner_guard
    guard = make_owner_guard(settings)

    async def callback(update, context):
        if not guard(update):
            return
        query = update.callback_query
        await query.answer()
        parts = query.data.split(":")
        try:
            action, packet_id = parts[1], int(parts[2])
        except (ValueError, IndexError):
            return
        row = db.conn.execute("SELECT * FROM notification_packet WHERE id=?", (packet_id,)).fetchone()
        if row is None or row["destination"] != str(settings.telegram_chat_id):
            return
        card = json.loads(row["card"])
        chat = settings.telegram_chat_id
        targets = card.get("targets", [])
        text, keyboard = "", []
        if action == "d":
            page = max(0, int(parts[3])) if len(parts) > 3 else 0
            full = card.get("details") or card["text"]
            pages = [full[i:i + 1800] for i in range(0, len(full), 1800)] or [full]
            page = min(page, len(pages) - 1)
            text = pages[page] + f"\n\nСтраница {page + 1}/{len(pages)}"
            nav = []
            if page:
                nav.append(Button("←", callback_data=f"nf:d:{packet_id}:{page - 1}"))
            if page + 1 < len(pages):
                nav.append(Button("→", callback_data=f"nf:d:{packet_id}:{page + 1}"))
            if nav:
                keyboard.append(nav)
        elif action in ("g", "a"):
            page = max(0, int(parts[3])) if len(parts) > 3 else 0
            text = "Выберите объект для графика:" if action == "g" else "Выберите объект для действий:"
            for index in range(page * 8, min(len(targets), (page + 1) * 8)):
                target = targets[index]
                if action == "g" and target.get("chart"):
                    button = Button(target["label"], callback_data=target["chart"])
                elif action == "g" and target.get("url"):
                    button = Button(target["label"], url=target["url"])
                else:
                    button = Button(target["label"], callback_data=f"nf:t:{packet_id}:{index}")
                keyboard.append([button])
            nav = []
            if page:
                nav.append(Button("←", callback_data=f"nf:{action}:{packet_id}:{page - 1}"))
            if (page + 1) * 8 < len(targets):
                nav.append(Button("→", callback_data=f"nf:{action}:{packet_id}:{page + 1}"))
            if nav:
                keyboard.append(nav)
            if not targets:
                text = "Для этого сообщения нет отдельных объектов. Подробности доступны по кнопке."
        elif action == "t":
            index = int(parts[3])
            if index < 0 or index >= len(targets):
                return
            target = targets[index]
            text = target["label"] + "\nДействия применяются только к этому объекту."
            iid, zid = target.get("instrument_id"), target.get("zone_id")
            if iid:
                keyboard.append([Button("🔄 Текущее состояние", callback_data=f"nav:refresh:{iid}"),
                                 Button("Показать LTF", callback_data=f"nav:ltf:{iid}")])
            if zid:
                tail = f"{zid}:{target.get('cycle_id', 1)}:{target.get('kind', 'touch')}"
                keyboard.extend([
                    [Button("Изучаю", callback_data=f"htf:ack:{tail}"), Button("Отложить на 24 ч", callback_data=f"htf:snooze:{tail}")],
                    [Button("🔕 Отключить", callback_data=f"htf:mute:{tail}"), Button("Добавить заметку", callback_data=f"htf:note:{tail}")],
                ])
            for label, key in (("Рабочее место", "url"), ("TradingView", "tv")):
                if target.get(key):
                    keyboard.append([Button(label, url=target[key])])
        else:
            return
        keyboard.append([Button("🔎 Подробнее", callback_data=f"nf:d:{packet_id}:0"),
                         Button("⋯ Действия", callback_data=f"nf:a:{packet_id}")])
        await reusable_panel(db, context.bot, chat, f"packet:{packet_id}", text, Markup(keyboard))

    app.add_handler(CallbackQueryHandler(callback, pattern=r"^nf:"))
