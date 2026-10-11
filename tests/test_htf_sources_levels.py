"""Этап 2: HTF-источники OB/FVG/BSL/SSL и контакты (правила v1, 10.10.2026).

Покрытие критериев: R09 (BSL→bear, SSL→bull, подтверждение ещё требуется),
R20 (тень/равенство ≠ закрытие за зоной), R21 (H1 не подтверждает проход W1),
R42 (пересечение между наблюдениями → ожидание, без выдуманного sweep),
R48-локально (несколько источников на интервале — неопределённость),
§7 (FVG_FILLED не архивирует зону), §2 (manual watch ≠ рыночный контакт),
R30 (источник, известный позже, не причиняет исторический BOS задним числом).
"""
from __future__ import annotations

import json

from app.config import DetectorConfig, Settings
from app.db import Database
from app.engine.ltf.engine import LtfEngine
from app.engine.scanner import Scanner
from app.models import (
    Candle,
    Direction,
    EventKind,
    Zone,
    ZoneStatus,
    ZoneType,
    close_boundary_ms,
)
from app.services.htf_context import (
    episode_sources,
    open_episode,
    plan_context_bar,
    record_interaction,
)
from app.services.htf_parent import (
    eligible_htf_parent,
    expected_direction_for_source,
    parent_decision,
)
from app.services.htf_profile import migrate_saved_htf_profile_v3
from app.worker import _LTF_CONTEXT_ZONE_TYPES, LTF_START_KINDS

H = 3_600_000
D = 24 * H
W = 7 * D
T = 1_780_012_800_000


def _zone(db, iid, ztype=ZoneType.OB, direction=Direction.BEAR, lower=90.0,
          upper=110.0, timeframe="D1", status=ZoneStatus.ACTIVE,
          confirmed_at=T - D, source="auto", evidence=None, created_at=0):
    zid = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ztype, direction=direction,
        timeframe=timeframe, lower=lower, upper=upper, formed_at=T - 2 * D,
        confirmed_at=confirmed_at, status=status, source=source,
        evidence=evidence or {}, created_at=created_at,
    ))
    return db.get_zone(zid)


def _d1_candle(iid, i, o, h, l, c, timeframe="D1"):
    tf_ms = {"H1": H, "D1": D, "W1": W}[timeframe]
    return Candle(
        instrument_id=iid, timeframe=timeframe, open_time=T + i * tf_ms,
        close_time=T + (i + 1) * tf_ms - 1, open=o, high=h, low=l, close=c,
        closed=True, source="test",
    )


def _kinds(db, zone_id):
    return {e.kind for e in db.get_events(zone_id)}


# --------------------------------------------------------------------- #
# R09: BSL → ожидание bear, SSL → bull; подтверждение ещё требуется
# --------------------------------------------------------------------- #

def test_r09_expected_direction_for_sources(db, instrument_id):
    bsl = _zone(db, instrument_id, ZoneType.BSL, Direction.BEAR, 15.0, 15.0)
    ssl = _zone(db, instrument_id, ZoneType.SSL, Direction.BULL, 14.0, 14.0)
    ob = _zone(db, instrument_id, ZoneType.OB, Direction.BEAR)
    assert expected_direction_for_source(bsl) == "bear"
    assert expected_direction_for_source(ssl) == "bull"
    assert expected_direction_for_source(ob) == "bear"
    manual = _zone(db, instrument_id, ZoneType.MANUAL, Direction.BULL,
                   source="manual", evidence={"direction_unset": True})
    from app.services.htf_context import candidate_direction
    assert candidate_direction(manual) == "neutral"


def test_r09_bsl_contact_starts_bear_wait(db, cfg, instrument_id):
    bsl = _zone(db, instrument_id, ZoneType.BSL, Direction.BEAR, 15.0, 15.0)
    LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, bsl, T)
    ep = open_episode(db, instrument_id)
    assert ep is not None and ep["candidate_direction"] == "bear"
    sources = episode_sources(db, ep["id"])
    assert [s["interaction"] for s in sources] == ["level_cross"]
    # контакт сам по себе сценарий не открывает — нужен BOS/SMS H1
    assert plan_context_bar(
        db, cfg, instrument_id, _d1_candle(instrument_id, 0, 16, 16, 15, 16),
        [], [_d1_candle(instrument_id, 0, 16, 16, 15, 16)], T,
    ) is None


