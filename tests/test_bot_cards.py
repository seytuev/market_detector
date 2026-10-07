"""Шаг 3 бота: /asset, /htf, /ltf и nav-навигация.

Паттерн tests/test_bot_commands.py: хендлеры из build_application
вызываются напрямую с фейковыми update/context, без сети. Карточки
проверяются и прямым вызовом render_* из app/bot/cards.py.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler

from app.bot.cards import render_asset, render_htf, render_ltf
from app.config import Settings
from app.db import Database
from app.models import (
    Direction,
    Instrument,
    Visit,
    Zone,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.models_ltf import (
    LtfObservation,
    LtfPivot,
    LtfScenario,
    LtfStructureEvent,
)
from app.notify.telegram import MUTE_FOREVER_MS, build_application
from tests.conftest import make_candle

TOKEN = "123:test-token"
CHAT_ID = "42"


def _settings() -> Settings:
    s = Settings()
    s.telegram_token = TOKEN
    s.telegram_chat_id = CHAT_ID
    return s


@pytest.fixture()
def db() -> Database:
    d = Database(":memory:")
    yield d
    d.close()


def _command(app, name: str):
    for h in app.handlers[0]:
        if isinstance(h, CommandHandler) and name in h.commands:
            return h.callback
    raise AssertionError(f"команда /{name} не зарегистрирована")


def _nav_callback(app):
    for h in app.handlers[0]:
        if isinstance(h, CallbackQueryHandler) and h.pattern.match("nav:x"):
            return h.callback
    raise AssertionError("nav-callback не зарегистрирован")


def _msg_update(text: str, chat_id: str = CHAT_ID):
    replies: list = []

    async def reply_text(t, reply_markup=None):
        replies.append((t, reply_markup))

    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        message=SimpleNamespace(text=text, reply_text=reply_text),
    ), replies


def _cb_update(data: str, chat_id: str = CHAT_ID):
    answers: list = []
    edits: list = []

    async def answer(text=None):
        answers.append(text)

    async def edit_message_text(t, reply_markup=None):
        edits.append((t, reply_markup))

    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        callback_query=SimpleNamespace(
            data=data, answer=answer, edit_message_text=edit_message_text
        ),
    ), answers, edits


def _context(args: list[str] | None = None):
    return SimpleNamespace(user_data={}, args=args or [])


def _instrument(db, symbol: str, venue: str = "binance") -> int:
    iid = db.upsert_instrument(Instrument(
        id=None, asset=symbol.removesuffix("USDT"), venue=venue,
        market_type="spot", symbol=symbol, quote_asset="USDT",
    ))
    db.set_instrument_ltf_analyze(iid, True)
    return iid


def _make_live(db, instrument_id: int, price: float = 100.0) -> int:
    now = now_ms()
    c = make_candle(
        now - 30 * 60_000, price, price + 1, price - 1, price,
        timeframe="H1", instrument_id=instrument_id,
    )
    db.insert_candles([c])
    db.set_quote(instrument_id, price, now)
    # F03: курсор обработки на последней закрытой H1 — иначе единая оценка
    # качества фиксирует processing_lag и data_state не становится ok
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(c.close_time))
    return now


def _zone(db, iid, type_, direction, tf, lower, upper, **kw) -> int:
    return db.insert_zone(Zone(
        id=None, instrument_id=iid, type=type_, direction=direction,
        timeframe=tf, lower=lower, upper=upper, formed_at=1, confirmed_at=2,
        status=ZoneStatus.ACTIVE, **kw,
    ))


@pytest.fixture()
def seeded(db):
    """ETH (binance): цена 100 внутри D1 OB 95–105; сверху D1 FVG 110–115;
    снизу W1 OB 50–60. Наблюдение bear (D1) со сценарием BOS (без диапазона)
    и наблюдение bull (W1) без сценария + pivots для ожидаемого BOS.
    BTC — на двух площадках (проверка неоднозначности /asset)."""
    eth = _instrument(db, "ETHUSDT")
    now = _make_live(db, eth, 100.0)
    z_d1 = _zone(db, eth, ZoneType.OB, Direction.BEAR, "D1", 95.0, 105.0,
                 has_tests=True, max_test_depth=0.3)
    z_fvg = _zone(db, eth, ZoneType.FVG, Direction.BEAR, "D1", 110.0, 115.0)
    z_w1 = _zone(db, eth, ZoneType.OB, Direction.BULL, "W1", 50.0, 60.0)
    db.open_visit(Visit(
        id=None, zone_id=z_d1, cycle_id=1, entered_at=now - 3_600_000,
        exited_at=now - 1_800_000, max_depth=0.3, exit_kind="return",
    ))
    obs_bear = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=eth, zone_id=z_d1, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active",
        activated_at=now - 10_000,
    ))
    obs_bull = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=eth, zone_id=z_w1, zone_version=1,
        cycle_id=1, direction=Direction.BULL, state="active",
        activated_at=now - 9_000,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs_bear.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="range_pending",
        created_at=now - 5_000, updated_at=now - 5_000,
    ))
    se = db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=sc.id, kind="BOS", stage="primary",
        direction=Direction.BEAR, break_level=110.0,
        break_candle_open_time=now - 6_000, occurred_at=now - 5_900,
        detected_at=now - 5_900, level_key="bos:primary:hl:1:110.0",
    ))
    db.update_ltf_scenario(sc.id, trigger_event_id=se.id)
    # якорная структура для «ожидаемого BOS» bull-контекста
    db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=eth, price=110.0, kind="high",
        pivot_at=now - 8_000, candle_open_time=now - 8_000,
        confirmed_at=now - 7_000, role="LH", state="confirmed",
    ))
    db.insert_ltf_pivot(LtfPivot(
        id=None, instrument_id=eth, price=90.0, kind="low",
        pivot_at=now - 6_500, candle_open_time=now - 6_500,
        confirmed_at=now - 6_000, role="LL", state="confirmed",
    ))
    btc_bin = _instrument(db, "BTCUSDT", venue="binance")
    btc_hl = _instrument(db, "BTCUSDT", venue="hyperliquid")
    return {
        "eth": eth, "z_d1": z_d1, "z_fvg": z_fvg, "z_w1": z_w1,
        "obs_bear": obs_bear, "obs_bull": obs_bull, "sc": sc,
        "btc_bin": btc_bin, "btc_hl": btc_hl,
    }


# ------------------------------ резолв символа ------------------------------

async def test_asset_exact_match(db, seeded):
    s = _settings()
    s.public_base_url = "https://htf.example.com"  # §12: URL-кнопка — только при публичном адресе
    app = build_application(s, db)
    update, replies = _msg_update("/asset ETH")
    await _command(app, "asset")(update, _context(["ETH"]))
    assert len(replies) == 1
    text, markup = replies[0]
    assert "ETHUSDT · binance · spot" in text
    assert "Цена:" in text and "Этап:" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row
                 if b.callback_data]
    assert f"nav:htf:{seeded['eth']}:all" in callbacks
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    assert urls and "ltf.html" in urls[0] and "token=" in urls[0]
    assert "htf.example.com" in urls[0]


async def test_asset_localhost_hides_app_url(db, seeded):
    """§12 ТЗ 07.10.2026: без публичного адреса localhost в кнопках не шлём."""
    app = build_application(_settings(), db)  # base → http://127.0.0.1:8000
    update, replies = _msg_update("/asset ETH")
    await _command(app, "asset")(update, _context(["ETH"]))
    _, markup = replies[0]
    urls = [b.url for row in markup.inline_keyboard for b in row if b.url]
    assert not any("127.0.0.1" in u or "localhost" in u for u in urls)


async def test_asset_ambiguous_shows_picker(db, seeded):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/asset BTC")
    await _command(app, "asset")(update, _context(["BTC"]))
    text, markup = replies[0]
    assert "Несколько совпадений" in text
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"nav:asset:{seeded['btc_bin']}" in callbacks
    assert f"nav:asset:{seeded['btc_hl']}" in callbacks


async def test_asset_unknown(db, seeded):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/asset XRP")
    await _command(app, "asset")(update, _context(["XRP"]))
    assert "не найден" in replies[0][0]


async def test_asset_without_args_shows_picker(db, seeded):
    app = build_application(_settings(), db)
    update, replies = _msg_update("/asset")
    await _command(app, "asset")(update, _context([]))
    text, markup = replies[0]
    assert "Выберите актив" in text
    assert markup is not None


# ------------------------------ render_asset ------------------------------

def test_render_asset_has_both_direction_contexts(db, seeded):
    settings = _settings()
    text = render_asset(db, settings, seeded["eth"])
    assert "ETHUSDT · binance · spot" in text
    # бычий и медвежий контексты — отдельными блоками
    assert "HTF-контекст (медвежий)" in text
    assert "HTF-контекст (бычий)" in text
    assert "внутри" in text  # цена 100 внутри D1 OB 95–105
    assert "Сценарий H1: BOS" in text


# ------------------------------ render_htf ------------------------------

def test_render_htf_order_inside_above_below(db, seeded):
    settings = _settings()
    text = render_htf(db, settings, seeded["eth"])
    i_inside = text.index("95–105,00")
    i_above = text.index("110,00–115,00")
    i_below = text.index("50–60,")
    assert i_inside < i_above < i_below


def test_render_htf_filter_d1_excludes_w1(db, seeded):
    settings = _settings()
    text = render_htf(db, settings, seeded["eth"], "d1")
    assert "W1" not in text
    assert "D1" in text


def test_render_htf_filter_inside_only(db, seeded):
    settings = _settings()
    text = render_htf(db, settings, seeded["eth"], "inside")
    assert "95–105,00" in text
    assert "110,00–115,00" not in text
    assert "50–60," not in text


def test_render_htf_uses_status_dictionary(db, seeded):
    settings = _settings()
    text = render_htf(db, settings, seeded["eth"])
    assert "статус: активна" in text  # формулировка из STATUS_RU
    assert "глубина теста 30%" in text


# ------------------------------ render_ltf ------------------------------

def test_render_ltf_confirmed_bos(db, seeded):
    """Выбран контекст со сценарием: подпись «подтверждён», без «ожидаемый»."""
    settings = _settings()
    text = render_ltf(db, settings, seeded["eth"])
    assert "BOS подтверждён: уровень 110,00" in text
    assert "Ожидаемый" not in text
    # диапазона нет — причина отсутствия зон входа
    assert "Подходящих зон входа нет: диапазон Premium/Discount ещё не готов" in text


def test_render_ltf_expected_bos(db, seeded):
    """Ручной выбор bull-контекста без сценария: «Ожидаемый BOS»,
    без «подтверждён»."""
    settings = _settings()
    db.set_meta(f"ltf:selected_context:{seeded['eth']}",
                str(seeded["obs_bull"].id))
    text = render_ltf(db, settings, seeded["eth"])
    assert "Ожидаемый BOS: закрытие H1 выше 110,00" in text
    assert "BOS подтверждён:" not in text


# ------------------------------ nav-роутинг ------------------------------

async def test_nav_asset_replaces_stub(db, seeded):
    app = build_application(_settings(), db)
    update, answers, edits = _cb_update(f"nav:asset:{seeded['eth']}")
    await _nav_callback(app)(update, _context())
    assert len(edits) == 1
    assert "ETHUSDT · binance · spot" in edits[0][0]


async def test_nav_htf_filter_via_callback(db, seeded):
    app = build_application(_settings(), db)
    update, answers, edits = _cb_update(f"nav:htf:{seeded['eth']}:d1")
    await _nav_callback(app)(update, _context())
    assert len(edits) == 1
    text, markup = edits[0]
    assert "W1" not in text
    # ряд фильтров на месте
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"nav:htf:{seeded['eth']}:inside" in callbacks


async def test_nav_zone_card_and_tests(db, seeded):
    app = build_application(_settings(), db)
    update, _, edits = _cb_update(f"nav:zone:{seeded['z_d1']}")
    await _nav_callback(app)(update, _context())
    assert "Границы" in edits[0][0]
    callbacks = [b.callback_data for row in edits[0][1].inline_keyboard for b in row]
    assert f"nav:zt:{seeded['z_d1']}" in callbacks

    update, _, edits = _cb_update(f"nav:zt:{seeded['z_d1']}")
    await _nav_callback(app)(update, _context())
    assert "История тестов" in edits[0][0]
    assert "глубина 30%" in edits[0][0]


async def test_nav_zone_mute(db, seeded):
    app = build_application(_settings(), db)
    update, answers, _ = _cb_update(f"nav:zm:{seeded['z_d1']}")
    await _nav_callback(app)(update, _context())
    st = db.get_alert_state(seeded["z_d1"], 1, "touch")
    assert st is not None and st.muted_until == MUTE_FOREVER_MS
    assert answers == ["Уведомления по зоне отключены."]


async def test_nav_ltf_and_ctxsel(db, seeded):
    app = build_application(_settings(), db)
    update, _, edits = _cb_update(f"nav:ltf:{seeded['eth']}")
    await _nav_callback(app)(update, _context())
    assert "LTF: ETHUSDT" in edits[0][0]

    # выбор другого контекста — как POST select-context
    update, answers, edits = _cb_update(
        f"nav:ctxsel:{seeded['eth']}:{seeded['obs_bull'].id}"
    )
    await _nav_callback(app)(update, _context())
    assert answers == ["Контекст выбран."]
    assert db.get_meta(f"ltf:selected_context:{seeded['eth']}") == \
        str(seeded["obs_bull"].id)
    assert "Ожидаемый BOS" in edits[0][0]


async def test_nav_unknown_callback_answers(db, seeded):
    """Неизвестный nav-callback — пустой answer(), без падений."""
    app = build_application(_settings(), db)
    update, answers, edits = _cb_update("nav:unknown:1")
    await _nav_callback(app)(update, _context())
    assert edits == []
    assert answers == [None]


async def test_nav_stranger_guard(db, seeded):
    app = build_application(_settings(), db)
    update, answers, edits = _cb_update(f"nav:ltf:{seeded['eth']}", chat_id="999")
    await _nav_callback(app)(update, _context())
    assert edits == [] and answers == []
