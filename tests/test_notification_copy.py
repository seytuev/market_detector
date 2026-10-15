"""Regressions from the owner's Telegram examples, 15 October 2026."""
from types import SimpleNamespace

import pytest

from app.bot.cards import render_chart_caption
from app.config import DetectorConfig
from app.models import Direction, Event, EventKind, Instrument, Zone, ZoneStatus, ZoneType
from app.models_ltf import LtfEvent
from app.notify.compact import delivery_delay, ltf_text, ordered_entries
from app.notify.ltf_queue import LtfDispatcher
from app.notify.ltf_templates import LtfContext
from app.notify.queue import EventView
from app.notify.telegram import LogSender, _render_event_block
from tests.test_ltf_notify import BOS_PAYLOAD, T0, _event
from tests.test_notification_outbox import parent


def context(direction=Direction.BEAR):
    return LtfContext(
        instrument=Instrument(1, "BTC", "binance", "spot", "BTCUSDT", "USDT"),
        zone=Zone(1, 1, ZoneType.BSL, Direction.BEAR, "D1", 83933, 83933,
                  formed_at=T0, confirmed_at=T0, status=ZoneStatus.ACTIVE),
        observation=None,
        scenario=SimpleNamespace(direction=direction),
    )


def event(kind, **payload):
    return LtfEvent(id=None, observation_id=1, scenario_id=1, kind=kind,
                    payload=payload, occurred_at=T0, detected_at=T0, dedupe_key="copy")


def test_nearest_zones_use_break_close_and_do_not_invent_live_quote():
    entries = [
        {"type": "FVG", "lower": 83360.01, "upper": 83383.41},
        {"type": "FVG", "lower": 82521.31, "upper": 82605.78},
        {"type": "BSL", "lower": 81938.96, "upper": 81938.96},
    ]
    text = ltf_text(event("bos", close=80572.97, break_level=80579.43, entries=entries), context())
    assert text.index("BSL H1") < text.index("FVG H1")
    assert "83 360,01" not in text  # furthest is in Details
    assert "Закрытие H1: 80 572,97" in text
    assert "до границы 1,70%" in text
    assert "Ещё зон: 1" in text
    assert "сам слом не подтверждает вход" in text
    assert len(text.encode("utf-16-le")) // 2 <= 1000
    without_price = ltf_text(event("bos", entries=entries), context())
    assert "до границы" not in without_price
    assert "по близости" not in without_price


def test_duplicate_zones_keep_exception_warning():
    entries = [dict(type="FVG", lower=10, upper=20),
               dict(type="FVG", lower=10, upper=20, outside_premium=True)]
    assert len(ordered_entries(entries)) == 1
    assert ordered_entries(entries)[0]["outside_premium"]
    text = ltf_text(event("entries_ready", entries=entries), context(Direction.BULL))
    assert "вне Discount" in text
    assert "вне Premium" not in text
    assert "Диапазон:" not in text  # delta message


def test_delayed_delivery_is_relative_to_event_not_detection():
    assert delivery_delay(T0, T0 + 60_000) == ""
    assert "9 мин после события" in delivery_delay(T0, T0 + 9 * 60_000)
    assert delivery_delay(T0, T0 - 60_000) == ""


@pytest.mark.parametrize("kind,extra,expected,forbidden", [
    ("bos", {"range_pending": True}, "экстремума тремя свечами H1", "ждём реакцию"),
    ("bos", {}, "вход пока не сформирован", "ждём реакцию"),
    ("touch", {"type": "BSL"}, "ждём закрытия H1", "Снятие ликвидности подтверждено"),
    ("sweep_confirmed", {}, "H1 закрылась обратно", "без возврата"),
    ("sweep_failed", {}, "без возврата", "закрылась обратно"),
    ("sweep_failed", {"outcome": "equal_close"}, "ждём однозначного закрытия", "повторный вход по нему исключён"),
])
def test_next_step_matches_actual_stage(kind, extra, expected, forbidden):
    text = ltf_text(event(kind, **extra), context())
    assert expected in text
    assert forbidden not in text