def test_r09_ssl_contact_starts_bull_wait(db, cfg, instrument_id):
    ssl = _zone(db, instrument_id, ZoneType.SSL, Direction.BULL, 14.0, 14.0)
    LtfEngine(db, cfg).on_htf_zone_touched(instrument_id, ssl, T)
    ep = open_episode(db, instrument_id)
    assert ep is not None and ep["candidate_direction"] == "bull"


def test_r09_taken_level_eligible_strictly_before_contact(db, cfg, instrument_id):
    # LEVEL_TAKEN завершает уровень в момент контакта (display_until=ts):
    # допуск родителя оценивается строго до контакта
    bsl = _zone(db, instrument_id, ZoneType.BSL, Direction.BEAR, 15.0, 15.0)
    db.update_zone(bsl.id, status=ZoneStatus.TAKEN, display_until=T,
                   end_reason="swept (§7)")
    taken = db.get_zone(bsl.id)
    assert parent_decision(taken, cfg, T - 1).eligible is True
    assert parent_decision(taken, cfg, T).eligible is False


def test_r09_worker_constants_cover_levels_and_crossing():
    assert EventKind.LEVEL_TAKEN in LTF_START_KINDS
    assert EventKind.JUMP_THROUGH in LTF_START_KINDS
    assert _LTF_CONTEXT_ZONE_TYPES["BSL"] is ZoneType.BSL
    assert _LTF_CONTEXT_ZONE_TYPES["SSL"] is ZoneType.SSL
    assert DetectorConfig().htf_context_type_set() == {"OB", "FVG", "BSL", "SSL"}


# --------------------------------------------------------------------- #
# §7 + §5: FVG_FILLED не архивирует; проход — только закрытием своего ТФ
# --------------------------------------------------------------------- #

def test_fvg_fill_does_not_archive_zone(db, cfg, instrument_id):
    fvg = _zone(db, instrument_id, ZoneType.FVG, Direction.BULL, 100.0, 101.0)
    scanner = Scanner(db, cfg)
    assert [e.kind for e in scanner.on_price(instrument_id, 100.9, T)] == \
        [EventKind.TOUCH]
    ev = scanner.on_price(instrument_id, 99.0, T + 1_000)
    assert [e.kind for e in ev] == [EventKind.FVG_FILLED]
    fresh = db.get_zone(fvg.id)
    # заполнение — факт визита: зона остаётся актуальной, рисунок не закрыт
    assert fresh.status in (ZoneStatus.ACTIVE, ZoneStatus.WEAKENED)
    assert fresh.market_validity == "active"
    assert fresh.display_until is None
    # статистика визита сохранена
    visits = db.get_visits(fvg.id, fvg.cycle_id)
    assert visits and max(v.max_depth for v in visits) >= 1.0


def test_r20_fvg_pass_only_by_strict_close_beyond(db, cfg, instrument_id):
    fvg = _zone(db, instrument_id, ZoneType.FVG, Direction.BULL, 100.0, 110.0)
    scanner = Scanner(db, cfg)
    # равенство дальней границе — не проход
    scanner.on_closed_candle(_d1_candle(instrument_id, 0, 111, 112, 99, 100.0))
    # тень за границей при закрытии внутри — не проход
    scanner.on_closed_candle(_d1_candle(instrument_id, 1, 105, 106, 95, 105))
    assert [e for e in db.get_events(fvg.id) if e.kind == EventKind.SOURCE_PASSED] == []
    assert db.get_zone(fvg.id).market_validity == "active"
    # закрытие строго ниже нижней границы — проход подтверждён
    scanner.on_closed_candle(_d1_candle(instrument_id, 2, 105, 106, 98, 99.0))
    passed = [e for e in db.get_events(fvg.id) if e.kind == EventKind.SOURCE_PASSED]
    assert len(passed) == 1
    fresh = db.get_zone(fvg.id)
    assert fresh.market_validity == "invalid"
    assert fresh.evidence.get("invalidated_at") is not None


