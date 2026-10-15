"""Regressions from the live /now and BTC H1/D1 bot examples."""
from datetime import datetime, timezone

import pytest

from app.bot.cards import _fmt_hhmm, render_chart_caption, render_now
from app.bot.chart_image import current_snapshot, render_bot_chart
from app.config import Settings
from app.models import now_ms
from tests.conftest import make_candle
from tests.test_bot_cards import db, seeded  # noqa: F401


def test_now_uses_moscow_time_and_distinguishes_duplicate_symbols(db, seeded):
    at = int(datetime(2026, 10, 15, 20, 26, tzinfo=timezone.utc).timestamp()*1000)
    assert _fmt_hhmm(at) == "23:26 МСК"
    text = render_now(db, Settings())
    assert "HTF: OB D1" in text
    assert "ETHUSDT · binance" not in text
    assert "BTCUSDT · binance spot" in text
    assert "BTCUSDT · hyperliquid spot" in text
    assert "зон допуска H1:" in text


def _current(now):
    return {"instrument": {"symbol": "BTCUSDT"}, "price": 82395.68, "quote_at": now,
            "stage": "Ожидаем возврат в Discount", "last_closed_h1": now-60_000,
            "selected_context_id": 1, "current_scenario": None,
            "asset": {"governs": True, "asset_state": {"title": "H1 вверх · торговый сценарий пока не открыт"},
                      "structure": {"proven": True, "last_transition": {"kind": "BOS", "break_level": 81938.96}}}}


def test_caption_has_one_authoritative_state_and_d1_has_no_h1_bos():
    now = now_ms()
    cur = _current(now)
    h1 = render_chart_caption(cur, now, "H1", 7)
    assert h1.count("торговый сценарий пока не открыт") == 1
    assert "Ожидаем возврат" not in h1
    assert "BOS подтверждён: уровень 81 938,96" in h1
    daily = render_chart_caption(cur, now, "D1", 0)
    assert "BOS" not in daily and "Этап:" not in daily
    assert "Активные зоны D1/W1" in daily


@pytest.mark.parametrize("tf,step,count", [("H1", 3_600_000, 168), ("D1", 86_400_000, 120)])
def test_current_snapshot_preserves_period_and_renders_stv(db, seeded, tmp_path, tf, step, count):
    now = (now_ms() // step) * step + 1000
    iid = seeded["eth"]
    db.conn.execute("DELETE FROM candle WHERE instrument_id=? AND timeframe=?", (iid, tf))
    db.conn.commit()
    db.insert_candles([make_candle(now-1000-(count-i)*step, 100+i%4, 106+i%4, 94+i%4, 102+i%4,
                                 timeframe=tf, instrument_id=iid) for i in range(count)])
    db.insert_candles([make_candle(now-1000, 102, 105, 99, 103, timeframe=tf, instrument_id=iid, closed=False)])
    settings = Settings()
    snapshot = current_snapshot(db, seeded["obs_bear"].id, tf=tf, period_days=7, settings=settings,
                                now=now, quote_snapshot=(103, now))
    assert snapshot["fit_content"]
    assert len(snapshot["candles"]) == (168 if tf == "H1" else 120)
    assert snapshot["candles"][-1]["close"] == 103
    assert snapshot["quote"] == {"price": 103, "at": now}
    assert "последняя свеча формируется" in snapshot["caption"]
    assert "STV Market Researcher" in snapshot["subtitle"]
    if tf == "D1":
        assert len(snapshot["overlays"]) <= 8
        assert "layers" not in snapshot
    path = render_bot_chart(db, seeded["obs_bear"].id, tmp_path/f"stv-{tf}.png", tf=tf,
                           settings=settings, now=now, quote_snapshot=(103, now))
    from PIL import Image
    with Image.open(path) as img:
        assert img.size == (1920, 960)
        assert img.convert("RGB").getpixel((1900, 5)) == (255, 255, 255)


def test_layer_toggles_hide_pd_and_zone_layers(db, seeded):
    snap = current_snapshot(db, seeded["obs_bear"].id, settings=Settings(), layers=())
    assert snap["layer_settings"] == {"breaks": False, "expected": False, "points": "hidden", "zones": False, "htfContext": False}
    assert snap["layers"]["setup"] is None
    assert snap["layers"]["main_structure"] is None
    assert snap["layers"]["htf_ideas"] == []
