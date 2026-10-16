"""Rich, isolated feed workspace for visual and interaction verification."""
from tests.telegram_fixture_server import app, store, settings
from tests.test_telegram_feed import post
from app.research.feed_signals import FeedSignals
from app.research.ideas import Ideas

settings.research_ideas_enabled = True
assets = {}
for symbol, name in [('BTC', 'Bitcoin'), ('SOL', 'Solana'), ('ETH', 'Ethereum')]:
    assets[symbol] = store.create_asset({'display_symbol': symbol, 'name': name,
        'external_ids': [{'namespace': 'feed-fixture', 'external_id': symbol}]})

signals = FeedSignals(store)
rows = store.rows('SELECT id,current_revision_id FROM research_material')
for index, row in enumerate(rows):
    signals.save(row['id'], {'source_revision_id': row['current_revision_id'],
        'primary_asset_uid': assets['BTC']['asset_uid'], 'direction': 'bearish' if index == 1 else 'bullish'})

for external, symbol, title, original, direction in [
    ('solana', 'SOL', 'Мероприятие Solana в Шанхае отменили', 'Организаторы отменили мероприятие из-за проблем с площадкой. Новая дата не объявлена. Важно проверить реакцию сообщества и следующие анонсы.', 'bearish'),
    ('ethereum', 'ETH', 'Ethereum: новый этап масштабирования сети', 'Команда опубликовала план обновления и запуска новых инструментов для разработчиков. Проверяем сроки, документацию и фактическую активность сети.', 'bullish')]:
    store.ingest('feed-fixture', {'entity_kind': 'materials', 'entity_id': external, 'revision_id': 'v1',
        'operation': 'upsert', 'change_seq': 1, 'payload': {'source_kind': 'x', 'source_id': external,
        'author': external, 'title': title, 'original_text': original, 'canonical_url': 'https://example.test/' + external}})
    row = store.one('SELECT id,current_revision_id FROM research_material WHERE external_id=?', (external,))
    signals.save(row['id'], {'source_revision_id': row['current_revision_id'],
        'primary_asset_uid': assets[symbol]['asset_uid'], 'direction': direction})

Ideas(store).save({'title': 'ETH · активность сети после обновления', 'primary_asset_uid': assets['ETH']['asset_uid'],
    'direction': 'positive', 'status': 'watching', 'thesis': 'Обновление сети может повысить активность разработчиков и использование приложений.',
    'mechanism': 'Новые инструменты снижают барьеры для использования сети.', 'horizon_label': '1–2 недели',
    'review_condition': 'После публикации результатов обновления',
    'criteria': [{'type': 'invalidation', 'description': 'Обновление отменено или перенесено на неопределённый срок.'}]})
