/* Журнал LevelFrame (этап 6, макет §6.D): единая хронология GET /api/journal.
   Фильтры Все / Рынок / Решения / Доставка. Доставка — статус без перехода.
   Рынок и решения открывают исходный объект (desk / проверка / ltf.html). */
'use strict';

window.LFJournal = (() => {
  const $ = (id) => document.getElementById(id);
  let kind = 'all';

  function clock(ms) {
    if (!ms) return { hm: '—', tz: '' };
    const hm = new Date(ms).toLocaleTimeString('ru-RU', {
      hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Europe/Moscow',
    });
    return { hm, tz: 'MSK' };
  }

  function dateLine() {
    const raw = new Date().toLocaleDateString('ru-RU', {
      day: 'numeric', month: 'long', year: 'numeric', timeZone: 'Europe/Moscow',
    }).replace(/\s*г\.?$/, '');
    return raw + ' · время MSK';
  }

  const STATUS_RU = {
    sent: ['sent', 'Доставлено'],
    failed: ['failed', 'Ошибка'],
    pending: ['pending', 'Ожидание'],
  };

  function canOpen(item) {
    if (item.category === 'delivery') return false;
    const ref = item.ref || {};
    if (item.source === 'ltf') return !!(item.instrument_id || ref.observation_id);
    return !!ref.zone_id;
  }

  function openItem(item) {
    const ref = item.ref || {};
    if (item.source === 'ltf') {
      const q = new URLSearchParams();
      if (item.instrument_id) q.set('instrument', String(item.instrument_id));
      if (ref.observation_id) q.set('obs', String(ref.observation_id));
      location.href = '/ltf.html?' + q.toString();
      return;
    }
    if (item.category === 'decisions' && ref.zone_id && window.LFReview) {
      window.LFReview.openZone(ref.zone_id).then((opened) => {
        if (!opened) openZoneDetail(ref.zone_id);
      });
      return;
    }
    if (ref.zone_id) openZoneDetail(ref.zone_id);
  }

  function rowHtml(item) {
    const t = clock(item.at);
    const text = item.text ? `<div class="journal-text">${HTF.esc(item.text)}</div>` : '';
    let side = '';
    if (item.category === 'delivery') {
      const pair = STATUS_RU[item.status] || ['pending', item.status || 'Доставка'];
      side = `<span class="journal-status ${pair[0]}"><span class="state-dot"></span>${HTF.esc(pair[1])}</span>`;
    } else if (canOpen(item)) {
      side = '<button type="button" class="btn journal-open">Открыть событие</button>';
    }
    return `<div class="journal-time">${HTF.esc(t.hm)}${t.tz ? `<small>${t.tz}</small>` : ''}</div>` +
      `<div class="journal-body"><div class="journal-title">${HTF.esc(item.title || '')}</div>${text}</div>` +
      `<div class="journal-side">${side}</div>`;
  }

  function render(items) {
    const list = $('journal-list');
    if (!list) return;
    if (!items.length) {
      list.innerHTML = '<p class="journal-empty">Записей пока нет.</p>';
      return;
    }
    list.innerHTML = '';
    for (const item of items) {
      const row = document.createElement('article');
      row.className = 'journal-row';
      row.innerHTML = rowHtml(item);
      const btn = row.querySelector('.journal-open');
      if (btn) btn.onclick = () => openItem(item);
      list.appendChild(row);
    }
  }

  async function load() {
    const sub = $('journal-sub');
    if (sub) sub.textContent = dateLine();
    const list = $('journal-list');
    if (!list) return;
    try {
      const items = await HTF.api('/api/journal?kind=' + encodeURIComponent(kind) + '&limit=100');
      render(items);
    } catch (err) {
      list.innerHTML = '<p class="journal-empty">Журнал не загрузился. ' + HTF.esc(err.message) + '</p>';
    }
  }

  function setKind(next) {
    kind = next;
    document.querySelectorAll('#journal-filters [data-kind]').forEach((btn) => {
      const on = btn.dataset.kind === kind;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    load();
  }

  document.querySelectorAll('#journal-filters [data-kind]').forEach((btn) => {
    btn.onclick = () => setKind(btn.dataset.kind);
  });

  if (typeof registerWsHandler === 'function') {
    registerWsHandler((data) => {
      const view = $('view-journal');
      if (!view || !view.classList.contains('active')) return;
      if (data && (data.type === 'event' || data.type === 'zone' || data.type === 'delivery')) load();
    });
  }

  const view = $('view-journal');
  if (view && view.classList.contains('active')) load();

  return { show: load };
})();
