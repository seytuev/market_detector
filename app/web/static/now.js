/* Экран «Сейчас» (LevelFrame, этап 3 ребрендинга; макет §6.A плана).
   Обзор активов поверх read model /api/ltf/instruments + /current и очереди
   /api/candidates. Подключается после app.js: интеграция — через
   window.LFNow.show() из showView(), мост window.LFDesk.openInstrument() и
   подписку registerWsHandler() (оба объявлены в app.js). */

'use strict';

(() => {
  const $ = (id) => document.getElementById(id);
  const api = (path) => HTF.api(path);
  const esc = HTF.esc;
  const fmtPrice = HTF.fmtPrice;

  const INSTRUMENT_KEY = 'htf:instrument';
  // счётчик «На проверку» — та же выборка, что у очереди «Проверка» (app.js)
  const HTF_TFS = new Set(['D1', 'W1']);

  // Зеркалит серверный ATTENTION_ORDER (app/services/overview.py, L06)
  const ATTENTION_ORDER = ['review', 'price_in_zone', 'eligible', 'awaiting', 'data_problem', 'none'];
  const DATA_STATE_REASON_RU = {
    no_quote: 'нет котировки',
    no_h1_candles: 'нет свечей H1',
    current_candle_pending: 'Ожидаем данные текущей свечи H1',
    quote_stale: 'котировка устарела',
    h1_stale: 'свечи H1 устарели',
    source_stale: 'источник недоступен',
    replay_in_progress: 'идёт догрузка и пересчёт',
    processing_lag: 'расчёт отстаёт',
    history_gap: 'разрыв истории',
  };
  // Флага «наблюдение включено» в /api/ltf/instruments нет (список и так
  // содержит только наблюдаемые активы), поэтому «Наблюдаю» — активы под
  // наблюдением без требуемого действия, «Нужно внимание» — с действием или
  // проблемой данных
  const FILTERS = {
    all: () => true,
    attention: (r) => !!r.review_state?.needed || r.attention === 'price_in_zone' ||
      !!(r.data_state?.state && r.data_state.state !== 'ok' && r.data_state.reason !== 'current_candle_pending'),
    watch: () => true,
    zone: (r) => r.attention === 'price_in_zone' || r.stage === 'Цена в HTF-зоне' || r.stage === 'Цена в Entry Zone',
    eligible: (r) => !!r.scenario_summary?.confirmed,
  };

  const st = {
    rows: [],            // строки /api/ltf/instruments
    candidates: 0,       // HTF-кандидаты на проверку
    currents: new Map(), // instrument_id -> снимок /current
    prices: new Map(),   // instrument_id -> {price, at} из WS (новее снимка)
    versions: new Map(), // доменные версии снимка (§13) — рефетч по их росту
    filter: 'all',
    selectedId: null,
    reqSeq: 0,           // поздний ответ старого запроса экран не перезаписывает
    cardSeq: 0,          // поздний снимок чужой строки карточку не перезаписывает
    cardLoading: false,
    refreshTimer: null,
    ageTimer: null,
  };

  function isActive() {
    const v = $('view-now');
    return v && v.classList.contains('active');
  }

  function rank(r) {
    const i = ATTENTION_ORDER.indexOf(r.attention || 'none');
    return i === -1 ? ATTENTION_ORDER.length : i;
  }

  function ageText(ms) {
    if (!ms) return '—';
    const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return `${s} с назад`;
    const m = Math.round(s / 60);
    if (m < 60) return `${m} мин назад`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h} ч назад`;
    return `${Math.round(h / 24)} дн назад`;
  }

  function priceOf(iid) {
    const ws = st.prices.get(iid);
    if (ws && ws.price) return ws;
    const cur = st.currents.get(iid);
    if (cur && cur.price != null) return { price: cur.price, at: cur.quote_at };
    const row = st.rows.find((r) => r.instrument.id === iid);
    if (row && row.price != null) return { price: row.price, at: row.quote_at };
    return null;
  }

  function baseQuote(symbol) {
    return symbol.endsWith('USDT')
      ? [symbol.slice(0, -4), 'USDT']
      : [symbol, ''];
  }

  // ------------------------------------------------------------------ render

  function renderSubline() {
    const now = new Date();
    const date = now.toLocaleDateString('ru-RU',
      { day: 'numeric', month: 'long', timeZone: 'Europe/Moscow' });
    const time = now.toLocaleTimeString('ru-RU',
      { hour: '2-digit', minute: '2-digit', timeZone: 'Europe/Moscow' });
    $('now-subline').textContent = `${date} · ${time} МСК · ваш список наблюдения`;
  }

  function renderStats() {
    const attention = st.rows.filter(FILTERS.attention).length;
    const inZone = st.rows.filter(FILTERS.zone).length;
    const eligible = st.rows.filter(FILTERS.eligible).length;
    $('now-stat-review').textContent = attention;
    $('now-stat-review-note').textContent = attention ? 'Фильтр списка' : 'Нет таких активов';
    $('now-stat-zone').textContent = inZone;
    $('now-stat-eligible').textContent = eligible;
    $('now-assets-count').textContent = visibleRows().length;
  }

  function scenarioHtml(r) {
    const titled = r.asset && r.asset.asset_state && r.asset.asset_state.title;
    const main = r.scenario_summary?.label || titled || r.market_stage || r.stage || 'Сценарий не определён';
    const review = (r.review_state && r.review_state.needed)
      ? `<div class="state-sub"><a href="#review" data-view="review">Нужна проверка · ${r.review_state.count}</a></div>`
      : '';
    const direction = {bull:'Рост',bear:'Снижение'}[r.scenario_summary?.direction];
    return esc(main) + (direction ? `<div class="state-sub">${direction}</div>` : '') + review;
  }

  function zoneRelationHtml(r) {
    if (r.attention === 'price_in_zone') return 'Цена в зоне';
    const ds = r.data_state || {};
    if (r.presentation?.location?.text) return esc(LFUI.readable(r.presentation.location.text));
    const why = ds.reason ? LFUI.dataLabel(ds) : 'Нет данных о зоне';
    return `Не рассчитано<div class="state-sub">${esc(why)}</div>`;
  }

  function dataHtml(r) {
    const ds = r.data_state || {};
    if (!ds.state) return 'Ещё не проверены';
    if (ds.state === 'ok') {
      return '<span class="state-dot dot-positive"></span>Актуальны';
    }
    if (ds.reason === 'current_candle_pending') {
      return '<span class="state-dot dot-muted"></span>' + esc(LFUI.dataLabel(ds));
    }
    const word = ds.state === 'stale' ? 'Устарели' : 'Неполные';
    const why = DATA_STATE_REASON_RU[ds.reason] || ds.reason || '';
    return `<span class="state-dot dot-warning"></span>${word}` +
      (why ? `<div class="state-sub">${esc(why)}</div>` : '');
  }

  function rowHtml(r) {
    const ins = r.instrument;
    const [base, quote] = baseQuote(ins.symbol || '');
    const p = priceOf(ins.id);
    const when = p && p.at ? ageText(p.at) : '';
    return `<tr data-iid="${ins.id}" tabindex="0">` +
      `<td data-label="Актив / площадка">` +
      `<button type="button" class="asset-open" data-iid="${ins.id}">${esc(base)}${quote ? ' / ' + esc(quote) : ''}</button>` +
      `<div class="asset-sub">${esc(ins.venue)} · ${esc(ins.market_type)}</div></td>` +
      `<td data-label="Сценарий">${scenarioHtml(r)}</td>` +
      `<td data-label="Цена и зона">${zoneRelationHtml(r)}</td>` +
      `<td data-label="Данные">${dataHtml(r)}</td>` +
      `<td data-label="Цена, USDT" class="num">` +
      `<span class="price-val">${p ? esc(fmtPrice(p.price)) : '—'}</span>` +
      `<div class="price-age" title="${p && p.at ? esc(new Date(p.at).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })) : ''}">${esc(when)}</div></td>` +
      `<td data-label="Наблюдение">` +
      `<button type="button" class="btn small asset-off" aria-pressed="true" data-off="${ins.id}">Наблюдать</button></td></tr>`;
  }

  function visibleRows() {
    const fn = FILTERS[st.filter] || FILTERS.all;
    return st.rows.filter(fn).sort((a, b) => rank(a) - rank(b));
  }

  function renderTable() {
    const rows = visibleRows();
    $('now-tbody').innerHTML = rows.map(rowHtml).join('');
    const empty = $('now-empty');
    if (!st.rows.length) {
      empty.classList.remove('hidden');
      empty.innerHTML = 'Вы пока не наблюдаете за активами. <a href="/alt.html">Найти в скринере</a>';
    } else if (!rows.length) {
      empty.classList.remove('hidden');
      empty.innerHTML = 'Нет активов. <button type="button" id="now-filter-reset">Сбросить фильтр</button>';
    } else {
      empty.classList.add('hidden');
      empty.textContent = '';
    }
  }

  // ------------------------------------------------------------- карточка

  function headline(v) {
    if (v.market_stage) return v.market_stage;
    if (v.wait && v.wait.message && !v.selected_context_id) return v.wait.message;
    const ds = v.data_state || {};
    if (ds.state && ds.state !== 'ok') return 'Данные задерживаются';
    return v.stage || 'Активного контекста нет';
  }

  function leadText(v, r, tf) {
    const disabled = (v.reached_disabled || [])[0];
    if (disabled && disabled.message) return disabled.message;
    if (v.wait && v.wait.message && !v.selected_context_id) return v.wait.message;
    const ds = v.data_state || {};
    if (ds.state && ds.state !== 'ok') {
      return (DATA_STATE_REASON_RU[ds.reason] || 'Источник данных недоступен') +
        '. Показаны последние известные значения.';
    }
    const sc = v.current_scenario;
    if (sc && sc.break_level != null) {
      const side = sc.direction === 'bear' ? 'ниже' : 'выше';
      return `H1 закрылся ${side} ${fmtPrice(sc.break_level)}. Старший контекст ${tf} остаётся активным.`;
    }
    if (v.market_stage) return v.market_stage;
    return v.stage ? v.stage + '.' : '';
  }

  function waitText(v) {
    const sc = v.current_scenario;
    if (!sc) {
      if (v.scenario_waiting) {
        return 'Ждём подтверждённого слома структуры H1 — сценарий откроется после BOS/SMS.';
      }
      return 'Активного сценария нет';
    }
    if (!v.range) return 'Ждём подтверждения опор диапазона.';
    const n = (v.counts && v.counts.eligible) || 0;
    if (n > 0) {
      if (v.stage === 'Цена в Entry Zone') {
        return 'Цена уже в подходящей зоне — сценарий в точке входа.';
      }
      const e0 = (v.eligible_entries || [])[0];
      const rangeTxt = e0 ? ` к ${fmtPrice(e0.lower)}–${fmtPrice(e0.upper)}` : '';
      return `Ждём возврат цены${rangeTxt}.`;
    }
    return 'Подходящих зон сейчас нет — сценарий активен.';
  }

  function cancelText(v) {
    const c = v.cancel_condition;
    if (c && c.level != null && c.status && c.status !== 'undefined') {
      const side = c.side === 'above' ? 'выше' : 'ниже';
      const verb = c.status === 'occurred' ? 'Отмена произошла' : 'Отменит';
      return `${verb}: закрытие H1 строго ${side} ${fmtPrice(c.level)}`;
    }
    const sc = v.current_scenario;
    if (sc && sc.reverse_break && sc.reverse_break.price != null) {
      const side = sc.direction === 'bear' ? 'выше' : 'ниже';
      return `Закрытие H1 ${side} ${fmtPrice(sc.reverse_break.price)}`;
    }
    return null;
  }

  function factsHtml(v) {
    const facts = (v && v.liquidity_facts) || [];
    if (!facts.length) return '';
    return facts.slice(0, 3).map((f) =>
      `<div class="desk-fact">${esc(String(f.type || 'уровень').toUpperCase())} ${esc(f.timeframe || '')} ` +
      `${fmtPrice(f.level)} снят · не сценарий</div>`).join('');
  }

  function reviewBadge(v) {
    const rs = v && v.review_state;
    if (!rs || !rs.needed) return '';
    return `<a class="review-badge" href="#review">Нужна проверка · ${rs.count}</a>`;
  }

  function dirBadge(d) {
    if (d === 'bull') return '<span class="dir-badge bull">↑ Рост</span>';
    if (d === 'bear') return '<span class="dir-badge bear">↓ Снижение</span>';
    if (d === 'mixed') return '<span class="dir-badge">▲▼ Разные контексты</span>';
    return '';
  }

  function renderCard() {
    const el = $('now-card');
    const r = st.rows.find((row) => row.instrument.id === st.selectedId);
    if (!r) {
      el.innerHTML = '<p class="now-empty">Выберите актив в таблице.</p>';
      return;
    }
    const ins = r.instrument;
    const v = st.currents.get(ins.id);
    const tf = (r.htf_context && r.htf_context.timeframe) || '—';
    const [base, quote] = baseQuote(ins.symbol || '');
    const pair = quote ? `${base} / ${quote}` : (base || '—');
    if (!v) {
      const head = `<div class="now-card-head"><span>${esc(pair)} · ${esc(tf)}</span></div>`;
      if (st.cardLoading) {
        el.innerHTML = head +
          '<h3>Читаем карточку</h3>' +
          '<p class="now-empty">Снимок выбранного актива ещё загружается.</p>';
        return;
      }
      el.innerHTML = head +
        '<h3>Ошибка чтения снимка</h3>' +
        '<p class="now-empty">Карточка не скрыта: снимок актива не прочитан.</p>';
      return;
    }
    const p = priceOf(ins.id);
    const pres = v.presentation;
    const snapshotQuote = pres && pres.location ? pres.location.quote : null;
    const suppress = !!(p && snapshotQuote != null && Number(p.price) !== Number(snapshotQuote));
    if (pres && window.LFCopy) {
      const governed = pres.asset && pres.asset.governs;
      const arrow = v.direction === 'bull' ? '↑' : (v.direction === 'bear' ? '↓' : '');
      el.innerHTML = LFCopy.card(pres, {
        suppressLocation: suppress,
        visualHtml: window.LFCopy.visual(pres, { price: p ? p.price : null }),
        extraHtml:
          `${!governed && arrow ? `<div class="msg-v">${esc(arrow)} ${esc(v.direction === 'bull' ? 'рост' : v.direction === 'bear' ? 'снижение' : 'разные направления')}</div>` : ''}` +
          `<button type="button" class="btn primary now-open-desk" data-iid="${ins.id}">Открыть рабочее место</button>`,
        footHtml:
          `<div class="now-card-foot">Котировка · <span class="price-age">${esc(p ? ageText(p.at) : '—')}</span></div>`,
      });
      const tick = st.prices.get(ins.id);
      if (tick) LFCopy.updatePrice(el, Date.now() - tick.at <= 60000 ? tick.price : null);
      return;
    }
    const cancel = cancelText(v);
    el.innerHTML = `
      <div class="now-card-head">
        <span>${esc(pair)} · ${esc(tf)}</span>
        ${dirBadge(v.direction)}
      </div>
      <h3>${esc(headline(v))}</h3>
      ${reviewBadge(v)}
      <p class="now-card-lead">${esc(leadText(v, r, tf))}</p>
      ${factsHtml(v)}
      <dl class="now-qa">
        <dt class="qa-q">Чего ждём</dt>
        <dd class="qa-a">${esc(waitText(v))}</dd>
        ${cancel ? `<dt class="qa-q">Условие отмены</dt><dd class="qa-a">${esc(cancel)}</dd>` : ''}
      </dl>
      <button type="button" class="btn primary now-open-desk" data-iid="${ins.id}">Открыть рабочее место</button>
      <div class="now-card-foot">Котировка · <span class="price-age">${esc(p ? ageText(p.at) : '—')}</span></div>`;
  }

  function render() {
    if (!isActive()) return;
    renderSubline();
    renderStats();
    renderTable();
    renderCard();
  }

  // Точечное обновление возраста котировок без перерисовки (фокус/скролл
  // таблицы не сбрасываются)
  function refreshAges() {
    document.querySelectorAll('#now-tbody tr[data-iid]').forEach((tr) => {
      const p = priceOf(Number(tr.dataset.iid));
      const age = tr.querySelector('.price-age');
      const val = tr.querySelector('.price-val');
      if (age) age.textContent = p ? ageText(p.at) : '';
      if (val && p) val.textContent = fmtPrice(p.price);
    });
    const foot = $('now-card') && $('now-card').querySelector('.now-card-foot .price-age');
    if (foot && st.selectedId) {
      const p = priceOf(st.selectedId);
      foot.textContent = p ? ageText(p.at) : '—';
    }
    renderSubline();
  }

  // ------------------------------------------------------------------- data

  function pickSelected() {
    if (!st.rows.length) { st.selectedId = null; return; }
    if (st.selectedId && st.rows.some((r) => r.instrument.id === st.selectedId)) return;
    const saved = Number(localStorage.getItem(INSTRUMENT_KEY));
    if (saved && st.rows.some((r) => r.instrument.id === saved)) {
      st.selectedId = saved;
      return;
    }
    const rows = visibleRows();
    st.selectedId = rows.length ? rows[0].instrument.id : st.rows[0].instrument.id;
  }

  async function refresh() {
    if (!isActive()) return;
    const seq = ++st.reqSeq;
    try {
      const [ov, cands] = await Promise.all([
        api('/api/ltf/instruments'),
        api('/api/candidates'),
      ]);
      if (seq !== st.reqSeq) return;
      st.rows = (ov && ov.instruments) || [];
      // §13 (этап 6b): доменные версии снимка — точка отсчёта для WS-diff'ов
      if (ov && ov.versions && HTF.DomainSync) {
        for (const [key, v] of HTF.DomainSync.statusEntries(ov.versions)) {
          const cur = st.versions.get(key);
          if (cur == null || v > cur) st.versions.set(key, v);
        }
      }
      st.candidates = ((cands || []).filter((z) => HTF_TFS.has(z.timeframe))).length;
      // Цена строки уже в обзоре. Полный снимок H1 нужен только карточке
      // выбранного актива, а не каждому инструменту списка.
      pickSelected();
      render();
      loadRecent(seq);
      await loadCurrent(st.selectedId, seq);
    } catch (e) {
      console.warn('now refresh:', e);
    }
  }

  function scheduleRefresh() {
    if (st.refreshTimer) return;
    st.refreshTimer = setTimeout(() => {
      st.refreshTimer = null;
      refresh();
    }, 800);
  }

  // §14 (этап 6b): рефетч только при росте доменной версии; сообщение без
  // версий (старый сервер) — прежнее поведение (дебаунс-рефетч)
  function versionAdvanced(data) {
    const entries = HTF.DomainSync ? HTF.DomainSync.wsEntries(data) : [];
    if (!entries.length) return true;
    let changed = false;
    for (const [key, value] of entries) {
      if (value == null) continue;
      const cur = st.versions.get(key);
      // ключа ещё нет в снимке (например, candles вне versions листинга) —
      // консервативно считаем изменением
      if (cur == null || value > cur) { st.versions.set(key, value); changed = true; }
    }
    return changed;
  }

  function onWs(data) {
    if (!isActive()) return;
    if (document.hidden) return; // AC17: возврат на вкладку — общий recovery
    if (data.type === 'price' && data.instrument_id != null && data.price) {
      st.prices.set(data.instrument_id, { price: data.price, at: data.time || Date.now() });
      refreshAges();
      renderCard();
      // scheduleRefresh() с тика убран (AC16): снимки перечитываются по смене
      // доменных версий, котировка меняет только цену и возраст
    } else if (data.type === 'event' || data.type === 'zone' || data.type === 'ltf'
        || data.type === 'candle') {
      if (versionAdvanced(data)) scheduleRefresh();
    }
  }

  function show() {
    renderSubline();
    refresh();
    if (!st.ageTimer) {
      st.ageTimer = setInterval(() => {
        if (!isActive()) {
          clearInterval(st.ageTimer);
          st.ageTimer = null;
          return;
        }
        refreshAges();
        const dot = document.getElementById('ws-indicator');
        if (dot && dot.classList.contains('offline')) refresh();
      }, 15000);
    }
  }

  // ------------------------------------------------------------------ events

  function paintFilterButtons() {
    document.querySelectorAll('#view-now .now-filter').forEach((btn) => {
      btn.classList.toggle('active', (btn.dataset.filter || 'all') === st.filter);
    });
    document.querySelectorAll('.metric-filter').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.metric === st.filter);
      btn.setAttribute('aria-pressed', btn.dataset.metric === st.filter ? 'true' : 'false');
    });
  }

  document.querySelectorAll('#view-now .now-filter').forEach((btn) => {
    btn.addEventListener('click', () => {
      st.filter = btn.dataset.filter || 'all';
      paintFilterButtons();
      renderTable();
      renderStats();
    });
  });

  document.querySelectorAll('.metric-filter').forEach((btn) => {
    btn.addEventListener('click', () => {
      st.filter = st.filter === btn.dataset.metric ? 'all' : btn.dataset.metric;
      paintFilterButtons();
      renderTable();
      renderStats();
    });
  });

  async function loadRecent(seq) {
    const box = $('now-recent');
    if (!box) return;
    try {
      const data = await api('/api/journal/page?kind=all&limit=30');
      const items = LFUI.groupEvents(data.items).slice(0,5);
      if (seq !== st.reqSeq) return;
      if (!items || !items.length) {
        box.innerHTML = '<li class="now-empty">Записей журнала пока нет.</li>';
        return;
      }
      st.recentItems = items.map(group => group.items[0]);
      box.innerHTML = items.slice(0, 5).map((group,index) => {
        const item = group.items[0];
        const who = item.symbol ? `${esc(item.symbol)} · ` : '';
        return `<li><button type="button" class="now-recent-open" data-recent="${index}">` +
          `<span class="now-recent-time" title="${esc(HTF.fmtTime(item.at))}">${esc(ageText(item.at))}</span> ${who}${esc(LFUI.readable(item.title || 'Запись'))}${group.items.length > 1 ? ` · ${group.items.length} повторения` : ''}</button></li>`;
      }).join('');
    } catch (e) {
      if (seq !== st.reqSeq) return;
      box.innerHTML = '<li class="now-empty">Журнал не прочитан. <button type="button" id="now-recent-retry">Повторить</button></li>';
    }
  }

  document.getElementById('view-now').addEventListener('click', (e) => {
    if (e.target.id === 'now-filter-reset') {
      st.filter = 'all';
      paintFilterButtons();
      renderTable();
      renderStats();
    }
    if (e.target.id === 'now-recent-retry') loadRecent(st.reqSeq);
    const recent = e.target.closest('.now-recent-open');
    if (recent && window.LFJournal) LFJournal.details(st.recentItems[Number(recent.dataset.recent)]);
  });

  async function loadCurrent(id, seq) {
    if (!id) return;
    const my = ++st.cardSeq;
    if (!st.currents.has(id)) {
      st.cardLoading = true;
      renderCard();
    }
    let cur = null;
    try {
      cur = await api(`/api/ltf/instruments/${id}/current`);
    } catch (e) {
      cur = null;
    }
    if (seq !== st.reqSeq || my !== st.cardSeq || st.selectedId !== id) {
      if (my === st.cardSeq) st.cardLoading = false;
      return;
    }
    if (cur) st.currents.set(id, cur);
    else st.currents.delete(id);
    st.cardLoading = false;
    renderCard();
  }

  function selectRow(tr) {
    if (!tr) return;
    st.selectedId = Number(tr.dataset.iid);
    document.querySelectorAll('#now-tbody tr').forEach((el) =>
      el.classList.toggle('selected', el === tr));
    if (st.currents.has(st.selectedId)) renderCard();
    else loadCurrent(st.selectedId, st.reqSeq);
  }

  function openObserved(id) {
    if (window.LFDesk && id) window.LFDesk.openInstrument(Number(id));
  }

  $('now-tbody').addEventListener('click', (e) => {
    const off = e.target.closest('.asset-off');
    if (off) {
      e.preventDefault();
      e.stopPropagation();
      if (window.LFInstruments) {
        window.LFInstruments.setActive(Number(off.dataset.off), false);
      }
      return;
    }
    const tr = e.target.closest('tr');
    if (!tr || e.target.closest('a')) return;
    e.preventDefault();
    openObserved(tr.dataset.iid);
  });
  $('now-tbody').addEventListener('keydown', (e) => {
    if (e.target.closest('button, a, input, textarea, select')) return;
    const tr = e.target.closest('tr');
    if (!tr) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openObserved(tr.dataset.iid);
    }
  });

  $('now-card').addEventListener('click', (e) => {
    const btn = e.target.closest('.now-open-desk');
    if (btn && window.LFDesk) window.LFDesk.openInstrument(Number(btn.dataset.iid));
  });

  if (typeof registerWsHandler === 'function') registerWsHandler(onWs);
  document.addEventListener('lf-reconciled', () => { refresh(); });

  // Гонка инициализации: main() в app.js продолжается после await ensureToken
  // микрозадачей и может вызвать showView('now') до выполнения этого скрипта
  // (окно тогда активно, но рендер не вызван) — покрываем самостоятельно
  if (isActive()) show();

  window.LFNow = { show, refresh };
})();
