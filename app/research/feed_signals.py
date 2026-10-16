"""Revision-bound feed classifications, shared by X and Telegram."""
from .contracts import ResearchError, choice, decode, digest
from .records import Records


class FeedSignals:
    def __init__(self, store):
        self.s = store
        self.r = Records(store)

    @staticmethod
    def identifier(material_id, revision_id):
        return digest(['feed_classification', material_id, revision_id])

    def attach(self, items):
        if not items:
            return items
        marks = ','.join('?' for _ in items)
        saved = self.s.rows('SELECT o.id,o.payload,a.display_symbol FROM research_object o '
                            'LEFT JOIN research_asset a ON a.asset_uid=o.asset_uid WHERE o.kind=? AND o.id IN (' + marks + ')',
                            ('feed_classification', *(self.identifier(i['id'], i['revision_id']) for i in items)))
        by_id = {r['id']: {**decode(r['payload']), 'symbol': r['display_symbol']} for r in saved}
        automatic = self.s.rows('SELECT object_id,status,updated_at,payload FROM research_result '
            'WHERE kind=\'feed_classification\' AND object_id IN (' + marks + ')', tuple(i['revision_id'] for i in items))
        auto_by_revision = {r['object_id']: r for r in automatic}
        symbols = {t.get('symbol', '').upper() for i in items for t in
                   (i.get('telegram', {}).get('analysis') or {}).get('tickers', []) if t.get('symbol')}
        symbol_assets = self.s.rows('SELECT asset_uid,display_symbol FROM research_asset WHERE status!=\'archived\' '
                                    'AND upper(display_symbol) IN (' + ','.join('?' for _ in symbols) + ')',
                                    tuple(symbols)) if symbols else []
        linked = self.s.rows('SELECT m.revision_id,a.asset_uid,a.display_symbol FROM research_material_asset m '
                             'JOIN research_asset a ON a.asset_uid=m.asset_uid WHERE m.resolution IN (\'manual\',\'verified\') '
                             'AND m.role!=\'beneficiary_hypothesis\' AND m.revision_id IN (' + marks + ')',
                             tuple(i['revision_id'] for i in items))
        for item in items:
            manual = by_id.get(self.identifier(item['id'], item['revision_id']))
            auto = auto_by_revision.get(item['revision_id'])
            auto_payload = decode(auto['payload']) if auto else {}
            analysis = item.get('telegram', {}).get('analysis') or {}
            candidates = list(dict.fromkeys(t['symbol'] for t in analysis.get('tickers', []) if t.get('symbol')))
            assets = [{'asset_uid': r['asset_uid'], 'symbol': r['display_symbol']} for r in linked
                      if r['revision_id'] == item['revision_id']]
            direction = analysis.get('sentiment')
            if direction not in {'bullish', 'bearish'}:
                direction = None
            if manual:
                item['classification'] = {**manual, 'origin': 'manual',
                                          'assets': assets, 'candidate_symbols': candidates}
            elif auto and auto['status'] == 'succeeded':
                item['classification'] = {**auto_payload, 'assets': assets,
                    'candidate_symbols': [auto_payload['symbol']] if auto_payload.get('symbol') else []}
            else:
                main = [t['symbol'] for t in analysis.get('tickers', []) if t.get('role') == 'главный']
                primary_symbol = main[0] if len(main) == 1 else candidates[0] if len(candidates) == 1 else None
                matches = [r for r in symbol_assets if r['display_symbol'].upper() == (primary_symbol or '').upper()]
                primary = assets[0] if len(assets) == 1 else (
                    {'asset_uid': matches[0]['asset_uid'], 'symbol': matches[0]['display_symbol']}
                    if not assets and len(matches) == 1 else {})
                # A symbol alone is a candidate, never an unambiguous asset identity.
                item['classification'] = {'direction': direction, 'origin': 'model' if direction else 'pending',
                                          'primary_asset_uid': primary.get('asset_uid'),
                                          'symbol': primary.get('symbol'),
                                          'assets': assets, 'candidate_symbols': candidates}
            item['classification'].update({'auto_status': auto['status'] if auto else 'pending',
                'auto_error': auto_payload.get('error_code'), 'auto_updated_at': auto['updated_at'] if auto else None})
        return items

    def save(self, material_id, body):
        if self.s.db.is_postgres:
            self.s.execute('SELECT pg_advisory_xact_lock(?)',
                           (int(digest(['feed_classification', material_id])[:15], 16),))
        item = self.s.material(material_id)
        if item.get('lifecycle') != 'active' or item['source_kind'] not in {'telegram', 'x'}:
            raise ResearchError('material_not_found', 'Публикация недоступна', 404)
        if body.get('source_revision_id') != item['revision_id']:
            raise ResearchError('revision_conflict', 'Публикация изменилась. Обновите ленту и проверьте классификацию.', 409)
        direction = choice(body.get('direction'), {'bullish', 'bearish'}, 'direction')
        asset = self.s.require_asset(body.get('primary_asset_uid'))
        identifier = self.identifier(material_id, item['revision_id'])
        old = self.s.one('SELECT current_revision_id FROM research_object WHERE id=?', (identifier,))
        if old and body.get('expected_revision') != old['current_revision_id']:
            raise ResearchError('revision_conflict', 'Классификация изменена. Обновите ленту.', 409)
        value = {'title': 'Классификация ' + asset['display_symbol'], 'status': 'classified',
                 'primary_asset_uid': asset['asset_uid'], 'direction': direction,
                 'material_id': material_id, 'source_revision_id': item['revision_id']}
        return self.r.save('feed_classification', value, identifier if old else None,
                           expected=body.get('expected_revision'), new_id=identifier,
                           reason='Уточнены тикер и направление публикации')
