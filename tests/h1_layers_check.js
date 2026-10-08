/* Проверки общего слоя H1 без браузера. Запуск: node tests/h1_layers_check.js */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const root = path.join(__dirname, '..');
const layersPath = path.join(root, 'app', 'web', 'static', 'h1_layers.js');
const ltfPath = path.join(root, 'app', 'web', 'static', 'ltf.js');
const appPath = path.join(root, 'app', 'web', 'static', 'app.js');
const code = fs.readFileSync(layersPath, 'utf8');
const ltf = fs.readFileSync(ltfPath, 'utf8');
const app = fs.readFileSync(appPath, 'utf8');

const store = {};
const sandbox = {
  console,
  URLSearchParams,
  localStorage: {
    getItem: (key) => (Object.prototype.hasOwnProperty.call(store, key) ? store[key] : null),
    setItem: (key, value) => { store[key] = String(value); },
  },
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(code, sandbox);
const H = sandbox.H1Layers;

let failed = 0;
function check(name, cond) {
  if (!cond) {
    failed += 1;
    console.error('FAIL', name);
  } else {
    console.log('ok', name);
  }
}

check('модуль экспортирован', !!H && typeof H.breakPlan === 'function');

const missing = H.breakPlan({
  kind: 'BOS', direction: 'bear', break_level: 10,
  break_candle_open_time: 1_700_000_000_000, occurred_at: 1_700_003_600_000,
}, {});
check('нет опоры — начало не выдумано', missing.originKnown === false && missing.startMs === null);
check('файл слоя не содержит минус 10 свечей', !/-\s*10\s*\*\s*H1/.test(code));
check('ltf.js больше не вычитает 10 свечей', !ltf.includes('break_candle_open_time - 10 * H1_MS'));
check('app.js не подставляет начало из свечи слома', !app.includes('ev.break_candle_open_time || ev.occurred_at);\n    const endMs'));

const pivot = { 7: { id: 7, pivot_at: 1_699_000_000_000 } };
const known = H.breakPlan({
  kind: 'SMS', direction: 'bull', break_level: 12, level_pivot_id: 7,
  break_candle_open_time: 1_700_000_000_000,
}, pivot);
check('известная опора даёт её время', known.originKnown === true && known.startMs === 1_699_000_000_000);
check('подпись BOS и SMS различается', H.caption({ kind: 'BOS', direction: 'bear' }) === 'BOS ↓'
  && H.caption({ kind: 'SMS', direction: 'bull' }) === 'SMS ↑');

check('BSL и SSL — линия', H.zonePlan({ type: 'BSL', lower: 1, upper: 2 }).shape === 'line'
  && H.zonePlan({ type: 'SSL', lower: 1, upper: 1 }).shape === 'line'
  && H.zonePlan({ type: 'OB', lower: 1, upper: 1 }).shape === 'line');
check('OB с диапазоном — прямоугольник', H.zonePlan({ type: 'OB', lower: 98, upper: 102 }).shape === 'rect');

const merged = H.mergeCaption([
  { kind: 'BOS', direction: 'bear' },
  { kind: 'SMS', direction: 'bear' },
  { kind: 'BOS', direction: 'bear' },
]);
check('совпавшие подписи сохраняют оба факта', merged === 'BOS ↓ · SMS ↓ · ещё 1');

const err = H.emptyZoneMessage({ state: 'calculated', total: 3 }, { error: true, visible: 0 });
check('ошибка важнее пустого расчёта', err.text === 'Не удалось загрузить зоны H1' && err.retry === true);
const nodata = H.emptyZoneMessage({ state: 'no_data', reason: 'нет свечей H1' }, {});
check('нет данных называет причину', nodata.text.includes('нет свечей H1'));
const unrated = H.emptyZoneMessage({ state: 'calculated', total: 2 }, {
  eligibleOnly: true, scenarioOpen: false, visible: 0,
});
check('фильтр пригодности без сценария', unrated.text === 'Выбран фильтр пригодности, но сценарий не открыт' && unrated.showAll === true);
const hidden = H.emptyZoneMessage({ state: 'calculated', total: 4 }, {
  hiddenByFilter: 4, visible: 0, scenarioOpen: true,
});
check('скрыто фильтрами', hidden.text === 'Скрыто фильтрами: 4 зон' && hidden.reset === true);
const outside = H.emptyZoneMessage({ state: 'calculated', total: 2 }, { outside: 2, visible: 0 });
check('вне экрана', outside.text === '2 зон вне видимой области');
const none = H.emptyZoneMessage({ state: 'calculated', total: 0 }, { visible: 0 });
check('расчёт окончен и зон нет', none.text === 'На выбранном участке подтверждённые зоны H1 не найдены');

store['lf:h1-layers'] = JSON.stringify({ breaks: false });
const restored = H.loadSettings();
check('явное выключение BOS сохраняется', restored.breaks === false && restored.zones === true);
check('новый ключ получает значение по умолчанию', restored.points === 'recent' && restored.htfContext === true);

const recent = H.queryString({ from: 10, to: 20, context_id: 5 });
check('последние 20 не отправляют окно', recent.includes('points=recent') && !recent.includes('from=') && !recent.includes('to=') && recent.includes('context_id=5'));
H.saveSettings({ points: 'history', diagnostic: true });
const history = H.queryString({ from: 10, to: 20 });
check('история отправляет окно и диагностику', history.includes('points=history') && history.includes('from=10') && history.includes('to=20') && history.includes('diagnostic=true'));

const events = H.eventSource({
  structural_events: [],
  structure_events: [{ id: 'scenario-only' }],
});
check('пустой список инструмента не подменяется сценарием', events.length === 0);
check('старый ответ без поля читает события сценария', H.eventSource({ structure_events: [{ id: 1 }] }).length === 1);

H.saveSettings({ points: 'hidden' });
const markers = H.markerList({
  pivot_markers: [{ id: 1, role: 'HH', pivot_at: 1 }],
  structural_events: [{ id: 'e1', level_pivot_id: 9 }],
  anchor_refs: [{ id: 9, role: 'HL', pivot_at: 2, kind: 'low' }],
}, 'e1');
check('скрытые точки не возвращают двадцать, выбранная опора временная', markers.length === 1 && markers[0].temporary === true && markers[0].id === 9);

if (failed) {
  console.error('failed', failed);
  process.exit(1);
}
console.log('h1 layers ok');
