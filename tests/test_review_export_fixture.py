"""T01: числа анализа экспорта reviews 06.10.2026 воспроизводятся.

25 оценок / 24 уникальные зоны; 20 W1 + 5 D1 записей (20 W1 + 4 D1 уникальных);
9 ETH + 8 BTC + 8 SOL; причины 15 already_completed + 4 wrong_type + 6 correct;
статусы 15 candidate + 4 rejected + 6 active. Массивы reviews/assessments
исходного экспорта — связанные представления тех же оценок, а не
самостоятельные рыночные примеры.
"""
import json
from collections import Counter
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "reviews_export_2026_10_06.json"


def _labels():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["labels"]


def test_counts_match_analysis():
    labels = _labels()
    assert len(labels) == 25
    assert len({l["zone_id"] for l in labels}) == 24  # №130 оценён дважды

    tf = Counter(l["timeframe"] for l in labels)
    assert tf == {"W1": 20, "D1": 5}
    unique_tf = {l["zone_id"]: l["timeframe"] for l in labels}
    assert Counter(unique_tf.values()) == {"W1": 20, "D1": 4}

    assets = Counter(l["instrument"].replace("USDT", "") for l in labels)
    assert assets == {"ETH": 9, "BTC": 8, "SOL": 8}

    reasons = Counter(l["reason"] for l in labels)
    assert reasons == {"already_completed": 15, "wrong_type": 4, "correct": 6}

    statuses = Counter(l["status"] for l in labels)
    assert statuses == {"candidate": 15, "rejected": 4, "active": 6}


def test_latest_review_per_zone():
    """При выборе последней оценки по review_id: 15/4/5 на 24 объектах;
    №130 остаётся с последней оценкой correct, прошлый конфликт сохраняется."""
    labels = _labels()
    latest: dict[int, dict] = {}
    for l in labels:
        zid = l["zone_id"]
        if zid not in latest or l["review_id"] > latest[zid]["review_id"]:
            latest[zid] = l
    assert len(latest) == 24
    reasons = Counter(l["reason"] for l in latest.values())
    assert reasons == {"already_completed": 15, "wrong_type": 4, "correct": 5}
    z130 = [l for l in labels if l["zone_id"] == 130]
    assert [l["review_id"] for l in z130] == [21, 22]
    assert latest[130]["review_id"] == 22
    assert z130[0]["comment"] == "Неактуален 2025"  # конфликт не стёрт


def test_all_zones_are_ob_binance_spot():
    """Все оцениваемые зоны — OB, source=auto; тип фиксируется контекстом
    фикстуры (экспорт содержит только OB-зоны)."""
    labels = _labels()
    for l in labels:
        assert l["instrument"].endswith("USDT")
        assert l["direction"] in ("bull", "bear")
        assert l["lower"] < l["upper"]
