"""Клавиатуры бота: reply-меню и inline-навигация.

Callback-форматы: nav:now:refresh (обновить /now), nav:asset:<iid>
(карточка актива — следующий шаг), выбор актива — «<prefix>:<iid>».
"""
from __future__ import annotations

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)

from ..db import Database
from ..models import EventKind
from ..notify.suppress import (
    BOT_ALERT_GROUPS,
    BOT_GRP_ALT,
    BOT_GRP_HTF,
    BOT_GRP_LTF,
    BOT_GRP_SERVICE,
)
from ..texts_ru import ALT_EVENT_KIND_RU, KIND_RU, LTF_EVENT_KIND_RU

# Подписи кнопок reply-меню — текстовые, разбираются в handle_menu_text
MENU_NOW = "Сейчас"
MENU_PICK = "Выбрать актив"
MENU_HTF = "Контекст (HTF)"
MENU_LTF = "Структура H1"
MENU_ALERTS = "Уведомления"
MENU_OPEN_APP = "Открыть рабочее место"

# «Открыть рабочее место» — текстом, а не WebAppInfo: WebApp требует HTTPS,
# а сервис часто поднят на http://host:port; ссылку шлём сообщением
MENU_LABELS = frozenset({
    MENU_NOW, MENU_PICK, MENU_HTF, MENU_LTF, MENU_ALERTS, MENU_OPEN_APP,
})