def test_r20_level_pass_only_by_own_tf_close(db, cfg, instrument_id):
    bsl = _zone(db, instrument_id, ZoneType.BSL, Direction.BEAR, 15.0, 15.0)
    scanner = Scanner(db, cfg)
    # тень выше уровня, закрытие ниже — не проход (и не sweep)
    scanner.on_closed_candle(_d1_candle(instrument_id, 0, 14, 16, 13, 14.5))
    assert [e for e in db.get_events(bsl.id) if e.kind == EventKind.SOURCE_PASSED] == []
    # закрытие строго выше уровня — проход против ожидаемой реакции
    scanner.on_closed_candle(_d1_candle(instrument_id, 1, 14.5, 16, 14, 15.5))
    passed = [e for e in db.get_events(bsl.id) if e.kind == EventKind.SOURCE_PASSED]
    assert len(passed) == 1
    assert db.get_zone(bsl.id).evidence.get("pass_confirmed_at") is not None


def test_r21_h1_close_does_not_confirm_w1_pass(db, cfg, instrument_id):
    fvg = _zone(db, instrument_id, ZoneType.FVG, Direction.BULL, 100.0, 110.0,
                timeframe="W1")
    scanner = Scanner(db, cfg)
    # H1 закрылась ниже W1-зоны, но W1 ещё открыта — проход не подтверждён
    scanner.on_closed_candle(_d1_candle(instrument_id, 0, 105, 106, 95, 99.0,
                                        timeframe="H1"))
    assert [e for e in db.get_events(fvg.id) if e.kind == EventKind.SOURCE_PASSED] == []
    assert db.get_zone(fvg.id).market_validity == "active"
    # собственная W1 закрылась за границей — проход подтверждён
    scanner.on_closed_candle(_d1_candle(instrument_id, 0, 105, 106, 95, 99.0,
                                        timeframe="W1"))
    assert len([e for e in db.get_events(fvg.id) if e.kind == EventKind.SOURCE_PASSED]) == 1


# --------------------------------------------------------------------- #
# R42: пересечение всей зоны между наблюдениями
# --------------------------------------------------------------------- #

def test_r42_crossing_between_observations_recorded(db, cfg, instrument_id):
    zone = _zone(db, instrument_id, ZoneType.OB, Direction.BULL, 99.0, 101.0)
    scanner = Scanner(db, cfg)
    assert scanner.on_price(instrument_id, 105.0, T) == []
    ev = scanner.on_price(instrument_id, 95.0, T + 1_000)
    assert [e.kind for e in ev] == [EventKind.JUMP_THROUGH]
    evidence = ev[0].evidence
    assert evidence["contact_basis"] == "crossed_between_observations"
    assert evidence["observation_interval"] == [T, T + 1_000]
    assert evidence["exact_contact_unknown"] is True
    # ожидание LTF допустимо — но sweep/проход не выдуманы
    eng = LtfEngine(db, cfg)
    eng.on_htf_zone_touched(instrument_id, zone, T + 1_000,
                            evidence=dict(evidence))
    ep = open_episode(db, instrument_id)
    assert ep is not None
    source = episode_sources(db, ep["id"])[0]
    assert json.loads(source["evidence"])["contact_basis"] == \
        "crossed_between_observations"
    fresh = db.get_zone(zone.id)
    assert fresh.market_validity == "active"
    assert [e for e in db.get_events(zone.id) if e.kind == EventKind.SOURCE_PASSED] == []
    # обычный TOUCH не порождается
    assert [e for e in db.get_events(zone.id) if e.kind == EventKind.TOUCH] == []


def test_r42_reverse_crossing_also_detected(db, cfg, instrument_id):
    # медвежья зона, пересечённая вниз (против направления §6 jump): для
    # ожидания LTF это тоже контакт-кандидат
    zone = _zone(db, instrument_id, ZoneType.OB, Direction.BEAR, 99.0, 101.0)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 105.0, T)
    ev = scanner.on_price(instrument_id, 95.0, T + 1_000)
    assert [e.kind for e in ev] == [EventKind.JUMP_THROUGH]
    assert ev[0].evidence["contact_basis"] == "crossed_between_observations"


