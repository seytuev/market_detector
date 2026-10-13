"""Full-history search and navigation identity regressions for the redesign."""
import json
from tests.test_journal import db, settings, client, seeded, AUTH, BASE
from app.events.schema import apply_events_schema
from app.models import Event, EventKind


def page(client, **params):
    result = client.get('/api/journal/page', params=params, headers=AUTH)
    assert result.status_code == 200, result.text
    return result.json()


def test_source_identity_and_equal_time_pagination(seeded, db):
    # Across categories and sources, ids and times may be identical.
    db.conn.execute('UPDATE event SET occurred_at=?', (BASE,))
    db.conn.execute('UPDATE ltf_event SET occurred_at=?', (BASE,))
    db.conn.execute('UPDATE review SET created_at=?', (BASE,))
    db.conn.execute('UPDATE ltf_review SET created_at=?', (BASE,))
    ids, cursor = [], ''
    while True:
        result = page(seeded, limit=1, cursor=cursor)
        ids.extend(item['uid'] for item in result['items'])
        if not result['next_cursor']:
            break
        cursor = result['next_cursor']
    assert len(ids) == len(set(ids)) == 6
    assert 'ltf:market:1' in ids and 'htf:market:1' in ids


def test_search_precedes_limit_and_is_unicode(seeded, db):
    db.conn.execute("UPDATE ltf_review SET text='Ручная сверка завершена'")
    for i in range(600):
        db.insert_event(Event(None, 1, 1, EventKind.TOUCH, BASE + 100_000 + i,
                              BASE + 100_000 + i, 105.0))
    result = page(seeded, q='СВЕРКА', limit=1)
    assert result['total'] == 1
    assert result['items'][0]['source'] == 'ltf'
    assert result['items'][0]['category'] == 'decisions'


def test_context_publications_and_delivery_are_separate(seeded, db):
    apply_events_schema(db.conn)
    db.conn.execute("INSERT INTO events_publication(pub_key,bundle_id,kind,assessment_id,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                    ('pub1', 1, 'changes', 'a1', json.dumps({'text': 'Оценка изменилась'}), BASE))
    db.conn.execute("INSERT INTO events_delivery(bundle_id,recipient,channel,status,created_at) VALUES (1,'owner','telegram','unavailable',?)", (BASE,))
    market = page(seeded, scope='context', kind='market')
    assert market['total'] == 1
    assert market['items'][0]['ref']['assessment_id'] == 'a1'
    delivered = page(seeded, scope='context', kind='delivery')
    assert delivered['items'][0]['status'] == 'unavailable'


def test_msk_date_boundary_and_bad_filters(seeded, db):
    from datetime import datetime
    boundary = int(datetime.fromisoformat('2026-10-13T00:00:00+03:00').timestamp() * 1000)
    db.conn.execute('UPDATE event SET occurred_at=?', (boundary,))
    assert page(seeded, scope='htf', kind='market', from_date='2026-10-13', to_date='2026-10-13')['total'] == 1
    assert page(seeded, scope='htf', kind='market', to_date='2026-10-12')['total'] == 0
    for params in ({'cursor':'broken'}, {'from_date':'2026-10-14','to_date':'2026-10-12'}, {'scope':'bad'}):
        assert seeded.get('/api/journal/page',params=params,headers=AUTH).status_code == 400