MENU_HIST = "История"
MENU_STATUS = "Состояние сервиса"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Главное меню под полем ввода (2 ряда по 3 + ряд истории/статуса)."""
    return ReplyKeyboardMarkup(
        [
            [MENU_NOW, MENU_PICK, MENU_HTF],
            [MENU_LTF, MENU_ALERTS, MENU_OPEN_APP],
            [MENU_HIST, MENU_STATUS],
        ],
        resize_keyboard=True,
    )


def _instrument_rows(db: Database, prefix: str, instruments: list | None = None) -> list[list[InlineKeyboardButton]]:
    if instruments is None:
        instruments = db.get_instruments()
    instruments = sorted(
        instruments,
        key=lambda i: (i.symbol, i.venue, i.market_type),
    )
    return [
        [InlineKeyboardButton(
            f"{i.symbol} · {i.venue} · {i.market_type}",
            callback_data=f"{prefix}:{i.id}",
        )]
        for i in instruments
    ]


def instruments_inline(
    db: Database, prefix: str, instruments: list | None = None
) -> InlineKeyboardMarkup:
    """Inline-выбор актива: подпись «СИМВОЛ · биржа · рынок»,
    callback «<prefix>:<instrument_id>». instruments=None — все инструменты;
    иначе только переданные (например, совпавшие при поиске /asset)."""
    return InlineKeyboardMarkup(_instrument_rows(db, prefix, instruments))


def now_inline(db: Database) -> InlineKeyboardMarkup:
    """Кнопки активов (nav:asset:<iid>) + «🔄 Обновить» (nav:now:refresh)."""
    rows = _instrument_rows(db, "nav:asset")
    rows.append([InlineKeyboardButton("🔄 Обновить", callback_data="nav:now:refresh")])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------- #
# Карточки актива / HTF / LTF (шаг 3)
# --------------------------------------------------------------------- #

def public_base_url(settings) -> str | None:
    """Публичный базовый URL сервиса или None, если адрес локальный
    (ТЗ 07.10.2026 §12: не отправлять localhost/127.0.0.1 в кнопках)."""
    from urllib.parse import urlparse

    base = settings.effective_base_url()
    host = urlparse(base).hostname or ""
    if host in {"127.0.0.1", "localhost", "0.0.0.0", "::1"}:
        return None
    return base


def ltf_app_url(settings, instrument_id: int) -> str | None:
    """Ссылка «В приложении» на окно LTF выбранного инструмента
    (формат как в app.js: /ltf.html?token=…&instrument=…).
    None — публичный адрес не настроен (localhost слать нельзя, §12)."""
    base = public_base_url(settings)
    if base is None:
        return None
    return f"{base}/ltf.html?token={settings.auth_token}&instrument={instrument_id}"


def asset_inline(db: Database, settings, instrument_id: int) -> InlineKeyboardMarkup:
    """Кнопки карточки актива. График и уведомления — заглушки шагов 4/6."""
    iid = instrument_id
    rows = [
        [
            InlineKeyboardButton("График H1", callback_data=f"nav:chart:{iid}:H1"),
            InlineKeyboardButton("HTF-зоны", callback_data=f"nav:htf:{iid}:all"),
        ],
        [
            InlineKeyboardButton("Другие контексты", callback_data=f"nav:ctx:{iid}"),
            InlineKeyboardButton("Уведомления", callback_data=f"nav:alerts:{iid}"),
        ],
    ]
    app_url = ltf_app_url(settings, iid)
    if app_url is not None:
        rows.append([InlineKeyboardButton("В приложении", url=app_url)])
    return InlineKeyboardMarkup(rows)


def htf_inline(
    db: Database, instrument_id: int, zones: list, filt: str = "all"
) -> InlineKeyboardMarkup:
    """Зоны кнопками (nav:zone:<id>) + ряд фильтров nav:htf:<iid>:<f>."""
    iid = instrument_id
    rows = [
        [InlineKeyboardButton(
            f"{z.type.value.upper()} {z.timeframe} {z.lower:g}–{z.upper:g}",
            callback_data=f"nav:zone:{z.id}",
        )]
        for z in zones[:8]
    ]
    rows.append([
        InlineKeyboardButton("D1", callback_data=f"nav:htf:{iid}:d1"),
        InlineKeyboardButton("W1", callback_data=f"nav:htf:{iid}:w1"),
        InlineKeyboardButton("Все", callback_data=f"nav:htf:{iid}:all"),
    ])
    rows.append([
        InlineKeyboardButton("Внутри сейчас", callback_data=f"nav:htf:{iid}:inside"),
        InlineKeyboardButton("Ближайшие", callback_data=f"nav:htf:{iid}:near"),
    ])
    return InlineKeyboardMarkup(rows)


def zone_inline(zone) -> InlineKeyboardMarkup:
    """Кнопки карточки зоны."""
    zid, iid = zone.id, zone.instrument_id
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("График", callback_data=f"nav:chartz:{zid}")],
        [InlineKeyboardButton("LTF от этой зоны", callback_data=f"nav:ltf:{iid}")],
        [InlineKeyboardButton("История тестов", callback_data=f"nav:zt:{zid}")],
        [InlineKeyboardButton("Уведомления зоны", callback_data=f"nav:alz:{zid}")],
        [InlineKeyboardButton("Заглушить", callback_data=f"nav:zm:{zid}")],
    ])


def zone_tests_inline(zone) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← К зоне", callback_data=f"nav:zone:{zone.id}")],
    ])


def ltf_inline(db: Database, instrument_id: int) -> InlineKeyboardMarkup:
    """Кнопки карточки LTF-сценария. График/история — заглушки шагов 4/7."""
    iid = instrument_id
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("График", callback_data=f"nav:chart:{iid}:H1"),
            InlineKeyboardButton("Зоны входа", callback_data=f"nav:entries:{iid}"),
        ],
        [
            InlineKeyboardButton("Почему этот сценарий", callback_data=f"nav:why:{iid}"),
            InlineKeyboardButton("Другой контекст", callback_data=f"nav:ctx:{iid}"),
        ],
        [InlineKeyboardButton("История", callback_data=f"nav:hist:{iid}")],
    ])


def ltf_back_inline(instrument_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← К сценарию", callback_data=f"nav:ltf:{instrument_id}")],
    ])


def contexts_inline(instrument_id: int, contexts: list) -> InlineKeyboardMarkup:
    """Активные контексты кнопками: nav:ctxsel:<iid>:<observation_id>."""
    iid = instrument_id
    rows = [
        [InlineKeyboardButton(
            f"{c['direction']} {((c.get('parent_zone') or {}).get('type', '?')).upper()} "
            f"{(c.get('parent_zone') or {}).get('timeframe', '?')}",
            callback_data=f"nav:ctxsel:{iid}:{c['observation_id']}",
        )]
        for c in contexts
    ]
    rows.append([InlineKeyboardButton("← К сценарию", callback_data=f"nav:ltf:{iid}")])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------- #
# /chart — выбор параметров графика (шаг 4)
# --------------------------------------------------------------------- #

def chart_tf_inline(instrument_id: int) -> InlineKeyboardMarkup:
    """Выбор таймфрейма: nav:chart:<iid>:<TF>."""
    iid = instrument_id
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("H1", callback_data=f"nav:chart:{iid}:H1"),
            InlineKeyboardButton("D1", callback_data=f"nav:chart:{iid}:D1"),
            InlineKeyboardButton("W1", callback_data=f"nav:chart:{iid}:W1"),
        ],
        [InlineKeyboardButton("← К активу", callback_data=f"nav:asset:{iid}")],
    ])


def chart_period_inline(instrument_id: int, tf: str) -> InlineKeyboardMarkup:
    """Выбор периода H1: nav:chart:<iid>:<tf>:<days>."""
    iid = instrument_id
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("3 дня", callback_data=f"nav:chart:{iid}:{tf}:3"),
            InlineKeyboardButton("7 дней", callback_data=f"nav:chart:{iid}:{tf}:7"),
            InlineKeyboardButton("14 дней", callback_data=f"nav:chart:{iid}:{tf}:14"),
        ],
        [InlineKeyboardButton("← Таймфрейм", callback_data=f"nav:chart:{iid}")],
    ])


def chart_layers_inline(
    instrument_id: int, tf: str, days: int, mask: int
) -> InlineKeyboardMarkup:
    """Мультивыбор слоёв: переключатель nav:charttg:…:<mask>:<bit>,
    «Показать» — nav:chartgo:<iid>:<tf>:<days>:<mask>."""
    from .charts import DEFAULT_MASK, LAYER_BITS, LAYER_LABELS

    iid = instrument_id
    rows = [
        [InlineKeyboardButton(
            f"{'✅' if mask & bit else '☐'} {LAYER_LABELS[name]}",
            callback_data=f"nav:charttg:{iid}:{tf}:{days}:{mask}:{bit}",
        )]
        for name, bit in LAYER_BITS.items()
    ]
    if mask == DEFAULT_MASK:
        show = "Показать (все слои)"
    else:
        show = "Показать"
    rows.append([InlineKeyboardButton(
        f"📊 {show}", callback_data=f"nav:chartgo:{iid}:{tf}:{days}:{mask}"
    )])
    back = f"nav:chart:{iid}:{tf}" if tf == "H1" else f"nav:chart:{iid}"
    rows.append([InlineKeyboardButton("← Назад", callback_data=back)])
    return InlineKeyboardMarkup(rows)


def chart_result_inline(
    instrument_id: int, tf: str, days: int, mask: int,
    settings=None, instrument=None,
) -> InlineKeyboardMarkup:
    """Кнопки на сообщении с графиком (ТЗ 07.10.2026 §8): «Обновить» и
    периоды — сразу новый рендер той же маской; слои — отдельным сообщением;
    ссылки приложения/TradingView — только при публичном адресе (§12)."""
    iid = instrument_id
    rows = []
    period_row = [InlineKeyboardButton(
        "🔄 Обновить", callback_data=f"nav:chartgo:{iid}:{tf}:{days}:{mask}"
    )]
    if tf == "H1":
        period_row += [
            InlineKeyboardButton(
                f"{'• ' + str(d) + ' дн. •' if d == days else f'{d} дн.'}",
                callback_data=f"nav:chartgo:{iid}:H1:{d}:{mask}",
            )
            for d in (3, 7, 14)
        ]
    rows.append(period_row)
    # D1/W1 — отдельный просмотр HTF-контекста, не замена логики LTF (§8)
    rows.append([
        InlineKeyboardButton(
            f"{'• ' + t + ' •' if t == tf else t}",
            callback_data=f"nav:chartgo:{iid}:{t}:{days if t == 'H1' else 0}:{mask}",
        )
        for t in ("H1", "D1", "W1")
    ])
    rows.append([
        InlineKeyboardButton(
            "Слои…", callback_data=f"nav:chartlay:{iid}:{tf}:{days}:{mask}"
        ),
        InlineKeyboardButton("Зоны входа", callback_data=f"nav:entries:{iid}"),
    ])
    link_row = []
    if settings is not None:
        app_url = ltf_app_url(settings, iid)
        if app_url is not None:
            link_row.append(InlineKeyboardButton(
                "Структура H1 в приложении", url=app_url
            ))
    if instrument is not None:
        tv = (
            f"https://www.tradingview.com/chart/?symbol="
            f"{instrument['venue'].upper()}:{instrument['symbol']}"
        )
        link_row.append(InlineKeyboardButton("TradingView", url=tv))
    if link_row:
        rows.append(link_row)
    rows.append([InlineKeyboardButton("← Назад", callback_data=f"nav:asset:{iid}")])
    return InlineKeyboardMarkup(rows)


def ltf_signal_inline(
    kind: str, instrument_id: int, zone_id: int | None = None, settings=None,
    observation_id: int | None = None, instrument=None,
) -> InlineKeyboardMarkup:
    """Единый набор кнопок под LTF-сигналом (ТЗ п.10, ТЗ 07.10.2026 §8/§12):
    «График» — открытие за одно нажатие по контексту ЭТОГО сообщения
    (observation_id события, а не «первый активный сценарий»), «Подробнее»,
    «Открыть рабочее место» (URL при публичном адресе), «TradingView»,
    «Заглушить», «🔄 Обновить»; контекстные по виду события."""
    iid = instrument_id
    if observation_id is not None:
        chart_cb = f"nav:charto:{iid}:{observation_id}"
    else:
        chart_cb = f"nav:chart:{iid}:H1"
    rows = [[
        InlineKeyboardButton("📊 График", callback_data=chart_cb),
        InlineKeyboardButton("Подробнее", callback_data=f"nav:asset:{iid}"),
    ]]
    if kind in ("bos", "sms", "entries_ready", "range_ready"):
        rows.append([InlineKeyboardButton(
            "Зоны входа", callback_data=f"nav:entries:{iid}"
        )])
    elif kind == "touch":
        rows.append([InlineKeyboardButton(
            "Сценарий", callback_data=f"nav:why:{iid}"
        )])
    elif kind == "cancellation":
        rows.append([InlineKeyboardButton(
            "Причина отмены", callback_data=f"nav:why:{iid}"
        )])
    link_row = []
    if settings is not None:
        app_url = ltf_app_url(settings, iid)
        if app_url is not None:
            link_row.append(InlineKeyboardButton(
                "Открыть рабочее место", url=app_url
            ))
    if instrument is not None:
        tv = (
            f"https://www.tradingview.com/chart/?symbol="
            f"{instrument.venue.upper()}:{instrument.symbol}"
        )
        link_row.append(InlineKeyboardButton("TradingView", url=tv))
    if link_row:
        rows.append(link_row)
    # «Обновить» — текущее состояние НОВЫМ сообщением (исторический текст
    # сигнала не переписывается)
    rows.append([InlineKeyboardButton(
        "🔄 Обновить", callback_data=f"nav:refresh:{iid}"
    )])
    if zone_id is not None:
        rows.append([InlineKeyboardButton(
            "Заглушить", callback_data=f"nav:zm:{zone_id}"
        )])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------- #
# Watchlist (ТЗ п.8) и настройки уведомлений (ТЗ п.9)
# --------------------------------------------------------------------- #

def watchlist_inline(db: Database, chat_id: str) -> InlineKeyboardMarkup:
    """Строки списка: переключатель уведомлений (nav:wal) + «Удалить»
    (nav:wrm с подтверждением); ряд «Добавить актив»."""
    rows = []
    for w in db.list_watchlist(chat_id):
        ins = db.get_instrument(w["instrument_id"])
        if ins is None:
            continue
        iid = ins.id
        toggle = "🔔 выкл" if w["alerts_enabled"] else "🔕 вкл"
        rows.append([
            InlineKeyboardButton(toggle, callback_data=f"nav:wal:{iid}"),
            InlineKeyboardButton("🗑 Удалить", callback_data=f"nav:wrm:{iid}"),
        ])
    rows.append([InlineKeyboardButton("➕ Добавить актив", callback_data="nav:wadd")])
    return InlineKeyboardMarkup(rows)


def watchlist_add_inline(db: Database, chat_id: str) -> InlineKeyboardMarkup:
    """Выбор актива для добавления: только НЕ входящие в список."""
    listed = {w["instrument_id"] for w in db.list_watchlist(chat_id)}
    instruments = [i for i in db.get_instruments() if i.id not in listed]
    rows = [
        [InlineKeyboardButton(
            f"{i.symbol} · {i.venue} · {i.market_type}",
            callback_data=f"nav:waddok:{i.id}",
        )]
        for i in sorted(instruments, key=lambda i: (i.symbol, i.venue))
    ]
    rows.append([InlineKeyboardButton("← К списку", callback_data="nav:wlist")])
    return InlineKeyboardMarkup(rows)


def watchlist_remove_confirm_inline(instrument_id: int) -> InlineKeyboardMarkup:
    iid = instrument_id
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Удалить", callback_data=f"nav:wrmok:{iid}"),
        InlineKeyboardButton("Отмена", callback_data="nav:wlist"),
    ]])


_ALERT_GROUP_LABELS = {
    "htf": "HTF",
    "ltf": "LTF",
    "entry": "Вход (касание Entry Zone)",
    "scenario": "Сценарий (отмена)",
    "service": "Сервис",
    "alt": "Альткоины D1",
}


def _alert_kind_label(grp: str, kind: str) -> str:
    if grp == BOT_GRP_HTF:
        return KIND_RU.get(EventKind(kind), kind)
    if grp == BOT_GRP_SERVICE:
        return "сервисные сообщения"
    if grp == BOT_GRP_ALT:
        return ALT_EVENT_KIND_RU.get(kind, kind)
    return LTF_EVENT_KIND_RU.get(kind, kind)


def alerts_groups_inline() -> InlineKeyboardMarkup:
    """Меню групп /alerts (глобальный уровень)."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            _ALERT_GROUP_LABELS[grp], callback_data=f"nav:algrp:{grp}"
        )]
        for grp in BOT_ALERT_GROUPS
    ])


