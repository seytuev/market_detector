/* HTF Zones — фронтенд (vanilla JS + lightweight-charts).
   Токен владельца: prompt() при старте, хранится в localStorage,
   шлётся в каждом запросе (Bearer) и в WebSocket (?token=). */

'use strict';

// ---------------------------------------------------------------------------
// Состояние
// ---------------------------------------------------------------------------

const state = {
  // токен можно передать в URL (?token=...) — сохранится в localStorage
  token: new URLSearchParams(location.search).get('token')
    || localStorage.getItem('htf_token') || '',
  instruments: [],
  instrumentId: null,
  timeframe: 'D1',
  zones: [],
  groups: [],
  candles: [],
  lastPrice: null,
  chart: null,
  candleSeries: null,
  ws: null,
  drawMode: false,
  drawClicks: [],
  pointerDown: false,
  zoneStatusFilter: '',
  zoneTypeFilter: null,
  zoneTfFilter: null,
  zoneRelFilter: '',
  zoneBucket: 'live',
  tableMode: false,
  showAllTf: false,
  showCandidates: false,
  maxChartZones: 80,
  orderedZoneIds: null,
  // ТЗ §5: внутренние уровни выбранной зоны (рисуются, пока зона выбрана)
  innerLevels: [],
  innerLevelsZoneId: null,
};

// Статусы, видимые на графике по умолчанию
const CHART_STATUSES = new Set(['candidate', 'active', 'weakened', 'worked']);

// HTF Zones: в интерфейсе и сканировании только старшие ТФ (H1/H4 убраны
// по решению пользователя; данные H1 в БД сохраняются как история)
const HTF_TFS = new Set(['D1', 'W1']);
const isHtf = (z) => HTF_TFS.has(z.timeframe);

// Локальный fallback: актуальные формулировки приходят с сервера
// (GET /api/labels, источник app/texts_ru.py — тот же, что у Telegram)
let EVENT_KIND_RU = {
  approach: 'приближение на 2%',
  touch: 'первое касание',
  depth_50: 'глубина 50%',
  depth_90: 'глубина 90% (отработан)',
  d1_close_inside: 'закрепление внутри (D1)',
  jump_through: 'проход скачком',
  already_in_zone: 'цена уже в зоне',
  fvg_weakened: 'FVG ослаблен (50%)',
  fvg_filled: 'FVG перекрыт',
  ob_confirmed: 'OB подтверждён',
  breaker_created: 'создан Breaker',
  breaker_archived: 'Breaker архивирован',
  prb_archived: 'PRB архивирован',
  level_taken: 'уровень снят',
  zone_confirmed_by_user: 'подтверждено пользователем',
  data_stale: 'данные устарели',
  data_recovered: 'данные восстановлены',
};

let STATUS_RU = {
  candidate: 'кандидат', active: 'активна', weakened: 'ослаблена',
  worked: 'отработана', converted: 'конвертирована', archived: 'архив',
  taken: 'снята', rejected: 'отклонена',
};

async function loadLabels() {
  try {
    const l = await api('/api/labels');
    if (l.event_kinds) EVENT_KIND_RU = { ...EVENT_KIND_RU, ...l.event_kinds };
    if (l.statuses) STATUS_RU = { ...STATUS_RU, ...l.statuses };
  } catch (e) {
    console.warn('labels: используем локальный fallback', e);
  }
}

// ---------------------------------------------------------------------------
// API
// ---------------------------------------------------------------------------

// Общая реализация — common.js (window.HTF): токен, API, WebSocket,
// форматтеры. Здесь — тонкие делегаты, чтобы весь вызовной код не менять.
async function ensureToken() {
  state.token = await HTF.ensureToken();
}

async function api(path, options = {}) {
  const result = await HTF.api(path, options);
  state.token = HTF.getToken(); // после 401-reprompt токен мог обновиться
  return result;
}

// ---------------------------------------------------------------------------
// Утилиты
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);

function fmtPrice(v) {
  return HTF.fmtPrice(v);
}

