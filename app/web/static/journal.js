/* Журнал LevelFrame (этап 6, макет §6.D): единая хронология GET /api/journal.
   Фильтры Все / Рынок / Решения / Доставка. Доставка — статус без перехода.
   Рынок и решения открывают исходный объект (desk / проверка / ltf.html). */
'use strict';

window.LFJournal = (() => {
  const $ = (id) => document.getElementById(id);
  let kind = 'all';
  let loaded = [];
  let renderedSigs = new Set();
  let held = null;
  const GROUP_MS = 60000;

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

  function refBits(item) {
    const ref = item.ref || {};
    return [
      item.instrument_id ?? '',
      item.category || '',
      item.kind || '',
      item.status || '',
      item.title || '',
      item.text || '',
      ref.zone_id || '',
      ref.scenario_id || '',
      ref.observation_id || '',
    ].join('\u001f');
  }

  function sig(item) {
    const ref = item.ref || {};
    return [item.at, refBits(item), ref.event_id || '', ref.review_id || '', ref.delivery_id || ''].join(':');
  }

  function refLabel(item) {
    const ref = item.ref || {};
    const id = ref.event_id || ref.review_id || ref.delivery_id || ref.zone_id || ref.observation_id;
    return id ? `ID ${id}` : 'без id';
  }

  function passes(item) {
    const instrument = ($('journal-instrument') || {}).value || '';
    const query = (($('journal-query') || {}).value || '').trim().toLowerCase();
    const from = ($('journal-from') || {}).value || '';
    const to = ($('journal-to') || {}).value || '';
    if (instrument && (item.symbol || '') !== instrument) return false;
    if (query) {
      const hay = `${item.title || ''} ${item.text || ''}`.toLowerCase();
      if (!hay.includes(query)) return false;
    }
    if ((from || to) && item.at) {
      const day = new Date(item.at).toLocaleDateString('en-CA', { timeZone: 'Europe/Moscow' });
      if (from && day < from) return false;
      if (to && day > to) return false;
    }
    return true;
  }

  function groupsOf(items) {
    const groups = [];
    items.forEach((item) => {
      const prev = groups[groups.length - 1];
      const near = prev && Math.abs(prev.items[0].at - item.at) <= GROUP_MS
        && prev.items.every((row) => Math.abs(row.at - item.at) <= GROUP_MS);
      if (prev && near && prev.key === refBits(item)) prev.items.push(item);
      else groups.push({ key: refBits(item), items: [item] });
    });
    return groups;
  }

  function bindOpen(root, item) {
    const btn = root.querySelector('.journal-open');
    if (btn) btn.onclick = () => openItem(item);
  }

  function fillInstruments(items) {
    const select = $('journal-instrument');
    if (!select) return;
    const current = select.value;
    const symbols = [...new Set(items.map((item) => item.symbol).filter(Boolean))].sort();
    select.innerHTML = '<option value="">Все</option>' + symbols.map(
      (symbol) => `<option value="${HTF.esc(symbol)}">${HTF.esc(symbol)}</option>`
    ).join('');
    if (symbols.includes(current)) select.value = current;
  }

  function paint(items) {
    const list = $('journal-list');
    if (!list) return;
    const page = document.querySelector('#view-journal .journal-page');
    const top = page ? page.scrollTop : 0;
    const visible = items.filter(passes);
    if (!items.length) {
      list.innerHTML = '<p class="journal-empty">Записей пока нет.</p>';
      renderedSigs = new Set();
      return;
    }
    if (!visible.length) {
      list.innerHTML = '<p class="journal-empty">По фильтру среди загруженных записей ничего нет.</p>';
      renderedSigs = new Set(items.map(sig));
      return;
    }
    list.innerHTML = '';
    groupsOf(visible).forEach((group) => {
      if (group.items.length === 1) {
        const row = document.createElement('article');
        row.className = 'journal-row';
        row.innerHTML = rowHtml(group.items[0]);
        bindOpen(row, group.items[0]);
        list.appendChild(row);
        return;
      }
      const details = document.createElement('details');
      details.className = 'journal-group';
      const newest = group.items[0];
      const summary = document.createElement('summary');
      summary.className = 'journal-row';
      summary.innerHTML = rowHtml(newest).replace(
        'journal-side">',
        `journal-side"><span class="journal-count">${group.items.length} записей</span>`
      );
      details.appendChild(summary);
      group.items.forEach((item) => {
        const row = document.createElement('article');
        row.className = 'journal-row journal-origin';
        row.innerHTML = rowHtml(item) + `<div class="journal-id">${HTF.esc(refLabel(item))}</div>`;
        bindOpen(row, item);
        details.appendChild(row);
      });
      list.appendChild(details);
    });
    if (page && top > 48) page.scrollTop = top;
    renderedSigs = new Set(items.map(sig));
  }

  function render(items) {
    const page = document.querySelector('#view-journal .journal-page');
    const away = page && page.scrollTop > 80 && renderedSigs.size;
    const fresh = items.filter((item) => !renderedSigs.has(sig(item)));
    const banner = $('journal-new');
    if (away && fresh.length) {
      held = items;
      if (banner) {
        banner.classList.remove('hidden');
        banner.textContent = `Новые записи: ${fresh.length}`;
      }
      return;
    }
    if (banner) banner.classList.add('hidden');
    held = null;
    fillInstruments(items);
    paint(items);
  }

  async function load() {
    const sub = $('journal-sub');
    if (sub) sub.textContent = dateLine();
    const list = $('journal-list');
    if (!list) return;
    try {
      loaded = await HTF.api('/api/journal?kind=' + encodeURIComponent(kind) + '&limit=100');
      render(loaded || []);
    } catch (err) {
      list.innerHTML = '<p class="journal-empty">Журнал не загрузился. ' + HTF.esc(err.message) + '</p>';
    }
  }

  function setKind(next) {
    kind = next;
    renderedSigs = new Set();
    held = null;
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
  ['journal-instrument', 'journal-from', 'journal-to', 'journal-query'].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener('input', () => paint(loaded));
  });
  const newer = $('journal-new');
  if (newer) {
    newer.onclick = () => {
      const page = document.querySelector('#view-journal .journal-page');
      if (page) page.scrollTop = 0;
      renderedSigs = new Set();
      if (held) render(held);
    };
  }

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
