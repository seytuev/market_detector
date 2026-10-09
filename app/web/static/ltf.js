/* LTF «Текущий сетап» — окно текущей ситуации по активу (ТЗ «LTF Current
   Setup» §4–§8, §12, §14). Рабочий объект — инструмент и его выбранный
   HTF-контекст; этап, сценарий, диапазон и подходящие зоны берутся с
   сервера (read model /api/ltf/instruments/{id}/current), фронт «текущий
   сценарий» самостоятельно не восстанавливает. График, карточка, счётчик
   и таблица рендерятся из одного снимка (один state_version).
   Vanilla JS + lightweight-charts; слои — DOM-overlay поверх графика.
   Общие helper'ы — common.js (window.HTF). */

'use strict';

const { api, fmtPrice, fmtTime, esc, tradingviewUrl } = window.HTF;
const $ = (id) => document.getElementById(id);
const H1_MS = 3_600_000;
const DAY_MS = 86_400_000;

// ---------------------------------------------------------------------------
// Состояние
// ---------------------------------------------------------------------------

const state = {
  assets: [],                // /api/ltf/instruments — одна строка на instrument
  instrumentId: null,
  current: null,             // InstrumentCurrentView (снимок, state_version)
  layers: null,              // /chart слои выбранного контекста
  candles: [],
  journal: [],
  historyRows: null,         // ленивая загрузка вкладки «История»
  historyScenarioId: null,
  excludedRows: null,        // лениво — причины «нет подходящих зон»
  contextsCache: new Map(),  // instrument_id -> снимок /current (раскрытие в списке)
  bottomTab: 'eligible',
  mode: 'now',               // now | history
  sortMode: 'alpha',         // alpha | priority (L06; в localStorage не сохраняется)
  scaleDays: 3,
  layerToggles: { structure: false, liquidity: false, excluded: false, history: false, provisional: false },
  provUserSet: false,          // пользователь сам трогал переключатель §16.2
  selectedEntryZoneId: null,
  expandedEntryId: null,
  journalZoneFilter: null,
  priceFocus: null,
  lastPrice: null,
  chart: null,
  candleSeries: null,
  priceLine: null,
  ws: null,
  reloadTimer: null,
  currentReqSeq: 0,          // D01: номер запроса снимка — поздний ответ
                             // ранее выбранного инструмента экран не перезаписывает
  appliedSeq: 0,             // F04: state_seq последнего применённого снимка —
                             // WS-сообщения старше него (позднее эхо) игнорируются
  reviewFlash: null,
  inspectorDismissed: true,
  h1Layers: null,
  h1LoadError: false,
  h1Req: 0,
  h1SelectedEventId: null,
  h1SelectedZoneId: null,
  h1HistoryTimer: null,
  h1Settings: null,
};

// Локальные fallback-формулировки; актуальные — GET /api/labels (texts_ru)
let LTF_KIND_RU = {
  bos: 'слом BOS', sms: 'слом SMS', range_ready: 'диапазон готов',
  entries_ready: 'новые Entry Zones', touch: 'касание Entry Zone',
  sweep_confirmed: 'снятие подтверждено', sweep_failed: 'исход снятия уровня',
  cancellation: 'отмена сценария', note: 'заметка',
};
let LTF_OBS_STATE_RU = {
  waiting_structure: 'ожидание слома', active: 'активно',
  paused_data: 'пауза: данные', closed_by_parent: 'закрыто: HTF инвалидирована',
  closed_by_user: 'закрыто вручную',
};
let LTF_CANCEL_RU = {
  reverse_bos: 'обратный BOS H1', reverse_sms: 'обратный SMS H1',
  HTF_INVALIDATED: 'инвалидация HTF', manual: 'ручное завершение',
};
const DATA_QUALITY_RU = {
  live: 'данные актуальны',
  stale: 'данные устарели',
  gap: 'пропуск в данных',
  replaying: 'восстановление истории',
  ambiguous: 'данные неоднозначны',
};
// data_state сервера (§14): «данные поступают» и «свеча обработана» — раздельно
const DATA_STATE_RU = {
  ok: 'данные поступают',
  stale: 'данные устарели',
  data_pending: 'данных недостаточно',
  replaying: 'восстановление истории',
};
const DATA_STATE_REASON_RU = {
  no_quote: 'нет котировки',
  no_h1_candles: 'нет свечей H1',
  quote_stale: 'котировка устарела',
  h1_stale: 'свечи H1 устарели',
  source_stale: 'источник недоступен',
  replay_in_progress: 'идёт догрузка и пересчёт',
  processing_lag: 'Расчёт отстаёт',
  history_gap: 'Разрыв истории',
};
// reason-коды пригодности (§10/§12) — серверные стабильные коды
const REASON_RU = {
  ok: 'подходит',
  outside_pd: 'вне Premium/Discount',
  tested_too_deep: 'тест ≥ 90% глубины',
  type_disabled: 'тип отключён настройкой',
  invalid: 'зона невалидна',
  swept_level: 'уровень снят',
  level_broken: 'уровень пройден без возврата',
  fvg_filled: 'FVG перекрыт полностью',
  origin_unresolved: 'принадлежность движению не доказана',
  range_pending: 'диапазон ещё не подтверждён',
};
const ENTRY_STATE_RU = {
  fresh: 'свежая', tested: 'протестирована',
  out_of_range: 'вне диапазона', invalid: 'невалидна',
};
const FILL_STATUS_RU = {
  open: 'не перекрыт', partially_filled: 'перекрыт частично',
  filled: 'перекрыт полностью',
};
const LIQUIDITY_STATE_RU = {
  awaiting_close: 'снятие ожидает закрытия H1',
  confirmed: 'снятие с возвратом',
  failed: 'уровень пройден без возврата',
  equal_close: 'закрытие ровно на уровне — исход неопределён',
};
let LTF_REVIEW_DECISION_RU = {
  correct: 'размечено верно', now_irrelevant: 'сейчас неактуально',
  fix_boundaries: 'исправить границы', wrong_base: 'другое основание',
  wrong_type: 'неверный тип/форма', no_context: 'нет контекста', wrong: 'отклонена',
};
let LTF_REVIEW_REASON_RU = {
  wrong_movement: 'не то движение (movement)',
  wrong_eligible: 'не должна была попасть в сценарий',
  wrong_level: 'не тот экстремум BSL/SSL',
  wrong_fvg_base: 'не та свеча-основание FVG',
  wrong_ob_base: 'не та свеча-основание OB',
  late_zone: 'зона появилась слишком поздно',
  duplicate_zone: 'дублирует существующую зону',
  no_context: 'не могу оценить',
};
// L05: основание выбора контекста (коды сервера) — автовыбор есть
// навигационная политика, не доказательство силы сценария
const CONTEXT_BASIS_RU = {
  manual: 'Выбран вручную',
  price_inside: 'Цена внутри зоны',
  nearest: 'Ближайшая к цене зона',
  last_scenario: 'Последний действующий сценарий',
  last_contact: 'Последний контакт с зоной',
};
// L06: группы внимания по убыванию приоритета (зеркалит серверный
// ATTENTION_ORDER); внимание ≠ вероятность успеха сетапа
const ATTENTION_ORDER = ['review', 'price_in_zone', 'eligible', 'awaiting', 'data_problem', 'none'];
const ATTENTION_RU = {
  review: 'Требует проверки',
  price_in_zone: 'Цена в зоне',
  eligible: 'Есть подходящие зоны',
  awaiting: 'Ожидается структура',
  data_problem: 'Проблема данных',
};

// U01-lite: единый ключ выбранного инструмента для обеих страниц (HTF/LTF)
const INSTRUMENT_KEY = 'htf:instrument';
function readSavedInstrument() {
  const saved = Number(localStorage.getItem(INSTRUMENT_KEY));
  if (saved) return saved;
  // миграция со старого ключа LTF-страницы — читаем и переносим один раз
  const legacy = localStorage.getItem('ltf:instrument');
  if (legacy) {
    localStorage.setItem(INSTRUMENT_KEY, legacy);
    localStorage.removeItem('ltf:instrument');
    return Number(legacy) || null;
  }
  return null;
}

function dirWord(d) {
  return d === 'bull' ? 'рост' : d === 'bear' ? 'снижение' : d === 'mixed' ? 'разные контексты' : '—';
}
function dirArrow(d) {
  return d === 'bull' ? '▲' : d === 'bear' ? '▼' : d === 'mixed' ? '▲▼' : '·';
}
function ctxLabel(type, tf, direction) {
  // §5: подпись вида «FVG D1 · медвежий»
  const dirRu = direction === 'bull' ? 'бычий' : direction === 'bear' ? 'медвежий' : '';
  return `${(type || '').toUpperCase()} ${tf || ''}${dirRu ? ' · ' + dirRu : ''}`;
}

function isCompactLtf() {
  return window.matchMedia('(max-width: 1279px)').matches;
}
function isMobileLtf() {
  return window.matchMedia('(max-width: 900px)').matches;
}
function openInspector() {
  if (!isCompactLtf() || isMobileLtf()) return;
  $('ltf-inspector').classList.add('open');
  state.inspectorDismissed = false;
}
function closeInspector() {
  $('ltf-inspector').classList.remove('open');
  state.inspectorDismissed = true;
}

// ---------------------------------------------------------------------------
// Загрузка данных
// ---------------------------------------------------------------------------

async function loadLabels() {
  try {
    const l = await api('/api/labels');
    if (l.ltf_event_kinds) LTF_KIND_RU = { ...LTF_KIND_RU, ...l.ltf_event_kinds };
    if (l.ltf_observation_states) LTF_OBS_STATE_RU = { ...LTF_OBS_STATE_RU, ...l.ltf_observation_states };
    if (l.ltf_cancellation_reasons) LTF_CANCEL_RU = { ...LTF_CANCEL_RU, ...l.ltf_cancellation_reasons };
    if (l.ltf_review_decisions) LTF_REVIEW_DECISION_RU = { ...LTF_REVIEW_DECISION_RU, ...l.ltf_review_decisions };
    if (l.ltf_review_reasons) LTF_REVIEW_REASON_RU = { ...LTF_REVIEW_REASON_RU, ...l.ltf_review_reasons };
  } catch (e) {
    console.warn('labels: локальный fallback', e);
  }
}

async function loadAssets() {
  applyAssets(await api('/api/ltf/instruments'));
}

function applyAssets(payload) {
  state.assets = payload.instruments;
  const sel = $('ltf-instrument');
  const prev = state.instrumentId;
  sel.innerHTML = '';
  for (const a of state.assets) {
    const opt = document.createElement('option');
    opt.value = a.instrument.id;
    opt.textContent = `${a.instrument.symbol} · ${a.instrument.venue}`;
    sel.appendChild(opt);
  }
  if (prev && state.assets.some((a) => a.instrument.id === prev)) sel.value = prev;
  // фильтр бирж — из фактического списка
  const venueSel = $('flt-venue');
  const venuePrev = venueSel.value;
  const venues = [...new Set(state.assets.map((a) => a.instrument.venue))].sort();
  venueSel.innerHTML = '<option value="">Биржа: все</option>' +
    venues.map((v) => `<option>${esc(v)}</option>`).join('');
  venueSel.value = venues.includes(venuePrev) ? venuePrev : '';
  const stageSel = $('flt-stage');
  const stagePrev = stageSel.value;
  const stages = [...new Set(state.assets.map((a) => a.stage))].sort();
  stageSel.innerHTML = '<option value="">Этап: все</option>' +
    stages.map((s) => `<option>${esc(s)}</option>`).join('');
  stageSel.value = stages.includes(stagePrev) ? stagePrev : '';
  renderAssetsList();
}

function assetVisible(a) {
  const sym = $('flt-symbol').value.trim().toLowerCase();
  if (sym && !a.instrument.symbol.toLowerCase().includes(sym)) return false;
  const venue = $('flt-venue').value;
  if (venue && a.instrument.venue !== venue) return false;
  const stage = $('flt-stage').value;
  if (stage && a.stage !== stage) return false;
  const dir = $('flt-direction').value;
  if (dir && a.direction !== dir) return false;
  return true;
}

