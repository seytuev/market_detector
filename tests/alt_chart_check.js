/* Проверки проекции графика альткоинов. Запуск: node tests/alt_chart_check.js */
const path = require('path');
const ac = require(path.join(__dirname, '..', 'app', 'web', 'static', 'alt_chart.js'));

const DAY = ac.DAY_MS;
const AS_OF = 1_800_000_000_000;
let failed = 0;

function check(name, cond) {
  if (!cond) {
    failed += 1;
    console.error('FAIL', name);
  } else {
    console.log('ok', name);
  }
}

function struct(id, kind, open, extra) {
  return Object.assign({
    id, kind, level_price: 1, close_price: 1.1, candle_open_time: open,
    source_event_id: kind.toLowerCase() + ':7:' + open,
    historical: false, anchors: {},
  }, extra || {});
}

function life(id, type, open, source) {
  return {
    id, event_type: type, event_time_ms: open + DAY,
    source_event_id: source || (type + ':7:' + open),
    payload: { candle_open_time: open },
  };
}

function view(extra) {
  return Object.assign({
    mode: 'setup', asOfMs: AS_OF, setupId: 7,
    visibleFromMs: AS_OF - 200 * DAY, visibleToMs: AS_OF,
    includeReverse: false, selectedEventKey: null, historyTypes: null,
  }, extra || {});
}

const oldOpen = AS_OF - 400 * DAY;
const freshBos = AS_OF - 10 * DAY;
const freshSms = AS_OF - 5 * DAY;
const freshSsl = AS_OF - 2 * DAY;
const structures = [struct(1, 'BOS', oldOpen)];
for (let i = 0; i < 300; i += 1) {
  structures.push(struct(1000 + i, 'BOS', oldOpen - (i + 1) * DAY));
}
structures.push(struct(2, 'BOS', freshBos));
structures.push(struct(3, 'SMS', freshSms));
structures.push(struct(4, 'SSL', freshSsl));
structures.push(struct(5, 'BOS_REV', freshBos));

const detail = {
  setup_id: 7,
  as_of_ms: AS_OF,
  structure_events: structures,
  events: [life(50, 'bos_confirmed', oldOpen, 'bos:7:' + oldOpen)],
  confirmation: {
    event_id: 50, event_type: 'bos_confirmed', event_time_ms: oldOpen + DAY,
    source_event_id: 'bos:7:' + oldOpen, candle_open_time: oldOpen,
    structure_link: { status: 'exact', structure_event_id: 1 },
  },
  entries: [],
  breakout: null,
};

const normalized = ac.normalizeDetail(detail);
const oldFact = normalized.events.find((event) => event.details.structure_id === 1 || event.key === 'fact:bos:7:' + oldOpen);
check('C04/C06 одно подтверждение — один факт', oldFact && oldFact.roles.indexOf('confirmation') >= 0 && oldFact.roles.indexOf('structure') >= 0);
check('C04 historical не обязателен для скрытия', oldFact.importance === 'key');

const selected = ac.selectChartEvents(normalized.events, view());
const markerTypes = selected.markers.map((event) => event.type).sort();
check('C02 на графике только свежие BOS/SMS/SSL', markerTypes.join(',') === 'BOS,SMS,SSL');
check('C02 старое подтверждение сохранено вне окна', selected.offscreenKeys.some((event) => event.key === oldFact.key));
check('C05 обратный BOS скрыт', !selected.markers.some((event) => event.type === 'BOS_REV'));

const withReverse = ac.selectChartEvents(normalized.events, view({ includeReverse: true }));
check('C05 обратный BOS показывается как обратная структура',
  withReverse.markers.some((event) => event.type === 'BOS_REV' && event.label === 'BOS обр.'));

const future = ac.normalizeDetail({
  setup_id: 7, as_of_ms: AS_OF,
  structure_events: [struct(9, 'BOS', AS_OF)],
  events: [], entries: [],
});
const futureSel = ac.selectChartEvents(future.events, view());
check('C12 событие позже as_of скрыто', futureSel.markers.length === 0 && futureSel.offscreenKeys.length === 0);

const noAsOf = ac.selectChartEvents(normalized.events, view({ asOfMs: null }));
check('без as_of маркеры не строятся по часам компьютера', noAsOf.markers.length === 0 && noAsOf.freshnessKnown === false);