function fmtTime(ms) {
  return HTF.fmtTime(ms);
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function instrumentLabel(ins) {
  return `${ins.symbol} · ${ins.venue}`;
}

// ---------------------------------------------------------------------------
// График
// ---------------------------------------------------------------------------

function initChart() {
  const theme = HTF.chartTheme();
  state.chart = LightweightCharts.createChart($('chart'), {
    layout: {
      background: { color: theme.background },
      textColor: theme.text,
    },
    grid: {
      vertLines: { color: theme.grid },
      horzLines: { color: theme.grid },
    },
    timeScale: { timeVisible: true, secondsVisible: false },
    rightPriceScale: { borderColor: theme.border },
    crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
    autoSize: true,
  });
  state.candleSeries = state.chart.addCandlestickSeries({
    upColor: theme.up, downColor: theme.down,
    wickUpColor: theme.up, wickDownColor: theme.down,
    borderVisible: false,
  });
  // перерисовка зон при прокрутке/масштабировании по времени
  state.chart.timeScale().subscribeVisibleLogicalRangeChange(() => drawZones());
  // Перетаскивание шкалы цен меняет вертикальный масштаб БЕЗ события ТФ —
  // у LC такого события нет; перерисовываем по движению указателя во время
  // любых перетаскиваний (троттлинг через rAF, чтобы не дёргать DOM зон
  // на каждый mousemove)
  let redrawQueued = false;
  const queueZoneRedraw = () => {
    if (redrawQueued) return;
    redrawQueued = true;
    requestAnimationFrame(() => { redrawQueued = false; drawZones(); });
  };
  state.chart.subscribeCrosshairMove(() => {
    if (state.pointerDown) queueZoneRedraw();
  });
  const chartWrap = $('chart-container');
  chartWrap.addEventListener('pointerdown', () => { state.pointerDown = true; });
  window.addEventListener('pointerup', () => {
    if (!state.pointerDown) return;
    state.pointerDown = false;
    queueZoneRedraw(); // финальная перерисовка после отпускания
  });
  // двойной клик по шкале цен сбрасывает масштаб (autoscale)
  chartWrap.addEventListener('dblclick', () => queueZoneRedraw());
  // режим рисования ручной зоны: два клика; первый задаёт цену И время
  // начала (якорь, ТЗ §7), второй — вторую цену
  state.chart.subscribeClick((param) => {
    if (!state.drawMode || !param.point) return;
    const price = state.candleSeries.coordinateToPrice(param.point.y);
    if (price === null) return;
    const timeSec = param.time !== undefined
      ? param.time
      : state.chart.timeScale().coordinateToTime(param.point.x);
    state.drawClicks.push({ price, timeSec: timeSec !== null ? Number(timeSec) : null });
    if (state.drawClicks.length === 2) {
      const [a, b] = state.drawClicks;
      const anchorMs = a.timeSec !== null ? a.timeSec * 1000 : null;
      openManualModal(Math.min(a.price, b.price), Math.max(a.price, b.price), anchorMs);
      exitDrawMode();
    }
  });
  window.addEventListener('resize', drawZones);
  // панель деталей сужает контейнер графика — пересчёт координат зон
  new ResizeObserver(() => drawZones()).observe($('chart-container'));
}

async function loadCandles() {
  if (!state.instrumentId) return;
  state.candles = await api(
    `/api/candles?instrument_id=${state.instrumentId}&timeframe=${state.timeframe}&limit=500`);
  state.candleSeries.setData(state.candles);
  if (state.candles.length) {
    state.lastPrice = state.candles[state.candles.length - 1].close;
    updateLastPrice();
  }
  state.chart.timeScale().fitContent();
  // пересчёт координат зон после полного макета графика
  requestAnimationFrame(drawZones);
}

// ---------------------------------------------------------------------------
// Зоны: загрузка и отрисовка поверх графика
// ---------------------------------------------------------------------------

async function loadZones() {
  if (!state.instrumentId) return;
  const [zones, grouped] = await Promise.all([
    api(`/api/zones?instrument_id=${state.instrumentId}`),
    api(`/api/zones/grouped?instrument_id=${state.instrumentId}`),
  ]);
  state.zones = zones;
  state.groups = grouped.groups.filter((g) => g.zones.length > 1);
  drawZones();
  renderZonesTable();
}

function drawZones() {
  const overlay = $('zone-overlay');
  overlay.innerHTML = '';
  if (!state.candleSeries || !state.candles.length) return;
  const chartEl = $('chart');
  const width = chartEl.clientWidth;
  const height = chartEl.clientHeight;
  // правый край зон — граница области графика, без налезания на шкалу цен
  const paneRight = width - state.chart.priceScale('right').width();
  const ts = state.chart.timeScale();
  // по умолчанию на графике только зоны выбранного ТФ; «Все ТФ» показывает
  // оба, а чекбоксы слоёв позволяют скрыть конкретный ТФ в этом режиме
  const tfOn = (tf) => {
    if (!state.showAllTf) return tf === state.timeframe;
    if (tf === 'D1') return !$('layer-tf-d1') || $('layer-tf-d1').checked;
    if (tf === 'W1') return !$('layer-tf-w1') || $('layer-tf-w1').checked;
    return true;
  };
  const tfMatch = (z) => isHtf(z) && tfOn(z.timeframe);

  // Полоса по ценовому диапазону с обрезкой по видимой области графика.
  // Координаты за пределами панели обрезаем — иначе зоны вне видимого
  // ценового диапазона «сваливаются» на край контейнера.
  const makeBand = (upper, lower, cls) => {
    const y1 = state.candleSeries.priceToCoordinate(upper);
    const y2 = state.candleSeries.priceToCoordinate(lower);
    if (y1 === null || y2 === null) return null;
    let top = Math.min(y1, y2);
    let bottom = Math.max(y1, y2);
    if (bottom < 0 || top > height) return null; // целиком вне видимых цен
    top = Math.max(0, top);
    bottom = Math.min(height, bottom);
    const div = document.createElement('div');
    div.className = cls;
    div.style.top = top + 'px';
    div.style.height = Math.max(2, bottom - top) + 'px';
    return div;
  };

  // Визуальное объединение групп на графике отключено по решению пользователя:
  // объединённая подсветка охватывала почти весь график и была бессмысленна.
  // Состав групп доступен через API /api/zones/grouped; исходные зоны,
  // границы и правила уведомлений не меняются (§10).
  const groupedIds = new Set();

  // пул зон для отрисовки: статус + ТФ + переключатель кандидатов;
  // при переполнении — ближайшие к текущей цене (выбранная зона — всегда).
  // Если зона выбрана (клик) — рисуем ТОЛЬКО её.
  let pool;
  if (state.selectedZoneId) {
    const sel = state.zones.find((z) => z.id === state.selectedZoneId);
    pool = sel && tfMatch(sel) ? [sel] : [];
  } else {
    pool = state.zones.filter((z) =>
      CHART_STATUSES.has(z.status) && tfMatch(z) &&
      (state.showCandidates || z.status !== 'candidate'));
    if (pool.length > state.maxChartZones) {
      const price = state.lastPrice || 0;
      pool.sort((a, b) => distanceToZone(a, price) - distanceToZone(b, price));
      pool = pool.slice(0, state.maxChartZones);
    }
  }

  for (const z of pool) {
    // §15.1.7: рисунок начинается от display_from (для FVG — средняя свеча
    // исходной тройки); formed_at — запасной вариант
    const fromMs = z.display_from || z.formed_at;
    let x1 = ts.timeToCoordinate(Math.floor(fromMs / 1000));
    // ТЗ §7: ручная зона начинается строго от якоря пользователя. Якорь левее
    // видимой области даёт отрицательную координату — рисуем от неё (overlay
    // обрезается контейнером), а не продлеваем зону влево клампингом в x1=0
    if (z.source === 'manual') {
      if (x1 === null) continue; // времени якоря нет в загруженных данных
    } else if (x1 === null || x1 < 0) {
      x1 = 0; // зона старше/левее видимой области
    }
    if (x1 > paneRight) continue; // формирование правее видимой области
    // §15.1.3: завершённая зона заканчивается в display_until и НЕ
    // продлевается вправо до текущей цены
    const completed = !!z.display_until;
    let x2 = paneRight;
    if (completed) {
      const xc = ts.timeToCoordinate(Math.floor(z.display_until / 1000));
      if (xc !== null) x2 = Math.min(paneRight, xc);
    }
    const div = makeBand(z.upper, z.lower,
      `zone-rect z-${z.type} status-${z.status}` +
      (completed ? ' status-completed' : '') +
      (state.selectedZoneId === z.id ? ' selected' : ''));
    if (!div) continue;
    div.dataset.zoneId = z.id;
    if (x2 <= x1) continue; // завершилась левее видимой области
    div.style.left = x1 + 'px';
    div.style.width = Math.max(8, x2 - x1) + 'px';
    div.onclick = () => openZoneDetail(z.id);

    if (!z.is_level) {
      const midY = state.candleSeries.priceToCoordinate(z.mid);
      if (midY !== null && midY >= 0 && midY <= height) {
        const mid = document.createElement('div');
        mid.className = 'zone-mid';
        mid.style.top = (midY - parseFloat(div.style.top)) + 'px';
        div.appendChild(mid);
      }
    }

    const label = document.createElement('span');
    label.className = 'zone-label';
    label.textContent =
      `${z.type.toUpperCase()} ${z.timeframe} · ${STATUS_RU[z.status] || z.status}` +
      (z.name ? ` · ${z.name}` : '') + (groupedIds.has(z.id) ? ' ⧉' : '');
    // подписи только у некандидатов — иначе подписи кандидатов превращают
    // график в кашу; текст кандидата доступен в tooltip (div.title)
    if (z.status !== 'candidate') div.appendChild(label);
    div.title = label.textContent;
    overlay.appendChild(div);
  }

  // ТЗ §5: внутренние уровни ликвидности выбранной зоны — тонкие линии
  // на цене уровня от свечи-pivot вправо (taken — до момента снятия).
  // Кандидат (не подтверждён) — пунктир, active — сплошная, taken — серая.
  if (state.selectedZoneId && state.innerLevelsZoneId === state.selectedZoneId) {
    for (const lv of state.innerLevels) {
      const y = state.candleSeries.priceToCoordinate(lv.price);
      if (y === null || y < 0 || y > height) continue;
      let lx1 = ts.timeToCoordinate(Math.floor(lv.pivot_time / 1000));
      if (lx1 === null || lx1 < 0) lx1 = 0;
      let lx2 = paneRight;
      if (lv.taken_at) {
        const xt = ts.timeToCoordinate(Math.floor(lv.taken_at / 1000));
        if (xt !== null) lx2 = Math.min(paneRight, xt);
      }
      if (lx2 <= lx1) continue;
      const div = document.createElement('div');
      const st = lv.status === 'candidate' ? 'candidate'
        : lv.status === 'taken' ? 'taken' : 'active';
      div.className = `inner-level il-${lv.kind} il-${st}`;
      div.style.top = y + 'px';
      div.style.left = lx1 + 'px';
      div.style.width = Math.max(4, lx2 - lx1) + 'px';
      div.title =
        `${lv.kind.toUpperCase()} · ${fmtPrice(lv.price)} · pivot ${fmtTime(lv.pivot_time)}` +
        ` · ${lv.confirmed_at ? 'подтверждён ' + fmtTime(lv.confirmed_at) : 'кандидат'}` +
        (lv.taken_at ? ` · снят ${fmtTime(lv.taken_at)}` : '');
      overlay.appendChild(div);
    }
  }
}

// ---------------------------------------------------------------------------
// Таблица зон (ближайшие к цене сверху; «цена внутри» — подсветка)
// ---------------------------------------------------------------------------

function distanceToZone(z, price) {
  if (price === null) return Infinity;
  if (price >= z.lower && price <= z.upper) return 0;
  return Math.min(Math.abs(price - z.lower), Math.abs(price - z.upper)) / price;
}

function priceRelation(z) {
  const p = state.lastPrice;
  if (p == null) return '';
  if (p >= z.lower && p <= z.upper) return 'inside';
  if (p < z.lower) return 'above';
  return 'below';
}

function relationLabel(rel) {
  return { inside: 'Цена внутри', above: 'Выше цены', below: 'Ниже цены' }[rel] || '';
}

function zoneRole(z) {
  if (z.type === 'ob') return z.direction === 'bear' ? 'Сопротивление' : 'Поддержка';
  if (z.type === 'fvg') return 'Дисбаланс';
  if (z.type === 'ssl' || z.type === 'bsl') return 'Ликвидность';
  if (z.type === 'manual') return 'Ручная';
  return z.type.toUpperCase();
}

function filteredZones() {
  let zones = state.zones.filter(isHtf);
  const bucket = state.zoneBucket || 'live';
  if (bucket === 'live') {
    zones = zones.filter((z) => !z.display_until && z.status !== 'candidate' && z.status !== 'rejected');
  } else if (bucket === 'candidate') {
    zones = zones.filter((z) => z.status === 'candidate' && !z.display_until);
  } else if (bucket === 'archive') {
    zones = zones.filter((z) => z.display_until || ['rejected', 'archived', 'taken', 'converted'].includes(z.status));
  }
  const f = state.zoneStatusFilter;
  if (f && f !== '' && f !== 'all' && bucket === 'live') {
    zones = zones.filter((z) => z.status === f);
  }
  if (state.zoneTypeFilter) zones = zones.filter((z) => z.type === state.zoneTypeFilter);
  if (state.zoneTfFilter) zones = zones.filter((z) => z.timeframe === state.zoneTfFilter);
  if (state.zoneRelFilter) zones = zones.filter((z) => priceRelation(z) === state.zoneRelFilter);
  const rank = (z) => {
    const rel = priceRelation(z);
    const dist = distanceToZone(z, state.lastPrice);
    return [rel === 'inside' ? 0 : 1, dist, -(z.confirmed_at || 0), z.id];
  };
  if (state.orderedZoneIds && state.orderedZoneIds.length) {
    const pos = new Map(state.orderedZoneIds.map((id, i) => [id, i]));
    zones.sort((a, b) => (pos.get(a.id) ?? 1e9) - (pos.get(b.id) ?? 1e9));
  } else {
    zones.sort((a, b) => {
      const ra = rank(a), rb = rank(b);
      for (let i = 0; i < ra.length; i++) if (ra[i] !== rb[i]) return ra[i] - rb[i];
      return 0;
    });
  }
  return zones;
}

function renderZonesTable() {
  const tbody = $('zones-table') && $('zones-table').querySelector('tbody');
  const list = $('zone-list');
  const zones = filteredZones();
  if (list) {
    list.innerHTML = '';
    if (!zones.length) {
      list.innerHTML = '<div class="ltf-empty">По выбранным условиям актуальных зон нет.</div>';
    }
    for (const z of zones) {
      const rel = priceRelation(z);
      const range = z.is_level ? fmtPrice(z.lower) : `${fmtPrice(z.lower)} — ${fmtPrice(z.upper)}`;
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'zone-card' + (z.id === state.selectedZoneId ? ' selected' : '');
      card.innerHTML =
        `<span class="swatch z-${esc(z.type)}"></span>` +
        `<div><div class="title">${z.type.toUpperCase()} · ${z.timeframe} <span class="dir ${z.direction}">${z.direction === 'bear' ? '↓' : '↑'}</span></div>` +
        `<div class="role">${esc(zoneRole(z))}</div>` +
        `<div class="range">${range}</div></div>` +
        `<span class="rel-chip ${rel}">${esc(relationLabel(rel) || (STATUS_RU[z.status] || z.status))}</span>`;
      card.onclick = () => openZoneDetail(z.id);
      list.appendChild(card);
    }
  }
  if (!tbody) {
    renderHeaderStats();
    if ($('zones-count')) $('zones-count').textContent = zones.length;
    return;
  }
  tbody.innerHTML = '';

  for (const z of zones) {
    const tr = document.createElement('tr');
    const inside = state.lastPrice !== null &&
      state.lastPrice >= z.lower && state.lastPrice <= z.upper;
    if (inside) tr.className = 'inside';
    if (z.id === state.selectedZoneId) tr.classList.add('selected');
    tr.tabIndex = 0;
    const range = z.is_level ? fmtPrice(z.lower) : `${fmtPrice(z.lower)}–${fmtPrice(z.upper)}`;
    tr.innerHTML =
      `<td data-label="Тип">${z.type.toUpperCase()}${z.direction === 'bear' ? ' ▼' : ' ▲'}</td>` +
      `<td data-label="ТФ">${z.timeframe}</td>` +
      `<td data-label="Диапазон">${range}</td><td data-label="Середина">${fmtPrice(z.mid)}</td>` +
      `<td data-label="Статус"><span class="badge ${z.status}">${STATUS_RU[z.status] || z.status}</span>` +
      (z.display_until ? ' <span class="badge completed" title="' +
        esc(z.end_reason || 'завершена') + '">завершена</span>' : '') +
      `</td>`;
    tr.title = z.name || z.comment || '';
    tr.onclick = () => openZoneDetail(z.id);
    tr.onkeydown = (event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); tr.click(); } };
    tbody.appendChild(tr);
  }
  if (!zones.length) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td colspan="5" class="ltf-empty">По выбранным фильтрам зон нет.</td>';
    tbody.appendChild(tr);
  }
  renderHeaderStats();
  $('zones-count').textContent = zones.length;
}

