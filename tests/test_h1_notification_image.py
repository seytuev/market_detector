"""Website renderer: event boundary, readable layers, and failed-render retry safety."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app.config import Settings
from app.models_ltf import LtfEvent
from app.models import Direction, Zone, ZoneStatus, ZoneType, TIMEFRAME_MINUTES
from app.notify.h1_image import event_snapshot, render_snapshot, snapshot_html, render_zone_notification
from tests.conftest import make_candle
from tests.test_ltf_notify import T0, _setup

H1 = 3_600_000


def test_event_keeps_context_before_leg_and_excludes_future(db, instrument_id, monkeypatch):
    _zone, obs, _sc = _setup(db, instrument_id)
    candles = [make_candle(T0 + i * H1, 100, 105, 95, 102,
                          timeframe="H1", instrument_id=instrument_id) for i in range(120)]
    db.insert_candles(candles)
    when = candles[-1].close_time
    event = LtfEvent(id=1, observation_id=obs.id, scenario_id=None,
                     kind="entries_ready", payload={}, occurred_at=when,
                     detected_at=when, dedupe_key="website-render")
    seen = {}

    def layers(*args, **kwargs):
        seen.update(kwargs)
        return {"setup": {"frame_from": candles[-12].open_time}, "detected_zones": []}

    monkeypatch.setattr("app.services.h1_chart.assemble_h1_layers", layers)
    before = event_snapshot(db, obs.id, Settings(), event, period_days=2)
    assert len(before["candles"]) == 120, "Keep the structure before the current leg"
    assert seen["as_of"] == when and seen["setup_event"] is True
    assert seen["window_from"] == candles[0].open_time
    db.insert_candles([
        make_candle(when + 1, 1000, 9000, 100, 8000, timeframe="H1", instrument_id=instrument_id),
    ])
    after = event_snapshot(db, obs.id, Settings(), event, period_days=2)
    assert before == after


def sample_snapshot():
    bars = [{"time": T0 // 1000 + i * 3600, "open": 100 + i % 3,
             "high": 105, "low": 95, "close": 103 - i % 3} for i in range(100)]
    return {
        "title": "BTCUSDT · H1 · Зоны готовы", "subtitle": "Снимок события · МСК",
        "event": {"payload": {}}, "candles": bars,
        "layers": {
            "detected_zones": [{"id": 1, "type": "FVG", "direction": "bear",
                                "lower": 101, "upper": 104, "formed_at": T0}],
            "pivot_markers": [{"id": 1, "pivot_at": T0 + 30 * H1,
                               "kind": "high", "role": "LH", "price": 105}],
        },
    }


def test_website_image_contains_candles_and_shared_zone_labels(tmp_path, monkeypatch):
    from app.notify import h1_image
    real_run = h1_image._run_chromium
    dom = []

    def capture(*args, **kwargs):
        result = real_run(*args, **kwargs)
        dom.append(result.stdout)
        return result

    monkeypatch.setattr(h1_image, "_run_chromium", capture)
    path = render_snapshot(sample_snapshot(), tmp_path / "h1.png")
    with Image.open(path) as image:
        assert image.size == (1920, 960)
        assert image.getpixel((1910, 5))[:3] == (255, 255, 255)
        colors = image.convert("RGB").getcolors(image.width * image.height)
        assert sum(n for n, rgb in colors if rgb == (161, 50, 68)) > 1000  # site's bear candles
    assert 'class="h1-zone-tag type-fvg' in dom[0]
    assert 'class="h1-zone-fills"' in dom[0]
    assert 'data-rendered="true"' in dom[0].split('</head>')[0]


def test_incomplete_browser_render_does_not_replace_saved_image(tmp_path, monkeypatch):
    from app.notify import h1_image
    path = tmp_path / "saved.png"
    path.write_bytes(b"saved-event-image")
    monkeypatch.setattr(h1_image, "chromium_path", lambda: "chromium")
    monkeypatch.setattr(h1_image, "_run_chromium", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout='<html data-render-error="Error: missing canvas"></html>',
    ))
    with pytest.raises(RuntimeError, match="missing canvas"):
        render_snapshot(sample_snapshot(), path)
    assert path.read_bytes() == b"saved-event-image"


def test_payload_cannot_close_inline_data_script():
    snapshot = sample_snapshot()
    snapshot["event"]["payload"]["reason"] = '</script><script>fetch("https://example.com")</script>'
    html = snapshot_html(snapshot)
    assert '</script><script>fetch(' not in html
    assert '\\u003c/script>' in html
    assert "connect-src 'none'" in html


@pytest.mark.parametrize("timeframe,kind", [("H1", ZoneType.FVG), ("H4", ZoneType.OB),
                                           ("D1", ZoneType.BSL), ("W1", ZoneType.SSL)])
def test_htf_notifications_use_website_format_on_native_timeframe(tmp_path, monkeypatch, timeframe, kind):
    from app.notify import h1_image
    real_run = h1_image._run_chromium
    dom = []

    def capture(*args, **kwargs):
        result = real_run(*args, **kwargs)
        dom.append(result.stdout)
        return result

    monkeypatch.setattr(h1_image, "_run_chromium", capture)
    step = TIMEFRAME_MINUTES[timeframe] * 60_000
    candles = [make_candle(T0 + i * step, 100, 105, 95, 102, timeframe=timeframe) for i in range(120)]
    zone = Zone(id=1, instrument_id=1, type=kind, direction=Direction.BULL,
                timeframe=timeframe, lower=101, upper=101 if kind in {ZoneType.BSL, ZoneType.SSL} else 104,
                formed_at=T0, confirmed_at=T0, status=ZoneStatus.ACTIVE)
    path = render_zone_notification(candles, zone, tmp_path / (timeframe + '.png'),
                                    "binance spot / BTCUSDT", event_at=candles[-1].close_time,
                                    event_price=102)
    with Image.open(path) as image:
        assert image.size == (1920, 960)
        assert image.getpixel((1910, 5))[:3] == (255, 255, 255)
    assert 'class="h1-zone-tag type-' + kind.value in dom[0]
    assert kind.value.upper() + ' ' + timeframe in dom[0]
    assert 'Цена события' in dom[0]


def test_alt_snapshot_never_imports_later_candles_or_setup_state(db):
    from tests.test_alt_notify import _seed, _event, T0 as ALT_T0, DAY
    from app.notify.alt_queue import AltDispatcher
    from app.notify.alt_chart import alt_snapshot
    from app.models_alt import AltCandle, AltStructureEvent
    from tests.test_alt_notify import FakeSender

    _asset, src, _frozen, setup = _seed(db)
    db.insert_alt_candles([AltCandle(src.id, ALT_T0 + i * DAY, 1.2, 1.8, 1.1, 1.7) for i in range(150)])
    when = ALT_T0 + 140 * DAY - 1
    event = _event(db, setup.id, 'entry_a', {"candle_open_time": ALT_T0 + 139 * DAY}, event_time=when)
    dispatcher = AltDispatcher(db, None, FakeSender())
    ctx = dispatcher._load_context(event)
    before = alt_snapshot(db, ctx, event)
    assert len(before['candles']) == 140
    assert before['markers'][0]['text'] == 'Вход A'
    ctx.setup.state = 'cancelled'
    db.insert_alt_structure_event(AltStructureEvent(None, setup.id, 'BOS', 10, 12, ALT_T0 + 145 * DAY))
    _event(db, setup.id, 'cancelled', {}, event_time=when + 8 * DAY)
    assert alt_snapshot(db, ctx, event) == before


def test_alt_small_prices_render_without_rounding_to_zero(tmp_path, monkeypatch):
    from app.notify import h1_image
    real_run = h1_image._run_chromium
    dom = []

    def capture(*args, **kwargs):
        result = real_run(*args, **kwargs)
        dom.append(result.stdout)
        return result

    monkeypatch.setattr(h1_image, "_run_chromium", capture)
    snapshot = sample_snapshot()
    snapshot.pop('layers')
    snapshot['timeframe'] = 'D1'
    for i, bar in enumerate(snapshot['candles']):
        bar.update(time=T0 // 1000 + i * 86400, open=.0000041, high=.0000048, low=.000004, close=.0000044)
    snapshot['overlays'] = [{"from": T0, "lower": .0000042, "upper": .0000046,
                             "mid": .0000044, "type": "OB", "label": "Диапазон D1"}]
    render_snapshot(snapshot, tmp_path / 'alt.png')
    assert '0,0000044</span>' in dom[0]
    assert 'class="h1-zone-tag type-ob' in dom[0]
