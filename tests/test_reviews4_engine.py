"""ТЗ 07.10.2026: T04 (scan limit — не граница базы), T09 (причинный anchor),
T13 (ручное исключение из входов), T10 (preferred entry zone), §5 (внешний
FVG: равенство краёв допустимо).
"""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.engine.fvg import scan_fvgs
from app.engine.orderblock import find_base, is_external
from app.models import (
    Direction,
    Zone,
    ZoneStatus,
    ZoneType,
    close_boundary_ms,
    now_ms,
)
from app.web.api import create_app
from tests.conftest import make_candle


def _sideways_bear(i: int) -> list:
    return make_candle(1000 + i * 3_600_000, 102, 103, 99, 100, "H1")


def test_scan_limit_marks_search_limited_not_base_edge(cfg):
    """T04: технический лимит 12 свечей — предупреждение о неполном поиске,
    а не объявление рыночной границы базы."""
    candles = [_sideways_bear(i) for i in range(15)]
    candles += [
        make_candle(1000 + 15 * 3_600_000, 100, 110, 99, 109, "H1"),  # импульс
        make_candle(1000 + 16 * 3_600_000, 150, 152, 149, 151, "H1"),
        make_candle(1000 + 17 * 3_600_000, 151, 153, 150, 152, "H1"),
        make_candle(1000 + 18 * 3_600_000, 152, 160, 158, 159, "H1"),
    ]
    fvg = scan_fvgs(candles, "H1")[-1]
    base = find_base(candles, fvg, cfg)
    assert base is not None
    assert len(base.source_candles) == cfg.uncalibrated_consolidation_max_candles
    assert base.evidence["base_search_limited"] is True
    assert "более ранний контекст не проверен" in \
        base.evidence["base_search_limited_reason"]


def test_anchor_only_causal_candle(cfg):
    """T09: якорная тень — только от свечи причинного движения (в цвете
    импульса); OHLC, направление и момент доступности сохраняются."""
    base_candle = make_candle(1000, 101, 102, 99, 100, "H1")      # база (bear)
    impulse = make_candle(2000, 98, 101, 97, 100, "H1")           # bull — причинная
    f1 = make_candle(3000, 100, 101, 99, 100.5, "H1")
    f2 = make_candle(4000, 100.5, 101.5, 100, 101, "H1")
    f3 = make_candle(5000, 101, 110, 108, 109, "H1")
    candles = [base_candle, impulse, f1, f2, f3]
    fvg = scan_fvgs(candles, "H1")[-1]
    base = find_base(candles, fvg, cfg)
    assert base is not None
    assert base.lower == 97.0  # тень причинной свечи расширила основание
    anchor = base.evidence["boundary_anchor"]
    assert anchor["ohlc"] == [98, 101, 97, 100]
    assert anchor["direction"] == "bull"
    assert anchor["available_at"] == close_boundary_ms(2000, "H1")
    assert anchor["original"] == 99.0


def test_anchor_rejected_for_counter_color_candle(cfg):
    """T09: свеча противоположного цвета не является причинным движением —
    её тень базу не расширяет (поздний ретест не переписывает геометрию)."""
    base_candle = make_candle(1000, 101, 102, 99, 100, "H1")
    counter = make_candle(2000, 100, 101, 97, 98, "H1")           # bear — НЕ причинная
    f2 = make_candle(3000, 98, 99, 97.5, 98.5, "H1")
    f3 = make_candle(4000, 98.5, 105, 104, 104.5, "H1")
    candles = [base_candle, counter, f2, f3]
    fvg = scan_fvgs(candles, "H1")[-1]
    base = find_base(candles, fvg, cfg)
    assert base is not None
    assert base.lower == 99.0
    assert "boundary_anchor" not in base.evidence


def test_external_fvg_touching_edges_allowed(cfg):
    """§5 ТЗ 07.10.2026: bull L_fvg >= U_ob / bear U_fvg <= L_ob — равенство
    касающихся краёв допустимо как неперекрытие положительной ширины."""
    from app.engine.orderblock import BaseRecord
    from app.engine.fvg import FvgRecord
    base = BaseRecord(direction=Direction.BULL, lower=90.0, upper=100.0,
                      formed_at=0, source_candles=[0], start_idx=0, end_idx=0,
                      gap_candles=1)
    fvg_touch = FvgRecord(direction=Direction.BULL, lower=100.0, upper=101.0,
                          formed_at=0, confirmed_at=0,
                          candle_open_times=(0, 1, 2))
    fvg_inside = FvgRecord(direction=Direction.BULL, lower=99.99, upper=101.0,
                           formed_at=0, confirmed_at=0,
                           candle_open_times=(0, 1, 2))
    assert is_external(base, fvg_touch) is True
    assert is_external(base, fvg_inside) is False


