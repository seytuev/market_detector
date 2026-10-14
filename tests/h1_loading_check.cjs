const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/static/app.js', 'utf8');
const body = source.slice(source.indexOf('async function loadH1Markers()'), source.indexOf('function applyH1LegFrame('));
let calls = 0, resolve, reject, painted = [];
const state = {instrumentId: 1, chartMode: 'h1', candleVersion: 1,
  h1Req: 0, candleSeries: {setMarkers: markers => {painted = markers;}}};
const context = {state, deskData: {current: {state_version: 1}},
  window: {H1Layers: {queryString: () => 'points=recent', seriesMarkers: layers => layers.markers}},
  h1QueryExtra: () => ({}), api: () => {calls++; return new Promise((ok, fail) => {resolve = ok; reject = fail;});},
  structurePointColor: () => '', renderDeskEntries() {}, applyH1LegFrame() {}, drawZones() {}, hideH1Chrome() {}};
vm.createContext(context); vm.runInContext(body, context);
(async () => {
  const first = context.loadH1Markers();
  state.candleVersion++;
  context.deskData.current.state_version++;
  const second = context.loadH1Markers();
  assert.equal(calls, 1, 'quote refresh must share the pending request');
  resolve({markers: ['BOS'], detected_zones: [{id: 1}], snapshot: {instrument_id: 1}});
  await Promise.all([first, second]);
  assert.deepEqual(painted, ['BOS']);
  const failed = context.loadH1Markers(); reject(new Error('temporary network error')); await failed;
  assert.deepEqual(painted, ['BOS'], 'temporary failure must preserve visible structure');
  assert.equal(state.h1Layers.detected_zones.length, 1);
  const switched = context.loadH1Markers(); state.instrumentId = 2; resolve({markers: ['old asset']}); await switched;
  assert.deepEqual(painted, ['BOS'], 'response for another instrument must not apply');
  console.log('H1 loading: single flight, visible snapshot preservation, instrument isolation passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
