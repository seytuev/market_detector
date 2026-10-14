"""Versioned structure upgrade. Old rows remain as historical evidence."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from ..engine.ltf.market_structure import RULE_VERSION, replay_structure
from ..models_ltf import RULE_VERSION_LTF


def legacy_scenarios(db, instrument_id):
    return set(json.loads(db.get_meta(f"structure:legacy_scenarios:{instrument_id}") or "[]"))


def prepare_upgrade(db, cfg, instrument_id, as_of):
    key = f"structure:rules:{instrument_id}"
    if db.get_meta(key) == RULE_VERSION:
        return False
    old = db.conn.execute(
        "SELECT 1 FROM ltf_pivot p JOIN calc_version v ON v.id=p.calc_version_id "
        "WHERE p.instrument_id=? AND v.kind='ltf_structure' AND v.rule_version<>? LIMIT 1",
        (instrument_id, RULE_VERSION_LTF)).fetchone()
    if old is None:
        db.set_meta(key, RULE_VERSION)
        return False
    candles = db.get_candles(instrument_id, "H1", end_ms=as_of)
    if not candles:
        return False
    # Keep the old scenario/event/notification journal. It must not suppress
    # a new calculation's historical events or keep an obsolete entry active.
    legacy_key = f"structure:legacy_scenarios:{instrument_id}"
    recorded = db.get_meta(legacy_key)
    if recorded is not None:
        return True
    if db.path != ":memory:" and not db.is_postgres:
        source_path = Path(db.path).resolve()
        backup = source_path.parent / "backups" / f"{source_path.stem}-before-{RULE_VERSION}.db"
        if not backup.exists():
            backup.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True) as source:
                with sqlite3.connect(str(backup)) as destination:
                    source.backup(destination)
    # One publication instead of an autocommit/fsync for every historical row.
    with db.batch_writes(join_repository_writes=True):
        scenarios = db.list_ltf_scenarios_for_instrument(instrument_id)
        for scenario in scenarios:
            db.conn.execute("UPDATE ltf_event SET delayed=1 WHERE scenario_id=? AND delivered=0", (scenario.id,))
            if scenario.state in ("range_pending", "monitoring_entries"):
                db.update_ltf_scenario(scenario.id, state="cancelled",
                                       cancellation_reason="structure_rules_changed",
                                       cancelled_at=as_of, updated_at=as_of)
        for observation in db.list_ltf_observations(instrument_id=instrument_id):
            if observation.state == "active":
                db.update_ltf_observation(observation.id, state="waiting_structure", updated_at=as_of)
        from ..engine.ltf.structure import sync_structure
        from .h1_setup import H1LegCursor
        if candles:
            now = candles[-1].close_time
            sync = sync_structure(db, cfg, instrument_id, candles, now)
            _, events = replay_structure(sync.avail, candles, now)
            valid = {f"{e.level_key}@{e.occurred_at}" for e in events}
            for row in db.conn.execute("SELECT id,trigger_bos_key,bos_at FROM h1_local_leg WHERE instrument_id=?",
                                       (instrument_id,)).fetchall():
                if row["trigger_bos_key"] not in valid:
                    db.conn.execute("UPDATE h1_local_leg SET state='superseded',superseded_at=bos_at WHERE id=?", (row["id"],))
            H1LegCursor(instrument_id).advance(db, sync.avail, candles, now, cfg)
        # The engine records the version only after replay succeeds. A failed
        # replay therefore retries on the next tick without sending old signals.
        db.set_meta(legacy_key, json.dumps([s.id for s in scenarios]))
        db._commit()
    return True
