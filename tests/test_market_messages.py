"""Каталог сообщений, уровни машины и три варианта карточки BTC."""
from __future__ import annotations

from app.config import Settings
from app.engine.ltf.breaks import expected_structure_conditions
from app.engine.ltf.pivots import PivotCandidate
from app.models import (
    Candle, Direction, Instrument, Zone, ZoneStatus, ZoneType,
)
from app.notify.formatting import fmt_price_ru, format_level
from app.services.market_copy import CATALOG_CODES, render
from app.services.presentation import (
    _reconcile_action, assemble, condition_texts, select_headline,
)
from app.services.reconcile import fvg_contradiction, read_reconcile, reconcile_zone
from tests.conftest import make_candle

T0 = 1_700_000_000_000
H1 = 3_600_000


def _pivot(price, kind, role, t, pid):
    return PivotCandidate(
        instrument_id=1, price=price, kind=kind, pivot_at=t,
        candle_open_time=t, confirmed_at=t + 100, left=1, right=1,
        state="confirmed", pivot_id=pid, role=role,
    )


def test_catalog_covers_every_code():
    assert len(CATALOG_CODES) == 56
    for code in CATALOG_CODES:
        msg = render(code, {"types": "OB", "timeframes": "D1"})
        assert msg["headline"]
        assert msg["headline"] != "Сообщение не настроено"
        assert len(msg["headline"]) <= 70
    assert render("Z99")["headline"] == "Сообщение не настроено"


def test_wait_headlines_are_mirrors():
    bull = render("H01", {"kind": "BOS", "level": "111"})
    bear = render("H02", {"kind": "BOS", "level": "111"})
    assert bull["headline"] == "Ожидаем подтверждения роста на H1"
    assert bear["headline"] == "Ожидаем подтверждения снижения на H1"
    assert "выше" in bull["detail"] and "ниже" in bear["detail"]
    touch = render("H11", {"zone": "FVG", "time": "—"})
    assert "касание само по себе вход не подтверждает" in touch["detail"].lower()
    assert "лонг готов" not in touch["detail"].lower()
    assert "точка входа" not in touch["headline"].lower()


def test_format_level_keeps_meaning():
    btc = format_level(81822.67)
    assert btc["text"] == "81 822,67"
    assert btc["approximate"] is False
    manual = format_level(80087.56632381)
    assert manual["approximate"] is True
    assert manual["compact"] == "80 087,57"
    assert manual["text"] == manual["exact"]
    assert "80 087,57" not in manual["text"]
    small = format_level(0.001678)
    assert small["text"] == "0,001678"
    assert fmt_price_ru(110) == "110,00"


def test_sms_without_pullback_is_not_ready():
    pivots = [
        _pivot(90, "low", "HL", T0 + 1000, 1),
        _pivot(100, "high", "HH", T0 + 1200, 2),
        _pivot(95, "low", "internal_low", T0 + 1400, 3),
    ]
    idle = expected_structure_conditions(pivots, [], Direction.BEAR, T0 + 50_000)
    assert idle["bos"]["status"] == "ready"
    assert idle["bos"]["level"] == 90
    assert idle["sms"]["status"] == "waiting_prerequisite"
    assert idle["sms"]["level"] == 95
    assert idle["sms"]["opens_scenario"] is False
    assert idle["either"] is False
    text = condition_texts(idle)[1]["text"]
    assert "откат" in text
    assert "95" in text
    broken = expected_structure_conditions(
        pivots,
        [make_candle(T0 + 2000, 92, 93, 86, 88, timeframe="H1")],
        Direction.BEAR, T0 + 50_000,
    )
    assert broken["bos"]["status"] == "occurred"
    assert broken["sms"]["status"] == "waiting_prerequisite"
    assert broken["sms"]["level"] == 95


def test_bull_conditions_mirror_bear():
    pivots = [
        _pivot(108, "high", "LH", T0 + 2000, 4),
        _pivot(88, "low", "LL", T0 + 2200, 5),
        _pivot(93, "high", "internal_high", T0 + 2400, 6),
    ]
    cond = expected_structure_conditions(pivots, [], Direction.BULL, T0 + 50_000)
    assert cond["bos"]["status"] == "ready"
    assert cond["bos"]["level"] == 108
    assert cond["bos"]["side"] == "above"
    assert cond["sms"]["level"] == 93
    assert cond["sms"]["status"] == "waiting_prerequisite"
    missing = expected_structure_conditions(
        [_pivot(88, "low", "LL", T0 + 2200, 5)], [], Direction.BULL, T0 + 50_000,
    )
    assert missing["bos"]["level"] is None
    assert "опоры" in missing["bos"]["missing"]