# ----- T13 / T10: веб-уровень -----

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture()
def settings(tmp_path) -> Settings:
    s = Settings()
    s.auth_token = TOKEN
    s.db_path = str(tmp_path / "htf_zones.db")
    return s


@pytest.fixture()
def client(db, settings) -> TestClient:
    return TestClient(create_app(db, settings))


def _ob(db, iid, **kw):
    base = dict(id=None, instrument_id=iid, type=ZoneType.OB,
                direction=Direction.BULL, timeframe="D1", lower=100.0,
                upper=110.0, formed_at=now_ms() - 100 * 86_400_000,
                confirmed_at=None, status=ZoneStatus.CANDIDATE, source="auto",
                source_candles=[now_ms() - 100 * 86_400_000], evidence={},
                created_at=now_ms())
    base.update(kw)
    return db.insert_zone(Zone(**base))


def test_deep_test_comment_excludes_entry_without_close_beyond(db, client, instrument_id):
    """T13 (№212/№215): «90% протестирована / множественный тест» — ручное
    исключение из новых входов; market_validity не трогаем, close_beyond не
    выдумываем, история тестов не сбрасывается."""
    zid = _ob(db, instrument_id, entry_eligible=True)
    resp = client.post(f"/api/zones/{zid}/review",
                       json={"decision": "wrong_type",
                             "text": "на 90% протестирована 13 июля, неактуально"},
                       headers=AUTH)
    assert resp.status_code == 200
    z = db.get_zone(zid)
    assert z.status == ZoneStatus.CANDIDATE          # не отклонена по типу
    assert z.market_validity == "active"             # пробой не выдуман
    assert z.display_until is None
    assert z.entry_eligible is False                 # исключена из новых входов
    assert z.evidence["manual_entry_exclusion"] == "deep_test_entry_excluded"
    a = resp.json()["assessment"]
    assert a["reason_code"] == "deep_test_entry_excluded"
    assert a["geometry_verdict"] == "unknown"        # не ошибка геометрии


def test_repeated_test_comment_reason(db, client, instrument_id):
    """T13 (№215): «Глубокий и множественный тест» — repeated_test_manual_exclusion."""
    zid = _ob(db, instrument_id)
    resp = client.post(f"/api/zones/{zid}/review",
                       json={"decision": "wrong_type",
                             "text": "Глубокий и множественный тест, неактуально"},
                       headers=AUTH)
    assert resp.status_code == 200
    z = db.get_zone(zid)
    assert z.evidence["manual_entry_exclusion"] == "repeated_test_manual_exclusion"
    assert z.entry_eligible is False
    assert z.market_validity == "active"


def test_prefer_entry_link(db, client, instrument_id):
    """T10/T11 (№342/№340): preferred-зона помечается ссылкой, широкая не
    удаляется и сохраняет собственные границы/историю."""
    wide = _ob(db, instrument_id, lower=90.0, upper=110.0)
    narrow = _ob(db, instrument_id, lower=95.0, upper=110.0,
                 confirmed_at=now_ms() - 90 * 86_400_000)
    resp = client.post(f"/api/zones/{narrow}/prefer-entry",
                       json={"supersedes_zone_id": wide,
                             "comment": "лучше брать точную зону"},
                       headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["entry_preference"] == "preferred"

    z_wide = db.get_zone(wide)
    z_narrow = db.get_zone(narrow)
    assert z_narrow.evidence["supersedes_for_entry"] == wide
    assert z_wide.evidence["superseded_for_entry_by"] == narrow
    assert (z_wide.lower, z_wide.upper) == (90.0, 110.0)  # границы не тронуты
    assert z_wide.status == ZoneStatus.CANDIDATE          # lifecycle свой

    # снятие пометки очищает обе стороны
    resp = client.post(f"/api/zones/{narrow}/prefer-entry",
                       json={"supersedes_zone_id": None}, headers=AUTH)
    assert resp.status_code == 200
    assert "superseded_for_entry_by" not in db.get_zone(wide).evidence
    assert "preferred_for_entry" not in db.get_zone(narrow).evidence
