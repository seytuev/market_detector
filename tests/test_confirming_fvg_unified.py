"""T08/T09 (ТЗ 06.10.2026 §3.4, §3.5, §7).

Единое основание подтверждения OB: evidence.actual_confirming_fvg,
evidence.confirming_fvg_formed_at и relation.confirming_fvg_id ссылаются
на один и тот же FVG с одной тройкой — и при подтверждении в момент
создания зоны, и при позднем; первая проверенная тройка хранится отдельно
(tested_fvg_triples). Ручное одобрение геометрии не создаёт несуществующие
доказательства (confirmed_at/external_fvg не синтезируются).
"""
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.engine.replay import migrate_display_fields
from app.engine.scanner import Scanner
from app.models import (
    Direction,
    EventKind,
    Instrument,
    Zone,
    ZoneRelation,
    ZoneStatus,
    ZoneType,
    now_ms,
)
from app.web.api import create_app
from tests.conftest import load_etalon_candles


@pytest.fixture()
def etalon_db(db, cfg, instrument_id):
    db.insert_candles(load_etalon_candles(instrument_id))
    Scanner(db, cfg).replay_instrument(instrument_id, timeframes={"H4"})
    return db


def test_evidence_and_relation_reference_same_fvg(etalon_db, instrument_id):
    """T08: у каждого подтверждённого OB evidence и relation — один объект."""
    db = etalon_db
    obs = db.get_zones(instrument_id, types=[ZoneType.OB])
    confirmed = [z for z in obs if z.confirmed_at is not None]
    assert confirmed, "эталон должен давать подтверждённые OB"
    for z in confirmed:
        rel = db.get_relation(z.id)
        assert rel is not None and rel.confirming_fvg_id is not None, z.id
        fvg = db.get_zone(rel.confirming_fvg_id)
        formed = z.evidence.get("confirming_fvg_formed_at")
        actual = z.evidence.get("actual_confirming_fvg")
        assert formed == fvg.formed_at, z.id
        assert actual is not None and actual["formed_at"] == fvg.formed_at, z.id
        assert actual["open_times"] == list(fvg.source_candles), z.id
        assert actual["range"] == [fvg.lower, fvg.upper], z.id
        assert z.confirmed_at == fvg.confirmed_at, z.id
        kinds = {e.kind for e in db.get_events(z.id)}
        assert EventKind.OB_CONFIRMED in kinds, z.id


def test_tested_triple_kept_separately(etalon_db, instrument_id):
    """T08: первая проверенная тройка хранится отдельно от фактического
    подтверждающего FVG (кейс №20/№292 — разные тройки в evidence/relation)."""
    db = etalon_db
    obs = [z for z in db.get_zones(instrument_id, types=[ZoneType.OB])
           if z.confirmed_at is not None]
    # эталонный OB: база найдена от внутреннего FVG, подтверждён внешним —
    # тройки различаются и обе сохранены
    diverged = [
        z for z in obs
        if z.evidence.get("tested_fvg_triples")
        and z.evidence["tested_fvg_triples"][0]
        != z.evidence["actual_confirming_fvg"]["open_times"]
    ]
    assert diverged, "ожидается OB с различными tested/actual тройками"


def test_repair_backfills_legacy_evidence_from_relation(db, cfg, instrument_id):
    """T08: legacy-зона, подтверждённая при создании без evidence-ключей,
    дозаполняется из relation идемпотентной миграцией."""
    fvg = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.FVG,
        direction=Direction.BULL, timeframe="D1", lower=105.0, upper=108.0,
        formed_at=1000, confirmed_at=2000, status=ZoneStatus.ACTIVE,
        source="auto", source_candles=[100, 500, 1000], evidence={}, created_at=100,
    ))
    ob = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="D1", lower=90.0, upper=100.0,
        formed_at=50, confirmed_at=2000, status=ZoneStatus.CANDIDATE,
        source="auto", source_candles=[10, 50], evidence={}, created_at=100,
    ))
    db.set_relation(ZoneRelation(zone_id=ob, confirming_fvg_id=fvg))

    stats = migrate_display_fields(db)
    z = db.get_zone(ob)
    assert z.evidence["confirming_fvg_formed_at"] == 1000
    assert z.evidence["actual_confirming_fvg"] == {
        "formed_at": 1000, "open_times": [100, 500, 1000], "range": [105.0, 108.0],
    }
    # повторный запуск идемпотентен
    migrate_display_fields(db)
    assert db.get_zone(ob).evidence["confirming_fvg_formed_at"] == 1000


# ----- T09: ручное одобрение без синтеза доказательств (кейс №290) -----

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


def test_review_correct_without_fvg_marks_manual_only(db, client, instrument_id):
    """T09 (кейс №290): correct на неподтверждённом кандидате — статус ACTIVE
    сохраняется (одобрение владельца не удаляется), но confirmed_at и
    external_fvg НЕ создаются; расхождение явно (manual_only)."""
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="W1", lower=100.0, upper=110.0,
        formed_at=now_ms() - 10 * 86_400_000, confirmed_at=None,
        status=ZoneStatus.CANDIDATE, source="auto",
        source_candles=[now_ms() - 10 * 86_400_000], evidence={}, created_at=now_ms(),
    ))
    resp = client.post(f"/api/zones/{zid}/review",
                       json={"decision": "correct", "text": "Актуален + SSL"},
                       headers=AUTH)
    assert resp.status_code == 200
    payload = resp.json()["zone"]
    assert payload["status"] == "active"
    assert payload["confirmation_state"] == "manual_only"
    assert payload["confirmed_at"] is None

    z = db.get_zone(zid)
    assert z.confirmed_at is None
    assert z.evidence.get("manual_confirmation_only") is True
    assert z.evidence.get("external_fvg") is not True
    assert "actual_confirming_fvg" not in z.evidence


def test_confirmation_state_fvg_confirmed(db, client, instrument_id):
    """confirmation_state=fvg_confirmed для зоны с confirmed_at."""
    zid = db.insert_zone(Zone(
        id=None, instrument_id=instrument_id, type=ZoneType.OB,
        direction=Direction.BULL, timeframe="W1", lower=100.0, upper=110.0,
        formed_at=now_ms() - 10 * 86_400_000,
        confirmed_at=now_ms() - 5 * 86_400_000,
        status=ZoneStatus.ACTIVE, source="auto",
        source_candles=[now_ms() - 10 * 86_400_000], evidence={}, created_at=now_ms(),
    ))
    resp = client.get(f"/api/zones/{zid}", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["zone"]["confirmation_state"] == "fvg_confirmed"
