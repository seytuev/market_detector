"""Resolve detected feed tickers against existing assets or public spot metadata."""
import re
import httpx
from .contracts import ResearchError, decode, digest
from .store import local_id
from .feed_signals import FeedSignals


class FeedAssets:
    def __init__(self, store, client=None):
        self.s, self.client = store, client

    def resolve(self, material_id, revision_id):
        item = self.s.material(material_id)
        if item['revision_id'] != revision_id:
            raise ResearchError('revision_conflict', 'Публикация изменилась. Обновите ленту.', 409)
        if item.get('lifecycle') != 'active' or item['source_kind'] not in {'x', 'telegram'}:
            raise ResearchError('material_not_found', 'Публикация недоступна', 404)
        # Read analysis without instantiating a provider or making a model request.
        row = self.s.one("SELECT payload FROM research_telegram_analysis WHERE revision_id=? AND status='succeeded'", (revision_id,))
        if row:
            item['telegram'] = decode(row['payload'])
        FeedSignals(self.s).attach([item])
        classification = item['classification']
        if classification.get('primary_asset_uid'):
            return self.s.asset(classification['primary_asset_uid'])
        symbol = classification.get('symbol')
        if not symbol and len(classification.get('candidate_symbols', [])) == 1:
            symbol = classification['candidate_symbols'][0]
        if not isinstance(symbol, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-]{0,29}', symbol):
            raise ResearchError('ticker_required', 'Сначала определите основной тикер публикации.')
        return self.ensure(symbol.upper())

    def ensure(self, symbol):
        matches = self.s.rows("SELECT asset_uid FROM research_asset WHERE upper(display_symbol)=? AND status='active'", (symbol,))
        if len(matches) == 1:
            return self.s.asset(matches[0]['asset_uid'])
        if matches:
            raise ResearchError('ambiguous_asset', 'Этот тикер есть у нескольких активов. Выберите нужный вручную.', 409)
        key = 'feed-asset-lookup:' + symbol
        state = self.s.state(key)
        if state.get('retry_at', 0) > self.s.clock():
            raise ResearchError('asset_lookup_unavailable', 'Не удалось найти актив в биржевом справочнике. Выберите его вручную.', 503)
        owns = self.client is None
        client = self.client or httpx.Client(timeout=5, follow_redirects=False)
        try:
            response = client.get('https://data-api.binance.vision/api/v3/exchangeInfo', params={'symbol': symbol + 'USDT'})
            response.raise_for_status()
            rows = response.json().get('symbols', [])
            market = next((r for r in rows if r.get('symbol') == symbol + 'USDT' and r.get('baseAsset') == symbol
                and r.get('quoteAsset') == 'USDT' and r.get('status') == 'TRADING' and r.get('isSpotTradingAllowed') is True), None)
            if not market:
                raise ResearchError('asset_lookup_unavailable', 'Актив не найден в биржевом справочнике. Выберите его вручную.', 422)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, ResearchError):
            with self.s.db.batch_writes():
                self.s.set_state(key, {'retry_at': self.s.clock() + 300000})
            raise ResearchError('asset_lookup_unavailable', 'Не удалось подтвердить актив по биржевому справочнику. Выберите его вручную.', 503) from None
        finally:
            if owns:
                client.close()
        # Canonical exchange identity; no technical instrument/watch subscription is created.
        identifier = local_id('binance:spot', 'asset', symbol)
        with self.s.db.batch_writes():
            if self.s.db.is_postgres:
                self.s.execute('SELECT pg_advisory_xact_lock(?)', (int(digest(key)[:15], 16),))
            matches = self.s.rows("SELECT asset_uid FROM research_asset WHERE upper(display_symbol)=? AND status='active'", (symbol,))
            if len(matches) == 1:
                return self.s.asset(matches[0]['asset_uid'])
            if matches:
                raise ResearchError('ambiguous_asset', 'Выберите нужный актив вручную.', 409)
            return self.s.create_asset({'display_symbol': symbol, 'name': symbol, 'asset_class': 'crypto',
                'external_ids': [{'namespace': 'binance:spot', 'external_id': market['symbol'],
                    'source': 'exchangeInfo', 'base_asset': symbol, 'quote_asset': 'USDT'}]},
                asset_uid=identifier, actor='feed-asset-resolution')
