"""Состояние актива не следует за ближайшей зоной. Приёмка ТЗ v3, случаи 1–21.

Числа 85 136,11 и 82 776,01 здесь — уровни фикстуры. Живой BOS пользователя
эти тесты не объявляют воспроизведённым.
"""
from __future__ import annotations

import json

from app.bot.cards import render_chart_caption, render_why
from app.config import Settings
from app.models import Direction, Event, EventKind, Zone, ZoneStatus, ZoneType, now_ms
from app.models_ltf import LtfObservation, LtfScenario
from app.notify.formatting import fmt_price_ru, fmt_time_msk
from app.services.asset_state import project_asset
from app.services.h1_setup import project_setup
from app.services.overview import instrument_current, instruments_overview
from tests.conftest import H1_MS, make_candle

T0 = 1_780_000_000_000
LEVEL = 82776.01
FVG_EDGE = 85136.11
MARCH_2025 = 1_740_960_000_000  # 03.03.2025 03:00 МСК


def _open(i: int) -> int:
    return T0 + i * H1_MS


def _close(i: int) -> int:
    return _open(i) + H1_MS - 1


def _zone(db, instrument_id, direction, lower, upper, *, kind=ZoneType.FVG,
          tf="D1", status=ZoneStatus.ACTIVE, formed=None, until=None, source="auto"):
    zone_id = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=kind, direction=direction,
        timeframe=tf, lower=lower, upper=upper,
        formed_at=formed if formed is not None else T0,
        confirmed_at=T0, status=status, source=source, display_until=until,
    ))
    return db.get_zone(zone_id)


def _obs(db, instrument_id, zone, direction, state, at, cycle=1):
    return db.insert_ltf_observation(LtfObservation(
        id=None, instrument_id=instrument_id, zone_id=zone.id, zone_version=1,
        cycle_id=cycle, direction=direction, state=state, activated_at=at,
    ))


def _scenario(db, obs, direction, state="monitoring_entries", created=None, cancelled_at=None):
    return db.insert_ltf_scenario(LtfScenario(
        id=None, observation_id=obs.id, direction=direction, trigger="BOS",
        stage="primary", state=state,
        created_at=created if created is not None else _close(20),
        cancelled_at=cancelled_at,
        cancellation_reason="reverse_bos" if cancelled_at else None,
    ))


def _leg(db, instrument_id, direction, level, at_i, *, key=None, anchor="pivot:12:LH",
         origin=80400.0, end=83600.0, superseded=None, epoch=1, close_price=None,
         break_open=None):
    bos = _close(at_i)
    opened = break_open if break_open is not None else _open(at_i)
    token = key or f"bos:primary:{'LH' if direction == 'bull' else 'HL'}:12:{level}@{bos}"
    evidence = {
        "break_open": opened,
        "close_price": level if close_price is None else close_price,
        "data_quality": "ok",
    }
    cur = db.conn.execute(
        """INSERT INTO h1_local_leg
           (instrument_id, direction, origin_anchor_key, origin_price, origin_at,
            origin_known_at, trigger_bos_key, bos_at, broken_anchor_key, state,
            endpoint_price, endpoint_at, endpoint_status, as_of, revision, reason,
            evidence, superseded_at, structure_epoch_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            instrument_id, direction, f"pivot:1:{'LL' if direction == 'bull' else 'HH'}",
            origin, _open(at_i - 4), bos, token, bos, anchor,
            "superseded" if superseded else "developing",
            end, bos, "provisional", bos, 1, None,
            json.dumps(evidence), superseded, epoch,
        ),
    )
    leg_id = int(cur.lastrowid)
    eq = (origin + end) / 2 if direction == "bull" else (end + origin) / 2
    lo, hi = (origin, end) if direction == "bull" else (end, origin)
    db.conn.execute(
        """INSERT INTO h1_local_leg_revision
           (leg_id, revision, lower, upper, eq, endpoint_price, endpoint_at,
            range_status, as_of, reason)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (leg_id, 1, lo, hi, eq, end, bos, "provisional", bos, None),
    )
    db._commit()
    return leg_id


def _entry(db, instrument_id, direction, lower, upper, formed):
    db.conn.execute(
        """INSERT INTO ltf_entry_zone
           (instrument_id, type, direction, lower, upper, formed_at, confirmed_at, validity)
           VALUES (?,?,?,?,?,?,?,'fresh')""",
        (instrument_id, "FVG", direction, lower, upper, formed, formed),
    )
    db._commit()


