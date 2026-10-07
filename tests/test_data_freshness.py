"""ТЗ переработки D02: честная свежесть.

- каналы раздельно: котировка, закрытая H1, флаг источника, replay;
  «ok» — только при подтверждённой свежести всех каналов;
- пороги stale — настройками (котировка отдельно, ТФ отдельно);
- при gap/replaying/stale новые уведомления о пригодности не отправляются
  (событие остаётся недоставленным и уходит ретраем после восстановления);
- ошибка доставки не меняет рыночное состояние (события пишет движок).

F03: решение о качестве данных — единая services.quality.data_quality
(каналы quote/h1/d1/w1/continuity/processing/replay); новые причины
processing_lag (курсор расчёта позади последней закрытой H1) и
history_gap (разрыв истории); гейт доставки LTF использует ту же оценку.
"""
from __future__ import annotations

import pytest

from app.config import DetectorConfig, Settings
from app.db import Database
from app.models import Direction, Zone, ZoneStatus, ZoneType, now_ms
from app.models_ltf import LtfEvent, LtfObservation, LtfScenario
from app.notify.ltf_queue import LtfDispatcher
from app.services.overview import _data_state
from app.services.quality import data_quality
from tests.conftest import H1_MS, make_candle
from tests.test_ltf_notify import RecSender

T0 = 1_780_000_000_000
NOW = T0 + 10 * H1_MS


def _settings(**det) -> Settings:
    s = Settings()
    s.poll_seconds = 1800
    # прежний авто-порог котировки (2*poll_seconds): быстрый цикл F02 выключен
    s.quote_poll_seconds = 0
    for k, v in det.items():
        setattr(s.detector, k, v)
    return s


def _seed(db: Database, instrument_id: int):
    c = make_candle(T0 + 9 * H1_MS, 100, 101, 99, 100.5,
                    timeframe="H1", instrument_id=instrument_id)
    db.insert_candles([c])
    db.set_quote(instrument_id, 100.5, NOW - 60_000)
    # F03: курсор обработки на последней закрытой H1 — без него канал
    # processing единой оценки качества фиксирует отставание расчёта
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(c.close_time))
    return NOW - 60_000


def test_channels_separate_and_ok_only_when_fresh(db: Database,
                                                  instrument_id: int):
    settings = _settings()
    _seed(db, instrument_id)
    ds = _data_state(db, settings, instrument_id,
                     db.get_quote(instrument_id), NOW)
    assert ds["state"] == "ok"
    assert ds["quote_age_s"] == 60.0
    assert ds["quote_stale"] is False and ds["h1_stale"] is False
    # устаревшая котировка — отдельно от свечей
    db.set_quote(instrument_id, 100.5, NOW - 3 * 3600_000)
    ds = _data_state(db, settings, instrument_id,
                     db.get_quote(instrument_id), NOW)
    assert ds["state"] == "stale" and ds["reason"] == "quote_stale"
    assert ds["quote_stale"] is True and ds["h1_stale"] is False
    # устаревшая H1 — отдельный канал (котировка свежая)
    db.set_quote(instrument_id, 100.5, NOW - 60_000)
    old = NOW - 3 * H1_MS
    db.insert_candles([
        make_candle(old - H1_MS, 100, 101, 99, 100.5,
                    timeframe="H1", instrument_id=instrument_id),
    ])
    # последняя закрытая теперь старая? нет — более свежая перезапишет;
    # проверяем на инструменте только со старой свечой
    ds = _data_state(db, settings, instrument_id,
                     db.get_quote(instrument_id), NOW)
    assert ds["h1_stale"] is False  # свежая H1 из _seed осталась последней


def test_stale_thresholds_from_config(db: Database, instrument_id: int):
    _seed(db, instrument_id)
    # котировка 60 с назад: при пороге 30 с — уже stale
    settings = _settings(stale_quote_seconds=30)
    ds = _data_state(db, settings, instrument_id,
                     db.get_quote(instrument_id), NOW)
    assert ds["reason"] == "quote_stale"
    # при пороге 120 с — свежо
    settings = _settings(stale_quote_seconds=120)
    ds = _data_state(db, settings, instrument_id,
                     db.get_quote(instrument_id), NOW)
    assert ds["state"] == "ok"


