"""A base is post-drawdown accumulation, not a min/max box of a cycle."""
import json

from app.alt.engine_v3 import DAY, WEEK, RULES_VERSION, analyze
from app.alt.store_v3 import list_structures, persist_result
from app.alt.runner import AltRunner
from app.models_alt import AltRun
from tests.test_alt_engine_v3 import FIXTURES, stamp


def daily(weeks):
    start = stamp("2025-01-06")
    return [{"open_time": start + i * WEEK + d * DAY,
             "open": o, "high": h, "low": l, "close": c}
            for i, (o, h, l, c) in enumerate(weeks) for d in range(7)]


PEAK = [(95, 100, 90, 95), (50, 75, 40, 45), (33, 36, 29, 30)]
BALANCE = [(26, 28, 24, 26), (26, 29, 24.5, 27)]


def test_good_accumulation_between_70_and_80_percent_drawdown():
    rows = daily(PEAK + BALANCE * 9)
    assert .70 < 1 - min(r["low"] for r in rows) / 100 < .80
    b, = analyze(rows)["structures"]
    assert (b["geometry"]["lower"], b["geometry"]["upper"]) == (24, 29)
    assert b["geometry"]["base_start_open_time"] == stamp("2025-01-27")
    assert b["revisions"][0]["available_at_ms"] - b["geometry"]["base_start_open_time"] >= 101 * DAY
    assert b["revisions"][0]["quality_metrics"]["prior_ath"] == 100


def test_same_sideways_pattern_above_70_percent_gate_is_not_a_base():
    rows = daily([(95, 100, 90, 95), (50, 75, 40, 45), (42, 44, 38, 40)]
                 + [(40, 43, 35, 40), (40, 44, 36, 41)] * 10)
    assert not analyze(rows)["structures"]


def test_exact_70_percent_upper_boundary_is_admissible():
    balance = [(26, 29, 24, 26), (26, 30, 24.5, 27)]
    assert analyze(daily(PEAK + balance * 9))["structures"][0]["geometry"]["upper"] == 30


def test_falling_market_below_70_percent_is_not_accumulation():
    decline = [(29-i*.5, 30-i*.5, 28-i*.5, 28.5-i*.5) for i in range(25)]
    assert not analyze(daily(PEAK + decline))["structures"]


def test_single_post_maturity_wick_cannot_expand_base_towards_ath():
    b, = analyze(daily(PEAK + BALANCE * 9 + [(26, 95, 25, 26)]))["structures"]
    assert (b["geometry"]["lower"], b["geometry"]["upper"]) == (24, 29)


def test_time_spent_falling_is_not_counted_as_accumulation():
    decline = [(80-i, 82-i, 78-i, 79-i) for i in range(30)]
    assert not analyze(daily(PEAK[:1] + decline + BALANCE * 5))["structures"]


def test_future_ath_does_not_retroactively_admit_earlier_balance():
    first = daily([(45, 50, 44, 45), (32, 35, 30, 31), (29, 30, 27, 28)] + BALANCE * 9)
    assert not analyze(first)["structures"]
    later = daily([(45, 50, 44, 45), (32, 35, 30, 31), (29, 30, 27, 28)]
                  + BALANCE * 9 + [(80, 100, 75, 95)] + PEAK[1:] + BALANCE * 9)
    result = analyze(later)
    assert result["structures"]
    assert all(b["geometry"]["base_start_open_time"] >= first[-1]["open_time"] + DAY for b in result["structures"])
    assert not analyze(later, as_of_ms=first[-1]["open_time"] + DAY)["structures"]


def test_rally_roundtrip_does_not_count_towards_later_accumulation_age():
    peak = [(950, 1000, 900, 950), (450, 700, 350, 400), (30, 40, 29, 31)]
    rally = [(v, v+2, v-2, v) for v in [35, 60, 100, 150, 180, 150, 100, 60, 35]]
    before = peak + BALANCE * 2 + rally
    assert not analyze(daily(before + BALANCE * 3))["structures"]
    b, = analyze(daily(before + BALANCE * 9))["structures"]
    assert b["geometry"]["base_start_open_time"] == stamp("2025-01-06") + len(before) * WEEK
    assert b["geometry"]["upper"] == 29


def test_independent_cycles_survive_the_new_admission_rule():
    first = PEAK + BALANCE * 9
    breakout = [(32, 40, 31, 38), (34, 35, 30, 32)]
    b1, b2 = analyze(daily(first + breakout + BALANCE * 9))["structures"]
    assert b1["state"] == "completed" and b2["state"] == "mature"
    assert b1["structure_key"] != b2["structure_key"]
    assert b1["geometry"]["base_end_open_time"] < b2["geometry"]["base_start_open_time"]


