/* Окно «Альткоины» — D1-аккумуляция после глубокого падения
   (ТЗ 07.10.2026 §16–§18): таблица с фильтрами и ранжированием сервера,
   большой D1-график выбранного сетапа, панель «Почему найдено», статус
   дневного прогона. Обновление после daily job — по WS {"type":"alt"},
   без ручной перезагрузки (§18). Vanilla JS + lightweight-charts;
   слои — price lines + DOM-overlay поверх графика (как ltf.js).
   Общие helper'ы — common.js (window.HTF). */

'use strict';

const { api, fmtPrice, fmtTime, esc } = window.HTF;
const $ = (id) => document.getElementById(id);
const DAY_MS = 86_400_000;

const state = {
  rows: [],
  bucket: 'eligible',
  selected: null,          // {kind: 'setup'|'candidate', id}
  detail: null,
  candles: [],
  chart: null,
  candleSeries: null,
  priceLines: [],
  reloadTimer: null,
  reqSeq: 0,
};

// ---------------------------------------------------------------------------
// Статус прогона (§18)
// ---------------------------------------------------------------------------

async function loadRunStatus() {
  const st = await api('/api/alt/run-status');
  const last = st.last_run;
  $('alt-run-last').textContent = last
    ? `Последний run: ${fmtTime(last.finished_ms || last.started_ms)} · ${last.status}` +
      (last.trigger === 'manual' ? ' (ручной)' : '')
    : 'Последний run: —';
  $('alt-run-asof').textContent = 'as_of: ' + (last && last.as_of_ms ? fmtTime(last.as_of_ms) : '—');
  $('alt-run-next').textContent = st.job_enabled
    ? 'Следующий запуск: ' + fmtTime(st.next_run_ms)
    : 'Расписание выключено';
  $('alt-run-counts').textContent = last
    ? `обработано: ${last.processed} · ошибок: ${last.errors}` +
      (last.skipped && last.skipped.length ? ` · пропущено: ${last.skipped.length}` : '')
    : '';
  // сбой источника ≠ «сетапов нет»: stale показываем явно
  const stale = (st.universe && st.universe.stale) || (last && last.universe_stale);
  $('alt-universe-stale').classList.toggle('hidden', !stale);
  $('alt-running').classList.toggle('hidden', !st.running);
  const attempt = st.last_attempt;
  if (attempt && attempt.status === 'error') {
    $('alt-run-last').textContent += ` · последняя попытка: ошибка (${fmtTime(attempt.finished_ms || attempt.started_ms)})`;
  }
}

// ---------------------------------------------------------------------------
// Таблица (§16) — порядок строк задаёт сервер (ранжирование §17)
// ---------------------------------------------------------------------------

function currentFilters() {
  const p = new URLSearchParams();
  p.set('bucket', state.bucket);
  const v = $('flt-venue').value;
  if (v) p.set('venue', v);
  if ($('flt-rank-min').value) p.set('rank_min', $('flt-rank-min').value);
  if ($('flt-rank-max').value) p.set('rank_max', $('flt-rank-max').value);
  if ($('flt-age-min').value) p.set('age_min', $('flt-age-min').value);
  if ($('flt-dd-min').value) p.set('dd_min', $('flt-dd-min').value);
  if ($('flt-structure').value) p.set('structure', $('flt-structure').value);
  return p;
}

async function loadTable() {
  const data = await api('/api/alt/setups?' + currentFilters());
  state.rows = data.rows;
  // счётчики стадий в подписях селекта
  const sel = $('flt-bucket');
  const names = {
    eligible: 'Подходящие', new_entries: 'Новые входы',
    awaiting_retest: 'Ожидание ретеста', mature: 'Зрелые',
    forming: 'Формирующиеся', review: 'Требуют проверки', history: 'История',
  };
  for (const opt of sel.options) {
    const n = data.buckets[opt.value];
    opt.textContent = names[opt.value]
      ? `${names[opt.value]}${n != null ? ` (${n})` : ''}`
      : opt.value;
  }
  // список бирж — из фактических строк
  const venueSel = $('flt-venue');
  const prev = venueSel.value;
  const venues = [...new Set(state.rows.map((r) => (r.source || {}).venue).filter(Boolean))].sort();
  venueSel.innerHTML = '<option value="">Биржа: все</option>' +
    venues.map((v) => `<option>${esc(v)}</option>`).join('');
  venueSel.value = venues.includes(prev) ? prev : '';
  renderTable();
}

