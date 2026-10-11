"""Приёмка графика события: масштаб H1, снимок, подписи, повтор картинки.

Цены не выдумываются. Детекция не участвует: фикстуры задают свечи и запись
события напрямую.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
import pytest
from matplotlib import pyplot as plt

from app.config import DetectorConfig, Settings
from app.models import Direction
from app.models_ltf import LtfEvent, LtfStructureEvent
from app.notify.chart_snapshot import chart_signature, right_pad
from app.notify.formatting import fmt_price_ru, fmt_time_msk
from tests.conftest import make_candle
from tests.test_ltf_notify import T0, _setup

H1 = 3_600_000


def _settings(tmp_path) -> Settings:
    settings = Settings()
    settings.db_path = str(tmp_path / "htf_zones.db")
    return settings


def _series(instrument_id: int, n: int = 14, base: float = 1200.0):
    return [
        make_candle(
            T0 + i * H1, base, base + 8, base - 8, base + 1,
            timeframe="H1", instrument_id=instrument_id,
        )
        for i in range(n)
    ]


def _event_at(candles) -> int:
    return candles[-1].close_time


def _render(db, obs, path, settings, candles, *, event=None, **kwargs):
    from app.bot.charts import render_ltf_chart
    figs = []
    real_close = plt.close
    plt.close = lambda fig=None: figs.append(fig)
    try:
        out = render_ltf_chart(
            db, obs.id, path, tf="H1", period_days=kwargs.pop("period_days", 7),
            settings=settings, now=_event_at(candles),
            end_ms=_event_at(candles) if event is not None else kwargs.get("end_ms"),
            event=event, **kwargs,
        )
    finally:
        plt.close = real_close
    return out, figs[0]


def _texts(fig) -> str:
    chunks = [t.get_text() for t in fig.texts if t.get_text()]
    for ax in fig.axes:
        chunks.extend(t.get_text() for t in ax.texts if t.get_text())
    return "\n".join(chunks)


def _inside(fig) -> None:
    fig.canvas.draw()
    renderer = fig.canvas.get_renderer()
    bbox = fig.bbox
    for artist in list(fig.texts):
        if not artist.get_text() or not artist.get_visible():
            continue
        box = artist.get_window_extent(renderer)
        assert box.x0 >= bbox.x0 - 2 and box.x1 <= bbox.x1 + 2
        assert box.y0 >= bbox.y0 - 2 and box.y1 <= bbox.y1 + 2
    for ax in fig.axes:
        for artist in ax.texts:
            if not artist.get_text() or not artist.get_visible():
                continue
            if artist.get_gid() == "secondary":
                continue
            box = artist.get_window_extent(renderer)
            assert box.x1 <= bbox.x1 + 2
            assert box.y0 >= bbox.y0 - 2 and box.y1 <= bbox.y1 + 2


def test_right_pad_stays_under_ten_percent():
    assert right_pad(14) <= 2
    assert right_pad(14) / 14 <= 0.10
    assert right_pad(60) / 60 <= 0.10


def test_short_series_fills_the_width(db, instrument_id, tmp_path, monkeypatch):
    """14 свечей не оставляют половину полотна пустой, окно названо фактом."""
    candles = _series(instrument_id, 14, base=1220.0)
    db.insert_candles(candles)
    _zone, obs, _sc = _setup(db, instrument_id)
    db.update_zone(obs.zone_id, lower=751.53, upper=888.37)
    settings = _settings(tmp_path)
    monkeypatch.setattr(plt, "close", lambda fig=None: None)
    path, fig = _render(db, obs, tmp_path / "short.png", settings, candles)
    assert path
    ax = fig.axes[0]
    n = 14
    _x0, x1 = ax.get_xlim()
    empty = (x1 - (n - 0.5)) / (x1 + 0.5)
    assert empty < 0.15
    y0, y1 = ax.get_ylim()
    assert y0 > 1000, "далёкая W1-зона не должна опустить масштаб к 751"
    note = _texts(fig)
    assert "вне окна" in note and "751,53" in note and "888,37" in note
    assert "14 свечей H1" in note
    assert "история короче" in note
    assert "история короче" in note or "обзор до" in note
    img = matplotlib.image.imread(path)
    assert img.shape[1] >= 1280
    _inside(fig)


def test_event_snapshot_ignores_later_candles(db, instrument_id, tmp_path):
    """Свечи и слом после события не меняют масштаб и цену на картинке."""
    candles = _series(instrument_id, 20, base=100.0)
    db.insert_candles(candles)
    _zone, obs, sc = _setup(db, instrument_id)
    when = _event_at(candles)
    ev = LtfEvent(
        id=7, observation_id=obs.id, scenario_id=sc.id, kind="bos",
        payload={"direction": "bear", "break_level": 108.0, "kind": "BOS"},
        occurred_at=when, detected_at=when, dedupe_key="bos-snap",
    )
    settings = _settings(tmp_path)
    _path, fig = _render(db, obs, tmp_path / "before.png", settings, candles, event=ev)
    y_before = fig.axes[0].get_ylim()
    note = _texts(fig)
    assert "BOS" in note and fmt_price_ru(108.0) in note
    assert fmt_time_msk(when) in note
    assert "Entry long" not in note

    db.insert_candles([
        make_candle(
            when + H1, 5000, 9000, 4900, 8000,
            timeframe="H1", instrument_id=instrument_id,
        )
    ])
    later = db.insert_ltf_scenario(type(sc)(
        id=None, observation_id=obs.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=when + H1,
    ))
    db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=later.id, kind="bos", stage="primary",
        direction=Direction.BULL, break_level=8000.0,
        break_candle_open_time=when + H1, occurred_at=when + 2 * H1,
        detected_at=when + 2 * H1, level_key="later",
    ))
    _path2, fig2 = _render(db, obs, tmp_path / "after.png", settings, candles, event=ev)
    y_after = fig2.axes[0].get_ylim()
    assert y_after[1] < 1000
    assert abs(y_before[1] - y_after[1]) < 1.0
    assert "8 000,00" not in _texts(fig2)
    assert fmt_price_ru(108.0) in _texts(fig2)


def test_cancellation_keeps_its_scenario(db, instrument_id, tmp_path):
    candles = _series(instrument_id, 16, base=100.0)
    db.insert_candles(candles)
    _zone, obs, old = _setup(db, instrument_id)
    when = _event_at(candles)
    db.update_ltf_scenario(
        old.id, state="cancelled", cancellation_reason="reverse_bos",
        cancelled_at=when, reverse_break_level_price=104.0,
    )
    db.insert_ltf_scenario(type(old)(
        id=None, observation_id=obs.id, direction=Direction.BULL,
        trigger="BOS", stage="primary", state="monitoring_entries",
        created_at=when + 1,
    ))
    db.insert_ltf_structure_event(LtfStructureEvent(
        id=None, scenario_id=old.id, kind="bos", stage="primary",
        direction=Direction.BEAR, break_level=104.0,
        break_candle_open_time=candles[-1].open_time, occurred_at=when,
        detected_at=when, level_key="cancel-level",
    ))
    ev = LtfEvent(
        id=8, observation_id=obs.id, scenario_id=old.id, kind="cancellation",
        payload={"reason": "reverse_bos"},
        occurred_at=when, detected_at=when, dedupe_key="cancel-snap",
    )
    settings = _settings(tmp_path)
    _path, fig = _render(db, obs, tmp_path / "cancel.png", settings, candles, event=ev)
    note = _texts(fig)
    assert "Отмена" in note
    assert "отмен" in note
    assert "обратный BOS" in note
    assert fmt_price_ru(104.0) in note


def test_liquidity_is_not_called_an_entry(db, instrument_id, tmp_path):
    candles = _series(instrument_id, 16, base=110.0)
    db.insert_candles(candles)
    _zone, obs, sc = _setup(db, instrument_id)
    when = _event_at(candles)
    ev = LtfEvent(
        id=9, observation_id=obs.id, scenario_id=sc.id, kind="touch",
        payload={
            "type": "SSL", "lower": 110.0, "upper": 110.0,
            "direction": "bear", "candle_open_time": candles[-1].open_time,
        },
        occurred_at=when, detected_at=when, dedupe_key="touch-ssl",
    )
    settings = _settings(tmp_path)
    _path, fig = _render(
        db, obs, tmp_path / "ssl.png", settings, candles, event=ev,
        layers=("entries",),
    )
    note = _texts(fig)
    assert "SSL ликвидность" in note
    assert "Entry long" not in note and "Entry short" not in note
    assert "Касание" in note


def test_current_chart_marks_unclosed_candle(db, instrument_id, tmp_path):
    """Текущий график показывает цену незакрытого бара. Событие её не берёт."""
    candles = _series(instrument_id, 16, base=100.0)
    last = candles[-1]
    forming = make_candle(
        last.open_time + H1, 100, 112, 99, 111,
        timeframe="H1", instrument_id=instrument_id, closed=False,
    )
    db.insert_candles([*candles, forming])
    _zone, obs, _sc = _setup(db, instrument_id)
    settings = _settings(tmp_path)
    from app.bot.charts import render_ltf_chart
    figs = []
    real_close = plt.close
    plt.close = lambda fig=None: figs.append(fig)
    try:
        path = render_ltf_chart(
            db, obs.id, tmp_path / "live.png", tf="H1", period_days=3,
            settings=settings, now=forming.open_time + 60_000,
        )
    finally:
        plt.close = real_close
    assert path
    assert "не закрыта" in _texts(figs[0])
    assert fmt_price_ru(111) in _texts(figs[0])


def test_labels_merge_same_price_and_keep_pd_name():
    from app.notify.chartlabels import layout_price_labels
    fig, ax = plt.subplots(figsize=(12.8, 7.2))
    ax.set_ylim(90, 120)
    ax.set_xlim(0, 10)
    artists = layout_price_labels(fig, ax, [
        (100.0, "BOS 100,00", "#fff", True),
        (100.0, "Цена 100,00", "#fff", True),
        (110.0, "PD 50%", "#9cf", False),
    ])
    joined = " ".join(t.get_text() for t in artists)
    assert "BOS 100,00 + Цена 100,00" in joined
    assert "PD 50%" in joined
    plt.close(fig)


def test_long_secondary_label_moves_inside(db, instrument_id, tmp_path):
    candles = _series(instrument_id, 20, base=100.0)
    db.insert_candles(candles)
    _zone, obs, sc = _setup(db, instrument_id)
    when = _event_at(candles)
    long_name = "Очень длинное имя уровня которое не должно обрезаться краем"
    ev = LtfEvent(
        id=10, observation_id=obs.id, scenario_id=sc.id, kind="entries_ready",
        payload={
            "range": {"lower": 96.0, "upper": 112.0, "mid": 104.0, "version": 1},
            "entries": [{
                "type": long_name, "lower": 101.0, "upper": 103.0,
                "direction": "bear",
            }],
        },
        occurred_at=when, detected_at=when, dedupe_key="long-label",
    )
    settings = _settings(tmp_path)
    _path, fig = _render(
        db, obs, tmp_path / "long.png", settings, candles, event=ev,
        layers=("entries", "range"),
    )
    note = _texts(fig)
    assert "PD 50%" in note
    assert long_name in note or "101,00" in note
    _inside(fig)
    img = matplotlib.image.imread(_path)
    import numpy as np
    target = 390
    idx = np.linspace(0, img.shape[1] - 1, target).astype(int)
    idy = np.linspace(0, img.shape[0] - 1, int(target * img.shape[0] / img.shape[1])).astype(int)
    small = img[idy][:, idx]
    assert small.shape[1] == 390 and small.std() > 0


@pytest.mark.asyncio
async def test_retry_reuses_png_without_recompute(db, instrument_id, tmp_path):
    from app.notify.ltf_queue import LtfDispatcher
    from tests.test_ltf_notify import RecSender

    candles = _series(instrument_id, 20, base=100.0)
    db.insert_candles(candles)
    _zone, obs, sc = _setup(db, instrument_id)
    when = _event_at(candles)
    ev, _created = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs.id, scenario_id=sc.id, kind="bos",
        payload={"break_level": 108.0, "direction": "bear"},
        occurred_at=when, detected_at=when, dedupe_key="bos-reuse",
    ))
    settings = _settings(tmp_path)
    disp = LtfDispatcher(db, DetectorConfig(), RecSender(), settings=settings)
    ctx = disp._load_context(ev)
    first = await disp._render_event_chart(ev, ctx)
    assert first and Path(first).exists()
    Path(first).write_bytes(b"sentinel-png")
    second = await disp._render_event_chart(ev, ctx)
    assert second == first
    assert Path(second).read_bytes() == b"sentinel-png"

    other, _created = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs.id, scenario_id=sc.id, kind="cancellation",
        payload={"reason": "reverse_bos", "break_level": 104.0},
        occurred_at=when, detected_at=when, dedupe_key="cancel-reuse",
    ))
    assert chart_signature(other) != chart_signature(ev)
    third = await disp._render_event_chart(other, disp._load_context(other))
    assert third != first
    assert Path(third).exists()
    assert Path(third).read_bytes() != b"sentinel-png"
