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
  // выбор свечи-якоря на графике для модалки исправления границ (U04)
  anchorPickMode: false,
};

// Режим «Проверка» (U04): собственный график + очередь + инспектор в одном
// экране. График создаётся лениво при первом входе во вкладку и живёт дальше.
const reviewState = {
  chart: null,
  candleSeries: null,
  candles: [],
  zone: null,        // карточка текущего кандидата (из GET /api/zones/{id})
  all: [],           // все кандидаты с сервера
  doneMap: new Map(), // id → кандидат: решения этой сессии (зона может остаться кандидатом)
  currentId: null,
  tfFilter: '',
  saving: false,
  advanceTimer: null,
  pointerDown: false,
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
    if (state.anchorPickMode) {
      const t = pickCandleTime(param, state.candles, state.chart);
      if (t !== null) finishAnchorPick(t * 1000);
      return;
    }
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
let boundsOriginal = null; // {lower, upper} — для предпросмотра «было → станет»
function askBounds(lower, upper, anchorEnabled) {
  $('bounds-title').textContent = anchorEnabled ? 'Исправить границы зоны' : 'Исправить кандидата';
  boundsOriginal = { lower, upper };
  $('bounds-lower').value = lower;
  $('bounds-upper').value = upper;
  setBoundsAnchor(null);
  updateBoundsPreview();
  $('bounds-anchor-wrap').classList.toggle('hidden', !anchorEnabled);
  $('bounds-error').textContent = '';
  HTF.openModal($('bounds-modal'));
  return new Promise((resolve) => { boundsResolve = resolve; });
}
function closeBounds(value = null) {
  if (state.anchorPickMode) finishAnchorPick(null);
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

// Предпросмотр правки границ до сохранения (U04): «было → станет».
function updateBoundsPreview() {
  const el = $('bounds-preview');
  if (!el) return;
  if (!boundsOriginal) { el.textContent = ''; return; }
  const loV = $('bounds-lower').value;
  const upV = $('bounds-upper').value;
  const lo = Number(loV);
  const up = Number(upV);
  if (!loV || !upV || !Number.isFinite(lo) || !Number.isFinite(up) || lo > up) {
    el.textContent = '';
    return;
  }
  el.textContent = `Было: ${fmtPrice(boundsOriginal.lower)} – ${fmtPrice(boundsOriginal.upper)}` +
    ` → станет: ${fmtPrice(lo)} – ${fmtPrice(up)}`;
}

// Якорь без ручного ввода миллисекунд (U04): datetime-local или клик по
// свече; сырые мс живут только в readonly-поле «Технические подробности».
function setBoundsAnchor(ms) {
  if (ms === null) {
    $('bounds-anchor').value = '';
    $('bounds-anchor-dt').value = '';
    $('bounds-anchor-display').textContent = '';
    return;
  }
  ms = Math.round(ms);
  $('bounds-anchor').value = String(ms);
  $('bounds-anchor-dt').value = toLocalInput(ms);
  $('bounds-anchor-display').textContent = 'Якорь: ' + fmtTime(ms);
}

// Режим выбора свечи-якоря кликом: модалка прячется, следующий клик по
// видимому графику (Обзор или Проверка) заполняет якорь и возвращает модалку.
function startAnchorPick() {
  state.anchorPickMode = true;
  $('bounds-modal').classList.add('hidden');
  $('anchor-pick-hint').classList.remove('hidden');
}
function finishAnchorPick(ms) {
  state.anchorPickMode = false;
  $('anchor-pick-hint').classList.add('hidden');
  if (boundsResolve) $('bounds-modal').classList.remove('hidden');
  if (ms !== null) setBoundsAnchor(ms);
}

// Время свечи по клику: param.time или координата, с привязкой к ближайшей
// загруженной свече (чтобы якорь всегда был временем открытия реальной свечи).
function pickCandleTime(param, candles, chart) {
  let timeSec = param.time !== undefined && param.time !== null
    ? Number(param.time)
    : (param.point ? Number(chart.timeScale().coordinateToTime(param.point.x)) : NaN);
  if (!Number.isFinite(timeSec)) return null;
  if (candles && candles.length) {
    let best = candles[0].time;
    let bestDist = Math.abs(best - timeSec);
    for (const c of candles) {
      const d = Math.abs(c.time - timeSec);
      if (d < bestDist) { bestDist = d; best = c.time; }
    }
    timeSec = best;
  }
  return timeSec;
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
        ? `<a class="btn small" href="/ltf.html?zone_id=${z.id}&token=${encodeURIComponent(state.token)}&instrument=${state.instrumentId}">Открыть LTF →</a>`
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
  const list = (await api('/api/candidates')).filter(isHtf); // только HTF
  reviewState.all = list;
  $('candidates-count').textContent = list.length;
  renderReviewQueue();
}

// Очередь с учётом фильтра ТФ и решений этой сессии: решения вроде
// no_context / now_irrelevant не меняют статус зоны, поэтому проверенные
// убираем из очереди локально (doneMap), иначе крутились бы по кругу.
function visibleCandidates() {
  return reviewState.all.filter((c) =>
    !reviewState.doneMap.has(c.id) &&
    (!reviewState.tfFilter || c.timeframe === reviewState.tfFilter));
}

function renderReviewQueue() {
  const box = $('candidates-list');
  const visible = visibleCandidates();
  const done = [...reviewState.doneMap.values()]
    .filter((c) => !reviewState.tfFilter || c.timeframe === reviewState.tfFilter).length;
  $('review-progress').textContent = `Проверено ${done} из ${done + visible.length}`;
  box.innerHTML = '';
  if (!visible.length) {
    box.innerHTML = '<div class="ltf-empty">Кандидатов на проверку нет.</div>';
    return;
  }
  for (const c of visible) {
    const card = document.createElement('div');
    card.className = 'candidate-card' + (c.id === reviewState.currentId ? ' selected' : '');
    card.dataset.zoneId = String(c.id);
    const why = explanationText(c.explanation);
    card.innerHTML = `
      <div class="cand-title">${c.instrument ? esc(c.instrument.symbol) : ''} · ${c.type.toUpperCase()} ${c.timeframe} ${c.direction === 'bull' ? '▲ Рост' : '▼ Снижение'}</div>
      <div>${fmtPrice(c.lower)} – ${fmtPrice(c.upper)}</div>
      ${why ? `<p class="cand-explain">${esc(why)}</p>` : ''}`;
    card.onclick = () => openReviewCandidate(c);
    box.appendChild(card);
  }
}

// График режима проверки: отдельный экземпляр от графика «Обзора».
function initReviewChart() {
  const theme = HTF.chartTheme();
  reviewState.chart = LightweightCharts.createChart($('review-chart'), {
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
  reviewState.candleSeries = reviewState.chart.addCandlestickSeries({
    upColor: theme.up, downColor: theme.down,
    wickUpColor: theme.up, wickDownColor: theme.down,
    borderVisible: false,
  });
  reviewState.chart.timeScale().subscribeVisibleLogicalRangeChange(() => drawReviewZone());
  // как в Обзоре: перетаскивание шкал не даёт событий ТФ — перерисовываем
  // зону по движению указателя (троттлинг через rAF)
  let redrawQueued = false;
  const queueRedraw = () => {
    if (redrawQueued) return;
    redrawQueued = true;
    requestAnimationFrame(() => { redrawQueued = false; drawReviewZone(); });
  };
  reviewState.chart.subscribeCrosshairMove(() => {
    if (reviewState.pointerDown) queueRedraw();
  });
  const wrap = $('review-chart-container');
  wrap.addEventListener('pointerdown', () => { reviewState.pointerDown = true; });
  window.addEventListener('pointerup', () => {
    if (!reviewState.pointerDown) return;
    reviewState.pointerDown = false;
    queueRedraw();
  });
  wrap.addEventListener('dblclick', () => queueRedraw());
  // выбор свечи-якоря кликом (модалка исправления границ, U04)
  reviewState.chart.subscribeClick((param) => {
    if (!state.anchorPickMode) return;
    const t = pickCandleTime(param, reviewState.candles, reviewState.chart);
    if (t !== null) finishAnchorPick(t * 1000);
  });
  new ResizeObserver(() => drawReviewZone()).observe(wrap);
}

// Вход во вкладку «Проверка»: график при первом входе, очередь, автовыбор
// первого кандидата, если текущий не выбран или уже выпал из очереди.
async function enterReviewMode() {
  if (!reviewState.chart) initReviewChart();
  await loadCandidates();
  const visible = visibleCandidates();
  if (!visible.length) return;
  if (!visible.some((c) => c.id === reviewState.currentId)) {
    openReviewCandidate(visible[0]);
  }
}

// Клик по кандидату: НЕ переключаем вкладку — грузим его инструмент/ТФ,
// свечи вокруг формирования зоны и открываем инспектор здесь же.
async function openReviewCandidate(c) {
  if (reviewState.advanceTimer) {
    clearTimeout(reviewState.advanceTimer);
    reviewState.advanceTimer = null;
  }
  reviewState.currentId = c.id;
  renderReviewQueue();
  const insp = $('review-inspector');
  insp.innerHTML = '<h2>Проверка зоны</h2><p class="muted">Загружаю зону и свечи…</p>';
  if (!reviewState.chart) initReviewChart();
  const [detail, candles] = await Promise.all([
    api(`/api/zones/${c.id}`),
    api(`/api/candles?instrument_id=${c.instrument_id}&timeframe=${c.timeframe}&limit=5000`),
  ]);
  if (reviewState.currentId !== c.id) return; // пользователь выбрал другого
  reviewState.zone = detail.zone;
  reviewState.candles = candles;
  reviewState.candleSeries.setData(candles);
  // окно: от display_from/formed_at с запасом до текущего момента
  const z = detail.zone;
  const tfSec = TF_SECONDS[z.timeframe] || 86400;
  const fromMs = z.display_from || z.formed_at;
  const firstBar = candles.length ? candles[0].time : Math.floor(fromMs / 1000);
  const to = Math.floor(Date.now() / 1000) + 3 * tfSec;
  let from = Math.max(firstBar, Math.floor(fromMs / 1000) - 15 * tfSec);
  if (from >= to) from = Math.max(firstBar, to - 95 * tfSec); // зона старше загруженных свечей
  requestAnimationFrame(() => requestAnimationFrame(() => {
    reviewState.chart.timeScale().setVisibleRange({ from, to });
    drawReviewZone();
  }));
  renderReviewInspector(detail);
  drawReviewZone();
}

// Зона на графике проверки: границы, середина, статус — как в Обзоре,
// но всегда одна зона.
function drawReviewZone() {
  const overlay = $('review-zone-overlay');
  if (!overlay) return;
  overlay.innerHTML = '';
  const z = reviewState.zone;
  if (!z || !reviewState.candleSeries || !reviewState.candles.length) return;
  const chartEl = $('review-chart');
  const width = chartEl.clientWidth;
  const height = chartEl.clientHeight;
  const paneRight = width - reviewState.chart.priceScale('right').width();
  const ts = reviewState.chart.timeScale();
  const y1 = reviewState.candleSeries.priceToCoordinate(z.upper);
  const y2 = reviewState.candleSeries.priceToCoordinate(z.lower);
  if (y1 === null || y2 === null) return;
  let top = Math.min(y1, y2);
  let bottom = Math.max(y1, y2);
  if (bottom < 0 || top > height) return;
  top = Math.max(0, top);
  bottom = Math.min(height, bottom);
  const fromMs = z.display_from || z.formed_at;
  let x1 = ts.timeToCoordinate(Math.floor(fromMs / 1000));
  if (x1 === null || x1 < 0) x1 = 0;
  if (x1 > paneRight) return;
  let x2 = paneRight;
  if (z.display_until) {
    const xc = ts.timeToCoordinate(Math.floor(z.display_until / 1000));
    if (xc !== null) x2 = Math.min(paneRight, xc);
  }
  if (x2 <= x1) return;
  const div = document.createElement('div');
  div.className = `zone-rect z-${z.type} status-${z.status}` +
    (z.display_until ? ' status-completed' : '') + ' selected';
  div.style.top = top + 'px';
  div.style.height = Math.max(2, bottom - top) + 'px';
  div.style.left = x1 + 'px';
  div.style.width = Math.max(8, x2 - x1) + 'px';
  if (!z.is_level) {
    const midY = reviewState.candleSeries.priceToCoordinate(z.mid);
    if (midY !== null && midY >= 0 && midY <= height) {
      const mid = document.createElement('div');
      mid.className = 'zone-mid';
      mid.style.top = (midY - top) + 'px';
      div.appendChild(mid);
    }
  }
  const label = document.createElement('span');
  label.className = 'zone-label review-zone-label';
  label.textContent =
    `${z.type.toUpperCase()} ${z.timeframe} · ${STATUS_RU[z.status] || z.status}` +
    (z.name ? ` · ${z.name}` : '');
  div.appendChild(label);
  div.title = label.textContent;
  overlay.appendChild(div);
}

const VERDICT_GEOM_RU = {
  valid: 'верна', invalid: 'ошибка', needs_correction: 'исправлена', unknown: 'нет оценки',
};
const VERDICT_LIFE_RU = { completed: 'завершён', converted: 'конвертирован' };

// Инспектор кандидата внутри вкладки «Проверка»: решения о геометрии;
// рыночная актуальность показана отдельным блоком и не смешивается с ними.
function renderReviewInspector(detail) {
  const z = detail.zone;
  const el = $('review-inspector');
  const ins = detail.instrument;
  const assessments = detail.assessments || [];
  const lastA = assessments[assessments.length - 1];
  const range = z.is_level ? fmtPrice(z.lower) : `${fmtPrice(z.lower)} – ${fmtPrice(z.upper)}`;
  el.innerHTML = `
    <h3>${ins ? esc(ins.symbol) + ' · ' : ''}${z.type.toUpperCase()} ${z.timeframe}${z.name ? ' · ' + esc(z.name) : ''} <span class="badge ${z.status}">${STATUS_RU[z.status] || z.status}</span></h3>
    <dl>
      <dt>Диапазон</dt><dd>${range}</dd>
      <dt>Середина</dt><dd>${fmtPrice(z.mid)}</dd>
      <dt>Направление</dt><dd class="dir ${z.direction}">${z.direction === 'bull' ? 'Рост' : 'Снижение'}</dd>
      <dt>Основание зоны</dt><dd>${fmtTime(z.formed_at)}</dd>
    </dl>
    <div class="rv-status">
      <h4>Актуальность зоны</h4>
      <p>Статус: ${STATUS_RU[z.status] || z.status}${z.display_until ? ' · завершена' + (z.end_reason ? ' (' + esc(z.end_reason) + ')' : '') : ' · живая'}</p>
      ${lastA
        ? `<p>Последняя оценка (${fmtTime(lastA.reviewed_at)}): геометрия — ${VERDICT_GEOM_RU[lastA.geometry_verdict] || lastA.geometry_verdict}` +
          `, цикл — ${lastA.lifecycle_verdict ? (VERDICT_LIFE_RU[lastA.lifecycle_verdict] || lastA.lifecycle_verdict) : '—'}` +
          `${lastA.requires_clarification ? ' · требует уточнения' : ''}</p>`
        : '<p>Оценок пока нет.</p>'}
      <p class="muted">Это справка об актуальности — решение ниже только о геометрии разметки.</p>
    </div>
    <div class="review-block">
      <h4>Решение о разметке</h4>
      <textarea id="rv-comment" rows="2" placeholder="Комментарий к решению"></textarea>
      <div class="review-actions">
        <button class="btn ok" type="button" data-review="correct">1 · Размечено верно</button>
        <button class="btn primary" type="button" data-review="fix_boundaries">2 · Исправить границы</button>
        <button class="btn danger" type="button" data-review="wrong_type">3 · Отклонить (неверный тип)</button>
        <details data-section="other-reviews"><summary class="btn">Другие решения</summary><div class="review-actions">
          <button class="btn" type="button" data-review="wrong_base">Другое основание</button>
          <button class="btn" type="button" data-review="now_irrelevant">Сейчас неактуально</button>
          <button class="btn" type="button" data-review="already_breaker">Уже Breaker</button>
          <button class="btn" type="button" data-review="no_context">Нет контекста</button>
        </div></details>
      </div>
      <div id="rv-verdict" class="review-verdict"></div>
    </div>
    <details data-section="history"><summary>История</summary>
      <h4>События (${detail.events.length})</h4>
      <ul>${detail.events.map((e) => `<li><span class="ev-time">${fmtTime(e.occurred_at)}</span>${EVENT_KIND_RU[e.kind] || e.kind} @ ${fmtPrice(e.price)}${e.delayed ? ' (восстановлено)' : ''}</li>`).join('') || '<li>нет</li>'}</ul>
      <h4>Проверки (${detail.reviews.length})</h4>
      <ul>${detail.reviews.map((r) => `<li><span class="ev-time">${fmtTime(r.created_at)}</span>${REVIEW_DECISION_RU[r.decision] || r.decision}${r.text ? ': ' + esc(r.text) : ''}</li>`).join('') || '<li>нет</li>'}</ul>
      ${detail.boundary_corrections.length ? `<h4>Правки границ</h4><ul>${detail.boundary_corrections.map((c) => `<li>${fmtPrice(c.original_lower)}–${fmtPrice(c.original_upper)} → ${fmtPrice(c.corrected_lower)}–${fmtPrice(c.corrected_upper)}${c.anchor_candle_open_time ? ' · якорь ' + fmtTime(c.anchor_candle_open_time) : ''}${c.reason ? ' · ' + esc(c.reason) : ''}</li>`).join('')}</ul>` : ''}
      ${assessments.length ? `<h4>Оценки</h4><ul>${assessments.map((a) => `<li><span class="ev-time">${fmtTime(a.reviewed_at)}</span>${REVIEW_DECISION_RU[a.review_decision] || a.review_decision}: геометрия ${VERDICT_GEOM_RU[a.geometry_verdict] || a.geometry_verdict}${a.lifecycle_verdict ? ', цикл ' + (VERDICT_LIFE_RU[a.lifecycle_verdict] || a.lifecycle_verdict) : ''}${a.requires_clarification ? ', требует уточнения' : ''}</li>`).join('')}</ul>` : ''}
    </details>
  `;
  el.querySelectorAll('[data-review]').forEach((btn) => {
    btn.onclick = () => submitReviewDecision(btn.dataset.review);
  });
}

// Решение в режиме проверки (U04): тот же POST /api/zones/{id}/review, что и
// в инспекторе Обзора. При ошибке кандидат остаётся в очереди, перехода к
// следующему нет. После успеха — автопереход к следующему кандидату.
async function submitReviewDecision(decision) {
  const z = reviewState.zone;
  if (!z || reviewState.saving) return;
  const textEl = $('rv-comment');
  const body = { decision, text: textEl ? textEl.value.trim() : '' };
  if (decision === 'fix_boundaries') {
    const bounds = await askBounds(z.lower, z.upper, true);
    if (!bounds) return;
    Object.assign(body, bounds);
  }
  reviewState.saving = true;
  const btns = document.querySelectorAll('#review-inspector [data-review]');
  const vEl = $('rv-verdict');
  btns.forEach((b) => { b.disabled = true; });
  if (vEl) vEl.textContent = 'Сохраняю решение… первая проверка зоны может занять до минуты';
  let res;
  try {
    res = await api(`/api/zones/${z.id}/review`, {
      method: 'POST', body: JSON.stringify(body),
    });
  } catch (err) {
    reviewState.saving = false;
    btns.forEach((b) => { b.disabled = false; });
    if (vEl) vEl.textContent = 'Ошибка: ' + err.message + ' — кандидат остался в очереди.';
    return;
  }
  reviewState.saving = false;
  const cand = reviewState.all.find((c) => c.id === z.id) || z;
  reviewState.doneMap.set(z.id, cand);
  if (vEl && res.assessment) {
    const a = res.assessment;
    vEl.textContent = `Сохранено. Геометрия: ${VERDICT_GEOM_RU[a.geometry_verdict] || a.geometry_verdict}` +
      (a.lifecycle_verdict ? ` · цикл: ${VERDICT_LIFE_RU[a.lifecycle_verdict] || a.lifecycle_verdict}` : '') +
      (a.requires_clarification ? ' · требует уточнения' : '');
  }
  loadZones(); // вкладка «Обзор» увидит новые статусы
  reviewState.advanceTimer = setTimeout(async () => {
    reviewState.advanceTimer = null;
    await loadCandidates();
    advanceReviewQueue();
  }, 400);
}

// Следующий кандидат очереди (по кругу); после решения — первый видимый.
function nextReviewCandidate() {
  const visible = visibleCandidates();
  if (!visible.length) return;
  const idx = visible.findIndex((c) => c.id === reviewState.currentId);
  openReviewCandidate(visible[(idx + 1) % visible.length]);
}

function advanceReviewQueue() {
  const visible = visibleCandidates();
  if (!visible.length) {
    reviewState.currentId = null;
    reviewState.zone = null;
    reviewState.candles = [];
    if (reviewState.candleSeries) reviewState.candleSeries.setData([]);
    drawReviewZone();
    renderReviewQueue();
    $('review-inspector').innerHTML =
      '<h2>Проверка зоны</h2><p class="muted">Очередь пуста — все кандидаты проверены.</p>';
    return;
  }
  openReviewCandidate(visible[0]);
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

// L04: группы настроек — из GET /api/settings (поле groups), не из META
const SETTINGS_GROUP_TITLES = {
  analysis: 'Анализ',
  delivery: 'Доставка и уведомления',
  experimental: 'Экспериментальные (не калиброваны)',
};

function settingsFieldHtml(key, value, uncal) {
  const meta = SETTINGS_META[key] || { title: key, desc: '' };
  const badge = uncal
    ? '<span class="uncalibrated-hint">не калибровано</span>' : '';
  const desc = (meta.desc || '').replace(/\s*§[\d.]+/g, '');
  const head = `<div class="field-title">${meta.title}${badge}</div>` +
    (desc ? `<div class="field-desc">${desc}</div>` : '');
  if (typeof value === 'boolean') {
    return head +
      `<label class="check-label"><input type="checkbox" data-key="${key}" ` +
      `${value ? 'checked' : ''}><span>${value ? 'включено' : 'выключено'}</span></label>`;
  }
  return head + `<input data-key="${key}" value="${esc(value)}">`;
}

async function openSettings() {
  const data = await api('/api/settings');
  const form = $('settings-form');
  form.innerHTML = '';
  const uncal = new Set(data.uncalibrated || []);
  const deprecated = new Set(data.deprecated || []);
  const groupOf = data.groups || {};
  const entries = Object.entries(data.detector);
  settingsOriginal = data.detector || {};
  const navGroups = [];
  for (const group of ['analysis', 'delivery', 'experimental']) {
    const inGroup = entries.filter(
      ([k]) => !deprecated.has(k) && (groupOf[k] || 'analysis') === group);
    if (!inGroup.length) continue;
    navGroups.push(group);
    if (group === 'experimental') {
      // сворачиваемый раздел внизу формы с явной пометкой о влиянии на расчёты
      const det = document.createElement('details');
      det.className = 'settings-exp';
      det.innerHTML =
        `<summary class="settings-group" data-group="${group}">${SETTINGS_GROUP_TITLES[group]}</summary>` +
        '<div class="field-desc settings-exp-note">Значения не калиброваны и ' +
        'могут влиять на расчёты: найденные зоны, экстремумы и допуски.</div>';
      for (const [key, value] of inGroup) {
        const wrap = document.createElement('div');
        wrap.className = 'settings-field';
        wrap.innerHTML = settingsFieldHtml(key, value, uncal.has(key));
        det.appendChild(wrap);
      }
      form.appendChild(det);
    } else {
      const header = document.createElement('div');
      header.className = 'settings-group';
      header.dataset.group = group;
      header.textContent = SETTINGS_GROUP_TITLES[group];
      form.appendChild(header);
      for (const [key, value] of inGroup) {
        const wrap = document.createElement('div');
        wrap.className = 'settings-field';
        wrap.innerHTML = settingsFieldHtml(key, value, uncal.has(key));
        form.appendChild(wrap);
      }
    }
  }
  // устаревшие поля (L04): не редактируются — только строка-примечание
  const depEntries = entries.filter(([k]) => deprecated.has(k));
  if (depEntries.length) {
    const note = document.createElement('div');
    note.className = 'settings-deprecated';
    note.innerHTML = depEntries.map(([k]) =>
      `<div>Поле <code>${esc(k)}</code> устарело и не редактируется.</div>`).join('');
    form.appendChild(note);
  }
  // сайдбар-навигация по группам: переход к разделу без перезагрузки
  const nav = $('settings-nav');
  if (nav) {
    nav.innerHTML = '';
    for (const group of navGroups) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.textContent = SETTINGS_GROUP_TITLES[group];
      btn.onclick = () => {
        nav.querySelectorAll('button').forEach((b) => b.classList.toggle('active', b === btn));
        const target = form.querySelector(`.settings-group[data-group="${group}"]`);
        if (target) {
          const det = target.closest('details');
          if (det) det.open = true;
          target.scrollIntoView({ behavior: 'smooth', block: 'start' });
        }
      };
      nav.appendChild(btn);
    }
    if (nav.firstChild) nav.firstChild.classList.add('active');
  }
  $('settings-status').textContent = '';
}

// POST /api/settings напрямую через fetch: общий api() сворачивает тело 422
// в строку, а для показа ошибок по полям нужен detail.fields (L04)
async function postSettings(payload) {
  const doFetch = () => fetch('/api/settings', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': 'Bearer ' + HTF.getToken(),
    },
    body: JSON.stringify(payload),
  });
  let resp = await doFetch();
  if (resp.status === 401) {
    localStorage.removeItem('htf_token');
    await HTF.ensureToken('Токен не подошёл. Проверьте значение и попробуйте снова.');
    resp = await doFetch();
  }
  return resp;
}

// L04: сервер валидирует патч строго — отправляем только изменённые поля
let settingsOriginal = {};

async function saveSettings() {
  const payload = {};
  document.querySelectorAll('#settings-form [data-key]').forEach((inp) => {
    const key = inp.dataset.key;
    const orig = settingsOriginal[key];
    if (inp.type === 'checkbox') {
      if (inp.checked !== Boolean(orig)) payload[key] = inp.checked;
    } else if (inp.value !== String(orig ?? '')) {
      const num = Number(inp.value);
      payload[key] = inp.value !== '' && !isNaN(num) ? num : inp.value;
    }
  });
  const status = $('settings-status');
  const form = $('settings-form');
  form.querySelectorAll('.field-error').forEach((e) => e.remove());
  form.querySelectorAll('.settings-field.invalid').forEach((e) => e.classList.remove('invalid'));
  status.textContent = '';
  let resp;
  try {
    resp = await postSettings(payload);
  } catch (err) {
    status.textContent = 'Не удалось сохранить: ' + err.message;
    return;
  }
  if (resp.status === 422) {
    // ошибки по полям: подсветка у полей + сводка сверху; форма не закрывается
    let fields = {};
    try { fields = ((await resp.json()).detail || {}).fields || {}; } catch (e) { /* не JSON */ }
    const names = [];
    for (const [key, msg] of Object.entries(fields)) {
      names.push(`${(SETTINGS_META[key] || {}).title || key}: ${msg}`);
      const inp = form.querySelector(`[data-key="${key}"]`);
      const wrap = inp && inp.closest('.settings-field');
      if (wrap) {
        wrap.classList.add('invalid');
        const err = document.createElement('div');
        err.className = 'field-error';
        err.textContent = msg;
        wrap.appendChild(err);
      }
    }
    status.textContent = names.length
      ? 'Не сохранено: ' + names.join('; ')
      : 'Не сохранено: настройки отклонены сервером.';
    return;
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (e) { /* не JSON */ }
    status.textContent = 'Не сохранено: ' +
      (typeof detail === 'string' ? detail : 'ошибка сервера');
    return;
  }
  const res = await resp.json();
  status.textContent = `Сохранено: ${res.applied.length} параметров.`;
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
  if (name === 'review') enterReviewMode().catch((e) => console.warn('review mode:', e));
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

// U01-lite: единый ключ выбранного инструмента для обеих страниц (HTF/LTF).
// Приоритет: ?instrument= в URL → localStorage → первый в списке.
const INSTRUMENT_KEY = 'htf:instrument';

function resolveInitialInstrument() {
  const ids = new Set(state.instruments.map((i) => i.id));
  const fromUrl = Number(new URLSearchParams(location.search).get('instrument'));
  if (fromUrl && ids.has(fromUrl)) return fromUrl;
  const saved = Number(localStorage.getItem(INSTRUMENT_KEY));
  if (saved && ids.has(saved)) return saved;
  return state.instruments[0].id;
}

function updateLtfLinks() {
  const url = new URL('/ltf.html', location.origin);
  if (state.token) url.searchParams.set('token', state.token);
  if (state.instrumentId) url.searchParams.set('instrument', String(state.instrumentId));
  const href = url.pathname + url.search;
  if ($('lnk-ltf')) $('lnk-ltf').href = href;
  if ($('lnk-ltf-mobile')) $('lnk-ltf-mobile').href = href;
}

function syncInstrumentContext() {
  // выбор инструмента разделяется со страницей LTF: localStorage + ?instrument=
  // (history.replaceState, без перезагрузки; hash вкладки сохраняется)
  localStorage.setItem(INSTRUMENT_KEY, String(state.instrumentId));
  const url = new URL(location.href);
  url.searchParams.set('instrument', String(state.instrumentId));
  history.replaceState(null, '', url.pathname + url.search + url.hash);
  updateLtfLinks();
}

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
    state.instrumentId = resolveInitialInstrument();
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

  const hideInspector = () => {
    showInspector(false);
    clearZoneSelection();
  };
  $('instrument-select').onchange = (e) => {
    state.instrumentId = Number(e.target.value);
    syncInstrumentContext();
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
  $('bounds-lower').oninput = updateBoundsPreview;
  $('bounds-upper').oninput = updateBoundsPreview;
  $('bounds-anchor-dt').onchange = (e) => {
    const raw = e.target.value;
    if (!raw) { setBoundsAnchor(null); return; }
    const ms = new Date(raw).getTime(); // datetime-local → локальное время
    if (!isNaN(ms)) setBoundsAnchor(ms);
  };
  $('bounds-anchor-pick').onclick = startAnchorPick;
  $('bounds-anchor-clear').onclick = () => setBoundsAnchor(null);
  $('anchor-pick-cancel').onclick = () => finishAnchorPick(null);
  if ($('review-tf-filter')) {
    $('review-tf-filter').onchange = (e) => {
      reviewState.tfFilter = e.target.value;
      renderReviewQueue();
    };
  }
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
    if (state.anchorPickMode) finishAnchorPick(null);
    else if (!$('bounds-modal').classList.contains('hidden')) closeBounds();
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
  // Горячие клавиши режима проверки (U04): 1 — верно, 2 — границы,
  // 3 — отклонить, → / Enter — следующий. Не срабатывают из полей ввода.
  document.addEventListener('keydown', (e) => {
    if (!$('view-review').classList.contains('active')) return;
    // открыта модалка (границы, ручная зона) или идёт выбор якоря — не вмешиваемся
    if (state.anchorPickMode) return;
    if (document.querySelector('.modal:not(.hidden)')) return;
    const ae = document.activeElement;
    if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.tagName === 'SELECT')) return;
    // Enter на сфокусированной кнопке — её собственное действие, не «следующий»
    if (e.key === 'Enter' && ae && ae.tagName === 'BUTTON') return;
    if (e.key === '1') { e.preventDefault(); submitReviewDecision('correct'); }
    else if (e.key === '2') { e.preventDefault(); submitReviewDecision('fix_boundaries'); }
    else if (e.key === '3') { e.preventDefault(); submitReviewDecision('wrong_type'); }
    else if (e.key === 'ArrowRight' || e.key === 'Enter') { e.preventDefault(); nextReviewCandidate(); }
  });

  await loadInstruments();
  syncInstrumentContext();
  await loadLabels();
  await reloadAll();
  connectWs();
}

main().catch((err) => {
  document.body.insertAdjacentHTML('beforeend',
    `<div style="position:fixed;bottom:10px;left:10px;background:#ef5350;color:#fff;padding:8px 14px;border-radius:6px;z-index:200">Ошибка: ${esc(err.message)}</div>`);
});