def _btc_zone():
    return {
        "id": 1, "type": "fvg", "timeframe": "W1", "direction": "bull",
        "lower": 81951, "upper": 82563, "status": "active",
    }


def test_btc_variant_contradiction_does_not_invent_bos():
    facts = {
        "instrument": {"symbol": "BTCUSDT", "venue": "Binance", "market_type": "spot"},
        "instrument_id": 1, "as_of": T0, "ruleset_id": "x", "data_version": 1,
        "price": 81822.67, "quote_at": T0, "quote_ok": True,
        "data_state": {"state": "ok"},
        "engine": {"ltf_enabled": True, "ltf_analyze": True, "replaying": False},
        "direction": "bull", "basis": "nearest",
        "selected": {"id": 1, "state": "waiting_structure"},
        "zone": _btc_zone(),
        "inconsistent": {
            "blocks": True, "scope": "недельной FVG",
            "gap": "зона сохранена действующей, но движение требует сверки заполнения",
            "job": "расчёт в очереди. Запустить сверку",
            "action": {
                "kind": "reconcile", "zone_id": 1,
                "label": "Запустить сверку", "href": "/api/zones/1/reconcile",
            },
        },
        "other_zones": [
            {"code": "C06", "zone_id": 2, "params": {"missing": "условие подтверждения не записано"}},
            {"code": "C14", "zone_id": 3, "detail": "Причина завершения не записана"},
        ],
        "conditions": {"bos": {"status": "ready", "level": 83000, "kind": "BOS", "side": "above"}},
    }
    card = assemble(facts)
    assert card["headline"]["code"] == "C18"
    assert "недельной FVG" in card["headline"]["headline"]
    assert "83 000" not in card["headline"]["headline"]
    assert card["next_conditions"] == []
    assert "83 000" not in (card["structure"]["text"] or "")
    assert card["location"]["relation"] == "below"
    codes = [z["code"] for z in card["other_zones"]]
    assert codes[0] in {"C06", "C14"}
    assert "C06" in codes and "C14" in codes
    joined = " ".join(z.get("detail") or "" for z in card["other_zones"])
    assert "пробоем" not in joined
    parent = [row for row in card["termination_conditions"] if row["code"] == "parent_fvg"]
    assert parent
    assert "W1" not in parent[0]["headline"]
    assert "полном заполнении" in parent[0]["headline"]
    assert card["actions"] == [{
        "kind": "reconcile", "zone_id": 1,
        "label": "Запустить сверку", "href": "/api/zones/1/reconcile",
    }]


def test_btc_variant_filled_fvg_is_not_waiting():
    facts = {
        "instrument_id": 1, "as_of": T0, "price": 81822.67, "quote_ok": True,
        "data_state": {"state": "ok"},
        "engine": {"ltf_enabled": True, "ltf_analyze": True},
        "selected": None,
        "recent_fill": {"time": "01.10.2026 12:00 МСК", "next": "подтверждённого контекста сейчас нет"},
    }
    card = assemble(facts)
    assert card["headline"]["code"] == "C11"
    assert card["next_conditions"] == []
    assert "BOS" not in card["headline"]["headline"]