// Небольшая статистика в шапке по выбранному инструменту
function renderHeaderStats() {
  const el = $('header-stats');
  if (!el || !state.instrumentId) return;
  const zones = state.zones.filter(isHtf); // статистика по HTF-зонам
  const live = zones.filter((z) => !z.display_until);
  const by = (pred) => zones.filter(pred).length;
  const inside = zones.filter((z) =>
    state.lastPrice !== null && !z.display_until &&
    state.lastPrice >= z.lower && state.lastPrice <= z.upper).length;
  const ssl = by((z) => z.type === 'ssl' && z.status === 'active');
  const bsl = by((z) => z.type === 'bsl' && z.status === 'active');
  const items = [
    ['Активные', by((z) => z.status === 'active' && !z.display_until)],
    ['Кандидаты', by((z) => z.status === 'candidate' && !z.display_until)],
    ['Цена внутри', inside],
    ['SSL/BSL активные', `${ssl}/${bsl}`],
    ['Завершённые', zones.length - live.length],
    ['Всего', zones.length],
  ];
  el.innerHTML = items.map(([k, v]) =>
    `<span class="hs-item">${k}: <b>${v}</b></span>`).join('');
}

function updateLastPrice() {
  if ($('last-price')) $('last-price').textContent = fmtPrice(state.lastPrice);
  const ins = state.instruments.find((i) => i.id === state.instrumentId);
  if ($('instrument-meta') && ins) {
    $('instrument-meta').textContent = `${ins.venue} · ${ins.market_type || 'spot'}`;
  }
  document.querySelectorAll('.zone-card .rel-chip').forEach((chip, idx) => {
    /* подписи положения обновляются при полном рендере списка */
  });
}

// ---------------------------------------------------------------------------
// Панель деталей зоны
// ---------------------------------------------------------------------------

const TF_SECONDS = { H1: 3600, H4: 14400, D1: 86400, W1: 604800 };