const other = ac.normalizeDetail({
  setup_id: 8, as_of_ms: AS_OF,
  structure_events: [struct(1, 'BOS', freshBos)],
  events: [], entries: [],
});
check('C13 чужой сетап отфильтрован', ac.selectChartEvents(other.events, view()).markers.length === 0);

const histDetail = {
  setup_id: 7, as_of_ms: AS_OF, historical: true,
  structure_events: [Object.assign(struct(1, 'BOS', freshBos), { historical: true })],
  events: [life(50, 'bos_confirmed', freshBos, 'bos:7:' + freshBos)],
  confirmation: {
    event_id: 50, event_type: 'bos_confirmed', event_time_ms: freshBos + DAY,
    source_event_id: 'bos:7:' + freshBos, candle_open_time: freshBos,
    structure_link: { status: 'unknown', structure_event_id: null },
  },
  entries: [],
};
const histNorm = ac.normalizeDetail(histDetail);
const histFact = histNorm.events.find((event) => event.type === 'BOS');
check('C04 historical=true не прячет основание', histFact.importance === 'key' && histFact.details.historical === true);
check('нет связи — статус неизвестен, факт на месте', histFact.details.structure_link === 'unknown');

const candle = freshBos;
const same = ac.normalizeDetail({
  setup_id: 7, as_of_ms: AS_OF,
  structure_events: [
    struct(1, 'BOS', candle),
    struct(2, 'SMS', candle),
  ],
  events: [
    life(10, 'bos_confirmed', candle, 'bos:7:' + candle),
    life(11, 'bos_confirmed', candle, 'bos:7:' + candle),
    life(12, 'sms_confirmed', candle, 'sms:7:' + candle),
    {
      id: 13, event_type: 'entry_a', event_time_ms: candle + DAY,
      source_event_id: 'entry_a:7', payload: {},
    },
  ],
  confirmation: {
    event_id: 10, event_type: 'bos_confirmed', event_time_ms: candle + DAY,
    source_event_id: 'bos:7:' + candle, candle_open_time: candle,
    structure_link: { status: 'exact', structure_event_id: 1 },
  },
  entries: [{ id: 3, kind: 'A', event_time_ms: candle + DAY, price: 2 }],
});
const bosFacts = same.events.filter((event) => event.type === 'BOS');
check('C06 дубликат BOS — один факт', bosFacts.length === 1 && bosFacts[0].source_ids.indexOf('event:10') >= 0 && bosFacts[0].source_ids.indexOf('event:11') >= 0);
const grouped = ac.groupMarkers(same.events.filter((event) => event.importance === 'key' || event.type === 'SMS').map((event) => ({
  event, x: 100, position: event.position,
})), { measure: () => 40, gap: 8 });
const below = grouped.filter((group) => group.position === 'belowBar');
check('C06 одна группа и +2 без завышения дубля', below.length === 1 && below[0].count === 3 && below[0].label.indexOf('+2') >= 0);

const candles = [];
for (let i = 0; i < 400; i += 1) candles.push({ open_time: AS_OF - (400 - i) * DAY });
const initial = ac.initialTimeRange(candles, AS_OF);
check('C03 начальный вид — последние 180 D1', initial.fromMs === candles[220].open_time && initial.toMs === candles[399].open_time);
const jumpMissing = ac.jumpTimeRange(oldOpen, candles.slice(-180));
check('C14 нет соседней привязки', jumpMissing.missing === true && jumpMissing.fromMs == null);

const targets = [
  { tp: 1, price: 5, hit: true, passed_at_confirmation: false },
  { tp: 2, price: 8, hit: false, passed_at_confirmation: true },
  { tp: 3, price: 9, hit: false, passed_at_confirmation: false },
  { tp: 4, price: 40, hit: false, passed_at_confirmation: false },
];
check('C16 ближайшая — первая не пройденная над close', ac.nearestTarget(targets, 8.5).target.tp === 3);
check('C16 нет цели над ценой', ac.nearestTarget([{ tp: 1, price: 3, hit: false, passed_at_confirmation: false }], 8.5).target == null);

const scale = ac.priceRange([{ time: 1, low: 10, high: 12 }], null, null);
check('C09 шкала не уходит ниже нуля и не включает далёкую цель', scale.min >= 0 && scale.max < 20);
check('K<=0 не получает край', ac.levelPlacement(0, scale) === 'hidden');
check('далёкая цель — верхний край', ac.levelPlacement(40, scale) === 'above');

