"""T01/T02 (ТЗ 07.10.2026 §2, §12): статистика разбора reviews (4).json.

76 оценок / 76 zone.id; 61 correct + 15 wrong_type по decision;
нормализованные причины 61 correct + 9 wrong_type + 6 already_completed
(15 отклонений ≠ 15 ошибок распознавания); 20 W1 + 56 D1; 38 bull + 38 bear;
BTC 24, ETH 25, SOL 27; группы подтверждения 29 manual_only / 35 fvg_confirmed
/ 12 unconfirmed.
"""
import json
from collections import Counter
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "reviews4_export_2026_10_07.json"


def _labels():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["labels"]


def test_counts_match_analysis():
    labels = _labels()
    assert len(labels) == 76
    assert len({l["zone_id"] for l in labels}) == 76

    decisions = Counter(l["decision"] for l in labels)
    assert decisions == {"correct": 61, "wrong_type": 15}

    reasons = Counter(l["reason"] for l in labels)
    assert reasons == {"correct": 61, "wrong_type": 9, "already_completed": 6}

    tf = Counter(l["timeframe"] for l in labels)
    assert tf == {"D1": 56, "W1": 20}

    directions = Counter(l["direction"] for l in labels)
    assert directions == {"bull": 38, "bear": 38}

    assets = Counter(l["instrument"].replace("USDT", "") for l in labels)
    assert assets == {"BTC": 24, "ETH": 25, "SOL": 27}

    conf = Counter(l["confirmation_state"] for l in labels)
    assert conf == {"manual_only": 29, "fvg_confirmed": 35, "unconfirmed": 12}


def test_confirmation_groups_semantics():
    """29 manual_only — все correct; 12 unconfirmed — все wrong_type;
    35 fvg_confirmed: 32 correct + 3 wrong_type."""
    labels = _labels()
    mo = [l for l in labels if l["confirmation_state"] == "manual_only"]
    assert len(mo) == 29 and all(l["decision"] == "correct" for l in mo)
    un = [l for l in labels if l["confirmation_state"] == "unconfirmed"]
    assert len(un) == 12 and all(l["decision"] == "wrong_type" for l in un)
    fc = [l for l in labels if l["confirmation_state"] == "fvg_confirmed"]
    assert len(fc) == 35
    assert sum(1 for l in fc if l["decision"] == "correct") == 32
    assert sum(1 for l in fc if l["decision"] == "wrong_type") == 3


def test_manual_only_registry_ids():
    """Список 29 manual_only совпадает с §5 ТЗ."""
    expected = {284, 327, 568, 310, 40, 585, 324, 373, 340, 631, 92, 95, 764,
                732, 490, 752, 246, 479, 792, 513, 499, 782, 238, 516, 263,
                267, 803, 807, 810}
    actual = {l["zone_id"] for l in _labels()
              if l["confirmation_state"] == "manual_only"}
    assert actual == expected


def test_long_base_examples_present():
    """T03: №505 (12 свечей) и №516 (11) — положительные эталоны длинных
    баз; №526 (12) отклонён — длина сама по себе не разделяет классы."""
    by_id = {l["zone_id"]: l for l in _labels()}
    assert by_id[505]["decision"] == "correct"
    assert by_id[516]["decision"] == "correct"
    assert by_id[526]["decision"] == "wrong_type"
    assert by_id[505]["timeframe"] == by_id[516]["timeframe"] == "D1"
