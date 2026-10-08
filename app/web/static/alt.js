/* Рабочее место «Альткоины D1».
   Правила графика — alt_chart.js и docs/Altcoins_Chart_Declutter_Spec_RU.md.
   Фильтры отображения не меняют сетапы, входы, цели и уведомления. */
'use strict';

const { api, fmtPrice, fmtTime, esc } = window.HTF;
const $ = (id) => document.getElementById(id);
const DAY_MS = 86400000;
const PREF_KEY = 'lf:alt:prefs';

const EVENT_RANK = {
  cancelled: 100, expired_no_retest: 100, targets_completed: 100,
  review_required: 90, data_stale: 90, entry_a: 80, entry_b: 80,
  retest: 70, breakout: 60, bos_confirmed: 40, sms_confirmed: 40,
  target_hit: 30, ssl_taken: 20, manipulation_ended: 15,
  manipulation_started: 15, mature_frozen: 10, forming_started: 5,
};

const HISTORY_TYPES = [
  ['BOS', 'BOS'], ['SMS', 'SMS'], ['SSL', 'SSL'],
  ['BOS_REV', 'BOS обр.'], ['SMS_REV', 'SMS обр.'],
  ['entry_a', 'Вход A'], ['entry_b', 'Вход B'],
  ['breakout', 'Выход'], ['retest', 'Ретест'], ['target_hit', 'Цели'],
  ['cancelled', 'Отмена'], ['expired_no_retest', 'Срок'],
  ['targets_completed', 'Цели выполнены'],
];

const COLUMNS = [
  { id: 'asset', title: 'Монета / источник', render: (r) => `${esc(r.asset.symbol)} <span class="alt-dim">${esc((r.source || {}).symbol || '')} ${esc((r.source || {}).venue || '')}</span>` },
  { id: 'stage', title: 'Стадия', render: (r) => esc(r.terminal ? r.state_ru : (r.stage_ru || r.state_ru || '—')) },
  { id: 'close', title: 'Закрытие D1', num: true, render: (r) => fmtPrice(r.last_close) },
  { id: 'distance', title: 'До входа', num: true, render: (r) => r.distance_pct == null ? '—' : fmtPct(r.distance_pct) },
  { id: 'age', title: 'Возраст, дн', num: true, render: (r) => r.age_days == null ? '—' : String(r.age_days) },
  { id: 'dd', title: 'Падение от ATH', num: true, render: (r) => fmtPct(r.drawdown_pct) },
  { id: 'last', title: 'Последнее событие', render: (r) => r.last_event ? `${esc(r.last_event.label_ru)} <span class="alt-dim">${fmtDate(r.last_event.event_time_ms)}</span>` : esc(r.reason || '—') },
  { id: 'rank', title: 'Ранг капитализации', num: true, render: (r) => r.asset.cmc_rank || '—' },
  { id: 'ath', title: 'ATH', render: (r) => `${fmtPrice(r.ath_price)} <span class="alt-dim">${fmtDate(r.ath_open_time)}</span>` },
  { id: 'pmin', title: 'Минимум после ATH', render: (r) => `${fmtPrice(r.p_min)} <span class="alt-dim">${fmtDate(r.p_min_open_time)}</span>` },
  { id: 'l', title: 'L', num: true, render: (r) => fmtPrice(r.range && r.range.lower) },
  { id: 'u', title: 'U', num: true, render: (r) => fmtPrice(r.range && r.range.upper) },
  { id: 'm', title: 'M', num: true, render: (r) => fmtPrice(r.range && r.range.mid) },
  { id: 'w', title: 'W', num: true, render: (r) => fmtPrice(r.range && r.range.width) },
  { id: 'width', title: 'Ширина', render: (r) => (r.width_up_pct == null ? '—' : `+${fmtPct(r.width_up_pct)} / −${fmtPct(r.width_down_pct)}`) },
  { id: 'flags', title: 'Флаги', render: (r) => esc(flagsText(r)) },
  { id: 'entry', title: 'Вход', render: (r) => (r.entry_kinds && r.entry_kinds.length ? esc(r.entry_kinds.join('+')) : '—') },
  { id: 'tp', title: 'TP', render: (r) => (!r.targets || !r.targets.length ? '—' : r.targets.map((t) => `${t.hit ? '✓' : ''}TP${t.tp}`).join(' ')) },
  { id: 'k', title: 'K', num: true, render: (r) => (r.cancel && r.cancel.price != null ? fmtPrice(r.cancel.price) : '—') },
  { id: 'kreach', title: 'K достижим', render: (r) => (r.cancel ? (r.cancel.reachable ? 'да' : 'нет') : '—') },
  { id: 'deadline', title: 'Срок ретеста', render: (r) => (r.retest_deadline_ms ? fmtTime(r.retest_deadline_ms) : '—') },
  { id: 'reason', title: 'Причина пропуска', render: (r) => esc(r.reason || (r.run_status && r.run_status !== 'processed' ? r.run_status : '—')) },
];
const DEFAULT_COLUMNS = ['asset', 'stage', 'close', 'distance', 'age', 'dd', 'last'];

const state = {
  rows: [], buckets: {}, venues: [], listPhase: 'loading', listError: '',
  listSeq: 0, reqSeq: 0,
  bucket: 'eligible', venue: '', search: '', sort: 'server',
  rankMin: '', rankMax: '', ageMin: '', ageMax: '', ddMin: '', ddMax: '',
  structure: '', stage: '',
  selected: null, detail: null, normalized: null, nodata: null,
  cardPhase: 'idle', cardError: '', outsideFilter: false,
  eventMode: 'setup', selectedEventKey: null,
  layers: {
    range: true, entries: true, manipulation: true, targets: 'nearest',
    cancel: true, reverse: false, reverseUser: false, historyTypes: null,
  },
  tableMode: false, columns: DEFAULT_COLUMNS.slice(),
  pane: 'list',
  timeframe: 'D1',
  candles: [], rawCandles: [], viewCandles: [], candleByTime: new Map(),
  chart: null, candleSeries: null, ro: null,
  manualPrice: null, priceRange: null, followRight: true,
  pendingViewport: null, viewports: {},
  suspendPrice: false, applyingPrice: false,
  freshNote: '', latestStructureMs: null, jumpNote: '', rangeNote: '',
  retestNote: '', targetNote: '', autoNote: '',
  run: null, refreshing: false, reloadTimer: null,
  drawQueued: false,
};

function fmtPct(value) {
  if (value == null || Number.isNaN(Number(value))) return '—';
  return Number(value).toLocaleString('ru-RU', { maximumFractionDigits: 2 }) + '%';
}
function fmtDate(ms) {
  if (!ms) return '—';
  return new Date(ms).toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' });
}
function inputDate(ms) {
  if (!ms) return '';
  return new Date(ms).toISOString().slice(0, 10);
}
function dateMs(value) {
  return value ? Date.parse(value + 'T00:00:00Z') : null;
}
function mskDay(ms) {
  return new Date(ms).toLocaleDateString('en-CA', { timeZone: 'Europe/Moscow' });
}
function objectId(row) {
  if (!row) return 0;
  if (row.setup_id != null) return row.setup_id;
  if (row.candidate_id != null) return row.candidate_id;
  return row.asset ? row.asset.id : 0;
}
function flagsText(row) {
  const flags = row.flags || {};
  const parts = [];
  if (flags.structure_event) parts.push('BOS/SMS');
  if (flags.ssl_event) parts.push('SSL');
  if (flags.manipulation_active) parts.push('манип.');
  if (flags.breakout_confirmed) parts.push('выход');
  if (flags.retest_received) parts.push('ретест');
  return parts.join(' ') || '—';
}
function sameSelection(row) {
  const sel = state.selected;
  if (!sel || !row) return false;
  if (sel.kind === 'setup') return row.setup_id === sel.id;
  return sel.kind === 'candidate' && row.candidate_id === sel.id;
}
function currentRow() {
  return state.rows.find(sameSelection) || null;
}
function hasSecondary() {
  return !!(state.venue || state.search || state.rankMin !== '' || state.rankMax !== '' ||
    state.ageMin !== '' || state.ageMax !== '' || state.ddMin !== '' || state.ddMax !== '' ||
    state.structure || state.stage);
}
function reverseOn() {
  if (state.layers.reverseUser) return !!state.layers.reverse;
  return state.eventMode === 'history';
}
function matchSearch(row, query) {
  if (!query) return true;
  const blob = [row.asset && row.asset.symbol, row.asset && row.asset.name, (row.source || {}).symbol, (row.source || {}).venue]
    .join(' ').toLowerCase();
  return blob.includes(query.trim().toLowerCase());
}
function viewRows() {
  const rows = state.rows.filter((row) => matchSearch(row, state.search));
  if (state.sort === 'server') return rows;
  const copy = rows.slice();
  copy.sort((a, b) => {
    const ida = objectId(a);
    const idb = objectId(b);
    let av; let bv; let dir = 1;
    if (state.sort === 'distance') { av = a.distance_pct; bv = b.distance_pct; }
    else if (state.sort === 'rank') { av = a.asset.cmc_rank || null; bv = b.asset.cmc_rank || null; }
    else { av = a.age_days; bv = b.age_days; dir = -1; }
    const an = av == null; const bn = bv == null;
    if (an && bn) return ida - idb;
    if (an) return 1;
    if (bn) return -1;
    if (av !== bv) return dir * (av - bv);
    return ida - idb;
  });
  return copy;
}

function defaultPrefs() {
  return {
    v: 1, bucket: 'eligible', venue: '', search: '', sort: 'server',
    rankMin: '', rankMax: '', ageMin: '', ageMax: '', ddMin: '', ddMax: '',
    structure: '', stage: '', eventMode: 'setup', timeframe: 'D1',
    layers: {
      range: true, entries: true, manipulation: true, targets: 'nearest',
      cancel: true, reverse: false, reverseUser: false, historyTypes: null,
    },
    tableMode: false, columns: DEFAULT_COLUMNS.slice(), selected: null,
  };
}
function loadPrefs() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(PREF_KEY) || 'null'); } catch (e) { saved = null; }
  const prefs = saved && saved.v === 1 ? saved : defaultPrefs();
  state.bucket = prefs.bucket || 'eligible';
  state.venue = prefs.venue || '';
  state.search = prefs.search || '';
  state.sort = prefs.sort || 'server';
  ['rankMin', 'rankMax', 'ageMin', 'ageMax', 'ddMin', 'ddMax', 'structure', 'stage'].forEach((key) => {
    state[key] = prefs[key] == null ? '' : String(prefs[key]);
  });
  state.eventMode = prefs.eventMode || 'setup';
  state.timeframe = prefs.timeframe === 'W1' ? 'W1' : 'D1';
  state.layers = Object.assign(defaultPrefs().layers, prefs.layers || {});
  state.tableMode = !!prefs.tableMode;
  state.columns = Array.isArray(prefs.columns) && prefs.columns.length ? prefs.columns.filter((id) => COLUMNS.some((col) => col.id === id)) : DEFAULT_COLUMNS.slice();
  if (prefs.selected && prefs.selected.kind && prefs.selected.id != null) state.savedSelected = prefs.selected;
}
function savePrefs() {
  const payload = {
    v: 1, bucket: state.bucket, venue: state.venue, search: state.search, sort: state.sort,
    rankMin: state.rankMin, rankMax: state.rankMax, ageMin: state.ageMin, ageMax: state.ageMax,
    ddMin: state.ddMin, ddMax: state.ddMax, structure: state.structure, stage: state.stage,
    eventMode: state.eventMode, layers: state.layers, tableMode: state.tableMode,
    timeframe: state.timeframe,
    columns: state.columns,
    selected: state.selected ? { kind: state.selected.kind, id: state.selected.id } : null,
  };
  try { localStorage.setItem(PREF_KEY, JSON.stringify(payload)); } catch (e) { /* приватный режим */ }
}

