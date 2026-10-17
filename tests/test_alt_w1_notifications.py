"""Owner's HBAR notification: safe copy, event-time chart and delivery fallback."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.alt.engine_v3 import DAY, WEEK
from app.alt.store_v3 import insert_event
from app.config import Settings
from app.models_alt import AltCandle, AltInstrumentSource
from app.notify import alt_w1
from app.notify.alt_queue import AltDispatcher
from app.notify.compact import notification_html
from app.notify.navigation import card_keyboard
from app.notify.outbox import Card
from app.notify.telegram import LogSender, TelegramSender
from tools.preview_alt_w1_notification import example


@pytest.fixture()
def hbar(db):
    ctx, rows = example(db)
    return db, ctx, rows


def test_hbar_card_names_zone_cycle_and_separate_invalidation_rules(hbar):
    _, ctx, _ = hbar
    text, details = alt_w1.message(ctx, delivered_at=ctx.lead["event_time_ms"])
    assert text.startswith("HBAR · Касание FVG W1\nBinance Spot · HBARUSDT")
    assert "FVG W1: 0,0423–0,0507" in text
    assert "Цена касания: 0,045" in text
    assert "База 2022–2023: 0,055–0,0845 · исторические уровни" in text
    assert "Отмена FVG: закрытие W1 ниже 0,0423" in text
    assert "Отмена сценария: закрытие W1 ниже 0,0356" in text
    assert "15.10.2026 23:53 МСК" in text
    assert len(text.encode("utf-16-le"))//2 < 1000
    assert len(details) > len(text) and "Тени W1 сценарий не отменяют" in details
    html = notification_html(text, 1000)
    assert html.startswith("<b>HBAR · Касание FVG W1</b>\n")
    assert "&lt;b&gt;" not in html and "<b>" not in text


def test_transport_escapes_untrusted_symbols_once(hbar):
    _, ctx, _ = hbar
    ctx.asset.symbol = "<coin&>"
    text, _ = alt_w1.message(ctx, delivered_at=ctx.lead["event_time_ms"])
    html = notification_html(text, 1000)
    assert "&lt;coin&amp;&gt;" in html
    assert "&amp;lt;" not in html


def test_zone_invalidation_keeps_bounds_and_does_not_cancel_whole_scenario(hbar):
    db, ctx, _ = hbar
    ctx.lead["event_type"] = "zone_invalidated"
    ctx.lead["price"] = .05
    ctx.lead["payload"].pop("zone")
    zone = next(z for z in ctx.frame["scenario_state"]["pd_zones"] if z["id"] == ctx.lead["payload"]["zone_id"])
    zone["invalidated_at_ms"] = ctx.lead["event_time_ms"]
    text, _ = alt_w1.message(ctx, delivered_at=ctx.lead["event_time_ms"])
    assert "Зона FVG отменена W1" in text and "FVG W1: 0,0423–0,0507" in text
    assert "Закрытие W1: 0,05" in text
    assert "Отмена сценария: закрытие W1 ниже 0,0356" in text
    scene = alt_w1.snapshot(db, ctx)
    changed = next(z for z in scene["overlays"] if z["label"] == "FVG W1 · отменена")
    assert changed["muted"]


def test_depression_notice_has_no_pre_bos_cancellation(hbar):
    _, ctx, _ = hbar
    ctx.lead["event_type"] = "manipulation_started"
    state = ctx.frame["scenario_state"]
    state["scenarios"] = []
    state["depression"] = {"minimum": .0356, "bos_level": .0586}
    text, _ = alt_w1.message(ctx, delivered_at=ctx.lead["event_time_ms"])
    assert "Минимум депрессии: 0,0356" in text
    assert "Ожидается закрытие W1 выше 0,0586" in text
    assert "Отмена сценария:" not in text


@pytest.mark.parametrize("precision", ["quote_crossing", "D1"])
def test_approximate_observations_do_not_claim_an_exact_touch(hbar, precision):
    db, ctx, _ = hbar
    ctx.lead["payload"]["precision"] = precision
    text, _ = alt_w1.message(ctx, delivered_at=ctx.lead["event_time_ms"])
    scene = alt_w1.snapshot(db, ctx)
    assert "Цена касания:" not in text
    if precision == "quote_crossing":
        assert "Пересечение FVG W1" in text
        assert "Цена сделки после пересечения" in text
        assert scene["quote"]["label"] == "Сделка после пересечения"
    else:
        assert "точное время касания неизвестно" in text
        assert "quote" not in scene


def test_w1_chart_excludes_future_and_incomplete_weeks(hbar):
    db, ctx, rows = hbar
    before = alt_w1.snapshot(db, ctx)
    at = ctx.lead["event_time_ms"]
    assert before["timeframe"] == "W1" and before["quote"]["price"] == .045
    assert all(bar["time"]*1000+WEEK <= at for bar in before["candles"])
    assert 30 < len(before["candles"]) <= 52
    selected = next(z for z in before["overlays"] if z["label"] == "FVG W1 · тест")
    assert (selected["lower"], selected["upper"]) == (.0423, .0507)
    assert not any(z["type"] == "base" for z in before["overlays"])
    reference = next(z for z in before["overlays"] if z["type"] == "base_reference")
    assert (reference["lower"], reference["upper"]) == (.055, .0845)
    assert reference["from"] == before["candles"][0]["time"]*1000
    assert reference["to"] is None and "Исторические уровни базы 2022–2023" == reference["label"]
    assert {l["label"] for l in before["levels"]} >= {"L", "M", "U"}
    future = (at//DAY+1)*DAY
    db.insert_alt_candles([AltCandle(ctx.source.id, future+i*DAY, 1, 999, .01, 100) for i in range(14)])
    again = alt_w1.load_context(db, rows)
    assert alt_w1.snapshot(db, again) == before


@pytest.mark.parametrize("ended", [False, True])
def test_base_inside_chart_retains_its_actual_period(hbar, ended):
    db, ctx, _ = hbar
    first = alt_w1.snapshot(db, ctx)["candles"][0]["time"]*1000
    ctx.frame["base_start_open_time"] = first + 8*WEEK
    ctx.frame["base_end_open_time"] = first + 19*WEEK if ended else None
    ctx.frame["base_end_confirmed_at_ms"] = first + 20*WEEK if ended else None
    scene = alt_w1.snapshot(db, ctx)
    base = next(z for z in scene["overlays"] if z["type"] == "base")
    assert base["from"] == ctx.frame["base_start_open_time"]
    assert base["to"] == (ctx.frame["base_end_confirmed_at_ms"] or ctx.lead["event_time_ms"])
    assert not any(z["type"] == "base_reference" for z in scene["overlays"])


@pytest.mark.parametrize("lower,upper", [(.0000051, .00000988), (1., 2.)])
def test_historical_base_bounds_stay_visible_even_outside_recent_prices(hbar, lower, upper):
    db, ctx, _ = hbar
    ctx.frame.update(lower=lower, upper=upper, mid=(lower+upper)/2,
                     scenario_state={"pd_zones": [], "scenarios": []})
    scene = alt_w1.snapshot(db, ctx)
    assert scene["price_bounds"]["min"] <= lower
    assert scene["price_bounds"]["max"] >= upper


def test_retry_keeps_original_venue_after_primary_source_changes(hbar):
    db, ctx, rows = hbar
    before = alt_w1.snapshot(db, ctx)
    newer = db.upsert_alt_instrument_source(AltInstrumentSource(None, ctx.asset.id, "bybit", "HBARUSDT", source_version=2))
    start = before["candles"][0]["time"]*1000
    db.insert_alt_candles([AltCandle(newer.id, start+i*DAY, 500, 999, 400, 700) for i in range(14)])
    restored = alt_w1.load_context(db, rows)
    assert restored.source.id == ctx.source.id and restored.source.venue == "binance"
    assert alt_w1.snapshot(db, restored) == before


async def test_image_failure_preserves_card_then_retry_caches_event_image(hbar, tmp_path, monkeypatch):
    db, ctx, rows = hbar
    settings = Settings()
    settings.db_path = str(tmp_path/"state.db")
    settings.public_base_url = "https://example.com"
    dispatcher = AltDispatcher(db, settings, LogSender())
    row = {"id": 910, "status": "pending", "card": "{}"}
    members = [{"event_id": r["id"], "reason": None} for r in rows]
    calls = []

    def broken(*args):
        calls.append("failed")
        raise RuntimeError("Chromium unavailable")

    monkeypatch.setattr(alt_w1, "render_chart", broken)
    fallback = await dispatcher._render_v3(row, members)
    assert fallback.chart_pending and not fallback.image_path
    assert "Цена касания: 0,045" in fallback.text and fallback.details

    def render(_db, _ctx, path):
        calls.append("rendered")
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (10, 10), "white").save(path)
        return str(path)

    monkeypatch.setattr(alt_w1, "render_chart", render)
    complete = await dispatcher._render_v3(row, members)
    assert Path(complete.image_path).is_file() and not complete.chart_pending
    cached = await dispatcher._render_v3(row, members)
    assert cached.image_path == complete.image_path and calls == ["failed", "rendered"]
    assert complete.targets[0]["kind"] == "alt_w1"
    assert complete.targets[0]["url"] == f"https://example.com/alt.html?base={ctx.structure_id}&tf=W1"
    buttons = card_keyboard(910, complete.targets).inline_keyboard[0]
    assert len(buttons) == 2 and buttons[0].url == complete.targets[0]["url"]
    assert buttons[1].callback_data == "nf:d:910:0"


async def test_late_zone_updates_photo_for_the_same_packet(hbar, tmp_path, monkeypatch):
    db, ctx, rows = hbar
    settings = Settings()
    settings.db_path = str(tmp_path/"state.db")
    dispatcher = AltDispatcher(db, settings, LogSender())

    def render(_db, _ctx, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (10, 10), "white").save(path)
        return str(path)

    monkeypatch.setattr(alt_w1, "render_chart", render)
    row = {"id": 911, "status": "pending", "card": "{}"}
    members = [{"event_id": r["id"], "reason": None} for r in rows]
    first = await dispatcher._render_v3(row, members)
    next_event = {**ctx.lead, "id": "second-zone", "event_time_ms": ctx.lead["event_time_ms"]+1000,
        "price": ctx.frame["lower"], "payload": {**ctx.lead["payload"], "zone_id": ctx.frame["structure_key"]+":L",
            "kind": "boundary", "zone": {"lower": ctx.frame["lower"], "upper": ctx.frame["lower"]}}}
    insert_event(db, ctx.asset.id, ctx.source.id, ctx.frame["structure_key"], next_event)
    rows = db.conn.execute("SELECT * FROM alt_v3_event WHERE delivered=0 ORDER BY id").fetchall()
    updated = await dispatcher._render_v3({**row, "status": "sent", "card": json.dumps({"image_path":first.image_path})},
                                         [{"event_id":r["id"],"reason":None} for r in rows])
    assert updated.image_path != first.image_path and updated.replace_image
    assert "Событий в сообщении: 2" in updated.text
    assert "Нижняя граница базы" in updated.details and "FVG W1" in updated.details


async def test_telegram_sends_photo_with_one_bold_heading_and_working_details(hbar, tmp_path):
    _, ctx, _ = hbar
    text, details = alt_w1.message(ctx, delivered_at=ctx.lead["event_time_ms"])
    image = tmp_path/"image.png"
    Image.new("RGB", (10, 10), "white").save(image)
    calls = []

    class Bot:
        async def send_photo(self, **kwargs):
            assert kwargs["photo"].read().startswith(b"\x89PNG")
            calls.append(kwargs)
            return SimpleNamespace(message_id=42)

    sender = object.__new__(TelegramSender)
    sender.chat_id = "owner"
    sender._bot = Bot()
    card = Card(text, details, str(image), [{"kind":"alt_w1","url":"https://example.com/alt.html?base=5&tf=W1"}])
    assert await sender.send_card(card, 10) == 42
    assert calls[0]["parse_mode"] == "HTML"
    assert calls[0]["caption"].startswith("<b>HBAR · Касание FVG W1</b>")
    assert "&lt;b&gt;" not in calls[0]["caption"]
    assert calls[0]["reply_markup"].inline_keyboard[0][-1].callback_data == "nf:d:10:0"


async def test_telegram_replaces_image_when_packet_gains_a_zone(tmp_path):
    image = tmp_path/"updated.png"
    Image.new("RGB", (10, 10), "white").save(image)
    calls = []

    class Bot:
        async def edit_message_media(self, **kwargs):
            calls.append(kwargs)

    sender = object.__new__(TelegramSender)
    sender.chat_id = "owner"
    sender._bot = Bot()
    card = Card("HBAR · Касание FVG W1", "Обе зоны", str(image), [{"kind":"alt_w1"}])
    card.replace_image = True
    await sender.edit_card(42, card, 10, photo=True)
    assert calls[0]["message_id"] == 42
    assert calls[0]["media"].caption == "<b>HBAR · Касание FVG W1</b>"
    assert calls[0]["media"].parse_mode == "HTML"


def test_w1_png_uses_website_style_and_full_zone_bounds(hbar, tmp_path, monkeypatch):
    db, ctx, _ = hbar
    from app.notify import h1_image
    real_run = h1_image._run_chromium
    dom = []

    def capture(*args, **kwargs):
        result = real_run(*args, **kwargs)
        dom.append(result.stdout)
        return result

    monkeypatch.setattr(h1_image, "_run_chromium", capture)
    path = alt_w1.render_chart(db, ctx, tmp_path/"hbar.png")
    with Image.open(path) as image:
        assert image.size == (1920, 960)
        assert image.getpixel((1910, 5))[:3] == (255, 255, 255)
    assert 'data-rendered="true"' in dom[0]
    assert "FVG W1 · тест" in dom[0] and "0,0423–0,0507" in dom[0]
    assert "Цена касания" in dom[0]
    assert 'class="h1-zone zone-rect z-base_reference"' in dom[0]
    assert "Исторические уровни базы 2022–2023</span>" in dom[0]
    assert "border-style: dashed" in dom[0]