// Прокручивает график к зоне: при необходимости переключает ТФ и ставит
// видимый диапазон от начала формирования зоны до текущего момента.
async function focusZoneOnChart(z) {
  const tfOptionExists = [...$('tf-select').options].some((o) => o.value === z.timeframe);
  if (z.timeframe !== state.timeframe && TF_SECONDS[z.timeframe] && tfOptionExists) {
    state.timeframe = z.timeframe;
    $('tf-select').value = z.timeframe;
    await loadCandles();
  }
  const tfSec = TF_SECONDS[state.timeframe] || 86400;
  const nowSec = Math.floor(Date.now() / 1000);
  const fromSec = Math.floor(z.formed_at / 1000);
  const firstBar = state.candles.length ? state.candles[0].time : fromSec;
  let to = nowSec + 3 * tfSec;
  let from = Math.max(firstBar, fromSec - 15 * tfSec);
  if (from >= to) from = Math.max(firstBar, to - 95 * tfSec); // зона старше загруженных свечей
  // Два кадра: панель деталей меняет ширину графика, LC при ресайзе держит
  // barSpacing и сдвигает диапазон — ставим диапазон после перестройки
  requestAnimationFrame(() => requestAnimationFrame(() => {
    state.chart.timeScale().setVisibleRange({ from, to });
    drawZones();
  }));
}

// Сброс выбора зоны: график снова показывает все зоны
function clearZoneSelection() {
  if (!state.selectedZoneId) return;
  state.selectedZoneId = null;
  state.innerLevels = [];
  state.innerLevelsZoneId = null;
  drawZones();
}

// Решение по зоне из панели деталей (§15.3, R13): раздельная оценка
// геометрии и актуальности. fix_boundaries требует границы и (необязательно)
// свечу-якорь; остальные решения отправляют decision + текст из textarea.
async function reviewZoneAction(zoneId, decision, lower, upper) {
  const textEl = $('review-comment');
  const text = textEl ? textEl.value.trim() : '';
  const body = { decision, text };
  if (decision === 'fix_boundaries') {
    const bounds = await askBounds(lower, upper, true);
    if (!bounds) return;
    Object.assign(body, bounds);
  }
  // ревью перед оценкой воспроизводит историю инструмента (R02) — это может
  // занять десятки секунд; блокируем кнопки и честно показываем ожидание
  const btns = document.querySelectorAll('#detail-content [data-review]');
  const vEl = $('review-verdict');
  btns.forEach((b) => { b.disabled = true; });
  if (vEl) vEl.textContent = 'Сохраняю решение… первая проверка зоны может занять до минуты';
  let res;
  try {
    res = await api(`/api/zones/${zoneId}/review`, {
      method: 'POST', body: JSON.stringify(body),
    });
  } catch (err) {
    btns.forEach((b) => { b.disabled = false; });
    if (vEl) vEl.textContent = 'Ошибка: ' + err.message;
    return;
  }
  await Promise.all([loadZones(), loadCandidates()]);
  await openZoneDetail(zoneId);
  // показать вердикты последней оценки (геометрия / жизненный цикл)
  const a = res.assessment;
  const vEl2 = $('review-verdict');
  if (a && vEl2) {
    vEl2.textContent = `Геометрия: ${a.geometry_verdict}` +
      (a.lifecycle_verdict ? ` · цикл: ${a.lifecycle_verdict}` : '') +
      (a.requires_clarification ? ' · требует уточнения' : '');
  }
}
window.reviewZoneAction = reviewZoneAction;

let boundsResolve = null;
function askBounds(lower, upper, anchorEnabled) {
  $('bounds-title').textContent = anchorEnabled ? 'Исправить границы зоны' : 'Исправить кандидата';
  $('bounds-lower').value = lower;
  $('bounds-upper').value = upper;
  $('bounds-anchor').value = '';
  $('bounds-anchor-wrap').classList.toggle('hidden', !anchorEnabled);
  $('bounds-error').textContent = '';
  HTF.openModal($('bounds-modal'));
  return new Promise((resolve) => { boundsResolve = resolve; });
}
function closeBounds(value = null) {
  HTF.closeModal($('bounds-modal'));
  if (boundsResolve) { boundsResolve(value); boundsResolve = null; }
}
function saveBounds() {
  const lower = Number($('bounds-lower').value);
  const upper = Number($('bounds-upper').value);
  if (!$('bounds-lower').value || !$('bounds-upper').value || !Number.isFinite(lower) || !Number.isFinite(upper) || lower > upper) {
    $('bounds-error').textContent = 'Проверьте границы: нижняя должна быть не выше верхней.';
    return;
  }
  const value = { lower, upper };
  if (!$('bounds-anchor-wrap').classList.contains('hidden') && $('bounds-anchor').value) {
    const anchor = Number($('bounds-anchor').value);
    if (!Number.isSafeInteger(anchor) || anchor < 0) {
      $('bounds-error').textContent = 'Время свечи-якоря должно быть целым числом миллисекунд.';
      return;
    }
    value.anchor_candle_open_time = anchor;
  }
  closeBounds(value);
}

const REVIEW_DECISION_RU = {
  correct: 'размечено верно',
  now_irrelevant: 'сейчас неактуально',
  fix_boundaries: 'исправлены границы',
  wrong_base: 'другое основание',
  already_breaker: 'уже Breaker',
  no_context: 'нет контекста',
  wrong_type: 'неверный тип/форма',
  confirmed: 'подтверждена',
  corrected: 'исправлены границы',
  rejected: 'отклонена',
  wrong: 'отклонена',
};

function openZoneDetail(zoneId) {
  // инспектор живёт во вкладке «Обзор» — переключаемся на неё, иначе клик
  // из «Проверки» или «Событий» открывал бы детали в скрытой вкладке
  showView('overview');
  return loadZoneDetail(zoneId, true);
}