function renderAssetsList() {
  const el = $('ltf-obs-list');
  const items = state.assets.filter(assetVisible);
  // L06: автоматически по приоритету НЕ пересортировываем — порядок строк
  // стабилен при обновлениях; сортировка — только по кнопке «По приоритету».
  // Array.prototype.sort в JS стабилен: внутри группы остаётся алфавитный
  // порядок сервера.
  if (state.sortMode === 'priority') {
    const rank = (a) => {
      const i = ATTENTION_ORDER.indexOf(a.attention || 'none');
      return i === -1 ? ATTENTION_ORDER.length : i;
    };
    items.sort((a, b) => rank(a) - rank(b));
  }
  if (!items.length) {
    el.innerHTML = emptyStateHtml('По фильтрам активов нет.', 'ltf-flt-reset', 'Сбросить фильтр');
    $('ltf-flt-reset').onclick = () => {
      $('flt-symbol').value = '';
      $('flt-venue').value = '';
      $('flt-stage').value = '';
      $('flt-direction').value = '';
      // change на контейнере фильтров обновит и счётчик, и список
      $('ltf-filters').dispatchEvent(new Event('change'));
    };
    return;
  }
  el.innerHTML = items.map((a) => {
    const ins = a.instrument;
    const cls = ['ltf-obs-item', 'ltf-asset'];
    if (ins.id === state.instrumentId) cls.push('selected');
    const ctx = a.htf_context;
    const ds = a.data_state || {};
    return `
      <div class="${cls.join(' ')}" data-instrument-id="${ins.id}">
        <div class="ltf-obs-head">
          <b>${esc(ins.symbol)}</b>
          <span class="dir ${esc(a.direction || '')}">${dirArrow(a.direction)} ${ctx ? esc(ctx.type.toUpperCase()) + ' ' + esc(ctx.timeframe) : ''}</span>
        </div>
        <div class="ltf-obs-sub">
          <span class="stage-badge">${esc(a.stage)}</span>
          ${a.attention && a.attention !== 'none' ? `<span class="badge attn-${esc(a.attention)}" title="${esc(a.attention_reason || '')}">${esc(ATTENTION_RU[a.attention] || a.attention)}</span>` : ''}
          ${ds.state && ds.state !== 'ok' ? `<span class="badge dq-stale">${esc(DATA_STATE_RU[ds.state] || ds.state)}</span>` : ''}
        </div>
        <div class="ltf-asset-meta">
          <span title="Число подходящих Entry Zone">Зоны: <b class="asset-eligible">${a.eligible_count}</b></span>
          <span class="ltf-obs-time" title="Время последнего значимого события">${a.last_event_at ? fmtTime(a.last_event_at) : '—'}</span>
        </div>
        ${a.contexts_count > 0 ? `<button class="ctx-toggle" data-ctx-ins="${ins.id}" aria-expanded="false">Контексты: ${a.contexts_count}</button><div class="ctx-list hidden" data-ctx-list="${ins.id}"></div>` : ''}
      </div>`;
  }).join('');
  for (const div of el.querySelectorAll('.ltf-asset')) {
    div.onclick = () => {
      $('ltf-obs-panel').classList.remove('open');
      selectInstrument(Number(div.dataset.instrumentId));
    };
  }
  for (const btn of el.querySelectorAll('.ctx-toggle')) {
    btn.onclick = async (e) => {
      e.stopPropagation();
      const iid = Number(btn.dataset.ctxIns);
      const list = el.querySelector(`[data-ctx-list="${iid}"]`);
      const open = list.classList.contains('hidden');
      list.classList.toggle('hidden');
      btn.setAttribute('aria-expanded', String(open));
      if (open) await renderContextsList(iid, list);
    };
  }
}

async function renderContextsList(instrumentId, container) {
  container.innerHTML = '<div class="ltf-empty">Загрузка…</div>';
  let view = state.contextsCache.get(instrumentId);
  if (!view || instrumentId === state.instrumentId) {
    view = instrumentId === state.instrumentId && state.current
      ? state.current
      : await api(`/api/ltf/instruments/${instrumentId}/current`);
    state.contextsCache.set(instrumentId, view);
  }
  const contexts = view.contexts || [];
  if (!contexts.length) {
    container.innerHTML = '<div class="ltf-empty">Действующих контекстов нет.</div>';
    return;
  }
  // L05: конфликт направлений — обе зоны видны и помечены
  const conflict = new Set(contexts.map((c) => c.direction)).size > 1;
  container.innerHTML =
    (conflict
      ? '<div class="ctx-conflict-note">Конфликт контекстов: активны зоны противоположных направлений</div>'
      : '') +
    contexts.map((c) => {
    const z = c.parent_zone || {};
    const selected = c.observation_id === view.selected_context_id;
    // L05: нейтральная подпись основания (навигационная политика сервера)
    const basis = selected && view.selected_context_basis
      ? `<div class="ctx-basis">Показан контекст: ${esc(CONTEXT_BASIS_RU[view.selected_context_basis] || view.selected_context_basis)}</div>`
      : '';
    // §4.2: границы, направление, последнее касание, актуальность — даты
    // подписаны по смыслу; дата создания OB не выдаётся за время контакта
    return `<div class="ctx-item${selected ? ' selected' : ''}${conflict ? ' conflict' : ''}" data-ctx-obs="${c.observation_id}" data-ctx-ins="${instrumentId}">
      <div><b>${esc(ctxLabel(z.type, z.timeframe, c.direction))}</b>
        <span class="badge">${esc(c.parent_validity === 'active' ? 'актуален' : (c.parent_validity || '—'))}</span>
        ${conflict ? '<span class="badge ctx-conflict" title="Активны контексты противоположных направлений">конфликт</span>' : ''}</div>
      <div class="ctx-bounds">[${fmtPrice(z.lower)}–${fmtPrice(z.upper)}]</div>
      <div class="ctx-touch">последнее касание: ${c.last_touch_at ? fmtTime(c.last_touch_at) : '—'}</div>
      ${basis}
    </div>`;
  }).join('');
  for (const item of container.querySelectorAll('.ctx-item')) {
    item.onclick = async (e) => {
      e.stopPropagation();
      const iid = Number(item.dataset.ctxIns);
      const obsId = Number(item.dataset.ctxObs);
      await api(`/api/ltf/instruments/${iid}/select-context`, {
        method: 'POST', body: JSON.stringify({ observation_id: obsId }),
      });
      state.contextsCache.delete(iid);
      if (iid === state.instrumentId) await reloadCurrent({ keepRange: true });
      else await selectInstrument(iid);
      await loadAssets();
    };
  }
}

// ---------------------------------------------------------------------------
// Текущий снимок инструмента (§14): один ответ — график/карточка/счётчик/таблица
// ---------------------------------------------------------------------------

// U01-lite: ссылка LTF→HTF несёт выбранный инструмент (токен, как и раньше,
// common.js заберёт из URL в localStorage и подчистит адресную строку)
function updateHtfLink() {
  const link = $('lnk-htf');
  if (!link) return;
  const url = new URL('/', location.origin);
  url.searchParams.set('token', window.HTF.getToken());
  if (state.instrumentId) url.searchParams.set('instrument', String(state.instrumentId));
  link.href = url.pathname + url.search;
  // таб режима «Контекст» (рабочее место desk) — тот же инструмент и токен
  const desk = $('lnk-desk');
  if (desk) desk.href = url.pathname + url.search + '#desk';
}

async function selectInstrument(id) {
  if (!id) return;
  state.instrumentId = id;
  state.h1SelectedEventId = null;
  state.h1SelectedZoneId = null;
  state.h1Req += 1;
  localStorage.setItem(INSTRUMENT_KEY, String(id));
  updateHtfLink();
  $('ltf-instrument').value = id;
  state.historyRows = null;
  state.excludedRows = null;
  state.journalZoneFilter = null;
  state.expandedEntryId = null;
  state.selectedEntryZoneId = null;
  state.priceFocus = null;
  state.contextsCache.delete(id);
  const url = new URL(location.href);
  url.searchParams.set('instrument', String(id));
  url.searchParams.delete('obs');
  history.replaceState(null, '', url.pathname + url.search);
  renderAssetsList();
  await reloadCurrent({ keepRange: false });
}

async function reloadCurrent({ keepRange }) {
  const id = state.instrumentId;
  // D01: защита от поздних ответов при быстром переключении активов —
  // применяется только последний запрос и только для текущего инструмента
  const req = ++state.currentReqSeq;
  const stale = () => req !== state.currentReqSeq || id !== state.instrumentId;
  const keep = keepRange && state.chart ? state.chart.timeScale().getVisibleLogicalRange() : null;
  const keepFocus = keepRange ? state.priceFocus : null;
  // F04/§13: снимок /current, слои (chart/journal) и список активов связаны
  // одной версией state_version (общий счётчик state_seq); расхождение
  // версий означает, что запись вклинилась между запросами, — пакет
  // перечитывается целиком, левая карточка не рисует счётчик чужого состояния
  const loadBundle = async () => {
    const view = await api(`/api/ltf/instruments/${id}/current`);
    if (stale()) return null;
    const obsId = view.selected_context_id;
    const h1q = window.H1Layers
      ? window.H1Layers.queryString(Object.assign({ context_id: obsId || undefined }, visibleWindowMs()))
      : '';
    const [layers, candles, journal, assets, h1] = await Promise.all([
      obsId ? api(`/api/ltf/observations/${obsId}/chart`) : Promise.resolve(null),
      api(`/api/candles?instrument_id=${id}&timeframe=H1&limit=2500`),
      obsId ? api(`/api/ltf/observations/${obsId}/journal`) : Promise.resolve(null),
      api('/api/ltf/instruments'),
      api(`/api/ltf/instruments/${id}/structure?${h1q}`).catch(() => ({ __error: true })),
    ]);
    if (stale()) return null;
    return { view, layers, candles, journal, assets, h1 };
  };
  const mismatch = (b) =>
    b.assets.state_version !== b.view.state_version ||
    (b.layers && b.layers.state_version !== b.view.state_version) ||
    (b.journal && b.journal.state_version !== b.view.state_version);
  let bundle = await loadBundle();
  if (!bundle) return;
  if (mismatch(bundle)) {
    bundle = await loadBundle(); // один повтор всего пакета
    if (!bundle) return;
    if (mismatch(bundle)) {
      // запись идёт непрерывно — применяем последний снимок /current и его
      // слои; следующее WS-сообщение поднимет версию ещё раз
      console.debug('ltf: state_version пакета расходятся после повтора',
        { current: bundle.view.state_version,
          assets: bundle.assets.state_version,
          layers: bundle.layers && bundle.layers.state_version,
          journal: bundle.journal && bundle.journal.state_version });
    }
  }
  state.current = bundle.view;
  state.lastPrice = bundle.view.price;
  state.layers = bundle.layers;
  state.h1Req += 1;
  state.h1LoadError = !!(bundle.h1 && bundle.h1.__error);
  state.h1Layers = state.h1LoadError
    ? { detected_zones: [], structural_events: [], layer_status: { zones: { state: 'error', total: 0 } }, snapshot: {} }
    : bundle.h1;
  state.candles = bundle.candles;
  state.journal = bundle.journal ? bundle.journal.events : [];
  state.appliedSeq = bundle.view.state_version;
  state.historyRows = null;
  state.excludedRows = null;
  applyAssets(bundle.assets);
  renderTopbar();
  syncProvisionalToggle();
  renderCard();
  renderEntries();
  renderJournal();
  renderHistoryCount();
  setCandles({ visibleRange: keep, focus: keepFocus });
  paintH1Markers();
  requestAnimationFrame(() => requestAnimationFrame(drawLtfLayers));
}

// §16.2 (предлагаемый режим): переключатель слоя виден только при включённой
// настройке ltf_provisional_range_enabled (флаг приходит в /current)
function syncProvisionalToggle() {
  const wrap = $('ltf-prov-toggle');
  if (!wrap) return;
  const cb = wrap.querySelector('input');
  const enabled = !!(state.current && state.current.provisional_range_enabled);
  wrap.classList.toggle('hidden', !enabled);
  if (!enabled) {
    state.layerToggles.provisional = false;
    state.provUserSet = false;
    cb.checked = false;
  } else if (!state.provUserSet) {
    // настройка включена — слой показываем сразу (это её назначение)
    state.layerToggles.provisional = true;
    cb.checked = true;
  }
}

// ---------------------------------------------------------------------------
// Верхняя панель (§4.1)
// ---------------------------------------------------------------------------

function renderTopbar() {
  const v = state.current;
  if (!v) return;
  const ins = v.instrument || {};
  $('ltf-venue').textContent = ins.venue || '';
  $('ltf-market').textContent = ins.market_type || '';
  $('ltf-last-price').textContent = v.price != null ? fmtPrice(v.price) : '—';
  $('ltf-quote-at').textContent = v.quote_at ? 'котировка ' + fmtTime(v.quote_at) : '';
  $('ltf-last-candle').textContent = v.last_closed_h1
    ? 'H1: ' + fmtTime(v.last_closed_h1) : 'H1: —';
  const ds = v.data_state || {};
  const indData = $('ltf-ind-data');
  indData.textContent = DATA_STATE_RU[ds.state] || ds.state || '—';
  indData.className = 'data-ind di-' + (ds.state === 'ok' ? 'ok' : 'bad');
  // D02: возраст каналов — отдельно: котировка и последняя закрытая H1
  indData.title = 'Поступление данных' +
    (ds.reason ? ': ' + (DATA_STATE_REASON_RU[ds.reason] || ds.reason) : '') +
    (ds.quote_age_s != null ? `\nКотировка: ${Math.round(ds.quote_age_s)} с назад` : '') +
    (ds.h1_age_s != null ? `\nЗакрытая H1: ${Math.round(ds.h1_age_s / 60)} мин назад` : '') +
    (ds.source_stale ? '\nИсточник: недоступен (флаг воркера)' : '');
  const processed = v.last_processed_h1 != null && v.last_closed_h1 != null &&
    v.last_processed_h1 >= v.last_closed_h1;
  const indProc = $('ltf-ind-processed');
  indProc.textContent = processed ? 'свеча обработана' : 'свеча не обработана';
  indProc.className = 'data-ind di-' + (processed ? 'ok' : 'warn');
  indProc.title = `Последняя закрытая H1: ${v.last_closed_h1 ? fmtTime(v.last_closed_h1) : '—'} · ` +
    `обработана движком: ${v.last_processed_h1 ? fmtTime(v.last_processed_h1) : '—'}`;
  updatePriceLine();
}