function fmtPct(v, digits = 1) {
  return v == null ? '—' : v.toLocaleString('ru-RU', { maximumFractionDigits: digits }) + '%';
}
function fmtDate(ms) {
  return ms ? new Date(ms).toLocaleDateString('ru-RU', { timeZone: 'Europe/Moscow' }) : '—';
}
function flagsText(r) {
  const f = r.flags || {};
  const out = [];
  if (f.structure_event) out.push('BOS/SMS');
  if (f.ssl_event) out.push('SSL');
  if (f.manipulation_active) out.push('манип.');
  if (f.breakout_confirmed) out.push('выход');
  if (f.retest_received) out.push('ретест');
  return out.join(' ') || '—';
}
function tpText(r) {
  if (!r.targets || !r.targets.length) return '—';
  return r.targets.map((t) => `${t.hit ? '✓' : ''}TP${t.tp}`).join(' ');
}
function kText(r) {
  if (!r.cancel) return '—';
  if (r.cancel.price == null) return '—';
  return `${fmtPrice(r.cancel.price)} · ${r.cancel.mode === 'close_on_closed_d1' ? 'Close' : 'тень'}*`;
}

function renderTable() {
  const tb = $('alt-tbody');
  const empty = $('alt-table-empty');
  if (!state.rows.length) {
    tb.innerHTML = '';
    empty.classList.remove('hidden');
    empty.textContent = 'По выбранным фильтрам строк нет. Это не означает «сетапов нет вообще» — проверьте статус прогона выше.';
    return;
  }
  empty.classList.add('hidden');
  tb.innerHTML = state.rows.map((r) => {
    const id = r.setup_id != null ? `s${r.setup_id}` : (r.candidate_id != null ? `c${r.candidate_id}` : '');
    const sel = state.selected &&
      ((state.selected.kind === 'setup' && r.setup_id === state.selected.id) ||
       (state.selected.kind === 'candidate' && r.candidate_id === state.selected.id));
    const range = r.range || {};
    const width = (r.width_up_pct != null && r.width_down_pct != null)
      ? `+${fmtPct(r.width_up_pct)} / −${fmtPct(r.width_down_pct)}`
      : '—';
    const reason = r.reason || '';
    return `<tr data-id="${id}" class="${sel ? 'selected' : ''}${r.terminal ? ' alt-terminal' : ''}">
      <td title="${esc(r.asset.name || '')}">${esc(r.asset.symbol)} <span class="alt-dim">${esc((r.source || {}).symbol || '')} ${esc((r.source || {}).venue || '')}</span></td>
      <td>${r.asset.cmc_rank || '—'}</td>
      <td>${fmtPrice(r.ath_price)} <span class="alt-dim">${fmtDate(r.ath_open_time)}</span></td>
      <td>${fmtPrice(r.p_min)} <span class="alt-dim">${fmtDate(r.p_min_open_time)}</span></td>
      <td>${fmtPct(r.drawdown_pct)}</td>
      <td>${fmtPrice(r.last_close)}</td>
      <td>${fmtPrice(range.lower)}</td>
      <td>${fmtPrice(range.upper)}</td>
      <td>${fmtPrice(range.mid)}</td>
      <td>${fmtPrice(range.width)}</td>
      <td title="(U/L−1)×100% вверх / (1−L/U)×100% снижение">${width}</td>
      <td>${r.age_days != null ? r.age_days : '—'}</td>
      <td>${esc(r.stage_ru || r.state_ru)}${r.universe_eligible === false ? ' <span class="badge" title="Актив выпал из текущей выборки CMC — наблюдение продолжается до завершения сетапа">вне выборки</span>' : ''}</td>
      <td>${esc(flagsText(r))}</td>
      <td>${r.entry_kinds && r.entry_kinds.length ? esc(r.entry_kinds.join('+')) : '—'}</td>
      <td title="${r.targets.map((t) => `TP${t.tp}=${fmtPrice(t.price)}${t.hit ? ' (пройдена)' : ''}${t.passed_at_confirmation ? ' к моменту подтверждения' : ''}`).join('; ')}">${tpText(r)}</td>
      <td title="Режим отмены — проектная настройка v1, не согласована владельцем">${kText(r)}</td>
      <td>${r.cancel ? (r.cancel.reachable ? 'да' : '<span class="alt-neg">нет</span>') : '—'}</td>
      <td>${r.retest_deadline_ms ? fmtTime(r.retest_deadline_ms) : '—'}</td>
      <td>${r.distance_pct != null ? fmtPct(r.distance_pct) : '—'}</td>
      <td class="alt-dim" title="${esc(reason)}">${esc(r.run_status && r.run_status !== 'processed' ? `${r.run_status}: ${reason || ''}` : (reason || ''))}</td>
    </tr>`;
  }).join('');
  for (const tr of tb.querySelectorAll('tr[data-id]')) {
    tr.onclick = () => {
      const id = tr.dataset.id;
      if (id.startsWith('s')) selectRow('setup', Number(id.slice(1)));
      else if (id.startsWith('c')) selectRow('candidate', Number(id.slice(1)));
    };
  }
}