async function loadZoneDetail(zoneId, focusChart) {
  const commentEl = $('review-comment');
  const commentKeep = commentEl ? commentEl.value : '';
  const commentFocused = document.activeElement === commentEl;
  const openDetails = [...document.querySelectorAll('#detail-content details[open]')]
    .map((el) => el.dataset.section);
  const d = await api(`/api/zones/${zoneId}`);
  const z = d.zone;
  state.selectedZoneId = zoneId;
  // ТЗ §5: внутренние уровни ликвидности — рисуются на графике, пока
  // открыта эта зона; обновляются при каждой перерисовке деталей
  state.innerLevels = d.inner_levels || [];
  state.innerLevelsZoneId = zoneId;
  const el = $('detail-content');
  const rel = d.relation || {};
  const relRows = [];
  if (rel.parent_ob) relRows.push(`<li>Исходный OB ${rel.parent_ob.timeframe} · ${fmtPrice(rel.parent_ob.lower)}–${fmtPrice(rel.parent_ob.upper)}</li>`);
  if (rel.confirming_fvg) relRows.push(`<li>Подтверждающий FVG [${fmtPrice(rel.confirming_fvg.lower)}–${fmtPrice(rel.confirming_fvg.upper)}]</li>`);
  if (rel.predecessor_ob) relRows.push(`<li>Предшественник OB ${rel.predecessor_ob.timeframe}</li>`);
  const range = z.is_level ? fmtPrice(z.lower) : `${fmtPrice(z.lower)} – ${fmtPrice(z.upper)}`;

  el.innerHTML = `
    <h3>${z.type.toUpperCase()} ${z.timeframe} ${z.name ? '· ' + esc(z.name) : ''} <span class="badge ${z.status}">${STATUS_RU[z.status] || z.status}</span>${z.display_until ? ' <span class="badge completed">завершена</span>' : ''}</h3>
    <p class="inspector-actions">
      <button type="button" class="btn primary" id="btn-focus-zone">На графике</button>
      ${(z.type === 'ob' && HTF_TFS.has(z.timeframe))
        ? `<a class="btn small" href="/ltf.html?zone_id=${z.id}&token=${encodeURIComponent(state.token)}">Открыть LTF →</a>`
        : ''}
    </p>
    <dl>
      <dt>Диапазон</dt><dd>${range}</dd>
      <dt>Середина</dt><dd>${fmtPrice(z.mid)}</dd>
      <dt>Направление</dt><dd class="dir ${z.direction}">${z.direction === 'bull' ? 'Рост' : 'Снижение'}</dd>
      <dt>Статус</dt><dd>${STATUS_RU[z.status] || z.status}${z.display_until ? ' · завершена' : ''}</dd>
      <dt>Основание зоны</dt><dd>${fmtTime(z.formed_at)}</dd>
      <dt>Подтверждение зоны</dt><dd>${fmtTime(z.confirmed_at)}</dd>
    </dl>
    <div class="review-block">
      <h4>Проверка зоны</h4>
      <textarea id="review-comment" rows="2"
        placeholder="Комментарий к решению"></textarea>
      <div class="review-actions">
        <button class="btn ok" type="button" data-review="correct">Размечено верно</button>
        <button class="btn primary" type="button" data-review="fix_boundaries">Исправить границы</button>
        <details data-section="other-reviews"><summary class="btn">Другие решения</summary><div class="review-actions">
        <button class="btn" type="button" data-review="now_irrelevant">Сейчас неактуально</button>
        <button class="btn" type="button" data-review="wrong_base">Другое основание</button>
        <button class="btn" type="button" data-review="already_breaker">Уже Breaker</button>
        <button class="btn" type="button" data-review="no_context">Нет контекста</button>
        <button class="btn danger" type="button" data-review="wrong_type">Неверный тип/форма</button>
        </div></details>
      </div>
      <div id="review-verdict" class="review-verdict"></div>
    </div>
    <details data-section="history"><summary>История</summary>
    <h4>События (${d.events.length})</h4>
    <ul>${d.events.map((e) => `<li><span class="ev-time">${fmtTime(e.occurred_at)}</span>${EVENT_KIND_RU[e.kind] || e.kind} @ ${fmtPrice(e.price)}${e.delayed ? ' (восстановлено)' : ''}</li>`).join('') || '<li>нет</li>'}</ul>
    <h4>Визиты (${d.visits.length})</h4>
    <ul>${d.visits.map((v) => `<li>${fmtTime(v.entered_at)} → ${v.exited_at ? fmtTime(v.exited_at) : 'внутри'} · глубина ${(v.max_depth * 100).toFixed(0)}%</li>`).join('') || '<li>нет</li>'}</ul>
    <h4>Проверки (${d.reviews.length})</h4>
    <ul>${d.reviews.map((r) => `<li><span class="ev-time">${fmtTime(r.created_at)}</span>${REVIEW_DECISION_RU[r.decision] || r.decision}${r.text ? ': ' + esc(r.text) : ''}</li>`).join('') || '<li>нет</li>'}</ul>
    ${d.boundary_corrections.length ? `<h4>Правки границ</h4><ul>${d.boundary_corrections.map((c) => `<li>${fmtPrice(c.original_lower)}–${fmtPrice(c.original_upper)} → ${fmtPrice(c.corrected_lower)}–${fmtPrice(c.corrected_upper)}${c.anchor_candle_open_time ? ' · якорь ' + fmtTime(c.anchor_candle_open_time) : ''}${c.reason ? ' · ' + esc(c.reason) : ''}</li>`).join('')}</ul>` : ''}
    </details>
    ${relRows.length ? `<details data-section="links"><summary>Связи</summary><ul>${relRows.join('')}</ul></details>` : ''}
    ${z.source_candles.length ? `<details data-section="candles"><summary>Исходные свечи</summary><ul>${z.source_candles.map((t) => `<li>${fmtTime(t)}</li>`).join('')}</ul></details>` : ''}
    <details data-section="diag"><summary>Диагностика</summary><dl>
      <dt>Источник</dt><dd>${z.source === 'manual' ? 'ручная' : 'автоматическая'}${d.instrument ? ' · ' + esc(instrumentLabel(d.instrument)) : ''}</dd>
      <dt>Рисунок от</dt><dd>${fmtTime(z.display_from)}</dd>
      <dt>Рисунок до</dt><dd>${z.display_until ? fmtTime(z.display_until) : 'живая'}</dd>
      ${z.end_reason ? `<dt>Причина завершения</dt><dd>${esc(z.end_reason)}</dd>` : ''}
      ${z.breaker_pending ? `<dt>Breaker</dt><dd>ожидает новый FVG пробоя</dd>` : ''}
      ${z.breaker_forbidden ? `<dt>Breaker</dt><dd>запрещён: был тест больше 50%</dd>` : ''}
      <dt>Возраст</dt><dd>${z.age_days} дн.</dd>
      <dt>Версия границ</dt><dd>${z.boundary_version}</dd>
    </dl>
    ${z.comment ? `<p>${esc(z.comment)}</p>` : ''}
    ${d.assessments.length ? `<h4>Оценки</h4><ul>${d.assessments.map((a) => `<li><span class="ev-time">${fmtTime(a.reviewed_at)}</span>${REVIEW_DECISION_RU[a.review_decision] || a.review_decision}: геометрия ${a.geometry_verdict}${a.lifecycle_verdict ? ', цикл ' + a.lifecycle_verdict : ''}${a.requires_clarification ? ', требует уточнения' : ''}</li>`).join('')}</ul>` : ''}
    </details>
  `;
  const focusBtn = $('btn-focus-zone');
  if (focusBtn) focusBtn.onclick = () => focusZoneOnChart(z);
  el.querySelectorAll('[data-review]').forEach((btn) => {
    btn.onclick = () => reviewZoneAction(z.id, btn.dataset.review, z.lower, z.upper);
  });
  if (commentKeep) $('review-comment').value = commentKeep;
  if (commentFocused) $('review-comment').focus();
  for (const name of openDetails) {
    const node = el.querySelector(`details[data-section="${name}"]`);
    if (node) node.open = true;
  }
  showInspector(true);
  renderZonesTable();
  drawZones(); // внутренние уровни выбранной зоны (ТЗ §5)
  if (focusChart) focusZoneOnChart(z);
}

function showGroupMembers(group) {
  const el = $('detail-content');
  el.innerHTML = `
    <h3>Визуальная группа (${group.zones.length} зон)</h3>
    <p>Объединение только для отображения: исходные границы, середины
    и правила уведомлений зон не меняются.</p>
    <dl><dt>Охват</dt><dd>${fmtPrice(group.lower)} – ${fmtPrice(group.upper)}</dd></dl>
    ${group.zones.map((z) => `
      <div class="candidate-card">
        <div class="cand-title">${z.type.toUpperCase()} ${z.timeframe} · ${STATUS_RU[z.status] || z.status}</div>
        <div>${fmtPrice(z.lower)} – ${fmtPrice(z.upper)}, M=${fmtPrice(z.mid)}</div>
        <button class="btn small" onclick="openZoneDetail(${z.id})">Открыть</button>
      </div>`).join('')}
  `;
  showInspector(true);
}

// ---------------------------------------------------------------------------
// События
// ---------------------------------------------------------------------------

async function loadEvents() {
  const events = await api('/api/events?limit=50');
  renderEvents(events);
}

function renderEvents(events) {
  const ul = $('events-list');
  if (ul) {
    ul.innerHTML = '';
    events.slice(0, 3).forEach((e) => ul.appendChild(eventLi(e)));
  }
  const full = $('events-full');
  if (full) {
    full.innerHTML = '';
    events.forEach((e) => full.appendChild(eventLi(e)));
  }
  if ($('events-count')) $('events-count').textContent = events.length;
}

function showInspector(open) {
  const detail = $('zone-detail');
  if (!detail) return;
  detail.classList.toggle('hidden', !open);
  $('zone-list')?.classList.toggle('hidden', open);
  const tableWrap = $('zone-table-wrap');
  if (tableWrap) tableWrap.classList.toggle('hidden', open || !state.tableMode);
}