def _episode(db, instrument_id, zone_id, direction, state, started, interaction="touch",
             confirmed_at=None, scenario_id=None):
    cur = db.conn.execute(
        """INSERT INTO htf_context_episode
           (instrument_id, started_at, last_distinct_interaction_at, candidate_direction,
            basis, state, expires_at, confirmed_scenario_id, confirmed_at, rule_version, evidence)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            instrument_id, started, started, direction, f"fvg:D1:{direction}", state,
            started + 30 * 86_400_000, scenario_id, confirmed_at, "htf-context-1", "{}",
        ),
    )
    episode_id = int(cur.lastrowid)
    db.conn.execute(
        """INSERT INTO htf_context_source
           (episode_id, zone_id, zone_version, interaction, interaction_at, evidence)
           VALUES (?,?,?,?,?,?)""",
        (episode_id, zone_id, 1, interaction, started, "{}"),
    )
    db._commit()
    return episode_id


def _view(db, instrument_id, *, price=83000.0, nav=None, data=None, at=None):
    return project_asset(
        db, instrument_id, at or _close(40),
        navigation=nav, price=price,
        data_state=data if data is not None else {"state": "ok", "reason": None},
    )


def _events(db):
    return db.conn.execute("SELECT COUNT(*) AS n FROM ltf_event").fetchone()["n"]


def _scenarios(db):
    return db.conn.execute("SELECT COUNT(*) AS n FROM ltf_scenario").fetchone()["n"]


def test_01_new_bull_bos_replaces_old_bear(db, instrument_id):
    bear_zone = _zone(db, instrument_id, Direction.BEAR, 83521.15, FVG_EDGE)
    _obs(db, instrument_id, bear_zone, Direction.BEAR, "waiting_structure", _open(1))
    bull_zone = _zone(db, instrument_id, Direction.BULL, 60000, 66000, kind=ZoneType.OB, tf="W1")
    obs = _obs(db, instrument_id, bull_zone, Direction.BULL, "active", _open(2), cycle=2)
    _scenario(db, obs, Direction.BULL, created=_close(21))
    _leg(db, instrument_id, "bear", FVG_EDGE, 10, anchor="pivot:9:HL",
         origin=86698.0, end=80393.0, superseded=_close(20), epoch=1)
    _leg(db, instrument_id, "bull", LEVEL, 20, origin=81603.52, end=83528.98, epoch=2)
    _entry(db, instrument_id, "bull", 81650, 82000, _open(16))
    asset = _view(db, instrument_id, nav={"selected_context_id": 1, "selection_basis": "nearest"})
    assert asset["structure"]["direction"] == "bull"
    assert asset["structure"]["proven"] is True
    assert asset["asset_state"]["title"] == "LONG · ждём откат"
    assert asset["structure"]["last_transition"]["break_level"] == LEVEL
    text = " ".join(asset["compact"])
    assert "85 136,11" not in text
    assert any("85 136,11" in line for line in asset["details"]["history"])


def test_02_nearest_bear_does_not_flip_long(db, instrument_id):
    near = _zone(db, instrument_id, Direction.BEAR, 83000, 84000)
    far = _zone(db, instrument_id, Direction.BULL, 60000, 66000, kind=ZoneType.OB, tf="D1")
    _obs(db, instrument_id, near, Direction.BEAR, "waiting_structure", _open(1))
    obs = _obs(db, instrument_id, far, Direction.BULL, "active", _open(2), cycle=2)
    _scenario(db, obs, Direction.BULL, created=_close(21))
    _leg(db, instrument_id, "bull", LEVEL, 20)
    _entry(db, instrument_id, "bull", 81000, 81500, _open(16))
    asset = _view(db, instrument_id, nav={"selected_context_id": 1, "selection_basis": "nearest"})
    assert asset["asset_state"]["title"] == "LONG · ждём откат"
    assert any("не задаёт состояние" in line for line in asset["details"]["other_contexts"])


def test_03_manual_selection_is_navigation_only(db, instrument_id):
    bull = _zone(db, instrument_id, Direction.BULL, 60000, 66000, kind=ZoneType.OB)
    bear = _zone(db, instrument_id, Direction.BEAR, 90000, 91000)
    obs = _obs(db, instrument_id, bull, Direction.BULL, "active", _open(1))
    bear_obs = _obs(db, instrument_id, bear, Direction.BEAR, "waiting_structure", _open(2), cycle=2)
    _scenario(db, obs, Direction.BULL, created=_close(21))
    _leg(db, instrument_id, "bull", LEVEL, 20)
    _entry(db, instrument_id, "bull", 81000, 81500, _open(16))
    first = _view(db, instrument_id, nav={"selected_context_id": obs.id, "selection_basis": "last_scenario"})
    second = _view(db, instrument_id, nav={"selected_context_id": bear_obs.id, "selection_basis": "manual"})
    assert first["snapshot_id"] == second["snapshot_id"]
    assert first["asset_state"] == second["asset_state"]
    assert first["structure"] == second["structure"]
    assert first["pd"] == second["pd"]
    assert first["navigation"]["selected_context_id"] != second["navigation"]["selected_context_id"]


def test_04_price_wobble_does_not_flip_or_notify(db, instrument_id):
    _zone(db, instrument_id, Direction.BEAR, 82900, 83100)
    bull = _zone(db, instrument_id, Direction.BULL, 60000, 66000)
    obs = _obs(db, instrument_id, bull, Direction.BULL, "active", _open(1), cycle=2)
    _scenario(db, obs, Direction.BULL, created=_close(21))
    _leg(db, instrument_id, "bull", LEVEL, 20)
    _entry(db, instrument_id, "bull", 81000, 81500, _open(16))
    before = _events(db)
    low = _view(db, instrument_id, price=82800)
    high = _view(db, instrument_id, price=86000)
    assert low["snapshot_id"] == high["snapshot_id"]
    assert low["asset_state"]["direction"] == "long"
    assert high["asset_state"]["direction"] == "long"
    assert _events(db) == before


def test_05_fvg_edge_is_not_a_bos(db, instrument_id):
    _zone(db, instrument_id, Direction.BEAR, 83521.15, FVG_EDGE)
    _leg(db, instrument_id, "bull", LEVEL, 20)
    asset = _view(db, instrument_id, price=84000)
    text = asset["asset_state"]["title"] + " " + " ".join(asset["compact"])
    assert "85 136,11" not in text
    assert asset["structure"]["last_transition"]["break_level"] == LEVEL
    assert "BOS" in text


def test_06_same_price_is_bos_when_pivot_is_proven(db, instrument_id):
    _zone(db, instrument_id, Direction.BEAR, 83521.15, FVG_EDGE)
    _leg(db, instrument_id, "bear", FVG_EDGE, 12, anchor="pivot:7350:HL",
         origin=86698.0, end=80393.0)
    asset = _view(db, instrument_id)
    transition = asset["structure"]["last_transition"]
    assert transition["break_level"] == FVG_EDGE
    assert transition["broken_anchor_key"] == "pivot:7350:HL"
    assert fmt_time_msk(_close(12)) in " ".join(asset["compact"])
    assert "—" not in " ".join(asset["compact"])


def test_07_unproven_bos_is_not_confirmed_with_blank_time(db, instrument_id):
    _leg(
        db, instrument_id, "bear", FVG_EDGE, 12,
        key=f"bos:primary:bull:5@{_close(12)}",
        anchor="pivot:5:HL",
    )
    asset = _view(db, instrument_id)
    text = (asset["asset_state"]["title"] or "") + " " + " ".join(asset["compact"])
    assert asset["asset_state"]["title"] == "Данные H1 неполные"
    assert asset["structure"]["proven"] is False
    assert "подтверждён" not in text
    assert "—" not in text
    assert asset["structure"]["last_transition"] is None


def test_08_bull_bos_without_long_context_is_not_replaced_by_short(db, instrument_id):
    bear = _zone(db, instrument_id, Direction.BEAR, 83000, FVG_EDGE)
    _obs(db, instrument_id, bear, Direction.BEAR, "waiting_structure", _open(1))
    _leg(db, instrument_id, "bull", LEVEL, 20)
    asset = _view(db, instrument_id, nav={"selected_context_id": 1, "selection_basis": "nearest"})
    assert asset["asset_state"]["title"] == "H1 вверх · торговый контекст не подтверждён"
    assert "SHORT" not in asset["asset_state"]["title"]


def test_09_long_context_without_bos_waits(db, instrument_id):
    zone = _zone(db, instrument_id, Direction.BULL, 60000, 66000, kind=ZoneType.OB, tf="D1")
    _obs(db, instrument_id, zone, Direction.BULL, "waiting_structure", _open(1))
    asset = _view(db, instrument_id)
    assert asset["asset_state"]["title"] == "Контекст LONG · ждём H1"
    assert "подтверждён" not in asset["asset_state"]["title"]
    assert "подтверждён" not in " ".join(asset["compact"])


def test_10_missing_scenario_row_is_inconsistent_and_not_created(db, instrument_id):
    zone = _zone(db, instrument_id, Direction.BULL, 60000, 66000)
    _episode(
        db, instrument_id, zone.id, "bull", "confirmed", _open(5),
        confirmed_at=_close(20),
    )
    _leg(db, instrument_id, "bull", LEVEL, 20)
    before = _scenarios(db)
    asset = _view(db, instrument_id)
    assert asset["asset_state"]["title"] == "Состояние уточняется"
    assert asset["asset_state"]["reason_code"] == "state_inconsistent"
    assert "бессроч" not in " ".join(asset["compact"])
    assert any("не создаёт" in line for line in asset["compact"])
    assert _scenarios(db) == before


def test_11_worked_fvg_still_supports_context(db, instrument_id):
    zone = _zone(
        db, instrument_id, Direction.BULL, 60000, 66000,
        status=ZoneStatus.ARCHIVED, tf="W1",
    )
    obs = _obs(db, instrument_id, zone, Direction.BULL, "active", _open(2))
    _scenario(db, obs, Direction.BULL, created=_close(21))
    _episode(db, instrument_id, zone.id, "bull", "awaiting_h1", _open(3), interaction="full_fill")
    _leg(db, instrument_id, "bull", LEVEL, 20)
    asset = _view(db, instrument_id)
    grounds = " ".join(asset["details"]["grounds"])
    assert "зона отработана, факт поддерживает контекст" in grounds
    assert "Контекст завершён" not in asset["asset_state"]["title"]
    assert "Контекст завершён" not in " ".join(asset["compact"])
    assert asset["structure"]["direction"] == "bull"


def test_12_opposite_sources_are_not_a_structure_conflict(db, instrument_id):
    bull = _zone(db, instrument_id, Direction.BULL, 60000, 66000)
    bear = _zone(db, instrument_id, Direction.BEAR, 90000, 91000)
    obs = _obs(db, instrument_id, bull, Direction.BULL, "active", _open(1))
    _obs(db, instrument_id, bear, Direction.BEAR, "waiting_structure", _open(2), cycle=2)
    _scenario(db, obs, Direction.BULL, created=_close(21))
    _leg(db, instrument_id, "bull", LEVEL, 20)
    asset = _view(db, instrument_id)
    assert asset["structure"]["direction"] == "bull"
    assert "конфликт" not in (asset["asset_state"]["title"] or "").lower()
    assert asset["asset_state"]["title"].startswith("LONG") or asset["asset_state"]["title"].startswith("H1 вверх")


def test_13_later_import_of_older_event_stays_historical(db, instrument_id):
    _leg(db, instrument_id, "bull", LEVEL, 30, epoch=2)
    _leg(db, instrument_id, "bear", FVG_EDGE, 10, anchor="pivot:9:HL",
         origin=86698.0, end=80393.0, epoch=1)
    asset = _view(db, instrument_id, at=_close(40))
    assert asset["structure"]["direction"] == "bull"
    assert asset["structure"]["last_transition"]["break_level"] == LEVEL


def test_14_old_completion_stays_in_history(db, instrument_id):
    _zone(
        db, instrument_id, Direction.BULL, 81500, 89376.9,
        kind=ZoneType.FVG, tf="W1", status=ZoneStatus.ARCHIVED, until=MARCH_2025,
    )
    _leg(db, instrument_id, "bull", LEVEL, 20)
    asset = _view(db, instrument_id)
    assert "03.03.2025" not in " ".join(asset["compact"])
    assert "03.03.2025" not in (asset["asset_state"]["title"] or "")
    assert any("03.03.2025" in line for line in asset["details"]["history"])


def test_15_crosses_are_not_called_sweeps(db, instrument_id):
    for i, level in enumerate((100.0, 110.0, 120.0)):
        zone = _zone(db, instrument_id, Direction.BULL, level, level, kind=ZoneType.BSL, tf="D1")
        db.insert_event(Event(
            id=None, zone_id=zone.id, cycle_id=i + 1, kind=EventKind.LEVEL_TAKEN,
            occurred_at=_close(5 + i), detected_at=_close(5 + i) + 10, price=level,
        ))
    _leg(db, instrument_id, "bull", LEVEL, 20)
    asset = _view(db, instrument_id)
    table = asset["details"]["liquidity"]
    assert len(table) == 3
    joined = " ".join(table)
    assert "пересечение" in joined
    assert "sweep" not in joined.lower()
    assert "reclaim" not in joined.lower()
    assert "снятие с возвратом" not in joined
    assert "sweep" not in " ".join(asset["compact"]).lower()


def test_16_one_bar_reversal_is_one_long_snapshot(db, instrument_id):
    bear = _zone(db, instrument_id, Direction.BEAR, 90000, 91000)
    bull = _zone(db, instrument_id, Direction.BULL, 60000, 66000)
    bear_obs = _obs(db, instrument_id, bear, Direction.BEAR, "active", _open(1))
    bull_obs = _obs(db, instrument_id, bull, Direction.BULL, "active", _open(2), cycle=2)
    _scenario(db, bear_obs, Direction.BEAR, state="cancelled", created=_close(10), cancelled_at=_close(20))
    _scenario(db, bull_obs, Direction.BULL, created=_close(20))
    _leg(db, instrument_id, "bear", FVG_EDGE, 10, anchor="pivot:9:HL",
         origin=86698.0, end=80393.0, superseded=_close(20), epoch=1)
    _leg(db, instrument_id, "bull", LEVEL, 20, epoch=2)
    before = _events(db)
    asset = _view(db, instrument_id)
    again = _view(db, instrument_id, nav={"selected_context_id": bear_obs.id, "selection_basis": "manual"})
    assert asset["asset_state"]["direction"] == "long"
    assert asset["snapshot_id"] == again["snapshot_id"]
    assert _events(db) == before


def test_17_stale_h1_blocks_entry_and_names_the_last_trustworthy_time(db, instrument_id):
    zone = _zone(db, instrument_id, Direction.BULL, 60000, 66000)
    obs = _obs(db, instrument_id, zone, Direction.BULL, "active", _open(1))
    _scenario(db, obs, Direction.BULL, created=_close(21))
    _leg(db, instrument_id, "bull", LEVEL, 20)
    _entry(db, instrument_id, "bull", 81000, 82000, _open(16))
    asset = _view(
        db, instrument_id, price=81500,
        data={"state": "stale", "reason": "h1_stale"},
    )
    assert asset["asset_state"]["title"] == "Данные H1 неполные"
    assert asset["next_action"]["kind"] == "blocked"
    text = " ".join(asset["compact"])
    assert "устарели" in text
    assert fmt_time_msk(_close(20)) in text
    assert "в зоне входа" not in text


def test_18_new_bear_bos_replaces_old_bull(db, instrument_id):
    zone = _zone(db, instrument_id, Direction.BEAR, 90000, 91000, kind=ZoneType.OB, tf="D1")
    obs = _obs(db, instrument_id, zone, Direction.BEAR, "active", _open(1))
    _scenario(db, obs, Direction.BEAR, created=_close(31))
    _leg(db, instrument_id, "bull", LEVEL, 10, superseded=_close(30), epoch=1)
    _leg(db, instrument_id, "bear", 80000.0, 30, anchor="pivot:40:HL",
         origin=83600.0, end=79000.0, epoch=2)
    _entry(db, instrument_id, "bear", 82000, 83000, _open(26))
    asset = _view(db, instrument_id, price=80000)
    assert asset["structure"]["direction"] == "bear"
    assert asset["asset_state"]["title"].startswith("SHORT")
    assert asset["structure"]["last_transition"]["break_level"] == 80000.0


def test_19_list_card_chart_and_bot_share_the_projection(db, instrument_id):
    zone = _zone(db, instrument_id, Direction.BULL, 60000, 66000, kind=ZoneType.OB, tf="W1")
    obs = _obs(db, instrument_id, zone, Direction.BULL, "active", _open(1))
    _scenario(db, obs, Direction.BULL, created=_close(21))
    _leg(db, instrument_id, "bull", LEVEL, 20, origin=81603.52, end=83528.98)
    _entry(db, instrument_id, "bull", 81650, 82000, _open(16))
    now = now_ms()
    candle = make_candle(now - 30 * 60_000, 83000, 83100, 82900, 83000, timeframe="H1", instrument_id=instrument_id)
    db.insert_candles([candle])
    db.set_quote(instrument_id, 83000, now)
    db.set_meta(f"ltf:h1:last_close:{instrument_id}", str(candle.close_time))
    settings = Settings()
    row = next(item for item in instruments_overview(db, settings) if item["instrument"]["id"] == instrument_id)
    current = instrument_current(db, settings, instrument_id)
    why = render_why(db, settings, instrument_id)
    caption = render_chart_caption(current, now, "H1", 3)
    setup = project_setup(db, settings.detector, instrument_id, now, mode="current")
    title = row["asset"]["asset_state"]["title"]
    assert title == current["asset"]["asset_state"]["title"]
    assert title in why
    assert title in caption
    assert row["asset"]["structure"]["direction"] == current["asset"]["structure"]["direction"] == "bull"
    assert setup["direction"] == "bull"
    assert setup["movement_id"] == current["asset"]["structure"]["movement_id"]
    assert row["asset"]["pd"]["lower"] == current["asset"]["pd"]["lower"]
    assert "82 776,01" in caption


def test_20_compact_window_has_one_status_and_five_lines(db, instrument_id):
    zone = _zone(db, instrument_id, Direction.BULL, 60000, 66000, kind=ZoneType.MANUAL, tf="D1", source="manual")
    obs = _obs(db, instrument_id, zone, Direction.BULL, "active", _open(1))
    _scenario(db, obs, Direction.BULL, created=_close(21))
    _leg(db, instrument_id, "bull", LEVEL, 20)
    _entry(db, instrument_id, "bull", 81000, 81500, _open(16))
    asset = _view(db, instrument_id, nav={"selection_basis": "nearest"})
    text = " ".join(asset["compact"])
    assert asset["asset_state"]["title"]
    assert len(asset["compact"]) <= 5
    assert text.count("BOS ") <= 1
    assert "waiting_structure" not in text
    assert "nearest" not in text
    assert " active" not in text
    assert "—" not in text
    assert "Контекст контекст" not in text
    assert "Условие BOS" not in text


def test_21_repeat_navigation_does_not_change_the_market_result(db, instrument_id):
    bull = _zone(db, instrument_id, Direction.BULL, 60000, 66000)
    bear = _zone(db, instrument_id, Direction.BEAR, 90000, 91000)
    obs = _obs(db, instrument_id, bull, Direction.BULL, "active", _open(1))
    other = _obs(db, instrument_id, bear, Direction.BEAR, "waiting_structure", _open(2), cycle=2)
    _scenario(db, obs, Direction.BULL, created=_close(21))
    _leg(db, instrument_id, "bull", LEVEL, 20)
    before = _events(db)
    snaps = [
        _view(db, instrument_id, nav={"selected_context_id": obs.id, "selection_basis": "manual"}),
        _view(db, instrument_id, nav={"selected_context_id": other.id, "selection_basis": "nearest"}),
        _view(db, instrument_id, nav={"selected_context_id": obs.id, "selection_basis": "manual"}),
    ]
    assert snaps[0]["snapshot_id"] == snaps[1]["snapshot_id"] == snaps[2]["snapshot_id"]
    assert snaps[0]["asset_state"] == snaps[2]["asset_state"]
    assert _events(db) == before


def test_sms_is_not_renamed_to_bos(db, instrument_id):
    bos = _close(20)
    _leg(
        db, instrument_id, "bull", LEVEL, 20,
        key=f"sms:primary:LH:12:{LEVEL}@{bos}",
        anchor="pivot:12:LH",
    )
    asset = _view(db, instrument_id)
    assert asset["structure"]["last_transition"]["kind"] == "SMS"
    assert asset["compact"][0].startswith("SMS ")
    assert "BOS" not in asset["compact"][0]


def test_four_part_key_does_not_invent_a_price(db, instrument_id):
    _leg(
        db, instrument_id, "bull", 5, 20,
        key=f"bos:primary:bull:5@{_close(20)}",
        anchor="pivot:5:LH",
    )
    asset = _view(db, instrument_id)
    assert asset["structure"]["proven"] is False
    assert fmt_price_ru(FVG_EDGE) == "85 136,11"