const retest = ac.retestSpan({
  flags: { retest_received: true },
  breakout: { closed_at: candle + DAY, retest_deadline_ms: candle + 20 * DAY },
  events: [{ id: 1, event_type: 'retest', event_time_ms: candle + 4 * DAY, payload: { candle_open_time: candle + 3 * DAY } }],
}, candle + 100 * DAY);
check('C10 ретест заканчивается на принятой свече', retest.endMs === candle + 4 * DAY);

const manip = ac.manipulationEpisodes([
  { id: 1, started_candle_open_time: AS_OF - 500 * DAY, ended_candle_open_time: AS_OF - 490 * DAY, min_price: 1 },
], 'setup', AS_OF);
check('C11 эпизод вне 180 дней не выбран', manip.length === 0);
check('область вне экрана не клиппится в полный график', ac.intervalOnScreen(1, 2, 10, 20) == null);

const sparse = [];
for (let i = 0; i < 30; i += 1) {
  sparse.push({
    event: {
      key: 'e' + i, type: 'BOS', label: 'BOS', importance: 'secondary', roles: ['structure'],
      candle_open_time_ms: i * DAY, available_at_ms: i * DAY, source_id: i, position: 'aboveBar',
    },
    x: i * 30,
    position: 'aboveBar',
  });
}
const budgeted = ac.groupMarkers(sparse, { measure: () => 10, gap: 8, extraBudget: 24 });
const keys = new Set();
budgeted.forEach((group) => group.eventKeys.forEach((key) => keys.add(key)));
check('C07 бюджет 24 кластера и все факты внутри', budgeted.length === 24 && keys.size === 30);

const started = Date.now();
const many = [];
for (let i = 0; i < 3000; i += 1) {
  many.push({
    event: {
      key: 'm' + i, type: 'BOS', label: 'BOS', importance: 'secondary', roles: ['structure'],
      candle_open_time_ms: i * DAY, available_at_ms: i * DAY, source_id: i, position: 'aboveBar',
    },
    x: (i % 200) * 5,
    position: 'aboveBar',
  });
}
ac.groupMarkers(many, { measure: () => 12, gap: 8, extraBudget: 24 });
const elapsed = Date.now() - started;
console.log('group 3000 events ms', elapsed);
check('группировка 3000 событий завершается', elapsed < 1000);

/* UI-01: агрегация W1 из D1, недели с понедельника 00:00 UTC. */
const MON = Date.UTC(2026, 8, 7); // понедельник
check('W1: понедельник остаётся понедельником', ac.weekStartMs(MON) === MON);
check('W1: среда уходит на понедельник', ac.weekStartMs(MON + 2 * DAY) === MON);
check('W1: воскресенье уходит на понедельник той же недели', ac.weekStartMs(MON + 6 * DAY) === MON);
check('W1: следующий понедельник — новая неделя', ac.weekStartMs(MON + 7 * DAY) === MON + 7 * DAY);

function d1(n, o, h, l, c, v) {
  return { open_time: MON + n * DAY, open: o, high: h, low: l, close: c, volume: v };
}
const d1seq = [];
for (let i = 0; i < 7; i += 1) d1seq.push(d1(i, 10 + i, 20 + i, 5 + i, 15 + i, 100 + i));
[7, 8, 10, 11, 12, 13].forEach((n, j) => d1seq.push(d1(n, 30 + j, 40 + j, 25 + j, 35 + j, 10)));
[14, 15, 16].forEach((n, j) => d1seq.push(d1(n, 50 + j, 60 + j, 45 + j, 55 + j, 1 + j)));
const w1 = ac.aggregateW1(d1seq);
check('W1: три недели из 16 свечей', w1.length === 3);
check('W1: OHLCV первой недели совпадает с ручной агрегацией',
  w1[0].open_time === MON && w1[0].open === 10 && w1[0].high === 26 && w1[0].low === 5 &&
  w1[0].close === 21 && w1[0].volume === 721);
check('W1: OHLCV второй недели по имеющимся свечам',
  w1[1].open_time === MON + 7 * DAY && w1[1].open === 30 && w1[1].high === 45 &&
  w1[1].low === 25 && w1[1].close === 40 && w1[1].volume === 60);
