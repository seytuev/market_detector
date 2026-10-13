import asyncio
import json
import time
from dataclasses import asdict
from types import SimpleNamespace
import pytest
from app.config import Settings
from app.services.settings_jobs import SettingsJobs, SettingsConflict
from app.services.data_sync import DataSync
from app.services.maintenance import backup, retain


async def test_job_finishes_after_waiter_timeout(db, tmp_path):
    settings = Settings(db_path=str(tmp_path / "source.db"))
    worker = SimpleNamespace(db=db, _stop=asyncio.Event())
    sync = DataSync(worker)
    app = SimpleNamespace(state=SimpleNamespace(data_sync=sync))
    class Engine:
        def resync_structure_params(self):
            time.sleep(.1)
        def reclassify_active_entries(self):
            return {}
    def save(settings, candidate):
        (tmp_path / "settings.json").write_text(json.dumps(asdict(candidate)))
    jobs = SettingsJobs(app,db,settings,Engine(),save)
    job, task = await jobs.submit({"ltf_structure_left":4,"expected_config_version":0})
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task),.01)
    await task
    assert jobs.status()["status"] == "ready"
    assert jobs.status()["finished_at"]
    assert settings.detector.ltf_structure_left == 4 and jobs.version == 1
    with pytest.raises(SettingsConflict):
        await jobs.submit({"expected_config_version":0,"ltf_structure_left":5})


async def test_failed_recalculation_rolls_back_state_and_profile(db,tmp_path):
    settings = Settings(db_path=str(tmp_path / "source.db"))
    app = SimpleNamespace(state=SimpleNamespace())
    class Engine:
        def resync_structure_params(self):
            db.set_meta("partial_recalculation", "wrong")
            raise ValueError("injected failure")
    jobs = SettingsJobs(app,db,settings,Engine(),lambda *args: None)
    job, task = await jobs.submit({"ltf_structure_left":4})
    with pytest.raises(ValueError): await task
    assert db.get_meta("partial_recalculation") is None
    assert settings.detector.ltf_structure_left == 3
    assert jobs.version == 0 and jobs.status()["status"] == "failed"


def test_backup_and_retention_preserve_pending(db,instrument_id,tmp_path):
    row = db.spool_quote(instrument_id,source="ws",event_at=1000,received_at=1001,
        price=100,price_kind="trade",source_seq="1")
    db.fail_quote(row,"fault")
    backup(db,tmp_path / "copy")
    from app.db import Database
    restored = Database(str(tmp_path / "copy/htf_zones.db"))
    assert restored.load_spool(instrument_id)[0]["status"] == "retry"
    assert restored.conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    retain(restored,processed_days=0,job_days=0)
    assert restored.load_spool(instrument_id)
    restored.close()
