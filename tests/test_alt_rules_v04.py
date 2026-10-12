"""Приёмка alt-0.4 по ТЗ 12.10.2026.

Расчёт читает только OHLC. Интервалы эталонов проверяют результат и не
передаются в алгоритм. Старые тесты alt-0.3, где нижняя линия LINK равна
5.41, остаются критерием предыдущей версии.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.alt.rules_v04 import RULES_VERSION, analyze
from app.alt.store_v04 import persist_result, select_frame
from app.db import Database
from app.models_alt import AltCandle, AltRangeEpisode
from app.services.alt_overview import build_effective_range

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "alt_v2" / "ohlc"
DAY_MS = 86_400_000
TICKERS = ("LINK", "HBAR", "NEAR", "AAVE", "SUI", "ENA", "PUMP", "TAO", "QNT", "ONDO")


def utc_ms(year: int, month: int, day: int) -> int:
    dt = datetime.datetime(year, month, day, tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def as_date(ms: int | None):
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, tz=datetime.timezone.utc).date()


def load_candles(ticker: str) -> list[AltCandle]:
    rows = json.loads((FIXTURES / f"{ticker}.json").read_text(encoding="utf-8"))
    return [
        AltCandle(
            source_id=1,
            open_time=row["open_time"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
        )
        for row in rows
    ]


def geometry_rows(result: dict) -> list[tuple]:
    rows = []
    for structure in result["structures"]:
        for revision in structure["revisions"]:
            rows.append((
                revision["revision_id"],
                round(revision["lower"], 6),
                round(revision["upper"], 6),
                revision["revision_reason"],
                revision["available_at_ms"],
                revision.get("base_end_open_time"),
            ))
    return rows


def visible(result: dict, as_of_ms: int) -> list[tuple]:
    return [row for row in geometry_rows(result) if row[4] <= as_of_ms]


@pytest.fixture(scope="module")
def results() -> dict[str, dict]:
    return {
        ticker: analyze(load_candles(ticker), computed_at_ms=1)
        for ticker in TICKERS
    }


def _in_band(structure: dict, low_lo: float, low_hi: float, up_lo: float, up_hi: float) -> bool:
    geometry = structure["geometry"]
    return (
        low_lo <= geometry["lower"] <= low_hi
        and up_lo <= geometry["upper"] <= up_hi
    )


def test_rules_have_no_ticker_branches() -> None:
    text = Path("app/alt/rules_v04.py").read_text(encoding="utf-8")
    for name in ("LINK", "HBAR", "NEAR", "AAVE", "SUI", "ENA", "PUMP", "TAO", "QNT", "ONDO"):
        assert name not in text


def test_link_etalon_uses_outer_edge(results) -> None:
    result = results["LINK"]
    assert result["rules_version"] == RULES_VERSION
    assert result["status"] == "ok"
    matched = [
        item for item in result["structures"]
        if _in_band(item, 4.60, 4.85, 9.45, 9.75)
    ]
    assert len(matched) == 1
    structure = matched[0]
    geometry = structure["geometry"]
    assert as_date(geometry["base_start_open_time"]).year == 2022
    assert abs(geometry["lower"] - 5.41) > 0.2
    final = structure["revisions"][-2]
    assert final["revision_reason"] == "late_boundary_confirmed"
    assert as_date(final["available_at_ms"]) >= datetime.date(2023, 8, 29)
    assert abs(final["lower"] - geometry["lower"]) < 1e-9
    done = structure["revisions"][-1]
    assert done["geometry_kind"] == "completed_base"
    assert done["available_at_ms"] > final["available_at_ms"]
    assert abs(done["lower"] - final["lower"]) < 1e-9
    assert abs(done["upper"] - final["upper"]) < 1e-9
    supports = final["boundary_evidence"]["lower"]["supports"]
    assert any(abs(item["price"] - 4.761) < 0.02 for item in supports)
    old_anchor = [
        item["price"]
        for item in supports + (final["boundary_evidence"]["lower"].get("excluded") or [])
    ]
    assert not any(abs(price - 2.0006) < 0.01 for price in old_anchor)
    highs = final["boundary_evidence"]["upper"]["supports"]
    assert any(abs(item["price"] - 9.64) < 0.02 for item in highs)
    assert final["boundary_evidence"]["lower"]["method"] == "outer_edge"


def test_link_june_line_is_not_available_before_confirmation(results) -> None:
    candles = load_candles("LINK")
    as_of = utc_ms(2023, 6, 30)
    early = analyze(candles, as_of_ms=as_of, computed_at_ms=2)
    for revision in visible(early, as_of):
        assert abs(revision[1] - 4.761) > 0.05
    full_visible = visible(results["LINK"], as_of)
    assert full_visible == visible(early, as_of)
    later = [
        revision for revision in geometry_rows(results["LINK"])
        if abs(revision[1] - 4.761) < 0.02 and abs(revision[2] - 9.64) < 0.05
        and revision[3] == "late_boundary_confirmed"
    ]
    assert later
    assert as_date(later[0][4]) >= datetime.date(2023, 8, 29)


def test_hbar_etalon_keeps_local_exit(results) -> None:
    result = results["HBAR"]
    assert result["status"] == "ok"
    matched = [
        item for item in result["structures"]
        if _in_band(item, 0.028, 0.036, 0.130, 0.150)
    ]
    assert len(matched) == 1
    structure = matched[0]
    start = as_date(structure["geometry"]["base_start_open_time"])
    assert start >= datetime.date(2022, 12, 1)
    end = as_date(structure["geometry"]["base_end_open_time"])
    assert end is not None and end < datetime.date(2024, 12, 1)
    assert structure["children"]
    assert structure["children"][0]["reason"] == "local_range_exit"
    final = structure["revisions"][-2]
    low_prices = [
        item["price"]
        for item in final["boundary_evidence"]["lower"]["supports"]
        + (final["boundary_evidence"]["lower"].get("excluded") or [])
    ]
    assert not any(abs(price - 0.00914) < 0.0001 for price in low_prices)
    assert structure["children"][0]["parent_structure_key"] == structure["structure_key"]
    excluded = [item["price"] for item in final["boundary_evidence"]["upper"]["excluded"]]
    assert not any(abs(price - 0.3922) < 0.01 for price in excluded + [
        item["price"] for item in final["boundary_evidence"]["upper"]["supports"]
    ])


def test_near_upper_is_in_band_and_lower_conflict_is_explicit(results) -> None:
    """Общее правило внешнего края не подтверждает опубликованный низ 1.08–1.14.

    Верх 2.846 укладывается в 2.82–2.93. Низ получается 1.020: это край
    отдельного подтверждённого кластера, а не 0.971 и не полка 1.094.
    Интервал эталона не расширяется. Отдельной ветки для тикера нет.
    """
    result = results["NEAR"]
    assert result["status"] == "ok"
    matched = []
    for item in result["structures"]:
        upper = item["geometry"]["upper"]
        start = as_date(item["geometry"]["base_start_open_time"])
        if start and start.year == 2022 and start.month == 12 and 2.82 <= upper <= 2.93:
            matched.append(item)
    assert len(matched) == 1
    structure = matched[0]
    lower = structure["geometry"]["lower"]
    assert lower < 1.08
    assert abs(lower - 0.971) > 0.02
    final = structure["revisions"][-2]
    assert 2.82 <= final["upper"] <= 2.93
    excluded = final["boundary_evidence"]["lower"]["excluded"]
    assert any(
        abs(item["price"] - 0.971) < 0.01 and item["reason"] == "unconfirmed_extreme"
        for item in excluded
    )
    etalon_prices = []
    for revision in structure["revisions"]:
        for side in ("lower", "upper"):
            evidence = revision["boundary_evidence"][side]
            for bucket in ("supports", "excluded"):
                etalon_prices.extend(item["price"] for item in evidence.get(bucket) or [])
    assert not any(abs(price - 3.443) < 0.01 for price in etalon_prices)
    may = [
        item for item in result["structures"]
        if as_date(item["geometry"]["base_start_open_time"]) == datetime.date(2022, 5, 11)
    ]
    assert len(may) == 1
    prior = may[0]["geometry"]
    assert as_date(prior["base_end_open_time"]) == datetime.date(2022, 11, 7)
    assert prior["lower"] < 3.443 < prior["upper"]


def test_transfer_assets_run_without_etalon(results) -> None:
    for ticker in ("AAVE", "SUI", "ENA", "PUMP", "TAO", "QNT", "ONDO"):
        result = results[ticker]
        assert result["rules_version"] == "alt-0.4"
        assert result["status"] in ("ok", "no_structure", "data_pending", "insufficient_history")
        for structure in result["structures"]:
            assert structure["rules_version"] == "alt-0.4"
            assert structure["geometry"]["upper"] > structure["geometry"]["lower"]


def test_persist_selects_revision_available_at_as_of(results) -> None:
    db = Database(":memory:")
    count = persist_result(db, 7, 3, results["LINK"])
    assert count == len(results["LINK"]["structures"])
    assert db.conn.execute("SELECT COUNT(*) FROM alt_range_episode").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM alt_event").fetchone()[0] == 0
    assert db.conn.execute("SELECT COUNT(*) FROM notification_packet").fetchone()[0] == 0
    again = persist_result(db, 7, 3, results["LINK"])
    assert again == count
    assert db.conn.execute(
        "SELECT COUNT(*) FROM alt_structure WHERE rules_version=?",
        (RULES_VERSION,),
    ).fetchone()[0] == count
    early = select_frame(db, 7, 3, utc_ms(2023, 6, 30))
    if early is not None:
        assert abs(early["lower"] - 4.761) > 0.05
    late_rows = db.conn.execute(
        """SELECT lower, upper, available_at_ms, revision_reason
           FROM alt_structure_revision
           WHERE rules_version=? AND revision_reason='late_boundary_confirmed'""",
        (RULES_VERSION,),
    ).fetchall()
    june = [
        row for row in late_rows
        if abs(row["lower"] - 4.761) < 0.02 and abs(row["upper"] - 9.64) < 0.05
    ]
    assert june
    assert june[0]["available_at_ms"] > utc_ms(2023, 6, 30)
    frame = select_frame(db, 7, 3, june[0]["available_at_ms"])
    assert frame is not None
    assert frame["rules_version"] == "alt-0.4"
    assert frame["geometry_source"] == "structural"
    db.close()


def test_manual_revision_beats_structural_frame() -> None:
    structural = {
        "lower": 4.761, "upper": 9.64, "geometry_source": "structural",
        "rules_version": "alt-0.4", "revision_id": "abc:3",
    }
    episode = AltRangeEpisode(
        id=4, asset_id=1, source_id=2, origin_key="v2:1:2:1",
        lower=5.41, upper=9.57, width=4.16, mid=7.49,
        anchor_start_open_time=1, base_start_open_time=1,
        rules_version="alt-0.3", detected_at_ms=50,
    )
    stored = SimpleNamespace(
        id=9, lower=2.0006, upper=11.03, width=9.0294, mid=6.5153,
        start_anchor_open_time=10, mature_at_ms=20,
    )
    manual = SimpleNamespace(
        id=3, source_kind="manual", lower=4.8, upper=9.7, mid=7.25, width=4.9,
        base_start_open_time=100, base_end_open_time=200,
        reason="уточнение", created_ms=300,
    )
    picked = build_effective_range(
        manual=manual, structural=structural, episode=episode, stored=stored, source_id=2,
    )
    assert picked["geometry_source"] == "manual"
    assert picked["lower"] == 4.8 and picked["upper"] == 9.7
    auto = build_effective_range(
        manual=None, structural=structural, episode=episode, stored=stored, source_id=2,
    )
    assert auto["geometry_source"] == "structural"
    assert auto["lower"] == 4.761 and auto["upper"] == 9.64
    legacy = build_effective_range(
        manual=None, structural=None, episode=episode, stored=stored, source_id=2,
    )
    assert legacy["geometry_source"] == "episode"
    assert legacy["lower"] == 5.41