check('W1: полная первая неделя без флагов', w1[0].partial === false && w1[0].gaps === false);
check('W1: пропуск D1 внутри недели помечен', w1[1].gaps === true && w1[1].partial === false);
check('W1: незавершённая последняя неделя', w1[2].partial === true && w1[2].gaps === false);
check('W1: неделя до воскресенья не считается неполной',
  ac.aggregateW1(d1seq.slice(0, 7))[0].partial === false);
check('W1: пустой вход — пустой выход', ac.aggregateW1([]).length === 0);

/* Слои как данные: уровни и области для экрана и экспорта. */
const layerDetail = {
  frozen_range: { lower: 10, upper: 20, mid: 15 },
  anchors: { start: { open_time: MON } },
  as_of_ms: MON + 40 * DAY,
  manipulation_episodes: [
    { id: 1, started_candle_open_time: MON + 5 * DAY, ended_candle_open_time: null, min_price: 8 },
  ],
  targets: [
    { tp: 1, price: 30, hit: false, passed_at_confirmation: false },
    { tp: 2, price: 25, hit: false, passed_at_confirmation: false },
  ],
  cancel: { price: 5 },
  events: [],
};
const lvl = ac.collectLevels(layerDetail, { range: true, targets: 'nearest', cancel: true }, 18);
check('слои: L/U/M/TP/K собраны',
  lvl.levels.map((l2) => l2.name).join(',') === 'L,U,M,TP2,K' && lvl.targetNote === '');
check('слои: cls уровней', lvl.levels[0].cls === 'range' && lvl.levels[3].cls === 'tp' && lvl.levels[4].cls === 'k');
const lvlNoClose = ac.collectLevels(layerDetail, { range: true, targets: 'nearest', cancel: true }, null);
check('слои: без закрытия TP нет, причина явная',
  lvlNoClose.levels.length === 4 && lvlNoClose.targetNote === 'Нет цены закрытия D1');
const lvlAll = ac.collectLevels(layerDetail, { range: false, targets: 'all', cancel: false }, 18);
check('слои: режим «все цели» без L/U/M и K',
  lvlAll.levels.map((l2) => l2.name).join(',') === 'TP1,TP2');
const bx = ac.collectBoxes(layerDetail, {
  range: true, manipulation: true, entries: false, eventMode: 'setup',
}, MON + 30 * DAY);
check('слои: область накопления и активная манипуляция',
  bx.boxes.length === 2 &&
  bx.boxes[0].kind === 'range' && bx.boxes[0].startMs === MON && bx.boxes[0].endMs === MON + 31 * DAY &&
  bx.boxes[1].kind === 'manip' && bx.boxes[1].upper === 10 && bx.boxes[1].lower === 8);
const bxRetest = ac.collectBoxes({
  frozen_range: { lower: 10, upper: 20, mid: 15 },
  breakout: { closed_at: MON + 10 * DAY, retest_deadline_ms: null },
  flags: {},
  events: [],
}, { range: false, manipulation: false, entries: true, eventMode: 'setup' }, null);
check('слои: ретест без границы — причина вместо области',
  bxRetest.boxes.length === 0 && /не задана/.test(bxRetest.retestNote));

function shelfBar(i, low, high, close) {
  return { open_time: MON + i * DAY, open: close, low, high, close };
}
const shelfCandles = [];
for (let i = 0; i < 40; i += 1) shelfCandles.push(shelfBar(i, 10, 14, 12));
shelfCandles[3].low = 8;
for (let i = 40; i < 55; i += 1) shelfCandles.push(shelfBar(i, 18, 40, 30));
const shelfDetail = {
  frozen_range: { lower: 8, upper: 90, mid: 49 },
  anchors: { start: { open_time: MON } },
  candles: shelfCandles,
  manipulation_episodes: [
    { id: 1, started_candle_open_time: MON + 3 * DAY, ended_candle_open_time: null, min_price: 8 },
  ],
};
const shelfShown = ac.chartRange(shelfDetail, MON + 54 * DAY);
check('база у дна: пол около 10, потолок около 14, без импульса',
  shelfShown.shelf === true &&
  shelfShown.lower === 10 && shelfShown.upper === 14 &&
  shelfShown.endMs === MON + 40 * DAY);
