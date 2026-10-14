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
  let cursor = null;
  let request = 0;
  let filterTimer = null;
  const GROUP_MS = 60000;
  const FILTER_FIELDS = {symbol:'journal-instrument',from_date:'journal-from',to_date:'journal-to',q:'journal-query',scope:'journal-scope-filter',event_type:'journal-type-filter'};

  function queryParams() {
    const query = new URLSearchParams({kind,limit:'50'});
    Object.entries(FILTER_FIELDS).forEach(([key,id]) => { if ($(id)?.value) query.set(key,$(id).value); });
    return query;
  }
  function writeFilters() {
    const url = new URL(location.href), query = queryParams();
    ['kind',...Object.keys(FILTER_FIELDS)].forEach(key => {
      if (query.has(key)) url.searchParams.set(key,query.get(key)); else url.searchParams.delete(key);
    });
    history.replaceState(null,'',url.pathname+url.search+url.hash);
    document.dispatchEvent(new Event('lf-filters-changed'));
  }

  function clock(ms) {
    if (!ms) return { hm: '—', tz: '' };
    const hm = new Date(ms).toLocaleTimeString('ru-RU', {
      hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'Europe/Moscow',
    });
    return { hm, tz: new Date(ms).toLocaleDateString('ru-RU', {timeZone: 'Europe/Moscow'}) + ' МСК' };
  }

  function dateLine() {
    const raw = new Date().toLocaleDateString('ru-RU', {
      day: 'numeric', month: 'long', year: 'numeric', timeZone: 'Europe/Moscow',
    }).replace(/\s*г\.?$/, '');
    return raw + ' · время МСК';
  }

  const STATUS_RU = {
    sent: ['sent', 'Отправлено'],
    failed: ['failed', 'Не удалось отправить'],
    pending: ['pending', 'Ожидает отправки'],
    suppressed: ['pending', 'Отправка подавлена'],
    skipped: ['pending', 'Отправка пропущена'],
    unavailable: ['failed', 'Канал недоступен'],
    disabled: ['pending', 'Канал отключён'],
    channel_off: ['pending', 'Канал отключён'],
  };

  function canOpen(item) {
    if (item.source === 'alt') return !!item.ref?.setup_id;
    if (item.source === 'context') return !!item.ref?.assessment_id;
    if (item.category === 'delivery') return false;
    const ref = item.ref || {};
    if (item.source === 'ltf') return !!(item.instrument_id || ref.observation_id);
    return !!ref.zone_id;
  }

  function openItem(item) {
    const ref = item.ref || {};
    LFUI.closeDrawer();
    if (item.source === 'alt' && ref.setup_id) {
      location.href = `/alt.html?view=analysis&setup=${encodeURIComponent(ref.setup_id)}&event=${encodeURIComponent(ref.event_id)}&at=${item.at}`;
      return;
    }
    if (item.source === 'context' && ref.assessment_id) {
      location.href = '/events.html?assessment=' + encodeURIComponent(ref.assessment_id);
      return;
    }
    if (item.source === 'ltf') {
      if (item.instrument_id) LFDesk.openInstrument(item.instrument_id, {mode:'h1', eventId:ref.event_id, at:item.at});
      return;
    }
    if (item.category === 'decisions' && ref.zone_id && window.LFReview) {
      window.LFReview.openZone(ref.zone_id).then((opened) => {
        if (!opened) openZoneDetail(ref.zone_id);
      });
      return;
    }
    if (ref.zone_id && item.instrument_id) LFDesk.openInstrument(item.instrument_id, {mode:'context',zoneId:ref.zone_id,at:item.at});
    else if (ref.zone_id) openZoneDetail(ref.zone_id);
  }

  function showDetails(item) {
    const ref = item.ref || {};
    LFUI.openDrawer('Событие', `<h3>${HTF.esc(LFUI.readable(item.title))}</h3><p>${HTF.esc(HTF.fmtTime(item.at))}</p>` +
      (item.text ? `<p>${HTF.esc(LFUI.readable(item.text))}</p>` : '') +
      `<dl class="lf-facts"><dt>Область</dt><dd>${HTF.esc({htf:'Зоны D1/W1',ltf:'Структура H1',alt:'Скринер',context:'Рыночный фон'}[item.source] || '—')}</dd>` +
      `<dt>ID</dt><dd>${HTF.esc(LFUI.eventId(item))}</dd>${ref.zone_id ? `<dt>Зона</dt><dd>${ref.zone_id}</dd>` : ''}` +
      `${item.status ? `<dt>Отправка</dt><dd>${HTF.esc((STATUS_RU[item.status] || ['', 'Состояние уточняется'])[1])}</dd>` : ''}</dl>` +
      `${canOpen(item) ? '<button type="button" class="btn primary" id="journal-chart">Открыть связанный объект</button>' : ''}`,
      root => root.querySelector('#journal-chart')?.addEventListener('click', () => openItem(item)));
  }

  function rowHtml(item) {
    const t = clock(item.at);
    const text = item.text ? `<div class="journal-text">${HTF.esc(item.text)}</div>` : '';
    let side = '';
    if (item.category === 'delivery') {
      const pair = STATUS_RU[item.status] || ['pending', item.status || 'Доставка'];
      side = `<span class="journal-status ${pair[0]}"><span class="state-dot"></span>${HTF.esc(pair[1])}</span>`;
    }
    side += '<button type="button" class="btn journal-open">Подробнее</button>';
    return `<div class="journal-time">${HTF.esc(t.hm)}${t.tz ? `<small>${t.tz}</small>` : ''}</div>` +
      `<div class="journal-body"><div class="journal-title">${HTF.esc(LFUI.readable(item.title))}</div>${text}</div>` +
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

  function stableId(item) {
    return LFUI.eventId(item);
  }

  function dedupe(items) {
    const seen = new Set();
    const out = [];
    items.forEach((item) => {
      const id = stableId(item);
      if (!id) {
        out.push(item);
        return;
      }
      if (seen.has(id)) return;
      seen.add(id);
      out.push(item);
    });
    return out;
  }

  function groupsOf(items) {
    return LFUI.groupEvents(items);
  }

  function bindOpen(root, item) {
    const btn = root.querySelector('.journal-open');
    if (btn) btn.onclick = () => showDetails(item);
  }

  function fillInstruments(items) {
    const select = $('journal-instrument');
    if (!select) return;
    const current = select.value;
    const symbols = [...new Set(items.map((item) => item.symbol).filter(Boolean).concat(current || []))].sort();
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
    const visible = items;
    if (!items.length) {
      list.innerHTML = '<p class="journal-empty">Записей пока нет.</p>';
      renderedSigs = new Set();
      return;
    }
    if (!visible.length) {
      list.innerHTML = '<p class="journal-empty">Нет событий по выбранным фильтрам.</p>';
      renderedSigs = new Set(items.map(sig));
      return;
    }
    list.innerHTML = '';
    groupsOf(dedupe(visible)).forEach((group) => {
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
      const redundant = summary.querySelector('button');
      if (redundant) redundant.remove();
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
    paint(items);
  }

  async function load(append = false) {
    const sub = $('journal-sub');
    if (sub) sub.textContent = dateLine();
    const list = $('journal-list');
    if (!list) return;
    const seq = ++request;
    const query = queryParams();
    if (append && cursor) query.set('cursor',cursor);
    list.setAttribute('aria-busy','true');
    $('journal-more').disabled = true;
    try {
      const data = await HTF.api('/api/journal/page?' + query);
      if (seq !== request) return;
      loaded = LFUI.uniqueEvents(append ? loaded.concat(data.items) : data.items);
      cursor = data.next_cursor;
      if (append) paint(loaded); else render(loaded);
      fillInstruments(data.symbols.map(symbol => ({symbol})));
      const select = $('journal-type-filter'), current = select.value;
      select.innerHTML = '<option value="">Все типы</option>' + data.types.map(type => `<option value="${HTF.esc(type.kind)}">${HTF.esc(LFUI.readable(type.label))}</option>`).join('');
      select.value = current;
      $('journal-scope').textContent = `По всей истории · найдено ${data.total} · загружено ${loaded.length}`;
      $('journal-more').classList.toggle('hidden',!cursor);
    } catch (err) {
      if (seq === request) LFUI.error(err,'Журнал не загрузился',() => load(append));
    } finally {
      if (seq === request) { list.setAttribute('aria-busy','false'); $('journal-more').disabled = false; }
    }
  }

  function applyKindFromUrl() {
    let stored = '';
    try {
      stored = sessionStorage.getItem('lf:journalKind') || '';
      if (stored) sessionStorage.removeItem('lf:journalKind');
    } catch (err) { stored = ''; }
    const query = new URLSearchParams(location.search).get('kind') || '';
    const pick = stored || query;
    if (['all', 'market', 'decisions', 'delivery'].includes(pick)) kind = pick;
    const params = new URLSearchParams(location.search);
    Object.entries(FILTER_FIELDS).forEach(([key,id]) => {
      const el = $(id), value = params.get(key) || '';
      if (el.tagName === 'SELECT' && value && ![...el.options].some(o => o.value === value)) el.add(new Option(value,value));
      el.value = value;
    });
    document.dispatchEvent(new Event('lf-filters-changed'));
    document.querySelectorAll('#journal-filters [data-kind]').forEach((btn) => {
      const on = btn.dataset.kind === kind;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
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
    writeFilters();
    load();
  }

  document.querySelectorAll('#journal-filters [data-kind]').forEach((btn) => {
    btn.onclick = () => setKind(btn.dataset.kind);
  });
  Object.values(FILTER_FIELDS).forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener('input', () => { clearTimeout(filterTimer); writeFilters(); renderedSigs = new Set(); held = null; filterTimer = setTimeout(() => load(),250); });
  });
  $('journal-more').onclick = () => load(true);
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

  return {
    show() {
      applyKindFromUrl();
      return load();
    },
    details: showDetails,
    open(filters = {}) {
      Object.entries(filters).forEach(([key,value]) => {
        if (key === 'kind') kind = value;
        else if (FILTER_FIELDS[key]) { const el = $(FILTER_FIELDS[key]);
          if (el.tagName === 'SELECT' && value && ![...el.options].some(o => o.value === value)) el.add(new Option(value,value));
          el.value = value;
        }
      });
      writeFilters(); showView('journal');
    },
  };
})();