def test_taken_htf_level_does_not_duplicate_price_or_claim_h1_return():
    ctx = context()
    ev = Event(None, 1, 1, EventKind.LEVEL_TAKEN, occurred_at=T0,
               detected_at=T0, price=83933)
    text = _render_event_block(EventView(ev, ctx.zone, ctx.instrument))
    assert "Цена события" not in text
    assert "исключён из новых входов" in text
    assert "H1 закрылась обратно" not in text  # HTF crossing is not an H1 sweep confirmation
    assert "🎯" not in text


@pytest.mark.parametrize("age,stale", [(3_600_000, False), (43 * 3_600_000, True), (None, True)])
def test_chart_distinguishes_snapshot_time_from_candle_freshness(age, stale):
    cur = dict(instrument={"symbol": "ETHUSDT"}, price=2505.26, quote_at=T0,
               last_closed_h1=T0 - age if age is not None else None,
               stage="Наблюдение", current_scenario=None, selected_context_id=None)
    text = render_chart_caption(cur, T0, "H1", 7)
    assert ("Свечи H1 неактуальны" in text) == stale
    assert "Снимок запроса" in text
    assert "Последнее закрытие H1" in text
    assert "Реальный график" not in text


async def test_same_break_combines_different_parent_entries_without_losing_details(db, instrument_id):
    sender = LogSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    events = []
    for i, entry in enumerate(BOS_PAYLOAD["entries"]):
        _, obs, sc = parent(db, instrument_id, i)
        events.append(_event(db, obs, sc, "bos", {**BOS_PAYLOAD, "entries": [entry]}, f"copy:{i}"))
    assert await disp.deliver(events) == 3
    assert len(sender.cards) == 1
    card = sender.cards[0][0]
    assert len(card.targets) == 3
    assert "FVG H1" in card.text and "OB H1" in card.text
    assert "BSL H1" in card.details
    assert "Ещё зон: 1" in card.text
    # A truly different break still receives its own card.
    changed = _event(db, obs, sc, "bos", {**BOS_PAYLOAD, "break_level": 111}, "copy:new-break")
    await disp.deliver([changed])
    assert len(sender.cards) == 2


def test_downtime_digest_marks_old_assessment_and_shows_recorded_changes(db):
    from app.events import publish, store
    from tests.test_events_publish import _settings, _asmt
    settings = _settings()
    settings.events_digest_gap_seconds = 600
    asmt = _asmt(direction="short", evaluated=T0, aid="copy-old")
    asmt.update(unknown_dependencies=["liquidations"], coverage={"ok": 20, "total": 20})
    store.insert_assessment(db, asmt)
    publish.startup_digest(db, settings, T0 + 43 * 3_600_000)
    bundle = store.list_bundles(db)[0]
    text = publish.render_bundle_text(db, bundle["id"], "https://example.com")
    assert "последняя сохранённая оценка" in text
    assert "43 ч 0 мин" in text
    assert "Оценка на:" in text
    assert "дождитесь новой оценки" in text
    assert "Изменений в журнале не зафиксировано" in text
    assert "Не учтено: ликвидации" in text
    assert "ok;" not in text


async def test_alt_compact_card_keeps_all_target_prices(db):
    from app.notify.alt_queue import AltDispatcher
    from tests.test_alt_notify import _seed, _event as alt_event, _settings, FakeSender, TARGETS
    _seed(db, targets=TARGETS)
    sender = FakeSender()
    dispatcher = AltDispatcher(db, _settings(), sender)
    alt_event(db, 1, "entry_a", {"price": 1.9})
    await dispatcher.dispatch_pending()
    assert len(sender.alt) == 1
    for number in range(1, 5):
        assert f"TP{number}" in sender.alt[0]
