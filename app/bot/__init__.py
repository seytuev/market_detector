"""Каркас Telegram-бота: доступ, клавиатуры, карточки, хендлеры команд.

Регистрация — app/bot/handlers.py::register_bot_handlers, вызывается из
app/notify/telegram.py::build_application. Read model «текущая ситуация»
— app/services/overview.py (общий с HTTP API).
"""