// ---------------------------------------------------------------------------
// Правая карточка (§4.4)
// ---------------------------------------------------------------------------

async function noZonesReason(v) {
  // §4.4: конкретная причина отсутствия подходящих зон (п.15)
  const ds = v.data_state || {};
  if (ds.state === 'data_pending') return 'данные неполные: ' + (DATA_STATE_REASON_RU[ds.reason] || ds.reason || '—');
  const sc = v.current_scenario;
  if (!sc) return null;
  if (!v.range) return null; // ждём диапазон — не «нет зон»
  if (state.excludedRows === null) {
    const resp = await api(`/api/ltf/scenarios/${sc.id}/entries?view=excluded`);
    state.excludedRows = resp.entries; // F04: конверт {state_version, entries}
  }
  const rows = state.excludedRows;
  if (!rows.length) return 'нет зон нужного движения (кандидаты не сформированы)';
  const counts = {};
  for (const r of rows) counts[r.reason] = (counts[r.reason] || 0) + 1;
  const parts = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .map(([reason, n]) => `${REASON_RU[reason] || reason} — ${n}`);
  return 'исключены: ' + parts.join('; ');
}

// ---------------------------------------------------------------------------
// U02: первые строки карточки — «что происходит / почему / чего ждём /
// что отменит». Данных нет — об этом написано явно, ничего не выдумываем.
// ---------------------------------------------------------------------------

function expectedBreakText(v, ctx) {
  const exp = (state.layers && state.layers.expected) || {};
  const dir = (exp.bos && exp.bos.direction) || (ctx && ctx.direction) || v.direction;
  const side = dir === 'bear' ? 'ниже' : 'выше';
  const parts = [];
  if (exp.bos) parts.push(`BOS — закрытие H1 строго ${side} ${fmtPrice(exp.bos.level)}`);
  if (exp.sms) parts.push(`SMS — закрытие H1 строго ${side} ${fmtPrice(exp.sms.level)}`);
  if (!parts.length) {
    return 'Уровень ожидаемого слома пока не определён: структура H1 ещё не подтверждена.';
  }
  return 'Сценарий откроется после подтверждённого слома: ' + parts.join('; ') +
    '. Тень без закрытия сломом не считается.';
}

function scenarioWaitText(v) {
  if (!v.range) {
    return 'Ждём подтверждения опор диапазона: экстремум становится опорой ' +
      'после трёх закрытых свечей справа.';
  }
  const n = v.counts.eligible;
  if (n > 0) {
    if (v.stage === 'Цена в Entry Zone') {
      return 'Цена уже в подходящей зоне — сценарий в точке входа.';
    }
    if (n === 1) return 'Ждём возврат цены к подходящей зоне.';
    return `Ждём возврат цены к ${n} подходящим зонам.`;
  }
  return 'Подходящих зон нет: все исключены правилами — причины во вкладке «История».';
}

function cancellationText(sc, v) {
  const c = v && v.cancel_condition;
  if (c && c.level != null && c.status && c.status !== 'undefined') {
    const side = c.side === 'above' ? 'выше' : 'ниже';
    const verb = c.status === 'occurred' ? 'Отмена произошла' : 'Отменит';
    const kind = c.kind ? ` (${c.kind})` : '';
    return `${verb}: закрытие H1 строго ${side} ${fmtPrice(c.level)}${kind}.`;
  }
  if (sc && sc.reverse_break && sc.reverse_break.price != null) {
    const side = sc.direction === 'bear' ? 'выше' : 'ниже';
    return `Отмена произошла: закрытие H1 строго ${side} ${fmtPrice(sc.reverse_break.price)}.`;
  }
  return 'Условие отмены сервер ещё не определил.';
}

function scenarioQa(v, ctx) {
  const sc = v.current_scenario;
  const z = (ctx && ctx.parent_zone) || null;
  if (!ctx) {
    const waitMsg = (v.wait && v.wait.message) || v.market_stage
      || 'Нет подтверждённого HTF-контекста.';
    return {
      what: waitMsg,
      why: waitMsg,
      wait: waitMsg,
      cancel: 'Отменять нечего: сценарий ещё не открыт.',
    };
  }
  const zoneTxt = z
    ? `родительская HTF-зона ${ctxLabel(z.type, z.timeframe, ctx.direction)} ` +
      `[${fmtPrice(z.lower)}–${fmtPrice(z.upper)}]`
    : 'нет данных о родительской зоне';
  if (!sc) {
    let what = `HTF-контекст ${z ? ctxLabel(z.type, z.timeframe, ctx.direction) : ''} ` +
      'активен; сценария пока нет — ждём подтверждённого слома структуры на H1.';
    const c = v.scenario_waiting && v.scenario_waiting.last_cancellation;
    if (c) {
      what += ` Предыдущий сценарий отменён: ` +
        `${LTF_CANCEL_RU[c.reason] || c.reason || '—'} · ${fmtTime(c.cancelled_at)}.`;
    }
    return {
      what,
      why: zoneTxt +
        (ctx.last_touch_at ? `, последнее касание ${fmtTime(ctx.last_touch_at)}` : '') + '.',
      wait: expectedBreakText(v, ctx),
      cancel: 'Отменять нечего: сценарий ещё не открыт.',
    };
  }
  const dirTxt = sc.direction === 'bear' ? 'снижения' : 'роста';
  const sideTxt = sc.direction === 'bear' ? 'ниже' : 'выше';
  const brkTxt = sc.break_level != null
    ? ` (${sc.trigger || 'слом'} ${fmtPrice(sc.break_level)})` : '';
  const whyParts = [zoneTxt];
  if (sc.break_level != null) {
    whyParts.push(`подтверждённый ${sc.trigger || 'слом'} уровня ${fmtPrice(sc.break_level)}` +
      (sc.break_candle_open_time
        ? ` (закрытие H1 ${fmtTime(sc.break_candle_open_time + H1_MS - 1)})` : ''));
  } else {
    whyParts.push('уровень подтверждающего слома — нет данных');
  }
  const rng = v.range;
  if (rng) {
    whyParts.push(`диапазон v${rng.version} [${fmtPrice(rng.lower)}–${fmtPrice(rng.upper)}]` +
      (rng.available_at ? `, подтверждён ${fmtTime(rng.available_at)}` : ''));
  } else {
    whyParts.push('диапазон ещё не подтверждён');
  }
  return {
    what: `Сценарий ${dirTxt}: H1 закрылся ${sideTxt} уровня структуры${brkTxt}. ` +
      `Этап: ${v.stage || '—'}.`,
    why: whyParts.join('; ') + '.',
    wait: scenarioWaitText(v),
    cancel: cancellationText(sc, v),
  };
}

async function renderCard() {
  const el = $('ltf-card');
  const v = state.current;
  if (!v) { el.innerHTML = '<div class="ltf-empty">Выберите актив слева.</div>'; return; }
  const ins = v.instrument || {};
  const sc = v.current_scenario;
  const ctxFound = (v.contexts || []).find((c) => c.observation_id === v.selected_context_id) || null;
  const ctx = ctxFound || {};
  const z = ctx.parent_zone || {};
  const ds = v.data_state || {};
  const qa = scenarioQa(v, ctxFound);

  let positionText;
  if (ctx.price_position === 'inside') positionText = 'цена в HTF-зоне';
  else if (ctx.price_position === 'above') positionText = 'выше HTF-зоны';
  else if (ctx.price_position === 'below') positionText = 'ниже HTF-зоны';
  else positionText = 'положение цены неактуально (нет свежей котировки)';

  let html = '';
  if (v.presentation && window.LFCopy) {
    html += LFCopy.card(v.presentation);
  } else {
    html += `
    <div class="scenario-stage ${sc ? (v.counts.eligible > 0 ? 'ok' : 'wait') : 'wait'}">
      <span class="stage-label">Текущий этап</span><strong>${esc(v.stage || '—')}</strong>
    </div>`;
  }
  if (!(v.presentation && window.LFCopy)) {
    html += `
    ${ds.state && ds.state !== 'ok' ? `<div class="ltf-cancel-note">${esc(DATA_STATE_RU[ds.state] || ds.state)}${ds.reason ? ': ' + esc(DATA_STATE_REASON_RU[ds.reason] || ds.reason) : ''}. Положение цены и расстояния могут быть неактуальны.</div>` : ''}
    ${v.contexts_conflict ? '<div class="ltf-cancel-note ctx-conflict-banner">Конфликт контекстов: активны HTF-зоны противоположных направлений — направление не усредняется. Обе зоны — в списке контекстов панели «Активы».</div>' : ''}
    <ul class="scenario-qa">
      <li><span class="qa-q">Почему</span><span class="qa-a">${esc(qa.why)}</span></li>
      <li><span class="qa-q">Чего ждём</span><span class="qa-a">${esc(qa.wait)}</span></li>
      <li><span class="qa-q">Что отменит</span><span class="qa-a">${esc(qa.cancel)}</span></li>
    </ul>
    <dl>
      <dt>Инструмент</dt><dd>${esc(ins.symbol || '?')} · ${esc(ins.venue || '')} · ${esc(ins.market_type || '')}</dd>
      <dt>HTF-контекст</dt><dd>${esc(ctxLabel(z.type, z.timeframe, ctx.direction))} [${fmtPrice(z.lower)}–${fmtPrice(z.upper)}]</dd>
      <dt>Показан контекст</dt><dd>${esc(CONTEXT_BASIS_RU[v.selected_context_basis] || '—')}</dd>
      <dt>Положение цены</dt><dd>${esc(positionText)}</dd>
      <dt>Актуальность родителя</dt><dd>${esc(ctx.parent_validity === 'active' ? 'актуален' : (ctx.parent_validity || '—'))}</dd>
      <dt>Последнее касание HTF</dt><dd>${ctx.last_touch_at ? fmtTime(ctx.last_touch_at) : '—'}</dd>`;
  } else {
    html += '<dl>';
  }

  if (sc) {
    const rng = v.range;
    html += `
      <dt>Направление</dt><dd>${dirWord(sc.direction)}</dd>
      <dt>BOS/SMS</dt><dd>${esc(sc.trigger || '—')} · ${dirWord(sc.direction)} · ${sc.stage === 'secondary' ? 'вторичный' : 'первичный'}</dd>
      ${sc.break_candle_open_time ? `<dt>Подтверждающая свеча</dt><dd>закрытие H1 ${fmtTime(sc.break_candle_open_time + H1_MS - 1)}</dd>` : ''}
      ${sc.break_level != null ? `<dt>Уровень слома</dt><dd>${fmtPrice(sc.break_level)}</dd>` : ''}`;
    if (rng) {
      const anchors = rng.anchors || {};
      const anchorLine = (name, p) => p
        ? `${name} ${fmtPrice(p.price)} · экстремум ${fmtTime(p.pivot_at)}, подтверждён ${p.confirmed_at ? fmtTime(p.confirmed_at) : '—'}`
        : null;
      html += `
      <dt>Диапазон v${rng.version}</dt><dd>${fmtPrice(rng.lower)} – ${fmtPrice(rng.upper)}${rng.kind === 'origin_reversal' ? ' · origin' : ''}</dd>
      ${anchorLine('Опора low', anchors.low) ? `<dt>Опоры</dt><dd>${esc(anchorLine('Опора low', anchors.low))}</dd>` : ''}
      ${anchorLine('Опора high', anchors.high) ? `<dt></dt><dd>${esc(anchorLine('Опора high', anchors.high))}</dd>` : ''}
      <dt>Середина</dt><dd>${fmtPrice(rng.mid)}</dd>`;
    } else {
      // §4.4: после слома без диапазона — не «слома нет»
      html += `<dt>Диапазон</dt><dd>BOS подтверждён. Ждём подтверждения опор диапазона</dd>`;
    }
    const sctx = sc.context;
    if (sctx && sctx.complete) {
      // §18: контекстный допуск FVG вне Premium
      const ctxTxt = sc.direction === 'bear'
        ? 'обновление лоя = снятие SSL + тест 50% D1 FVG'
        : 'обновление хая = снятие BSL + тест 50% D1 FVG';
      html += `<dt>Контекст §18</dt><dd>${ctxTxt} — FVG вне Premium допускается</dd>`;
    }
    html += `<dt>Подходящих зон</dt><dd>${v.counts.eligible}</dd>
      <dt>Следующий шаг</dt><dd>${esc(v.stage || '—')}</dd>
    </dl>`;
    if (rng && v.counts.eligible === 0) {
      html += `<div class="ltf-cancel-note" id="ltf-no-zones">Причина: вычисляется…</div>`;
    }
    // «Завершить сценарий» — только у живого сценария (серверное состояние §8)
    html += `<button id="btn-close-scenario" class="btn danger">Завершить сценарий</button>`;
  } else {
    html += `</dl>`;
    if (v.scenario_waiting) {
      // §8: отменённый сценарий — только в истории; в карточке — ожидание нового
      const c = v.scenario_waiting.last_cancellation;
      html += `<div class="scenario-stage wait"><span class="stage-label">Сценарий</span><strong>Ожидание нового сценария</strong>
        ${c ? `<div class="stage-line">Предыдущий отменён: ${esc(LTF_CANCEL_RU[c.reason] || c.reason || '—')} · ${fmtTime(c.cancelled_at)} — подробности во вкладке «История».</div>` : ''}
      </div>`;
    }
  }
  html += `<details class="context-detail"><summary>Время и происхождение</summary><dl>
    <dt>Основание зоны</dt><dd>${fmtTime(z.formed_at)}</dd>
    <dt>Подтверждение зоны</dt><dd>${fmtTime(z.confirmed_at)}</dd>
    <dt>Активация LTF</dt><dd>${fmtTime(ctx.activated_at)}</dd>
    <dt>state_version</dt><dd>${v.state_version}</dd>
  </dl></details>`;
  el.innerHTML = html;

  if (sc && v.range && v.counts.eligible === 0) {
    const note = $('ltf-no-zones');
    noZonesReason(v).then((reason) => {
      if (note && reason) note.textContent = 'Нет подходящих зон: ' + reason;
    }).catch(() => { if (note) note.textContent = ''; });
  }
  const btn = $('btn-close-scenario');
  if (btn && sc) {
    btn.onclick = () => {
      HTF.openModal($('ltf-close-modal'));
      $('ltf-close-error').textContent = '';
      $('ltf-close-confirm').onclick = async () => {
        try {
          await api(`/api/ltf/scenarios/${sc.id}/close`, { method: 'POST' });
          HTF.closeModal($('ltf-close-modal'));
          await reloadCurrent({ keepRange: true });
          await loadAssets();
        } catch (err) { $('ltf-close-error').textContent = err.message; }
      };
    };
  }
}

