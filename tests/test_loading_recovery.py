import asyncio
import json

from app.services.asset_state import _quality
from app.services.data_sync import read_json, JOB_KEY
from app.services.runtime import recovery_progress
from tests.test_data_sync import market


async def test_quote_checkpoint_does_not_wait_for_newer_ticks(market):
    db, worker, _, _, instruments, _ = market
    ins = instruments[0]
    first = db.spool_quote(ins.id, source='rest', event_at=1, received_at=1,
                           price=100, price_kind='trade', source_seq='first')
    checkpoint = worker.market_queue.checkpoint(ins.id)
    second = db.spool_quote(ins.id, source='ws', event_at=2, received_at=2,
                            price=101, price_kind='trade', source_seq='second')
    assert checkpoint == first and second > first
    assert not await worker.market_queue.wait_through(ins.id, checkpoint, .01)
    db.delete_spool([first])
    assert await worker.market_queue.wait_through(ins.id, checkpoint, .01)
    assert db.spool_counts()[ins.id] == 1


async def test_manual_refresh_during_restore_finishes_with_source_data(market, monkeypatch):
    db, worker, exchange, _, instruments, _ = market
    worker._restoring_instruments.update(i.id for i in instruments)
    worker.data_sync.quote_processing_timeout_seconds = .01
    # Restore owns every asset; the manual job must not wait for these locks.
    async with worker.data_sync.instrument(instruments[0].id):
        worker.data_sync.start()
        await asyncio.wait_for(worker.data_sync._task, 3)
    job = read_json(db, JOB_KEY)
    assert job['status'] == 'partial'
    assert job['channels_completed'] == job['channels_total']
    assert not job['processing_continues']
    assert all(s['finished_at'] for s in job['stages'])
    for ins in instruments:
        assert db.get_quote(ins.id)
        assert len(db.get_candles(ins.id, 'H1')) == 7
    assert any(e['error'] == 'processing_lag' for e in job['errors'])


async def test_recovery_progress_is_visible_only_while_owned(market):
    db, worker, _, _, instruments, _ = market
    ins = instruments[0]
    progress = dict(phase='calculation', processed=400, total=1000, through=123)
    db.set_meta(f'runtime:recovery:{ins.id}', json.dumps(progress))
    assert recovery_progress(db, ins.id) == []
    worker._set_replaying(ins.id, True)
    assert recovery_progress(db, ins.id)[0]['processed'] == 400
    bad, message = _quality(dict(state='replaying', reason='replay_in_progress',
                                channels={'replay': {'progress': progress}}))
    assert bad and '400' in message and '1 000' in message and 'расчёт LTF' in message
    worker._set_replaying(ins.id, False)
    assert recovery_progress(db, ins.id) == []

def test_historical_leg_replay_keeps_published_head(db, instrument_id):
    from app.services.h1_setup import H1LegCursor
    from app.config import DetectorConfig
    from tests.conftest import make_h1_candles
    candles = make_h1_candles([(100, 101, 99, 100)] * 8, 1_790_000_000_000, instrument_id)
    head = candles[-1].close_time
    db.set_meta(f'h1:leg:scanned:{instrument_id}', str(head))
    db.set_meta(f'h1:main_structure:{instrument_id}', '{"head":true}')
    H1LegCursor(instrument_id, ordered_candles=True).advance(
        db, [], candles[:3], candles[2].close_time, DetectorConfig())
    assert db.get_meta(f'h1:leg:scanned:{instrument_id}') == str(head)
    assert json.loads(db.get_meta(f'h1:main_structure:{instrument_id}')) == {'head': True}