function queryString() {
  const params = new URLSearchParams();
  params.set('bucket', state.bucket);
  if (state.venue) params.set('venue', state.venue);
  if (state.rankMin !== '') params.set('rank_min', state.rankMin);
  if (state.rankMax !== '') params.set('rank_max', state.rankMax);
  if (state.ageMin !== '') params.set('age_min', state.ageMin);
  if (state.ageMax !== '') params.set('age_max', state.ageMax);
  if (state.ddMin !== '') params.set('dd_min', state.ddMin);
  if (state.ddMax !== '') params.set('dd_max', state.ddMax);
  if (state.structure) params.set('structure', state.structure);
  return params.toString();
}

function fillFilterInputs() {
  $('flt-rank-min').value = state.rankMin;
  $('flt-rank-max').value = state.rankMax;
  $('flt-age-min').value = state.ageMin;
  $('flt-age-max').value = state.ageMax;
  $('flt-dd-min').value = state.ddMin;
  $('flt-dd-max').value = state.ddMax;
  $('flt-structure').value = state.structure;
  $('flt-stage').value = state.stage;
  $('alt-search').value = state.search;
  $('alt-sort').value = state.sort;
  $('flt-venue').value = state.venue;
}
function fillVenues() {
  const select = $('flt-venue');
  const venues = new Set(state.venues);
  if (state.venue) venues.add(state.venue);
  const sorted = [...venues].filter(Boolean).sort();
  select.innerHTML = '<option value="">Все</option>' + sorted.map((venue) => `<option value="${esc(venue)}">${esc(venue)}</option>`).join('');
  select.value = state.venue;
}
function showErrors(errors) {
  $('err-rank').textContent = errors.rank || '';
  $('err-age').textContent = errors.age || '';
  $('err-dd').textContent = errors.dd || '';
}
function readNumber(id) {
  const raw = $(id).value.trim();
  if (!raw) return { empty: true, value: '' };
  const value = Number(raw);
  if (!Number.isFinite(value)) return { error: 'Нужно число' };
  return { value };
}
function validateFilters() {
  const errors = {};
  const rankMin = readNumber('flt-rank-min');
  const rankMax = readNumber('flt-rank-max');
  const ageMin = readNumber('flt-age-min');
  const ageMax = readNumber('flt-age-max');
  const ddMin = readNumber('flt-dd-min');
  const ddMax = readNumber('flt-dd-max');
  [rankMin, rankMax, ageMin, ageMax, ddMin, ddMax].forEach((item) => {
    if (item.error) item.bad = true;
    else if (!item.empty && item.value < 0) item.bad = true;
  });
  if (rankMin.bad || rankMax.bad) errors.rank = 'Ранг — неотрицательное число';
  else if (!rankMin.empty && !rankMax.empty && rankMin.value > rankMax.value) errors.rank = 'Нижняя граница больше верхней';
  if (ageMin.bad || ageMax.bad) errors.age = 'Возраст — неотрицательное число дней';
  else if (!ageMin.empty && !ageMax.empty && ageMin.value > ageMax.value) errors.age = 'Нижняя граница больше верхней';
  if (ddMin.bad || ddMax.bad || (!ddMin.empty && ddMin.value > 100) || (!ddMax.empty && ddMax.value > 100)) {
    errors.dd = 'Падение — от 0 до 100%';
  } else if (!ddMin.empty && !ddMax.empty && ddMin.value > ddMax.value) errors.dd = 'Нижняя граница больше верхней';
  return errors;
}