def test_btc_variant_live_parent_uses_machine_level():
    ready = {
        "instrument_id": 1, "as_of": T0, "price": 81822.67, "quote_ok": True,
        "data_state": {"state": "ok"},
        "engine": {"ltf_enabled": True, "ltf_analyze": True},
        "direction": "bull", "basis": "nearest",
        "selected": {"id": 4, "state": "waiting_structure"},
        "zone": {"id": 4, "type": "ob", "timeframe": "D1", "direction": "bull",
                 "lower": 80000, "upper": 83000, "status": "active"},
        "conditions": {
            "bos": {"kind": "BOS", "status": "ready", "level": 82100, "side": "above",
                    "opens_scenario": True, "timeframe": "H1", "requires_close": True, "strict": True},
            "sms": {"kind": "SMS", "status": "waiting_prerequisite", "level": 81900, "side": "above",
                    "missing": "сначала нужен подтверждённый откат", "opens_scenario": False},
            "either": False, "opens_scenario": ["BOS"],
        },
    }
    card = assemble(ready)
    assert card["headline"]["code"] == "H01"
    assert "82 100" in card["headline"]["detail"]
    sms = [c for c in card["next_conditions"] if c["kind"] == "SMS"][0]
    assert "откат" in sms["text"]
    unknown = dict(ready)
    unknown["conditions"] = {
        "bos": {"kind": "BOS", "status": "waiting_prerequisite", "level": None,
                "missing": "после минимума нет подтверждённой опоры", "opens_scenario": False},
        "sms": {"kind": "SMS", "status": "unavailable", "level": None,
                "missing": "нет подтверждённого внутреннего экстремума"},
    }
    hidden = assemble(unknown)
    assert hidden["headline"]["code"] == "H03"
    assert "82" not in hidden["headline"]["headline"]
    assert "опоры" in hidden["headline"]["detail"]


def test_headline_priority_keeps_fresh_bos_when_quote_is_stale():
    base = {
        "data_state": {"state": "stale", "reason": "quote_stale", "quote_age_s": 40},
        "engine": {"ltf_enabled": True, "ltf_analyze": True},
        "direction": "bull",
        "selected": {"id": 1},
        "conditions": {"bos": {"status": "occurred", "kind": "BOS", "level": 100, "side": "above"}},
    }
    code, _params = select_headline(base)
    assert code == "H07"
    waiting = dict(base)
    waiting["conditions"] = {"bos": {"status": "ready", "kind": "BOS", "level": 100, "side": "above"}}
    waiting["data_state"] = {"state": "stale", "reason": "processing_lag"}
    assert select_headline(waiting)[0] == "Q03"
    off = dict(base)
    off["engine"] = {"ltf_enabled": False, "ltf_analyze": True}
    assert select_headline(off)[0] == "Q05"
    replay = dict(base)
    replay["engine"] = {"ltf_enabled": True, "ltf_analyze": True, "replaying": True}
    assert select_headline(replay)[0] == "Q06"
    replay["conditions"] = {"bos": {"status": "ready", "kind": "BOS", "level": 100, "side": "above"}}
    held = assemble(replay)
    assert held["headline"]["code"] == "Q06"
    assert held["next_conditions"] == []


def test_reconcile_action_follows_job_state():
    queued = _reconcile_action(7, {"status": "queued"})
    assert queued["label"] == "Запустить сверку"
    assert queued["href"] == "/api/zones/7/reconcile"
    assert _reconcile_action(7, {"status": "running"}) is None
    assert _reconcile_action(7, {"status": "resolved", "outcome": "fill_recorded"}) is None
    retry = _reconcile_action(7, {"status": "error", "error": "сбой"})
    assert retry["label"] == "Повторить расчёт"


def test_reconcile_records_only_proven_fill(db):
    iid = db.upsert_instrument(Instrument(
        id=None, asset="BTC", venue="binance", market_type="spot",
        symbol="BTCUSDT", quote_asset="USDT",
    ))
    zone_id = db.insert_zone(Zone(
        id=None, instrument_id=iid, type=ZoneType.FVG, direction=Direction.BULL,
        timeframe="W1", lower=81951, upper=82563, formed_at=T0, confirmed_at=T0,
        status=ZoneStatus.ACTIVE,
    ))
    zone = db.get_zone(zone_id)
    db.insert_candles([make_candle(
        T0, 82600, 82700, 81900, 82000, timeframe="W1", instrument_id=iid,
    )])
    assert fvg_contradiction(db, zone, 81822.67, True) is True
    assert read_reconcile(db, zone_id)["status"] == "queued"
    result = reconcile_zone(db, Settings(), zone_id)
    assert result["status"] == "resolved"
    assert result["outcome"] == "fill_recorded"
    fresh = db.get_zone(zone_id)
    assert fresh.display_until is not None
    assert "fvg_filled" in (fresh.end_reason or "")
    assert fvg_contradiction(db, fresh, 81822.67, True) is False
