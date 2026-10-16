"""Provider backoff and independent classification lane, without real API calls."""
import asyncio
import threading
from email.utils import formatdate
import httpx
import pytest
from app.config import Settings
from app.models import now_ms
from app.research.contracts import ResearchError, encode
from app.research.jobs import ResearchRunner
from app.research.model import Model
from app.research.store import Store

SPEC = {'prompt': 'Return JSON', 'schema': {'name': 'test', 'schema': {}}}
CONFIG = {'provider': 'xai', 'key': 'fixture-key', 'cheap': 'fixture-grok', 'strong': 'fixture-grok'}


@pytest.mark.parametrize('retry', ['seconds', 'date', 'missing'])
def test_real_429_sets_shared_short_cooldown_and_prevents_repeat_http(db, retry):
    clock = [now_ms() // 1000 * 1000]
    s = Store(db, clock=lambda: clock[0]); calls = []
    headers = {'Retry-After': '3'} if retry == 'seconds' else {'Retry-After': formatdate(clock[0]/1000+3, usegmt=True)} if retry == 'date' else {}
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers=headers, json={'error': 'rate limit'})
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        first = Model(s, CONFIG, client)
        with pytest.raises(ResearchError) as error:
            first.call('feed_classification', {}, spec=SPEC)
        wait = 3 if retry != 'missing' else 2
        assert error.value.retry_after == wait
        assert error.value.code == 'model_rate_limited'
        assert first.status()['last_request']['http_status'] == 429
        second = Model(s, CONFIG, client)
        with pytest.raises(ResearchError):
            second.call('telegram_analysis', {}, spec=SPEC)
        assert len(calls) == 1
        assert s.one('SELECT COUNT(*) AS n FROM research_model_usage')['n'] == 1
        clock[0] += wait * 1000
        assert second.call('feed_classification', {}, spec=SPEC)['output'] == {}
        assert len(calls) == 2
        assert second.status()['rate_limit']['retry_after'] == 0


def test_429_without_header_backs_off_in_seconds_not_fifteen_minutes(db):
    clock = [now_ms()];s = Store(db, clock=lambda: clock[0]);calls = []
    def respond(request):
        calls.append(request);return httpx.Response(429)
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        model = Model(s, CONFIG, client)
        for expected in [2, 4, 8, 16, 32, 60, 60]:
            with pytest.raises(ResearchError) as error:
                model.call('feed_classification', {}, spec=SPEC)
            assert error.value.retry_after == expected
            clock[0] += expected * 1000
        assert len(calls) == 7


def cfg():
    return Settings(auth_token='fixture', research_enabled=True, research_deribit_enabled=False,
        research_telegram_analysis_enabled=False, research_telegram_digest_enabled=False)


def test_rate_limit_is_deferred_until_provider_deadline_and_survives_restart(db, monkeypatch):
    runner = ResearchRunner(db, cfg());now = runner.s.clock()
    job = runner.enqueue('feed_classification', {'material_id': 'post', 'revision_id': 'rev'}, 'rev')
    class Fake:
        def __init__(self, *args): pass
        def maintenance(self, runner): pass
        def classify(self, *args):
            error = ResearchError('model_rate_limited', 'provider', 429)
            error.rate_limit_source = 'provider';error.retry_after = 7
            raise error
    monkeypatch.setattr('app.research.feed_classifier.FeedClassifier', Fake)
    result = runner.step()
    assert result['status'] == 'pending' and result['attempts'] == 0
    row = runner.s.one('SELECT next_attempt_at,payload FROM research_job WHERE id=?', (job['id'],))
    assert now + 7000 <= row['next_attempt_at'] < now + 8000
    assert 'provider' in row['payload']
    restarted = ResearchRunner(db, cfg())
    restarted.step()
    assert restarted.s.one('SELECT next_attempt_at FROM research_job WHERE id=?', (job['id'],))['next_attempt_at'] == row['next_attempt_at']


@pytest.mark.asyncio
async def test_classification_finishes_while_slow_source_is_still_running(db, monkeypatch):
    runner = ResearchRunner(db, cfg())
    now = runner.s.clock()
    runner.last_schedule = runner.last_calendar = runner.last_workbench = now
    slow = runner.enqueue('collect')
    fast = runner.enqueue('feed_classification', {'material_id': 'post', 'revision_id': 'rev'}, 'rev')
    started, finished, classified = threading.Event(), threading.Event(), threading.Event()
    def collect(*args):
        started.set();assert finished.wait(5)
    class Fake:
        def __init__(self, *args): pass
        def maintenance(self, runner): pass
        def classify(self, *args):
            assert started.wait(2)
            classified.set();return {'status': 'succeeded'}
    monkeypatch.setattr('app.research.jobs.collect_deribit', collect)
    monkeypatch.setattr('app.research.feed_classifier.FeedClassifier', Fake)
    stop = asyncio.Event();task = asyncio.create_task(runner.run(stop))
    try:
        assert await asyncio.to_thread(classified.wait, 3)
        assert runner.job(slow['id'])['status'] == 'running'
        # The other lane is independently leased while the source call is blocked.
        assert runner.job(fast['id'])['status'] in {'running', 'succeeded'}
    finally:
        finished.set();stop.set();await asyncio.wait_for(task, 5)
    assert runner.job(fast['id'])['status'] == 'succeeded'


def test_fast_lane_prioritizes_new_posts_but_services_old_backlog(db, monkeypatch):
    runner = ResearchRunner(db, cfg());runner.lane = 'classification';seen = []
    class Fake:
        def __init__(self, *args): pass
        def maintenance(self, runner): pass
        def classify(self, mid, rev):
            seen.append(mid);return {'status': 'succeeded'}
    monkeypatch.setattr('app.research.feed_classifier.FeedClassifier', Fake)
    for index in range(6):
        job = runner.enqueue('feed_classification', {'material_id': str(index), 'revision_id': str(index)}, str(index))
        runner.s.execute('UPDATE research_job SET created_at=? WHERE id=?', (index, job['id']))
    for _ in range(4):runner.step()
    assert seen == ['5', '4', '3', '0']


def test_lanes_do_not_run_duplicate_telegram_request(db, monkeypatch):
    runner = ResearchRunner(db, cfg());runner.lane = 'general'
    fake = lambda *args: type('Classifier', (), {'maintenance': lambda *args: None})()
    monkeypatch.setattr('app.research.feed_classifier.FeedClassifier', fake)
    source = runner.enqueue('telegram_analysis', {'material_id': 'p', 'revision_id': 'r'}, 'r')
    classification = runner.enqueue('feed_classification', {'material_id': 'p', 'revision_id': 'r'}, 'r')
    now = runner.s.clock()
    runner.last_schedule = runner.last_calendar = runner.last_workbench = now
    assert runner.step() is None
    runner.s.execute("UPDATE research_job SET status='running',lease_until=? WHERE id=?", (now+60000, source['id']))
    runner.lane = 'classification'
    assert runner.step() is None