// ---------------------------------------------------------------------------
// Таблицы зон (§4.5, §12)
// ---------------------------------------------------------------------------

function distOf(row, price) {
  if (!price || price <= 0) return null;
  const abs = row.is_level
    ? Math.abs(price - row.lower)
    : Math.max(row.lower - price, 0, price - row.upper);
  return { abs, pct: 100 * abs / price };
}

const DIST_TITLE = 'Расстояние до зоны: 0, если цена внутри; иначе расстояние до ближайшей границы, делённое на текущую цену (не до середины)';

function zoneRowsHtml(rows, { withReason }) {
  const ins = state.current && state.current.instrument;
  return rows.map((row) => {
    const d = distOf(row, state.lastPrice);
    const rangeTxt = row.is_level
      ? fmtPrice(row.lower)
      : `${fmtPrice(row.lower)}–${fmtPrice(row.upper)}`;
    const liq = row.liquidity_state
      ? `<span class="badge liq-${esc(row.liquidity_state)}">${esc(LIQUIDITY_STATE_RU[row.liquidity_state] || row.liquidity_state)}</span>`
      : '';
    const selectedCls = row.entry_zone_id === state.selectedEntryZoneId ? 'selected' : '';
    const expanded = row.entry_zone_id === state.expandedEntryId;
    const depth = row.max_test_depth != null ? Math.round(row.max_test_depth * 100) + '%' : '—';
    const reasonTxt = REASON_RU[row.reason] || row.reason || '—';
    // L01: дополнительные блокирующие причины и контекстный допуск (§18)
    const blocking = (row.blocking_reasons || [])
      .filter((r) => r && r !== row.reason)
      .map((r) => REASON_RU[r] || r);
    const contextAdmitted = row.admission_basis === 'context_exception' || row.outside_premium;
    const colspan = withReason ? 9 : 8;
    const detail = `<tr class="entry-detail${expanded ? '' : ' hidden'}" data-detail-of="${row.entry_zone_id}"><td colspan="${colspan}"><dl>
      <dt>Почему ${withReason ? 'исключена' : 'показана'}</dt><dd>${esc(reasonTxt)}</dd>
      ${blocking.length ? `<dt>Также блокирует</dt><dd>${esc(blocking.join('; '))}</dd>` : ''}
      ${contextAdmitted ? '<dt>Контекст §18</dt><dd>Зона вне Premium — допущена по контексту (снятие SSL/BSL + тест 50% D1 FVG); не критично для этого сценария</dd>' : ''}
      <dt>Сформирована</dt><dd>${fmtTime(row.formed_at)}</dd>
      <dt>Подтверждена</dt><dd>${fmtTime(row.confirmed_at)}</dd>
      <dt>Первое касание</dt><dd>${row.first_test_at ? fmtTime(row.first_test_at) : '—'}</dd>
      ${row.fill_status ? `<dt>Перекрытие FVG</dt><dd>${esc(FILL_STATUS_RU[row.fill_status] || row.fill_status)}</dd>` : ''}
      <dt>Середина</dt><dd>${row.is_level ? '—' : fmtPrice(row.mid)}</dd>
      <dt>Версия диапазона</dt><dd>${row.range_version != null ? row.range_version : '—'}${row.outdated ? ' · устарела' : ''}</dd>
    </dl>${ltfReviewBlock(row)}</td></tr>`;
    return `<tr class="entry-row ${selectedCls}" data-zone-id="${row.entry_zone_id}">
      <td data-label="Тип">${esc(row.type)}</td>
      <td data-label="Направление" class="dir ${esc(row.direction)}">${row.direction === 'bull' ? '▲ Рост' : '▼ Снижение'}</td>
      <td data-label="Диапазон">${rangeTxt}</td>
      <td data-label="P/D">${esc(row.half && row.half !== 'none' ? row.half : '—')}${row.partial ? ' · частично' : ''}${row.outside_premium ? ' <span class="badge ltf-badge-op">вне Premium</span>' : ''}</td>
      <td data-label="Глубина тестов">${depth}</td>
      <td data-label="Расстояние" class="col-dist" title="${DIST_TITLE}">${d ? d.pct.toFixed(2) + '%' : '—'}</td>
      <td data-label="Статус"><span class="badge es-${esc(row.state)}">${esc(ENTRY_STATE_RU[row.state] || row.state)}</span> ${liq}</td>
      ${withReason ? `<td data-label="Причина">${esc(reasonTxt)}${contextAdmitted ? ' <span class="badge ltf-badge-op">допущена по контексту</span>' : ''}</td>` : ''}
      <td data-label="Действия" class="row-actions">
        <button class="btn small" data-act="chart" title="Показать на графике">На графике</button>
        ${ins ? `<a class="btn small" target="_blank" href="${tradingviewUrl(ins)}" title="TradingView">TV</a>` : ''}
        <button class="btn small" data-act="why" title="Почему показана / исключена">${expanded ? 'Скрыть' : 'Почему показана'}</button>
        <button class="btn small" data-act="history" title="События по зоне">Журнал</button>
      </td>
    </tr>${detail}`;
  }).join('');
}

function bindZoneRows(el, rows) {
  for (const tr of el.querySelectorAll('tbody tr.entry-row')) {
    const zid = Number(tr.dataset.zoneId);
    tr.onclick = () => {
      state.selectedEntryZoneId = zid === state.selectedEntryZoneId ? null : zid;
      rerenderTables();
      drawLtfLayers();
    };
    tr.querySelector('[data-act="why"]').onclick = (e) => {
      e.stopPropagation();
      state.expandedEntryId = zid === state.expandedEntryId ? null : zid;
      rerenderTables();
    };
    const reviewBox = el.querySelector(`.ltf-review[data-review-zone="${zid}"]`);
    if (reviewBox) {
      reviewBox.querySelectorAll('[data-review]').forEach((btn) => {
        btn.onclick = (e) => {
          e.stopPropagation();
          reviewEntryZone(zid, btn.dataset.review, reviewBox);
        };
      });
    }
    tr.querySelector('[data-act="chart"]').onclick = (e) => {
      e.stopPropagation();
      focusEntryZone(zid);
    };
    tr.querySelector('[data-act="history"]').onclick = (e) => {
      e.stopPropagation();
      state.journalZoneFilter = zid === state.journalZoneFilter ? null : zid;
      renderJournal();
      activateBottomTab('events');
    };
  }
}

function rerenderTables() {
  renderEntries();
  if (state.historyRows !== null) renderHistory();
}

// ---------------------------------------------------------------------------
// Разметка Entry Zones (ревью): оценка только фиксируется, зона не меняется
// ---------------------------------------------------------------------------

function ltfReviewBlock(row) {
  const zid = row.entry_zone_id;
  const flash = state.reviewFlash && state.reviewFlash.zoneId === zid
    ? state.reviewFlash.text : '';
  const reasonOpts = Object.entries(LTF_REVIEW_REASON_RU)
    .map(([code, ru]) => `<option value="${esc(code)}">${esc(ru)}</option>`).join('');
  const lastA = row.latest_assessment || null;
  const history = (row.reviews || []).map((r) => {
    const a = lastA && lastA.review_id === r.id ? lastA : null;
    return `<li><span class="ev-time">${fmtTime(r.created_at)}</span>` +
      esc(LTF_REVIEW_DECISION_RU[r.decision] || r.decision) +
      (a ? ` · геометрия ${esc(a.geometry_verdict)}` +
        (a.lifecycle_verdict === 'tested' ? ', касание было' : '') +
        (a.requires_clarification ? ', требует уточнения' : '') : '') +
      (r.text ? ': ' + esc(r.text) : '') + `</li>`;
  }).join('');
  return `<div class="review-block ltf-review" data-review-zone="${zid}">
    <textarea class="ltf-review-comment" rows="2"
      placeholder="Комментарий к оценке (необязательно)"></textarea>
    <div class="review-actions">
      <button class="btn ok" type="button" data-review="correct">Размечено верно</button>
      <button class="btn primary" type="button" data-review="fix_boundaries">Исправить границы</button>
      <details data-section="other-reviews"><summary class="btn">Другие решения</summary><div class="review-actions">
        <button class="btn" type="button" data-review="now_irrelevant">Сейчас неактуально</button>
        <button class="btn" type="button" data-review="wrong_base">Другое основание</button>
        <button class="btn danger" type="button" data-review="wrong_type">Неверный тип/форма</button>
        <button class="btn" type="button" data-review="no_context">Нет контекста</button>
      </div></details>
    </div>
    <div class="review-actions ltf-review-extra">
      <label>Нижняя <input class="ltf-review-lower" type="number" step="any" value="${row.lower}"></label>
      <label>Верхняя <input class="ltf-review-upper" type="number" step="any" value="${row.upper}"></label>
      <select class="ltf-review-reason" title="Причина (необязательно)">
        <option value="">Причина (необязательно)</option>${reasonOpts}
      </select>
    </div>
    <div class="review-verdict">${esc(flash)}</div>
    ${history ? `<ul class="ltf-review-history">${history}</ul>` : ''}
  </div>`;
}

async function reviewEntryZone(zoneId, decision, box) {
  const textEl = box.querySelector('.ltf-review-comment');
  const reasonEl = box.querySelector('.ltf-review-reason');
  const verdictEl = box.querySelector('.review-verdict');
  const sc = state.current && state.current.current_scenario;
  const body = {
    decision,
    text: textEl ? textEl.value.trim() : '',
    scenario_id: sc ? sc.id : null,
  };
  const reason = reasonEl ? reasonEl.value : '';
  if (reason) body.reason_code = reason;
  if (decision === 'fix_boundaries') {
    const lower = Number(box.querySelector('.ltf-review-lower').value);
    const upper = Number(box.querySelector('.ltf-review-upper').value);
    if (!Number.isFinite(lower) || !Number.isFinite(upper)) {
      if (verdictEl) verdictEl.textContent = 'Укажите обе границы (нижнюю и верхнюю)';
      return;
    }
    body.lower = lower;
    body.upper = upper;
  }
  const btns = box.querySelectorAll('[data-review]');
  btns.forEach((b) => { b.disabled = true; });
  let res;
  try {
    res = await api(`/api/ltf/entry-zones/${zoneId}/review`, {
      method: 'POST', body: JSON.stringify(body),
    });
  } catch (err) {
    btns.forEach((b) => { b.disabled = false; });
    if (verdictEl) verdictEl.textContent = 'Ошибка: ' + err.message;
    return;
  }
  const a = res.assessment || {};
  state.reviewFlash = {
    zoneId,
    text: `Геометрия: ${a.geometry_verdict}` +
      (a.lifecycle_verdict === 'tested' ? ' · касание было' : '') +
      (a.requires_clarification ? ' · требует уточнения' : '') +
      ' · зона не изменена',
  };
  await reloadCurrent({ keepRange: true });
}

// U05: пустые состояния с причиной и действием
function emptyStateHtml(text, actionId, actionLabel) {
  return `<div class="ltf-empty">${esc(text)}` +
    (actionId
      ? `<div class="ltf-empty-actions"><button class="btn small" id="${actionId}">${esc(actionLabel)}</button></div>`
      : '') +
    `</div>`;
}

