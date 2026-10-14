/* HTF Zones — фронтенд (vanilla JS + lightweight-charts).
   Токен владельца: prompt() при старте, хранится в localStorage,
   шлётся в каждом запросе (Bearer) и в WebSocket (?token=). */

'use strict';

// ---------------------------------------------------------------------------
// Состояние
// ---------------------------------------------------------------------------

const state = {
  // токен можно передать в URL (?token=...) — сохранится в localStorage
  token: '',
  instruments: [],
  instrumentId: null,
  timeframe: 'D1',
  chartMode: 'context',
  savedTimeframe: 'D1',
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
  // Снимок «Все ТФ» / D1 / W1 до входа в структуру H1. Пока null — при входе
  // селекторы включаются. Пока режим открыт, повторная отрисовка их не трогает.
  tfLayersBeforeH1: null,
  maxChartZones: 80,
  orderedZoneIds: null,
  // ТЗ §5: внутренние уровни выбранной зоны (рисуются, пока зона выбрана)
  innerLevels: [],
  innerLevelsZoneId: null,
  // выбор свечи-якоря на графике для модалки исправления границ (U04)
  anchorPickMode: false,
  h1Layers: null,
  dailyStructure: null,
  h1Req: 0,
  h1LoadError: false,
  h1SelectedEventId: null,
  h1SelectedZoneId: null,
  h1PriceFocus: null,
  h1HistoryTimer: null,
  h1Settings: null,
  loadedCandleTf: null,
  loadedCandleInstrument: null,
  candleReq: 0,
  zoneReq: 0,
  eventReq: 0,
  candidateReq: 0,
  candleVersion: 0,
  warmupText: null,   // §11: текст прогрева над пустым графиком (событие lf-warmup)
};

// Режим «Проверка» (U04): собственный график + очередь + инспектор в одном
// экране. График создаётся лениво при первом входе во вкладку и живёт дальше.
const reviewState = {
  chart: null,
  candleSeries: null,
  candles: [],
  zone: null,        // карточка текущего кандидата (из GET /api/zones/{id})
  currentCand: null, // строка очереди /api/candidates (причина, объяснение)
  all: [],           // все кандидаты с сервера
  doneMap: new Map(), // id → кандидат: решения этой сессии (зона может остаться кандидатом)
  currentId: null,
  tfFilter: '',
  saving: false,
  advanceTimer: null,
  pointerDown: false,
  boundsDrafts: new Map(),
};

// Статусы, видимые на графике по умолчанию
const CHART_STATUSES = new Set(['candidate', 'active', 'weakened', 'worked']);
// Снятый HTF BSL/SSL остаётся на графике 7 суток. Линия кончается в display_until.
const SWEPT_LEVEL_KEEP_MS = 7 * 86_400_000;

function sweptLevelVisible(z, now = Date.now()) {
  if (!z || (z.type !== 'ssl' && z.type !== 'bsl')) return false;
  if (z.status !== 'taken' || !z.display_until) return false;
  const age = now - z.display_until;
  return age >= 0 && age <= SWEPT_LEVEL_KEEP_MS;
}

function structurePointColor(role) {
  const name = String(role || '').toUpperCase();
  const css = getComputedStyle(document.documentElement);
  if (name === 'HH' || name === 'HL') {
    return css.getPropertyValue('--lf-positive').trim() || '#62C9B0';
  }
  if (name === 'LL' || name === 'LH') {
    return css.getPropertyValue('--lf-negative').trim() || '#F08D98';
  }
  return css.getPropertyValue('--lf-text2').trim() || '#A2B0C5';
}

// Момент снятия — время котировки, не открытие свечи. Шкала графика знает
// только открытия, поэтому конец линии ставится внутрь той свечи, куда
// попало снятие, и не тянется до правого края.
function barCoordinate(ts, ms, bars = state.candles, timeframe = state.timeframe) {
  if (ms == null || !bars.length) return null;
  const sec = Math.floor(Number(ms) / 1000);
  if (!Number.isFinite(sec)) return null;
  const exact = ts.timeToCoordinate(sec);
  if (exact !== null) return exact;
  let lo = 0;
  let hi = bars.length - 1;
  let best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (bars[mid].time <= sec) { best = mid; lo = mid + 1; }
    else hi = mid - 1;
  }
  if (best < 0) return null;
  const x0 = ts.timeToCoordinate(bars[best].time);
  if (x0 === null) return null;
  const next = bars[best + 1];
  if (next) {
    const x1 = ts.timeToCoordinate(next.time);
    if (x1 === null || next.time <= bars[best].time) return x0;
    const frac = (sec - bars[best].time) / (next.time - bars[best].time);
    return x0 + Math.min(1, Math.max(0, frac)) * (x1 - x0);
  }
  const prev = bars[best - 1];
  const period = prev
    ? (bars[best].time - prev.time)
    : (TF_SECONDS[timeframe] || 86400);
  const prevX = prev ? ts.timeToCoordinate(prev.time) : null;
  const barPx = prevX !== null ? (x0 - prevX) : 8;
  const frac = period > 0 ? Math.min(1, Math.max(0, (sec - bars[best].time) / period)) : 1;
  return x0 + frac * Math.max(barPx, 1);
}

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

// Типы зон (app/texts_ru.py TYPE_RU) — для карточек очереди проверки
let TYPE_RU = {
  fvg: 'FVG', ob: 'Orderblock', prb: 'PRB', breaker: 'Breaker',
  ssl: 'SSL', bsl: 'BSL', manual: 'Ручная зона',
};