const shelfBoxes = ac.collectBoxes(shelfDetail, {
  range: true, manipulation: true, entries: false, eventMode: 'setup',
}, MON + 54 * DAY);
check('заливка базы короче импульса, вынос стыкуется с полом',
  shelfBoxes.boxes[0].kind === 'range' && shelfBoxes.boxes[0].endMs === MON + 40 * DAY &&
  shelfBoxes.boxes[0].lower === 10 && shelfBoxes.boxes[0].upper === 14 &&
  shelfBoxes.boxes[1].upper === 10 && shelfBoxes.boxes[1].lower === 8);
const shelfLevels = ac.collectLevels(shelfDetail, { range: true, targets: 'nearest', cancel: false }, 12);
check('линии L/U/M совпадают с базой у дна',
  shelfLevels.levels.map((level) => level.name + ':' + level.price).join(',') === 'L:10,U:14,M:12');
const breakOnly = ac.collectBoxes({
  frozen_range: { lower: 10, upper: 20, mid: 15 },
  anchors: { start: { open_time: MON } },
  breakout: { closed_at: MON + 12 * DAY },
}, { range: true, manipulation: false, entries: false }, MON + 30 * DAY);
check('без свечей заливка всё равно кончается на выходе',
  breakOnly.boxes.length === 1 && breakOnly.boxes[0].endMs === MON + 12 * DAY &&
  breakOnly.boxes[0].lower === 10 && breakOnly.boxes[0].shelf === false);

/* UI-04: модель сцены в пикселях с подставными преобразованиями. */
const pxMap = {
  mapTime: (ms) => Math.round(ms / DAY) * 10,
  mapPrice: (p) => 300 - p,
};
const scene = ac.buildScene({
  viewFromMs: 10 * DAY, viewToMs: 20 * DAY,
  candles: [], mapTime: pxMap.mapTime, mapPrice: pxMap.mapPrice,
  priceRange: { min: 0, max: 200 }, chartHeight: 300, paneWidth: 400,
  boxes: [
    { kind: 'range', startMs: 12 * DAY, endMs: 18 * DAY, upper: 100, lower: 50 },
    { kind: 'manip', startMs: 25 * DAY, endMs: 27 * DAY, upper: 100, lower: 50 },
  ],
  lines: [
    { name: 'L', price: 100, cls: 'range' },
    { name: 'U', price: 120, cls: 'range' },
    { name: 'TP1', price: 105, cls: 'tp' },
    { name: 'K', price: 500, cls: 'k' },
  ],
  markers: [
    { x: 50, price: 100, position: 'aboveBar', label: 'выход', hasKey: true },
    { x: 60, price: 90, position: 'belowBar', label: 'BOS', hasKey: false },
  ],
});
check('сцена: область в виде — rect, вне вида — пропущена', scene.rects.length === 1 &&
  scene.rects[0].x === 120 && scene.rects[0].width === 60 &&
  scene.rects[0].y === 200 && scene.rects[0].height === 50);
check('сцена: близкие уровни группируются, дальний K пропущен', scene.lines.length === 2 &&
  scene.lines[0].label === 'U 120' && scene.lines[1].label === '2 уровня' &&
  scene.lines[1].kind === 'tp');
check('сцена: маркеры над/под свечой', scene.markers.length === 2 &&
  scene.markers[0].y === 180 && scene.markers[0].key === true &&
  scene.markers[1].y === 214 && scene.markers[1].position === 'belowBar');
check('сцена: области клиппятся видимым диапазоном', ac.buildScene({
  viewFromMs: 10 * DAY, viewToMs: 20 * DAY,
  candles: [], mapTime: pxMap.mapTime, mapPrice: pxMap.mapPrice,
  priceRange: { min: 0, max: 200 }, chartHeight: 300,
  boxes: [{ kind: 'range', startMs: 8 * DAY, endMs: 22 * DAY, upper: 100, lower: 50 }],
  lines: [], markers: [],
}).rects[0].x === 100);
check('сцена: уровень вне ценовой шкалы не рисуется', ac.buildScene({
  viewFromMs: null, viewToMs: null,
  candles: [], mapTime: pxMap.mapTime, mapPrice: pxMap.mapPrice,
  priceRange: { min: 0, max: 200 }, chartHeight: 300,
  boxes: [], lines: [{ name: 'TP4', price: 300, cls: 'tp' }], markers: [],
}).lines.length === 0);

if (failed) {
  console.error(failed + ' failed');
  process.exit(1);
}
console.log('all ok');