function openExcludedView() {
  $('flt-hist-view').value = 'excluded';
  state.historyRows = null;
  activateBottomTab('history');
}

function renderEntries() {
  const el = $('ltf-entries');
  const v = state.current;
  const rows = (v && v.eligible_entries) || [];
  // §4.5: основной счётчик — число подходящих зон сценария
  $('ltf-eligible-count').textContent = v ? v.counts.eligible : 0;
  if (!v) { el.innerHTML = ''; return; }
  const ds = v.data_state || {};
  if (ds.state && ds.state !== 'ok') {
    // отсутствие данных — не «нет сетапа»: причина + действие «Проверить данные»
    el.innerHTML = emptyStateHtml(
      'Данные не поступают: ' + (DATA_STATE_REASON_RU[ds.reason] || DATA_STATE_RU[ds.state] || ds.state || '—') +
      '. Подходящие зоны и расстояния могут быть неактуальны.',
      'ltf-empty-check', 'Проверить данные');
    $('ltf-empty-check').onclick = () => reloadCurrent({ keepRange: true });
    return;
  }
  if (!v.selected_context_id) {
    const msg = (v.wait && v.wait.message) || v.market_stage
      || 'Нет подтверждённого HTF-контекста.';
    el.innerHTML = emptyStateHtml(msg);
    return;
  }
  if (!v.current_scenario) {
    el.innerHTML = emptyStateHtml(
      'Ожидание структуры: сценарий откроется после подтверждённого слома — закрытия H1 за уровнем. Зоны появятся после слома и диапазона.');
    return;
  }
  if (!v.range) {
    el.innerHTML = emptyStateHtml(
      'Ожидание опор диапазона: слом подтверждён, ждём подтверждения экстремумов тремя закрытыми свечами справа.');
    return;
  }
  if (!rows.length) {
    if (v.counts.excluded > 0) {
      el.innerHTML = emptyStateHtml(
        `Все зоны исключены правилами (${v.counts.excluded}).`,
        'ltf-empty-excluded', 'Открыть исключённые');
      $('ltf-empty-excluded').onclick = openExcludedView;
    } else {
      el.innerHTML = emptyStateHtml(
        'Подходящих зон сейчас нет: кандидаты нужного движения ещё не сформированы — причина в карточке справа.');
    }
    return;
  }
  el.innerHTML = `<div class="table-wrap"><table id="ltf-entries-table">
    <thead><tr>
      <th>Тип</th><th>Напр.</th><th>Диапазон/уровень</th><th>P/D</th><th>Глубина</th>
      <th class="col-dist" title="${DIST_TITLE}">Дист.</th><th>Статус</th><th></th>
    </tr></thead><tbody>${zoneRowsHtml(rows, { withReason: false })}</tbody></table></div>`;
  bindZoneRows(el, rows);
}

// ---------------------------------------------------------------------------
// Вкладка «История» (§12): исключённые и исторические строки с причинами
// ---------------------------------------------------------------------------

function historyScenarioId() {
  const v = state.current;
  if (v && v.current_scenario) return v.current_scenario.id;
  return state.historyScenarioId;
}

async function loadHistoryRows() {
  const v = state.current;
  let scId = historyScenarioId();
  if (scId === null && v && v.selected_context_id) {
    // нет активного сценария — история последнего (отменённого) сценария (п.03)
    const card = await api(`/api/ltf/observations/${v.selected_context_id}`);
    const scenarios = card.scenarios || [];
    if (scenarios.length) {
      state.historyScenarioId = scenarios[scenarios.length - 1].id;
      scId = state.historyScenarioId;
    }
  }
  if (scId === null) {
    state.historyRows = [];
    return;
  }
  const view = $('flt-hist-view').value || 'excluded';
  const resp = await api(`/api/ltf/scenarios/${scId}/entries?view=${view}`);
  state.historyRows = resp.entries; // F04: конверт {state_version, entries}
}

function renderHistoryCount() {
  const v = state.current;
  $('ltf-history-count').textContent = v ? v.counts.excluded + v.counts.historical : 0;
}

async function renderHistory() {
  const el = $('ltf-history');
  if (state.historyRows === null) {
    el.innerHTML = '<div class="ltf-empty">Загрузка…</div>';
    try {
      await loadHistoryRows();
    } catch (err) {
      el.innerHTML = `<div class="ltf-empty">Не удалось загрузить историю: ${esc(err.message)}</div>`;
      return;
    }
  }
  const reasonSel = $('flt-hist-reason');
  const reasonPrev = reasonSel.value;
  const reasons = [...new Set(state.historyRows.map((r) => r.reason).filter(Boolean))].sort();
  reasonSel.innerHTML = '<option value="">Причина: все</option>' +
    reasons.map((r) => `<option value="${esc(r)}">${esc(REASON_RU[r] || r)}</option>`).join('');
  reasonSel.value = reasons.includes(reasonPrev) ? reasonPrev : '';
  const rows = reasonSel.value
    ? state.historyRows.filter((r) => r.reason === reasonSel.value)
    : state.historyRows;
  if (!rows.length) {
    if (state.historyRows.length) {
      // строки есть, но фильтр по причине их скрыл
      el.innerHTML = emptyStateHtml('Строк по фильтрам нет.', 'ltf-hist-reset', 'Сбросить фильтр');
      $('ltf-hist-reset').onclick = () => { $('flt-hist-reason').value = ''; renderHistory(); };
    } else {
      el.innerHTML = emptyStateHtml($('flt-hist-view').value === 'history'
        ? 'Исторических строк нет: зон прошлых версий диапазона не было.'
        : 'Исключённых зон в текущей версии диапазона нет.');
    }
    return;
  }
  el.innerHTML = `<div class="table-wrap"><table id="ltf-history-table">
    <thead><tr>
      <th>Тип</th><th>Напр.</th><th>Диапазон/уровень</th><th>P/D</th><th>Глубина</th>
      <th class="col-dist" title="${DIST_TITLE}">Дист.</th><th>Статус</th><th>Причина</th><th></th>
    </tr></thead><tbody>${zoneRowsHtml(rows, { withReason: true })}</tbody></table></div>`;
  bindZoneRows(el, rows);
}

// ---------------------------------------------------------------------------
// Вкладка «События» (журнал выбранного контекста, §12)
// ---------------------------------------------------------------------------

function renderJournal() {
  const el = $('ltf-journal');
  let events = state.journal;
  $('ltf-journal-count').textContent = events.length;
  if (state.journalZoneFilter) {
    events = events.filter((e) => e.payload && e.payload.entry_zone_id === state.journalZoneFilter);
  }
  if (!events.length) {
    if (state.journalZoneFilter) {
      el.innerHTML = '<li class="ltf-empty">По выбранной зоне событий нет. ' +
        '<button class="btn small" id="ltf-journal-reset">Сбросить фильтр</button></li>';
      $('ltf-journal-reset').onclick = () => { state.journalZoneFilter = null; renderJournal(); };
    } else {
      el.innerHTML = '<li class="ltf-empty">Событий пока нет.</li>';
    }
    return;
  }
  const brief = (e) => {
    const p = e.payload || {};
    if (e.kind === 'bos' || e.kind === 'sms') {
      return `уровень ${fmtPrice(p.break_level)}${p.range_pending ? ' · ожидание диапазона' : ''}` +
        (p.entries && p.entries.length ? ` · зон: ${p.entries.length}` : '');
    }
    if (e.kind === 'entries_ready') return `новых зон: ${(p.entries || []).length}`;
    if (e.kind === 'range_ready') return p.range ? `[${fmtPrice(p.range.lower)}; ${fmtPrice(p.range.upper)}]` : '';
    if (e.kind === 'touch') return `${esc(p.type || '')} ${fmtPrice(p.lower)}${p.upper !== p.lower ? '–' + fmtPrice(p.upper) : ''}`;
    if (e.kind === 'sweep_confirmed' || e.kind === 'sweep_failed') {
      const outcome = p.outcome ? ` · ${esc(LIQUIDITY_STATE_RU[p.outcome] || p.outcome)}` : '';
      return `уровень ${fmtPrice(p.level)}, закрытие ${fmtPrice(p.close_price)}${outcome}`;
    }
    if (e.kind === 'cancellation') return esc(LTF_CANCEL_RU[p.reason] || p.reason || '');
    return '';
  };
  el.innerHTML = events.slice().reverse().map((e) => `
    <li>
      <span class="ev-time">${fmtTime(e.occurred_at)}</span>
      <b>${esc(LTF_KIND_RU[e.kind] || e.kind)}</b> ${brief(e)}
      ${e.delayed ? ' <span class="badge dq-replaying">восстановлено</span>' : ''}
    </li>`).join('');
}

// ---------------------------------------------------------------------------
// График H1 и слои (§4.3, §5): DOM-overlay
// ---------------------------------------------------------------------------

function initChart() {
  const theme = HTF.chartTheme();
  // фон/сетка — из темы LevelFrame (chartTheme: var(--lf-surface) и т.д.),
  // чтобы работала светлая тема; белые растущие / синие падающие свечи —
  // устойчивая стилистика LTF-снимка
  state.chart = LightweightCharts.createChart($('chart'), {
    layout: { background: { color: theme.background }, textColor: theme.text },
    grid: {
      vertLines: { color: theme.grid, style: LightweightCharts.LineStyle.Dotted },
      horzLines: { color: theme.grid, style: LightweightCharts.LineStyle.Dotted },
    },
    timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 10 },
    rightPriceScale: { borderColor: theme.border },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    autoSize: true,
  });
  state.candleSeries = state.chart.addCandlestickSeries({
    upColor: '#E8EAF0', downColor: '#3B9DF0',
    wickUpColor: '#E8EAF0', wickDownColor: '#3B9DF0', borderVisible: false,
  });
  state.chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
    drawLtfLayers();
    scheduleH1HistoryReload();
  });
  window.addEventListener('resize', drawLtfLayers);
  new ResizeObserver(() => drawLtfLayers()).observe($('chart-container'));
  state.chart.subscribeCrosshairMove(() => drawLtfLayers());
}

// пустота справа после последней свечи (баров H1); временной setVisibleRange
// клампится к последнему бару — отступ задаётся только логическим диапазоном
const RIGHT_GAP_BARS = 14;

function defaultVisibleLogicalRange(days) {
  // §4.1: начальный масштаб — 3 календарных дня H1 с запасом справа
  const n = state.candles.length;
  const last = state.candles[n - 1].time;
  const fromTime = Math.max(state.candles[0].time, last - days * 86400);
  let fromIdx = state.candles.findIndex((c) => c.time >= fromTime);
  if (fromIdx < 0) fromIdx = 0;
  return { from: fromIdx, to: n - 1 + RIGHT_GAP_BARS };
}

function setCandles(opts = {}) {
  state.candleSeries.setData(state.candles);
  $('ltf-chart-empty').classList.toggle('hidden', state.candles.length > 0);
  if (!state.candles.length) {
    $('ltf-chart-empty').textContent = 'Нет данных H1 по инструменту.';
    return;
  }
  const keep = opts.visibleRange;
  if (keep && keep.from != null && keep.to != null) {
    // §4.1: ручной масштаб не сбрасываем на тиках/обновлениях
    state.chart.timeScale().setVisibleLogicalRange(keep);
  } else {
    state.chart.timeScale().setVisibleLogicalRange(
      defaultVisibleLogicalRange(state.scaleDays));
  }
  applyLtfPriceScale(opts.focus || null);
  updatePriceLine();
}

function updatePriceLine() {
  if (!state.candleSeries) return;
  if (state.priceLine) {
    state.candleSeries.removePriceLine(state.priceLine);
    state.priceLine = null;
  }
  const price = state.lastPrice != null ? state.lastPrice
    : (state.candles.length ? state.candles[state.candles.length - 1].close : null);
  if (price == null) return;
  // сама линия на всю ширину выключена — её рисует overlay только вправо от
  // последней свечи; priceLine остаётся ради метки на ценовой шкале
  state.priceLine = state.candleSeries.createPriceLine({
    price,
    color: '#5B8DEF',
    lineWidth: 1,
    lineStyle: LightweightCharts.LineStyle.Dashed,
    lineVisible: false,
    axisLabelVisible: true,
    title: state.lastPrice != null ? 'цена' : 'закрытие',
  });
}

function entryList() {
  const byId = new Map();
  for (const e of (state.layers && state.layers.entries) || []) {
    byId.set(e.entry_zone_id || e.id, e);
  }
  if (state.layerToggles.excluded) {
    for (const e of (state.layers && state.layers.entries_excluded) || []) {
      byId.set(e.entry_zone_id || e.id, e);
    }
  }
  for (const e of (state.current && state.current.eligible_entries) || []) {
    const id = e.entry_zone_id || e.id;
    if (id != null && !byId.has(id)) byId.set(id, e);
  }
  return [...byId.values()];
}