def alerts_instrument_groups_inline(instrument_id: int) -> InlineKeyboardMarkup:
    """«Уведомления» из карточки актива: группа → переключатели по активу."""
    iid = instrument_id
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            _ALERT_GROUP_LABELS[grp], callback_data=f"nav:alinsi:{grp}:{iid}"
        )]
        for grp in BOT_ALERT_GROUPS
    ] + [[InlineKeyboardButton("← К активу", callback_data=f"nav:asset:{iid}")]])


def alert_kinds_inline(
    db: Database, chat_id: str, grp: str,
    *, scope: str = "global", scope_ref: str = "", back: str = "nav:alerts",
) -> InlineKeyboardMarkup:
    """Переключатели видов группы. Дефолт — включено; запись в
    bot_alert_pref появляется при первом переключении."""
    rows = []
    for kind in BOT_ALERT_GROUPS[grp]:
        enabled = db.alert_pref_enabled(chat_id, scope, scope_ref, grp, kind)
        mark = "✅" if enabled else "☐"
        if scope == "instrument":
            cb = f"nav:altgi:{grp}:{kind}:{scope_ref}"
        elif scope == "context":
            cb = f"nav:altgc:{kind}:{scope_ref}"
        else:
            cb = f"nav:altg:{grp}:{kind}"
        rows.append([InlineKeyboardButton(
            f"{mark} {_alert_kind_label(grp, kind)}", callback_data=cb
        )])
    if scope == "global" and grp in (BOT_GRP_HTF, BOT_GRP_LTF):
        rows.append([InlineKeyboardButton(
            "по активу…", callback_data=f"nav:alins:{grp}"
        )])
    rows.append([InlineKeyboardButton("← Назад", callback_data=back)])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------- #
