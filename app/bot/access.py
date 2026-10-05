"""Ограничение доступа к боту чатом владельца (§11 п.8: приватный сервис)."""
from __future__ import annotations


def make_owner_guard(settings):
    """is_owner(update): True только для чата владельца из настроек.

    Без заданного telegram_chat_id доступ закрыт для всех."""
    owner_chat = settings.telegram_chat_id

    def is_owner(update) -> bool:
        chat = update.effective_chat
        return (
            bool(owner_chat)
            and chat is not None
            and str(chat.id) == str(owner_chat)
        )

    return is_owner