async function loadLabels() {
  try {
    const l = await api('/api/labels');
    if (l.event_kinds) EVENT_KIND_RU = { ...EVENT_KIND_RU, ...l.event_kinds };
    if (l.statuses) STATUS_RU = { ...STATUS_RU, ...l.statuses };
    if (l.types) TYPE_RU = { ...TYPE_RU, ...l.types };
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
  state.candleSeries.applyOptions({
    autoscaleInfoProvider: (original) => {
      const focus = state.h1PriceFocus;
      if (!focus) return original ? original() : null;
      const lo = Math.min(Number(focus.lower), Number(focus.upper));
      const hi = Math.max(Number(focus.lower), Number(focus.upper));
      if (!Number.isFinite(lo) || !Number.isFinite(hi)) return original ? original() : null;
      const mid = (lo + hi) / 2;
      const span = Math.max(Math.abs(hi - lo) / 2, Math.abs(mid) * 0.012, 1);
      return { priceRange: { minValue: mid - span * 1.6, maxValue: mid + span * 1.6 } };
    },
  });
  // Перерисовка зон при сдвиге времени. Свой кадр ноги и догрузка свечей
  // тоже шлют это событие — прокруткой считаем только жест на графике.
  state.chart.timeScale().subscribeVisibleLogicalRangeChange(() => {
    if (!state.h1ApplyingFrame && state.h1FrameKey && (state.pointerDown || state.h1WheelZoom)) {
      state.h1UserPanned = true;
    }
    drawZones();
    scheduleH1HistoryReload();
  });
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
  chartWrap.addEventListener('click', (event) => {
    if (event.target.closest('#h1-event-card, .h1-zone, .h1-zone-tag, .h1-liquidity')) return;
    closeH1Card();
  });
  chartWrap.addEventListener('pointerdown', () => { state.pointerDown = true; });
  chartWrap.addEventListener('wheel', () => {
    state.h1WheelZoom = true;
    clearTimeout(state.h1WheelTimer);
    state.h1WheelTimer = setTimeout(() => { state.h1WheelZoom = false; }, 400);
  }, { passive: true });
  window.addEventListener('pointerup', () => {
    if (!state.pointerDown) return;
    state.pointerDown = false;
    queueZoneRedraw(); // финальная перерисовка после отпускания
  });
  // двойной клик по шкале цен сбрасывает масштаб (autoscale)
  chartWrap.addEventListener('dblclick', () => {
    state.h1PriceFocus = null;
    queueZoneRedraw();
  });
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
  const id = state.instrumentId;
  const tf = state.timeframe;
  const req = ++state.candleReq;
  const limit = tf === 'H1' ? 1500 : 500;
  const preserve = state.loadedCandleTf === tf && state.loadedCandleInstrument === id && state.chart
    && state.candles && state.candles.length
    ? HTF.captureChartRange(state.chart, state.candles)
    : null;
  const response = await api(
    `/api/candles?instrument_id=${id}&timeframe=${tf}&limit=${limit}&with_version=true`);
  if (req !== state.candleReq || id !== state.instrumentId || tf !== state.timeframe) return;
  // Новый ответ — {candles, state_version}. Старый процесс отдаёт сам список:
  // нельзя записывать undefined, иначе график гаснет на .length.
  const versioned = response && !Array.isArray(response);
  const candles = versioned ? (response.candles || []) : (Array.isArray(response) ? response : []);
  const version = versioned && response.state_version != null ? response.state_version : null;
  if (version != null && state.loadedCandleInstrument === id && state.loadedCandleTf === tf
      && version < state.candleVersion) return;
  state.candles = candles;
  state.dailyStructure = versioned ? response.daily_structure || null : null;
  if (version != null) state.candleVersion = version;
  // §13 (этап 6b): доменные версии снимка — точка отсчёта для diff'ов
  if (versioned && response.versions) {
    domainSync.adopt(`candles:${id}:${tf}`, response.versions.candles);
    domainSync.adopt(`quote:${id}`, response.versions.quote);
  }
  state.loadedCandleTf = tf;
  state.loadedCandleInstrument = id;
  state.candleSeries.setData(state.candles);
  if (state.candles.length) {
    state.lastPrice = state.candles[state.candles.length - 1].close;
    updateLastPrice();
  }
  // Пока пользователь не сдвинул H1 сам, диапазон задаёт кадр ноги.
  // Иначе fitContent на 1500 свечей (и его восстановление после догрузки)
  // снова растягивает шкалу, и зоны входа становятся полосками.
  if (state.chartMode === 'h1' && state.h1FrameInstrument !== id) state.h1UserPanned = false;
  const holdForLeg = state.chartMode === 'h1' && !state.h1UserPanned;
  if (holdForLeg) {
    applyH1LegFrame(state.h1Layers);
  } else if (preserve) {
    HTF.restoreChartRange(state.chart, state.candles, preserve);
  } else if (!holdForLeg) {
    state.chart.timeScale().fitContent();
  }
  // пересчёт координат зон после полного макета графика
  requestAnimationFrame(drawZones);
  updateChartWarmup();
}

// Текст прогрева над пустым графиком (§11, AC01): свечей ещё нет, а сервер
// прогревается — показываем фазу/прогресс вместо пустой области
function updateChartWarmup() {
  const el = $('chart-warmup');
  if (!el) return;
  const show = !state.candles.length && !!state.warmupText;
  el.classList.toggle('hidden', !show);
  if (show) el.textContent = state.warmupText;
}

document.addEventListener('lf-warmup', (e) => {
  state.warmupText = e.detail && !e.detail.done ? e.detail.text : null;
  updateChartWarmup();
});

// ---------------------------------------------------------------------------
// Зоны: загрузка и отрисовка поверх графика
// ---------------------------------------------------------------------------

async function loadZones() {
  if (!state.instrumentId) return;
  const id = state.instrumentId;
  const req = ++state.zoneReq;
  // §13 (этап 6b): opt-in конверт {zones, versions}; сгруппированный ответ
  // несёт ту же версию домена зон
  const [zonesRes, grouped] = await Promise.all([
    api(`/api/zones?instrument_id=${id}&with_version=true`),
    api(`/api/zones/grouped?instrument_id=${id}`),
  ]);
  if (id !== state.instrumentId || req !== state.zoneReq) return;
  const zones = Array.isArray(zonesRes) ? zonesRes : (zonesRes && zonesRes.zones) || [];
  const zonesVersion = (!Array.isArray(zonesRes) && zonesRes && zonesRes.versions)
    ? zonesRes.versions.zones
    : (grouped && grouped.versions ? grouped.versions.zones : null);
  if (zonesVersion != null) domainSync.adopt('zones', zonesVersion);
  state.zones = zones;
  state.groups = grouped.groups.filter((g) => g.zones.length > 1);
  drawZones();
  renderZonesTable();
}

// BSL/SSL: цвет инвертирован относительно направления движения
// (BSL — зелёная, SSL — красная), совпадает с графиком и тегами.
function swatchDirClass(z) {
  const bear = z.direction === 'bear';
  const invert = z.type === 'BSL' || z.type === 'SSL';
  return (bear !== invert) ? 'dir-bear' : 'dir-bull';
}
window.swatchDirClass = swatchDirClass;

function safeSwatchClass(z) {
  try {
    const fn = typeof swatchDirClass === 'function' ? swatchDirClass : window.swatchDirClass;
    return fn ? fn(z) : '';
  } catch (err) {
    return '';
  }
}

function drawZones() {
  const overlay = $('zone-overlay');
  overlay.innerHTML = '';
  if (!state.candleSeries || !state.candles || !state.candles.length) return;
  const chartEl = $('chart');
  const width = chartEl.clientWidth;
  const height = chartEl.clientHeight;
  // правый край зон — граница области графика, без налезания на шкалу цен
  const paneRight = width - state.chart.priceScale('right').width();
  const ts = state.chart.timeScale();
  // Слой H1 готовится независимо от отбора и отрисовки HTF-контекста.
  drawH1Breaks(overlay, ts, paneRight, height);
  drawDailyStructure(overlay, ts, paneRight, height);
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
  // FVG: перекрытая движением часть не заливается, линии границ остаются
  // на всю длину. Глубина — max_test_depth от стороны входа (0..1).
  const appendPartialFill = (div, z, completed) => {
    if (z.type !== 'FVG' || completed || z.is_level) return;
    const depth = Math.max(0, Math.min(1, Number(z.max_test_depth) || 0));
    if (depth <= 0) return; // нетронутая зона заливается целиком
    div.classList.add('fvg-partial');
    const fill = document.createElement('span');
    fill.className = 'zone-fill';
    if (z.direction === 'bull') {
      fill.style.top = (depth * 100).toFixed(2) + '%';
      fill.style.bottom = '0';
    } else {
      fill.style.top = '0';
      fill.style.bottom = (depth * 100).toFixed(2) + '%';
    }
    div.appendChild(fill);
  };

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

  // §10: пересекающиеся ОДНОТИПНЫЕ зоны не накладываются друг на друга —
  // для визуальной группы рисуется ОДНА объединённая полоса (состав — в
  // бейдже и tooltip); разнотипные полосы обрезают друг друга (ниже).
  // Исходные зоны, границы, середины и правила уведомлений не меняются.
  const groupByZone = new Map();
  for (const g of state.groups) {
    for (const zid of g.zone_ids) groupByZone.set(zid, g);
  }

  // пул зон для отрисовки: статус + ТФ + переключатель кандидатов;
  // при переполнении — ближайшие к текущей цене (выбранная зона — всегда).
  // Если зона выбрана (клик) — рисуем ТОЛЬКО её.
  let pool;
  if (state.selectedZoneId) {
    const sel = state.zones.find((z) => z.id === state.selectedZoneId);
    pool = sel && tfMatch(sel) ? [sel] : [];
  } else {
    // ТЗ 07.10.2026 §3: актуальные зоны (relevant — canonical state с
    // сервера) рисуются СРАЗУ, без ожидания ручного ревью; статус candidate
    // скрывает только неподтверждённые объекты очереди проверки
    pool = state.zones.filter((z) => {
      if (!tfMatch(z)) return false;
      if (sweptLevelVisible(z)) return true;
      return CHART_STATUSES.has(z.status) &&
        (z.relevant || z.status !== 'candidate' || state.showCandidates);
    });
    if (pool.length > state.maxChartZones) {
      const price = state.lastPrice || 0;
      pool.sort((a, b) => distanceToZone(a, price) - distanceToZone(b, price));
      pool = pool.slice(0, state.maxChartZones);
    }
  }
  // HTF-контекст на H1 — отдельный слой. Галочки D1/W1 не снимаются.
  if (state.chartMode === 'h1' && window.H1Layers && !window.H1Layers.loadSettings().htfContext) {
    pool = [];
  }

  // Группы с 2+ участниками одного ТФ в пуле рисуются одной полосой.
  // D1 и W1 не смешиваются, даже если сервер когда-то отдал их одной группой.
  // При выбранной зоне (клик) показываем только её, без объединения.
  const nativeTf = state.chartMode === 'h1' ? 'H1' : state.timeframe;
  const foreignTf = (z) => z.timeframe !== nativeTf;
  const mergedGroups = new Map(); // `${group.id}:${tf}` -> [zones from pool]
  if (!state.selectedZoneId) {
    for (const z of pool) {
      // У каждого SSL/BSL свой экстремум и момент снятия.
      if (z.is_level) continue;
      const g = groupByZone.get(z.id);
      if (!g) continue;
      const key = `${g.id}:${z.timeframe}`;
      if (!mergedGroups.has(key)) mergedGroups.set(key, []);
      mergedGroups.get(key).push(z);
    }
    for (const [key, members] of mergedGroups) {
      if (members.length < 2) mergedGroups.delete(key);
    }
  }
  const mergedIds = new Set();
  for (const members of mergedGroups.values()) {
    for (const z of members) mergedIds.add(z.id);
  }

  // Разнотипные зоны не сливаются и не закрашивают друг друга: в месте
  // пересечения более широкая полоса обрезается более узкой (узкие «режут»
  // широкие). Каждая полоса рисуется оставшимися сегментами [lo, hi].
  // Уровни (SSL/BSL) — тонкие линии, в обрезке не участвуют.
  const subtractSegs = (segs, lo, hi) => {
    const out = [];
    for (const [a, b] of segs) {
      if (hi <= a || lo >= b) { out.push([a, b]); continue; }
      if (lo > a) out.push([a, Math.min(lo, b)]);
      if (hi < b) out.push([Math.max(hi, a), b]);
    }
    return out;
  };
  const clipItems = []; // {key, lower, upper, tf}
  for (const z of pool) {
    if (mergedIds.has(z.id) || z.is_level) continue;
    clipItems.push({ key: z.id, lower: z.lower, upper: z.upper, tf: z.timeframe });
  }
  for (const [key, members] of mergedGroups) {
    clipItems.push({
      key,
      tf: members[0].timeframe,
      lower: Math.min(...members.map((z) => z.lower)),
      upper: Math.max(...members.map((z) => z.upper)),
    });
  }
  // Обрезка только внутри одного ТФ: D1 не вырезает W1 и наоборот.
  // Внутри ТФ приоритет у более узкой полосы — она остаётся целой.
  const segsByKey = new Map();
  const byTf = new Map();
  for (const it of clipItems) {
    if (!byTf.has(it.tf)) byTf.set(it.tf, []);
    byTf.get(it.tf).push(it);
  }
  for (const items of byTf.values()) {
    items.sort((a, b) => (a.upper - a.lower) - (b.upper - b.lower));
    const taken = [];
    for (const it of items) {
      let segs = [[it.lower, it.upper]];
      for (const [lo, hi] of taken) segs = subtractSegs(segs, lo, hi);
      segsByKey.set(it.key, segs);
      taken.push([it.lower, it.upper]);
    }
  }

  for (const z of pool) {
    if (mergedIds.has(z.id)) continue; // входит в объединённую полосу ниже
    // §15.1.7: рисунок начинается от display_from (для FVG — средняя свеча
    // исходной тройки); formed_at — запасной вариант
    const fromMs = (z.is_level && z.extreme_at) || z.display_from || z.formed_at;
    let x1 = z.is_level ? barCoordinate(ts, fromMs)
      : ts.timeToCoordinate(Math.floor(fromMs / 1000));
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
    const keptLevel = sweptLevelVisible(z);
    let x2 = paneRight;
    if (completed) {
      const xc = barCoordinate(ts, z.display_until);
      if (xc !== null) x2 = Math.min(paneRight, xc);
      else if (z.is_level) continue; // снятие вне данных — линию вправо не тянем
    }
    if (x2 <= x1) continue; // завершилась левее видимой области
    const cls = `zone-rect z-${z.type} dir-${z.direction === 'bear' ? 'bear' : 'bull'}` +
      ` tf-${isHtf(z) ? 'htf' : 'ltf'} status-${z.status}` +
      (completed ? ' status-completed' : '') +
      (keptLevel ? ' level-kept' : '') +
      (foreignTf(z) ? ' tf-context' : '') +
      (state.selectedZoneId === z.id ? ' selected' : '');
    const labelText =
      `${z.type.toUpperCase()} ${z.timeframe} · ${STATUS_RU[z.status] || z.status}` +
      (z.name ? ` · ${z.name}` : '');
    // уровни рисуются целиком; полосы — сегментами после обрезки соседями
    const segs = z.is_level ? [[z.lower, z.upper]] : (segsByKey.get(z.id) || []);
    let labelPlaced = false;
    for (const [lo, hi] of segs) {
      const div = makeBand(hi, lo, cls);
      if (!div) continue;
      div.dataset.zoneId = z.id;
      appendPartialFill(div, z, completed);
      div.style.left = x1 + 'px';
      div.style.width = (z.is_level ? x2 - x1 : Math.max(8, x2 - x1)) + 'px';
      div.onclick = () => openZoneDetail(z.id);

      if (!z.is_level && z.mid >= lo && z.mid <= hi) {
        const midY = state.candleSeries.priceToCoordinate(z.mid);
        if (midY !== null && midY >= 0 && midY <= height) {
          const mid = document.createElement('div');
          mid.className = 'zone-mid';
          mid.style.top = (midY - parseFloat(div.style.top)) + 'px';
          div.appendChild(mid);
        }
      }

      // подписи только у некандидатов — иначе подписи кандидатов превращают
      // график в кашу; текст кандидата доступен в tooltip (div.title);
      // подпись — на первом отрисованном сегменте
      if (!labelPlaced && z.status !== 'candidate') {
        const label = document.createElement('span');
        label.className = 'zone-label';
        label.textContent = labelText;
        div.appendChild(label);
        labelPlaced = true;
      }
      div.title = labelText;
      overlay.appendChild(div);
    }
  }

  // §10: объединённые полосы визуальных групп (только однотипные зоны) —
  // вместо наложения зон друг на друга. Состав группы — в бейдже и tooltip;
  // клик открывает первую зону группы (исходные зоны доступны и в таблице
  // ниже). Границы — по видимым участникам; полоса тоже обрезается более
  // узкими разнотипными соседями (сегменты из segsByKey).
  for (const [key, members] of mergedGroups) {
    // начало полосы — самое раннее формирование участников
    let x1 = null;
    for (const z of members) {
      const fromMs = z.display_from || z.formed_at;
      let zx = ts.timeToCoordinate(Math.floor(fromMs / 1000));
      if (zx === null || zx < 0) zx = 0;
      if (x1 === null || zx < x1) x1 = zx;
    }
    if (x1 === null || x1 > paneRight) continue;
    const ordered = members.slice().sort((a, b) => a.id - b.id);
    const badgeText = `⧉ ${ordered.map((z) => `${z.type.toUpperCase()} ${z.timeframe}`).join(' + ')}`;
    const title = ordered.map((z) =>
      `${z.type.toUpperCase()} ${z.timeframe} · ${fmtPrice(z.lower)}–${fmtPrice(z.upper)}` +
      ` · ${STATUS_RU[z.status] || z.status}`).join('\n');
    const lead = ordered[0] || members[0] || {};
    const groupCls = 'zone-group' +
      (lead.direction === 'bear' ? ' dir-bear' : lead.direction === 'bull' ? ' dir-bull' : '') +
      ` tf-${isHtf(lead) ? 'htf' : 'ltf'}` +
      (members.every(foreignTf) ? ' tf-context' : '');
    let badgePlaced = false;
    for (const [lo, hi] of (segsByKey.get(key) || [])) {
      const div = makeBand(hi, lo, groupCls);
      if (!div) continue;
      div.style.left = x1 + 'px';
      div.style.width = Math.max(8, paneRight - x1) + 'px';
      if (!badgePlaced) {
        const badge = document.createElement('span');
        badge.className = 'zone-group-badge';
        badge.textContent = badgeText;
        div.appendChild(badge);
        badgePlaced = true;
      }
      div.title = title;
      div.onclick = () => openZoneDetail(ordered[0].id);
      overlay.appendChild(div);
    }
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

function hideH1Chrome() {
  ['h1-transition', 'h1-layer-status', 'h1-offscreen', 'h1-event-card'].forEach((id) => {
    const el = $(id);
    if (el) el.classList.add('hidden');
  });
}

function h1CardFormat() {
  return { time: fmtTime, price: fmtPrice };
}

function showH1Card(html) {
  const card = $('h1-event-card');
  if (!card) return;
  card.classList.remove('hidden');
  card.innerHTML = html + '<button type="button" class="btn small">Закрыть</button>';
  const button = card.querySelector('button');
  if (button) button.onclick = closeH1Card;
}

function closeH1Card() {
  const card = $('h1-event-card');
  if (!card || card.classList.contains('hidden')) return;
  card.classList.add('hidden'); card.innerHTML = '';
  state.h1SelectedEventId = null; state.h1SelectedZoneId = null;
  if (state.candleSeries && state.h1Layers && window.H1Layers) {
    state.candleSeries.setMarkers(state.h1Layers ? window.H1Layers.seriesMarkers(
      state.h1Layers, null, structurePointColor) : []);
  }
}

function h1PaneHeight(fullHeight) {
  let timeH = 0;
  try { timeH = state.chart.timeScale().height() || 0; } catch (e) { timeH = 0; }
  const pane = fullHeight - timeH;
  return pane > 20 ? pane : fullHeight;
}

function h1VisibleView(paneHeight) {
  const range = state.chart.timeScale().getVisibleRange();
  let priceMin = null;
  let priceMax = null;
  const top = state.candleSeries.coordinateToPrice(0);
  const bottom = state.candleSeries.coordinateToPrice(Math.max(0, paneHeight - 1));
  if (top != null && bottom != null && top !== bottom) {
    priceMin = Math.min(top, bottom);
    priceMax = Math.max(top, bottom);
  }
  return {
    timeFrom: range ? Math.floor(Number(range.from) * 1000) : null,
    timeTo: range ? Math.ceil(Number(range.to) * 1000) : null,
    priceMin,
    priceMax,
  };
}

function focusH1Zone(zone) {
  if (!state.chart || !zone || !state.candles.length) return;
  const fromMs = zone.display_from || zone.formed_at;
  if (fromMs == null) return;
  const origin = Math.floor(Number(fromMs) / 1000);
  const last = state.candles[state.candles.length - 1].time;
  const first = state.candles[0].time;
  state.h1SelectedZoneId = zone.id;
  state.h1PriceFocus = { lower: zone.lower, upper: zone.upper };
  state.h1UserPanned = true;
  if (state.h1Layers && state.h1Layers.setup) state.h1FrameKey = state.h1Layers.setup.movement_id;
  state.chart.timeScale().setVisibleRange({
    from: Math.max(first, origin - 48 * 3600),
    to: Math.min(last + 4 * 3600, Math.max(origin + 96 * 3600, first + 3600)),
  });
  try {
    state.chart.priceScale('right').applyOptions({ autoScale: true });
  } catch (e) { /* шкала ещё не готова */ }
}

function resetH1ZoneFilters() {
  if (!window.H1Layers) return;
  window.H1Layers.saveSettings({
    zones: true, ob: true, fvg: true, bsl: true, ssl: true,
    eligibleOnly: false, ideaId: '', candidates: false, historicalZones: false,
  });
  window.H1Layers.bindControls(onH1LayersChange, closeSelectedHtfIdea);
  state.h1Settings = window.H1Layers.loadSettings();
  drawZones();
}

function scheduleH1HistoryReload() {
  if (state.chartMode !== 'h1' || !window.H1Layers) return;
  if (window.H1Layers.loadSettings().points !== 'history') return;
  clearTimeout(state.h1HistoryTimer);
  state.h1HistoryTimer = setTimeout(() => {
    loadH1Markers().catch((e) => console.warn('h1 history:', e));
  }, 300);
}

function h1QueryExtra() {
  const extra = {};
  const ctx = deskData.current && deskData.current.selected_context_id;
  if (ctx) extra.context_id = ctx;
  const settings = window.H1Layers.loadSettings();
  if (settings.points === 'history' && state.chart) {
    const range = state.chart.timeScale().getVisibleRange();
    if (range && range.from != null && range.to != null) {
      extra.from = Math.floor(Number(range.from) * 1000);
      extra.to = Math.ceil(Number(range.to) * 1000);
    }
  }
  return extra;
}

function onH1LayersChange(settings) {
  const prev = state.h1Settings || window.H1Layers.loadSettings();
  state.h1Settings = settings;
  if (state.chartMode !== 'h1') return;
  const refetch = prev.points !== settings.points || !!prev.diagnostic !== !!settings.diagnostic
    || !!prev.historicalZones !== !!settings.historicalZones;
  if (refetch) {
    loadH1Markers().catch((e) => console.warn('h1 layers:', e));
    return;
  }
  if (state.candleSeries && state.h1Layers) {
    state.candleSeries.setMarkers(window.H1Layers.seriesMarkers(
      state.h1Layers, state.h1SelectedEventId, structurePointColor));
  }
  drawZones();
  renderDeskEntries();
}

async function closeSelectedHtfIdea(ideaId) {
  await api(`/api/ltf/ideas/${ideaId}/close`, { method: 'POST' });
  await loadH1Markers();
  await loadDeskExtras();
}

function drawDailyStructure(overlay, ts, paneRight, height) {
  const daily = state.dailyStructure;
  const mobile = window.matchMedia('(max-width: 767px)').matches;
  const mobileLabels = [];
  if (!$('layer-structure-d1')?.checked) return;
  const status = document.createElement('span');
  status.className = 'd1-structure-status';
  status.textContent = daily ? daily.text : 'Структура D1 не загружена · обновите данные';
  status.title = status.textContent;
  Object.assign(status.style, {position:'absolute', left:'8px', top:'8px',
    maxWidth:Math.max(100, paneRight - 24)+'px', padding:'4px 6px',
    color:daily?.data_quality === 'ok' ? 'var(--text, #263247)' : 'var(--warning, #9d6600)',
    background:'var(--bg, #fff)', border:'1px solid var(--border, #c6cfdd)',
    borderRadius:'4px', fontSize:'11px', overflow:'hidden', textOverflow:'ellipsis',
    whiteSpace:'nowrap', pointerEvents:'auto', zIndex:'20'});
  overlay.appendChild(status);
  if (!daily) return;
  const bars = state.candles;
  const xAt = (ms) => {
    const time = Math.floor(ms / 1000);
    if (!bars.length || time < bars[0].time) return null;
    let lo = 0, hi = bars.length - 1;
    while (lo < hi) {
      const mid = Math.ceil((lo + hi) / 2);
      if (bars[mid].time <= time) lo = mid; else hi = mid - 1;
    }
    // W1 maps each D1 anchor onto its containing weekly candle.
    return ts.timeToCoordinate(bars[lo].time);
  };
  const label = (x, y, text, role, title) => {
    const el = document.createElement('span');
    el.className = 'd1-structure-label ltf-pivot role-' + role;
    el.textContent = text; el.title = title;
    Object.assign(el.style, {position:'absolute', left:x + 'px',
      top:y + 'px', whiteSpace:'nowrap', zIndex:'15'});
    overlay.appendChild(el);
    el.style.left = Math.max(0, Math.min(x, paneRight - el.offsetWidth - 4)) + 'px';
    if (mobile) {
      mobileLabels.push(el);
    }
  };
  const visiblePoint = p => {
    const x = xAt(p.pivot_at);
    return x != null && x >= 0 && x <= paneRight;
  };
  (daily.pivots || []).filter(p => ['HH','HL','LH','LL'].includes(p.role) && visiblePoint(p)).slice(-20).forEach(p => {
    const x = xAt(p.pivot_at), y = state.candleSeries.priceToCoordinate(p.price);
    if (x == null || x < 0 || x > paneRight || y == null || y < 12 || y > height - 40) return;
    label(x - 5, y + (p.kind === 'high' ? -17 : 4), 'D1 ' + p.role, p.role,
      `D1 ${p.role} · ${fmtPrice(p.price)} · подтверждение ${fmtTime(p.confirmed_at)}`);
  });
  const line = (anchor, end, level, text, title, options = {}) => {
    const {kind = '', direction = 'bull', expected = false, color = null} = options;
    const x2 = end == null ? paneRight : xAt(end);
    const y = state.candleSeries.priceToCoordinate(level);
    if (x2 == null || x2 < 0 || y == null || y < 0 || y > height - 35) return;
    const x1 = xAt(anchor);
    const left = Math.max(0, x1 == null ? 0 : x1), right = Math.min(paneRight, x2);
    if (right < left) return;
    const el = document.createElement('div');
    el.className = 'd1-structure-line ' + (expected
      ? 'ltf-expected ltf-expected-' + kind.toLowerCase()
      : 'ltf-break ltf-' + kind.toLowerCase() + (direction === 'bull' ? ' ltf-up' : ' ltf-down'));
    Object.assign(el.style, {position:'absolute', left:left+'px', top:y+'px',
      width:(right-left)+'px', zIndex:'14'});
    if (color) { el.style.borderColor = color; el.style.color = color; }
    el.title = title;
    const caption = document.createElement('span');
    caption.className = 'd1-structure-label ' + (expected
      ? (direction === 'bull' ? 'lbl-above' : 'lbl-below') : 'h1-break-label');
    caption.textContent = text;
    caption.title = title;
    if (color) caption.style.color = color;
    el.appendChild(caption);
    overlay.appendChild(el);
    caption.style.left = Math.max(-left, Math.min(expected ? 2 : 0,
      paneRight - left - caption.offsetWidth - 4)) + 'px';
    if (mobile) mobileLabels.push(caption);
  };
  (daily.events || []).filter(e => {
    const end = xAt(e.open_time), start = xAt(e.anchor_at);
    return end != null && end >= 0 && (start == null || start <= paneRight);
  }).slice(-50).forEach(e => line(e.anchor_at, e.open_time, e.level,
    'D1 ' + e.kind + (e.direction === 'bull' ? ' ↑' : ' ↓'),
    `D1 ${e.kind} · ${fmtPrice(e.level)} · закрытие ${fmtTime(e.occurred_at)}`,
    {kind:e.kind, direction:e.direction}));
  if (daily.data_quality === 'ok') Object.entries(daily.expected || {}).forEach(([dir, conditions]) => {
    Object.values(conditions).filter(c => c.status === 'ready' && c.level != null).forEach(c => {
      line(c.pivot?.pivot_at || bars[0].time * 1000, null, c.level,
        'D1 ждём ' + c.kind + (dir === 'bull' ? ' ↑' : ' ↓'),
        `Условие ${c.kind} D1: закрытие дня строго ${dir === 'bull' ? 'выше' : 'ниже'} ${fmtPrice(c.level)}`,
        {kind:c.kind, direction:dir, expected:true});
    });
  });
  const current = typeof deskData === 'undefined' ? null : deskData.current;
  const sync = current?.instrument?.id != null && current.instrument.id === state.instrumentId
    ? current.asset?.trade_setup?.daily_alignment : null;
  const target = sync?.target;
  if (target) line(bars[0].time * 1000, null, target.price,
    'Цель · ' + target.type + ' D1', target.text, {expected:true, color:'#c88a35'});
  if (mobile) {
    // Current conditions and targets are drawn last: give them priority over
    // older event/pivot captions. Keep every level line at its actual price.
    const occupied = [status.getBoundingClientRect()];
    const bounds = overlay.getBoundingClientRect();
    for (const el of mobileLabels.reverse()) {
      const rect = el.getBoundingClientRect();
      const overlaps = occupied.some(other => rect.left < other.right + 4 &&
        rect.right + 4 > other.left && rect.top < other.bottom + 3 && rect.bottom + 3 > other.top);
      if (overlaps || rect.top < bounds.top || rect.bottom > bounds.top + height - 35) el.remove();
      else occupied.push(rect);
    }
  }
}

function drawH1Breaks(overlay, ts, paneRight, height) {
  if (state.chartMode !== 'h1' || !state.candleSeries || !window.H1Layers) {
    hideH1Chrome();
    return;
  }
  const layers = state.h1Layers;
  if (!layers) {
    hideH1Chrome();
    return;
  }
  const paneHeight = h1PaneHeight(height);
  window.H1Layers.draw(overlay, {
    layers,
    settings: window.H1Layers.loadSettings(),
    xOf: (ms) => barCoordinate(ts, ms),
    yOf: (price) => state.candleSeries.priceToCoordinate(price),
    paneRight,
    height: paneHeight,
    fmtPrice,
    fmtTime,
    view: h1VisibleView(paneHeight),
    selectedZoneId: state.h1SelectedZoneId,
    loadError: state.h1LoadError,
    transitionEl: $('h1-transition'),
    statusEl: $('h1-layer-status'),
    offscreenEl: $('h1-offscreen'),
    onZone: (zone, adm) => {
      state.h1SelectedZoneId = zone.id;
      drawZones();
      showH1Card(window.H1Layers.zoneCard(zone, adm, h1CardFormat()));
    },
    onEvent: (events) => {
      const first = events && events[0];
      state.h1SelectedEventId = first ? first.id : null;
      if (state.candleSeries) {
        state.candleSeries.setMarkers(window.H1Layers.seriesMarkers(
          layers, state.h1SelectedEventId, structurePointColor));
      }
      showH1Card((events || []).map((ev) => window.H1Layers.eventCard(ev, h1CardFormat())).join(''));
    },
    onShowZone: focusH1Zone,
    onRetry: () => loadH1Markers().catch((e) => console.warn('h1 retry:', e)),
    onResetFilters: resetH1ZoneFilters,
    onShowAllZones: () => {
      window.H1Layers.saveSettings({ eligibleOnly: false });
      const box = $('h1-eligible-only');
      if (box) box.checked = false;
      state.h1Settings = window.H1Layers.loadSettings();
      drawZones();
    },
  });
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
    // ТЗ 07.10.2026 §11: «Актуальные» — canonical relevant (подтверждённые,
    // непробитые, незавершённые), а не просто status=active.
    // Снятый BSL/SSL остаётся в этом списке 7 суток после снятия.
    zones = zones.filter((z) => z.relevant || sweptLevelVisible(z));
  } else if (bucket === 'candidate') {
    zones = zones.filter((z) => z.status === 'candidate' && !z.display_until);
  } else if (bucket === 'archive') {
    zones = zones.filter((z) =>
      !sweptLevelVisible(z) &&
      (z.display_until || ['rejected', 'archived', 'taken', 'converted'].includes(z.status)));
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

// Счётчики групп списка зон (кандидаты считаются очередью проверки отдельно)
function zoneBucketCounts() {
  const htf = state.zones.filter(isHtf);
  return {
    live: htf.filter((z) => z.relevant || sweptLevelVisible(z)).length,
    archive: htf.filter((z) =>
      !sweptLevelVisible(z) &&
      (z.display_until || ['rejected', 'archived', 'taken', 'converted'].includes(z.status))).length,
  };
}

// Мини-схема положения цены относительно зоны в карточке зоны
function zonePosBar(z) {
  const p = state.lastPrice;
  if (p == null || !(z.upper >= z.lower)) return '';
  const span = Math.max(z.upper - z.lower, Math.abs(z.upper) * 0.0005, 1e-12);
  const min = z.lower - span * 0.6;
  const max = z.upper + span * 0.6;
  const pct = (x) => Math.max(0, Math.min(100, ((x - min) / (max - min)) * 100));
  const bandLeft = pct(z.lower);
  const bandWidth = pct(z.upper) - bandLeft;
  const rel = priceRelation(z);
  return `<span class="zpos" aria-hidden="true" data-lo="${z.lower}" data-hi="${z.upper}">` +
    `<span class="zpos-band" style="left:${bandLeft.toFixed(1)}%;width:${Math.max(bandWidth, 1.5).toFixed(1)}%"></span>` +
    `<span class="zpos-price ${esc(rel)}" style="left:${pct(p).toFixed(1)}%" title="${esc(fmtPrice(p))}"></span>` +
    `</span>`;
}

// Живой сдвиг маркеров цены в карточках зон между полными рендерами списка
function updateZonePosBars() {
  const p = state.lastPrice;
  if (p == null) return;
  document.querySelectorAll('.zpos[data-lo]').forEach((bar) => {
    const lo = Number(bar.dataset.lo);
    const hi = Number(bar.dataset.hi);
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return;
    const span = Math.max(hi - lo, Math.abs(hi) * 0.0005, 1e-12);
    const min = lo - span * 0.6;
    const max = hi + span * 0.6;
    const pct = Math.max(0, Math.min(100, ((p - min) / (max - min)) * 100));
    const dot = bar.querySelector('.zpos-price');
    if (dot) dot.style.left = pct.toFixed(1) + '%';
  });
}

const ZONE_EMPTY_RU = {
  live: 'Актуальных зон по выбранным условиям нет. Список — подтверждённые и непробитые зоны D1 и W1.',
  candidate: 'Кандидатов по выбранным условиям нет. Кандидаты ждут вашей проверки в очереди.',
  archive: 'В архиве по выбранным условиям зон нет: отработанные, отклонённые и завершённые зоны.',
};

function renderZonesTable() {
  const tbody = $('zones-table') && $('zones-table').querySelector('tbody');
  const list = $('zone-list');
  try {
  const zones = filteredZones();
  const counts = zoneBucketCounts();
  if ($('rail-count-live')) $('rail-count-live').textContent = counts.live ? String(counts.live) : '';
  if ($('rail-count-archive')) $('rail-count-archive').textContent = counts.archive ? String(counts.archive) : '';
  renderPanelTabCount();
  if (list) {
    list.innerHTML = '';
    if (!zones.length) {
      list.innerHTML = `<div class="ltf-empty">${esc(ZONE_EMPTY_RU[state.zoneBucket] || 'По выбранным условиям зон нет.')}</div>`;
    }
    for (const z of zones) {
      const rel = priceRelation(z);
      const range = z.is_level ? fmtPrice(z.lower) : `${fmtPrice(z.lower)} — ${fmtPrice(z.upper)}`;
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'zone-card' + (z.id === state.selectedZoneId ? ' selected' : '');
      card.innerHTML =
        `<span class="zc-top"><span class="swatch ${safeSwatchClass(z)}"></span>` +
        `<span class="zc-type">${z.type.toUpperCase()} · ${z.timeframe}</span>` +
        `<span class="dir ${z.direction}" title="${z.direction === 'bear' ? 'Медвежья зона' : 'Бычья зона'}">${z.direction === 'bear' ? '↓' : '↑'}</span>` +
        `<span class="zc-status">${esc(STATUS_RU[z.status] || z.status)}${z.display_until ? ' · завершена' : ''}</span></span>` +
        `<span class="zc-range">${range}</span>` +
        zonePosBar(z) +
        `<span class="zc-bottom"><span class="role">${esc(zoneRole(z))}</span>` +
        (rel ? `<span class="rel-chip ${rel}">${esc(relationLabel(rel))}</span>` : '') +
        `</span>`;
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
  if ($('zones-count')) $('zones-count').textContent = zones.length;
  } catch (err) {
    const id = 'LF-UI-' + Date.now().toString(36);
    console.warn(id, err);
    if (list) {
      list.innerHTML = `<div class="ltf-empty">Список зон не собрался. Повторите загрузку. Код ${esc(id)}</div>` +
        '<button type="button" class="btn" id="zones-retry">Повторить</button>';
      const retry = document.getElementById('zones-retry');
      if (retry) retry.onclick = () => renderZonesTable();
    }
  }
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
    // ТЗ 07.10.2026 §11/§13: счётчики тех же выборок, что и таблица/график
    ['Актуальные', by((z) => z.relevant || sweptLevelVisible(z))],
    ['Кандидаты (неподтверждённые)', by((z) =>
      z.status === 'candidate' && !z.display_until && !z.relevant)],
    ['Цена внутри', inside],
    ['SSL/BSL активные', `${ssl}/${bsl}`],
    ['Завершённые', zones.length - live.length],
    ['Всего', zones.length],
  ];
  el.innerHTML = items.map(([k, v]) =>
    `<span class="hs-item">${k}: <b>${v}</b></span>`).join('');
}

function updateLastPrice(live = false) {
  if ($('last-price')) $('last-price').textContent = fmtPrice(state.lastPrice);
  if ($('desk-price') && state.lastPrice != null) {
    $('desk-price').textContent = fmtPrice(state.lastPrice);
  }
  const ins = state.instruments.find((i) => i.id === state.instrumentId);
  if ($('instrument-meta') && ins) {
    $('instrument-meta').textContent = `${ins.venue} · ${ins.market_type || 'spot'}`;
  }
  updateZonePosBars();
  if (live && window.LFCopy) LFCopy.updatePrice($('desk-scenario'), state.lastPrice);
}

// ---------------------------------------------------------------------------
// Панель деталей зоны
// ---------------------------------------------------------------------------

const TF_SECONDS = { H1: 3600, H4: 14400, D1: 86400, W1: 604800 };

// Прокручивает график к зоне: при необходимости переключает ТФ и ставит
// видимый диапазон от начала формирования зоны до текущего момента.
async function focusZoneOnChart(z) {
  const tfOptionExists = [...$('tf-select').options].some((o) => o.value === z.timeframe);
  if (state.chartMode !== 'h1' && z.timeframe !== state.timeframe && TF_SECONDS[z.timeframe] && tfOptionExists) {
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
function askBounds(lower, upper, anchorEnabled, draft = null) {
  $('bounds-title').textContent = anchorEnabled ? 'Исправить границы зоны' : 'Исправить кандидата';
  boundsOriginal = { lower, upper };
  $('bounds-lower').value = draft ? draft.lower : lower;
  $('bounds-upper').value = draft ? draft.upper : upper;
  setBoundsAnchor(draft?.anchor_candle_open_time ?? null);
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
  // инспектор живёт во вкладке рабочего места (desk) — переключаемся на неё,
  // иначе клик из «Проверки» или «Журнала» открывал бы детали в скрытой вкладке
  showView('desk');
  // выбор зоны на графике или в списке раскрывает вкладку «Зоны»
  setDeskPanelTab('zones');
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
        ? `<a class="btn small" href="/ltf.html?zone_id=${z.id}&instrument=${state.instrumentId}">Открыть LTF →</a>`
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
  setDeskPanelTab('zones');
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
  const id = state.instrumentId;
  const req = ++state.eventReq;
  // §13 (этап 6b): opt-in конверт {events, versions} — версии зон/истории
  // того же снимка
  const assetQ = state.instrumentId
    ? `/api/events?limit=50&instrument_id=${state.instrumentId}&with_version=true`
    : '/api/events?limit=50&with_version=true';
  const [assetRes, allRes] = await Promise.all([
    api(assetQ),
    api('/api/events?limit=50&with_version=true'),
  ]);
  if (id !== state.instrumentId || req !== state.eventReq) return;
  const unwrap = (r) => (Array.isArray(r) ? r : (r && r.events) || []);
  const versions = (!Array.isArray(assetRes) && assetRes && assetRes.versions)
    || (!Array.isArray(allRes) && allRes && allRes.versions) || null;
  if (versions) domainSync.adoptAll(HTF.DomainSync.statusEntries(versions));
  renderEvents(unwrap(assetRes), unwrap(allRes));
}

function renderEvents(assetEvents, allEvents) {
  const shown = (rows) => (rows || []).filter((e) => e.kind !== 'approach');
  assetEvents = shown(assetEvents);
  allEvents = shown(allEvents);
  const journal = allEvents || assetEvents;
  const ul = $('events-list');
  const label = $('events-strip-label');
  if (label) label.textContent = 'События актива';
  if (ul) {
    ul.innerHTML = '';
    (assetEvents || []).slice(0, 3).forEach((e) => ul.appendChild(eventLi(e)));
  }
  const full = $('events-full');
  if (full) {
    full.innerHTML = '';
    journal.forEach((e) => full.appendChild(eventLi(e)));
  }
  if ($('events-count')) $('events-count').textContent = journal.length;
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
    `<b>${esc(displayPair(sym))}</b> ${ztype}: ${EVENT_KIND_RU[e.kind] || e.kind} @ ${fmtPrice(e.price)}` +
    (e.delayed ? ' <span class="badge">восстановлено</span>' : '');
  if (e.zone) li.onclick = () => openZoneDetail(e.zone.id);
  li.style.cursor = 'pointer';
  return li;
}

// ---------------------------------------------------------------------------
// Кандидаты (§10): Подтвердить / Исправить / Отклонить
// ---------------------------------------------------------------------------

function explanationText(ev) {
  if (!ev) return '';
  if (typeof ev === 'string') return ev;
  if (typeof ev !== 'object') return '';
  const rule = ev.rule ? String(ev.rule) : '';
  const reason = typeof ev.reason === 'string' ? ev.reason : '';
  const notes = [];
  if (ev.external_fvg === false) notes.push('внешний FVG не найден');
  if (ev.phase === 'departed') notes.push('цена вышла из базы до подтверждения');
  if (ev.base_search_limited) notes.push('поиск базы упёрся в технический лимит');
  const head = rule || reason;
  const tail = notes
    .filter((note) => !head.toLowerCase().includes(note.toLowerCase()))
    .map((note) => note.charAt(0).toUpperCase() + note.slice(1))
    .join('. ');
  if (head && tail) return `${head}. ${tail}.`;
  if (head) return head;
  return tail ? `${tail}.` : '';
}

const UNCONFIRMED_RU = {
  timeline_violation: 'Нарушена хронология',
  data_incomplete: 'Не хватает свечей',
  no_external_fvg: 'Нет внешнего FVG',
};

function displayPair(symbol) {
  const s = symbol || '';
  if (s.endsWith('USDT') && s.length > 4) return s.slice(0, -4) + ' / USDT';
  return s || '—';
}

async function loadCandidates() {
  const id = state.instrumentId;
  const req = ++state.candidateReq;
  const list = (await api('/api/candidates')).filter(isHtf); // только HTF
  if (id !== state.instrumentId || req !== state.candidateReq) return;
  reviewState.all = list;
  const mine = state.instrumentId
    ? list.filter((z) => z.instrument_id === state.instrumentId)
    : list;
  if ($('candidates-count')) $('candidates-count').textContent = mine.length;
  const note = $('candidates-global-note');
  if (note) note.textContent = `По всем активам: ${list.length}`;
  renderReviewQueue();
}

// Выгрузка данных проверок: /api/export/reviews закрыт Bearer-токеном,
// поэтому качаем fetch'ем в blob, а не ссылкой. Ошибку показываем текстом
// на самой кнопке (отдельного статус-элемента в шапке очереди нет).
async function downloadLabels() {
  const btn = $('btn-download-labels');
  const label = btn.textContent;
  const fail = (msg) => {
    btn.textContent = msg;
    setTimeout(() => { btn.textContent = label; }, 3000);
  };
  const doFetch = () => fetch('/api/export/reviews', {
    headers: { 'Authorization': 'Bearer ' + HTF.getToken() },
  });
  let resp = await doFetch();
  if (resp.status === 401) {
    localStorage.removeItem('htf_token');
    await HTF.ensureToken('Токен не подошёл. Проверьте значение и попробуйте снова.');
    resp = await doFetch();
  }
  if (!resp.ok) {
    let detail = 'Ошибка выгрузки';
    try { detail = (await resp.json()).detail || detail; } catch (e) { /* не JSON */ }
    fail(detail);
    return;
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = 'reviews.json';
  a.click();
  URL.revokeObjectURL(url);
}

// Очередь с учётом фильтра ТФ и решений этой сессии. Сервер уже не отдаёт
// проверенных кандидатов (у них есть запись в review); doneMap лишь прячет
// только что проверенные до прихода свежего списка после loadCandidates().
function visibleCandidates() {
  return reviewState.all.filter((c) =>
    !reviewState.doneMap.has(c.id) &&
    (!reviewState.tfFilter || c.timeframe === reviewState.tfFilter));
}

function pluralTasks(n) {
  const m10 = n % 10, m100 = n % 100;
  if (m10 === 1 && m100 !== 11) return 'задача';
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return 'задачи';
  return 'задач';
}

function reviewLayoutIdle(on) {
  const layout = document.querySelector('#view-review .review-layout');
  if (layout) layout.classList.toggle('review-idle', !!on);
}

function renderReviewQueue() {
  const box = $('candidates-list');
  const visible = visibleCandidates();
  const done = [...reviewState.doneMap.values()]
    .filter((c) => !reviewState.tfFilter || c.timeframe === reviewState.tfFilter).length;
  if (!visible.length) {
    renderReviewEmpty();
    return;
  }
  reviewLayoutIdle(false);
  $('review-progress').textContent = `Осталось ${visible.length} · проверено ${done} за сессию`;
  const countEl = $('review-count');
  if (countEl) {
    countEl.innerHTML = `<span class="state-dot dot-warning"></span>` +
      `${visible.length} ${pluralTasks(visible.length)}`;
  }
  box.innerHTML = '';
  for (const c of visible) {
    const card = document.createElement('div');
    card.className = 'candidate-card' + (c.id === reviewState.currentId ? ' selected' : '');
    card.dataset.zoneId = String(c.id);
    const why = explanationText(c.explanation);
    const reason = UNCONFIRMED_RU[c.unconfirmed_reason] || c.unconfirmed_reason || 'Нужна проверка границ';
    card.title = [reason, why].filter(Boolean).join('\n');
    card.innerHTML = `
      <div class="cand-title">${c.instrument ? esc(displayPair(c.instrument.symbol)) : ''} · ${c.timeframe}</div>
      <div class="cand-type">${esc(TYPE_RU[c.type] || c.type.toUpperCase())} ${c.direction === 'bull' ? '▲ Рост' : '▼ Снижение'}</div>
      <div class="cand-range">${fmtPrice(c.lower)} – ${fmtPrice(c.upper)}</div>
      <p class="cand-explain">${esc(reason)}</p>
      ${why && why !== reason ? `<p class="cand-explain cand-detail">${esc(why)}</p>` : ''}`;
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

// Пустая очередь проверки (§7): состояние + возврат к наблюдению
function renderReviewEmpty() {
  reviewState.currentId = null;
  reviewState.currentCand = null;
  reviewLayoutIdle(true);
  const all = reviewState.all || [];
  const filter = reviewState.tfFilter;
  const pending = all.filter((c) => !reviewState.doneMap.has(c.id));
  const filteredOut = !!filter && pending.length > 0;
  const sessionDone = reviewState.doneMap.size;
  const insp = $('review-inspector');
  const progress = $('review-progress');
  if (progress) {
    progress.textContent = sessionDone
      ? `Осталось 0 · проверено ${sessionDone} за сессию`
      : 'Осталось 0';
  }
  const countEl = $('review-count');
  if (countEl) {
    countEl.innerHTML = '<span class="state-dot dot-positive"></span>0 задач';
  }
  let title;
  let body;
  let actions;
  if (filteredOut) {
    title = 'Нет зон для проверки по выбранным фильтрам';
    body = 'Ноль задач по фильтру не означает, что проверена вся база.';
    actions = '<button type="button" class="btn" id="rv-reset-filter">Сбросить фильтры</button>' +
      '<button type="button" class="btn" id="rv-back-desk">Вернуться к анализу</button>';
  } else {
    title = 'Все доступные зоны проверены';
    body = sessionDone
      ? `Проверено ${sessionDone} за сессию. Ноль доступных задач не означает, что проверена вся база.`
      : 'Новые задачи появятся после сканирования. Ноль доступных задач не означает, что проверена вся база.';
    actions = '<button type="button" class="btn" id="rv-back-desk">Вернуться к анализу</button>';
  }
  insp.innerHTML =
    '<div class="rv-task-over">Контроль разметки</div>' +
    `<h3>${title}</h3>` +
    `<p class="muted">${body}</p>` +
    actions;
  const reset = $('rv-reset-filter');
  if (reset) {
    reset.onclick = () => {
      reviewState.tfFilter = '';
      const sel = $('review-tf-filter');
      if (sel) sel.value = '';
      renderReviewQueue();
    };
  }
  const back = $('rv-back-desk');
  if (back) back.onclick = () => showView('desk');
}

// Вход во вкладку «Проверка»: график при первом входе, очередь, автовыбор
// первого кандидата, если текущий не выбран или уже выпал из очереди.
async function enterReviewMode() {
  try {
    if (!reviewState.chart) initReviewChart();
    await loadCandidates();
  } catch (err) {
    console.warn('review mode:', err);
    reviewLayoutIdle(true);
    const insp = $('review-inspector');
    if (insp) {
      insp.innerHTML = '<div class="rv-task-over">Контроль разметки</div>' +
        '<h3>Очередь не загрузилась</h3>' +
        '<p class="muted">Показан последний экран. Повторите загрузку.</p>' +
        '<button type="button" class="btn" id="rv-retry">Повторить</button>' +
        '<button type="button" class="btn" id="rv-back-desk">Вернуться к анализу</button>';
      const retry = $('rv-retry');
      if (retry) retry.onclick = () => enterReviewMode();
      const back = $('rv-back-desk');
      if (back) back.onclick = () => showView('desk');
    }
    return;
  }
  const visible = visibleCandidates();
  if (!visible.length) {
    renderReviewEmpty();
    return;
  }
  reviewLayoutIdle(false);
  if (!visible.some((c) => c.id === reviewState.currentId)) {
    openReviewCandidate(visible[0]);
  }
}

// Журнал → решение HTF: открыть задачу в очереди, если она ещё там.
async function openReviewForZone(zoneId) {
  showView('review');
  await enterReviewMode();
  const cand = reviewState.all.find(
    (c) => c.id === zoneId && !reviewState.doneMap.has(c.id));
  if (!cand) return false;
  await openReviewCandidate(cand);
  return true;
}
window.LFReview = { openZone: openReviewForZone };

function syncThemeChoices() {
  const cur = HTF.theme.get();
  document.querySelectorAll('[data-theme-choice]').forEach((btn) => {
    const on = btn.dataset.themeChoice === cur;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}

function initAppearance() {
  document.querySelectorAll('[data-theme-choice]').forEach((btn) => {
    btn.onclick = () => {
      HTF.theme.set(btn.dataset.themeChoice);
      syncThemeChoices();
    };
  });
  syncThemeChoices();
}

function paintCharts() {
  const theme = HTF.chartTheme();
  const opts = {
    layout: { background: { color: theme.background }, textColor: theme.text },
    grid: {
      vertLines: { color: theme.grid },
      horzLines: { color: theme.grid },
    },
    rightPriceScale: { borderColor: theme.border },
  };
  const candle = {
    upColor: theme.up, downColor: theme.down,
    wickUpColor: theme.up, wickDownColor: theme.down,
  };
  if (state.chart) {
    state.chart.applyOptions(opts);
    if (state.candleSeries) state.candleSeries.applyOptions(candle);
    requestAnimationFrame(drawZones);
    if (state.chartMode === 'h1') {
      loadH1Markers().catch((e) => console.warn('h1 markers:', e));
    }
  }
  if (reviewState.chart) {
    reviewState.chart.applyOptions(opts);
    if (reviewState.candleSeries) reviewState.candleSeries.applyOptions(candle);
    requestAnimationFrame(drawReviewZone);
  }
}
window.addEventListener('lf-theme', paintCharts);

// Клик по кандидату: НЕ переключаем вкладку — грузим его инструмент/ТФ,
// свечи вокруг формирования зоны и открываем инспектор здесь же.
async function openReviewCandidate(c) {
  if (reviewState.advanceTimer) {
    clearTimeout(reviewState.advanceTimer);
    reviewState.advanceTimer = null;
  }
  reviewState.currentId = c.id;
  reviewState.currentCand = c;
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
  const fromMs = (z.is_level && z.extreme_at) || z.display_from || z.formed_at;
  let x1 = z.is_level
    ? barCoordinate(ts, fromMs, reviewState.candles, z.timeframe)
    : ts.timeToCoordinate(Math.floor(fromMs / 1000));
  if (x1 === null || x1 < 0) x1 = 0;
  if (x1 > paneRight) return;
  let x2 = paneRight;
  if (z.display_until) {
    const xc = barCoordinate(ts, z.display_until, reviewState.candles, z.timeframe);
    if (xc !== null) x2 = Math.min(paneRight, xc);
    else if (z.is_level) return;
  }
  if (x2 <= x1) return;
  const div = document.createElement('div');
  div.className = `zone-rect z-${z.type} dir-${z.direction === 'bear' ? 'bear' : 'bull'}` +
    ` tf-${isHtf(z) ? 'htf' : 'ltf'} status-${z.status}` +
    (z.display_until ? ' status-completed' : '') + ' selected';
  div.style.top = top + 'px';
  div.style.height = Math.max(2, bottom - top) + 'px';
  div.style.left = x1 + 'px';
  div.style.width = (z.is_level ? x2 - x1 : Math.max(8, x2 - x1)) + 'px';
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

// Инспектор кандидата внутри вкладки «Проверка» (этап 5 ребрендинга, макет
// §6.C): задача, правило и решение в одном рабочем контексте. Решения — те
// же коды ReviewIn через submitReviewDecision; рыночная актуальность —
// отдельной справкой и не смешивается с решением о геометрии.
const REVIEW_RULE_FALLBACK =
  'Граница должна совпадать с экстремумом свечи основания. ' +
  'Сравните выделенную область со свечами формирования.';

function renderReviewInspector(detail) {
  const z = detail.zone;
  const el = $('review-inspector');
  const ins = detail.instrument;
  const assessments = detail.assessments || [];
  const lastA = assessments[assessments.length - 1];
  const cand = reviewState.currentCand || {};
  const rule = explanationText(cand.explanation) || REVIEW_RULE_FALLBACK;
  const range = z.is_level ? fmtPrice(z.lower) : `${fmtPrice(z.lower)} — ${fmtPrice(z.upper)}`;
  const market = (STATUS_RU[z.status] || z.status) +
    (z.display_until ? ' · завершена' + (z.end_reason ? ' (' + esc(z.end_reason) + ')' : '') : ' · живая');
  el.innerHTML = `
    <div class="rv-task-over">Задача #${z.id}</div>
    <h3>Проверьте границы зоны</h3>
    <p class="rv-rule">${esc(rule)}</p>
    <dl class="rv-facts">
      <dt>Объект</dt><dd>${esc(TYPE_RU[z.type] || z.type.toUpperCase())} · ${z.timeframe}${z.name ? ' · ' + esc(z.name) : ''}</dd>
      <dt>Текущие границы, USDT</dt><dd>${range}</dd>
      <dt>Рыночное состояние</dt><dd><span class="badge ${z.status}">${STATUS_RU[z.status] || z.status}</span> ${z.display_until ? 'завершена' : 'живая'}</dd>
      <dt>Инструмент</dt><dd>${ins ? esc(displayPair(ins.symbol)) + ' · ' + esc(ins.venue) : '—'}</dd>
    </dl>
    <div class="rv-actions-main">
      <button class="btn primary" type="button" data-review="correct">Подтвердить</button>
      <button class="btn danger" type="button" data-review="wrong_type">Отклонить</button>
      <button class="btn" type="button" id="rv-open-desk">Открыть актив</button>
    </div>
    <div class="rv-actions-secondary">
      <textarea id="rv-comment" rows="2" placeholder="Комментарий к решению"></textarea>
      <div class="review-actions">
        <button class="btn" type="button" data-review="fix_boundaries">Исправить границы</button>
        <details data-section="other-reviews"><summary class="btn">Другие решения</summary><div class="review-actions">
          <button class="btn" type="button" data-review="wrong_base">Другое основание</button>
          <button class="btn" type="button" data-review="now_irrelevant">Сейчас неактуально</button>
          <button class="btn" type="button" data-review="already_breaker">Уже Breaker</button>
          <button class="btn" type="button" data-review="no_context">Нет контекста</button>
        </div></details>
      </div>
      <div id="rv-verdict" class="review-verdict"></div>
    </div>
    <div class="rv-status">
      <h4>Актуальность зоны</h4>
      <p>Статус: ${esc(market)}</p>
      ${lastA
        ? `<p>Последняя оценка (${fmtTime(lastA.reviewed_at)}): геометрия — ${VERDICT_GEOM_RU[lastA.geometry_verdict] || lastA.geometry_verdict}` +
          `, цикл — ${lastA.lifecycle_verdict ? (VERDICT_LIFE_RU[lastA.lifecycle_verdict] || lastA.lifecycle_verdict) : '—'}` +
          `${lastA.requires_clarification ? ' · требует уточнения' : ''}</p>`
        : '<p>Оценок пока нет.</p>'}
      <p class="muted">Это справка об актуальности — решение выше только о геометрии разметки.</p>
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
  $('rv-open-desk').onclick = () => window.LFDesk.openInstrument(z.instrument_id);
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
    const bounds = await askBounds(z.lower, z.upper, true, reviewState.boundsDrafts.get(z.id));
    if (!bounds) return;
    reviewState.boundsDrafts.set(z.id,bounds);
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
    const diagnostic = LFUI.error(err,'Не удалось сохранить проверку',() => submitReviewDecision(decision));
    if (vEl) vEl.textContent = `Не удалось сохранить. Кандидат и введённые границы сохранены. ID ${diagnostic}`;
    return;
  }
  reviewState.saving = false;
  reviewState.boundsDrafts.delete(z.id);
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
    reviewState.zone = null;
    reviewState.candles = [];
    if (reviewState.candleSeries) reviewState.candleSeries.setData([]);
    drawReviewZone();
    renderReviewQueue();
    renderReviewEmpty();
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
    const anchorMs = HTF.parseMskLocal(anchorRaw); // datetime-local → московское время (ТЗ §6)
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
    title: 'Таймфреймы поиска',
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
    desc: '0.02 = 2%. Факт пишется, когда цена подошла к зоне ближе этого расстояния (§9). ' +
          'Уведомление по приближению не отправляется. Внутри зоны расстояние считается нулём.',
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
  notification_digest_seconds: {
    title: 'Интервал тихой сводки (сек)',
    desc: '900 секунд = 15 минут. Приближения, промежуточная глубина и состояние источников ' +
          'объединяются в сводку без звука. Касания, подтверждения и отмены отправляются сразу.',
    group: 'Уведомления',
  },
  notify_only_reviewed: {
    title: 'Уведомлять только о подтверждённых',
    desc: 'Если включено — автоматически найденные зоны молчат, пока вы не нажмёте «Подтвердить» ' +
          'в списке кандидатов (§10).',
    group: 'Уведомления',
  },
  ltf_notify_kinds: {
    title: 'События структуры H1',
    desc: 'Какие события H1 уходят в уведомления. Касания, подтверждения и отмены ' +
          'отправляются сразу, остальное — в тихой сводке.',
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
  entry_reuse_max_depth: {
    title: 'Предел повторного использования OB (доля)',
    desc: 'Повторный выбор OB как Entry Zone допустим, пока максимальная глубина ' +
          'самостоятельных тестов строго меньше порога. Максимум берётся за всю историю ' +
          'действующего OB: мелкий поздний тест не стирает более глубокий прежний.',
    group: 'Касания и глубина',
  },
  inner_level_pivot_left: {
    title: 'Внутренний уровень: свечей слева',
    desc: 'Подтверждение внутренних BSL/SSL — общее правило пивота на таймфрейме уровня.',
    group: 'Касания и глубина',
  },
  inner_level_pivot_right: {
    title: 'Внутренний уровень: свечей справа',
    desc: 'Внутренний экстремум подтверждён после закрытия N-й свечи справа.',
    group: 'Касания и глубина',
  },
  ltf_enabled: {
    title: 'LTF-мониторинг (H1)',
    desc: 'Наблюдение структуры H1 и Entry Zones. Выключение останавливает LTF-расчёт ' +
          'и не затрагивает зоны D1/W1.',
    group: 'LTF-наблюдения (H1)',
  },
  ltf_structure_left: {
    title: 'Структурные pivots слева (3–5)',
    desc: 'Сколько свечей слева должен превосходить структурный экстремум H1.',
    group: 'LTF-наблюдения (H1)',
  },
  ltf_structure_right: {
    title: 'Структурные pivots справа (3–5)',
    desc: 'Сколько свечей справа подтверждают структурный экстремум H1.',
    group: 'LTF-наблюдения (H1)',
  },
  ltf_entry_types: {
    title: 'Типы Entry Zones',
    desc: 'Применяется к расчёту пригодности, графику, счётчикам и новым уведомлениям. ' +
          'Отключение типа не удаляет уже найденные зоны и историю.',
    group: 'LTF-наблюдения (H1)',
  },
  ltf_history_days: {
    title: 'Глубина H1-истории (дней)',
    desc: 'Сколько H1-истории загружается до касания HTF-зоны при старте наблюдения.',
    group: 'LTF-наблюдения (H1)',
  },
  ltf_poll_seconds: {
    title: 'Цикл опроса H1 (сек)',
    desc: 'Как часто LTF-движок проверяет новые закрытия H1.',
    group: 'LTF-наблюдения (H1)',
  },
  ltf_live_grace_seconds: {
    title: 'Окно «live» для H1-событий (сек)',
    desc: 'Событие, обнаруженное в пределах окна после закрытия свечи, доставляется сразу. ' +
          'Позже — как догоняющий бэклог: доставка подавляется, как у replay.',
    group: 'LTF-наблюдения (H1)',
  },
  ltf_observation_stale_days: {
    title: 'Автоархивация наблюдений (дней)',
    desc: 'Наблюдение без активности дольше срока уходит в архив. 0 — выключено.',
    group: 'LTF-наблюдения (H1)',
  },
  ltf_range_anchor_policy: {
    title: 'Якоря диапазона',
    desc: 'От чего строить диапазон, пока нет подтверждённой пары продолжения ' +
          '(LH→LL / HL→HH) после первичного слома.',
    group: 'LTF-наблюдения (H1)',
  },
  ltf_provisional_range_enabled: {
    title: 'Предварительный диапазон',
    desc: 'Предлагаемый режим: пунктирный слой временного конца текущего движения до ' +
          'подтверждения опор. Только отображение — на подтверждённые диапазоны, отбор ' +
          'и уведомления не влияет.',
    group: 'LTF-наблюдения (H1)',
  },
  htf_context_types: {
    title: 'Источники HTF-контекста',
    desc: 'Какие зоны D1/W1 запускают LTF-наблюдение. Для уровней направление ожидания ' +
          'фиксировано (BSL — снятие вверх, SSL — снятие вниз), контакт — касание уровня.',
    group: 'HTF-контекст для LTF',
  },
  htf_context_wait_hours: {
    title: 'Ожидание подтверждения H1 (часов)',
    desc: 'Сколько часов эпизод контекста хранится от последнего отдельного взаимодействия ' +
          'до подтверждения H1. Повтор того же события срок не продлевает.',
    group: 'HTF-контекст для LTF',
  },
  htf_context_wait_hours_d1: {
    title: '…отдельно для D1 (часов)',
    desc: 'Переопределение общего срока для D1. 0 — использовать общий срок.',
    group: 'HTF-контекст для LTF',
  },
  htf_context_wait_hours_w1: {
    title: '…отдельно для W1 (часов)',
    desc: 'Переопределение общего срока для W1. 0 — использовать общий срок.',
    group: 'HTF-контекст для LTF',
  },
  htf_context_horizon_days: {
    title: 'Горизонт торгового контекста (дней)',
    desc: 'Сколько дней от глобальной опоры ноги сценарий остаётся торговым контекстом. ' +
          'Продлевается, пока нет квалифицированного обратного слома.',
    group: 'HTF-контекст для LTF',
  },
  htf_context_leg_anchor: {
    title: 'Якорь отсчёта ноги',
    desc: 'От какого момента отсчитывается горизонт контекста: последний обновлённый ' +
          'экстремум ноги или начало ноги (исходная опора).',
    group: 'HTF-контекст для LTF',
  },
  stale_quote_seconds: {
    title: 'Порог свежести котировок (сек)',
    desc: 'Котировка старше срока помечается устаревшей. 0 — автоматически: два интервала опроса.',
    group: 'Свежесть данных',
  },
  stale_h1_intervals: {
    title: 'Свежесть H1 (интервалов)',
    desc: 'Свечи H1 считаются устаревшими после N интервалов ТФ без обновления.',
    group: 'Свежесть данных',
  },
  stale_d1_intervals: {
    title: 'Свежесть D1 (интервалов)',
    desc: 'Свечи D1 считаются устаревшими после N интервалов ТФ без обновления.',
    group: 'Свежесть данных',
  },
  stale_w1_intervals: {
    title: 'Свежесть W1 (интервалов)',
    desc: 'Свечи W1 считаются устаревшими после N интервалов ТФ без обновления.',
    group: 'Свежесть данных',
  },
  uncalibrated_breakout_fvg_back_candles: {
    title: 'FVG пробоя: отступ назад (свечей)',
    desc: 'FVG привязывается к пробойному движению не раньше, чем за N свечей до пробоя.',
    group: 'Не калибровано',
  },
  uncalibrated_breakout_fvg_delay_candles: {
    title: 'FVG пробоя: предел задержки (свечей)',
    desc: 'На сколько свечей позже закрытия пробоя FVG ещё может подтвердиться.',
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

// Подразделы «Правил анализа»: порядок отображения и ключи каждой группы.
// htf_context_types и ltf_provisional_range_enabled сервер кладёт в
// «experimental», но они не uncalibrated — показываем тематически здесь.
const RULES_SUBSECTIONS = [
  { title: 'Поиск зон (HTF)', keys: [
    'lookback_days', 'lookback_days_d1', 'lookback_days_w1', 'scan_timeframes',
    'pivot_left', 'pivot_right', 'cluster_tolerance_pct'] },
  { title: 'Касания и глубина', keys: [
    'approach_pct', 'depth_mid', 'depth_worked', 'entry_reuse_max_depth',
    'inner_level_pivot_left', 'inner_level_pivot_right'] },
  { title: 'LTF-наблюдения (H1)', keys: [
    'ltf_enabled', 'ltf_provisional_range_enabled', 'ltf_structure_left',
    'ltf_structure_right', 'ltf_entry_types', 'ltf_history_days',
    'ltf_poll_seconds', 'ltf_live_grace_seconds', 'ltf_observation_stale_days',
    'ltf_range_anchor_policy'] },
  { title: 'HTF-контекст для LTF', keys: [
    'htf_context_types', 'htf_context_wait_hours', 'htf_context_wait_hours_d1',
    'htf_context_wait_hours_w1', 'htf_context_horizon_days',
    'htf_context_leg_anchor'] },
  { title: 'Свежесть данных', keys: [
    'stale_quote_seconds', 'stale_h1_intervals', 'stale_d1_intervals',
    'stale_w1_intervals'] },
  { title: 'Служебное', keys: ['rule_version'] },
];
const NOTIFY_ORDER = [
  'suppress_hours', 'notification_digest_seconds', 'delivery_target_seconds',
  'notify_only_reviewed', 'ltf_notify_kinds'];
const EXPERIMENTAL_ORDER = [
  'uncalibrated_approach_base', 'uncalibrated_cluster_denominator',
  'uncalibrated_consolidation_max_candles', 'uncalibrated_consolidation_overlap_pct',
  'uncalibrated_ob_delay_max_candles', 'uncalibrated_breakout_fvg_back_candles',
  'uncalibrated_breakout_fvg_delay_candles', 'uncalibrated_plateau_equal_peaks'];

// Доли (0..1) — рядом с полем показываем эквивалент в процентах.
const PERCENT_KEYS = new Set([
  'approach_pct', 'cluster_tolerance_pct', 'depth_mid', 'depth_worked',
  'entry_reuse_max_depth', 'uncalibrated_consolidation_overlap_pct']);

// CSV-перечисления: опции чипсов. Значение поля — join(',') выбранных.
const CSV_CHOICES = {
  scan_timeframes: [['D1', 'D1'], ['W1', 'W1']],
  ltf_entry_types: [['FVG', 'FVG'], ['OB', 'OB'], ['BSL', 'BSL'], ['SSL', 'SSL']],
  htf_context_types: [['OB', 'OB'], ['FVG', 'FVG'], ['BSL', 'BSL'], ['SSL', 'SSL']],
  ltf_notify_kinds: [
    ['bos_sms', 'Слом (BOS/SMS)'],
    ['entries_ready', 'Готовые зоны'],
    ['touch', 'Касание'],
    ['sweep_outcome', 'Исход снятия'],
    ['cancellation', 'Отмена'],
  ],
};

// Выбор из перечисления — селект с русскими подписями.
const SELECT_CHOICES = {
  ltf_range_anchor_policy: [
    ['origin_reversal', 'От истока слома (по умолчанию)'],
    ['continuation_only', 'Только подтверждённое продолжение'],
  ],
  htf_context_leg_anchor: [
    ['leg_extreme', 'Последний экстремум ноги'],
    ['leg_start', 'Начало ноги (исходная опора)'],
  ],
};

// Схема полей из GET /api/settings (ranges/enums) — заполняется в openSettings.
let settingsSchema = { ranges: {}, enums: {} };

function cleanSettingsDesc(desc) {
  return (desc || '')
    .replace(/\s*§[\d.]+/g, '')
    .replace(/\(\)/g, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
}

function csvOptionsFor(key) {
  const fromServer = (settingsSchema.enums[key] || []).map((v) => {
    const known = (CSV_CHOICES[key] || []).find(
      ([x]) => x.toLowerCase() === String(v).toLowerCase());
    return [known ? known[0] : v, known ? known[1] : v];
  });
  return fromServer.length ? fromServer : (CSV_CHOICES[key] || null);
}

function settingsControlHtml(key, value) {
  if (typeof value === 'boolean') {
    return `<label class="switch"><input type="checkbox" data-key="${key}" ` +
      `${value ? 'checked' : ''}><span class="switch-track" aria-hidden="true"></span></label>`;
  }
  const csv = csvOptionsFor(key);
  if (csv) {
    const selected = String(value).split(',').map((s) => s.trim().toLowerCase());
    const chips = csv.map(([v, label]) => {
      const on = selected.includes(String(v).toLowerCase());
      return `<label class="chip${on ? ' on' : ''}">` +
        `<input type="checkbox" data-csv-for="${key}" value="${esc(v)}"${on ? ' checked' : ''}>` +
        `<span>${esc(label)}</span></label>`;
    });
    return `<input type="hidden" data-key="${key}" value="${esc(value)}">` +
      `<div class="chip-row" data-csv="${key}">${chips.join('')}</div>`;
  }
  if (SELECT_CHOICES[key]) {
    const opts = SELECT_CHOICES[key].map(([v, label]) =>
      `<option value="${esc(v)}"${String(value) === v ? ' selected' : ''}>${esc(label)}</option>`).join('');
    return `<select data-key="${key}">${opts}</select>`;
  }
  const isNum = typeof value === 'number';
  const range = settingsSchema.ranges[key];
  const attrs = [
    `data-key="${key}"`,
    isNum ? 'type="number" step="any" inputmode="decimal" spellcheck="false"' : 'type="text"',
    range ? `min="${range[0]}" max="${range[1]}"` : '',
  ].filter(Boolean).join(' ');
  const hints = [];
  if (isNum && PERCENT_KEYS.has(key)) {
    hints.push(`<span class="field-hint pct" data-pct-for="${key}"></span>`);
  }
  if (range) hints.push(`<span class="field-hint">допустимо ${range[0]}…${range[1]}</span>`);
  return `<div class="control-row"><input ${attrs} value="${esc(value)}">${hints.join('')}</div>`;
}

function settingsFieldHtml(key, value, uncal) {
  const meta = SETTINGS_META[key] || { title: key, desc: '' };
  const badge = uncal
    ? '<span class="uncalibrated-hint">не калибровано</span>' : '';
  const desc = cleanSettingsDesc(meta.desc);
  const head = `<div class="field-title">${esc(meta.title)}${badge}</div>` +
    (desc ? `<div class="field-desc">${desc}</div>` : '');
  const control = settingsControlHtml(key, value);
  return typeof value === 'boolean'
    ? `<div class="settings-field-row"><div class="settings-field-info">${head}</div>${control}</div>`
    : head + control;
}

function appendSettingsFields(container, pairs, uncal) {
  for (const [key, value] of pairs) {
    const wrap = document.createElement('div');
    wrap.className = 'settings-field';
    wrap.innerHTML = settingsFieldHtml(key, value, uncal.has(key));
    container.appendChild(wrap);
  }
}

// «Правила анализа»: подразделы по RULES_SUBSECTIONS + блок не калиброванных.
function renderRulesPane(container, detector, uncal, deprecated) {
  for (const sub of RULES_SUBSECTIONS) {
    const pairs = sub.keys
      .filter((k) => !deprecated.has(k) && k in detector)
      .map((k) => [k, detector[k]]);
    if (!pairs.length) continue;
    const sec = document.createElement('div');
    sec.className = 'settings-subsection';
    sec.innerHTML = `<h3 class="settings-group-title">${esc(sub.title)}</h3>` +
      '<div class="settings-form"></div>';
    appendSettingsFields(sec.lastElementChild, pairs, uncal);
    container.appendChild(sec);
  }
  const exp = EXPERIMENTAL_ORDER
    .filter((k) => !deprecated.has(k) && k in detector)
    .map((k) => [k, detector[k]]);
  if (exp.length) {
    const det = document.createElement('details');
    det.className = 'settings-exp';
    det.innerHTML =
      `<summary class="settings-group" data-group="experimental">${esc(SETTINGS_GROUP_TITLES.experimental)} · ${exp.length}</summary>` +
      '<div class="field-desc settings-exp-note">Значения не калиброваны и ' +
      'могут влиять на расчёты: найденные зоны, экстремумы и допуски.</div>' +
      '<div class="settings-form"></div>';
    appendSettingsFields(det.lastElementChild, exp, uncal);
    container.appendChild(det);
  }
  // ключи вне подразделов — не теряем (страховка от рассинхрона с бэкендом);
  // доставку и устаревшие сюда не включаем — они рендерятся в своей панели
  const known = new Set(RULES_SUBSECTIONS.flatMap((s) => s.keys)
    .concat(EXPERIMENTAL_ORDER, NOTIFY_ORDER));
  const rest = Object.entries(detector).filter(([k]) =>
    !deprecated.has(k) && !known.has(k));
  if (rest.length) appendSettingsFields(container, rest, uncal);
}

function recalcNote(recalc) {
  const st = recalc && recalc.status;
  if (st === 'running') return 'Новые правила применяются. Показана предыдущая версия.';
  if (st === 'ready') return 'Пересчёт завершён. Новая версия опубликована.';
  if (st === 'failed') {
    return 'Пересчёт не выполнен. Действуют прежние значения.' +
      (recalc.error ? ' ' + recalc.error : '');
  }
  return 'Изменение правил требует пересчёта. Действующая версия остаётся доступной до публикации новой.';
}

function renderAssetActiveButton() {
  const btn = $('asset-active-btn');
  if (!btn) return;
  const ins = state.instruments.find((i) => i.id === state.instrumentId);
  if (!ins) {
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  const on = !!ins.enabled;
  btn.textContent = on ? 'Наблюдать' : 'Включить наблюдение';
  btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  btn.classList.toggle('primary', !on);
}

function renderSettingsAssets() {
  const box = $('settings-assets');
  if (!box) return;
  const count = $('settings-assets-count');
  if (count) {
    const on = state.instruments.filter((i) => i.enabled).length;
    count.textContent = state.instruments.length
      ? `${on} из ${state.instruments.length} под наблюдением` : '';
  }
  if (!state.instruments.length) {
    box.innerHTML = '<p class="field-desc">Активов нет.</p>';
    return;
  }
  box.innerHTML = state.instruments.map((ins) => {
    const on = !!ins.enabled;
    return `<div class="asset-switch${on ? '' : ' is-off'}">` +
      `<span class="state-dot ${on ? 'dot-positive' : 'dot-muted'}" aria-hidden="true"></span>` +
      `<div class="asset-info"><div class="asset-sym">${esc(displayPair(ins.symbol))}</div>` +
      `<div class="asset-sub">${esc(ins.venue)} · ${esc(ins.market_type || 'spot')}` +
      `${on ? '' : ' · снят с наблюдения'}</div></div>` +
      `<label class="switch"><input type="checkbox" data-active-id="${ins.id}" ` +
      `${on ? 'checked' : ''}><span class="switch-track" aria-hidden="true"></span></label></div>`;
  }).join('');
  box.querySelectorAll('[data-active-id]').forEach((inp) => {
    inp.addEventListener('change', () =>
      setInstrumentActive(Number(inp.dataset.activeId), inp.checked));
  });
}

async function setInstrumentActive(id, active) {
  const ins = state.instruments.find((i) => i.id === id);
  if (!ins || !!ins.enabled === !!active) return;
  let res;
  try {
    res = await api(`/api/instruments/${id}/active`, {
      method: 'POST', body: JSON.stringify({ active: !!active }),
    });
  } catch (e) {
    LFUI.error(e,'Не удалось изменить наблюдение',() => setInstrumentActive(id,active));
    renderSettingsAssets(); // не получилось — свитч назад
    return;
  }
  ins.enabled = !!(res && res.enabled);
  ins.ltf_analyze = !!(res && res.ltf_analyze);
  // Keep the selected chart and its history when observation is switched off.
  fillInstrumentSelect();
  renderAssetActiveButton();
  renderSettingsAssets();
  if (state.instrumentId) syncInstrumentContext();
  if (window.LFNow && window.LFNow.refresh) window.LFNow.refresh();
  loadDeskExtras().catch((e) => console.warn('desk extras:', e));
  if (state.instrumentId) reloadAll().catch((e) => console.warn('reload:', e));
}

window.LFInstruments = { setActive: setInstrumentActive };

async function openSettings() {
  if (!state.instruments.length) {
    try { state.instruments = await api('/api/instruments'); } catch (e) { /* список ниже */ }
  }
  renderSettingsAssets();
  const data = await api('/api/settings');
  const notify = $('settings-notify');
  const rules = $('settings-rules');
  if (!notify || !rules) return;
  notify.innerHTML = '';
  rules.innerHTML = '';
  settingsSchema = data.schema || { ranges: {}, enums: {} };
  const uncal = new Set(data.uncalibrated || []);
  const deprecated = new Set(data.deprecated || []);
  const detector = data.detector || {};
  settingsOriginal = detector;
  settingsConfigVersion = data.config_version ?? 0;
  const valueOf = (k) => (k in detector ? [k, detector[k]] : null);
  appendSettingsFields(notify, NOTIFY_ORDER.map(valueOf).filter(Boolean), uncal);
  renderRulesPane(rules, detector, uncal, deprecated);
  const depEntries = Object.keys(detector).filter((k) => deprecated.has(k));
  if (depEntries.length) {
    const note = document.createElement('div');
    note.className = 'settings-deprecated';
    note.innerHTML = depEntries.map((k) =>
      `<div>Поле <code>${esc(k)}</code> устарело и не редактируется.</div>`).join('');
    rules.appendChild(note);
  }
  const tg = $('settings-tg');
  if (tg) {
    tg.classList.remove('hidden');
    tg.innerHTML = data.telegram_configured
      ? '<span class="state-dot dot-positive"></span>Telegram подключён'
      : '<span class="state-dot dot-muted"></span>Telegram не подключён';
  }
  const ver = $('settings-rule-ver');
  if (ver) {
    const v = detector.rule_version;
    ver.textContent = v != null && v !== '' ? `Версия ${v}` : '';
  }
  const recalc = $('settings-recalc');
  if (recalc) recalc.textContent = recalcNote(data.recalc);
  syncThemeChoices();
  $('settings-status').textContent = '';
  updateSettingsDirty();
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
    body: JSON.stringify({...payload, expected_config_version: settingsConfigVersion}),
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
let settingsConfigVersion = 0;

function collectSettingsDelta() {
  const payload = {};
  document.querySelectorAll('#view-settings [data-key]').forEach((inp) => {
    const key = inp.dataset.key;
    const orig = settingsOriginal[key];
    if (inp.type === 'checkbox') {
      if (inp.checked !== Boolean(orig)) payload[key] = inp.checked;
    } else if (inp.value !== String(orig ?? '')) {
      const num = Number(inp.value);
      payload[key] = inp.value !== '' && !isNaN(num) ? num : inp.value;
    }
  });
  return payload;
}

function settingsDirty() {
  return Object.keys(collectSettingsDelta()).length > 0;
}

function updateSettingsDirty() {
  updatePctHints();
  const dirty = settingsDirty();
  const save = $('settings-save');
  const reset = $('settings-cancel');
  if (save) {
    save.disabled = !dirty;
    save.title = dirty ? '' : 'Есть изменения для сохранения';
  }
  if (reset) reset.disabled = !dirty;
}

function updatePctHints() {
  document.querySelectorAll('#view-settings [data-pct-for]').forEach((el) => {
    const inp = document.querySelector(
      `#view-settings [data-key="${el.dataset.pctFor}"]`);
    const v = inp ? Number(String(inp.value).replace(',', '.')) : NaN;
    el.textContent = Number.isFinite(v) && inp
      ? `= ${Math.round(v * 10000) / 100} %` : '';
  });
}

// Чипсы CSV: скрытый input data-key хранит join(','); порядок — как в списке
// опций, не в порядке щелчков.
function syncCsvHidden(key) {
  const hidden = document.querySelector(
    `#view-settings input[type="hidden"][data-key="${key}"]`);
  if (!hidden) return;
  const checked = new Set(
    [...document.querySelectorAll(`#view-settings [data-csv-for="${key}"]`)]
      .filter((c) => c.checked).map((c) => String(c.value).toLowerCase()));
  hidden.value = (csvOptionsFor(key) || [])
    .filter(([v]) => checked.has(String(v).toLowerCase()))
    .map(([v]) => v).join(',');
  document.querySelectorAll(`#view-settings .chip-row[data-csv="${key}"] .chip`)
    .forEach((chip) => {
      const cb = chip.querySelector('input');
      chip.classList.toggle('on', !!(cb && cb.checked));
    });
}

function initSettingsDirty() {
  const view = document.querySelector('#view-settings');
  if (!view || view.dataset.dirtyBound) return;
  view.dataset.dirtyBound = '1';
  const onChange = (event) => {
    const target = event.target;
    const csv = target && target.closest && target.closest('[data-csv-for]');
    if (csv) syncCsvHidden(csv.dataset.csvFor);
    updateSettingsDirty();
  };
  view.addEventListener('input', onChange);
  view.addEventListener('change', onChange);
}

async function saveSettings() {
  const payload = collectSettingsDelta();
  const status = $('settings-status');
  const form = $('view-settings');
  form.querySelectorAll('.field-error').forEach((e) => e.remove());
  form.querySelectorAll('.settings-field.invalid').forEach((e) => e.classList.remove('invalid'));
  status.textContent = '';
  let resp;
  try {
    resp = await postSettings(payload);
  } catch (err) {
    status.textContent = 'Изменения не сохранены. Действуют прежние значения. ' + err.message;
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
    status.textContent = 'Изменения не сохранены. Действуют прежние значения.' +
      (names.length ? ' ' + names.join('; ') : ' Настройки отклонены сервером.');
    return;
  }
  if (!resp.ok) {
    let detail = resp.statusText;
    try { detail = (await resp.json()).detail || detail; } catch (e) { /* не JSON */ }
    status.textContent = 'Изменения не сохранены. Действуют прежние значения. ' +
      (typeof detail === 'string' ? detail : 'ошибка сервера');
    return;
  }
  let res = await resp.json();
  if (resp.status === 202) {
    status.textContent = 'Настройки проверены. Идёт пересчёт…';
    $('settings-save').disabled = true;
    try {
      let job = res.recalc;
      while (['queued', 'running'].includes(job.status)) {
        await new Promise(resolve => setTimeout(resolve, 1000));
        job = await HTF.api(res.status_url);
        status.textContent = recalcNote(job);
      }
      if (job.status !== 'ready') throw new Error(job.error || 'Пересчёт не завершён');
      const updated = await HTF.api('/api/settings');
      res = {detector: updated.detector, config_version: updated.config_version, applied: Object.keys(payload)};
    } catch (err) {
      status.textContent = 'Настройки не применены: ' + err.message;
      updateSettingsDirty(); return;
    }
  }
  settingsConfigVersion = res.config_version ?? settingsConfigVersion;
  settingsOriginal = res.detector || { ...settingsOriginal, ...payload };
  updateSettingsDirty();
  status.textContent = `Сохранено: ${res.applied.length} параметров.`;
}

// Этап 3 ребрендинга: публичные маршруты — now / desk / review / journal /
// settings. Старые якоря (#overview, #events) — алиасы, id секций не меняем.
const VIEW_IDS = {
  now: 'view-now',
  desk: 'view-overview',
  review: 'view-review',
  journal: 'view-journal',
  help: 'view-help',
  settings: 'view-settings',
};
const VIEW_ALIASES = {
  overview: 'desk',
  events: 'journal',
  observation: 'now',
  analysis: 'desk',
};

function showView(name, navigation = 'push') {
  name = VIEW_ALIASES[name] || name;
  if (!VIEW_IDS[name]) name = 'now';
  document.querySelectorAll('.view').forEach((el) => el.classList.toggle('active', el.id === VIEW_IDS[name]));
  document.querySelectorAll('.app-tab[data-view], .mobile-nav a[data-view], .nav-more-panel a[data-view]').forEach((el) => {
    el.classList.toggle('active', el.dataset.view === name);
  });
  const moreBtn = document.getElementById('nav-more');
  if (moreBtn) moreBtn.classList.toggle('active', name === 'journal' || name === 'settings' || name === 'help');
  const morePanel = document.getElementById('nav-more-panel');
  if (morePanel) morePanel.classList.add('hidden');
  if (moreBtn) moreBtn.setAttribute('aria-expanded', 'false');
  if (name === 'settings') openSettings();
  if (name === 'now' && window.LFNow) window.LFNow.show();
  if (name === 'desk') {
    renderDeskHead(); // пара известна из списка активов, не ждём снимок /current
    requestAnimationFrame(drawZones);
    scheduleDeskRefresh(); // снимок /current мог устареть, пока desk был скрыт
  }
  if (name === 'review') enterReviewMode().catch((e) => console.warn('review mode:', e));
  if (name === 'journal' && window.LFJournal) window.LFJournal.show();
  if (location.hash !== '#' + name) history[navigation === 'replace' ? 'replaceState' : 'pushState'](null, '', '#' + name);
  document.querySelectorAll('.app-tab[data-view]').forEach(el => {
    if (el.dataset.view === name) el.setAttribute('aria-current','page'); else el.removeAttribute('aria-current');
  });
  document.dispatchEvent(new CustomEvent('lf-view',{detail:{name}}));
}

function setupViews() {
  document.querySelectorAll('[data-view]').forEach((el) => {
    if (el.getAttribute('href') && el.getAttribute('href').startsWith('/')) return;
    el.addEventListener('click', (event) => {
      event.preventDefault();
      showView(el.dataset.view);
    });
  });
  window.addEventListener('hashchange', () => showView((location.hash || '#now').slice(1), 'replace'));
  window.addEventListener('popstate', () => {
    const url = new URL(location.href), id = Number(url.searchParams.get('instrument'));
    showView((location.hash || '#now').slice(1), 'replace');
    if (id && id !== state.instrumentId) LFDesk.openInstrument(id,{history:false,mode:url.searchParams.get('mode') === 'h1' ? 'h1' : 'context'});
  });
  showView((location.hash || '#now').slice(1), 'replace');
}

// ---------------------------------------------------------------------------
// WebSocket: обновления без перезагрузки (§11 п.6)
// ---------------------------------------------------------------------------

// Доменные версии (§13/§14, этап 6b): рефетчим только домен, чья версия
// выросла; тик котировки сеть не трогает (AC16). Снимки со with_version=true
// записывают версии через adopt(), WS-сообщения и статус — через bump/diff.
const domainSync = new HTF.DomainSync();
domainSync
  .on('zones', () => loadZones()
    .then(() => {
      if (state.selectedZoneId) {
        loadZoneDetail(state.selectedZoneId, false).catch((e) => console.warn('zone detail:', e));
      }
    })
    .then(() => Promise.all([loadCandidates(), loadEvents()])))
  .on('candles', (key) => {
    const parts = String(key || '').split(':');
    const iid = Number(parts[1]);
    const tf = parts[2];
    if (iid && iid !== state.instrumentId) return null; // неотображаемый канал
    if (tf && tf !== state.timeframe) return null;
    const jobs = [loadCandles()];
    // на структуре H1 закрытие бара перечитывает и слои (прежний reloadAll)
    if (state.chartMode === 'h1' && window.H1Layers) jobs.push(loadH1Markers());
    return Promise.all(jobs);
  })
  .on('scenario', () => {
    // карточка сценария — только когда рабочее место на экране; вход во
    // вкладку и так делает scheduleDeskRefresh()
    if (isDeskActive()) scheduleDeskRefresh();
  })
  .on('config', () => loadInstruments());

// Дополнительные слушатели WS-сообщений: экран «Сейчас» (now.js) и другие
// модули подписываются, не меняя основной handleWsMessage
const wsExtraHandlers = [];
function registerWsHandler(fn) { wsExtraHandlers.push(fn); }

// Закрытый бар (§14): при непрерывности версии домена свечей (ровно +1) и
// наличии OHLC в сообщении — точечное обновление серии; при разрыве, старом
// конверте без нагрузки или чужом канале — обычный дебаунс-рефетч домена.
function handleWsCandle(data) {
  const iid = data.instrument_id;
  const tf = data.timeframe;
  if (iid === state.instrumentId && tf === 'D1') {
    loadCandles().then(() => Promise.all([loadH1Markers(), loadDeskExtras()]))
      .catch((e) => console.warn('D1 structure:', e));
  }
  const key = (iid != null && tf) ? `candles:${iid}:${tf}` : null;
  const version = data.domain_versions && data.domain_versions.candles;
  const current = iid != null && iid === state.instrumentId && tf === state.timeframe;
  const known = key ? domainSync.knownVersion(key) : null;
  const bar = current && data.open_time != null ? {
    time: Math.floor(Number(data.open_time) / 1000),
    open: data.open, high: data.high, low: data.low, close: data.close,
  } : null;
  if (bar && version != null && known != null && version === known + 1
      && state.candleSeries && state.candles.length) {
    const last = state.candles[state.candles.length - 1];
    // закрывшийся бар замещает формирующий того же периода или встаёт
    // строго следом; разрыв баров (синк догнал несколько) — рефетч домена
    const period = TF_SECONDS[tf] || 0;
    const contiguous = last && (bar.time === last.time
      || (period > 0 && bar.time === last.time + period));
    if (contiguous) {
      domainSync.adopt(key, version);
      state.candleSeries.update(bar);
      if (bar.time === last.time) state.candles[state.candles.length - 1] = bar;
      else state.candles.push(bar);
      drawZones();
      if (state.chartMode === 'h1' && window.H1Layers) {
        loadH1Markers().catch((e) => console.warn('h1 markers:', e));
      }
      return;
    }
  }
  if (key) domainSync.bump(key, version);
}

function handleWsMessage(data) {
  if (window.LFDataSync) LFDataSync.onWs(data);
  // §14/AC16: версии доменов — триггер точечных рефетчей; в скрытой вкладке
  // учёт и рефетчи продолжаются (серверные сигналы не останавливаем), а
  // визуальная работа тиков пропускается — возврат делает один recovery (AC17)
  if (data.type === 'candle') {
    handleWsCandle(data);
  } else {
    domainSync.notifyWs(data);
  }
  if (document.hidden) return;
  if (data.type === 'price') {
    if (data.instrument_id === state.instrumentId && data.price) {
      state.lastPrice = data.price;
      const bar = data.candles && data.candles[state.timeframe];
      if (bar && state.candleSeries && state.candles) {
        state.candleSeries.update(bar);
        const last = state.candles[state.candles.length - 1];
        if (last && last.time === bar.time) state.candles[state.candles.length - 1] = bar;
        else if (!last || bar.time > last.time) state.candles.push(bar);
      }
      // AC16: тик двигает цену, текущий бар и маркеры «зона—цена» — и всё;
      // drawZones()/сценарий/снимки пересчитываются по смене версий доменов
      updateLastPrice(true);
    }
  } else if (data.type === 'event') {
    const foreign = data.instrument_id != null
      && data.instrument_id !== state.instrumentId;
    if (data.event && data.event.kind !== 'approach' && !foreign && $('events-list')) {
      $('events-list').prepend(eventLi(data.event));
    }
  }
  // Дополнительные слушатели (экран «Сейчас» и др.): ошибка слушателя не
  // должна ломать основную обработку сообщения
  for (const fn of wsExtraHandlers) {
    try { fn(data); } catch (e) { console.warn('ws listener:', e); }
  }
}

function connectWs() {
  // соединение/переподключение/индикатор — в common.js (window.HTF)
  state.ws = HTF.connectWs(handleWsMessage, $('ws-indicator'), (isReconnect) => {
    // AC17: после reconnect — одно объединённое восстановление по версиям
    if (isReconnect && window.LFDataSync) return LFDataSync.recover();
  });
}

// ---------------------------------------------------------------------------
// Инструменты и старт
// ---------------------------------------------------------------------------

// U01-lite: единый ключ выбранного инструмента для обеих страниц (HTF/LTF).
// Приоритет: ?instrument= в URL → localStorage → первый в списке.
const INSTRUMENT_KEY = 'htf:instrument';

function resolveInitialInstrument() {
  if (!state.instruments.length) return null;
  const ids = new Set(state.instruments.map((i) => i.id));
  const fromUrl = Number(new URLSearchParams(location.search).get('instrument'));
  // Явный адрес открывает и выключенный актив: это уже один выбранный график.
  if (fromUrl && ids.has(fromUrl)) return fromUrl;
  const enabled = state.instruments.filter((i) => i.enabled);
  const pool = enabled.length ? enabled : state.instruments;
  const saved = Number(localStorage.getItem(INSTRUMENT_KEY));
  if (saved && pool.some((i) => i.id === saved)) return saved;
  return pool[0].id;
}

function updateLtfLinks() {
  ['lnk-ltf', 'lnk-ltf-nav', 'lnk-ltf-mobile'].forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (el.tagName === 'A') el.href = '#desk';
    el.dataset.chartMode = 'h1';
  });
}

function syncInstrumentContext() {
  // выбор инструмента разделяется со страницей LTF: localStorage + ?instrument=
  // (history.replaceState, без перезагрузки; hash вкладки сохраняется)
  if (!state.instrumentId) return;
  localStorage.setItem(INSTRUMENT_KEY, String(state.instrumentId));
  const url = new URL(location.href);
  url.searchParams.set('instrument', String(state.instrumentId));
  history.replaceState(null, '', url.pathname + url.search + url.hash);
  updateLtfLinks();
}

// Мост для экрана «Сейчас» (now.js): открыть рабочее место выбранного актива.
// Тот же механизм выбора, что у селекта в workbar: localStorage htf:instrument
// + ?instrument= (syncInstrumentContext), затем перезагрузка данных desk.
window.LFDesk = {
  async openInstrument(id, options = {}) {
    const numId = Number(id);
    if (numId && state.instruments.some((i) => i.id === numId)) {
      if (options.history !== false) {
        const url = new URL(location.href); url.searchParams.set('instrument',String(numId)); url.hash='desk';
        if (options.mode === 'h1') url.searchParams.set('mode','h1');
        else if (options.mode === 'context') url.searchParams.delete('mode');
        if (options.at) url.searchParams.set('at',String(options.at)); else url.searchParams.delete('at');
        if (options.eventId) url.searchParams.set('event',String(options.eventId)); else url.searchParams.delete('event');
        if (options.zoneId) url.searchParams.set('zone',String(options.zoneId)); else url.searchParams.delete('zone');
        history.pushState(null,'',url.pathname+url.search+url.hash);
      }
      state.instrumentId = numId;
      if (options.mode) setChartMode(options.mode);
      const sel = $('instrument-select');
      if (sel) sel.value = String(numId);
      syncInstrumentContext();
      showView('desk');
      showInspector(false);
      clearZoneSelection();
      try {
        await Promise.all([reloadAll(),loadDeskExtras()]);
        if (state.instrumentId !== numId) return;
        if (options.zoneId) await loadZoneDetail(Number(options.zoneId),false);
        if (options.at) focusAnalysisTime(Number(options.at));
      } catch (err) { LFUI.error(err,'Анализ не загрузился',() => LFDesk.openInstrument(numId,{...options,history:false})); }
    } else {
      showView('desk');
    }
  },
};

function focusAnalysisTime(at) {
  const index = state.candles.findIndex((c,i) => c.time * 1000 <= at && (!state.candles[i+1] || state.candles[i+1].time*1000 > at));
  if (index < 0 || at > (state.candles.at(-1)?.time || 0)*1000 + (TF_SECONDS[state.timeframe] || 3600)*1000) {
    LFUI.openDrawer('Событие за пределами истории','<p>Свеча события не входит в загруженную историю. Выбран правильный актив; увеличьте период истории.</p>'); return;
  }
  state.chart.timeScale().setVisibleLogicalRange({from:Math.max(0,index-20),to:index+12});
  const el = $('h1-event-card');
  if (el) { el.classList.remove('hidden'); el.textContent = 'Событие: ' + HTF.fmtTime(at); }
}

// ---------------------------------------------------------------------------
// Рабочее место (LevelFrame, этап 4 ребрендинга; макет §6.B плана): строка
// актива, список активов слева, карточка состояния сценария и «зоны
// сценария» под графиком — поверх read model /api/ltf/instruments +
// /api/ltf/instruments/{id}/current (тот же снимок, что у экрана «Сейчас»).
// Формулировки «что происходит / чего ждём / условие отмены» повторяют
// правила карточки now.js — держать синхронно с ней.
// ---------------------------------------------------------------------------

const deskData = {
  assets: [],           // строки /api/ltf/instruments
  currents: new Map(),  // instrument_id -> снимок /current (цены списка)
  current: null,        // снимок /current выбранного инструмента
  reqSeq: 0,            // поздний ответ старого запроса не применяется
  refreshTimer: null,
  readError: false,
  versionRetried: false,
  assetsVersion: null,
};

// L05: основания выбора контекста (коды сервера, app/services/overview.py)
const DESK_BASIS_RU = {
  manual: 'Выбран вручную',
  price_inside: 'Цена внутри зоны',
  nearest: 'Ближайшая к цене зона',
  last_scenario: 'Последний действующий сценарий',
  last_contact: 'Последний контакт с зоной',
};
const DESK_DATA_REASON_RU = {
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
// причины исключения Entry Zone (стабильные коды evaluate_final, §10)
const DESK_REASON_RU = {
  ok: 'Подходит по правилам',
  outside_pd: 'Вне Premium/Discount',
  tested_too_deep: 'Тест ≥ 90% глубины',
  historically_worked: 'Отработана до активации (≥ 90% глубины)',
  type_disabled: 'Тип отключён настройкой',
  invalid: 'Зона невалидна',
  swept_level: 'Уровень снят',
  level_broken: 'Уровень пройден без возврата',
  fvg_filled: 'FVG перекрыт полностью',
  origin_unresolved: 'Принадлежность движению не доказана',
  range_pending: 'Диапазон ещё не подтверждён',
};

function deskAgeText(ms) {
  if (!ms) return '—';
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 60) return `${s} с назад`;
  const m = Math.round(s / 60);
  if (m < 60) return `${m} мин назад`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} ч назад`;
  return `${Math.round(h / 24)} дн назад`;
}

function isDeskActive() {
  const v = $('view-overview');
  return v && v.classList.contains('active');
}

function deskDirBadge(d) {
  if (d === 'bull') return '<span class="dir-badge bull">↑ Рост</span>';
  if (d === 'bear') return '<span class="dir-badge bear">↓ Снижение</span>';
  if (d === 'mixed') return '<span class="dir-badge">▲▼ Разные контексты</span>';
  return '';
}

// Короткая подпись состояния в списке активов (макет §6.B):
// Сценарий / Конфликт / В зоне / Проверка / Ожидание
function deskAssetState(r) {
  if (r.scenario_summary) return {label:r.scenario_summary.label,dot:'dot-muted'};
  if (r.asset && r.asset.governs && r.market_stage) {
    const code = r.asset.asset_state && r.asset.asset_state.code;
    const dot = code === 'data_incomplete' || code === 'state_inconsistent' ? 'dot-warning' : 'dot-muted';
    return { label: r.market_stage, dot };
  }
  if (r.direction === 'mixed') return { label: 'Конфликт', dot: 'dot-warning' };
  switch (r.attention) {
    case 'eligible': return { label: 'Сценарий', dot: 'dot-positive' };
    case 'price_in_zone': return { label: 'В зоне', dot: 'dot-positive' };
    case 'review': return { label: 'Проверка', dot: 'dot-warning' };
    case 'awaiting': return { label: 'Ожидание', dot: 'dot-muted' };
    case 'data_problem': return { label: 'Данные задерживаются', dot: 'dot-warning' };
    default: return { label: r.stage || '—', dot: 'dot-muted' };
  }
}

function deskHeadline(v) {
  if (v.market_stage) return v.market_stage;
  if (v.wait && v.wait.message && !v.selected_context_id) return v.wait.message;
  const ds = v.data_state || {};
  if (ds.state && ds.state !== 'ok') {
    return 'Данные задерживаются';
  }
  return v.stage || 'Активного контекста нет';
}

function deskLeadText(v, r, tf) {
  const ds = v.data_state || {};
  const disabled = (v.reached_disabled || [])[0];
  if (disabled && disabled.message) return disabled.message;
  if (v.wait && v.wait.message && !v.selected_context_id) return v.wait.message;
  if (ds.state && ds.state !== 'ok') {
    return (DESK_DATA_REASON_RU[ds.reason] || 'Источник данных недоступен') +
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

function deskWaitText(v) {
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

function deskCancelText(v) {
  const c = v.cancel_condition;
  if (c && c.level != null && c.status && c.status !== 'undefined') {
    const side = c.side === 'above' ? 'выше' : 'ниже';
    const verb = c.status === 'occurred' ? 'Отмена произошла' : 'Отменит';
    const kind = c.kind ? ` (${c.kind})` : '';
    return `${verb}: закрытие H1 строго ${side} ${fmtPrice(c.level)}${kind}`;
  }
  const sc = v.current_scenario;
  if (sc && sc.reverse_break && sc.reverse_break.price != null) {
    const side = sc.direction === 'bear' ? 'выше' : 'ниже';
    return `Закрытие H1 ${side} ${fmtPrice(sc.reverse_break.price)}`;
  }
  return null;
}

function deskReviewBadge(v) {
  const rs = v && v.review_state;
  if (!rs || !rs.needed) return '';
  return `<a class="review-badge" href="#review">Нужна проверка · ${rs.count}</a>`;
}

function deskFactsHtml(v) {
  const facts = (v && v.liquidity_facts) || [];
  if (!facts.length) return '';
  return facts.slice(0, 3).map((f) =>
    `<div class="desk-fact">${esc(String(f.type || 'уровень').toUpperCase())} ${esc(f.timeframe || '')} ` +
    `${fmtPrice(f.level)} снят · не сценарий</div>`).join('');
}

function deskSelectedTf(v) {
  const ctx = (v.contexts || []).find((c) => c.observation_id === v.selected_context_id);
  return (ctx && ctx.parent_zone && ctx.parent_zone.timeframe) || state.timeframe;
}

function renderDeskHead() {
  const ins = state.instruments.find((i) => i.id === state.instrumentId);
  if ($('desk-symbol')) $('desk-symbol').textContent = ins ? displayPair(ins.symbol) : '—';
  if ($('desk-venue')) {
    $('desk-venue').textContent = ins ? `${ins.venue} · ${ins.market_type || 'spot'}` : '—';
  }
  const v = deskData.current;
  const price = (v && v.price != null) ? v.price : state.lastPrice;
  if ($('desk-price')) $('desk-price').textContent = price != null ? fmtPrice(price) : '—';
  renderDeskQuoteAge();
  const fresh = $('desk-fresh');
  if (!fresh) return;
  const ds = v && v.data_state;
  // качества данных без снимка не выдумываем: блок просто скрыт
  if (!ds || !ds.state) {
    fresh.classList.add('hidden');
    return;
  }
  fresh.classList.remove('hidden');
  const ok = ds.state === 'ok';
  fresh.innerHTML = `<span class="state-dot ${ok ? 'dot-positive' : 'dot-warning'}"></span>` +
    esc(ok ? 'Данные актуальны' : 'Данные задерживаются') +
    (!ok && ds.reason ? ` · ${esc(DESK_DATA_REASON_RU[ds.reason] || ds.reason)}` : '');
}

function renderDeskQuoteAge() {
  const el = $('desk-quote-age');
  if (!el) return;
  const v = deskData.current;
  const at = v && v.quote_at;
  el.textContent = 'Котировка · ' + (at ? deskAgeText(at) : '—');
}

function renderDeskAssets() {
  const el = $('desk-assets-list');
  if (!el) return;
  if (!deskData.assets.length) {
    el.innerHTML = '<div class="ltf-empty">Список наблюдения пуст.</div>';
    return;
  }
  el.innerHTML = deskData.assets.map((r) => {
    const ins = r.instrument;
    const cur = deskData.currents.get(ins.id);
    const price = (ins.id === state.instrumentId && state.lastPrice != null)
      ? state.lastPrice
      : (r.price != null ? r.price : (cur && cur.price != null ? cur.price : null));
    const st8 = deskAssetState(r);
    const sel = ins.id === state.instrumentId ? ' selected' : '';
    return `<div class="desk-asset-row">` +
      `<button type="button" class="desk-asset${sel}" data-iid="${ins.id}">` +
      `<span class="desk-asset-sym">${esc(displayPair(ins.symbol))}</span>` +
      `<span class="desk-asset-price">${price != null ? esc(fmtPrice(price)) : '—'}</span>` +
      `<span class="desk-asset-state"><span class="state-dot ${st8.dot}"></span>${esc(st8.label)}</span>` +
      `</button>` +
      `<button type="button" class="btn small desk-asset-off" data-off="${ins.id}" aria-label="Отключить наблюдение ${esc(ins.symbol)}" title="Отключить наблюдение">−</button>` +
      `</div>`;
  }).join('');
  el.querySelectorAll('.desk-asset').forEach((btn) => {
    btn.onclick = () => window.LFDesk.openInstrument(Number(btn.dataset.iid));
  });
  el.querySelectorAll('.desk-asset-off').forEach((btn) => {
    btn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      setInstrumentActive(Number(btn.dataset.off), false);
    };
  });
}

// ---------------------------------------------------------------------------
// Панель «Актив / Зоны» (объединённое окно правой колонки рабочего места):
// переключение вкладками, при выборе зоны на графике вкладка «Зоны»
// открывается сама.
// ---------------------------------------------------------------------------
function setDeskPanelTab(tab) {
  const panel = $('desk-panel');
  if (!panel) return;
  const next = tab === 'zones' ? 'zones' : 'scenario';
  panel.dataset.tab = next;
  document.querySelectorAll('.desk-panel-tabs .panel-tab').forEach((btn) => {
    const on = btn.dataset.panelTab === next;
    btn.classList.toggle('active', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  });
}

function renderPanelTabHead() {
  const el = $('panel-tab-symbol');
  if (!el) return;
  const ins = state.instruments.find((i) => i.id === state.instrumentId);
  el.textContent = ins ? displayPair(ins.symbol) : 'Актив';
}

function renderPanelTabCount() {
  const el = $('panel-tab-count');
  if (!el) return;
  const zones = filteredZones();
  if (!zones.length) {
    el.classList.add('hidden');
    el.textContent = '';
    return;
  }
  el.classList.remove('hidden');
  el.textContent = String(zones.length);
}

// Схема «зона — цена» под заголовком сообщения живёт в messages.js
// (LFCopy.visual): одна разметка на Обзоре и на рабочем месте.

function renderDeskScenario() {
  const el = $('desk-scenario');
  if (!el) return;
  if (deskData.readError) {
    el.classList.remove('hidden');
    el.innerHTML = '<h3>Ошибка чтения снимка</h3>' +
      '<p class="desk-sc-lead">Карточка не скрыта: снимок не прочитан. Обновите экран.</p>';
    return;
  }
  const v = deskData.current;
  if (!v) {
    el.classList.add('hidden');
    return;
  }
  el.classList.remove('hidden');
  const ins = state.instruments.find((i) => i.id === state.instrumentId) || v.instrument || {};
  const row = deskData.assets.find((r) => r.instrument.id === state.instrumentId);
  const tf = deskSelectedTf(v);
  const watching = !!(ins && ins.enabled);
  const ds = v.data_state || {};
  const pres = v.presentation;
  const live = state.lastPrice;
  const quote = pres && pres.location ? pres.location.quote : null;
  const suppress = live != null && quote != null && Number(live) !== Number(quote);
  if (pres && window.LFCopy) {
    el.innerHTML = LFUI.scenarioHtml(v.scenario_summary) + LFCopy.card(pres, {
      hideHeadline: !!v.scenario_summary,
      suppressLocation: suppress,
      visualHtml: '',
      hideDetails: false,
      simpleTradeTitle: true,
      deskLayout: true,
    }) +
      `<button type="button" id="desk-watch-btn" class="btn desk-watch ${watching ? 'is-on' : 'is-off'}" aria-pressed="${watching}">${watching ? '✓ Наблюдение включено' : 'Наблюдение выключено'}</button>` +
      '<button type="button" class="btn small" id="desk-data-details">Качество и время данных</button>' +
      `<details class="desk-basis"><summary>Основания и качество данных</summary><dl>` +
      `<dt>Показан контекст</dt><dd>${esc(DESK_BASIS_RU[v.selected_context_basis] || v.selected_context_basis || '—')}</dd>` +
      `<dt>Качество данных</dt><dd>${esc(ds.state === 'ok' ? 'Данные актуальны' : (DESK_DATA_REASON_RU[ds.reason] || ds.reason || ds.state || '—'))}</dd>` +
      `<dt>Версия состояния</dt><dd>${v.state_version != null ? v.state_version : '—'}</dd>` +
      `</dl></details>`;
    $('desk-watch-btn').onclick = toggleDeskWatch;
    $('desk-data-details').onclick = () => LFUI.openDrawer('Данные актива',LFUI.channelsHtml(ds));
    return;
  }
  const cancel = deskCancelText(v);
  const conflict = v.direction_conflict;
  el.innerHTML = `
    <div class="desk-sc-head">
      <span>${esc(displayPair(ins.symbol || ''))} · ${esc(tf)}</span>
      ${deskDirBadge(v.direction)}
    </div>
    <h3>${esc(deskHeadline(v))}</h3>
    ${deskReviewBadge(v)}
    <p class="desk-sc-lead">${esc(deskLeadText(v, row, tf))}</p>
    ${deskFactsHtml(v)}
    ${conflict ? `<p class="desk-sc-lead">${esc(conflict.note)}</p>` : ''}
    <dl class="now-qa desk-qa">
      <dt class="qa-q">Что происходит</dt>
      <dd class="qa-a">${esc(v.market_stage || v.stage || (row && row.stage) || '—')}</dd>
      <dt class="qa-q">Чего ждём</dt>
      <dd class="qa-a">${esc(deskWaitText(v))}</dd>
      ${cancel ? `<dt class="qa-q">Условие отмены</dt><dd class="qa-a">${esc(cancel)}</dd>` : ''}
    </dl>
    <button type="button" id="desk-watch-btn" class="btn desk-watch ${watching ? 'is-on' : 'is-off'}" aria-pressed="${watching}">${watching ? '✓ Наблюдение включено' : 'Наблюдение выключено'}</button>
    <button type="button" class="btn small" id="desk-data-details">Качество и время данных</button>
    <details class="desk-basis">
      <summary>Основания и качество данных</summary>
      <dl>
        <dt>Показан контекст</dt>
        <dd>${esc(DESK_BASIS_RU[v.selected_context_basis] || v.selected_context_basis || '—')}</dd>
        <dt>Качество данных</dt>
        <dd>${esc(ds.state === 'ok' ? 'Данные актуальны' : (DESK_DATA_REASON_RU[ds.reason] || ds.reason || ds.state || '—'))}</dd>
        <dt>Версия состояния</dt>
        <dd>${v.state_version != null ? v.state_version : '—'}</dd>
      </dl>
    </details>`;
  $('desk-watch-btn').onclick = toggleDeskWatch;
  $('desk-data-details').onclick = () => LFUI.openDrawer('Данные актива', LFUI.channelsHtml(ds));
}

async function toggleDeskWatch() {
  const ins = state.instruments.find((i) => i.id === state.instrumentId);
  if (!ins) return;
  const btn = $('desk-watch-btn');
  if (btn) btn.disabled = true;
  await setInstrumentActive(ins.id,!ins.enabled);
  renderDeskScenario();
  scheduleDeskRefresh();
}

function renderDeskEntries() {
  const el = $('desk-entries');
  if (!el) return;
  deskData.entryZones = new Map();
  // Зоны идут по приближенности к текущей цене (цена внутри зоны — первой),
  // а не в порядке выдачи сервера.
  const price = state.lastPrice;
  const byProximity = (a, b) => (price == null ? 0 : distanceToZone(a, price) - distanceToZone(b, price));
  if (state.chartMode === 'h1' && state.h1Layers && window.H1Layers) {
    const H = window.H1Layers;
    const entries = H.currentEntryZones(state.h1Layers, H.loadSettings(), price);
    const shown = entries.slice(0, 8);
    shown.forEach(z => deskData.entryZones.set(String(z.id), z));
    const quality = deskData.current?.data_state;
    const paused = quality && quality.state && quality.state !== 'ok' && quality.reason !== 'current_candle_pending';
    el.classList.remove('hidden');
    el.innerHTML = `<div class="desk-entries-head"><h2>Зоны текущего движения <span class="desk-entry-count">${entries.length}</span></h2>` +
      '<span class="muted">Участки входа в PD · клик покажет зону</span></div>' +
      '<div class="desk-entry-grid">' + shown.map(z =>
        `<button type="button" class="desk-entry current-entry" data-analysis-zone="${esc(z.id)}">` +
        `<span class="desk-entry-name">${esc(z.type)} H1 <span class="desk-entry-half">${esc(z.pdHalf)}</span></span>` +
        `<span class="desk-entry-range">${fmtPrice(z.admitted[0])}${z.admitted[0] === z.admitted[1] ? '' : '–' + fmtPrice(z.admitted[1])}</span>` +
        `<span class="desk-entry-status${z.entryReady && !paused ? ' ok' : ''}">${paused ? 'Данные уточняются' : z.entryReady ? 'Допущена к входу' : 'Наблюдение'} · тест ${Math.round((z.max_test_depth || 0) * 100)}%</span></button>`
      ).join('') + '</div>' +
      (entries.length > shown.length ? `<p class="desk-entry-more">Показаны ${shown.length} ближайших из ${entries.length}. Остальные — на графике.</p>` : '') +
      (entries.length ? '' : `<div class="ltf-empty">${state.h1Layers.setup ? 'В текущем PD подходящих зон пока нет.' : 'Ждём расчёт текущего движения H1.'}</div>`);
    return;
  }
  const v = deskData.current;
  if (!v) {
    el.classList.add('hidden');
    el.innerHTML = '';
    return;
  }
  el.classList.remove('hidden');
  if (!v.current_scenario) {
    el.innerHTML = '<div class="desk-entries-head"><h2>Зоны сценария</h2></div>' +
      '<div class="ltf-empty">Активного сценария нет.</div>';
    return;
  }
  const entries = (v.eligible_entries || []).slice().sort(byProximity);
  const rows = entries.map((e) => `
    <button type="button" class="desk-entry" data-analysis-zone="${e.entry_zone_id}">
      <span class="desk-entry-name">${esc(e.type)} · H1<span class="desk-entry-sub"> / текущий сценарий</span></span>
      <span class="desk-entry-range">${fmtPrice(e.lower)} — ${fmtPrice(e.upper)} USDT</span>
      <span class="desk-entry-status${e.eligible_now ? ' ok' : ''}">${e.eligible_now
        ? '<span class="state-dot dot-positive"></span>Подходит по правилам'
        : '<span class="state-dot dot-muted"></span>' + esc(DESK_REASON_RU[e.reason] || e.reason || '—')}</span>
    </button>`).join('');
  el.innerHTML = `<div class="desk-entries-head"><h2>Зоны сценария / ${entries.length}</h2>` +
    '<span class="muted">Тот же актив и контекст</span></div>' +
    (rows || '<div class="ltf-empty">Сценарий активен; подходящих зон сейчас нет.</div>');
}

function renderDeskExtras() {
  renderDeskHead();
  renderPanelTabHead();
  renderDeskAssets();
  renderDeskScenario();
  renderDeskEntries();
}

// Снимок /current грузится параллельно основному графику (reloadAll) и не
// блокирует его; ошибка чтения — карточка/зоны скрываются, график живёт
async function loadDeskExtras() {
  const id = state.instrumentId;
  if (deskData.pending && deskData.pending.id === id) return deskData.pending.promise;
  const pending = {id};
  deskData.pending = pending;
  pending.promise = fetchDeskExtras().finally(() => {
    if (deskData.pending === pending) deskData.pending = null;
  });
  return pending.promise;
}

async function fetchDeskExtras() {
  const id = state.instrumentId;
  const req = ++deskData.reqSeq;
  let assetsRes = null;
  let cur = null;
  let failed = false;
  try {
    if (id) {
      const bundle = await api(`/api/ltf/instruments/${id}/workspace?include_chart=false`);
      assetsRes = bundle.assets;
      cur = bundle.view;
    } else assetsRes = await api('/api/ltf/instruments');
  } catch (e) {
    failed = true;
  }
  if (req !== deskData.reqSeq || id !== state.instrumentId) return;
  if (failed || (id && !cur)) {
    deskData.readError = true;
    renderDeskScenario();
    return;
  }
  const assetsVersion = assetsRes && assetsRes.state_version;
  const currentVersion = cur && cur.state_version;
  if (assetsVersion != null && currentVersion != null && assetsVersion !== currentVersion) {
    if (!deskData.versionRetried) {
      deskData.versionRetried = true;
      deskData.reqSeq -= 1;
      return fetchDeskExtras();
    }
    deskData.readError = true;
    renderDeskScenario();
    return;
  }
  deskData.versionRetried = false;
  deskData.readError = false;
  if (assetsRes) {
    deskData.assets = assetsRes.instruments || [];
    deskData.assetsVersion = assetsVersion;
    // §13 (этап 6b): версии доменов списка (zones + scenario/quote по активам)
    if (assetsRes.versions) {
      domainSync.adoptAll(HTF.DomainSync.statusEntries(assetsRes.versions));
    }
  }
  deskData.current = cur;
  if (cur && cur.versions) {
    domainSync.adopt(`scenario:${id}`, cur.versions.scenario);
    domainSync.adopt('zones', cur.versions.zones);
    domainSync.adopt(`quote:${id}`, cur.versions.quote);
  }
  if (state.chartMode === 'h1') {
    loadH1Markers().catch((e) => console.warn('h1 markers:', e));
  }
  renderDeskExtras();
}

function scheduleDeskRefresh() {
  if (deskData.refreshTimer) return;
  deskData.refreshTimer = setTimeout(() => {
    deskData.refreshTimer = null;
    loadDeskExtras().catch((e) => console.warn('desk extras:', e));
  }, 800);
}

document.addEventListener('lf-reconciled', () => {
  loadDeskExtras().catch((e) => console.warn('desk reconcile:', e));
  if (state.instrumentId) loadZones().catch((e) => console.warn('zones reconcile:', e));
});

// Точечное обновление цены в строке списка активов без innerHTML-пересборки
function updateDeskAssetPrice(iid, price) {
  if (iid == null || price == null) return;
  const cell = document.querySelector(`.desk-asset[data-iid="${iid}"] .desk-asset-price`);
  if (cell) cell.textContent = fmtPrice(price);
}

function deskOnWs(data) {
  if (!isDeskActive()) return;
  if (data.type === 'price' && data.instrument_id === state.instrumentId) {
    // AC16: тик обновляет только цену и возраст котировки в шапке; карточка
    // сценария и снимок /current перечитываются по смене версии scenario
    renderDeskHead();
  } else if (data.type === 'price') {
    updateDeskAssetPrice(data.instrument_id, data.price);
  }
  // ltf/zone/event/candle: рефетчи идут через доменные версии в handleWsMessage
}

function fillInstrumentSelect() {
  const sel = $('instrument-select');
  if (!sel) return;
  sel.innerHTML = '';
  for (const ins of state.instruments) {
    const opt = document.createElement('option');
    opt.value = ins.id;
    opt.textContent = displayPair(ins.symbol) + (ins.enabled ? '' : ' (выкл.)');
    sel.appendChild(opt);
  }
  if (state.instrumentId) sel.value = String(state.instrumentId);
  renderAssetActiveButton();
}

async function loadInstruments() {
  state.instruments = await api('/api/instruments');
  if (!state.instrumentId) state.instrumentId = resolveInitialInstrument();
  fillInstrumentSelect();
  renderDeskHead();
}

// updateLayers объявлен внутри main(); смена режима зовёт его после чекбоксов.
let refreshLayers = () => {};

// На структуре H1 зоны старших ТФ видны только при «Все ТФ»: текущий ТФ графика
// становится H1, и без этого флага tfMatch отбрасывает D1 и W1. Включаем
// «Все ТФ», «Зоны D1» и «Зоны W1» один раз на вход. Снятие галочек в этом
// режиме сохраняется. Выход в контекст возвращает снимок.
function syncTfLayersForMode() {
  const all = $('tf-all');
  if (!all) return;
  const d1 = $('layer-tf-d1');
  const w1 = $('layer-tf-w1');
  if (state.chartMode === 'h1') {
    if (!state.tfLayersBeforeH1) {
      state.tfLayersBeforeH1 = {
        all: all.checked,
        d1: d1 ? d1.checked : true,
        w1: w1 ? w1.checked : true,
      };
      all.checked = true;
      if (d1) d1.checked = true;
      if (w1) w1.checked = true;
    }
  } else if (state.tfLayersBeforeH1) {
    all.checked = state.tfLayersBeforeH1.all;
    if (d1) d1.checked = state.tfLayersBeforeH1.d1;
    if (w1) w1.checked = state.tfLayersBeforeH1.w1;
    state.tfLayersBeforeH1 = null;
  }
}

function paintChartMode() {
  document.querySelectorAll('[data-chart-mode]').forEach((el) => {
    const on = el.dataset.chartMode === state.chartMode;
    el.classList.toggle('active', on);
    el.setAttribute('aria-pressed', on ? 'true' : 'false');
    if (on) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });
  const tf = $('tf-select');
  if (tf) tf.closest('.tf-switch')?.classList.toggle('hidden', state.chartMode === 'h1');
  const candleLabel = $('candle-tf-label');
  const hidden = $('chart-tf-label');
  if (state.chartMode === 'h1') {
    if (tf) { tf.value = 'H1'; tf.disabled = true; }
    if (candleLabel) candleLabel.textContent = 'Свечи H1';
    if (hidden) hidden.textContent = 'Свечи H1';
  } else {
    if (state.timeframe === 'H1') state.timeframe = state.savedTimeframe || 'D1';
    if (tf) { tf.disabled = false; tf.value = state.timeframe; }
    if (candleLabel) candleLabel.textContent = 'Свечи ' + state.timeframe;
    if (hidden) hidden.textContent = state.timeframe;
  }
  document.querySelectorAll('.h1-layer-controls').forEach((el) => {
    el.classList.toggle('hidden', state.chartMode !== 'h1');
  });
  syncTfLayersForMode();
  refreshLayers();
}

function setChartMode(mode) {
  const next = mode === 'h1' ? 'h1' : 'context';
  if (next === 'h1' && state.chartMode !== 'h1') {
    state.h1UserPanned = false;
    state.h1FrameKey = null;
    state.h1PriceFocus = null;
    state.h1FrameGen = (state.h1FrameGen || 0) + 1;
  }
  if (next === 'h1' && state.timeframe !== 'H1') {
    state.savedTimeframe = state.timeframe || 'D1';
    state.timeframe = 'H1';
  }
  if (next === 'context' && state.chartMode === 'h1') {
    state.timeframe = state.savedTimeframe || 'D1';
  }
  state.chartMode = next;
  const url = new URL(location.href);
  if (next === 'h1') url.searchParams.set('mode', 'h1');
  else url.searchParams.delete('mode');
  history.replaceState(null, '', url.pathname + url.search + url.hash);
  paintChartMode();
}

async function loadH1Markers() {
  if (!state.candleSeries) return;
  const id = state.instrumentId;
  const mode = state.chartMode;
  if (mode !== 'h1' || !id || !window.H1Layers) {
    state.h1Layers = null;
    state.h1LoadError = false;
    state.candleSeries.setMarkers([]);
    hideH1Chrome();
    return;
  }
  const q = window.H1Layers.queryString(h1QueryExtra());
  // A quote or forming candle must not replace an in-flight structure read.
  // Otherwise a slow response is discarded forever by the next refresh.
  const key = JSON.stringify([id, mode, q]);
  if (state.h1Pending && state.h1Pending.key === key) return state.h1Pending.promise;
  const pending = { key };
  state.h1Pending = pending;
  pending.promise = fetchH1Markers(id, mode, q).finally(() => {
    if (state.h1Pending === pending) state.h1Pending = null;
  });
  return pending.promise;
}

async function fetchH1Markers(id, mode, q) {
  const token = ++state.h1Req;
  let layers = null;
  try {
    layers = await api(`/api/ltf/instruments/${id}/structure?${q}`);
    if (token !== state.h1Req || id !== state.instrumentId || state.chartMode !== mode) return;
    state.h1LoadError = false;
    state.h1Layers = layers;
    renderDeskEntries();
  } catch (e) {
    if (token !== state.h1Req || id !== state.instrumentId || state.chartMode !== mode) return;
    state.h1LoadError = true;
    const hadLayers = state.h1Layers?.snapshot?.instrument_id === id;
    state.h1Layers = hadLayers ? state.h1Layers : {
      detected_zones: [],
      structural_events: [],
      layer_status: { zones: { state: 'error', total: 0 } },
      snapshot: {},
    };
    if (!hadLayers) state.candleSeries.setMarkers([]);
    drawZones();
    return;
  }
  state.candleSeries.setMarkers(window.H1Layers.seriesMarkers(
    layers, state.h1SelectedEventId, structurePointColor));
  applyH1LegFrame(layers);
  drawZones();
}

function applyH1LegFrame(layers) {
  const setup = layers && layers.setup;
  if (!state.chart || !state.candles.length || !window.H1Layers) return;
  if (state.h1FrameInstrument !== state.instrumentId) {
    state.h1FrameInstrument = state.instrumentId;
    state.h1UserPanned = false;
    state.h1FrameKey = null;
    state.h1FrameRetried = false;
  }
  const frameKey = setup?.movement_id ?? 'recent';
  if (state.h1UserPanned && state.h1FrameKey === frameKey) return;
  const candles = state.candles;
  const width = Math.max(0, $('chart').clientWidth - state.chart.priceScale('right').width());
  // Несколько дней текущей структуры и место справа для зон отката.
  const frame = window.H1Layers.currentFrame(candles.length, width);
  state.h1ApplyingFrame = true;
  state.h1PriceFocus = null;
  try {
    state.chart.timeScale().setVisibleLogicalRange(frame);
    state.h1FrameKey = frameKey;
  } catch (e) { /* шкала ещё не готова */ }
  const generation = (state.h1FrameGen = (state.h1FrameGen || 0) + 1);
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      if (generation !== state.h1FrameGen) return;
      state.h1ApplyingFrame = false;
      if (state.chartMode !== 'h1') return;
      state.h1FrameRetried = false;
      drawZones();
    });
  });
}

async function reloadAll() {
  const id = state.instrumentId;
  await loadCandles();
  if (id !== state.instrumentId) return;
  await Promise.all([loadZones(), loadEvents(), loadCandidates(), loadH1Markers()]);
  if (id === state.instrumentId && state.chartMode === 'h1' && state.h1Layers && state.h1Layers.snapshot &&
      state.h1Layers.snapshot.state_version != null && state.h1Layers.snapshot.state_version !== state.candleVersion) {
    await loadCandles();
    if (id === state.instrumentId) await loadH1Markers();
  }
}

function initSettingsSections() {
  const nav = document.getElementById('settings-sections');
  if (!nav || nav.dataset.bound) return;
  nav.dataset.bound = '1';
  const known = ['assets', 'notify', 'look', 'rules', 'sources'];
  const show = (id, write) => {
    const pane = known.includes(id) ? id : 'assets';
    document.querySelectorAll('[data-settings-pane]').forEach((el) => {
      el.classList.toggle('is-active', el.dataset.settingsPane === pane);
    });
    nav.querySelectorAll('[data-settings-pane-btn]').forEach((btn) => {
      const on = btn.dataset.settingsPaneBtn === pane;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
    if (!write) return;
    // новая вкладка начинается сверху: контейнер панелей — свой скролл-бокс
    const wrap = document.querySelector('.settings-panes');
    if (wrap) wrap.scrollTop = 0;
    const url = new URL(location.href);
    url.searchParams.set('section', pane);
    history.replaceState(null, '', url.pathname + url.search + url.hash);
  };
  nav.addEventListener('click', (event) => {
    const btn = event.target.closest('[data-settings-pane-btn]');
    if (btn) show(btn.dataset.settingsPaneBtn, true);
  });
  show(new URLSearchParams(location.search).get('section') || 'assets', false);
}

function initAnalysisPanels() {
  const view = $('view-overview'), layout = view.querySelector('.overview-layout');
  view.dataset.mobilePane = 'chart';
  const savedAssets = localStorage.getItem('lf:analysis:assets');
  if (savedAssets === 'closed') view.querySelector('.desk-main').classList.add('assets-hidden');
  const sideVisible = localStorage.getItem('lf:analysis:side') !== 'closed';
  layout.classList.toggle('side-hidden',!sideVisible);
  $('desk-side-toggle').setAttribute('aria-pressed',String(sideVisible));
  $('desk-side-toggle').onclick = () => {
    const visible = !layout.classList.toggle('side-hidden');
    $('desk-side-toggle').setAttribute('aria-pressed',String(visible));
    localStorage.setItem('lf:analysis:side',visible ? 'open' : 'closed');
    if (window.innerWidth < 1024) setPane('scenario');
  };
  $('desk-expand').onclick = () => {
    const expanded = view.classList.toggle('lf-chart-expanded');
    $('desk-expand').setAttribute('aria-pressed',String(expanded));
    $('desk-expand').textContent = expanded ? 'Вернуть панели' : 'Развернуть';
    $('desk-expand').setAttribute('aria-label', expanded ? 'Вернуть панели' : 'Развернуть график');
    $('desk-expand').title = expanded ? 'Вернуть панели' : 'Развернуть график';
  };
  function setPane(pane) {
    view.dataset.mobilePane = pane;
    view.querySelectorAll('[data-analysis-pane]').forEach(button => button.setAttribute('aria-pressed',String(button.dataset.analysisPane === pane)));
    if (pane === 'scenario') layout.classList.remove('side-hidden');
    requestAnimationFrame(drawZones);
  }
  LFUI.analysisTabs(view.querySelector('.lf-analysis-tabs'),setPane);
  const bottom = document.createElement('div'); bottom.className = 'lf-analysis-bottom-tabs';
  bottom.innerHTML = '<button type="button" class="btn small" data-bottom="zones" aria-pressed="true">Зоны сценария</button><button type="button" class="btn small" data-bottom="events" aria-pressed="false">События</button><button type="button" class="btn small" id="analysis-journal">Все события актива</button>';
  layout.after(bottom); view.dataset.bottom = localStorage.getItem('lf:analysis:bottom') || 'zones';
  const paintBottom = () => bottom.querySelectorAll('[data-bottom]').forEach(button => button.setAttribute('aria-pressed',String(button.dataset.bottom === view.dataset.bottom)));
  paintBottom();
  $('desk-entries').addEventListener('click',async event => {
    const button = event.target.closest('[data-analysis-zone]'); if (!button) return;
    const id = button.dataset.analysisZone;
    const initial = deskData.entryZones?.get(id)
      || deskData.current?.eligible_entries?.find(e => String(e.entry_zone_id) === id);
    if (state.chartMode !== 'h1') { setChartMode('h1'); await reloadAll(); }
    const zone = (state.h1Layers?.detected_zones || []).find(z => String(z.id) === id) || (initial ? {...initial,id} : null);
    if (!zone) { LFUI.openDrawer('Зона недоступна','<p>Зона больше не входит в текущую загруженную разметку. Обновите анализ.</p>'); return; }
    focusH1Zone(zone);
    LFUI.openDrawer('Зона H1', H1Layers.zoneCard(zone, null, h1CardFormat()));
    requestAnimationFrame(drawZones);
  });
  bottom.querySelectorAll('[data-bottom]').forEach(button => button.onclick = () => { view.dataset.bottom = button.dataset.bottom; localStorage.setItem('lf:analysis:bottom',button.dataset.bottom); paintBottom(); });
  $('analysis-journal').onclick = () => LFJournal.open({kind:'all',symbol:state.instruments.find(i => i.id === state.instrumentId)?.symbol || '',scope:''});
}

async function main() {
  await ensureToken();
  setupViews();
  initSettingsSections();
  initSettingsDirty();
  const assetsToggle = $('desk-assets-toggle');
  if (assetsToggle) {
    assetsToggle.onclick = () => {
      const desk = document.querySelector('.desk-main');
      if (!desk) return;
      const open = window.innerWidth >= 1440 ? !desk.classList.toggle('assets-hidden') : desk.classList.toggle('assets-open');
      assetsToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
      localStorage.setItem('lf:analysis:assets',open ? 'open' : 'closed');
    };
  }
  initChart();
  initAnalysisPanels();

  const hideInspector = () => {
    showInspector(false);
    clearZoneSelection();
  };
  const assetBtn = $('asset-active-btn');
  if (assetBtn) {
    assetBtn.onclick = () => {
      const ins = state.instruments.find((i) => i.id === state.instrumentId);
      if (ins) setInstrumentActive(ins.id, !ins.enabled);
    };
  }
  $('instrument-select').onchange = (e) => {
    state.instrumentId = Number(e.target.value);
    renderAssetActiveButton();
    state.h1SelectedEventId = null;
    state.h1SelectedZoneId = null;
    state.h1PriceFocus = null;
    hideH1Chrome();
    syncInstrumentContext();
    hideInspector();
    reloadAll();
    loadDeskExtras().catch((e2) => console.warn('desk extras:', e2));
  };
  $('tf-select').onchange = (e) => {
    if (e.target.value === 'H1') setChartMode('h1');
    else {
      state.savedTimeframe = e.target.value;
      state.timeframe = e.target.value;
      if (state.chartMode === 'h1') state.chartMode = 'context';
      paintChartMode();
    }
    hideInspector();
    reloadAll();
  };
  document.querySelectorAll('[data-chart-mode]').forEach((el) => {
    el.addEventListener('click', (ev) => {
      const mode = el.dataset.chartMode;
      if (mode !== 'h1' && mode !== 'context') return;
      ev.preventDefault();
      showView('desk');
      setChartMode(mode);
      reloadAll().catch((err) => console.warn('chart mode:', err));
    });
  });
  const updateLayers = () => {
    state.showAllTf = $('tf-all').checked;
    state.showCandidates = $('show-candidates').checked;
    // чекбоксы конкретных ТФ имеют смысл только в режиме «Все ТФ»:
    // иначе на графике всегда зоны выбранного ТФ
    ['layer-tf-d1', 'layer-tf-w1'].forEach((id) => {
      if ($(id)) $(id).disabled = !state.showAllTf;
    });
    const labels = [state.showAllTf && 'Все таймфреймы', state.showCandidates && 'Кандидаты'].filter(Boolean);
    if ($('layer-count')) $('layer-count').textContent = labels.length ? '· ' + labels.join(' · ') : '';
    drawZones();
  };
  refreshLayers = updateLayers;
  $('tf-all').onchange = updateLayers;
  $('show-candidates').onchange = updateLayers;
  ['layer-tf-d1', 'layer-tf-w1', 'layer-structure-d1', 'layer-history', 'layer-mids', 'layer-labels'].forEach((id) => {
    if ($(id)) $(id).onchange = updateLayers;
  });
  updateLayers();
  if (window.H1Layers) {
    state.h1Settings = window.H1Layers.loadSettings();
    window.H1Layers.bindControls(onH1LayersChange, closeSelectedHtfIdea);
  }
  $('zone-status-filter').onchange = (e) => { state.zoneStatusFilter = e.target.value; renderZonesTable(); };
  $('zone-type-filter').onchange = (e) => { state.zoneTypeFilter = e.target.value || null; renderZonesTable(); };
  $('zone-tf-filter').onchange = (e) => { state.zoneTfFilter = e.target.value || null; renderZonesTable(); };
  if ($('zone-rel-filter')) $('zone-rel-filter').onchange = (e) => { state.zoneRelFilter = e.target.value; renderZonesTable(); };
  document.querySelectorAll('.desk-panel-tabs .panel-tab').forEach((btn) => {
    btn.onclick = () => setDeskPanelTab(btn.dataset.panelTab);
  });
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
    const ms = HTF.parseMskLocal(raw); // datetime-local → московское время (ТЗ §6)
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
  if ($('btn-download-labels')) $('btn-download-labels').onclick = downloadLabels;
  $('draw-cancel').onclick = exitDrawMode;
  $('btn-settings').onclick = () => showView('settings');
  $('settings-save').onclick = saveSettings;
  $('settings-cancel').onclick = async () => {
    // «Сбросить»: откат полей к загруженным значениям, без ухода со страницы
    await openSettings();
    const status = $('settings-status');
    if (status) status.textContent = 'Правки отменены.';
  };
  if ($('settings-close')) {
    $('settings-close').onclick = () => {
      if (settingsDirty() &&
          !window.confirm('Есть неприменённые правки. Закрыть настройки без сохранения?')) return;
      showView('now');
    };
  }
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
    else if ((e.key === 'ArrowRight' || e.key === 'Enter') && reviewState.doneMap.has(reviewState.currentId)) { e.preventDefault(); nextReviewCandidate(); }
  });

  initAppearance();
  LFDataSync.init({
    selected: () => state.instrumentId,
    // §14/AC17: сверка доменных версий статуса → точечные рефетчи изменённых;
    // immediate (terminal job / recovery) дожидается и отложенных рефетчей
    syncStale: async (versions, opts) => {
      await domainSync.syncStatus(versions, opts);
      if (opts && opts.immediate) await domainSync.flush();
    },
    onRecover: () => {
      // возврат на вкладку / reconnect: дешёвая перерисовка после recovery
      // (данные уже свежие — рефетчи шли по версиям и в скрытой вкладке)
      updateLastPrice();
      drawZones();
      if (isDeskActive()) renderDeskHead();
    },
    reload: async () => {
      const saved = state.chart && state.candles.length ? HTF.captureChartRange(state.chart, state.candles) : null;
      const keepUserRange = state.chartMode !== 'h1' || !!state.h1UserPanned;
      const id = state.instrumentId;
      await reloadAll();
      await loadDeskExtras();
      if (window.LFNow) await LFNow.refresh();
      if (state.selectedZoneId) await loadZoneDetail(state.selectedZoneId, false);
      if (id !== state.instrumentId || !state.chart) return;
      if (keepUserRange && saved) HTF.restoreChartRange(state.chart, state.candles, saved);
      else if (state.chartMode === 'h1' && state.h1Layers) applyH1LegFrame(state.h1Layers);
    },
  });
  await loadInstruments();
  syncInstrumentContext();
  if (new URLSearchParams(location.search).get('mode') === 'h1') {
    showView('desk');
    setChartMode('h1');
  } else {
    paintChartMode();
  }
  await loadLabels();
  await reloadAll();
  const initialAt = Number(new URLSearchParams(location.search).get('at'));
  if (initialAt) focusAnalysisTime(initialAt);
  const initialZone = Number(new URLSearchParams(location.search).get('zone'));
  if (initialZone) await loadZoneDetail(initialZone,false);
  connectWs();
  registerWsHandler(deskOnWs);
  loadDeskExtras().catch((e) => console.warn('desk extras:', e));
  // Возраст котировки — каждые 15 с; снимок рабочего места — медленным
  // опросом (свежесть data_state не версионируется), он же резервный канал
  // при потере WebSocket
  setInterval(() => {
    if (!isDeskActive()) return;
    renderDeskQuoteAge();
    scheduleDeskRefresh();
  }, 15000);
}

main().catch((err) => {
  document.body.insertAdjacentHTML('beforeend',
    `<div style="position:fixed;bottom:10px;left:10px;background:#ef5350;color:#fff;padding:8px 14px;border-radius:6px;z-index:200">Ошибка: ${esc(err.message)}</div>`);
});

$('settings-logout')?.addEventListener('click', async () => {
  await HTF.api('/api/auth/session', {method:'DELETE'});
  location.reload();
});
