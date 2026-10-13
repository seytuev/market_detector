/* Shared presentation for analysis adapters, data channels and event details.
   Display only: never changes detector thresholds or scenario lifecycle. */
'use strict';
window.LFUI = (() => {
  const esc = HTF.esc;
  let drawer = null;
  let previousFocus = null;
  const REASONS = {
    no_quote: 'Котировка ещё не получена', no_h1_candles: 'Нет свечей H1',
    current_candle_pending: 'Ожидаем данные текущей свечи H1', quote_stale: 'Котировка устарела',
    h1_stale: 'Свечи H1 устарели', d1_stale: 'Свечи D1 устарели', w1_stale: 'Свечи W1 устарели',
    source_stale: 'Источник недоступен', processing_lag: 'Расчёт отстаёт', history_gap: 'Разрыв истории H1',
    replay_in_progress: 'История догружается и пересчитывается',
    quote_processing_failed: 'Не удалось обработать котировку', quote_processing_lag: 'Котировка ожидает обработки',
  };
  const CHANNELS = {quote: 'Котировка', h1: 'Закрытые свечи H1', d1: 'Закрытые свечи D1',
    w1: 'Закрытые свечи W1', continuity: 'Непрерывность H1', processing: 'Расчёт H1', replay: 'Восстановление'};
  const STATUS = {ok: 'Актуальны', stale: 'Устарели', missing: 'Недоступны', lagging: 'Ожидание обработки'};

  function readable(value) {
    return String(value || '').replace(/Цена в Entry Zone/g, 'Цена в зоне входа')
      .replace(/касание Entry Zone/gi, 'Цена коснулась зоны входа')
      .replace(/Новые входы не отправляются/g, 'Уведомления о новых условиях входа приостановлены');
  }
  function error(err, text = 'Не удалось загрузить данные', retry) {
    const id = 'LF-' + Date.now().toString(36).toUpperCase();
    console.error(id, err);
    let box = document.getElementById('lf-error');
    if (!box) {
      box = document.createElement('div'); box.id = 'lf-error'; box.setAttribute('role', 'alert');
      document.body.appendChild(box);
    }
    box.innerHTML = `<span>${esc(text)}. ID ${id}</span><button type="button" class="btn">Повторить</button><button type="button" class="btn" aria-label="Закрыть сообщение">×</button>`;
    const buttons = box.querySelectorAll('button');
    buttons[0].onclick = () => { box.remove(); if (retry) retry(); else location.reload(); };
    buttons[1].onclick = () => box.remove();
    return id;
  }
  function closeDrawer() {
    if (!drawer) return;
    drawer.remove(); drawer = null;
    if (previousFocus && previousFocus.isConnected) previousFocus.focus();
    previousFocus = null;
  }
  function openDrawer(title, html, onOpen) {
    const opener = drawer ? previousFocus : document.activeElement;
    closeDrawer(); previousFocus = opener;
    document.querySelectorAll('.layer-menu[open], .data-sync-bar details[open]').forEach(el => el.open = false);
    drawer = document.createElement('div'); drawer.className = 'lf-drawer-backdrop';
    drawer.innerHTML = `<section class="lf-drawer" role="dialog" aria-modal="true" aria-labelledby="lf-drawer-title"><header><h2 id="lf-drawer-title">${esc(title)}</h2><button type="button" class="btn" aria-label="Закрыть панель">×</button></header><div class="lf-drawer-body">${html}</div></section>`;
    document.body.appendChild(drawer);
    drawer.querySelector('header button').onclick = closeDrawer;
    drawer.onclick = event => { if (event.target === drawer) closeDrawer(); };
    drawer.querySelector('header button').focus();
    if (onOpen) onOpen(drawer);
  }
  function dataLabel(ds) {
    if (!ds || !ds.state) return 'Ещё не проверены';
    if (ds.reason === 'current_candle_pending') return REASONS.current_candle_pending;
    return ds.state === 'ok' ? 'Актуальны' : REASONS[ds.reason] || 'Есть ограничения данных';
  }
  function channelsHtml(ds) {
    const channels = ds && ds.channels || {};
    return `<p>${esc(dataLabel(ds))}</p><dl class="lf-facts">` + Object.entries(channels).map(([key, ch]) =>
      `<dt>${esc(CHANNELS[key] || key)}</dt><dd>${esc(REASONS[ch.reason] || STATUS[ch.status] || 'Ещё не проверено')}${ch.last_at ? `<small>${esc(HTF.fmtTime(ch.last_at))}</small>` : ''}${ch.threshold_s ? `<small>Порог актуальности: ${esc(ch.threshold_s)} с</small>` : ''}</dd>`).join('') + '</dl>';
  }
  function scenarioHtml(summary) {
    if (!summary) return '';
    const direction = {bull: 'Рост', bear: 'Снижение', long: 'Рост', short: 'Снижение'}[summary.direction];
    return `<div class="lf-scenario-summary"><span class="lf-caption">Сценарий</span><h3>${esc(summary.label)}</h3>${direction ? `<p>${esc(direction)}</p>` : ''}${summary.confirmed_at ? `<small>Подтверждён ${esc(HTF.fmtTime(summary.confirmed_at))}</small>` : ''}</div>`;
  }
  function eventId(item) {
    if (item.uid) return item.uid;
    const ref = item.ref || {};
    const id = ref.event_id ?? ref.review_id ?? ref.delivery_id;
    return id == null ? '' : `${item.source}:${item.category}:${id}`;
  }
  function uniqueEvents(items) {
    const ids = new Set();
    return items.filter(item => { const id = eventId(item); if (!id) return true;
      if (ids.has(id)) return false; ids.add(id); return true; });
  }
  function groupEvents(items) {
    const groups = [];
    for (const item of uniqueEvents(items)) {
      const ref = item.ref || {};
      // No object id -> no grouping. Keep one-minute groups; retain every origin.
      const object = ref.zone_id ?? ref.entry_zone_id ?? ref.scenario_id ?? ref.observation_id ?? ref.setup_id ?? ref.assessment_id;
      const key = object == null ? eventId(item) : JSON.stringify([item.source, item.category, item.instrument_id,
        item.symbol, item.kind, object, ref.zone_id, ref.entry_zone_id, item.status]);
      const last = groups[groups.length - 1];
      if (object != null && last && last.key === key && last.items[0].at - item.at <= 60000) last.items.push(item);
      else groups.push({key, items: [item]});
    }
    return groups;
  }
  document.addEventListener('keydown', event => {
    if (!drawer) return;
    if (event.key === 'Escape') { event.preventDefault(); closeDrawer(); return; }
    if (event.key === 'Tab') {
      const focus = [...drawer.querySelectorAll('button, a, input, select, textarea, summary')]
        .filter(el => !el.disabled && el.getClientRects().length);
      const first = focus[0], last = focus[focus.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  // Only one temporary menu can remain open at a time.
  document.addEventListener('toggle', event => {
    const el = event.target;
    if (!el.matches?.('.layer-menu, .data-sync-bar details, .lf-export') || !el.open) return;
    document.querySelectorAll('.layer-menu[open], .data-sync-bar details[open], .lf-export[open]').forEach(other => { if (other !== el) other.open = false; });
  }, true);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') document.querySelectorAll('.layer-menu[open], .data-sync-bar details[open], .lf-export[open]').forEach(el => { el.open = false; el.querySelector('summary')?.focus(); });
  });
  window.addEventListener('error', event => { if (event.error) error(event.error, 'Не удалось отобразить часть экрана'); });
  window.addEventListener('unhandledrejection', event => error(event.reason, 'Действие не завершено'));
  return {readable, error, openDrawer, closeDrawer, channelsHtml, dataLabel, scenarioHtml,
    eventId, uniqueEvents, groupEvents};
})();
