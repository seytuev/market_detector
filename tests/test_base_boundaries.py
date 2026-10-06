"""T15 (ТЗ 06.10.2026 §8): журнал решений базы и флаг самостоятельных FVG.

Никаких новых порогов длины/ширины/ATR из отклонённых примеров — только
воспроизводимая диагностика: решение include/exclude с основанием по каждой
свече и явный флаг свечей, входящих в самостоятельные FVG.
"""
from app.engine.fvg import scan_fvgs
from app.engine.orderblock import find_base
from tests.conftest import load_etalon_candles, make_candle


def test_candle_decisions_journal_complete(cfg):
    """Каждая рассмотренная свеча имеет решение и основание; члены базы
    совпадают с included_open_times."""
    candles = load_etalon_candles()
    fvg = next(f for f in scan_fvgs(candles, "H4")
               if f.formed_at == 1780531200000)  # внешний FVG эталона
    base = find_base(candles, fvg, cfg)
    assert base is not None
    decisions = base.evidence["candle_decisions"]
    assert decisions and all(d.get("reason") for d in decisions)
    included = [d["open_time"] for d in decisions if d["decision"] == "include"]
    assert sorted(included) == sorted(base.source_candles)
    # эталонная геометрия не изменилась (T24)
    assert base.source_candles == [
        1780416000000, 1780430400000, 1780444800000, 1780459200000,
    ]


def test_independent_fvg_members_flagged(cfg):
    """Свеча базы, входящая в самостоятельный FVG, помечается флагом —
    диагностика спорного признака §8, а не скрытое исключение."""
    candles = load_etalon_candles()
    fvg = next(f for f in scan_fvgs(candles, "H4")
               if f.formed_at == 1780531200000)
    base = find_base(candles, fvg, cfg)
    assert base is not None
    flagged = [d for d in base.evidence["candle_decisions"]
               if d.get("independent_fvg_member")]
    assert base.evidence["contains_independent_fvg_members"] == bool(flagged)
    for d in flagged:
        assert d["decision"] == "include"  # флаг — не исключение (T24)


def test_exit_candle_stops_expansion_with_reason(cfg):
    """Свеча выхода (тело за диапазоном) не включается, причина записана."""
    # бычий FVG после импульса; база — 2 свечи; перед ними — сильная свеча выхода
    bars = [
        make_candle(1000, 100, 160, 95, 155, "H1"),   # далеко внизу — выход
        make_candle(2000, 155, 158, 148, 150, "H1"),  # база
        make_candle(3000, 150, 153, 147, 152, "H1"),  # база
        make_candle(4000, 152, 165, 151, 164, "H1"),  # импульс
        make_candle(5000, 164, 170, 163, 169, "H1"),  # 1
        make_candle(6000, 169, 175, 168, 174, "H1"),  # 2
        make_candle(7000, 174, 190, 176, 189, "H1"),  # 3: High1=165 < Low3=176
    ]
    fvg = scan_fvgs(bars, "H1")[-1]
    base = find_base(bars, fvg, cfg)
    assert base is not None
    assert 1000 not in base.source_candles
    decisions = {d["open_time"]: d for d in base.evidence["candle_decisions"]}
    assert decisions[1000]["decision"] == "exclude"
    assert "выхода" in decisions[1000]["reason"]
    assert base.evidence["stop"] is not None


def test_no_new_thresholds_in_config(cfg):
    """T15: полей порогов базы не прибавилось — только прежняя
    некалиброванная настройка длины."""
    fields = set(type(cfg).__dataclass_fields__)
    assert "uncalibrated_consolidation_max_candles" in fields
    new = [f for f in fields if any(
        k in f for k in ("base_width", "base_atr", "consolidation_width",
                         "consolidation_pct")
    )]
    assert new == []
