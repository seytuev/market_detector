"""Шаг 4 бота: рендер LTF-графика, /chart и nav:chart*-навигация.

render_ltf_chart проверяется на синтетических данных (реальный PNG);
хендлеры — с патчем рендера (проверка «одного снимка»: один now идёт
и в картинку, и в caption). Паттерн фейков — как в test_bot_cards.py.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from telegram.ext import CallbackQueryHandler, CommandHandler

from app.bot.charts import DEFAULT_MASK, layers_from_mask, render_ltf_chart
from app.config import Settings
from app.db import Database
from app.notify.telegram import _fmt_time, build_application
from tests.conftest import make_candle
from tests.test_bot_cards import (  # noqa: F401 — фикстуры и хелперы
    _cb_update,
    _command,
    _context,
    _instrument,
    _make_live,
    _msg_update,
    _nav_callback,
    db,
    seeded,
)

TOKEN = "123:test-token"
CHAT_ID = "42"
H1 = 3_600_000


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.telegram_token = TOKEN
    s.telegram_chat_id = CHAT_ID
    s.db_path = str(tmp_path / "htf_zones.db")  # charts/ — в tmp, не в data/
    return s


@pytest.fixture()
def chart_seeded(db, seeded):
    """seeded + серия H1-свечей ETH за неделю (окно 7 дней /chart)."""
    from app.models import now_ms

    eth = seeded["eth"]
    now = now_ms()
    bars = [(100 + i % 5, 101 + i % 5, 99 + i % 5, 100 + i % 5)
            for i in range(28)]
    db.insert_candles([make_candle(
        now - (28 - i) * 6 * H1, o, h, l, c,
        timeframe="H1", instrument_id=eth,
    ) for i, (o, h, l, c) in enumerate(bars)])
    return seeded


def _fake_render(tmp_path, captured: dict):
    """Подмена render_ltf_chart: пишет минимальный PNG, запоминает kwargs."""
    def fake(db_, observation_id, out, **kw):
        captured.update(kw)
        captured["observation_id"] = observation_id
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return str(out)
    return fake


def _cb_photo_update(data: str, chat_id: str = CHAT_ID):
    answers: list = []
    edits: list = []
    texts: list = []
    photos: list = []

    async def answer(text=None):
        answers.append(text)

    async def edit_message_text(t, reply_markup=None):
        edits.append((t, reply_markup))

    async def reply_text(t, reply_markup=None):
        texts.append((t, reply_markup))

    async def reply_photo(photo, caption=None, reply_markup=None):
        photos.append({"caption": caption, "reply_markup": reply_markup})

    message = SimpleNamespace(reply_text=reply_text, reply_photo=reply_photo)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        callback_query=SimpleNamespace(
            data=data, answer=answer, edit_message_text=edit_message_text,
            message=message,
        ),
    ), answers, edits, texts, photos


def _msg_photo_update(text: str, chat_id: str = CHAT_ID):
    replies: list = []
    photos: list = []

    async def reply_text(t, reply_markup=None):
        replies.append((t, reply_markup))

    async def reply_photo(photo, caption=None, reply_markup=None):
        photos.append({"caption": caption, "reply_markup": reply_markup})

    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=int(chat_id)),
        message=SimpleNamespace(text=text, reply_text=reply_text,
                                reply_photo=reply_photo),
    ), replies, photos


# ------------------------------ render_ltf_chart ------------------------------

@pytest.mark.parametrize("mask", [0, 1, 2, 4, 8, DEFAULT_MASK])
def test_render_ltf_chart_layer_combos(db, chart_seeded, tmp_path, mask):
    """PNG создаётся при любой комбинации слоёв; отсутствие диапазона/
    entries (у seeded нет range) не роняет рендер."""
    settings = _settings(tmp_path)
    out = tmp_path / f"chart_{mask}.png"
    path = render_ltf_chart(
        db, chart_seeded["obs_bear"].id, out,
        tf="H1", period_days=7, layers=layers_from_mask(mask),
        settings=settings,
    )
    assert path is not None
    assert Path(path).is_file() and Path(path).stat().st_size > 0


def test_render_ltf_chart_no_data(db, chart_seeded, tmp_path):
    settings = _settings(tmp_path)
    # нет такого наблюдения
    assert render_ltf_chart(db, 9999, tmp_path / "x.png",
                            settings=settings) is None
    # нет D1-свечей — None, а не исключение
    assert render_ltf_chart(db, chart_seeded["obs_bear"].id,
                            tmp_path / "y.png", tf="D1",
                            settings=settings) is None


# ------------------------------ «один снимок» ------------------------------

async def test_chart_caption_matches_snapshot(db, chart_seeded, tmp_path,
                                              monkeypatch):
    """Один instrument_current на пару картинка+подпись: now из caption
    совпадает с now, переданным в рендер; слои — дефолт (все)."""
    settings = _settings(tmp_path)
    captured: dict = {}
    monkeypatch.setattr("app.bot.handlers.render_ltf_chart",
                        _fake_render(tmp_path, captured))
    app = build_application(settings, db)
    update, replies, photos = _msg_photo_update("/chart ETH H1")
    await _command(app, "chart")(update, _context(["ETH", "H1"]))
    assert replies == [] and len(photos) == 1
    caption = photos[0]["caption"]
    assert "ETHUSDT · H1" in caption
    assert "binance" not in caption  # §5.1: биржа/рынок не выводятся
    assert "BOS подтверждён" in caption  # у seeded сценарий с BOS
    assert "Текущая ситуация:" in caption
    assert _fmt_time(captured["now"]) in caption
    assert captured["layers"] == layers_from_mask(DEFAULT_MASK)
    assert captured["observation_id"] == chart_seeded["obs_bear"].id
    markup = photos[0]["reply_markup"]
    callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert f"nav:chartgo:{chart_seeded['eth']}:D1:0:{DEFAULT_MASK}" in callbacks
    assert f"nav:asset:{chart_seeded['eth']}" in callbacks


async def test_chart_no_context_answers_text(db, seeded, tmp_path):
    """Инструмент без наблюдений → текстовый ответ, фото нет."""
    settings = _settings(tmp_path)
    _instrument(db, "SOLUSDT")  # актив на анализе, но без наблюдений/данных
    app = build_application(settings, db)
    update, replies, photos = _msg_photo_update("/chart SOL H1")
    await _command(app, "chart")(update, _context(["SOL", "H1"]))
    assert photos == []
    assert replies and "недостаточно данных" in replies[0][0]


async def test_chart_without_args_shows_picker(db, seeded, tmp_path):
    app = build_application(_settings(tmp_path), db)
    update, replies, photos = _msg_photo_update("/chart")
    await _command(app, "chart")(update, _context([]))
    text, markup = replies[0]
    assert "выберите актив" in text
    assert any(b.callback_data.startswith("nav:chart:")
               for row in markup.inline_keyboard for b in row)


# ------------------------------ роутинг ------------------------------

async def test_nav_chart_one_click_and_layers(db, chart_seeded, tmp_path,
                                              monkeypatch):
    """ТЗ 07.10.2026 §8: «График» открывается за одно нажатие — сразу фото
    с дефолтами; выбор периода/слоёв — действия ПОСЛЕ выдачи графика."""
    settings = _settings(tmp_path)
    captured: dict = {}
    monkeypatch.setattr("app.bot.handlers.render_ltf_chart",
                        _fake_render(tmp_path, captured))
    app = build_application(settings, db)
    eth = chart_seeded["eth"]
    # ТФ-меню сохраняется для выбора D1/W1
    update, _, edits, _, _ = _cb_photo_update(f"nav:chart:{eth}")
    await _nav_callback(app)(update, _context())
    assert "Таймфрейм" in edits[0][0]
    # H1 — одно нажатие: сразу фото, без мастера период→слои
    update, _, _, _, photos = _cb_photo_update(f"nav:chart:{eth}:H1")
    await _nav_callback(app)(update, _context())
    assert len(photos) == 1
    assert captured["layers"] == layers_from_mask(DEFAULT_MASK)
    assert captured["tf"] == "H1"
    # на сообщении с графиком — периоды, слои, обновление (§8)
    callbacks = [b.callback_data for row in photos[0]["reply_markup"].inline_keyboard
                 for b in row if b.callback_data]
    assert f"nav:chartgo:{eth}:H1:3:{DEFAULT_MASK}" in callbacks
    assert f"nav:chartgo:{eth}:H1:7:{DEFAULT_MASK}" in callbacks
    assert f"nav:chartlay:{eth}:H1:3:{DEFAULT_MASK}" in callbacks
    # переключатель слоя: mask 15 ^ 2 (bos) = 13
    update, _, edits, _, _ = _cb_photo_update(f"nav:charttg:{eth}:H1:7:15:2")
    await _nav_callback(app)(update, _context())
    callbacks = [b.callback_data for row in edits[0][1].inline_keyboard
                 for b in row]
    assert f"nav:chartgo:{eth}:H1:7:13" in callbacks


async def test_nav_charto_uses_message_context(db, chart_seeded, tmp_path,
                                               monkeypatch):
    """§8: «График» из LTF-сообщения — контекст ЭТОГО события (observation),
    а не «первый активный сценарий»."""
    settings = _settings(tmp_path)
    captured: dict = {}
    monkeypatch.setattr("app.bot.handlers.render_ltf_chart",
                        _fake_render(tmp_path, captured))
    app = build_application(settings, db)
    eth, obs = chart_seeded["eth"], chart_seeded["obs_bull"]
    update, _, _, _, photos = _cb_photo_update(f"nav:charto:{eth}:{obs.id}")
    await _nav_callback(app)(update, _context())
    assert len(photos) == 1
    assert captured["observation_id"] == obs.id


async def test_nav_chartgo_sends_photo(db, chart_seeded, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    captured: dict = {}
    monkeypatch.setattr("app.bot.handlers.render_ltf_chart",
                        _fake_render(tmp_path, captured))
    app = build_application(settings, db)
    eth = chart_seeded["eth"]
    update, answers, _, _, photos = _cb_photo_update(
        f"nav:chartgo:{eth}:H1:3:9"  # mask 9 = htf + entries
    )
    await _nav_callback(app)(update, _context())
    assert len(photos) == 1
    assert answers == [None]
    assert captured["layers"] == layers_from_mask(9)
    assert captured["tf"] == "H1" and captured["period_days"] == 3


async def test_nav_chartz_uses_observation_or_zone_snapshot(
        db, chart_seeded, tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    captured: dict = {}
    monkeypatch.setattr("app.bot.handlers.render_ltf_chart",
                        _fake_render(tmp_path, captured))
    zone_snaps: list = []

    def fake_zone_chart(candles, zone, out, source):
        zone_snaps.append(zone.id)
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        return str(out)

    monkeypatch.setattr("app.bot.handlers.render_zone_chart", fake_zone_chart)
    app = build_application(settings, db)
    # зона с observation → LTF-рендер по этому observation
    update, _, _, _, photos = _cb_photo_update(f"nav:chartz:{chart_seeded['z_d1']}")
    await _nav_callback(app)(update, _context())
    assert captured["observation_id"] == chart_seeded["obs_bear"].id
    assert len(photos) == 1
    # зона без observation → fallback render_zone_chart самой зоны
    # (снимок по свечам ТФ зоны — se D1 для ETH)
    from app.models import now_ms

    now = now_ms()
    db.insert_candles([make_candle(
        now - i * 86_400_000, 100, 101, 99, 100,
        timeframe="D1", instrument_id=chart_seeded["eth"],
    ) for i in range(3, 0, -1)])
    update, _, _, _, photos = _cb_photo_update(f"nav:chartz:{chart_seeded['z_fvg']}")
    await _nav_callback(app)(update, _context())
    assert zone_snaps == [chart_seeded["z_fvg"]]
    assert len(photos) == 1


async def test_chart_stranger_guard(db, chart_seeded, tmp_path):
    app = build_application(_settings(tmp_path), db)
    eth = chart_seeded["eth"]
    update, answers, edits, texts, photos = _cb_photo_update(
        f"nav:chartgo:{eth}:H1:7:15", chat_id="999"
    )
    await _nav_callback(app)(update, _context())
    assert answers == [] and edits == [] and texts == [] and photos == []
