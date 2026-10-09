"""ТЗ 07.10.2026 §7: LTF-уведомления с собственным графиком H1; ошибка
генерации графика видна отдельно от события и досылается без повторного
рыночного сигнала (приёмка T14, T25)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import DetectorConfig, Settings
from app.db import Database
from app.models import now_ms
from app.notify.ltf_queue import LtfDispatcher
from tests.conftest import make_candle
from tests.test_ltf_notify import (
    BOS_PAYLOAD,
    RecSender,
    _event,
    _setup,
)

from tests.test_ltf_notify import T0  # то же время, что у событий test_ltf_notify
H1 = 3_600_000


def _settings(tmp_path) -> Settings:
    s = Settings()
    s.telegram_token = "123:test-token"
    s.telegram_chat_id = "42"
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


def _seed_chart(db: Database, instrument_id: int, tmp_path) -> Settings:
    """H1-свечи для рендера графика события (вокруг T0 — времени события)."""
    db.insert_candles([
        make_candle(T0 - (40 - i) * H1, 100 + i % 5, 101 + i % 5,
                    99 + i % 5, 100 + i % 5,
                    timeframe="H1", instrument_id=instrument_id)
        for i in range(40)
    ])
    return _settings(tmp_path)


async def test_bos_delivered_with_own_chart(db, instrument_id, tmp_path):
    """T14: BOS — собственный график H1 (фото с подписью), не превью ссылки."""
    zone, obs, sc = _setup(db, instrument_id)
    settings = _seed_chart(db, instrument_id, tmp_path)
    ev = _event(db, obs, sc, "bos", BOS_PAYLOAD, "bos:chart:1")
    sender = RecSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender, settings=settings)
    assert await disp.deliver([ev]) == 1
    assert len(sender.photos) == 1
    path, caption, markup = sender.photos[0]
    assert Path(path).exists()
    assert "BOS подтверждён" in caption
    assert markup is not None
    assert sender.texts == []  # короткий текст — целиком в caption
    assert db.get_ltf_event(ev.id).chart_state == "sent"


async def test_chart_failure_visible_and_retried(db, instrument_id, tmp_path,
                                                 monkeypatch):
    """T25: ошибка генерации — текст уходит с «График временно недоступен»,
    рыночное событие не повторяется; retry_charts досылает картинку."""
    zone, obs, sc = _setup(db, instrument_id)
    settings = _seed_chart(db, instrument_id, tmp_path)
    ev = _event(db, obs, sc, "bos", BOS_PAYLOAD, "bos:chart:2")
    sender = RecSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender, settings=settings)

    async def broken(ev_, ctx):
        raise RuntimeError("рендер упал (тест)")

    monkeypatch.setattr(disp, "_render_event_chart", broken)
    assert await disp.deliver([ev]) == 1
    assert sender.photos == []
    assert len(sender.texts) == 1
    assert "График временно недоступен" in sender.texts[0]
    stored = db.get_ltf_event(ev.id)
    assert stored.delivered and stored.chart_state == "failed"

    # рендер восстановился — досылка картинки без повторного текста события
    monkeypatch.undo()
    assert await disp.retry_charts() == 1
    assert len(sender.texts) == 1  # рыночное уведомление не повторялось
    assert len(sender.photos) == 1
    assert "График к событию" in sender.photos[0][1]
    assert db.get_ltf_event(ev.id).chart_state == "sent"
    # повторный прогон — уже ничего не шлёт
    assert await disp.retry_charts() == 0


async def test_text_kinds_without_chart_requirement(db, instrument_id,
                                                    tmp_path):
    """entries_ready — текстовое дополнение списка зон, график не обязателен."""
    zone, obs, sc = _setup(db, instrument_id)
    settings = _seed_chart(db, instrument_id, tmp_path)
    ev = _event(db, obs, sc, "entries_ready", {
        "range": {"lower": 90.0, "upper": 110.0, "mid": 100.0, "version": 1},
        "entries": [{"entry_zone_id": 3, "type": "BSL", "lower": 115.0,
                     "upper": 115.0, "mid": 115.0, "overlap": "full"}],
    }, "entries_ready:chart:3")
    sender = RecSender()
    # settings=None — гейт качества по meta-флагам (не установлены), график
    # для entries_ready и так не требуется
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    assert await disp.deliver([ev]) == 1
    assert len(sender.texts) == 1
    assert db.get_ltf_event(ev.id).chart_state == "none"
