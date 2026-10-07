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

if (failed) {
  console.error(failed + ' failed');
  process.exit(1);
}
console.log('all ok');