# /history — фильтры периода и типа (ТЗ п.11)
# --------------------------------------------------------------------- #

def history_period_inline(instrument_id: int) -> InlineKeyboardMarkup:
    """Выбор периода: nav:histp:<iid>:<hours>."""
    from .cards import HISTORY_PERIODS

    iid = instrument_id
    rows = [[
        InlineKeyboardButton(label, callback_data=f"nav:histp:{iid}:{hours}")
        for hours, label in HISTORY_PERIODS
    ]]
    rows.append([InlineKeyboardButton("← К активу", callback_data=f"nav:asset:{iid}")])
    return InlineKeyboardMarkup(rows)


def history_type_inline(instrument_id: int, hours: int) -> InlineKeyboardMarkup:
    """Выбор типа события: nav:histt:<iid>:<hours>:<type>."""
    from .cards import HISTORY_TYPES

    iid = instrument_id
    rows = [
        [InlineKeyboardButton(label, callback_data=f"nav:histt:{iid}:{hours}:{t}")]
        for t, label in HISTORY_TYPES
    ]
    rows.append([InlineKeyboardButton("← Период", callback_data=f"nav:hist:{iid}")])
    return InlineKeyboardMarkup(rows)


def history_result_inline(instrument_id: int) -> InlineKeyboardMarkup:
    iid = instrument_id
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("← Период", callback_data=f"nav:hist:{iid}")],
        [InlineKeyboardButton("← К активу", callback_data=f"nav:asset:{iid}")],
    ])
