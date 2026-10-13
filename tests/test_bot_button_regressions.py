"""Реальные пути кнопок: фото, фильтры, заметки и сохранение контекста."""
from types import SimpleNamespace

import pytest
from telegram.error import BadRequest

from app.bot.handlers import _parse_duration_ms
from app.bot.keyboards import chart_result_inline, main_menu_keyboard, MENU_LABELS
from app.notify.telegram import build_application
from tests.test_bot_cards import (
    db, seeded, _settings, _cb_update, _context, _nav_callback, _msg_update,
)
from tests.test_bot_chart import (
    _cb_photo_update, _fake_render, _settings as chart_settings,
)


@pytest.mark.parametrize("filt", ["inside", "near", "all", "d1", "w1"])
async def test_htf_buttons_match_visible_zones(db, seeded, filt):
    update, _, edits = _cb_update(f"nav:htf:{seeded['eth']}:{filt}")
    await _nav_callback(build_application(_settings(), db))(update, _context())
    text, markup = edits[0]
    zone_ids = [int(b.callback_data.split(":")[2])
                for row in markup.inline_keyboard for b in row
                if b.callback_data.startswith("nav:zone:")]
    expected = {
        "inside": [seeded["z_d1"]],
        "near": [seeded["z_d1"], seeded["z_fvg"], seeded["z_w1"]],
        "all": [seeded["z_d1"], seeded["z_fvg"], seeded["z_w1"]],
        "d1": [seeded["z_d1"], seeded["z_fvg"]],
        "w1": [seeded["z_w1"]],
    }
    assert zone_ids == expected[filt]
    assert "HTF-зоны:" in text


async def test_photo_navigation_replies_without_editing_signal(db, seeded):
    update, answers, edits = _cb_update(f"nav:asset:{seeded['eth']}")
    replies = []

    async def reply_text(text, reply_markup=None):
        replies.append((text, reply_markup))

    update.callback_query.message = SimpleNamespace(photo=[object()], reply_text=reply_text)
    await _nav_callback(build_application(_settings(), db))(update, _context())
    assert edits == []
    assert replies and "ETHUSDT" in replies[0][0]
    assert answers == [None]


async def test_repeated_refresh_handles_unchanged_message(db, seeded):
    update, answers, _ = _cb_update("nav:now:refresh")

    async def unchanged(*args, **kwargs):
        raise BadRequest("Message is not modified")

    update.callback_query.edit_message_text = unchanged
    await _nav_callback(build_application(_settings(), db))(update, _context())
    assert answers == [None]


async def test_menu_cancels_pending_note_instead_of_saving_label(db, seeded):
    app = build_application(_settings(), db)
    context = _context()
    context.user_data["pending_note_zone"] = seeded["z_d1"]
    update, replies = _msg_update("История")
    await app.handlers[0][1].callback(update, context)
    assert db.get_reviews(seeded["z_d1"]) == []
    assert "pending_note_zone" not in context.user_data
    assert "История" in replies[0][0]
    assert {button.text for row in main_menu_keyboard().keyboard for button in row} == MENU_LABELS


async def test_zone_ltf_selects_that_zone_and_mute_can_be_reversed(db, seeded):
    from app.notify.suppress import bot_delivery_blocked
    app = build_application(_settings(), db)
    iid, zid = seeded["eth"], seeded["z_w1"]
    update, _, _ = _cb_update(f"nav:ltfz:{zid}")
    await _nav_callback(app)(update, _context())
    assert db.get_meta(f"ltf:selected_context:{iid}") == str(seeded["obs_bull"].id)
    update, _, _ = _cb_update(f"nav:zm:{zid}")
    await _nav_callback(app)(update, _context())
    assert db.get_alert_state(zid, 1, "touch").muted_until is not None
    assert bot_delivery_blocked(db, "42", grp="ltf", kind="bos", instrument_id=iid, zone_id=zid)
    update, _, _ = _cb_update(f"nav:zum:{zid}")
    await _nav_callback(app)(update, _context())
    assert db.get_alert_state(zid, 1, "touch").muted_until is None
    assert not bot_delivery_blocked(db, "42", grp="ltf", kind="bos", instrument_id=iid, zone_id=zid)


async def test_message_chart_context_survives_layers_and_refresh(db, seeded, tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr("app.bot.handlers.render_ltf_chart", _fake_render(tmp_path, captured))
    app = build_application(chart_settings(tmp_path), db)
    iid, obs_id = seeded["eth"], seeded["obs_bull"].id
    update, _, _, _, photos = _cb_photo_update(f"nav:charto:{iid}:{obs_id}")
    await _nav_callback(app)(update, _context())
    assert "BOS подтверждён" not in photos[0]["caption"]
    buttons = [b for row in photos[0]["reply_markup"].inline_keyboard for b in row]
    layers = next(b.callback_data for b in buttons if b.text == "Слои…")
    update, _, _, texts, _ = _cb_photo_update(layers)
    await _nav_callback(app)(update, _context())
    toggle = texts[0][1].inline_keyboard[0][0].callback_data
    update, _, edits, _, _ = _cb_photo_update(toggle)
    await _nav_callback(app)(update, _context())
    show = next(b.callback_data for row in edits[0][1].inline_keyboard for b in row
                if b.callback_data.startswith("nav:chartgo:"))
    update, _, _, _, _ = _cb_photo_update(show)
    await _nav_callback(app)(update, _context())
    assert captured["observation_id"] == obs_id
    assert db.get_meta(f"ltf:selected_context:{iid}") is None


def test_switching_from_d1_to_h1_has_nonzero_period():
    markup = chart_result_inline(1, "D1", 0, 15, observation_id=2)
    buttons = [b for row in markup.inline_keyboard for b in row]
    assert next(b.callback_data for b in buttons if b.text == "H1") == "nav:chartgo:1:H1:3:15:2"


@pytest.mark.parametrize("duration", ["0h", "-1d", "bad"])
def test_invalid_mute_durations(duration):
    assert _parse_duration_ms(duration) is None


async def test_open_workspace_handles_local_and_authenticated_public_url(db):
    settings = _settings()
    app = build_application(settings, db)
    update, replies = _msg_update("Открыть рабочее место")
    await app.handlers[0][1].callback(update, _context())
    assert "не настроен" in replies[0][0]
    settings.public_base_url = "https://example.com"
    settings.auth_token = "a+b&c"
    update, replies = _msg_update("Открыть рабочее место")
    await app.handlers[0][1].callback(update, _context())
    assert replies[0][0] == "Анализ: https://example.com/?token=a%2Bb%26c"