function applyLtfPriceScale(focus) {
  if (!state.candleSeries) return;
  state.priceFocus = focus || null;
  state.candleSeries.applyOptions({
    autoscaleInfoProvider: (original) => {
      if (state.priceFocus) {
        const lo = Number(state.priceFocus.lower);
        const hi = Number(state.priceFocus.upper ?? state.priceFocus.lower);
        if (Number.isFinite(lo) && Number.isFinite(hi)) {
          const mid = (lo + hi) / 2;
          const span = Math.max(Math.abs(hi - lo) / 2, Math.abs(mid) * 0.015, 80);
          return { priceRange: { minValue: mid - span, maxValue: mid + span } };
        }
      }
      // видимые свечи — как считает сам график; внешкальные HTF-зоны
      // шкалу не растягивают (§5: указатель, не сжатие графика)
      return original ? original() : null;
    },
  });
  try {
    state.chart.priceScale('right').applyOptions({ autoScale: true });
  } catch (e) { /* шкала ещё не готова */ }
}

function structurePointColor(role) {
  const name = String(role || '').toUpperCase();
  const css = getComputedStyle(document.documentElement);
  if (name === 'HH' || name === 'HL') return css.getPropertyValue('--lf-positive').trim() || '#62C9B0';
  if (name === 'LL' || name === 'LH') return css.getPropertyValue('--lf-negative').trim() || '#F08D98';
  return css.getPropertyValue('--lf-text2').trim() || '#A2B0C5';
}

function visibleWindowMs() {
  if (!state.chart) return {};
  const range = state.chart.timeScale().getVisibleRange();
  if (!range || range.from == null || range.to == null) return {};
  return {
    from: Math.floor(Number(range.from) * 1000),
    to: Math.ceil(Number(range.to) * 1000),
  };
}

function paintH1Markers() {
  if (!state.candleSeries || !window.H1Layers) return;
  if (!state.h1Layers) {
    state.candleSeries.setMarkers([]);
    return;
  }
  state.candleSeries.setMarkers(window.H1Layers.seriesMarkers(
    state.h1Layers, state.h1SelectedEventId, structurePointColor));
}

function hideH1Chrome() {
  ['h1-transition', 'h1-layer-status', 'h1-offscreen', 'h1-event-card'].forEach((id) => {
    const el = $(id);
    if (el) el.classList.add('hidden');
  });
}

function showH1Card(html) {
  const card = $('h1-event-card');
  if (!card) return;
  card.classList.remove('hidden');
  card.innerHTML = html + '<button type="button" class="btn small">Закрыть</button>';
  const button = card.querySelector('button');
  if (button) button.onclick = () => {
    card.classList.add('hidden');
    card.innerHTML = '';
    state.h1SelectedEventId = null;
    state.h1SelectedZoneId = null;
    paintH1Markers();
  };
}

async function loadH1Structure() {
  const id = state.instrumentId;
  if (!id || !window.H1Layers) return;
  const token = ++state.h1Req;
  const obsId = state.current && state.current.selected_context_id;
  const q = window.H1Layers.queryString(Object.assign(
    { context_id: obsId || undefined },
    visibleWindowMs(),
  ));
  try {
    const data = await api(`/api/ltf/instruments/${id}/structure?${q}`);
    if (token !== state.h1Req || id !== state.instrumentId) return;
    state.h1Layers = data;
    state.h1LoadError = false;
  } catch (e) {
    if (token !== state.h1Req || id !== state.instrumentId) return;
    state.h1LoadError = true;
    state.h1Layers = {
      detected_zones: [],
      structural_events: [],
      layer_status: { zones: { state: 'error', total: 0 } },
      snapshot: {},
    };
  }
  paintH1Markers();
  drawLtfLayers();
}

function scheduleH1HistoryReload() {
  if (!window.H1Layers || window.H1Layers.loadSettings().points !== 'history') return;
  clearTimeout(state.h1HistoryTimer);
  state.h1HistoryTimer = setTimeout(() => {
    loadH1Structure().catch((e) => console.warn('h1 history:', e));
  }, 300);
}

function onH1LayersChange(settings) {
  const prev = state.h1Settings || window.H1Layers.loadSettings();
  state.h1Settings = settings;
  const refetch = prev.points !== settings.points || !!prev.diagnostic !== !!settings.diagnostic
    || !!prev.historicalZones !== !!settings.historicalZones;
  if (refetch) {
    loadH1Structure().catch((e) => console.warn('h1 layers:', e));
    return;
  }
  paintH1Markers();
  drawLtfLayers();
}

async function closeSelectedHtfIdea(ideaId) {
  await api(`/api/ltf/ideas/${ideaId}/close`, { method: 'POST' });
  await loadH1Structure();
}

function resetH1ZoneFilters() {
  if (!window.H1Layers) return;
  window.H1Layers.saveSettings({
    zones: true, ob: true, fvg: true, bsl: true, ssl: true,
    eligibleOnly: false, ideaId: '', candidates: false, historicalZones: false,
  });
  window.H1Layers.bindControls(onH1LayersChange, closeSelectedHtfIdea);
  state.h1Settings = window.H1Layers.loadSettings();
  drawLtfLayers();
}

function focusH1Zone(zone) {
  if (!state.chart || !zone || !state.candles.length) return;
  const fromMs = zone.display_from || zone.formed_at;
  if (fromMs == null) return;
  const origin = Math.floor(Number(fromMs) / 1000);
  const last = state.candles[state.candles.length - 1].time;
  const first = state.candles[0].time;
  state.h1SelectedZoneId = zone.id;
  state.chart.timeScale().setVisibleRange({
    from: Math.max(first, origin - 48 * 3600),
    to: Math.min(last + 4 * 3600, Math.max(origin + 96 * 3600, first + 3600)),
  });
  applyLtfPriceScale({ lower: zone.lower, upper: zone.upper });
}

function drawLtfLayers() {
  const overlay = $('ltf-overlay');
  overlay.innerHTML = '';
  if (!state.candleSeries || !state.candles.length) {
    hideH1Chrome();
    return;
  }
  if (!state.layers && !state.h1Layers) return;
  const chartEl = $('chart');
  const width = chartEl.clientWidth;
  const height = chartEl.clientHeight;
  const paneRight = width - state.chart.priceScale('right').width();
  const ts = state.chart.timeScale();
  const layers = state.layers || {};
  const toggles = state.layerToggles;
  const showHtf = !window.H1Layers || window.H1Layers.loadSettings().htfContext;

  const xOf = (ms) => ts.timeToCoordinate(Math.floor(ms / 1000));
  const yOf = (p) => state.candleSeries.priceToCoordinate(p);
  const addDiv = (cls, title) => {
    const div = document.createElement('div');
    div.className = cls;
    if (title) div.title = title;
    overlay.appendChild(div);
    return div;
  };
  // горизонтальная полоса по ценам, обрезанная по видимой области
  const band = (upper, lower, cls, title) => {
    const y1 = yOf(upper);
    const y2 = yOf(lower);
    if (y1 === null && y2 === null) return null;
    const ya = y1 === null ? (upper > lower ? 0 : height) : y1;
    const yb = y2 === null ? (lower < upper ? height : 0) : y2;
    let top = Math.max(0, Math.min(ya, yb));
    let bottom = Math.min(height, Math.max(ya, yb));
    if (bottom < 0 || top > height) return null;
    const div = addDiv(cls, title);
    div.style.top = top + 'px';
    div.style.height = Math.max(6, bottom - top) + 'px';
    return div;
  };
  const hline = (price, x1, x2, cls, title) => {
    const y = yOf(price);
    if (y === null || y < -4 || y > height + 4) return null;
    const div = addDiv(cls, title);
    div.style.top = y + 'px';
    div.style.left = Math.max(0, x1) + 'px';
    div.style.width = Math.max(4, Math.min(paneRight, x2) - Math.max(0, x1)) + 'px';
    return div;
  };

  // 1) родительская HTF-зона выбранного контекста: линии границ от
  //    формирования вправо с ценой на конце (без сплошного блока)
  const parent = layers.parent_zone;
  if (parent && showHtf) {
    const fromMs = parent.display_from || parent.formed_at;
    let x1 = fromMs ? xOf(fromMs) : 0;
    if (x1 === null || x1 < 0) x1 = 0;
    let x2 = paneRight;
    if (parent.display_until) {
      const xc = xOf(parent.display_until);
      if (xc !== null) x2 = Math.min(paneRight, xc);
    }
    const label = ctxLabel(parent.type, parent.timeframe, parent.direction);
    for (const price of [parent.upper, parent.lower]) {
      const div = hline(price, x1, x2, 'ltf-parent-line',
        `HTF ${label} [${fmtPrice(parent.lower)}–${fmtPrice(parent.upper)}]`);
      if (div && x2 - x1 >= 40) div.textContent = fmtPrice(price);
    }
  }

  // 1b) актуальная цена: пунктир только вправо от последней свечи
  //     (метка на ценовой шкале — у priceLine, линия на всю ширину выкл.)
  if (state.candles.length) {
    const price = state.lastPrice != null ? state.lastPrice
      : state.candles[state.candles.length - 1].close;
    const xLast = xOf(state.candles[state.candles.length - 1].time * 1000);
    if (xLast !== null && xLast < paneRight) {
      hline(price, xLast, paneRight, 'ltf-price-now',
        `${state.lastPrice != null ? 'цена' : 'закрытие'} ${fmtPrice(price)}`);
    }
  }

  // 2) рабочий диапазон 0/50/100 с подписями Premium/Discount (только текущий)
  //    рисуется от структурных опор (anchor-pivots пары §7) вправо, не на всю ширину
  const rangePivotById = new Map((layers.pivots || []).map((p) => [p.id, p]));
  const rangeX1 = (r) => {
    const times = [r.anchor_low_pivot_id, r.anchor_high_pivot_id]
      .map((id) => rangePivotById.get(id))
      .filter(Boolean)
      .map((p) => p.pivot_at);
    if (!times.length) return 0;
    const x = xOf(Math.min(...times));
    return (x === null || x < 0) ? 0 : x;
  };
  const setup = layers.setup;
  const cur = (layers.ranges || []).find((r) => r.current);
  if (setup && setup.lower != null && setup.upper != null) {
    const originAt = setup.origin_anchor && setup.origin_anchor.at;
    const sx = originAt != null ? xOf(originAt) : 0;
    const rx = (sx === null || sx < 0) ? 0 : sx;
    const provisional = setup.range_status !== 'confirmed';
    const cls = 'ltf-range-line' + (provisional ? ' ltf-range-provisional' : ' ltf-range-confirmed');
    const title = (setup.history_label ? 'история · ' : '')
      + (setup.label || 'PD H1 текущего движения') + ' · ' + (setup.pd_label || '');
    hline(setup.upper, rx, paneRight, cls, title + ' · H ' + fmtPrice(setup.upper));
    hline(setup.eq, rx, paneRight, cls + ' ltf-range-mid', title + ' · 50% ' + fmtPrice(setup.eq));
    hline(setup.lower, rx, paneRight, cls, title + ' · L ' + fmtPrice(setup.lower));
  } else if (cur) {
    const rx = rangeX1(cur);
    const pb = band(cur.upper, cur.mid, 'ltf-half-premium',
      `Premium [${fmtPrice(cur.mid)}; ${fmtPrice(cur.upper)}]`);
    const db = band(cur.mid, cur.lower, 'ltf-half-discount',
      `Discount [${fmtPrice(cur.lower)}; ${fmtPrice(cur.mid)}]`);
    if (pb) { pb.style.left = rx + 'px'; pb.style.width = (paneRight - rx) + 'px'; pb.textContent = 'Premium'; }
    if (db) { db.style.left = rx + 'px'; db.style.width = (paneRight - rx) + 'px'; db.textContent = 'Discount'; }
    hline(cur.upper, rx, paneRight, 'ltf-range-line',
      `R_high ${fmtPrice(cur.upper)} · v${cur.version} · доступен ${fmtTime(cur.available_at)}`);
    hline(cur.mid, rx, paneRight, 'ltf-range-line ltf-range-mid', `M ${fmtPrice(cur.mid)}`);
    hline(cur.lower, rx, paneRight, 'ltf-range-line', `R_low ${fmtPrice(cur.lower)} · v${cur.version}`);
  }

  // 2b) «История сценариев»: предыдущие версии диапазона — пунктир (§4.3)
  if (toggles.history) {
    for (const r of layers.ranges || []) {
      if (r.current) continue;
      const rx = rangeX1(r);
      hline(r.upper, rx, paneRight, 'ltf-range-line ltf-range-old',
        `R_high ${fmtPrice(r.upper)} · v${r.version} (старая версия)`);
      hline(r.mid, rx, paneRight, 'ltf-range-line ltf-range-old ltf-range-mid',
        `M ${fmtPrice(r.mid)} · v${r.version}`);
      hline(r.lower, rx, paneRight, 'ltf-range-line ltf-range-old',
        `R_low ${fmtPrice(r.lower)} · v${r.version}`);
    }
  }

  // 2c) «Предварительный диапазон» (§16.2 — предлагаемый режим): пунктир,
  //     подпись «предварительный», визуально отличим от подтверждённого.
  //     Только отображение: зоны/сигналы/уведомления из него не строятся.
  const prov = state.current && state.current.provisional_range;
  if (toggles.provisional && prov) {
    const refX = prov.ref_pivot_at ? xOf(prov.ref_pivot_at) : null;
    const x1 = (refX === null || refX < 0) ? 0 : refX;
    const provLine = (price, label) => {
      const div = hline(price, x1, paneRight, 'ltf-range-line ltf-range-provisional',
        `${label} ${fmtPrice(price)} · предварительный диапазон (конец движения ` +
        `ещё не подтверждён 3 правыми свечами) · §16.2 предлагаемый режим`);
      if (div && paneRight - x1 >= 90) div.textContent = label;
    };
    provLine(prov.upper, 'предварительный');
    provLine(prov.mid, 'предв. 50%');
    provLine(prov.lower, 'предварительный');
  }

  // 3) Зоны сценария. Если расчёт H1 инструмента уже пришёл, его зоны
  //    рисует общий слой ниже — здесь те же геометрии не дублируются.
  const useDetected = !!(state.h1Layers && Array.isArray(state.h1Layers.detected_zones) && !state.h1LoadError);
  if (!useDetected) for (const e of entryList()) {
    const id = e.entry_zone_id || e.id;
    const fromMs = e.confirmed_at || e.formed_at;
    let x1 = xOf(fromMs);
    if (x1 === null || x1 < 0) x1 = 0;
    const stateKey = e.entry_state || e.state || '';
    const excluded = e.reason && e.reason !== 'ok' && !e.outside_premium;
    const cls = `ltf-entry ltf-entry-${String(e.type || '').toLowerCase()} es-${stateKey}` +
      (excluded ? ' ltf-entry-excluded' : '') +
      (e.overlap === 'partial' || e.partial ? ' partial' : '') +
      (id === state.selectedEntryZoneId ? ' selected' : '');
    const title =
      `${e.type} H1 [${fmtPrice(e.lower)}–${fmtPrice(e.upper)}] · ${ENTRY_STATE_RU[stateKey] || stateKey}` +
      `${e.overlap === 'partial' || e.partial ? ' · частично' : ''}` +
      `${e.outside_premium ? ' · вне Premium (допущено по контексту §18)' : ''}` +
      `${excluded ? ' · исключена: ' + (REASON_RU[e.reason] || e.reason) : ''}`;
    if (x1 > paneRight) continue;
    if (e.is_level) {
      hline(e.lower, x1, paneRight, cls + ' ltf-level', title);
    } else {
      const div = band(e.upper, e.lower, cls, title);
      if (div) {
        div.style.left = x1 + 'px';
        div.style.width = Math.max(8, paneRight - x1) + 'px';
        div.textContent = `${e.type} H1`;
      }
    }
  }

  // 4–4b) BOS/SMS и ожидаемые уровни рисует общий слой H1Layers ниже.
  //    Начало отрезка берётся только из известной опоры.

  // 5) «Подробная структура»: запасные маркеры, если расчёт точек H1 не пришёл
  if (toggles.structure && !(state.h1Layers && Array.isArray(state.h1Layers.pivot_markers))) {
    for (const p of layers.pivots || []) {
      if (p.state !== 'confirmed') continue;
      const x = xOf(p.pivot_at);
      const y = yOf(p.price);
      if (x === null || y === null || x < 0 || x > paneRight || y < 0 || y > height) continue;
      const div = addDiv(
        `ltf-pivot ltf-pivot-${p.kind} role-${p.role}`,
        `${p.role !== 'none' ? p.role : p.kind} · экстремум ${fmtTime(p.pivot_at)}` +
        ` · подтверждён ${p.confirmed_at ? fmtTime(p.confirmed_at) : 'ещё нет'}`);
      div.style.left = (x - 5) + 'px';
      div.style.top = (p.kind === 'high' ? y - 12 : y + 4) + 'px';
      div.textContent = p.role !== 'none' ? p.role : '';
    }
  }

  // 6) «Внутренняя ликвидность»: internal high/low (скрыты по умолчанию)
  if (toggles.liquidity) {
    for (const p of layers.pivots || []) {
      if (p.role !== 'internal_high' && p.role !== 'internal_low') continue;
      const x1 = xOf(p.pivot_at);
      if (x1 === null || x1 > paneRight) continue;
      hline(p.price, Math.max(0, x1), paneRight, 'ltf-internal',
        `${p.role} · ${fmtPrice(p.price)} · экстремум ${fmtTime(p.pivot_at)}`);
    }
  }

  if (!window.H1Layers || !state.h1Layers) return;
  const viewRange = ts.getVisibleRange();
  let priceMin = null;
  let priceMax = null;
  const top = state.candleSeries.coordinateToPrice(0);
  const bottom = state.candleSeries.coordinateToPrice(height);
  if (top != null && bottom != null) {
    priceMin = Math.min(top, bottom);
    priceMax = Math.max(top, bottom);
  }
  window.H1Layers.draw(overlay, {
    layers: state.h1Layers,
    settings: window.H1Layers.loadSettings(),
    xOf,
    yOf,
    paneRight,
    height,
    fmtPrice,
    fmtTime,
    view: {
      timeFrom: viewRange ? Math.floor(Number(viewRange.from) * 1000) : null,
      timeTo: viewRange ? Math.ceil(Number(viewRange.to) * 1000) : null,
      priceMin,
      priceMax,
    },
    selectedZoneId: state.h1SelectedZoneId,
    loadError: state.h1LoadError,
    transitionEl: $('h1-transition'),
    statusEl: $('h1-layer-status'),
    offscreenEl: $('h1-offscreen'),
    onZone: (zone, adm) => {
      state.h1SelectedZoneId = zone.id;
      drawLtfLayers();
      showH1Card(window.H1Layers.zoneCard(zone, adm, { time: fmtTime, price: fmtPrice }));
    },
    onEvent: (events) => {
      const first = events && events[0];
      state.h1SelectedEventId = first ? first.id : null;
      paintH1Markers();
      showH1Card((events || []).map((ev) => window.H1Layers.eventCard(ev, { time: fmtTime, price: fmtPrice })).join(''));
    },
    onShowZone: focusH1Zone,
    onRetry: () => loadH1Structure().catch((e) => console.warn('h1 retry:', e)),
    onResetFilters: resetH1ZoneFilters,
    onShowAllZones: () => {
      window.H1Layers.saveSettings({ eligibleOnly: false });
      const box = $('h1-eligible-only');
      if (box) box.checked = false;
      state.h1Settings = window.H1Layers.loadSettings();
      drawLtfLayers();
    },
  });
}