def test_r48_multiple_sources_one_interval_keep_uncertainty(db, cfg, instrument_id):
    bull = _zone(db, instrument_id, ZoneType.OB, Direction.BULL, 99.0, 101.0)
    bear = _zone(db, instrument_id, ZoneType.OB, Direction.BEAR, 99.0, 101.0,
                 confirmed_at=T - D + 1)
    scanner = Scanner(db, cfg)
    scanner.on_price(instrument_id, 105.0, T)
    ev = scanner.on_price(instrument_id, 95.0, T + 1_000)
    assert {e.zone_id for e in ev} == {bull.id, bear.id}
    eng = LtfEngine(db, cfg)
    for e in ev:
        z = db.get_zone(e.zone_id)
        eng.on_htf_zone_touched(instrument_id, z, e.occurred_at,
                                evidence=dict(e.evidence))
    ep = open_episode(db, instrument_id)
    sources = episode_sources(db, ep["id"])
    # оба источника-кандидата сохранены; направление не выбирается порядком
    # записей — конфликт фиксируется как neutral (определит BOS/SMS)
    assert len(sources) == 2
    assert {json.loads(s["evidence"])["contact_basis"] for s in sources} == \
        {"crossed_between_observations"}
    assert ep["candidate_direction"] == "neutral"


# --------------------------------------------------------------------- #
# §2/R30: служебная пометка ≠ рыночный контакт; поздно известный источник
# --------------------------------------------------------------------- #

def test_manual_watch_marks_source_basis(db, cfg, instrument_id):
    zone = _zone(db, instrument_id, ZoneType.OB, Direction.BEAR)
    LtfEngine(db, cfg).on_htf_zone_touched(
        instrument_id, zone, T, evidence={"contact_basis": "manual_watch"})
    ep = open_episode(db, instrument_id)
    source = episode_sources(db, ep["id"])[0]
    assert json.loads(source["evidence"])["contact_basis"] == "manual_watch"


def test_r30_manual_source_known_later_does_not_backdate(db, cfg, instrument_id):
    zone = _zone(db, instrument_id, ZoneType.MANUAL, Direction.BEAR,
                 source="manual", created_at=T + 10 * H)
    record_interaction(db, cfg, instrument_id, zone, T + H)
    ep = open_episode(db, instrument_id)
    # эпизод начинается не раньше доступности источника
    assert ep["started_at"] == T + 10 * H
    source = episode_sources(db, ep["id"])[0]
    assert source["interaction_at"] == T + 10 * H
    evidence = json.loads(source["evidence"])
    assert evidence["declared_interaction_at"] == T + H


# --------------------------------------------------------------------- #
# Профиль: миграция v3 OB / OB,FVG → OB,FVG,BSL,SSL
# --------------------------------------------------------------------- #

def _settings(tmp_path, profile):
    settings = Settings()
    settings.db_path = str(tmp_path / "x.db")
    (tmp_path / "settings.json").write_text(
        json.dumps({"detector": {"htf_context_types": profile}}),
        encoding="utf-8",
    )
    return settings


def test_profile_v3_migrates_legacy_defaults(tmp_path):
    db = Database(":memory:")
    try:
        settings = _settings(tmp_path, "OB,FVG")
        report = migrate_saved_htf_profile_v3(db, settings)
        assert report["action"] == "migrated"
        assert report["added"] == ["BSL", "SSL"]
        assert settings.detector.htf_context_types == "OB,FVG,BSL,SSL"
        saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert saved["detector"]["htf_context_types"] == "OB,FVG,BSL,SSL"
        # повторный вызов ничего не пишет
        again = migrate_saved_htf_profile_v3(db, settings)
        assert again.get("repeated") is True
    finally:
        db.close()


def test_profile_v3_migrates_ob_only_and_skips_explicit(tmp_path):
    db = Database(":memory:")
    try:
        settings = _settings(tmp_path, "OB")
        assert migrate_saved_htf_profile_v3(db, settings)["action"] == "migrated"
        db2 = Database(":memory:")
        try:
            explicit = _settings(tmp_path, "OB,FVG,BSL")
            skipped = migrate_saved_htf_profile_v3(db2, explicit)
            assert skipped["action"] == "skipped"
            saved = json.loads(
                (tmp_path / "settings.json").read_text(encoding="utf-8"))
            assert saved["detector"]["htf_context_types"] == "OB,FVG,BSL"
        finally:
            db2.close()
    finally:
        db.close()