// ---------------------------------------------------------------------------
// График D1 + слои (§16)
// ---------------------------------------------------------------------------

function initChart() {
  const theme = HTF.chartTheme();
  state.chart = LightweightCharts.createChart($('chart'), {
    layout: { background: { color: '#12161c' }, textColor: theme.text },
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
  state.chart.timeScale().subscribeVisibleLogicalRangeChange(() => drawAltLayers());
  window.addEventListener('resize', drawAltLayers);
  new ResizeObserver(() => drawAltLayers()).observe($('chart-container'));
}

function clearPriceLines() {
  for (const pl of state.priceLines) {
    try { state.candleSeries.removePriceLine(pl); } catch (e) { /* уже снята */ }
  }
  state.priceLines = [];
}

function addPriceLine(price, color, title, style) {
  state.priceLines.push(state.candleSeries.createPriceLine({
    price, color, lineWidth: 1,
    lineStyle: style != null ? style : LightweightCharts.LineStyle.Solid,
    axisLabelVisible: true, title,
  }));
}

async function selectRow(kind, id) {
  state.selected = { kind, id };
  const url = new URL(location.href);
  const row = state.rows.find((r) =>
    (kind === 'setup' && r.setup_id === id) || (kind === 'candidate' && r.candidate_id === id));
  if (row) url.searchParams.set('asset', String(row.asset.cmc_id));
  history.replaceState(null, '', url.pathname + url.search);
  renderTable();
  const req = ++state.reqSeq;
  const detail = await api(kind === 'setup' ? `/api/alt/setup/${id}` : `/api/alt/candidate/${id}`);
  if (req !== state.reqSeq) return; // поздний ответ не перезаписывает выбор
  state.detail = detail;
  $('alt-why-btn').disabled = false;
  renderChart();
  renderWhyButton();
}

function renderChart() {
  const d = state.detail;
  clearPriceLines();
  state.candleSeries.setMarkers([]);
  $('alt-overlay').innerHTML = '';
  const note = $('alt-k-note');
  note.classList.add('hidden');
  if (!d) return;
  const candles = d.candles || [];
  state.candles = candles.map((c) => ({
    time: Math.floor(c.open_time / 1000),
    open: c.open, high: c.high, low: c.low, close: c.close,
  }));
  state.candleSeries.setData(state.candles);
  $('alt-chart-empty').classList.toggle('hidden', state.candles.length > 0);
  $('alt-chart-title').textContent =
    `График · D1 · ${d.asset ? d.asset.symbol : ''} ${d.source ? d.source.symbol + ' · ' + d.source.venue : ''}`;
  if (!state.candles.length) return;

  const frozen = d.frozen_range || d.range || null;
  const YELLOW = '#E3B341', ORANGE = '#E8863A', GREEN = '#62C9B0', RED = '#F08D98';
  if (frozen) {
    addPriceLine(frozen.upper, YELLOW, 'U');
    addPriceLine(frozen.lower, YELLOW, 'L');
    addPriceLine(frozen.mid, ORANGE, 'M', LightweightCharts.LineStyle.Dashed);
  }
  for (const t of d.targets || []) {
    if (t.price != null) {
      addPriceLine(t.price, GREEN, `TP${t.tp}${t.hit ? ' ✓' : ''}`,
        LightweightCharts.LineStyle.SparseDotted);
    }
  }
  // K — ТОЛЬКО при положительном уровне; K<=0 — пояснение без растягивания
  // шкалы к отрицательной цене (§16)
  if (d.cancel && d.cancel.price != null) {
    if (d.cancel.price > 0) {
      addPriceLine(d.cancel.price, RED, 'K', LightweightCharts.LineStyle.Dashed);
    } else {
      note.classList.remove('hidden');
      note.textContent = `Уровень отмены K = ${fmtPrice(d.cancel.price)}. ` +
        'По выбранной формуле ценовой уровень отмены неположительный — ' +
        'линия K не рисуется, шкала не растягивается к отрицательным ценам.';
    }
  }

  // маркеры событий (BOS/SMS зелёные, SSL/вынос красные, выход/входы)
  const markers = [];
  const push = (openTime, m) => {
    if (openTime) markers.push({ time: Math.floor(openTime / 1000), ...m });
  };
  for (const e of d.structure_events || []) {
    const bull = e.kind === 'BOS' || e.kind === 'SMS';
    push(e.candle_open_time, {
      position: bull ? 'belowBar' : 'aboveBar',
      color: bull ? GREEN : RED,
      shape: bull ? 'arrowUp' : 'arrowDown',
      text: e.kind,
    });
  }
  if (d.breakout && d.breakout.closed_at) {
    push(d.breakout.closed_at - DAY_MS, {
      position: 'aboveBar', color: GREEN, shape: 'arrowUp', text: 'выход',
    });
  }
  for (const en of d.entries || []) {
    push(en.event_time_ms - DAY_MS, {
      position: 'belowBar',
      color: en.kind === 'A' ? '#5B8DEF' : '#B07FE8',
      shape: 'circle', text: `вход ${en.kind}`,
    });
  }
  markers.sort((a, b) => a.time - b.time);
  state.candleSeries.setMarkers(markers);

  state.chart.timeScale().fitContent();
  requestAnimationFrame(() => requestAnimationFrame(drawAltLayers));
}

function drawAltLayers() {
  const overlay = $('alt-overlay');
  overlay.innerHTML = '';
  const d = state.detail;
  if (!d || !state.candles.length) return;
  const chartEl = $('chart');
  const width = chartEl.clientWidth;
  const height = chartEl.clientHeight;
  const paneRight = width - state.chart.priceScale('right').width();
  const ts = state.chart.timeScale();
  const xOf = (ms) => ts.timeToCoordinate(Math.floor(ms / 1000));
  const yOf = (p) => state.candleSeries.priceToCoordinate(p);
  const lastOt = state.candles[state.candles.length - 1].time * 1000;

  const box = (x1ms, x2ms, upper, lower, cls, title) => {
    let x1 = xOf(x1ms);
    let x2 = xOf(x2ms);
    if (x1 === null) x1 = 0;
    if (x2 === null) x2 = paneRight;
    x1 = Math.max(0, Math.min(x1, paneRight));
    x2 = Math.max(0, Math.min(x2, paneRight));
    const y1 = yOf(upper);
    const y2 = yOf(lower);
    if (y1 === null && y2 === null) return;
    const top = Math.max(0, Math.min(y1 == null ? 0 : y1, y2 == null ? height : y2));
    const bottom = Math.min(height, Math.max(y1 == null ? 0 : y1, y2 == null ? height : y2));
    if (x2 <= x1 || bottom <= top) return;
    const div = document.createElement('div');
    div.className = cls;
    if (title) div.title = title;
    div.style.left = x1 + 'px';
    div.style.width = (x2 - x1) + 'px';
    div.style.top = top + 'px';
    div.style.height = (bottom - top) + 'px';
    overlay.appendChild(div);
  };

  const frozen = d.frozen_range || d.range || null;
  const anchors = d.anchors;
  if (frozen && anchors && anchors.start) {
    // аккумуляционная рамка: от стартовой опоры до фиксации зрелости
    // (или до текущей свечи для формирующегося), между L и U — жёлтая
    const end = d.frozen_range ? d.frozen_range.mature_at_ms : lastOt + DAY_MS;
    box(anchors.start.open_time, end, frozen.upper, frozen.lower,
      'alt-range-box',
      `Аккумуляция [${fmtPrice(frozen.lower)}–${fmtPrice(frozen.upper)}] · ` +
      `опора доступна ${fmtTime(anchors.start.available_at_ms)}`);
  }
  // слой манипуляции (красный): эпизоды ниже L
  if (frozen) {
    for (const m of d.manipulation_episodes || []) {
      const end = m.ended_candle_open_time != null
        ? m.ended_candle_open_time + DAY_MS : lastOt + DAY_MS;
      box(m.started_candle_open_time, end, frozen.lower, m.min_price,
        'alt-manip-box',
        `Манипуляция: мин. ${fmtPrice(m.min_price)}, дней ниже L: ${m.days_below}`);
    }
    // область ретеста [M,U] после выхода — до deadline/ретеста
    if (d.breakout && d.breakout.closed_at) {
      const flags = d.flags || {};
      const end = flags.retest_received
        ? lastOt
        : Math.min(d.breakout.retest_deadline_ms || lastOt, lastOt);
      box(d.breakout.closed_at, end + DAY_MS, frozen.upper, frozen.mid,
        'alt-retest-box', 'Область ретеста [M, U]');
    }
  }
}

// ---------------------------------------------------------------------------
// «Почему найдено» (§16)
// ---------------------------------------------------------------------------

function rowHtml(label, value) {
  return `<div class="alt-why-row"><span>${esc(label)}</span><b>${value == null ? '—' : value}</b></div>`;
}

function renderWhyButton() {
  $('alt-why-btn').disabled = !state.detail;
}

function renderWhy() {
  const d = state.detail;
  if (!d) return;
  const a = d.asset || {};
  const s = d.source || {};
  const fr = d.frozen_range;
  const cls = d.classifier;
  const parts = [];
  parts.push('<h4>Актив и источник</h4>');
  parts.push(rowHtml('Монета', `${esc(a.symbol || '')} · ${esc(a.name || '')} · CMC #${a.cmc_rank || '—'} (id ${a.cmc_id})`));
  parts.push(rowHtml('Источник', `${esc(s.venue || '—')} · ${esc(s.symbol || '—')} · ${esc(s.quote || '')} · история: ${esc(s.history_scope || '—')} · source v${s.source_version || '—'}`));
  parts.push(rowHtml('as_of', d.as_of_ms ? fmtTime(d.as_of_ms) : '—'));
  const v = d.versions || {};
  parts.push(rowHtml('Версии', `правила ${esc(v.rule || '—')} · классификатор ${esc(v.classifier || '—')} · источник v${v.source || '—'} · диапазон v${v.range || '—'}`));
  if (s.history_scope === 'partial') {
    parts.push('<p class="alt-warn">Подтверждена только частичная история: ATH — «максимум доступного фрагмента», не достоверный ATH всей пары.</p>');
  }

  if (d.ath) {
    parts.push('<h4>ATH и падение (§5)</h4>');
    parts.push(rowHtml('ATH', `${fmtPrice(d.ath.ath_price)} · ${fmtDate(d.ath.ath_open_time)}`));
    parts.push(rowHtml('Минимум после ATH', `${fmtPrice(d.ath.p_min)} · ${fmtDate(d.ath.p_min_open_time)}`));
    parts.push(rowHtml('Историческое падение', d.ath.drawdown != null ? fmtPct(d.ath.drawdown * 100) : '—'));
  }

  if (d.anchors) {
    parts.push('<h4>Опоры диапазона (pivots 3+3, доступны после правых D1)</h4>');
    parts.push(rowHtml('Старт (минимум)', `${fmtDate(d.anchors.start.open_time)} · доступна ${fmtTime(d.anchors.start.available_at_ms)}`));
    parts.push(rowHtml('Отскок (первичный верх)', `${fmtDate(d.anchors.rebound.open_time)} · доступна ${fmtTime(d.anchors.rebound.available_at_ms)}`));
    if (d.anchors.alternative_anchor_open_times && d.anchors.alternative_anchor_open_times.length) {
      parts.push(rowHtml('Альтернативные опоры', d.anchors.alternative_anchor_open_times.map(fmtDate).join(', ')));
    }
  }
  if (fr) {
    parts.push('<h4>Замороженный диапазон (§8)</h4>');
    parts.push(rowHtml('L / U / M / W', `${fmtPrice(fr.lower)} / ${fmtPrice(fr.upper)} / ${fmtPrice(fr.mid)} / ${fmtPrice(fr.width)}`));
    parts.push(rowHtml('Зрелость зафиксирована', `${fmtTime(fr.mature_at_ms)} · свечей в диапазоне: ${fr.included_candles}`));
  }
  if (d.range_versions && d.range_versions.length > 1) {
    parts.push(rowHtml('Версии расширения', d.range_versions.map((x) => `v${x.version} [${fmtPrice(x.lower)}–${fmtPrice(x.upper)}]`).join(' → ')));
  }

  if (cls) {
    const th = cls.thresholds || {};
    parts.push('<h4>Классификатор боковика (§7, проектные пороги)</h4>');
    parts.push(rowHtml('Наклон центра (slope)', `${cls.slope_normalized != null ? cls.slope_normalized.toFixed(4) : '—'} ≤ ${th.slope_max ?? '—'} · направление: ${esc(cls.slope_sign || '—')}`));
    parts.push(rowHtml('Сдвиг центра', `${cls.center_shift != null ? cls.center_shift.toFixed(4) : '—'} ≤ ${th.center_shift_max ?? '—'}`));
    parts.push(rowHtml('Вердикт', cls.ready ? (cls.sideways ? 'боковик' : 'не боковик: ' + esc((cls.failed_conditions || []).join(', '))) : 'недостаточно данных'));
    parts.push(rowHtml('Блок / версия', `${th.block_days ?? '—'} D1 · ${esc(cls.classifier_version || '—')} · ${esc(cls.thresholds_note || '')}`));
  }

  if (d.confirmation) {
    parts.push('<h4>Подтверждение и входы</h4>');
    parts.push(rowHtml('Первое подтверждение', `${esc(d.confirmation.event_type)} · ${fmtTime(d.confirmation.event_time_ms)}`));
    if (d.target_snapshot && d.target_snapshot.bases) {
      parts.push(rowHtml('Основания', esc(d.target_snapshot.bases.join(' + '))));
    }
    for (const e of d.entries || []) {
      parts.push(rowHtml(`Вход ${e.kind}`,
        e.kind === 'A'
          ? `по закрытию ${fmtPrice(e.price)} · ${fmtTime(e.event_time_ms)}`
          : `ретест [${fmtPrice((e.zone || {}).lower)}–${fmtPrice((e.zone || {}).upper)}] · ${fmtTime(e.event_time_ms)}`));
    }
  }
  if (d.targets && d.targets.length) {
    parts.push(rowHtml('Цели', d.targets.map((t) =>
      `TP${t.tp}=${fmtPrice(t.price)}${t.hit ? ' (пройдена' + (t.passed_at_confirmation ? ' к моменту подтверждения' : '') + ')' : ''}`).join('; ')));
  }
  if (d.cancel) {
    parts.push('<h4>Отмена (§12, проектная настройка)</h4>');
    parts.push(rowHtml('K = 2L − U', d.cancel.price != null ? fmtPrice(d.cancel.price) : '—'));
    parts.push(rowHtml('Режим', `${esc(d.cancel.mode_ru)} · ${esc(d.cancel.mode_note)}`));
    if (!d.cancel.reachable) parts.push(`<p class="alt-warn">${esc(d.cancel.nonpositive_text)}</p>`);
  }
  if (d.formulas) {
    parts.push('<h4>Формулы</h4>');
    parts.push(rowHtml('Отмена', esc(d.formulas.cancel)));
    parts.push(rowHtml('Цели', esc(d.formulas.targets)));
    parts.push(rowHtml('Ширина', `${esc(d.formulas.width_up)} / ${esc(d.formulas.width_down)}`));
  }
  if (d.events && d.events.length) {
    parts.push('<h4>События сетапа</h4><ul class="alt-why-events">' +
      d.events.map((e) => `<li><span class="alt-dim">${fmtTime(e.event_time_ms)}</span> ${esc(e.event_type)}</li>`).join('') + '</ul>');
  }
  $('alt-why').innerHTML = parts.join('');
}

// ---------------------------------------------------------------------------
// Deep link ?asset={cmc_id} (из Telegram, §18) и WS-обновление
// ---------------------------------------------------------------------------

async function selectByAsset(cmcId) {
  let row = state.rows.find((r) => r.asset.cmc_id === cmcId);
  if (!row) {
    // актива может не быть в текущем фильтре — ищем в полном списке
    const all = await api('/api/alt/setups?bucket=all');
    row = all.rows.find((r) => r.asset.cmc_id === cmcId);
    if (row) {
      state.bucket = row.terminal ? 'history' : 'eligible';
      $('flt-bucket').value = state.bucket;
      await loadTable();
      row = state.rows.find((r) => r.asset.cmc_id === cmcId) || row;
    }
  }
  if (!row) {
    $('alt-chart-empty').classList.remove('hidden');
    $('alt-chart-empty').textContent = `Актив CMC #${cmcId} не найден в текущем снимке.`;
    return;
  }
  if (row.setup_id != null) await selectRow('setup', row.setup_id);
  else if (row.candidate_id != null) await selectRow('candidate', row.candidate_id);
}

function scheduleReload() {
  clearTimeout(state.reloadTimer);
  state.reloadTimer = setTimeout(async () => {
    try {
      await Promise.all([loadTable(), loadRunStatus()]);
      if (state.selected) await selectRow(state.selected.kind, state.selected.id);
    } catch (e) { console.warn('alt: обновление после прогона', e); }
  }, 400);
}

async function init() {
  await HTF.ensureToken();
  initChart();
  const params = new URL(location.href).searchParams;
  try {
    await Promise.all([loadTable(), loadRunStatus()]);
  } catch (e) {
    $('alt-table-empty').classList.remove('hidden');
    $('alt-table-empty').textContent = 'Ошибка загрузки: ' + e.message;
  }
  const asset = params.get('asset');
  if (asset) await selectByAsset(Number(asset));

  HTF.connectWs((msg) => {
    // §18: после daily job страница обновляется без ручной перезагрузки
    if (msg && msg.type === 'alt') scheduleReload();
  });

  $('flt-bucket').onchange = () => { state.bucket = $('flt-bucket').value; loadTable(); };
  $('flt-apply').onclick = () => loadTable();
  $('flt-reset').onclick = () => {
    for (const id of ['flt-venue', 'flt-rank-min', 'flt-rank-max', 'flt-age-min', 'flt-dd-min', 'flt-structure']) {
      $(id).value = '';
    }
    loadTable();
  };
  $('alt-recalc').onclick = async () => {
    const btn = $('alt-recalc');
    btn.disabled = true;
    try {
      await api('/api/alt/recalc', { method: 'POST', body: '{}' });
      $('alt-running').classList.remove('hidden');
      setTimeout(() => { loadRunStatus(); loadTable(); }, 15_000);
    } catch (e) {
      alert('Пересчёт не запущен: ' + e.message);
    } finally {
      btn.disabled = false;
    }
  };
  $('alt-why-btn').onclick = () => { renderWhy(); HTF.openModal($('alt-why-modal')); };
  $('alt-why-close').onclick = () => HTF.closeModal($('alt-why-modal'));
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init);
} else {
  init();
}