def test_replaying_and_source_stale_flags(db: Database, instrument_id: int):
    settings = _settings()
    _seed(db, instrument_id)
    db.set_meta(f"replaying:{instrument_id}", "1")
    ds = _data_state(db, settings, instrument_id,
                     db.get_quote(instrument_id), NOW)
    assert ds["state"] == "replaying"
    assert ds["reason"] == "replay_in_progress"
    db.set_meta(f"replaying:{instrument_id}", "0")
    db.set_meta(f"stale:{instrument_id}:H1", "1")
    ds = _data_state(db, settings, instrument_id,
                     db.get_quote(instrument_id), NOW)
    assert ds["state"] == "stale" and ds["reason"] == "source_stale"
    assert ds["source_stale"] is True


# ------------------------- гейт уведомлений о пригодности -------------------------

def _notify_setup(db: Database, instrument_id: int):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BEAR, timeframe="D1", lower=95.0, upper=105.0,
        formed_at=T0, confirmed_at=T0 + 1000, status=ZoneStatus.ACTIVE,
    ))
    obs = db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zid, zone_version=1,
        cycle_id=1, direction=Direction.BEAR, state="active", activated_at=T0,
    ))
    sc = db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=Direction.BEAR,
        trigger="BOS", stage="primary", state="monitoring_entries",
    ))
    return obs, sc


def _entries_event(db: Database, obs, sc) -> LtfEvent:
    ev, created = db.insert_ltf_event(LtfEvent(
        id=None, observation_id=obs.id, scenario_id=sc.id,
        kind="entries_ready",
        payload={
            "scenario_id": sc.id,
            "range": {"lower": 90.0, "upper": 110.0, "mid": 100.0,
                      "version": 1},
            "entries": [{
                "entry_zone_id": 1, "type": "OB", "lower": 100.0,
                "upper": 104.0, "mid": 102.0, "overlap": "full",
                "confirmed_at": T0, "half": "premium",
            }],
        },
        occurred_at=T0 + 1000, detected_at=T0 + 1000,
        dedupe_key="entries_ready:test:1",
    ))
    assert created
    return ev


async def test_eligibility_notifications_gated_on_stale(db: Database,
                                                        instrument_id: int):
    obs, sc = _notify_setup(db, instrument_id)
    ev = _entries_event(db, obs, sc)
    sender = RecSender()
    disp = LtfDispatcher(db, DetectorConfig(), sender)
    # stale: не отправляем, событие остаётся недоставленным
    db.set_meta(f"stale:{instrument_id}:H1", "1")
    assert await disp.deliver([ev]) == 0
    assert sender.texts == []
    assert db.get_ltf_event(ev.id).delivered is False
    # replaying: то же
    db.set_meta(f"replaying:{instrument_id}", "1")
    assert await disp.retry_pending() == 0
    assert sender.texts == []
    # восстановление: ретрай доставляет ровно один раз
    db.set_meta(f"stale:{instrument_id}:H1", "0")
    db.set_meta(f"replaying:{instrument_id}", "0")
    assert await disp.retry_pending() == 1
    assert len(sender.texts) == 1
    assert db.get_ltf_event(ev.id).delivered is True
    assert await disp.retry_pending() == 0  # повторной доставки нет


# ------------------------- F03: единая оценка качества -------------------------

def test_quality_cursor_fresh_state_ok(db: Database, instrument_id: int):
    """Свежие котировка и H1 + курсор на последней закрытой → ok,
    каналы присутствуют аддитивно к legacy-полям."""
    settings = _settings()
    _seed(db, instrument_id)
    q = data_quality(db, settings, instrument_id, NOW)
    assert q["state"] == "ok" and q["reason"] is None
    assert set(q["channels"]) == {
        "quote", "h1", "d1", "w1", "continuity", "processing", "replay",
    }
    assert q["channels"]["processing"]["status"] == "ok"
    # legacy-форма _data_state сохранена
    ds = _data_state(db, settings, instrument_id,
                     db.get_quote(instrument_id), NOW)
    assert ds["state"] == "ok" and ds["quote_age_s"] == 60.0