def test_sui_screenshot_ath_box_is_rejected_at_all_prefixes():
    rows = json.loads((FIXTURES / "SUI.json").read_text())
    for at in ["2025-03-24", "2025-10-13", "2026-05-25", "2026-10-06"]:
        result = analyze(rows, as_of_ms=stamp(at))
        assert not any(b["geometry"]["base_start_open_time"] == stamp("2024-10-28")
                       or b["geometry"]["upper"] == 5.3687 for b in result["structures"])
        for b in result["structures"]:
            r = b["revisions"][0]
            assert r["upper"] <= r["quality_metrics"]["admission_ceiling"] + 1e-10


def test_sui_lower_accumulation_is_a_separate_100_day_base():
    rows = json.loads((FIXTURES / "SUI.json").read_text())
    assert not analyze(rows, as_of_ms=stamp("2026-09-13"))["structures"]
    b, = analyze(rows)["structures"]
    assert b["geometry"]["base_start_open_time"] == stamp("2026-06-01")
    assert (b["geometry"]["lower"], b["geometry"]["upper"]) == (.6354, .954)
    assert b["revisions"][0]["available_at_ms"] == stamp("2026-09-14")
    assert b["revisions"][-1]["base_end_confirmed_at_ms"] == stamp("2026-09-28")


async def test_rule_upgrade_backfills_once_and_hides_preserved_old_structures(market):
    from app.alt.store_v3 import frame
    from app.notify.alt_queue import AltDispatcher
    from app.notify.outbox import Card
    from tests.test_alt_notify import FakeSender
    db, settings, src, current = market
    asset = db.get_alt_asset(src.asset_id)
    # Simulate the old read model and an already queued old notification.
    db.conn.execute("UPDATE alt_structure SET rules_version='alt-0.5'")
    db.conn.execute("UPDATE alt_structure_revision SET rules_version='alt-0.5'")
    db.conn.execute("UPDATE alt_structure_revision SET revision_id='old-' || revision_id")
    db.conn.execute("UPDATE alt_structure SET structure_key='old-' || structure_key")
    db.conn.execute("UPDATE alt_v3_event SET structure_key='old-' || structure_key")
    db.conn.execute("UPDATE alt_v3_event SET event_key='old-' || event_key")
    event = db.conn.execute("SELECT id FROM alt_v3_event LIMIT 1").fetchone()[0]
    db.conn.execute("UPDATE alt_v3_event SET delivered=0 WHERE id=?", (event,))
    db.conn.commit()
    sender = FakeSender()
    dispatcher = AltDispatcher(db, settings, sender)
    dispatcher.outbox.put("alt_v3", "old-rule-packet", Card(""), [event])
    await dispatcher.dispatch_v3()
    assert not sender.alt
    assert frame(db, current["structure_id"]) is None
    assert not list_structures(db, asset.id, src.id)
    db.set_meta(f"alt:engine:{asset.id}:{src.id}", "v3")
    db.set_meta(f"alt:loaded:{asset.id}:{src.id}", "old")
    db.set_meta(f"alt:rules:{asset.id}:{src.id}", "alt-0.5")
    runner = AltRunner(db, settings, {}, None)
    mode, _ = await runner._process_asset(asset, src, 0, stamp("2026-08-18"))
    assert mode == "backfill"
    assert db.conn.execute("SELECT COUNT(*) FROM alt_structure WHERE rules_version='alt-0.5'").fetchone()[0] > 0
    assert list_structures(db, asset.id, src.id)[-1]["rules_version"] == RULES_VERSION
    assert db.conn.execute("SELECT COUNT(*) FROM alt_v3_event WHERE delivered=0").fetchone()[0] == 0
    assert (await runner._process_asset(asset, src, 0, stamp("2026-08-18")))[0] == "live"


def test_new_rules_trigger_one_immediate_run_after_old_success(db):
    from datetime import datetime, timezone
    from app.config import Settings
    now = datetime(2026, 10, 16, 12, tzinfo=timezone.utc)
    db.insert_alt_run(AltRun(None, started_ms=int(now.timestamp()*1000), as_of_ms=0, status="ok"))
    db.set_meta("alt:engine:1:1", "v3")
    db.set_meta("alt:v3:initialized", "old")
    runner = AltRunner(db, Settings(), {}, None)
    assert runner.should_run(now)
    db.set_meta("alt:v3:rules_attempted", RULES_VERSION)
    assert not runner.should_run(now)


# Share the real NEAR DB fixture rather than synthesizing API/store contracts.
from tests.test_alt_v3_integration import market
