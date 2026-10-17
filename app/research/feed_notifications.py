"""Important, analyzed X/TG posts delivered through the durable shared outbox."""
from __future__ import annotations

from .contracts import decode, safe_url
from ..notify.formatting import fmt_time_msk
from ..notify.outbox import Card


class FeedNotifications:
    def __init__(self, store, settings, outbox):
        self.s, self.settings, self.outbox = store, settings, outbox
        for channel in ('x_feed', 'tg_feed'):
            outbox.register(channel, self.validate, lambda identifier, status: None)
        # Establish the boundary before collectors run; never flood the chat
        # with stored posts or old posts discovered during source backfills.
        self._boundaries()

    def _enabled(self, channel):
        return (self.settings.research_enabled and self.settings.research_feed_notifications_enabled
                and bool(self.settings.telegram_token and self.settings.telegram_chat_id)
                and self.outbox.thread_for_channel(channel) is not None)

    def _boundaries(self):
        result = {}
        for channel in ('x_feed', 'tg_feed'):
            if not self._enabled(channel):
                continue
            key = f'feed-notifications:{self.outbox.destination}:{channel}:{self.outbox.thread_for_channel(channel)}'
            with self.s.db.batch_writes():
                state = self.s.state(key)
                if 'since' not in state:
                    stamp = self.s.clock()
                    self.s.execute('INSERT INTO research_source_state(id,updated_at,payload) VALUES(?,?,?) '
                                   'ON CONFLICT(id) DO NOTHING', (key, stamp, '{"since":' + str(stamp) + '}'))
                    state = self.s.state(key)
            result[channel] = state['since']
        return result

    def _rows(self, kind, since, material_id=None):
        # Full Telegram analysis already has impact, including neutral news.
        # X uses the same impact scale in its classification request.
        def field(column, nested, name):
            if self.s.db.is_postgres:
                path = '{analysis,' + name + '}' if nested else '{' + name + '}'
                return f"({column}::jsonb #>> '{path}')"
            path = '$.analysis.' if nested else '$.'
            return f"json_extract({column},'{path}{name}')"

        analysis, status = 'c.payload', 'c.status'
        impact, confidence = field('c.payload', False, 'impact'), field('c.payload', False, 'confidence')
        if kind == 'telegram':
            analysis = "CASE WHEN t.status='succeeded' THEN t.payload ELSE c.payload END"
            status = "CASE WHEN t.status='succeeded' THEN t.status ELSE c.status END"
            impact = f"CASE WHEN t.status='succeeded' THEN {field('t.payload', True, 'impact')} ELSE {impact} END"
            confidence = f"CASE WHEN t.status='succeeded' THEN {field('t.payload', True, 'confidence')} ELSE {confidence} END"
        return self.s.rows(
            "SELECT m.id,r.revision_id,r.source_kind,r.published_at,r.payload," + analysis + " AS analysis_payload "
            "FROM research_material m JOIN research_material_revision r ON r.revision_id=m.current_revision_id "
            "LEFT JOIN research_result c ON c.kind='feed_classification' AND c.object_id=r.revision_id "
            "LEFT JOIN research_telegram_analysis t ON t.revision_id=r.revision_id "
            "WHERE r.source_kind=? AND m.lifecycle='active' AND r.lifecycle='active' "
            "AND m.first_available_at>=? AND COALESCE(r.published_at,m.first_available_at)>=? "
            "AND r.available_at<=? AND COALESCE(r.published_at,m.first_available_at)<=? "
            "AND (" + status + ")='succeeded' AND CAST((" + impact + ") AS INTEGER)>=? "
            "AND (" + confidence + ") IN ('medium','high') "
            + ("AND m.id=? " if material_id else
               "AND NOT EXISTS (SELECT 1 FROM notification_packet p WHERE p.destination=? "
               "AND p.channel=? AND p.semantic_key=r.revision_id) ")
            + "ORDER BY r.available_at,r.revision_id LIMIT 100",
            (kind, since, since, self.s.clock(), self.s.clock(),
             max(4, min(5, self.settings.research_feed_notification_min_impact)),
             *((material_id,) if material_id else (self.outbox.destination, 'tg_feed' if kind == 'telegram' else 'x_feed'))))

    @staticmethod
    def _analysis(row):
        value = decode(row['analysis_payload'])
        return value.get('analysis', value)

    def plan(self):
        for channel, since in self._boundaries().items():
            kind = 'telegram' if channel == 'tg_feed' else 'x'
            for row in self._rows(kind, since):
                source, analysis = decode(row['payload']), self._analysis(row)
                author = str(source.get('author') or source.get('source_id') or kind)
                title = analysis.get('title_ru') or source.get('title') or 'Важная публикация'
                summary = analysis.get('summary_ru') or analysis.get('reason_ru') or ''
                relevance = analysis.get('why_matters_ru') or ''
                # Card text is plain; TelegramSender escapes it exactly once.
                lines = [f"📰 {'TG Feed' if kind == 'telegram' else 'X Feed'} · {author[:150]}",
                         str(title)[:200], str(summary)[:1600]]
                if relevance:
                    lines.append(str(relevance)[:700])
                lines.append(f"Значимость: {analysis['impact']}/5 · оценка модели")
                if row['published_at']:
                    lines.append('Публикация: ' + fmt_time_msk(row['published_at']))
                url = safe_url(source.get('canonical_url'))
                if url:
                    lines.append('Оригинал: ' + url)
                details = '\n\n'.join([str(title), str(summary), str(relevance),
                                       str(source.get('original_text') or '')[:12000], url])
                card = Card('\n\n'.join(line for line in lines if line), details,
                            targets=[{'kind': 'feed_post', 'label': 'Оригинал публикации', 'url': url}])
                # The revision and topic are frozen by the shared outbox.
                self.outbox.put(channel, row['revision_id'], card, [])

    def validate(self, packet, members):
        channel = packet['channel']
        if not self._enabled(channel):
            return 'Рассылка ленты выключена'
        kind = 'telegram' if channel == 'tg_feed' else 'x'
        row = self.s.one('SELECT material_id FROM research_material_revision WHERE revision_id=?',
                         (packet['semantic_key'],))
        if not row:
            return 'Публикация недоступна'
        since = self._boundaries()[channel]
        rows = self._rows(kind, since, row['material_id'])
        if not rows or rows[0]['revision_id'] != packet['semantic_key']:
            return 'Публикация изменена, удалена или больше не проходит фильтр'
        return None
