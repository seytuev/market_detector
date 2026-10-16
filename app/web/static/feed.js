'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = path => HTF.api('/api/research/v1' + path);
  const date = value => value ? new Date(value).toLocaleString('ru-RU', {timeZone:'Europe/Moscow'}) + ' МСК' : '—';
  let items = [], more = false, busy = false, selected = null;
  let stars;
  try { stars = new Set(JSON.parse(localStorage.getItem('stv:feed:stars') || '[]')); } catch { stars = new Set(); }
  const kind = item => item.source_kind === 'telegram' ? 'Telegram' : 'X';
  function render() {
    const q = $('feed-search').value.trim().toLowerCase(), filter = $('feed-kind').value;
    const shown = items.filter(item => (!filter || (filter === 'x' ? kind(item) === 'X' : kind(item) === 'Telegram')) &&
      (!$('feed-stars').checked || stars.has(item.id)) && [item.title,item.original_text,item.author,item.source_id].join(' ').toLowerCase().includes(q));
    $('feed-count').textContent = `Показано ${shown.length} · Загружено ${items.length}`;
    $('feed-list').innerHTML = shown.map(item => `<article class="research-card feed-post${selected === item.id ? ' selected' : ''}"><div class="feed-post-head"><span class="research-badge">${kind(item)}</span><span>${esc(item.author || item.source_id || 'Источник')}</span><button class="btn small" data-star="${esc(item.id)}" aria-label="Избранное" aria-pressed="${stars.has(item.id)}">${stars.has(item.id) ? '★' : '☆'}</button></div><button class="feed-open" data-open="${esc(item.id)}">${esc(item.title || (item.original_text || 'Публикация').slice(0,160))}</button><p class="research-muted">${esc(date(item.published_at))}</p></article>`).join('') || '<div class="research-card"><h2>Публикаций пока нет</h2><p class="research-muted">Проверьте фильтры и подключённые источники.</p></div>';
    $('feed-more').hidden = !more;
    $('feed-list').querySelectorAll('[data-star]').forEach(button => button.onclick = () => {
      const id = button.dataset.star; stars.has(id) ? stars.delete(id) : stars.add(id);
      try { localStorage.setItem('stv:feed:stars', JSON.stringify([...stars])); } catch { $('feed-status').textContent = 'Избранное сохранено только до закрытия страницы.'; }
      render();
    });
    $('feed-list').querySelectorAll('[data-open]').forEach(button => button.onclick = () => {
      const item = items.find(i => i.id === button.dataset.open); selected = item.id;
      const url = /^https?:\/\//i.test(item.canonical_url || '') ? `<a class="btn small" href="${esc(item.canonical_url)}" target="_blank" rel="noopener noreferrer">Открыть оригинал ↗</a>` : '';
      $('feed-detail').innerHTML = `<span class="research-badge">${kind(item)}</span><h2>${esc(item.title || item.author || 'Публикация')}</h2><p class="research-muted">${esc(item.author || item.source_id)} · ${esc(date(item.published_at))}</p><div class="feed-text">${esc(item.original_text || 'Текст отсутствует в сохранённом материале.')}</div>${url}<button class="btn small" id="feed-analysis">Разбор и версии</button>`;
      $('feed-analysis').onclick = () => ResearchUI.material(item.id).catch(error => { $('feed-status').textContent = error.message; });
      render();
    });
  }
  async function load(append = false) {
    if (busy) return; busy = true;
    $('feed-refresh').disabled = $('feed-more').disabled = true;
    $('feed-status').textContent = 'Загружаем публикации…';
    try {
      await HTF.ensureToken();
      const last = append && items.at(-1);
      const response = await api('/feed?limit=50' + (last ? `&before=${last.published_at}&before_id=${encodeURIComponent(last.id)}` : ''));
      items = append ? [...items, ...response.data.items] : response.data.items;
      more = response.data.has_more; render();
      $('feed-status').textContent = 'Лента обновлена. Избранное хранится в этом браузере.';
      const [config, channels] = await Promise.all([api('/config'),api('/channels')]);
      const terminal = config.data.sources.terminal;
      $('feed-sources').textContent = `Импорт STVTerminal: ${terminal.last_success_at ? date(terminal.last_success_at) : 'нет успешных обновлений'} · Telegram: ${channels.data.items.filter(c => c.enabled).length} каналов включено из ${channels.data.items.length}`;
    } catch (error) {
      $('feed-status').textContent = error.message || 'Не удалось загрузить ленту.';
      $('feed-sources').textContent = 'Состояние источников недоступно. Проверьте настройки исследований.';
    } finally { busy = false; $('feed-refresh').disabled = $('feed-more').disabled = false; }
  }
  $('feed-refresh').onclick = () => load(); $('feed-more').onclick = () => load(true);
  ['feed-kind','feed-stars'].forEach(id => $(id).onchange = render);
  $('feed-search').oninput = render;
  load();
})();