function eventLi(e) {
  const li = document.createElement('li');
  const sym = e.instrument ? e.instrument.symbol : '';
  const ztype = e.zone ? e.zone.type.toUpperCase() : '';
  li.innerHTML = `<span class="ev-time">${fmtTime(e.occurred_at)}</span>` +
    `<b>${esc(sym)}</b> ${ztype}: ${EVENT_KIND_RU[e.kind] || e.kind} @ ${fmtPrice(e.price)}` +
    (e.delayed ? ' <span class="badge">восстановлено</span>' : '');
  if (e.zone) li.onclick = () => openZoneDetail(e.zone.id);
  li.style.cursor = 'pointer';
  return li;
}

// ---------------------------------------------------------------------------
// Кандидаты (§10): Подтвердить / Исправить / Отклонить
// ---------------------------------------------------------------------------

function explanationText(ev) {
  if (!ev || typeof ev !== 'object') return '';
  if (ev.reason) return String(ev.reason);
  const parts = [];
  for (const [key, value] of Object.entries(ev)) {
    if (value == null || typeof value === 'object') continue;
    parts.push(`${key}: ${value}`);
  }
  return parts.join(' · ');
}

async function loadCandidates() {
  const drafts = {};
  const activeCard = document.activeElement && document.activeElement.closest
    ? document.activeElement.closest('.candidate-card') : null;
  const activeId = activeCard && activeCard.dataset.zoneId;
  document.querySelectorAll('#candidates-list .candidate-card').forEach((card) => {
    const ta = card.querySelector('textarea');
    if (ta && ta.value) drafts[card.dataset.zoneId] = ta.value;
  });
  const list = (await api('/api/candidates')).filter(isHtf); // только HTF
  $('candidates-count').textContent = list.length;
  const box = $('candidates-list');
  box.innerHTML = '';
  if (!list.length) {
    box.innerHTML = '<div class="ltf-empty">Кандидатов на проверку нет.</div>';
    return;
  }
  for (const c of list) {
    const card = document.createElement('div');
    card.className = 'candidate-card';
    card.dataset.zoneId = String(c.id);
    const why = explanationText(c.explanation);
    card.innerHTML = `
      <div class="cand-title">${c.instrument ? esc(c.instrument.symbol) : ''} · ${c.type.toUpperCase()} ${c.timeframe} ${c.direction === 'bull' ? '▲ Рост' : '▼ Снижение'}</div>
      <div>${fmtPrice(c.lower)} – ${fmtPrice(c.upper)}</div>
      ${why ? `<p class="cand-explain">${esc(why)}</p>` : ''}
      <textarea rows="1" placeholder="Комментарий (необязательно)"></textarea>
      <div class="cand-actions">
        <button class="btn small primary" data-act="confirmed">Подтвердить</button>
        <button class="btn small" data-act="corrected">Исправить</button>
        <button class="btn small danger" data-act="rejected">Отклонить</button>
      </div>`;
    const ta = card.querySelector('textarea');
    if (drafts[c.id]) ta.value = drafts[c.id];
    card.querySelectorAll('button').forEach((btn) => {
      btn.onclick = () => reviewCandidate(c, btn.dataset.act, ta.value);
    });
    card.onclick = (ev) => {
      if (ev.target.closest('button, textarea')) return;
      openZoneDetail(c.id);
    };
    box.appendChild(card);
    if (activeId && String(c.id) === activeId) ta.focus();
  }
}

async function reviewCandidate(c, decision, text) {
  const body = { decision, text };
  if (decision === 'corrected') {
    const bounds = await askBounds(c.lower, c.upper, false);
    if (!bounds) return;
    Object.assign(body, bounds);
  }
  // та же длительная переоценка, что и в панели деталей — блокируем кнопки
  const card = document.querySelector(`#candidates-list .candidate-card[data-zone-id="${c.id}"]`);
  const btns = card ? card.querySelectorAll('button') : [];
  btns.forEach((b) => { b.disabled = true; });
  try {
    await api(`/api/zones/${c.id}/review`, { method: 'POST', body: JSON.stringify(body) });
  } catch (err) {
    btns.forEach((b) => { b.disabled = false; });
    alert('Ошибка сохранения проверки: ' + err.message);
    return;
  }
  await Promise.all([loadCandidates(), loadZones(), loadEvents()]);
}

// ---------------------------------------------------------------------------
// Ручная зона (§10)
// ---------------------------------------------------------------------------

function enterDrawMode() {
  state.drawMode = true;
  state.drawClicks = [];
  $('draw-hint').classList.remove('hidden');
}

function exitDrawMode() {
  state.drawMode = false;
  state.drawClicks = [];
  $('draw-hint').classList.add('hidden');
}

// datetime-local ← ms (локальное время пользователя)
function toLocalInput(ms) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function openManualModal(lower, upper, anchorMs) {
  $('mz-lower').value = lower !== null && lower !== undefined ? lower : '';
  $('mz-upper').value = upper !== null && upper !== undefined ? upper : '';
  $('mz-level').value = '';
  // якорь: время первого клика при рисовании; из формы — текущее время (ТЗ §7)
  $('mz-anchor').value = toLocalInput(anchorMs || Date.now());
  // типовое правило: по умолчанию OB для диапазонной зоны
  $('mz-ztype').value = 'ob';
  HTF.openModal($('manual-modal'));
}

async function saveManualZone() {
  $('manual-status').textContent = '';
  const level = parseFloat($('mz-level').value);
  const body = {
    instrument_id: state.instrumentId,
    direction: $('mz-direction').value,
    timeframe: $('mz-timeframe').value,
    name: $('mz-name').value,
    comment: $('mz-comment').value,
    zone_type: $('mz-ztype').value || null,
  };
  // начало зоны на графике (необязательное; пустое — сервер возьмёт «сейчас»)
  const anchorRaw = $('mz-anchor').value;
  if (anchorRaw) {
    const anchorMs = new Date(anchorRaw).getTime(); // datetime-local → локальное время
    if (!isNaN(anchorMs)) body.anchor_time = anchorMs;
  }
  if (!isNaN(level)) {
    body.level = level;
    body.zone_type = null; // уровень (L == U) — без типового правила (ТЗ §7)
  } else {
    body.lower = parseFloat($('mz-lower').value);
    body.upper = parseFloat($('mz-upper').value);
    if (isNaN(body.lower) || isNaN(body.upper) || body.lower > body.upper) {
      $('manual-status').textContent = 'Укажите уровень или корректные нижнюю и верхнюю границы.';
      return;
    }
  }
  try { await api('/api/zones/manual', { method: 'POST', body: JSON.stringify(body) }); }
  catch (err) { $('manual-status').textContent = err.message; return; }
  HTF.closeModal($('manual-modal'));
  await loadZones();
}

// ---------------------------------------------------------------------------
// Настройки (DetectorConfig; uncalibrated — «не калибровано, §14»)
// ---------------------------------------------------------------------------

