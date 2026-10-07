"""Рендер экспортируемых PNG (ТЗ 07.10.2026 §9–§10; приёмка T22, T23).

Геометрические ассерты на сохранённом PNG/артистах, а не визуальные
скриншоты: заголовок целиком внутри фигуры, подписи не пересекаются,
числа не подменены, «UTC»/биржи нет, время — МСК.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib
import pytest
from matplotlib import pyplot as plt

from app.db import Database
from app.models import Candle, Direction, Zone, ZoneStatus, ZoneType
from app.notify.chartimg import render_zone_chart
from app.notify.chartlabels import _spread_positions

T0 = 1_780_272_000_000  # 2026-06-01 00:00 UTC


def _candles(n: int = 60, base: float = 100.0, step: float = 0.5) -> list[Candle]:
    out = []
    for i in range(n):
        o = base + i * step
        out.append(Candle(
            instrument_id=1, timeframe="D1",
            open_time=T0 + i * 86_400_000,
            close_time=T0 + (i + 1) * 86_400_000,
            open=o, high=o + 1.0, low=o - 1.0, close=o + step / 2,
        ))
    return out


def _zone(lower: float, upper: float, ztype: ZoneType = ZoneType.FVG) -> Zone:
    return Zone(
        id=None, instrument_id=1, type=ztype, direction=Direction.BULL,
        timeframe="D1", lower=lower, upper=upper, formed_at=T0,
        confirmed_at=T0, status=ZoneStatus.ACTIVE,
    )


def _render_fig(monkeypatch, *args, **kwargs):
    """Рендер с захватом фигуры до plt.close (для геометрических ассертов)."""
    figs = []
    real_close = plt.close
    monkeypatch.setattr(plt, "close", lambda fig=None: figs.append(fig))
    path = render_zone_chart(*args, **kwargs)
    monkeypatch.setattr(plt, "close", real_close)
    return path, figs[0]


def test_png_min_width_1280(tmp_path):
    """§10.3: ширина экспорта ≥ 1280 px."""
    path = render_zone_chart(_candles(), _zone(110.0, 112.0),
                             tmp_path / "z.png", "binance spot / BTCUSDT")
    img = matplotlib.image.imread(path)
    assert img.shape[1] >= 1280


def test_header_inside_figure(tmp_path, monkeypatch):
    """§10.1: все строки заголовка/футера внутри границ сохранённой фигуры,
    включая длинный символ (T23)."""
    _, fig = _render_fig(
        monkeypatch, _candles(), _zone(110.0, 112.0), tmp_path / "z.png",
        "binance spot / VERYLONGSYMBOLUSDT",
    )
    renderer = fig.canvas.get_renderer()
    bbox = fig.bbox
    texts = [t for t in fig.texts if t.get_text()]
    assert texts, "заголовок должен существовать"
    for t in texts:
        tb = t.get_window_extent(renderer)
        assert tb.x0 >= bbox.x0 - 1 and tb.x1 <= bbox.x1 + 1
        assert tb.y0 >= bbox.y0 - 1 and tb.y1 <= bbox.y1 + 1
    joined = " ".join(t.get_text() for t in texts)
    assert "UTC" not in joined and "binance" not in joined
    assert "МСК" in joined


def test_narrow_fvg_labels_do_not_overlap(tmp_path, monkeypatch):
    """§10.2/T22: узкая FVG с близкими low/mid/high — все три числа
    читаемы (не пересекаются) и не подменены."""
    _, fig = _render_fig(
        monkeypatch, _candles(base=80.0, step=0.01),
        _zone(81.951, 82.563 * 0 + 82.063),  # узкая зона
        tmp_path / "z.png", "binance spot / BTCUSDT",
    )
    ax = fig.axes[0]
    renderer = fig.canvas.get_renderer()
    price_texts = [
        t for t in ax.texts
        if t.get_text() and any(ch.isdigit() for ch in t.get_text())
    ]
    assert len(price_texts) >= 3, "low/mid/high подписаны"
    boxes = sorted(
        (t.get_window_extent(renderer) for t in price_texts),
        key=lambda b: -b.y0,
    )
    for a, b in zip(boxes, boxes[1:]):
        # вертикальный зазор между соседними подписями
        assert a.y0 >= b.y1 - 1, f"наложение: {a} vs {b}"
    values = {t.get_text().split()[-1] for t in price_texts}
    assert "81,951" in values and "82,063" in values


def test_level_zone_has_label(tmp_path, monkeypatch):
    """§9.6: уровень SSL/BSL подписан типом и ценой; середины нет (T11)."""
    _, fig = _render_fig(
        monkeypatch, _candles(), _zone(2600.15, 2600.15, ZoneType.SSL),
        tmp_path / "z.png", "binance spot / ETHUSDT",
    )
    ax = fig.axes[0]
    texts = [t.get_text() for t in ax.texts if t.get_text()]
    assert any("SSL" in t and "2 600,15" in t for t in texts)
    assert not any("50%" in t for t in texts)


def test_spread_positions_pure():
    """Де-оверлап: зазор не меньше высоты строки, порядок по цене сохранён."""
    ys = _spread_positions([10.0, 9.99, 9.98, 5.0], 0.05)
    assert ys == sorted(ys, reverse=True)
    for a, b in zip(ys, ys[1:]):
        assert a - b >= 0.05 - 1e-9


def test_example_images_for_report(tmp_path):
    """Примеры «после» для отчёта: узкая FVG и SSL после снятия."""
    out_dir = Path("data/charts")
    p1 = render_zone_chart(
        _candles(base=80.0, step=0.01), _zone(81.951, 82.063),
        out_dir / "tz_0710_narrow_fvg_after.png", "binance spot / BTCUSDT",
    )
    taken = _zone(2600.15, 2600.15, ZoneType.SSL)
    taken = Zone(
        id=None, instrument_id=1, type=ZoneType.SSL, direction=Direction.BULL,
        timeframe="D1", lower=2600.15, upper=2600.15, formed_at=T0,
        confirmed_at=T0, status=ZoneStatus.TAKEN,
    )
    p2 = render_zone_chart(
        _candles(base=2550.0, step=1.0), taken,
        out_dir / "tz_0710_ssl_taken_after.png", "binance spot / ETHUSDT",
    )
    assert Path(p1).exists() and Path(p2).exists()


def test_far_htf_context_does_not_squeeze(db, tmp_path, monkeypatch):
    """§9.2/T24: родитель далеко за масштабом — свечи не сжимаются,
    значения контекста доступны текстом."""
    import matplotlib.pyplot as plt
    from app.bot.charts import render_ltf_chart, layers_from_mask, DEFAULT_MASK
    from tests.test_bot_chart import _settings, chart_seeded as _chart_fx
    from tests.test_bot_cards import seeded as _seeded_fx

    chart_seeded = _chart_fx.__wrapped__(db, _seeded_fx.__wrapped__(db))

    # переносим родительскую зону далеко от цен свечей (~100)
    db.update_zone(chart_seeded["z_d1"], lower=9000.0, upper=9100.0)
    settings = _settings(tmp_path)
    figs = []
    monkeypatch.setattr(plt, "close", lambda fig=None: figs.append(fig))
    path = render_ltf_chart(
        db, chart_seeded["obs_bear"].id, tmp_path / "far.png",
        tf="H1", period_days=3, layers=layers_from_mask(DEFAULT_MASK),
        settings=settings,
    )
    assert path is not None
    ax = figs[0].axes[0]
    y0, y1 = ax.get_ylim()
    assert y1 < 1000.0, "масштаб не должен включать далёкую HTF-зону"
    note = " ".join(t.get_text() for t in figs[0].texts)
    assert "вне окна" in note and "9 000,00" in note