function focusEntryZone(zoneId) {
  const row = entryList().find((e) => (e.entry_zone_id || e.id) === zoneId)
    || ((state.current && state.current.eligible_entries) || [])
      .find((e) => e.entry_zone_id === zoneId);
  if (!row || !state.chart || !state.candles.length) return;
  const fromMs = row.formed_at || row.confirmed_at;
  const origin = Math.floor(fromMs / 1000);
  const last = state.candles[state.candles.length - 1].time;
  const first = state.candles[0].time;
  const from = Math.max(first, origin - 48 * 3600);
  const to = Math.min(last + 4 * 3600, Math.max(origin + 80 * 3600, from + 96 * 3600));
  state.selectedEntryZoneId = zoneId;
  state.chart.timeScale().setVisibleRange({ from, to });
  applyLtfPriceScale(row);
  rerenderTables();
  requestAnimationFrame(() => requestAnimationFrame(drawLtfLayers));
}

// ---------------------------------------------------------------------------
// Настройки LTF — через общий /api/settings
// ---------------------------------------------------------------------------

async function openSettings() {
  const instruments = await api('/api/instruments');
  const s = await api('/api/settings');
  const d = s.detector;
  state.ltfSettingsOriginal = d;
  const entryTypes = new Set((d.ltf_entry_types || '').split(','));
  const notifyKinds = new Set((d.ltf_notify_kinds || '').split(','));
  const ctxTypes = new Set((d.htf_context_types || 'OB').split(','));
  $('ltf-settings-form').innerHTML = `
    <label><input type="checkbox" id="ls-enabled" ${d.ltf_enabled ? 'checked' : ''}> Включить LTF-мониторинг</label>
    <fieldset><legend>HTF-контекст для запуска наблюдения (D1/W1)</legend>
      <label><input type="checkbox" class="ls-ctx" value="OB" ${ctxTypes.has('OB') ? 'checked' : ''}> OB — согласованный триггер</label>
      <label><input type="checkbox" class="ls-ctx" value="FVG" ${ctxTypes.has('FVG') ? 'checked' : ''}> FVG — контекст D1/W1</label>
      <small>PRB/Breaker/BSL/SSL триггерами не являются. Тест и актуальность FVG — по общему движку: касание 50% не прекращает FVG.</small>
    </fieldset>
    <label>Структурные pivots слева (3–5)
      <input id="ls-left" type="number" min="3" max="5" value="${d.ltf_structure_left}"></label>
    <label>Структурные pivots справа (3–5)
      <input id="ls-right" type="number" min="3" max="5" value="${d.ltf_structure_right}"></label>
    <label>Опоры диапазона: правые свечи
      <input id="ls-range-right" type="number" value="${d.ltf_range_right}" disabled>
      <small>Поле устарело и не редактируется: опоры диапазона подтверждаются ровно тремя закрытыми свечами справа, отдельно от структурного профиля.</small></label>
    <fieldset><legend>Типы отображаемых Entry Zones</legend>
      ${['FVG', 'OB', 'BSL', 'SSL'].map((t) =>
        `<label><input type="checkbox" class="ls-et" value="${t}" ${entryTypes.has(t) ? 'checked' : ''}> ${t}</label>`).join('')}
    </fieldset>
    <fieldset><legend>Доставка (выключение не меняет анализ)</legend>
      ${[['bos_sms', 'BOS/SMS'], ['entries_ready', 'готовые Entry Zones'],
         ['touch', 'касание'], ['sweep_outcome', 'исход liquidity-теста'],
         ['cancellation', 'отмена']].map(([v, label]) =>
        `<label><input type="checkbox" class="ls-nk" value="${v}" ${notifyKinds.has(v) ? 'checked' : ''}> ${label}</label>`).join('')}
    </fieldset>
    <fieldset><legend>Анализировать без касания HTF-зоны</legend>
      ${instruments.map((ins) =>
        `<label class="check-label"><input type="checkbox" class="ls-analyze" data-instrument-id="${ins.id}" ${ins.ltf_analyze ? 'checked' : ''}> ${esc(ins.symbol)} · ${esc(ins.venue)}</label>`).join('') || '<p class="ltf-empty">Нет инструментов.</p>'}
      <small>По умолчанию выключено: LTF ждёт нового касания подтверждённого OB D1/W1. Включите актив, чтобы открыть наблюдения сразу по всем таким OB. Снятие галочки не закрывает уже открытые.</small>
    </fieldset>
    <label>Опрос H1, секунд <input id="ls-poll" type="number" min="30" value="${d.ltf_poll_seconds}"></label>
    <label><input type="checkbox" id="ls-provisional" ${d.ltf_provisional_range_enabled ? 'checked' : ''}> Предварительный диапазон — предлагаемый режим (§16.2), владельцем не подтверждён
      <small>Выключен по умолчанию. Только отображение: пунктирный слой временного конца текущего движения до подтверждения опор тремя правыми свечами. Не создаёт подтверждённые сигналы, версии диапазона и уведомления.</small></label>`;
  HTF.openModal($('ltf-settings-modal'));
  $('ltf-settings-status').textContent = '';
}

async function saveSettings() {
  const status = $('ltf-settings-status');
  status.textContent = '';
  const d = state.ltfSettingsOriginal || {};
  const cand = {
    ltf_enabled: $('ls-enabled').checked,
    ltf_structure_left: Number($('ls-left').value),
    ltf_structure_right: Number($('ls-right').value),
    // ltf_range_right — устаревшее поле (L04), сервер его отклоняет
    ltf_entry_types: [...document.querySelectorAll('.ls-et:checked')].map((c) => c.value).join(','),
    ltf_notify_kinds: [...document.querySelectorAll('.ls-nk:checked')].map((c) => c.value).join(','),
    htf_context_types: [...document.querySelectorAll('.ls-ctx:checked')].map((c) => c.value).join(',') || 'OB',
    ltf_provisional_range_enabled: $('ls-provisional').checked,
    ltf_poll_seconds: Number($('ls-poll').value),
  };
  // L04: сервер валидирует патч строго — отправляем только изменённые поля
  const payload = {};
  for (const [key, val] of Object.entries(cand)) {
    if (val !== d[key]) payload[key] = val;
  }
  try {
    const doFetch = () => fetch('/api/settings', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + window.HTF.getToken(),
      },
      body: JSON.stringify(payload),
    });
    let resp = await doFetch();
    if (resp.status === 401) {
      localStorage.removeItem('htf_token');
      await window.HTF.ensureToken('Токен не подошёл. Проверьте значение и попробуйте снова.');
      resp = await doFetch();
    }
    if (resp.status === 422) {
      let fields = {};
      try { fields = ((await resp.json()).detail || {}).fields || {}; } catch (e) { /* не JSON */ }
      status.textContent = 'Не удалось сохранить: ' +
        (Object.entries(fields).map(([k, m]) => `${k}: ${m}`).join('; ') ||
          'настройки отклонены сервером');
      return;
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try { detail = (await resp.json()).detail || detail; } catch (e) { /* не JSON */ }
      status.textContent = 'Не удалось сохранить: ' +
        (typeof detail === 'string' ? detail : 'ошибка сервера');
      return;
    }
    const r = await resp.json();
    state.ltfSettingsOriginal = r.detector || { ...d, ...payload };
    let analyzeSaved = 0;
    for (const c of document.querySelectorAll('.ls-analyze')) {
      const id = Number(c.dataset.instrumentId);
      if (!id) continue;
      await api(`/api/instruments/${id}/ltf-analyze`, {
        method: 'POST', body: JSON.stringify({ analyze: c.checked }),
      });
      analyzeSaved += 1;
    }
    await loadAssets();
    if (state.instrumentId) await reloadCurrent({ keepRange: true });
    status.textContent = analyzeSaved
      ? `Сохранено: ${r.applied.length} параметров и ${analyzeSaved} активов.`
      : `Сохранено: ${r.applied.length} параметров.`;
  } catch (err) {
    status.textContent = 'Не удалось сохранить: ' + err.message;
  }
}