// Подробные русские описания параметров детектора.
// title — понятное название, desc — что делает и откуда в спеке, group — раздел.
const SETTINGS_META = {
  lookback_days: {
    title: 'Глубина первичного поиска (дней, fallback)',
    desc: 'Запасное окно для таймфреймов без собственной глубины (у D1 и W1 — свои ' +
          'параметры ниже). Сохранённые активные зоны не удаляются, даже если дата их ' +
          'формирования вышла за это окно.',
    group: 'Поиск зон',
  },
  lookback_days_d1: {
    title: 'Глубина истории D1 (дней)',
    desc: 'Как далеко в прошлое загружается дневная история при подключении инструмента (§1). ' +
          'По умолчанию 365 — последний год.',
    group: 'Поиск зон',
  },
  lookback_days_w1: {
    title: 'Глубина истории W1 (дней)',
    desc: 'Как далеко в прошлое загружается недельная история при подключении инструмента (§1). ' +
          'По умолчанию 730 — последние 2 года.',
    group: 'Поиск зон',
  },
  scan_timeframes: {
    title: 'Таймфреймы поиска (через запятую)',
    desc: 'HTF Zones: работают только старшие ТФ — D1,W1 (по умолчанию). H1/H4 убраны из ' +
          'интерфейса и сканирования по решению пользователя. Применяется после перезапуска сервиса.',
    group: 'Поиск зон',
  },
  pivot_left: {
    title: 'Пивот: свечей слева',
    desc: 'Для SSL/BSL: экстремум — свеча, чей High/Low строго выше/ниже N свечей слева (§7).',
    group: 'Поиск зон',
  },
  pivot_right: {
    title: 'Пивот: свечей справа',
    desc: 'Экстремум становится активным только после закрытия N-й свечи справа (§7).',
    group: 'Поиск зон',
  },
  approach_pct: {
    title: 'Порог приближения к зоне (доля)',
    desc: '0.02 = 2%. Событие «приближение» создаётся, когда цена подошла к зоне ближе этого ' +
          'расстояния (§9). Внутри зоны расстояние считается нулём.',
    group: 'Касания и глубина',
  },
  depth_mid: {
    title: 'Глубина «ослаблен» (доля)',
    desc: '0.5 = 50% глубины зоны. Для FVG — статус «ослабленный» (§3), для OB/PRB/Breaker — ' +
          'событие достижения середины (§2, §8).',
    group: 'Касания и глубина',
  },
  depth_worked: {
    title: 'Глубина «отработан» (доля)',
    desc: '0.9 = 90% глубины. После этого касания объекта больше не отслеживаются и уведомления ' +
          'прекращаются; у OB продолжает проверяться превращение в Breaker (§6).',
    group: 'Касания и глубина',
  },
  suppress_hours: {
    title: 'Пауза повторных уведомлений (часов)',
    desc: '120 ч = 5 дней (§8). Повтор того же события на той же глубине молчит, пока не пройдёт ' +
          'срок с момента успешной доставки. Само истечение срока события не создаёт.',
    group: 'Уведомления',
  },
  delivery_target_seconds: {
    title: 'Цель доставки уведомления (сек)',
    desc: '120 секунд — цель приёмки при нормальной доступности источника и Telegram (§11). ' +
          'Это цель, а не измеренная гарантия.',
    group: 'Уведомления',
  },
  notify_only_reviewed: {
    title: 'Уведомлять только о подтверждённых',
    desc: 'Если включено — автоматически найденные зоны молчат, пока вы не нажмёте «Подтвердить» ' +
          'в списке кандидатов (§10).',
    group: 'Уведомления',
  },
  uncalibrated_cluster_denominator: {
    title: 'Знаменатель допуска кластера экстремумов',
    desc: 'Формула объединения: (p_max − p_min) / X ≤ допуск. X = p_min — рабочее значение; ' +
          'знаменатель отдельно не утверждён (§14.3).',
    group: 'Не калибровано',
  },
  cluster_tolerance_pct: {
    title: 'Допуск объединения экстремумов (доля)',
    desc: '0.02 = 2% (§7). Близкие SSL/BSL объединяются в одну группу; уровень группы — крайний ' +
          'максимум/минимум. Сама формула допуска — рабочая, см. знаменатель в разделе §14.',
    group: 'Поиск зон',
  },
  uncalibrated_consolidation_max_candles: {
    title: 'Максимум свечей в базе OB',
    desc: 'Ограничение длины консолидации при поиске Orderblock (§14.1). Точный критерий базы ' +
          'ещё не согласован — кандидаты проверяются вручную.',
    group: 'Не калибровано',
  },
  uncalibrated_consolidation_overlap_pct: {
    title: 'Допуск перекрытия свечей базы (доля)',
    desc: 'Рабочее значение 0 — дополнительный послабляющий фильтр перекрытия диапазонов свечей ' +
          'консолидации (§14.1). Не калибровано.',
    group: 'Не калибровано',
  },
  uncalibrated_ob_delay_max_candles: {
    title: 'Предел отложенного FVG (свечей)',
    desc: 'Подтверждающий FVG может появиться спустя несколько свечей после базы — это предел ' +
          'расстояния между ними (§14.2). Условия разрыва связи не согласованы.',
    group: 'Не калибровано',
  },
  uncalibrated_plateau_equal_peaks: {
    title: 'Учитывать плато с равными пиками',
    desc: 'Строгий пивот 3+3 пропускает плато с равными вершинами. Отдельный согласованный пример ' +
          'ещё не задан (§14.3) — включение меняет условие поиска экстремумов.',
    group: 'Не калибровано',
  },
  uncalibrated_approach_base: {
    title: 'База расчёта приближения',
    desc: 'От чего считать 2% приближения: price = от текущей цены. База расчёта ещё не ' +
          'согласована (§9, §14.4).',
    group: 'Не калибровано',
  },
  rule_version: {
    title: 'Версия правил',
    desc: 'Служебная метка, сохраняется у каждой зоны — позволяет отличить объекты, найденные ' +
          'разными версиями детектора.',
    group: 'Служебное',
  },
};

const SETTINGS_GROUP_ORDER = [
  'Поиск зон',
  'Касания и глубина',
  'Уведомления',
  'Не калибровано',
  'Служебное',
];

async function openSettings() {
  const data = await api('/api/settings');
  const form = $('settings-form');
  form.innerHTML = '';
  const uncal = new Set(data.uncalibrated);
  const entries = Object.entries(data.detector);
  for (const group of SETTINGS_GROUP_ORDER) {
    const inGroup = entries.filter(([k]) => (SETTINGS_META[k]?.group || 'Служебное') === group);
    if (!inGroup.length) continue;
    const header = document.createElement('div');
    header.className = 'settings-group';
    header.dataset.group = group;
    header.textContent = group;
    form.appendChild(header);
    for (const [key, value] of inGroup) {
      const meta = SETTINGS_META[key] || { title: key, desc: '' };
      const badge = uncal.has(key)
        ? '<span class="uncalibrated-hint">не калибровано</span>' : '';
      const wrap = document.createElement('div');
      wrap.className = 'settings-field';
      const desc = (meta.desc || '').replace(/\s*§[\d.]+/g, '');
      const head = `<div class="field-title">${meta.title}${badge}</div>` +
        (desc ? `<div class="field-desc">${desc}</div>` : '');
      if (typeof value === 'boolean') {
        wrap.innerHTML = head +
          `<label class="check-label"><input type="checkbox" data-key="${key}" ` +
          `${value ? 'checked' : ''}><span>${value ? 'включено' : 'выключено'}</span></label>`;
      } else {
        wrap.innerHTML = head + `<input data-key="${key}" value="${esc(value)}">`;
      }
      form.appendChild(wrap);
    }
  }
  const nav = $('settings-nav');
  if (nav) {
    nav.innerHTML = '';
    for (const group of groups) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = group;
      btn.onclick = () => {
        nav.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
        const target = form.querySelector(`.settings-group[data-group="${group}"]`);
        if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      };
      nav.appendChild(btn);
    }
    if (nav.firstChild) nav.firstChild.classList.add('active');
  }
  $('settings-status').textContent = '';
}

async function saveSettings() {
  const payload = {};
  document.querySelectorAll('#settings-form [data-key]').forEach((inp) => {
    if (inp.type === 'checkbox') payload[inp.dataset.key] = inp.checked;
    else {
      const num = Number(inp.value);
      payload[inp.dataset.key] = inp.value !== '' && !isNaN(num) ? num : inp.value;
    }
  });
  const res = await api('/api/settings', { method: 'POST', body: JSON.stringify(payload) });
  $('settings-status').textContent = `Сохранено: ${res.applied.length} параметров.`;
}