function renderTabs() {
  document.querySelectorAll('.alt-tab').forEach((button) => {
    const bucket = button.dataset.bucket;
    const count = state.buckets[bucket];
    const name = button.dataset.label || button.dataset.name || button.textContent.replace(/\s+\d+$/, '').replace(/\s+\(\d+\)$/, '');
    if (!button.dataset.label) button.dataset.label = name.trim();
    button.textContent = button.dataset.label + (count != null ? ` ${count}` : '');
    const on = state.bucket === bucket;
    button.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  const filters = $('alt-filters-toggle');
  const n = ['venue', 'rankMin', 'rankMax', 'ageMin', 'ageMax', 'ddMin', 'ddMax', 'structure', 'stage']
    .filter((key) => state[key] !== '' && state[key] != null).length;
  filters.textContent = n ? `Фильтры (${n})` : 'Фильтры';
}
function renderChips() {
  const chips = [];
  const add = (key, label) => chips.push(`<button type="button" class="alt-chip-btn" data-chip="${key}">${esc(label)} ×</button>`);
  if (state.venue) add('venue', 'Биржа: ' + state.venue);
  if (state.rankMin !== '' || state.rankMax !== '') add('rank', `Ранг ${state.rankMin || '…'}–${state.rankMax || '…'}`);
  if (state.ageMin !== '' || state.ageMax !== '') add('age', `Возраст ${state.ageMin || '…'}–${state.ageMax || '…'} дн`);
  if (state.ddMin !== '' || state.ddMax !== '') add('dd', `Падение ${state.ddMin || '…'}–${state.ddMax || '…'}%`);
  if (state.structure) {
    const names = { bos_sms: 'BOS/SMS', manipulation: 'Манипуляция', breakout: 'Выход' };
    add('structure', names[state.structure] || state.structure);
  }
  if (state.stage) add('stage', state.stage === 'mature' ? 'Зрелые' : 'Формирующиеся');
  if (state.search) add('search', 'Поиск: ' + state.search);
  $('alt-chips').innerHTML = chips.join('');
}
function emptyListText() {
  if (state.listPhase === 'loading') return 'Загрузка списка…';
  if (hasSecondary()) return 'По текущим фильтрам ничего не показано. Это не значит, что сетапов нет в системе.';
  const total = Object.values(state.buckets).reduce((sum, n) => sum + (Number(n) || 0), 0);
  if (!total) return 'В текущем снимке нет сетапов. Проверьте обработку: сбой источника не означает пустой рынок.';
  if (state.bucket === 'history') return 'В истории нет завершённых сетапов.';
  return 'В этой группе пусто.';
}
function renderList() {
  const rows = viewRows();
  state.view = rows;
  $('alt-shown').textContent = 'Показано ' + rows.length;
  $('alt-outside').classList.toggle('hidden', !state.outsideFilter);
  const list = $('alt-list');
  const empty = $('alt-list-empty');
  const error = $('alt-list-error');
  error.classList.toggle('hidden', state.listPhase !== 'error');
  if (state.listPhase === 'error') error.firstChild && (error.childNodes[0].textContent = 'Не удалось загрузить список. ' + (state.listError || ''));
  if (!rows.length) {
    list.innerHTML = '';
    empty.classList.remove('hidden');
    empty.textContent = emptyListText();
    return;
  }
  empty.classList.add('hidden');
  const keepFocus = document.activeElement && list.contains(document.activeElement);
  list.innerHTML = rows.map((row) => {
    const selected = sameSelection(row) || (state.nodata && state.nodata === row);
    const kind = row.setup_id != null ? 'setup' : (row.candidate_id != null ? 'candidate' : 'nodata');
    const id = kind === 'setup' ? row.setup_id : (kind === 'candidate' ? row.candidate_id : row.asset.cmc_id);
    const stage = row.terminal ? row.state_ru : (row.stage_ru || row.state_ru || '—');
    const dist = row.distance_pct == null ? '' : `До входа ${fmtPct(row.distance_pct)}`;
    const last = row.last_event
      ? `${row.last_event.label_ru} · ${fmtDate(row.last_event.event_time_ms)}`
      : (row.reason || '');
    return `<button type="button" class="alt-row" role="option" data-kind="${kind}" data-id="${id}" aria-selected="${selected ? 'true' : 'false'}">
      <span class="alt-row-symbol">${esc(row.asset.symbol)}</span>
      <span class="alt-row-stage">${esc(stage)}</span>
      <span class="alt-row-sub">${esc((row.source || {}).symbol || '')} · ${esc((row.source || {}).venue || 'источник не выбран')}</span>
      <span class="alt-row-last">${esc([dist, last].filter(Boolean).join(' · ') || '—')}</span>
    </button>`;
  }).join('');
  if (keepFocus) {
    const current = list.querySelector('[aria-selected="true"]');
    if (current) current.focus();
  }
  renderTable();
}

function renderFresh() {
  const run = state.run;
  const last = run && run.last_run;
  const asOf = (state.detail && state.detail.as_of_ms) || (last && last.as_of_ms) || state.asOf;
  $('alt-asof').textContent = asOf ? 'Данные на ' + fmtTime(asOf) : 'Свежесть данных не определена';
  let status = 'Обработка ещё не выполнялась';
  if (run && run.running) status = 'Идёт обработка';
  else if (last) {
    const names = { ok: 'Обновлены', no_universe: 'Нет вселенной', error: 'Ошибка обработки' };
    status = (names[last.status] || last.status) + (last.trigger === 'manual' ? ', ручной запуск' : '');
  }
  if (state.refreshing) status += ' · список обновляется, снимок на экране сохранён';
  $('alt-run-state').textContent = status;
  const stale = !!(run && ((run.universe && run.universe.stale) || (last && last.universe_stale)));
  const attemptError = run && run.last_attempt && run.last_attempt.status === 'error';
  const skipped = last && last.skipped && last.skipped.length;
  const problem = $('alt-problem');
  problem.classList.toggle('hidden', !(stale || attemptError || last && last.errors));
  const reasons = [];
  if (stale) reasons.push('снимок вселенной устарел');
  if (attemptError) reasons.push('последняя попытка завершилась ошибкой');
  if (last && last.errors) reasons.push('ошибок: ' + last.errors);
  if (skipped) reasons.push('пропущено: ' + last.skipped.length);
  problem.title = reasons.join('. ');
  $('alt-running').classList.toggle('hidden', !(run && run.running));
  const lines = [];
  if (last) {
    lines.push(`Последний запуск: ${fmtTime(last.finished_ms || last.started_ms)}, статус ${last.status}.`);
    lines.push(`Обработано: ${last.processed}. Ошибок: ${last.errors}.`);
    if (skipped) lines.push('Пропуски: ' + last.skipped.map((item) => `${item.symbol || item.asset_id}: ${item.reason}`).join('; '));
  }
  lines.push(run && run.job_enabled ? `Следующий запуск: ${fmtTime(run.next_run_ms)}.` : 'Расписание выключено.');
  lines.push('Соединение в шапке — только канал WebSocket. Оно не заменяет свежесть свечей.');
  $('alt-proc-body').textContent = lines.join(' ');
}

function lastEventOf(events) {
  if (!events || !events.length) return null;
  return events.reduce((best, event) => {
    const score = [event.event_time_ms || 0, EVENT_RANK[event.event_type] || 0, event.id || 0];
    const prev = [best.event_time_ms || 0, EVENT_RANK[best.event_type] || 0, best.id || 0];
    return score[0] > prev[0] || (score[0] === prev[0] && (score[1] > prev[1] || (score[1] === prev[1] && score[2] > prev[2])))
      ? event : best;
  });
}
function targetState(target) {
  if (target.passed_at_confirmation) return 'пройдена до подтверждения';
  if (target.hit) return 'достигнута';
  return 'ожидается';
}
function remainText(detail) {
  if (detail.terminal) {
    return `Завершён ${fmtTime(detail.terminated_ms)}. Причина: ${detail.state_ru}. Обратный отсчёт не показывается.`;
  }
  const deadline = detail.breakout && detail.breakout.retest_deadline_ms;
  if (!deadline) return 'Срок ретеста не задан.';
  const left = deadline - Date.now();
  const fresh = detail.as_of_ms ? 'Данные на ' + fmtTime(detail.as_of_ms) + '.' : 'Свежесть снимка не определена.';
  if (left < 0) return `Срок ${fmtTime(deadline)} уже прошёл. ${fresh}`;
  const days = Math.floor(left / DAY_MS);
  const hours = Math.floor((left % DAY_MS) / 3600000);
  return `До ${fmtTime(deadline)} осталось ${days} дн. ${hours} ч. Отсчёт на момент просмотра. ${fresh}`;
}
function entryText(entry) {
  const when = fmtTime(entry.event_time_ms);
  if (entry.kind === 'A') {
    return `Вход A · закрытие подтверждающей D1 ${fmtPrice(entry.price)} · зафиксирован ${when}. Это историческая цена закрытия, не текущая заявка.`;
  }
  const zone = entry.zone || {};
  return `Вход B · зона ретеста ${fmtPrice(zone.lower)}–${fmtPrice(zone.upper)} · зафиксирован ${when}. Историческая фиксация первого принятого ретеста.`;
}

function renderCard() {
  const body = $('alt-card-body');
  if (state.cardPhase === 'loading') {
    body.innerHTML = '<p class="alt-empty">Загрузка карточки…</p>';
    return;
  }
  if (state.cardPhase === 'missing') {
    body.innerHTML = `<p class="alt-warn">${esc(state.cardError || 'Объект не найден')}</p><p>Другой сетап вместо него не открыт.</p>`;
    return;
  }
  if (state.cardPhase === 'error') {
    body.innerHTML = `<p class="alt-warn">${esc(state.cardError || 'Ошибка запроса')}</p><button type="button" class="btn small" id="alt-retry-card">Повторить</button>`;
    $('alt-retry-card').onclick = () => { if (state.selected) selectRow(state.selected.kind, state.selected.id); };
    return;
  }
  if (state.cardPhase === 'nodata' && state.nodata) {
    const row = state.nodata;
    body.innerHTML = `<p><b>${esc(row.asset.symbol)}</b> · ${esc(row.state_ru)}</p><p>${esc(row.reason || 'Нет данных по источнику')}</p><p class="alt-dim">Это проблема данных, а не отсутствие сетапов на рынке.</p>`;
    return;
  }
  const detail = state.detail;
  if (!detail) {
    body.innerHTML = '<p class="alt-empty">Объект не выбран.</p>';
    return;
  }
  const last = lastEventOf(detail.events);
  const parts = [];
  parts.push('<h3>Сейчас</h3>');
  parts.push(`<p>${esc(detail.state_ru)}${last ? ` · ${esc(last.label_ru || last.event_type)} · ${fmtTime(last.event_time_ms)}` : ''}</p>`);
  if (detail.terminal) parts.push(`<p>Завершение: ${esc(detail.state_ru)} · ${fmtTime(detail.terminated_ms)}</p>`);
  if (detail.universe_eligible === false) parts.push('<p>Актив вне текущей выборки. Наблюдение идёт до завершения сетапа.</p>');

  parts.push('<h3>Основание</h3>');
  if (!detail.confirmation) {
    parts.push('<p>Диапазон формируется / подтверждения ещё нет.</p>');
  } else {
    const link = detail.confirmation.structure_link || {};
    const candle = detail.confirmation.candle_open_time;
    parts.push(`<p>${esc(detail.confirmation.event_type)} · ${fmtTime(detail.confirmation.event_time_ms)}</p>`);
    if (link.status === 'unknown') parts.push('<p>Связь со структурной записью не определена.</p>');
    if (candle) parts.push(`<p><button type="button" class="btn small" data-jump="${candle}">К свече основания</button></p>`);
  }

  parts.push('<h3>Входы</h3>');
  if (!detail.entries || !detail.entries.length) parts.push('<p>Зафиксированных входов A/B нет.</p>');
  else detail.entries.forEach((entry) => parts.push(`<p>${esc(entryText(entry))}</p>`));
  const row = currentRow();
  if (row && row.distance_pct != null) {
    parts.push(`<p class="alt-dim">Расстояние до зоны входа по последнему закрытию D1: ${fmtPct(row.distance_pct)}. Это метрика ранжирования, не сигнал к сделке.</p>`);
  }

  parts.push('<h3>Цели</h3>');
  if (!detail.targets || !detail.targets.length) parts.push('<p>Цели появятся после подтверждения.</p>');
  else {
    parts.push('<dl class="alt-kv">' + detail.targets.map((target) =>
      `<dt>TP${target.tp}</dt><dd>${fmtPrice(target.price)} · ${targetState(target)}</dd>`
    ).join('') + '</dl>');
  }

  parts.push('<h3>Отмена</h3>');
  if (!detail.cancel) parts.push('<p>Уровень отмены ещё не рассчитан.</p>');
  else if (!(detail.cancel.price > 0)) {
    parts.push(`<p>Расчётный уровень отмены ≤ 0; на графике не отображается. ${esc(detail.cancel.mode_ru || '')}</p>`);
  } else {
    parts.push(`<p>K ${fmtPrice(detail.cancel.price)} · ${esc(detail.cancel.mode_ru || '')}. ${esc(detail.cancel.mode_note || '')}</p>`);
  }

  parts.push('<h3>Срок ретеста</h3>');
  parts.push(`<p>${esc(remainText(detail))}</p>`);

  const anchors = detail.anchors;
  const range = detail.frozen_range || detail.range;
  const revision = detail.range_revision;
  parts.push(`<p><button type="button" class="btn small" id="alt-edit-range">Редактировать диапазон</button> ${revision ? `<span class="alt-dim">${revision.source_kind === 'manual' ? 'Ручной' : 'Авто'} · ревизия ${revision.revision}</span>` : '<span class="alt-dim">Автоматический диапазон</span>'}</p>`);
  parts.push('<h3>Почему найдено</h3>');
  const why = [];
  if (anchors && anchors.start) why.push(`Старт ${fmtDate(anchors.start.open_time)}, доступен ${fmtTime(anchors.start.available_at_ms)}.`);
  if (anchors && anchors.rebound) why.push(`Отскок ${fmtDate(anchors.rebound.open_time)}.`);
  if (range) why.push(`Диапазон L ${fmtPrice(range.lower)}, U ${fmtPrice(range.upper)}, M ${fmtPrice(range.mid)}.`);
  const shownRange = window.AltChart.chartRange(detail, null);
  if (shownRange && shownRange.shelf && range &&
      (Math.abs(shownRange.lower - range.lower) > range.lower * 0.01 ||
       Math.abs(shownRange.upper - range.upper) > range.upper * 0.01)) {
    why.push(`На графике база у дна ${fmtPrice(shownRange.lower)}–${fmtPrice(shownRange.upper)}: пол, где цена консолидируется.`);
  }
  if (detail.confirmation) why.push(`Подтверждение: ${detail.confirmation.event_type}.`);
  parts.push(`<p>${esc(why.join(' ') || 'Кратких опор в снимке нет.')}</p>`);

  const formulas = detail.formulas || {};
  parts.push(`<details class="alt-fold"><summary>Формулы</summary><p>Отмена: ${esc(formulas.cancel || '—')}</p><p>Цели: ${esc(formulas.targets || '—')}</p><p>Ширина: ${esc(formulas.width_up || '')} / ${esc(formulas.width_down || '')}</p><p>Ретест: ${esc(formulas.retest_zone || '—')}</p></details>`);
  const versions = detail.versions || {};
  const classifier = detail.classifier;
  parts.push(`<details class="alt-fold"><summary>Параметры и классификатор</summary><p>Правила ${esc(versions.rule || '—')}, классификатор ${esc(versions.classifier || '—')}, источник v${esc(versions.source || '—')}, диапазон v${esc(versions.range || '—')}.</p>${classifier ? `<p>Наклон ${classifier.slope_normalized != null ? classifier.slope_normalized.toFixed(4) : '—'}, сдвиг ${classifier.center_shift != null ? classifier.center_shift.toFixed(4) : '—'}. ${esc(classifier.thresholds_note || '')}</p>` : '<p>Классификатор не записан.</p>'}</details>`);
  const versionsOfRange = detail.range_versions || [];
  parts.push(`<details class="alt-fold"><summary>Версии диапазона</summary>${versionsOfRange.length ? versionsOfRange.map((item) => `<p>v${esc(item.version)} [${fmtPrice(item.lower)}–${fmtPrice(item.upper)}]. Границы по времени в снимке нет, поэтому старая версия на графике не рисуется.</p>`).join('') : '<p>Одна версия диапазона.</p>'}</details>`);
  const source = detail.source || {};
  parts.push(`<details class="alt-fold"><summary>Данные и версии</summary><p>${esc(source.venue || '—')} · ${esc(source.symbol || '—')} · история ${esc(source.history_scope || '—')}.</p><p>${esc((detail.candle_history || {}).note || '')}</p><p>as_of ${detail.as_of_ms ? fmtTime(detail.as_of_ms) : 'не задан'}.</p></details>`);
  body.innerHTML = parts.join('');
  body.querySelectorAll('[data-jump]').forEach((button) => {
    button.onclick = () => jumpTo(Number(button.dataset.jump));
  });
  $('alt-edit-range').onclick = openRangeEditor;
}

function rangeEditPayload() {
  return {
    lower: Number($('range-edit-l').value), upper: Number($('range-edit-u').value),
    base_start_open_time: dateMs($('range-edit-start').value),
    base_end_open_time: dateMs($('range-edit-end').value),
    reason: $('range-edit-reason').value.trim(),
    expected_revision: Number((state.detail.range_revision || {}).revision || 0),
  };
}
function openRangeEditor() {
  const detail = state.detail;
  if (!detail || !state.selected) return;
  const range = detail.frozen_range || detail.range;
  const revision = detail.range_revision || {};
  $('range-edit-l').value = range.lower;
  $('range-edit-u').value = range.upper;
  $('range-edit-start').value = inputDate(revision.base_start_open_time || range.start_anchor_open_time || (detail.anchors && detail.anchors.start && detail.anchors.start.open_time));
  $('range-edit-end').value = inputDate(revision.base_end_open_time);
  $('range-edit-reason').value = '';
  $('alt-range-editor-meta').textContent = `${detail.asset.symbol} · ${state.selected.kind} · текущая ревизия ${revision.revision || 0}`;
  $('alt-range-preview').textContent = 'Измените значения и нажмите «Проверить».';
  $('alt-range-error').textContent = '';
  $('range-save-btn').disabled = true;
  $('alt-range-editor').showModal();
}
async function previewRangeEdit() {
  const sel = state.selected;
  $('alt-range-error').textContent = '';
  $('range-save-btn').disabled = true;
  try {
    const result = await api(`/api/alt/ranges/${sel.kind}/${sel.id}/preview`, {
      method: 'POST', body: JSON.stringify(rangeEditPayload()),
    });
    const d = result.derived;
    const hit = d.targets.filter((item) => item.hit).length;
    $('alt-range-preview').textContent = `M ${fmtPrice(result.range.mid)}, W ${fmtPrice(result.range.width)}, K ${fmtPrice(d.cancel.price)}. Исторически достигнуто целей: ${hit}; выходов вверх: ${d.breakouts.length}; эпизодов ниже L: ${d.excursions_below.length}. Неполученных событий старой версии будет закрыто: ${result.changes.superseded_pending_events}.`;
    $('range-save-btn').disabled = false;
  } catch (error) {
    $('alt-range-error').textContent = error.message;
  }
}
async function saveRangeEdit() {
  const sel = state.selected;
  $('range-save-btn').disabled = true;
  try {
    const payload = rangeEditPayload();
    payload.idempotency_key = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
    await api(`/api/alt/ranges/${sel.kind}/${sel.id}/revisions`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    $('alt-range-editor').close();
    await selectRow(sel.kind, sel.id, { force: true });
    await loadTable();
  } catch (error) {
    $('alt-range-error').textContent = error.message;
    $('range-save-btn').disabled = false;
  }
}
async function restoreAutoRange() {
  const sel = state.selected;
  try {
    await api(`/api/alt/ranges/${sel.kind}/${sel.id}/restore-auto`, {
      method: 'POST', body: JSON.stringify({
        expected_revision: Number((state.detail.range_revision || {}).revision || 0),
        idempotency_key: (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`),
      }),
    });
    $('alt-range-editor').close();
    await selectRow(sel.kind, sel.id, { force: true });
    await loadTable();
  } catch (error) { $('alt-range-error').textContent = error.message; }
}

function renderJournal() {
  const host = $('alt-journal-list');
  const events = (state.normalized && state.normalized.events) || [];
  const type = $('journal-type').value;
  const from = $('journal-from').value;
  const to = $('journal-to').value;
  const known = [...new Set(events.map((event) => event.type))];
  const select = $('journal-type');
  const prev = select.value;
  select.innerHTML = '<option value="">Все</option>' + known.map((item) => {
    const label = (HISTORY_TYPES.find((pair) => pair[0] === item) || [item, item])[1];
    return `<option value="${esc(item)}">${esc(label)}</option>`;
  }).join('');
  select.value = known.includes(prev) || prev === '' ? prev : '';
  const shown = events.filter((event) => {
    if (type && event.type !== type) return false;
    const day = event.available_at_ms ? mskDay(event.available_at_ms) : '';
    if (from && day && day < from) return false;
    if (to && day && day > to) return false;
    return true;
  }).sort((a, b) => (b.available_at_ms || 0) - (a.available_at_ms || 0) || (b.source_id || 0) - (a.source_id || 0));
  if (!state.detail) {
    host.innerHTML = '<p class="alt-empty">Журнал откроется вместе с сетапом.</p>';
    return;
  }
  if (!shown.length) {
    host.innerHTML = '<p class="alt-empty">В журнале этого сетапа нет событий по фильтру. Фильтр графика сюда не подставляется.</p>';
    return;
  }
  host.innerHTML = shown.map((event) => `<button type="button" class="alt-event" data-key="${esc(event.key)}" aria-current="${event.key === state.selectedEventKey ? 'true' : 'false'}">
    <span>${fmtTime(event.available_at_ms)}</span>
    <span>${esc(event.details.label_ru || event.label)}${event.importance === 'key' ? ' · ключевое' : ''}${event.details.historical ? ' · историческое восстановление' : ''}</span>
    <span>К свече</span>
  </button>`).join('');
  host.querySelectorAll('.alt-event').forEach((button) => {
    button.onclick = () => {
      state.selectedEventKey = button.dataset.key;
      const event = events.find((item) => item.key === button.dataset.key);
      renderJournal();
      if (event) jumpTo(event.candle_open_time_ms);
      scheduleDraw();
    };
  });
}

function renderHeadline() {
  const detail = state.detail;
  const row = currentRow() || state.nodata;
  const asset = (detail && detail.asset) || (row && row.asset);
  const source = (detail && detail.source) || (row && row.source) || {};
  const title = $('alt-chart-title');
  if (state.cardPhase === 'missing') {
    title.textContent = state.cardError || 'Объект не найден';
    $('alt-chart-sub').textContent = '';
    $('alt-picked').textContent = '';
    return;
  }
  if (!asset) {
    title.textContent = state.cardPhase === 'loading' ? 'Загрузка…' : 'Выберите монету';
    $('alt-chart-sub').textContent = '';
    $('alt-picked').textContent = '';
    return;
  }
  title.textContent = asset.symbol + (state.cardPhase === 'loading' ? ' · загрузка' : '');
  const close = row && row.last_close != null ? row.last_close : null;
  const when = row && row.last_close_open_time;
  const bits = [source.symbol, source.venue, state.timeframe];
  if (close != null) bits.push('Цена закрытия D1 ' + fmtPrice(close));
  if (when) bits.push(fmtDate(when));
  $('alt-chart-sub').textContent = bits.filter(Boolean).join(' · ');
  $('alt-picked').textContent = 'Выбрано: ' + asset.symbol + (source.venue ? ' · ' + source.venue : '');
}

function renderNotes() {
  const notes = [];
  if (state.detail && state.detail.as_of_ms == null) notes.push('Свежесть снимка не определена. События на графике скрыты.');
  if (state.detail && state.detail.candle_history) notes.push(state.detail.candle_history.note);
  if (state.freshNote) notes.push(state.freshNote);
  if (state.freshNote && state.latestStructureMs) notes.push('Последнее известное структурное событие: ' + fmtTime(state.latestStructureMs));
  if (state.jumpNote) notes.push(state.jumpNote);
  if (state.rangeNote) notes.push(state.rangeNote);
  if (state.retestNote) notes.push(state.retestNote);
  if (state.targetNote) notes.push(state.targetNote);
  if (state.autoNote) notes.push(state.autoNote);
  if (state.timeframe === 'W1' && state.viewCandles.length) {
    if (state.viewCandles[state.viewCandles.length - 1].partial) {
      notes.push('Последняя свеча W1 — незавершённая неделя: она изменится по мере закрытия D1.');
    }
    if (state.viewCandles.some((candle) => candle.gaps)) {
      notes.push('В истории W1 есть недели с пропущенными D1.');
    }
  }
  if (state.eventMode === 'history') notes.push('История относится к выбранному сетапу, не ко всем сетапам этой монеты.');
  $('alt-chart-note').textContent = notes.filter(Boolean).join(' ');
}

function writeUrl() {
  const url = new URL(location.href);
  url.searchParams.delete('setup');
  url.searchParams.delete('candidate');
  url.searchParams.delete('asset');
  const row = currentRow();
  const asset = (state.detail && state.detail.asset) || (row && row.asset) || (state.nodata && state.nodata.asset);
  if (state.selected && state.selected.kind === 'setup') url.searchParams.set('setup', String(state.selected.id));
  if (state.selected && state.selected.kind === 'candidate') url.searchParams.set('candidate', String(state.selected.id));
  if (asset && asset.cmc_id != null) url.searchParams.set('asset', String(asset.cmc_id));
  history.replaceState(null, '', url.pathname + url.search);
}

function bucketFor(detail) {
  if (!detail) return 'eligible';
  if (detail.kind === 'candidate') return detail.state === 'forming' ? 'forming' : 'review';
  if (detail.terminal) return 'history';
  if (detail.state === 'review_required') return 'review';
  if (detail.state === 'mature') return 'mature';
  if (detail.state === 'forming') return 'forming';
  return 'eligible';
}

async function loadTable() {
  const req = ++state.listSeq;
  try {
    const data = await api('/api/alt/setups?' + queryString());
    if (req !== state.listSeq) return;
    state.rows = data.rows || [];
    state.buckets = data.buckets || {};
    state.venues = data.venues || [];
    state.asOf = data.as_of_ms;
    state.listPhase = 'ready';
    fillVenues();
    renderTabs();
    renderChips();
    renderList();
    renderFresh();
  } catch (e) {
    if (req !== state.listSeq) return;
    state.listPhase = 'error';
    state.listError = e.message || '';
    renderList();
  }
}
async function loadStatus() {
  state.run = await api('/api/alt/run-status');
  renderFresh();
}

async function selectRow(kind, id, options) {
  const opts = options || {};
  if (!opts.preserve && state.selected) saveViewport();
  const req = ++state.reqSeq;
  state.selected = { kind, id };
  state.nodata = null;
  if (!opts.preserve) state.selectedEventKey = null;
  state.cardPhase = 'loading';
  state.detail = null;
  state.normalized = null;
  state.outsideFilter = false;
  state.jumpNote = '';
  state.rangeNote = '';
  state.autoNote = '';
  const saved = state.viewports[kind + ':' + id];
  if (opts.preserve) state.pendingViewport = opts.preserve;
  else {
    // UI-02: ручная ценовая шкала между активами не переносится — ни чужая,
    // ни сохранённая от прежнего визита этой же строки.
    state.manualPrice = null;
    state.pendingViewport = saved && saved.timeRange
      ? { timeRange: saved.timeRange, follow: saved.follow }
      : { initial: true };
  }
  if (window.innerWidth < 1100 && !opts.fromWs) state.pane = 'chart';
  applyPane();
  writeUrl();
  savePrefs();
  renderList();
  renderHeadline();
  renderCard();
  if (!opts.preserve) clearSeries();
  let detail;
  try {
    detail = await api(kind === 'setup' ? `/api/alt/setup/${id}` : `/api/alt/candidate/${id}`);
  } catch (e) {
    if (req !== state.reqSeq) return false;
    const missing = /не найден/i.test(String(e.message || ''));
    if (opts.optional && missing) {
      state.selected = null;
      state.detail = null;
      state.normalized = null;
      state.cardPhase = 'idle';
      state.cardError = '';
      state.pane = 'list';
      applyPane();
      writeUrl();
      savePrefs();
      renderList();
      renderHeadline();
      renderCard();
      renderChart();
      return false;
    }
    state.cardPhase = 'error';
    state.cardError = e.message || 'Ошибка запроса';
    renderCard();
    renderHeadline();
    renderChart();
    return false;
  }
  if (req !== state.reqSeq || !state.selected || state.selected.kind !== kind || state.selected.id !== id) return false;
  state.detail = detail;
  state.normalized = window.AltChart.normalizeDetail(detail);
  state.cardPhase = 'ready';
  state.outsideFilter = !state.rows.some(sameSelection);
  renderList();
  renderHeadline();
  renderCard();
  renderJournal();
  renderChart();
  return true;
}

function showNodata(row) {
  if (state.selected) saveViewport();
  state.selected = null;
  state.detail = null;
  state.normalized = null;
  state.nodata = row;
  state.cardPhase = 'nodata';
  state.outsideFilter = false;
  if (window.innerWidth < 1100) state.pane = 'chart';
  applyPane();
  writeUrl();
  renderList();
  renderHeadline();
  renderCard();
  renderJournal();
  renderChart();
}

async function openExact(kind, id) {
  state.selected = { kind, id };
  state.cardPhase = 'loading';
  state.detail = null;
  const req = ++state.reqSeq;
  renderHeadline();
  renderCard();
  let detail;
  try {
    detail = await api(kind === 'setup' ? `/api/alt/setup/${id}` : `/api/alt/candidate/${id}`);
  } catch (e) {
    if (req !== state.reqSeq) return;
    state.cardPhase = 'missing';
    state.cardError = (kind === 'setup' ? 'Сетап ' : 'Диапазон ') + id + ' не найден';
    state.selected = { kind, id };
    renderHeadline();
    renderCard();
    renderChart();
    return;
  }
  if (req !== state.reqSeq) return;
  state.detail = detail;
  state.normalized = window.AltChart.normalizeDetail(detail);
  state.cardPhase = 'ready';
  state.outsideFilter = !state.rows.some(sameSelection);
  state.pendingViewport = { initial: true };
  if (window.innerWidth < 1100) state.pane = 'chart';
  applyPane();
  writeUrl();
  savePrefs();
  renderList();
  renderHeadline();
  renderCard();
  renderJournal();
  renderChart();
}

async function selectByAsset(cmcId) {
  const usable = (row) => row && row.asset && row.asset.cmc_id === cmcId && (row.setup_id != null || row.candidate_id != null);
  let row = state.rows.find(usable);
  if (!row) {
    const all = await api('/api/alt/setups?bucket=all');
    row = (all.rows || []).find(usable);
    if (!row) {
      state.cardPhase = 'missing';
      state.cardError = 'Актив CMC #' + cmcId + ' не найден в текущем снимке.';
      renderCard();
      renderHeadline();
      return;
    }
    state.bucket = row.terminal ? 'history' : (row.stage === 6 ? 'review' : 'eligible');
    state.stage = '';
    savePrefs();
    syncControls();
    await loadTable();
    row = state.rows.find(usable) || row;
  }
  if (row.setup_id != null) await selectRow('setup', row.setup_id);
  else await selectRow('candidate', row.candidate_id);
}

async function showInList() {
  const detail = state.detail;
  if (!detail) return;
  state.venue = '';
  state.rankMin = state.rankMax = state.ageMin = state.ageMax = state.ddMin = state.ddMax = '';
  state.structure = '';
  state.search = '';
  state.stage = '';
  state.bucket = bucketFor(detail);
  if (state.bucket === 'mature' || state.bucket === 'forming') state.stage = state.bucket;
  fillFilterInputs();
  savePrefs();
  syncControls();
  await loadTable();
  state.outsideFilter = !state.rows.some(sameSelection);
  renderList();
}

function clearSeries() {
  if (!state.candleSeries) return;
  state.candleSeries.setData([]);
  state.candleSeries.setMarkers([]);
  $('alt-overlay').innerHTML = '';
}

function initChart() {
  if (state.chart || typeof LightweightCharts === 'undefined') return;
  const theme = HTF.chartTheme();
  state.chart = LightweightCharts.createChart($('chart'), {
    layout: { background: { color: theme.background }, textColor: theme.text },
    grid: {
      vertLines: { color: theme.grid, style: LightweightCharts.LineStyle.Dotted },
      horzLines: { color: theme.grid, style: LightweightCharts.LineStyle.Dotted },
    },
    timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 4, borderColor: theme.border },
    rightPriceScale: { borderColor: theme.border, autoScale: false },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    autoSize: true,
  });
  state.candleSeries = state.chart.addCandlestickSeries({
    upColor: theme.up, downColor: theme.down,
    wickUpColor: theme.up, wickDownColor: theme.down, borderVisible: false,
  });
  state.candleSeries.setMarkers([]);
  state.chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
    state.followRight = pinnedRight();
    scheduleDraw();
  });
  state.chart.timeScale().subscribeVisibleTimeRangeChange(() => {
    if (!state.suspendPrice && !state.manualPrice) applyPrice();
  });
  state.ro = new ResizeObserver(() => scheduleDraw());
  state.ro.observe($('chart-container'));
}
function paintChartTheme() {
  if (!state.chart) return;
  const theme = HTF.chartTheme();
  state.chart.applyOptions({
    layout: { background: { color: theme.background }, textColor: theme.text },
    grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
    rightPriceScale: { borderColor: theme.border },
    timeScale: { borderColor: theme.border },
  });
  state.candleSeries.applyOptions({
    upColor: theme.up, downColor: theme.down,
    wickUpColor: theme.up, wickDownColor: theme.down,
  });
}
function destroyChart() {
  if (state.ro) state.ro.disconnect();
  if (state.chart) state.chart.remove();
  state.chart = null;
  state.candleSeries = null;
}
function pinnedRight() {
  if (!state.chart || !state.candles.length) return false;
  const range = state.chart.timeScale().getVisibleLogicalRange();
  if (!range) return false;
  const last = state.candles.length - 1;
  return last >= range.from && last <= range.to && (range.to - last) <= 6;
}
function applyPrice() {
  if (!state.chart || !state.candles.length || state.applyingPrice) return;
  state.applyingPrice = true;
  try {
    const visible = state.chart.timeScale().getVisibleRange();
    const range = state.manualPrice || window.AltChart.priceRange(
      state.candles, visible && visible.from, visible && visible.to
    );
    state.priceRange = range;
    const scale = state.chart.priceScale('right');
    scale.applyOptions({ autoScale: false });
    scale.setVisibleRange({ from: range.min, to: range.max });
  } catch (e) {
    // UI-02: сбой расчёта не должен оставлять шкалу другого актива —
    // безопасный fit по всей новой серии.
    console.warn('alt: ценовая шкала', e);
    try {
      const fallback = window.AltChart.priceRange(state.candles, null, null);
      state.priceRange = fallback;
      state.chart.priceScale('right').setVisibleRange({ from: fallback.min, to: fallback.max });
    } catch (e2) {
      console.warn('alt: ценовая шкала, авто-fit', e2);
      try { state.chart.priceScale('right').applyOptions({ autoScale: true }); } catch (e3) { /* noop */ }
    }
  } finally {
    state.applyingPrice = false;
  }
}
function saveViewport() {
  if (!state.selected || !state.chart) return;
  state.viewports[state.selected.kind + ':' + state.selected.id] = {
    timeRange: state.chart.timeScale().getVisibleRange(),
    follow: state.followRight,
  };
}
function closedCandles(detail) {
  const asOf = detail.as_of_ms;
  return (detail.candles || []).filter((candle) => asOf == null || candle.open_time + DAY_MS <= asOf);
}
function renderChart() {
  const empty = $('alt-chart-empty');
  if (!state.chart) return;
  if (state.cardPhase === 'loading') {
    empty.classList.remove('hidden');
    empty.textContent = 'Загрузка графика…';
    clearSeries();
    return;
  }
  const detail = state.detail;
  if (!detail) {
    clearSeries();
    empty.classList.remove('hidden');
    empty.textContent = state.cardPhase === 'nodata'
      ? ((state.nodata && state.nodata.reason) || 'Для этой строки нет графика сетапа.')
      : 'Выберите монету — график и карточка обновятся.';
    $('alt-chart-note').textContent = '';
    return;
  }
  const raw = closedCandles(detail);
  state.rawCandles = raw;
  const view = state.timeframe === 'W1' ? window.AltChart.aggregateW1(raw) : raw;
  state.viewCandles = view;
  const theme = HTF.chartTheme();
  state.candles = view.map((candle) => {
    const point = {
      time: Math.floor(candle.open_time / 1000),
      open: candle.open, high: candle.high, low: candle.low, close: candle.close,
    };
    if (candle.partial) {
      point.color = theme.text;
      point.wickColor = theme.text;
    }
    return point;
  });
  state.candleByTime = new Map(state.candles.map((candle) => [candle.time, candle]));
  if (!state.candles.length) {
    clearSeries();
    empty.classList.remove('hidden');
    empty.textContent = 'Нет свечей D1 для этого источника.';
    renderNotes();
    return;
  }
  empty.classList.add('hidden');
  const pending = state.pendingViewport;
  state.pendingViewport = null;
  state.suspendPrice = true;
  state.candleSeries.setData(state.candles);
  state.candleSeries.setMarkers([]);
  try {
    if (pending && pending.followEdge) {
      state.manualPrice = pending.manualPrice || null;
      state.chart.timeScale().scrollToRealTime();
    } else if (pending && pending.timeRange) {
      state.manualPrice = pending.manualPrice || null;
      state.chart.timeScale().setVisibleRange(pending.timeRange);
    } else if (pending && pending.fit) {
      state.manualPrice = null;
      state.chart.timeScale().fitContent();
    } else if (pending && pending.fromMs != null) {
      state.manualPrice = null;
      state.chart.timeScale().setVisibleRange({ from: pending.fromMs / 1000, to: pending.toMs / 1000 });
    } else if (pending && pending.initial) {
      const viewport = window.AltChart.initialTimeRange(raw, detail.as_of_ms);
      state.manualPrice = null;
      if (viewport) state.chart.timeScale().setVisibleRange({ from: viewport.fromMs / 1000, to: viewport.toMs / 1000 });
    }
  } catch (e) { /* серия ещё не измерена */ }
  state.suspendPrice = false;
  applyPrice();
  scheduleDraw();
  renderNotes();
}
function scheduleDraw() {
  if (state.drawQueued) return;
  state.drawQueued = true;
  requestAnimationFrame(() => {
    state.drawQueued = false;
    drawLayers();
  });
}
function measureLabel(text) {
  if (!state.measure) {
    state.measure = document.createElement('canvas').getContext('2d');
  }
  state.measure.font = '11px Inter, Segoe UI, sans-serif';
  return state.measure.measureText(String(text)).width + 10;
}
function showLevel(price) {
  const current = state.priceRange || { min: 0, max: price };
  const pad = Math.max(Math.abs(price) * 0.02, 1e-8);
  state.manualPrice = {
    min: Math.max(0, Math.min(current.min, price - pad)),
    max: Math.max(current.max, price + pad),
  };
  applyPrice();
  scheduleDraw();
  saveViewport();
}
/* Привязка времени к свече текущего таймфрейма (UI-01):
   в W1 события и области якорятся на неделю, содержащую их дату D1. */
function anchorMs(ms) {
  return state.timeframe === 'W1' ? window.AltChart.weekStartMs(ms) : ms;
}
function anchorBoxMs(startMs, endMs) {
  if (state.timeframe !== 'W1') return { startMs, endMs };
  return {
    startMs: window.AltChart.weekStartMs(startMs),
    endMs: window.AltChart.weekStartMs(endMs) + 7 * DAY_MS,
  };
}
function chartContext() {
  const visible = state.chart.timeScale().getVisibleRange();
  return {
    viewFromMs: visible ? visible.from * 1000 : null,
    viewToMs: visible ? visible.to * 1000 : null,
    candles: state.viewCandles,
    mapTime: (ms) => state.chart.timeScale().timeToCoordinate(Math.floor(anchorMs(ms) / 1000)),
    mapPrice: (price) => state.candleSeries.priceToCoordinate(price),
    priceRange: state.priceRange,
    chartHeight: $('chart').clientHeight || 0,
  };
}
function paintBox(overlay, startMs, endMs, upper, lower, cls, title) {
  if (!state.chart.timeScale().getVisibleRange()) return;
  const span = anchorBoxMs(startMs, endMs);
  const rect = window.AltChart.boxRectPx({
    startMs: span.startMs, endMs: span.endMs, upper, lower,
  }, chartContext());
  if (!rect) return;
  const box = document.createElement('div');
  box.className = 'alt-zone ' + cls;
  box.title = title;
  box.style.left = rect.x + 'px';
  box.style.width = rect.width + 'px';
  box.style.top = rect.y + 'px';
  box.style.height = rect.height + 'px';
  overlay.appendChild(box);
}
function boxTitles(box) {
  if (box.kind === 'range') {
    return {
      cls: 'alt-range-box',
      title: box.shelf
        ? `База у дна ${fmtPrice(box.lower)}–${fmtPrice(box.upper)}`
        : `Аккумуляция ${fmtPrice(box.lower)}–${fmtPrice(box.upper)}`,
    };
  }
  if (box.kind === 'manip') {
    return {
      cls: 'alt-manip-box',
      title: `Манипуляция, минимум ${fmtPrice(box.minPrice)}`,
    };
  }
  return { cls: 'alt-retest-box', title: 'Область ретеста [M, U]' };
}
function layerView() {
  return {
    range: state.layers.range,
    manipulation: state.layers.manipulation,
    entries: state.layers.entries,
    eventMode: state.eventMode,
  };
}
function computeMarkerGroups() {
  const detail = state.detail;
  const visible = state.chart.timeScale().getVisibleRange();
  const projection = window.AltChart.selectChartEvents(state.normalized.events, {
    mode: state.eventMode,
    asOfMs: detail.as_of_ms,
    setupId: detail.setup_id,
    visibleFromMs: visible ? visible.from * 1000 : null,
    visibleToMs: visible ? visible.to * 1000 : null,
    includeReverse: reverseOn(),
    selectedEventKey: state.selectedEventKey,
    historyTypes: state.layers.historyTypes,
  });
  state.freshNote = projection.freshNote || '';
  state.latestStructureMs = projection.latestStructureMs;
  const items = [];
  projection.markers.forEach((event) => {
    if (event.candle_open_time_ms == null) return;
    const sec = Math.floor(anchorMs(event.candle_open_time_ms) / 1000);
    if (!state.candleByTime.has(sec)) return;
    const x = state.chart.timeScale().timeToCoordinate(sec);
    if (x == null) return;
    items.push({ event, x, position: event.position });
  });
  return window.AltChart.groupMarkers(items, { measure: measureLabel, selectedKey: state.selectedEventKey })
    .map((group) => {
      const sec = Math.floor(anchorMs(group.fromMs) / 1000);
      const bar = state.candleByTime.get(sec) || state.candles[0];
      if (!bar) return null;
      const price = group.position === 'aboveBar' ? bar.high : bar.low;
      const y = state.candleSeries.priceToCoordinate(price);
      if (y == null || group.x == null) return null;
      return Object.assign({}, group, { price, y });
    })
    .filter(Boolean);
}
function drawLayers() {
  const overlay = $('alt-overlay');
  if (!overlay || !state.chart) return;
  overlay.innerHTML = '';
  const detail = state.detail;
  if (!detail || !state.candles.length || !state.normalized) return;
  const markerGroups = computeMarkerGroups();
  const last = state.rawCandles.length ? state.rawCandles[state.rawCandles.length - 1].open_time : null;
  const collectedBoxes = window.AltChart.collectBoxes(detail, layerView(), last);
  state.retestNote = collectedBoxes.retestNote || '';
  collectedBoxes.boxes.forEach((box) => {
    const text = boxTitles(box);
    paintBox(overlay, box.startMs, box.endMs, box.upper, box.lower, text.cls, text.title);
  });
  const lastClose = state.rawCandles.length ? state.rawCandles[state.rawCandles.length - 1].close : null;
  const collected = window.AltChart.collectLevels(detail, state.layers, lastClose);
  state.targetNote = collected.targetNote || '';
  const levels = collected.levels;
  const scale = state.priceRange;
  const placed = [];
  const chips = { above: [], below: [] };
  levels.forEach((level) => {
    const where = window.AltChart.levelPlacement(level.price, scale);
    if (where === 'hidden') return;
    if (where === 'above' || where === 'below') { chips[where].push(level); return; }
    const y = state.candleSeries.priceToCoordinate(level.price);
    if (y == null) { chips[level.price > scale.max ? 'above' : 'below'].push(level); return; }
    placed.push({ level, y });
  });
  placed.sort((a, b) => a.y - b.y);
  const groups = [];
  placed.forEach((item) => {
    const prev = groups[groups.length - 1];
    if (prev && Math.abs(item.y - prev.y) < 14) prev.items.push(item);
    else groups.push({ y: item.y, items: [item] });
  });
  const pane = Math.max(0, $('chart').clientWidth - state.chart.priceScale('right').width());
  groups.forEach((group) => {
    const line = document.createElement('div');
    line.className = 'alt-level ' + group.items[0].level.cls;
    line.style.top = group.y + 'px';
    line.style.width = pane + 'px';
    overlay.appendChild(line);
    const label = document.createElement('button');
    label.type = 'button';
    label.className = 'alt-level-label';
    label.style.top = (group.y - 8) + 'px';
    label.style.left = Math.max(0, pane - 120) + 'px';
    label.textContent = group.items.length === 1
      ? group.items[0].level.name + ' ' + fmtPrice(group.items[0].level.price)
      : group.items.length + ' уровня';
    label.title = group.items.map((item) => item.level.name + ' ' + fmtPrice(item.level.price)).join('\n');
    label.onclick = () => {
      if (group.items.length === 1) return;
      label.textContent = group.items.map((item) => item.level.name + ' ' + fmtPrice(item.level.price)).join(' · ');
    };
    overlay.appendChild(label);
  });
  ['above', 'below'].forEach((where) => {
    chips[where].forEach((level, index) => {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'alt-edge ' + where;
      button.style[where === 'above' ? 'top' : 'bottom'] = (8 + index * 24) + 'px';
      button.textContent = `${level.name} ${fmtPrice(level.price)} · Показать`;
      button.onclick = () => showLevel(level.price);
      overlay.appendChild(button);
    });
  });
  markerGroups.forEach((group) => {
    const y = group.position === 'aboveBar' ? group.y - 20 : group.y + 4;
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'alt-marker' + (group.hasKey ? ' key' : '');
    button.style.left = group.x + 'px';
    button.style.top = y + 'px';
    button.textContent = group.label;
    button.onclick = (click) => {
      click.stopPropagation();
      if (group.count === 1) {
        state.selectedEventKey = group.eventKeys[0];
        renderJournal();
        scheduleDraw();
        return;
      }
      const pop = document.createElement('div');
      pop.className = 'alt-pop';
      pop.style.left = group.x + 'px';
      pop.style.top = (y + 22) + 'px';
      group.eventKeys.forEach((key) => {
        const event = state.normalized.events.find((item) => item.key === key);
        if (!event) return;
        const item = document.createElement('button');
        item.type = 'button';
        item.textContent = `${event.label} · ${fmtTime(event.available_at_ms)}`;
        item.onclick = (inner) => {
          inner.stopPropagation();
          state.selectedEventKey = key;
          renderJournal();
          scheduleDraw();
        };
        pop.appendChild(item);
      });
      overlay.appendChild(pop);
    };
    overlay.appendChild(button);
  });
  renderNotes();
}

function jumpTo(candleOpenMs) {
  if (!state.detail) return;
  state.autoNote = '';
  const spec = window.AltChart.jumpTimeRange(candleOpenMs, state.rawCandles);
  if (spec.missing) {
    const bounds = spec.loadedFromMs ? ` Фрагмент: ${fmtDate(spec.loadedFromMs)} — ${fmtDate(spec.loadedToMs)}.` : '';
    state.jumpNote = spec.reason + '.' + bounds + ' Маркер на соседнюю свечу не переносится.';
    renderNotes();
    return;
  }
  state.jumpNote = '';
  state.followRight = false;
  state.manualPrice = null;
  state.pendingViewport = spec;
  renderChart();
}
function commandViewport(spec) {
  state.manualPrice = null;
  state.autoNote = '';
  state.pendingViewport = spec;
  renderChart();
}

/* UI-01: D1/W1 — серия пересобирается из тех же D1 клиентской агрегацией,
   видимый временной охват сохраняется в датах, цена пересчитывается. */
function switchTimeframe(tf) {
  if (tf !== 'D1' && tf !== 'W1') return;
  if (tf === state.timeframe) return;
  state.timeframe = tf;
  savePrefs();
  syncControls();
  if (!state.detail || !state.chart) {
    renderChart();
    return;
  }
  const visible = state.chart.timeScale().getVisibleRange();
  state.followRight = pinnedRight();
  if (visible) commandViewport({ fromMs: visible.from * 1000, toMs: visible.to * 1000 });
  else commandViewport({ fit: true });
}

/* UI-04: экспорт PNG — снимок canvas графика + единая модель сцены
   (заливки, уровни, маркеры), заголовок с тикером, ТФ и версией правил. */
function cssVarValue(name, fallback) {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}
function exportColors() {
  return {
    range: { fill: 'rgba(227, 179, 65, 0.12)', border: 'rgba(227, 179, 65, 0.7)' },
    manip: { fill: 'rgba(240, 141, 152, 0.16)', border: 'rgba(240, 141, 152, 0.8)' },
    retest: { fill: 'rgba(98, 201, 176, 0.14)', border: 'rgba(98, 201, 176, 0.8)' },
    line: {
      range: cssVarValue('--lf-warning', '#E3B341'),
      tp: cssVarValue('--lf-positive', '#62C9B0'),
      k: cssVarValue('--lf-negative', '#F08D98'),
    },
    text: cssVarValue('--lf-text', '#E8EDF5'),
    dim: cssVarValue('--lf-text2', '#A2B0C5'),
    surface: cssVarValue('--lf-surface', '#131B28'),
    raised: cssVarValue('--lf-raised', '#1B2434'),
    border: cssVarValue('--lf-border', '#2C3A4E'),
    brand: cssVarValue('--lf-brand', '#4268DD'),
  };
}
function buildExportScene() {
  const detail = state.detail;
  const last = state.rawCandles.length ? state.rawCandles[state.rawCandles.length - 1].open_time : null;
  const lastClose = state.rawCandles.length ? state.rawCandles[state.rawCandles.length - 1].close : null;
  const ctx = chartContext();
  const boxes = window.AltChart.collectBoxes(detail, layerView(), last).boxes.map((box) => {
    const span = anchorBoxMs(box.startMs, box.endMs);
    return Object.assign({}, box, { startMs: span.startMs, endMs: span.endMs });
  });
  const levels = window.AltChart.collectLevels(detail, state.layers, lastClose).levels;
  const markers = computeMarkerGroups().map((group) => ({
    x: group.x,
    price: group.price,
    position: group.position,
    label: group.label,
    hasKey: group.hasKey,
  }));
  return window.AltChart.buildScene({
    candles: ctx.candles,
    viewFromMs: ctx.viewFromMs,
    viewToMs: ctx.viewToMs,
    mapTime: ctx.mapTime,
    mapPrice: ctx.mapPrice,
    priceRange: state.priceRange,
    chartHeight: ctx.chartHeight,
    paneWidth: Math.max(0, $('chart').clientWidth - state.chart.priceScale('right').width()),
    boxes,
    lines: levels,
    markers,
    fmtPrice,
  });
}
async function exportPng(zoom) {
  const detail = state.detail;
  if (!state.chart || !detail || !state.candles.length || !state.normalized) return;
  drawLayers();
  await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const shot = state.chart.takeScreenshot();
  const chartWidth = $('chart').clientWidth || shot.width;
  const chartHeight = $('chart').clientHeight || shot.height;
  const ratio = chartWidth ? shot.width / chartWidth : 1;
  const k = ratio * (zoom || 1);
  const colors = exportColors();
  const headerH = 40;
  const out = document.createElement('canvas');
  out.width = Math.round(shot.width * (zoom || 1));
  out.height = Math.round(shot.height * (zoom || 1) + headerH * k);
  const ctx = out.getContext('2d');
  ctx.fillStyle = colors.surface;
  ctx.fillRect(0, 0, out.width, out.height);
  const bandH = headerH * k;
  ctx.fillStyle = colors.raised;
  ctx.fillRect(0, 0, out.width, bandH);
  const asset = detail.asset || {};
  const source = detail.source || {};
  const versions = detail.versions || {};
  const lastOpen = state.rawCandles.length ? state.rawCandles[state.rawCandles.length - 1].open_time : null;
  ctx.textBaseline = 'middle';
  ctx.fillStyle = colors.text;
  ctx.font = `600 ${Math.round(13 * k)}px Inter, "Segoe UI", sans-serif`;
  ctx.fillText(
    [asset.symbol, source.symbol, source.venue, state.timeframe].filter(Boolean).join(' · '),
    Math.round(10 * k), Math.round(bandH * 0.3)
  );
  ctx.fillStyle = colors.dim;
  ctx.font = `${Math.round(11 * k)}px Inter, "Segoe UI", sans-serif`;
  ctx.fillText(
    ['Данные на ' + (lastOpen ? fmtDate(lastOpen) : '—'), 'правила ' + (versions.rule || '—')]
      .join(' · '),
    Math.round(10 * k), Math.round(bandH * 0.72)
  );
  ctx.drawImage(shot, 0, bandH, shot.width * (zoom || 1), shot.height * (zoom || 1));
  const scene = buildExportScene();
  ctx.save();
  ctx.translate(0, bandH);
  ctx.scale(k, k);
  ctx.beginPath();
  ctx.rect(0, 0, chartWidth, chartHeight);
  ctx.clip();
  scene.rects.forEach((rect) => {
    const style = colors[rect.kind] || colors.range;
    ctx.fillStyle = style.fill;
    ctx.fillRect(rect.x, rect.y, rect.width, rect.height);
    ctx.strokeStyle = style.border;
    ctx.lineWidth = 1;
    ctx.setLineDash(rect.kind === 'range' ? [] : [4, 3]);
    ctx.beginPath();
    ctx.moveTo(rect.x, rect.y + 0.5);
    ctx.lineTo(rect.x + rect.width, rect.y + 0.5);
    if (rect.kind !== 'manip') {
      ctx.moveTo(rect.x, rect.y + rect.height - 0.5);
      ctx.lineTo(rect.x + rect.width, rect.y + rect.height - 0.5);
    }
    ctx.stroke();
  });
  ctx.setLineDash([]);
  const labelFont = '11px Inter, "Segoe UI", sans-serif';
  scene.lines.forEach((line) => {
    ctx.strokeStyle = colors.line[line.kind] || colors.dim;
    ctx.lineWidth = 1;
    ctx.setLineDash([5, 4]);
    ctx.beginPath();
    ctx.moveTo(0, line.y + 0.5);
    ctx.lineTo(line.width || chartWidth, line.y + 0.5);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.font = labelFont;
    const w = ctx.measureText(line.label).width + 10;
    const x = Math.max(0, (line.width || chartWidth) - w - 8);
    ctx.fillStyle = colors.surface;
    ctx.strokeStyle = colors.border;
    ctx.fillRect(x, line.y - 15, w, 15);
    ctx.strokeRect(x, line.y - 15, w, 15);
    ctx.fillStyle = colors.text;
    ctx.textBaseline = 'middle';
    ctx.fillText(line.label, x + 5, line.y - 7.5);
  });
  scene.markers.forEach((marker) => {
    ctx.font = labelFont;
    const w = ctx.measureText(marker.label).width + 10;
    const x = marker.x - w / 2;
    ctx.fillStyle = colors.surface;
    ctx.strokeStyle = marker.key ? colors.brand : colors.border;
    ctx.lineWidth = 1;
    ctx.fillRect(x, marker.y, w, 15);
    ctx.strokeRect(x, marker.y, w, 15);
    ctx.fillStyle = colors.text;
    ctx.textBaseline = 'middle';
    ctx.fillText(marker.label, x + 5, marker.y + 7.5);
  });
  ctx.restore();
  const ticker = String(asset.symbol || 'ALT').replace(/[^A-Za-z0-9_-]+/g, '') || 'ALT';
  const datePart = lastOpen ? mskDay(lastOpen) : 'nodate';
  const name = `${ticker}_${state.timeframe}_${datePart}.png`;
  out.toBlob((blob) => {
    if (!blob) return;
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(link.href), 10000);
  }, 'image/png');
}

function applyPane() {
  $('alt-page').dataset.pane = state.pane;
  if (state.chart) scheduleDraw();
}
function applyTableMode() {
  $('alt-table-mode').classList.toggle('hidden', !state.tableMode);
  $('alt-work').classList.toggle('hidden', state.tableMode);
  $('alt-journal').classList.toggle('hidden', state.tableMode);
  $('alt-table-toggle').setAttribute('aria-pressed', state.tableMode ? 'true' : 'false');
  $('alt-table-toggle').textContent = state.tableMode ? 'К графику' : 'Таблица';
}
function renderTable() {
  const columns = state.columns.map((id) => COLUMNS.find((col) => col.id === id)).filter(Boolean);
  $('alt-thead').innerHTML = '<tr>' + columns.map((col) => `<th${col.num ? ' class="num"' : ''}>${esc(col.title)}</th>`).join('') + '</tr>';
  const rows = state.view || viewRows();
  $('alt-tbody').innerHTML = rows.map((row) => {
    const selected = sameSelection(row);
    const kind = row.setup_id != null ? 'setup' : (row.candidate_id != null ? 'candidate' : 'nodata');
    const id = kind === 'setup' ? row.setup_id : (kind === 'candidate' ? row.candidate_id : '');
    const cmc = row.asset && row.asset.cmc_id != null ? row.asset.cmc_id : '';
    return `<tr aria-selected="${selected ? 'true' : 'false'}" data-kind="${kind}" data-id="${id}" data-cmc="${cmc}">` +
      columns.map((col) => `<td${col.num ? ' class="num"' : ''}>${col.render(row)}</td>`).join('') + '</tr>';
  }).join('');
}
function renderColumns() {
  $('alt-columns').innerHTML = state.columns.concat(COLUMNS.map((col) => col.id).filter((id) => !state.columns.includes(id)))
    .filter((id, index, list) => list.indexOf(id) === index)
    .map((id) => {
      const col = COLUMNS.find((item) => item.id === id);
      const on = state.columns.includes(id);
      return `<div class="alt-col-row"><label><input type="checkbox" data-col="${id}" ${on ? 'checked' : ''}> ${esc(col.title)}</label>
        <button type="button" data-up="${id}" class="btn small">Выше</button></div>`;
    }).join('');
}
function syncControls() {
  fillFilterInputs();
  renderTabs();
  document.querySelectorAll('.alt-modes button').forEach((button) => {
    button.setAttribute('aria-pressed', button.dataset.mode === state.eventMode ? 'true' : 'false');
  });
  document.querySelectorAll('.alt-tf button').forEach((button) => {
    button.setAttribute('aria-pressed', button.dataset.tf === state.timeframe ? 'true' : 'false');
  });
  $('layer-range').checked = !!state.layers.range;
  $('layer-entries').checked = !!state.layers.entries;
  $('layer-manip').checked = !!state.layers.manipulation;
  $('layer-targets').value = state.layers.targets || 'nearest';
  $('layer-cancel').checked = !!state.layers.cancel;
  $('layer-reverse').checked = reverseOn();
  const box = $('layer-types');
  if (!box.dataset.ready) {
    box.dataset.ready = '1';
    box.insertAdjacentHTML('beforeend', HISTORY_TYPES.map(([id, label]) =>
      `<label><input type="checkbox" data-htype="${id}" checked> ${label}</label>`
    ).join(''));
  }
  const enabled = state.layers.historyTypes;
  box.querySelectorAll('[data-htype]').forEach((input) => {
    input.checked = !enabled || enabled.includes(input.dataset.htype);
  });
  applyTableMode();
  applyPane();
  renderColumns();
}

function openPanel(id, opener) {
  $(id).classList.remove('hidden');
  opener.setAttribute('aria-expanded', 'true');
  state.panelOpener = opener;
  const focus = $(id).querySelector('input, select, button');
  if (focus) focus.focus();
}
function closePanel(id) {
  $(id).classList.add('hidden');
  const opener = state.panelOpener;
  state.panelOpener = null;
  if (opener && opener.focus) opener.focus();
  document.querySelectorAll(`[aria-controls="${id}"]`).forEach((button) => button.setAttribute('aria-expanded', 'false'));
}

async function refreshFromWs() {
  const preserve = {
    followEdge: state.followRight,
    timeRange: state.followRight ? null : (state.chart ? state.chart.timeScale().getVisibleRange() : null),
    manualPrice: state.manualPrice,
  };
  const scroll = $('alt-list').scrollTop;
  const selected = state.selected ? { kind: state.selected.kind, id: state.selected.id } : null;
  state.refreshing = true;
  renderFresh();
  try {
    await loadTable();
    await loadStatus();
    if (selected && state.selected && state.selected.kind === selected.kind && state.selected.id === selected.id) {
      await selectRow(selected.kind, selected.id, { preserve, fromWs: true });
    }
  } catch (e) {
    console.warn('alt: обновление', e);
  }
  $('alt-list').scrollTop = scroll;
  state.refreshing = false;
  renderFresh();
}

function bind() {
  document.querySelectorAll('.alt-tab').forEach((button) => {
    button.onclick = () => {
      state.bucket = button.dataset.bucket;
      state.stage = '';
      $('flt-stage').value = '';
      savePrefs();
      renderTabs();
      loadTable();
    };
  });
  $('flt-venue').onchange = () => {
    state.venue = $('flt-venue').value;
    savePrefs();
    renderChips();
    renderTabs();
    loadTable();
  };
  $('alt-filters-toggle').onclick = () => {
    const panel = $('alt-filter-panel');
    if (panel.classList.contains('hidden')) openPanel('alt-filter-panel', $('alt-filters-toggle'));
    else closePanel('alt-filter-panel');
  };
  $('alt-filter-panel').onsubmit = (event) => {
    event.preventDefault();
    const errors = validateFilters();
    showErrors(errors);
    if (Object.keys(errors).length) return;
    state.rankMin = $('flt-rank-min').value.trim();
    state.rankMax = $('flt-rank-max').value.trim();
    state.ageMin = $('flt-age-min').value.trim();
    state.ageMax = $('flt-age-max').value.trim();
    state.ddMin = $('flt-dd-min').value.trim();
    state.ddMax = $('flt-dd-max').value.trim();
    state.structure = $('flt-structure').value;
    state.stage = $('flt-stage').value;
    if (state.stage) state.bucket = state.stage;
    savePrefs();
    renderTabs();
    loadTable();
  };
  $('alt-reset').onclick = () => {
    state.venue = '';
    state.rankMin = state.rankMax = state.ageMin = state.ageMax = state.ddMin = state.ddMax = '';
    state.structure = '';
    state.search = '';
    state.stage = '';
    if (state.bucket === 'mature' || state.bucket === 'forming') state.bucket = 'eligible';
    showErrors({});
    fillFilterInputs();
    savePrefs();
    renderTabs();
    loadTable();
  };
  $('alt-chips').onclick = (event) => {
    const button = event.target.closest('[data-chip]');
    if (!button) return;
    const chip = button.dataset.chip;
    if (chip === 'venue') state.venue = '';
    if (chip === 'rank') { state.rankMin = ''; state.rankMax = ''; }
    if (chip === 'age') { state.ageMin = ''; state.ageMax = ''; }
    if (chip === 'dd') { state.ddMin = ''; state.ddMax = ''; }
    if (chip === 'structure') state.structure = '';
    if (chip === 'stage') {
      state.stage = '';
      if (state.bucket === 'mature' || state.bucket === 'forming') state.bucket = 'eligible';
    }
    if (chip === 'search') state.search = '';
    fillFilterInputs();
    savePrefs();
    renderTabs();
    loadTable();
  };
  $('alt-search').oninput = () => {
    state.search = $('alt-search').value;
    savePrefs();
    renderChips();
    renderList();
  };
  $('alt-sort').onchange = () => {
    state.sort = $('alt-sort').value;
    savePrefs();
    renderList();
  };
  $('alt-list').onclick = (event) => {
    const button = event.target.closest('.alt-row');
    if (!button) return;
    const row = (state.view || []).find((item) => {
      if (button.dataset.kind === 'setup') return String(item.setup_id) === button.dataset.id;
      if (button.dataset.kind === 'candidate') return String(item.candidate_id) === button.dataset.id;
      return item.kind === 'nodata' && String(item.asset.cmc_id) === button.dataset.id;
    });
    if (!row) return;
    if (row.kind === 'nodata') showNodata(row);
    else selectRow(button.dataset.kind, Number(button.dataset.id));
  };
  $('alt-list').onkeydown = (event) => {
    if (!['ArrowDown', 'ArrowUp', 'Enter'].includes(event.key)) return;
    const items = [...$('alt-list').querySelectorAll('.alt-row')];
    if (!items.length) return;
    const index = items.findIndex((item) => item === document.activeElement);
    if (event.key === 'Enter') {
      event.preventDefault();
      $('alt-card').classList.add('open');
      const focus = $('alt-card-body').querySelector('button, summary');
      if (focus) focus.focus();
      else { $('alt-card').tabIndex = -1; $('alt-card').focus(); }
      return;
    }
    event.preventDefault();
    const next = Math.max(0, Math.min(items.length - 1, (index < 0 ? 0 : index) + (event.key === 'ArrowDown' ? 1 : -1)));
    items[next].click();
  };
  $('alt-retry-list').onclick = () => loadTable();
  $('range-preview-btn').onclick = previewRangeEdit;
  $('range-save-btn').onclick = saveRangeEdit;
  $('range-restore-btn').onclick = restoreAutoRange;
  $('range-cancel-btn').onclick = () => $('alt-range-editor').close();
  ['range-edit-l', 'range-edit-u', 'range-edit-start', 'range-edit-end'].forEach((id) => {
    $(id).addEventListener('input', () => { $('range-save-btn').disabled = true; });
  });
  $('alt-reveal').onclick = () => showInList();
  $('alt-back').onclick = () => {
    state.pane = 'list';
    applyPane();
    const current = $('alt-list').querySelector('[aria-selected="true"]');
    if (current) current.focus();
    else $('alt-list').focus();
  };
  document.querySelectorAll('.alt-modes button').forEach((button) => {
    button.onclick = () => {
      state.eventMode = button.dataset.mode;
      savePrefs();
      syncControls();
      scheduleDraw();
      renderNotes();
    };
  });
  document.querySelectorAll('.alt-tf button').forEach((button) => {
    button.onclick = () => switchTimeframe(button.dataset.tf);
  });
  $('alt-auto').onclick = () => {
    state.followRight = false;
    commandViewport({ fit: true });
    const hist = state.detail && state.detail.candle_history;
    state.autoNote = hist && (hist.truncated || hist.windowed)
      ? '«Авто» показал все загруженные свечи, но загружен фрагмент истории пары, а не вся рыночная история.'
      : '';
    renderNotes();
  };
  $('alt-png').onclick = () => { exportPng(1); };
  $('alt-png2').onclick = () => { exportPng(2); };
  $('alt-layers-toggle').onclick = () => {
    if ($('alt-layers').classList.contains('hidden')) openPanel('alt-layers', $('alt-layers-toggle'));
    else closePanel('alt-layers');
  };
  const layerChange = () => {
    state.layers.range = $('layer-range').checked;
    state.layers.entries = $('layer-entries').checked;
    state.layers.manipulation = $('layer-manip').checked;
    state.layers.targets = $('layer-targets').value;
    state.layers.cancel = $('layer-cancel').checked;
    state.layers.reverse = $('layer-reverse').checked;
    state.layers.reverseUser = true;
    const picked = [...$('layer-types').querySelectorAll('[data-htype]')].filter((input) => input.checked).map((input) => input.dataset.htype);
    state.layers.historyTypes = picked.length === HISTORY_TYPES.length ? null : picked;
    savePrefs();
    scheduleDraw();
  };
  $('alt-layers').addEventListener('change', layerChange);
  $('alt-180').onclick = () => {
    state.rangeNote = '';
    state.followRight = true;
    const viewport = window.AltChart.initialTimeRange(state.rawCandles, state.detail && state.detail.as_of_ms);
    commandViewport(viewport || { initial: true });
  };
  $('alt-range').onclick = () => {
    if (!state.detail) return;
    const anchor = state.detail.anchors && state.detail.anchors.start && state.detail.anchors.start.open_time;
    const spec = window.AltChart.rangeTimeRange(state.rawCandles, anchor == null ? null : anchor);
    state.rangeNote = spec.note || '';
    state.followRight = false;
    commandViewport(spec.missing ? { initial: true } : spec);
  };
  $('alt-all-history').onclick = () => {
    state.rangeNote = '';
    state.followRight = false;
    commandViewport({ fit: true });
  };
  $('alt-expand').onclick = () => {
    const work = $('alt-work');
    const on = !work.classList.contains('chart-expanded');
    work.classList.toggle('chart-expanded', on);
    $('alt-expand').setAttribute('aria-pressed', on ? 'true' : 'false');
    scheduleDraw();
  };
  $('alt-card-toggle').onclick = () => {
    const card = $('alt-card');
    const open = !card.classList.contains('open');
    card.classList.toggle('open', open);
    $('alt-card-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      state.panelOpener = $('alt-card-toggle');
      const focus = $('alt-card-body').querySelector('button, summary');
      if (focus) focus.focus();
    }
  };
  $('alt-card-close').onclick = () => {
    if (window.innerWidth <= 767) $('alt-back').click();
    else {
      $('alt-card').classList.remove('open');
      $('alt-card-toggle').setAttribute('aria-expanded', 'false');
      if (state.panelOpener && state.panelOpener.focus) state.panelOpener.focus();
      else $('alt-card-toggle').focus();
    }
  };
  $('alt-collapse-list').onclick = () => $('alt-work').classList.toggle('list-collapsed');
  $('alt-collapse-card').onclick = () => $('alt-work').classList.toggle('card-collapsed');
  $('alt-proc-toggle').onclick = () => {
    const panel = $('alt-proc');
    const open = panel.classList.contains('hidden');
    panel.classList.toggle('hidden', !open);
    $('alt-proc-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  $('alt-recalc').onclick = async () => {
    const button = $('alt-recalc');
    button.disabled = true;
    try {
      await api('/api/alt/recalc', { method: 'POST', body: '{}' });
      $('alt-running').classList.remove('hidden');
    } catch (e) {
      alert('Пересчёт не запущен: ' + e.message);
    } finally {
      button.disabled = false;
    }
  };
  $('journal-type').onchange = () => renderJournal();
  $('journal-from').onchange = () => renderJournal();
  $('journal-to').onchange = () => renderJournal();
  $('alt-table-toggle').onclick = () => {
    state.tableMode = !state.tableMode;
    savePrefs();
    applyTableMode();
    if (state.tableMode) renderTable();
    else if (state.chart) scheduleDraw();
  };
  $('alt-table-back').onclick = () => {
    state.tableMode = false;
    savePrefs();
    applyTableMode();
  };
  $('alt-columns-toggle').onclick = () => {
    if ($('alt-columns').classList.contains('hidden')) openPanel('alt-columns', $('alt-columns-toggle'));
    else closePanel('alt-columns');
  };
  $('alt-columns').onclick = (event) => {
    const up = event.target.closest('[data-up]');
    if (up) {
      const id = up.dataset.up;
      const index = state.columns.indexOf(id);
      if (index > 0) {
        const swap = state.columns[index - 1];
        state.columns[index - 1] = id;
        state.columns[index] = swap;
        savePrefs();
        renderColumns();
        renderTable();
      }
    }
  };
  $('alt-columns').onchange = (event) => {
    const input = event.target.closest('[data-col]');
    if (!input) return;
    const id = input.dataset.col;
    if (input.checked) {
      if (!state.columns.includes(id)) state.columns.push(id);
    } else state.columns = state.columns.filter((item) => item !== id);
    if (!state.columns.length) state.columns = ['asset'];
    savePrefs();
    renderTable();
  };
  $('alt-tbody').onclick = (event) => {
    const rowEl = event.target.closest('tr[data-kind]');
    if (!rowEl) return;
    const row = (state.view || []).find((item) => {
      if (rowEl.dataset.kind === 'setup') return String(item.setup_id) === rowEl.dataset.id;
      if (rowEl.dataset.kind === 'candidate') return String(item.candidate_id) === rowEl.dataset.id;
      return item.kind === 'nodata' && String(item.asset.cmc_id) === rowEl.dataset.cmc;
    });
    state.tableMode = false;
    savePrefs();
    applyTableMode();
    if (!row) return;
    if (row.kind === 'nodata') showNodata(row);
    else selectRow(rowEl.dataset.kind, Number(rowEl.dataset.id));
  };
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const tag = document.activeElement && document.activeElement.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') return;
    if (event.target.closest && event.target.closest('#chart-container')) return;
    if (!$('alt-layers').classList.contains('hidden')) { closePanel('alt-layers'); return; }
    if (!$('alt-filter-panel').classList.contains('hidden')) { closePanel('alt-filter-panel'); return; }
    if (!$('alt-columns').classList.contains('hidden')) { closePanel('alt-columns'); return; }
    if ($('alt-card').classList.contains('open') && window.innerWidth < 1440) {
      $('alt-card-close').click();
      return;
    }
    if (state.pane === 'chart' && window.innerWidth < 1100) $('alt-back').click();
  });
  window.addEventListener('lf-theme', paintChartTheme);
  window.addEventListener('pagehide', destroyChart);
}

async function init() {
  await HTF.ensureToken();
  loadPrefs();
  syncControls();
  initChart();
  bind();
  try {
    await Promise.all([loadTable(), loadStatus()]);
  } catch (e) {
    state.listPhase = 'error';
    state.listError = e.message || '';
    renderList();
  }
  const params = new URL(location.href).searchParams;
  if (params.get('setup')) await openExact('setup', Number(params.get('setup')));
  else if (params.get('candidate')) await openExact('candidate', Number(params.get('candidate')));
  else if (params.get('asset')) await selectByAsset(Number(params.get('asset')));
  else if (state.savedSelected && (state.savedSelected.kind === 'setup' || state.savedSelected.kind === 'candidate')) {
    await selectRow(state.savedSelected.kind, Number(state.savedSelected.id), { optional: true });
  }
  if (!state.selected && !state.nodata && state.cardPhase !== 'missing') {
    const first = viewRows().find((row) => row.setup_id != null || row.candidate_id != null);
    if (first) await selectRow(first.setup_id != null ? 'setup' : 'candidate', first.setup_id != null ? first.setup_id : first.candidate_id);
  }
  HTF.connectWs((msg) => {
    if (msg && msg.type === 'alt') {
      clearTimeout(state.reloadTimer);
      state.reloadTimer = setTimeout(refreshFromWs, 400);
    }
  });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