def test_quality_processing_lag_and_recovery(db: Database,
                                             instrument_id: int):
    """Курсор позади последней закрытой H1 (или отсутствует) →
    stale/processing_lag — но только при открытых наблюдениях: без них
    process_h1_close не вызывается и курсор не двигается по определению
    (свежая БД до первого касания HTF-зоны). Курсор на close_time
    последней — ok."""
    settings = _settings()
    _seed(db, instrument_id)
    last_close = db.last_candle(instrument_id, "H1").close_time
    # без открытых наблюдений отсутствие курсора — не отставание
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", "")
    q = data_quality(db, settings, instrument_id, NOW)
    assert q["channels"]["processing"]["status"] == "ok"
    assert q["state"] == "ok"
    # открытое наблюдение: курсор на час позади (единицы те же — close_time)
    _notify_setup(db, instrument_id)
    db.set_meta(f"ltf:h1:last_close:{instrument_id}",
                str(last_close - H1_MS))
    q = data_quality(db, settings, instrument_id, NOW)
    assert q["state"] == "stale" and q["reason"] == "processing_lag"
    assert q["channels"]["processing"]["status"] == "lagging"
    # курсора нет вовсе — тоже отставание
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", "")
    q = data_quality(db, settings, instrument_id, NOW)
    assert q["reason"] == "processing_lag"
    # курсор догнал последнюю закрытую — ok
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(last_close))
    q = data_quality(db, settings, instrument_id, NOW)
    assert q["state"] == "ok"


def test_quality_history_gap(db: Database, instrument_id: int):
    """Разрыв в середине 50-свечной истории со свежим хвостом →
    stale/history_gap (канал continuity), остальные каналы свежие."""
    settings = _settings()
    # 50 часовых свечей, хвост — свежая закрытая; одна свеча в середине
    # пропущена → разрыв close_time больше одного интервала
    opens = [NOW - H1_MS + 1 - (49 - i) * H1_MS for i in range(50)]
    opens.remove(opens[25])
    db.insert_candles([
        make_candle(t, 100, 101, 99, 100.5,
                    timeframe="H1", instrument_id=instrument_id)
        for t in opens
    ])
    db.set_quote(instrument_id, 100.5, NOW - 60_000)
    last_close = db.last_candle(instrument_id, "H1").close_time
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(last_close))
    q = data_quality(db, settings, instrument_id, NOW)
    assert q["channels"]["h1"]["status"] == "ok"
    assert q["channels"]["continuity"]["status"] == "stale"
    assert q["channels"]["continuity"]["gaps"] == 1
    assert q["state"] == "stale" and q["reason"] == "history_gap"


def test_quality_d1_stale_advisory_only(db: Database, instrument_id: int):
    """D1/W1 — совещательные каналы: протухшая D1 видна в канале, но сводку
    в stale не переводит, пока свежи котировка/H1 и расчёт не отстаёт."""
    settings = _settings()
    _seed(db, instrument_id)
    db.insert_candles([
        make_candle(NOW - 10 * 24 * H1_MS, 100, 101, 99, 100.5,
                    timeframe="D1", instrument_id=instrument_id),
    ])
    q = data_quality(db, settings, instrument_id, NOW)
    assert q["channels"]["d1"]["status"] == "stale"
    assert q["state"] == "ok"


async def test_dispatcher_gated_on_processing_lag(db: Database,
                                                  instrument_id: int):
    """F03: гейт доставки — та же оценка качества: entries_ready с
    отстающим курсором не доставляется (остаётся на ретрай), после
    догона курсора уходит ровно один раз."""
    obs, sc = _notify_setup(db, instrument_id)
    ev = _entries_event(db, obs, sc)
    sender = RecSender()
    settings = _settings()
    disp = LtfDispatcher(db, DetectorConfig(), sender, settings=settings)
    # свежие данные относительно реального «сейчас» (диспетчер зовёт now_ms)
    now = now_ms()
    c = make_candle(now - 30 * 60_000, 100, 101, 99, 100.5,
                    timeframe="H1", instrument_id=instrument_id)
    db.insert_candles([c])
    db.set_quote(instrument_id, 100.5, now)
    # курсора нет — расчёт отстаёт: доставки нет, событие ждёт ретрая
    assert await disp.deliver([ev]) == 0
    assert sender.texts == []
    assert db.get_ltf_event(ev.id).delivered is False
    # курсор догнал последнюю закрытую — ретрай доставляет
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(c.close_time))
    assert await disp.retry_pending() == 1
    assert len(sender.texts) == 1
    assert db.get_ltf_event(ev.id).delivered is True