function showView(name) {
  const allowed = ['overview', 'review', 'events', 'settings'];
  if (!allowed.includes(name)) name = 'overview';
  document.querySelectorAll('.view').forEach((el) => el.classList.toggle('active', el.id === 'view-' + name));
  document.querySelectorAll('.app-tab[data-view], .mobile-nav a[data-view]').forEach((el) => {
    el.classList.toggle('active', el.dataset.view === name);
  });
  if (name === 'settings') openSettings();
  if (name === 'overview') requestAnimationFrame(drawZones);
  if (location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
}

function setupViews() {
  document.querySelectorAll('[data-view]').forEach((el) => {
    if (el.getAttribute('href') && el.getAttribute('href').startsWith('/')) return;
    el.addEventListener('click', (event) => {
      event.preventDefault();
      showView(el.dataset.view);
    });
  });
  window.addEventListener('hashchange', () => showView((location.hash || '#overview').slice(1)));
  showView((location.hash || '#overview').slice(1));
}

// ---------------------------------------------------------------------------
// WebSocket: обновления без перезагрузки (§11 п.6)
// ---------------------------------------------------------------------------

function handleWsMessage(data) {
  if (data.type === 'price') {
    if (data.instrument_id === state.instrumentId && data.price) {
      state.lastPrice = data.price;
      const bar = data.candles && data.candles[state.timeframe];
      if (bar && state.candleSeries) {
        state.candleSeries.update(bar);
        const last = state.candles[state.candles.length - 1];
        if (last && last.time === bar.time) state.candles[state.candles.length - 1] = bar;
        else if (!last || bar.time > last.time) state.candles.push(bar);
      }
      updateLastPrice();
      drawZones();
    }
  } else if (data.type === 'candle') {
    // новая закрытая свеча на источнике — догружаем график и зоны
    if (data.instrument_id === state.instrumentId) {
      loadCandles().then(() => loadZones());
    }
  } else if (data.type === 'event') {
    if (data.event) {
      $('events-list').prepend(eventLi(data.event));
    }
    loadZones().then(() => {
      if (state.selectedZoneId) loadZoneDetail(state.selectedZoneId, false);
    });
  } else if (data.type === 'zone') {
    loadZones().then(() => {
      if (state.selectedZoneId) loadZoneDetail(state.selectedZoneId, false);
    });
    loadCandidates();
  }
}

function connectWs() {
  // соединение/переподключение/индикатор — в common.js (window.HTF)
  state.ws = HTF.connectWs(handleWsMessage, $('ws-indicator'));
}

// ---------------------------------------------------------------------------
// Инструменты и старт
// ---------------------------------------------------------------------------

async function loadInstruments() {
  state.instruments = await api('/api/instruments');
  const sel = $('instrument-select');
  sel.innerHTML = '';
  for (const ins of state.instruments) {
    const opt = document.createElement('option');
    opt.value = ins.id;
    opt.textContent = ins.symbol + (ins.enabled ? '' : ' (откл.)');
    sel.appendChild(opt);
  }
  if (state.instruments.length && !state.instrumentId) {
    state.instrumentId = state.instruments[0].id;
    sel.value = state.instrumentId;
  }
}

async function reloadAll() {
  await loadCandles();
  await Promise.all([loadZones(), loadEvents(), loadCandidates()]);
}

async function main() {
  await ensureToken();
  setupViews();
  initChart();
  $('lnk-ltf').href = '/ltf.html?token=' + encodeURIComponent(state.token);
  if ($('lnk-ltf-mobile')) $('lnk-ltf-mobile').href = $('lnk-ltf').href;

  const hideInspector = () => {
    showInspector(false);
    clearZoneSelection();
  };
  $('instrument-select').onchange = (e) => {
    state.instrumentId = Number(e.target.value);
    hideInspector();
    reloadAll();
  };
  $('tf-select').onchange = (e) => {
    state.timeframe = e.target.value;
    if ($('chart-tf-label')) $('chart-tf-label').textContent = state.timeframe;
    hideInspector();
    reloadAll();
  };
  const updateLayers = () => {
    state.showAllTf = $('tf-all').checked;
    state.showCandidates = $('show-candidates').checked;
    // чекбоксы конкретных ТФ имеют смысл только в режиме «Все ТФ»:
    // иначе на графике всегда зоны выбранного ТФ
    ['layer-tf-d1', 'layer-tf-w1'].forEach((id) => {
      if ($(id)) $(id).disabled = !state.showAllTf;
    });
    const labels = [state.showAllTf && 'Все ТФ', state.showCandidates && 'Кандидаты'].filter(Boolean);
    if ($('layer-count')) $('layer-count').textContent = labels.length ? '· ' + labels.join(' · ') : '';
    drawZones();
  };
  $('tf-all').onchange = updateLayers;
  $('show-candidates').onchange = updateLayers;
  ['layer-tf-d1', 'layer-tf-w1', 'layer-history', 'layer-mids', 'layer-labels'].forEach((id) => {
    if ($(id)) $(id).onchange = updateLayers;
  });
  updateLayers();
  $('zone-status-filter').onchange = (e) => { state.zoneStatusFilter = e.target.value; renderZonesTable(); };
  $('zone-type-filter').onchange = (e) => { state.zoneTypeFilter = e.target.value || null; renderZonesTable(); };
  $('zone-tf-filter').onchange = (e) => { state.zoneTfFilter = e.target.value || null; renderZonesTable(); };
  if ($('zone-rel-filter')) $('zone-rel-filter').onchange = (e) => { state.zoneRelFilter = e.target.value; renderZonesTable(); };
  document.querySelectorAll('.rail-tab').forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll('.rail-tab').forEach((t) => t.classList.toggle('active', t === tab));
      state.zoneBucket = tab.dataset.bucket;
      state.orderedZoneIds = null;
      renderZonesTable();
    };
  });
  if ($('btn-resort')) $('btn-resort').onclick = () => { state.orderedZoneIds = null; renderZonesTable(); };
  if ($('btn-table-mode')) $('btn-table-mode').onclick = () => {
    state.tableMode = !state.tableMode;
    $('btn-table-mode').textContent = state.tableMode ? 'Карточки' : 'Таблица';
    $('zone-list').classList.toggle('hidden', state.tableMode);
    $('zone-table-wrap').classList.toggle('hidden', !state.tableMode);
  };
  $('detail-close').onclick = () => {
    showInspector(false);
    clearZoneSelection();
    renderZonesTable();
    requestAnimationFrame(drawZones);
  };
  $('bounds-save').onclick = saveBounds;
  $('bounds-cancel').onclick = () => closeBounds();
  $('draw-cancel').onclick = exitDrawMode;
  $('btn-settings').onclick = () => showView('settings');
  $('settings-save').onclick = saveSettings;
  $('settings-cancel').onclick = () => showView('overview');
  $('mz-save').onclick = saveManualZone;
  $('mz-cancel').onclick = () => HTF.closeModal($('manual-modal'));
  $('btn-new-zone').onclick = () => HTF.openModal($('zone-create-choice'));
  $('choice-draw').onclick = () => { HTF.closeModal($('zone-create-choice')); enterDrawMode(); };
  $('choice-form').onclick = () => { HTF.closeModal($('zone-create-choice')); openManualModal(null, null); };
  $('choice-cancel').onclick = () => HTF.closeModal($('zone-create-choice'));
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!$('bounds-modal').classList.contains('hidden')) closeBounds();
    else if (!$('zone-create-choice').classList.contains('hidden')) HTF.closeModal($('zone-create-choice'));
    else if (!$('manual-modal').classList.contains('hidden')) HTF.closeModal($('manual-modal'));
    else if (!$('settings-modal').classList.contains('hidden')) HTF.closeModal($('settings-modal'));
    else if ($('zone-detail') && !$('zone-detail').classList.contains('hidden')) {
      showInspector(false);
      clearZoneSelection();
      renderZonesTable();
      requestAnimationFrame(drawZones);
    } else exitDrawMode();
  });

  await loadInstruments();
  await loadLabels();
  await reloadAll();
  connectWs();
}

main().catch((err) => {
  document.body.insertAdjacentHTML('beforeend',
    `<div style="position:fixed;bottom:10px;left:10px;background:#ef5350;color:#fff;padding:8px 14px;border-radius:6px;z-index:200">Ошибка: ${esc(err.message)}</div>`);
});