// ---------------------------------------------------------------------------
// Рабочее место: вкладки, фильтры, масштаб, слои
// ---------------------------------------------------------------------------

function activateBottomTab(name) {
  const root = document.querySelector('.ltf-layout');
  root.querySelectorAll('.bottom-tab').forEach((button) => {
    const active = button.dataset.panel === name;
    button.classList.toggle('active', active);
    button.setAttribute('aria-selected', String(active));
  });
  root.querySelectorAll('[data-content]').forEach((panel) =>
    panel.classList.toggle('hidden', panel.dataset.content !== name));
  state.bottomTab = name;
  sessionStorage.setItem('ltf-bottom-tab', name);
  if (name === 'history') renderHistory();
}

function setMode(mode) {
  state.mode = mode;
  $('ltf-mode-now').classList.toggle('active', mode === 'now');
  $('ltf-mode-history').classList.toggle('active', mode === 'history');
  // «История» — режим просмотра: старые версии диапазона и вкладка истории;
  // расчёт структуры и актуальность зон это не меняет (п.05)
  state.layerToggles.history = mode === 'history';
  const cb = document.querySelector('#ltf-layer-toggles [data-layer="history"]');
  if (cb) cb.checked = state.layerToggles.history;
  activateBottomTab(mode === 'history' ? 'history' : 'eligible');
  drawLtfLayers();
}

function setupLtfWorkspace() {
  const root = document.querySelector('.ltf-layout');
  root.querySelectorAll('.bottom-tab').forEach((button) => {
    button.onclick = () => activateBottomTab(button.dataset.panel);
  });
  const initialTab = sessionStorage.getItem('ltf-bottom-tab');
  activateBottomTab(['eligible', 'events', 'history'].includes(initialTab) ? initialTab : 'eligible');
  const collapse = $('ltf-bottom-collapse');
  const setCollapsed = (value) => {
    root.style.gridTemplateRows = '';
    root.classList.toggle('bottom-collapsed', value);
    collapse.textContent = value ? 'Развернуть' : 'Свернуть';
    collapse.setAttribute('aria-expanded', String(!value));
    sessionStorage.setItem('ltf-bottom-collapsed', String(value));
    requestAnimationFrame(drawLtfLayers);
    window.dispatchEvent(new Event('resize'));
  };
  collapse.onclick = () => setCollapsed(!root.classList.contains('bottom-collapsed'));
  setCollapsed(sessionStorage.getItem('ltf-bottom-collapsed') === 'true');

  const filters = $('ltf-filters');
  const toggle = $('ltf-filter-toggle');
  toggle.onclick = () => {
    filters.classList.toggle('hidden');
    toggle.setAttribute('aria-expanded', String(!filters.classList.contains('hidden')));
  };
  const updateCount = () => {
    const count = [...filters.querySelectorAll('select, input')].filter((el) => el.value).length;
    $('ltf-filter-count').textContent = count ? `· ${count}` : '';
  };
  filters.addEventListener('change', () => { updateCount(); renderAssetsList(); });
  $('flt-symbol').addEventListener('input', () => { updateCount(); renderAssetsList(); });
  updateCount();

  // L06: сортировка «По приоритету» — только по кнопке (повторное нажатие —
  // возврат к алфавитному порядку); режим хранится в состоянии страницы,
  // не в localStorage. Фильтры работают поверх любого порядка.
  const sortBtn = $('ltf-sort-priority');
  sortBtn.onclick = () => {
    state.sortMode = state.sortMode === 'priority' ? 'alpha' : 'priority';
    const active = state.sortMode === 'priority';
    sortBtn.setAttribute('aria-pressed', String(active));
    sortBtn.classList.toggle('active', active);
    renderAssetsList();
    // выбранный актив остаётся выделенным и видимым при любой сортировке
    const sel = $('ltf-obs-list').querySelector('.ltf-asset.selected');
    if (sel) sel.scrollIntoView({ block: 'nearest' });
  };

  document.querySelectorAll('.ltf-scale-group .ltf-tab').forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll('.ltf-scale-group .ltf-tab').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      state.scaleDays = Number(btn.dataset.days);
      // смена масштаба — только отображение; структуру и зоны не трогает (п.05)
      if (state.candles.length) {
        state.chart.timeScale().setVisibleLogicalRange(
          defaultVisibleLogicalRange(state.scaleDays));
      }
    };
  });
  $('ltf-to-price').onclick = () => {
    state.priceFocus = null;
    applyLtfPriceScale(null);
    if (state.candles.length) {
      state.chart.timeScale().setVisibleLogicalRange(
        defaultVisibleLogicalRange(state.scaleDays));
    }
  };
  $('ltf-mode-now').onclick = () => setMode('now');
  $('ltf-mode-history').onclick = () => setMode('history');
  if (window.H1Layers) {
    state.h1Settings = window.H1Layers.loadSettings();
    window.H1Layers.bindControls(onH1LayersChange, closeSelectedHtfIdea);
  }
  document.querySelectorAll('#ltf-layer-toggles input').forEach((cb) => {
    cb.onchange = () => {
      state.layerToggles[cb.dataset.layer] = cb.checked;
      if (cb.dataset.layer === 'provisional') state.provUserSet = true;
      drawLtfLayers();
    };
  });
  $('flt-hist-view').onchange = () => { state.historyRows = null; renderHistory(); };
  $('flt-hist-reason').onchange = () => renderHistory();
}

// ---------------------------------------------------------------------------
// WebSocket (§14): обновления без перезагрузки; после reconnect — полный снимок
// ---------------------------------------------------------------------------

function handleWsMessage(data) {
  // F04: каждое WS-сообщение несёт state_seq; сообщение старше уже
  // применённого снимка — позднее эхо, перезагружать экран не нужно
  if (typeof data.state_seq === 'number' && state.appliedSeq &&
      data.state_seq < state.appliedSeq) return;
  if (data.type === 'price') {
    const ins = state.current && state.current.instrument;
    if (ins && data.instrument_id === ins.id && data.price) {
      state.lastPrice = data.price;
      $('ltf-last-price').textContent = fmtPrice(data.price);
      updatePriceLine();
      drawLtfLayers();
      // dist в таблицах пересчитываем локально по той же формуле (§12)
      for (const tr of document.querySelectorAll('.ltf-bottom tbody tr.entry-row')) {
        const cell = tr.querySelector('.col-dist');
        if (!cell) continue;
        const zid = Number(tr.dataset.zoneId);
        const row = entryList().find((e) => (e.entry_zone_id || e.id) === zid)
          || ((state.current && state.current.eligible_entries) || [])
            .find((e) => e.entry_zone_id === zid);
        if (row) {
          const d = distOf(row, state.lastPrice);
          cell.textContent = d ? d.pct.toFixed(2) + '%' : '—';
        }
      }
    }
  } else if (data.type === 'ltf') {
    if (state.instrumentId && data.instrument_id !== state.instrumentId) return;
    clearTimeout(state.reloadTimer);
    state.reloadTimer = setTimeout(async () => {
      await loadAssets();
      if (state.instrumentId) await reloadCurrent({ keepRange: true });
    }, 500);
  } else if (data.type === 'candle' && data.timeframe === 'H1') {
    const ins = state.current && state.current.instrument;
    if (ins && data.instrument_id === ins.id && state.instrumentId) {
      clearTimeout(state.reloadTimer);
      state.reloadTimer = setTimeout(() => reloadCurrent({ keepRange: true }), 500);
    }
  }
}

// ---------------------------------------------------------------------------
// Deep-link: ?obs= и ?zone_id= маппируются на контекст (§14)
// ---------------------------------------------------------------------------

async function resolveDeepLink() {
  const params = new URLSearchParams(location.search);
  const obs = Number(params.get('obs')) || null;
  const zoneId = Number(params.get('zone_id')) || null;
  const instrumentParam = Number(params.get('instrument')) || null;
  if (obs) {
    try {
      const card = await api(`/api/ltf/observations/${obs}`);
      const iid = card.instrument && card.instrument.id;
      if (iid) {
        try {
          await api(`/api/ltf/instruments/${iid}/select-context`, {
            method: 'POST', body: JSON.stringify({ observation_id: obs }),
          });
        } catch (e) { /* контекст в истории — выберет политика сервера */ }
        return iid;
      }
    } catch (e) { /* наблюдение недоступно — fallback ниже */ }
  }
  if (zoneId) {
    try {
      const observations = await api('/api/ltf/observations?tab=all');
      const match = observations.find((o) => o.parent_zone && o.parent_zone.id === zoneId);
      if (match && match.instrument) {
        try {
          await api(`/api/ltf/instruments/${match.instrument.id}/select-context`, {
            method: 'POST', body: JSON.stringify({ observation_id: match.id }),
          });
        } catch (e) { /* в истории */ }
        return match.instrument.id;
      }
    } catch (e) { /* fallback ниже */ }
  }
  if (instrumentParam && state.assets.some((a) => a.instrument.id === instrumentParam)) {
    return instrumentParam;
  }
  const saved = readSavedInstrument();
  if (saved && state.assets.some((a) => a.instrument.id === saved)) return saved;
  return state.assets.length ? state.assets[0].instrument.id : null;
}

// ---------------------------------------------------------------------------
// Старт
// ---------------------------------------------------------------------------

async function main() {
  await window.HTF.ensureToken();
  setupLtfWorkspace();
  updateHtfLink();
  initChart();

  $('ltf-instrument').onchange = (e) => {
    if (e.target.value) selectInstrument(Number(e.target.value));
  };
  $('ltf-burger').onclick = () => $('ltf-obs-panel').classList.toggle('open');
  $('ltf-card-close').onclick = closeInspector;
  $('ltf-inspector').addEventListener('click', (event) => {
    if (isCompactLtf() && !isMobileLtf() && event.target === $('ltf-inspector')) closeInspector();
  });
  $('ltf-close-cancel').onclick = () => HTF.closeModal($('ltf-close-modal'));
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (!$('ltf-close-modal').classList.contains('hidden')) HTF.closeModal($('ltf-close-modal'));
    else if (!$('ltf-settings-modal').classList.contains('hidden')) HTF.closeModal($('ltf-settings-modal'));
    else if ($('ltf-inspector').classList.contains('open')) closeInspector();
    else $('ltf-obs-panel').classList.remove('open');
  });
  $('btn-ltf-settings').onclick = openSettings;
  $('ltf-settings-save').onclick = saveSettings;
  $('ltf-settings-cancel').onclick = () => HTF.closeModal($('ltf-settings-modal'));
  window.addEventListener('resize', () => {
    if (!isCompactLtf()) $('ltf-inspector').classList.remove('open');
  });

  await loadLabels();
  await loadAssets();
  const initial = await resolveDeepLink();
  if (initial) {
    await selectInstrument(initial);
  } else {
    $('ltf-chart-empty').textContent = 'Активов с LTF-наблюдениями нет — включите «Анализировать» в настройках LTF или дождитесь касания HTF-зоны.';
    $('ltf-chart-empty').classList.remove('hidden');
  }
  // после reconnect — полный снимок: пропущенные сообщения не должны
  // удерживать отменённый сценарий текущим (§14, п.18)
  state.ws = window.HTF.connectWs(handleWsMessage, $('ws-indicator'), async (isReconnect) => {
    if (!isReconnect) return;
    await loadAssets();
    if (state.instrumentId) await reloadCurrent({ keepRange: true });
  });
}

document.addEventListener('lf-reconciled', () => {
  if (state.instrumentId) reloadCurrent({ keepRange: true });
});

main().catch((err) => {
  document.body.insertAdjacentHTML('beforeend',
    `<div style="position:fixed;bottom:10px;left:10px;background:#ef5350;color:#fff;padding:8px 14px;border-radius:6px;z-index:200">Ошибка: ${esc(err.message)}</div>`);
});
