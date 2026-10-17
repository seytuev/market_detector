"""Forum routing, durable topic isolation and important-post delivery."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.config import DetectorConfig, Settings
from app.notify.navigation import reusable_panel, reusable_photo
from app.notify.outbox import Card, Outbox
from app.notify.telegram import LogSender, TelegramSender
from app.research.feed_classifier import FeedClassifier
from app.research.feed_notifications import FeedNotifications
from app.research.store import Store
from tests.test_feed_classifier import source


class TopicSender(LogSender):
    chat_id = '-1004352888499'
    topics = {'htf': 2, 'ltf': 2, 'alt': 3, 'alt_v3': 3, 'x_feed': 4, 'tg_feed': 6}

    def thread_for_channel(self, channel):
        return self.topics.get(channel)


def box_for(db):
    box = Outbox(db, TopicSender(), DetectorConfig())
    for channel in ('htf', 'ltf', 'alt', 'alt_v3', 'service'):
        box.register(channel, lambda row, members: None, lambda i, s: None)
    box.renderers['digest'] = box.render_digest
    return box


def test_topic_configuration_is_optional_and_strict(monkeypatch):
    monkeypatch.setenv('TELEGRAM_TRADING_THREAD_ID', '2')
    monkeypatch.setenv('TELEGRAM_SCREENER_THREAD_ID', '3')
    assert Settings().telegram_topic_ids['events'] == 2
    assert Settings().telegram_topic_ids['alt_v3'] == 3
    monkeypatch.setenv('TELEGRAM_TRADING_THREAD_ID', '')
    assert Settings().telegram_trading_thread_id is None
    for raw in ('0', '-1', 'bad'):
        monkeypatch.setenv('TELEGRAM_TRADING_THREAD_ID', raw)
        with pytest.raises(ValueError):
            Settings()


async def test_digest_is_partitioned_by_topic_and_not_blocked_by_other_topic(db):
    box = box_for(db)
    ids = [box.put(ch, ch, Card(ch), [i], quiet=True)
           for i, ch in enumerate(('htf', 'ltf', 'alt', 'service'), 1)]
    db.conn.execute('UPDATE notification_packet SET due_at=0')
    db.conn.commit()
    await box.flush()
    cards = {card.message_thread_id: card.text for card, _, _ in box.sender.cards}
    assert set(cards) == {None, 2, 3}
    assert 'htf' in cards[2] and 'ltf' in cards[2] and 'alt' not in cards[2]
    assert all(box.get(i)['status'] == 'sent' for i in ids)
    # A fourth topic must not wait for the other three digest intervals.
    box.register('x_feed', lambda r, m: None, lambda i, s: None)
    pid = box.put('x_feed', 'new-topic', Card('X'), [], quiet=True)
    db.conn.execute('UPDATE notification_packet SET due_at=0 WHERE id=?', (pid,))
    db.conn.commit()
    await box.flush()
    assert box.sender.cards[-1][0].message_thread_id == 4


async def test_topic_survives_renderer_restart_and_late_media(db, tmp_path):
    box = box_for(db)
    pid = box.put('htf', 'photo', Card('event', chart_pending=True), [1])
    await box.flush()
    assert box.sender.cards[0][0].message_thread_id == 2
    photo = tmp_path / 'chart.png'
    photo.write_bytes(b'fixture')

    async def render(row, members):
        return Card('updated', image_path=str(photo))

    restarted = box_for(db)
    restarted.sender.topics = {**TopicSender.topics, 'htf': 99}
    restarted.renderers['htf'] = render
    await restarted.retry_media()
    await restarted.flush()
    assert restarted.sender.cards[0][0].message_thread_id == 2


@pytest.mark.parametrize('channel,topic', [('htf', 2), ('ltf', 2), ('alt', 3), ('alt_v3', 3)])
@pytest.mark.parametrize('stored_topic', ['missing', None])
async def test_packets_from_unrouted_release_acquire_current_topic(db, channel, topic, stored_topic):
    box = box_for(db)
    pid = box.put(channel, 'legacy', Card('old queued event'), [1])
    card = {'text': 'old queued event'}
    if stored_topic is None:
        card['message_thread_id'] = None
    db.conn.execute('UPDATE notification_packet SET card=? WHERE id=?', (json.dumps(card), pid))
    db.conn.commit()
    await box.flush()
    assert box.sender.cards[0][0].message_thread_id == topic


async def test_unrouted_mixed_digest_is_split_before_sending(db):
    box = box_for(db)
    box.sender.topics = {}
    children = [box.put(ch, ch, Card(ch), [i], quiet=True)
                for i, ch in enumerate(('htf', 'ltf', 'alt_v3'), 1)]
    old_digest = box._create_digest([box.get(pid) for pid in children])
    box.sender.topics = TopicSender.topics
    db.conn.execute('UPDATE notification_packet SET due_at=0')
    db.conn.commit()
    await box.flush()
    assert box.get(old_digest)['status'] == 'suppressed'
    assert {card.message_thread_id for card, _, _ in box.sender.cards} == {2, 3}
    assert all(box.get(pid)['status'] == 'sent' for pid in children)


async def test_legacy_late_chart_inherits_parent_topic(db):
    box = box_for(db)
    pid = box.put('alt_v3', 'legacy', Card('screener'), [1])
    chart = box.put('chart', f'media:{pid}', Card('chart'), [])
    await box.flush()
    assert [card.message_thread_id for card, _, _ in box.sender.cards] == [3, 3]


async def test_sender_routes_text_and_photo_but_edits_need_no_thread(db, tmp_path):
    sender = TelegramSender('123:fixture', TopicSender.chat_id, db, topic_ids=TopicSender.topics)
    sender._bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=12)),
                                send_photo=AsyncMock(return_value=SimpleNamespace(message_id=13)),
                                edit_message_text=AsyncMock())
    assert await sender.send_card(Card('text', message_thread_id=2), 1) == 12
    assert sender._bot.send_message.call_args.kwargs['message_thread_id'] == 2
    path = tmp_path / 'photo.png'
    path.write_bytes(b'fixture')
    await sender.send_card(Card('photo', image_path=str(path), message_thread_id=3), 2)
    assert sender._bot.send_photo.call_args.kwargs['message_thread_id'] == 3
    await sender.edit_card(12, Card('updated', message_thread_id=2), 1)
    assert 'message_thread_id' not in sender._bot.edit_message_text.call_args.kwargs
    await sender.send_ltf('ltf')
    assert sender._bot.send_message.call_args.kwargs['message_thread_id'] == 2
    await sender.send_alt('alt')
    assert sender._bot.send_message.call_args.kwargs['message_thread_id'] == 3
    await sender.send_text('service')
    assert 'message_thread_id' not in sender._bot.send_message.call_args.kwargs


async def test_panels_and_charts_are_cached_per_topic(db, tmp_path):
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=[SimpleNamespace(message_id=10), SimpleNamespace(message_id=11)]),
                          edit_message_text=AsyncMock(), edit_message_media=AsyncMock())
    for thread in (2, 3, 2):
        await reusable_panel(db, bot, '42', 'same', 'text', message_thread_id=thread)
    assert bot.send_message.call_count == 2 and bot.edit_message_text.call_count == 1
    assert [c.kwargs['message_thread_id'] for c in bot.send_message.call_args_list] == [2, 3]
    photo = tmp_path / 'photo.png'
    photo.write_bytes(b'fixture')
    reply = AsyncMock(side_effect=[SimpleNamespace(message_id=20), SimpleNamespace(message_id=21)])
    for thread in (2, 3, 2):
        await reusable_photo(db, bot, '42', 'same', str(photo), 'text', None, reply, message_thread_id=thread)
    assert reply.call_count == 2 and bot.edit_message_media.call_count == 1


def feed_fixture(db):
    clock = [1_790_000_000_000]
    store = Store(db, clock=lambda: clock[0])
    cfg = Settings(telegram_token='123:fixture', telegram_chat_id=TopicSender.chat_id,
                   research_enabled=True, research_feed_notifications_enabled=True)
    box = box_for(db)
    feed = FeedNotifications(store, cfg, box)
    return store, cfg, box, feed, clock


def analyzed(store, external, kind='x', impact=4, confidence='medium', published=None):
    mid, rev = source(store, kind=kind, external=external)
    if published is not None:
        store.execute('UPDATE research_material_revision SET published_at=? WHERE revision_id=?', (published, rev))
    payload = {'impact': impact, 'confidence': confidence, 'reason_ru': 'Существенная новость <test>',
               'source_revision_id': rev}
    if kind == 'telegram':
        store.execute("INSERT INTO research_telegram_analysis VALUES(?,'succeeded',?,NULL,?)",
                      (rev, store.clock(), json.dumps({'analysis': {**payload, 'summary_ru': 'Важная новость'}})))
    else:
        store.execute("INSERT INTO research_result(id,kind,object_id,status,created_at,updated_at,payload) "
                      "VALUES(?,'feed_classification',?,'succeeded',?,?,?)",
                      (FeedClassifier.identifier(rev), rev, store.clock(), store.clock(), json.dumps(payload)))
    return mid, rev


async def test_only_new_important_analyzed_posts_go_to_correct_topics_once(db):
    s, cfg, box, feed, clock = feed_fixture(db)
    # A post predating enablement, including a late collector backfill, is skipped.
    analyzed(s, 'backfill', published=clock[0] - 1000)
    clock[0] += 1000
    analyzed(s, 'x-important')
    analyzed(s, 'tg-important', kind='telegram')
    analyzed(s, 'noise', impact=2)
    analyzed(s, 'uncertain', confidence='low')
    source(s, external='unanalyzed')
    feed.plan()
    await box.flush()
    assert len(box.sender.cards) == 2
    assert {c.message_thread_id for c, _, _ in box.sender.cards} == {4, 6}
    assert '<test>' in next(c.text for c, _, _ in box.sender.cards if c.message_thread_id == 4)
    # A restart must reuse the original boundary and the durable dedup keys.
    FeedNotifications(s, cfg, box).plan()
    await box.flush()
    assert len(box.sender.cards) == 2


async def test_stored_posts_and_changed_or_disabled_posts_are_not_sent(db):
    s, cfg, box, feed, clock = feed_fixture(db)
    clock[0] -= 1000
    analyzed(s, 'already-stored')
    clock[0] += 2000
    mid, rev = analyzed(s, 'new')
    feed.plan()
    s.execute("UPDATE research_material SET lifecycle='deleted' WHERE id=?", (mid,))
    await box.flush()
    assert box.sender.cards == []
    analyzed(s, 'disabled')
    feed.plan()
    cfg.research_feed_notifications_enabled = False
    await box.flush()
    assert box.sender.cards == []


@pytest.mark.parametrize('kind', ['x', 'telegram'])
def test_feed_queries_use_postgres_json_and_driver_parameters(kind):
    from psycopg._queries import PostgresQuery
    from psycopg.adapt import Transformer
    from app.postgres import translate
    queries = []

    def rows(query, args):
        converted = translate(query)
        PostgresQuery(Transformer()).convert(converted, args)
        assert 'json_extract' not in converted
        assert '#>>' in converted
        queries.append(converted)
        return []

    feed = object.__new__(FeedNotifications)
    feed.s = SimpleNamespace(db=SimpleNamespace(is_postgres=True), clock=lambda: 1000, rows=rows)
    feed.settings = SimpleNamespace(research_feed_notification_min_impact=4)
    feed.outbox = SimpleNamespace(destination='42')
    feed._rows(kind, 500)
    feed._rows(kind, 500, 'material')
    assert len(queries) == 2
