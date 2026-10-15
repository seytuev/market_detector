const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/static/app.js', 'utf8');
const body = source.slice(source.indexOf('async function loadH1Markers()'), source.indexOf('function applyH1LegFrame('));
let calls = 0, resolve, reject, painted = [];
const state = {instrumentId: 1, chartMode: 'h1', candleVersion: 1,
  h1Req: 0, candleSeries: {setMarkers: markers => {painted = markers;}}};
const context = {state, deskData: {current: {state_version: 1}},
  analysisApi: (...args) => context.api(...args),
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
  state.instrumentId = 1; state.h1Layers = {snapshot: {state_version: 50}};
  let candleLoads = 0, structureLoads = 0;
  Object.assign(context, {loadCandles: async () => {candleLoads++;},
    loadZones: async () => {}, loadEvents: async () => {}, loadCandidates: async () => {}});
  vm.runInContext(source.slice(source.indexOf('async function reloadAll()'), source.indexOf('function initSettingsSections()')), context);
  context.loadH1Markers = async () => {structureLoads++;};
  await context.reloadAll();
  assert.equal(candleLoads, 1, 'quote versions must not cause a duplicate candle load');
  assert.equal(structureLoads, 1, 'quote versions must not cause a duplicate structure load');
  let finishCandles;
  context.loadCandles = () => new Promise(resolve => {finishCandles = resolve;});
  const concurrent = context.reloadAll();
  assert.equal(structureLoads, 2, 'structure must load while candle intake is pending');
  finishCandles(); await concurrent;
  context.loadCandles = async () => {throw Object.assign(new Error('instrument changed'), {name:'AbortError'});};
  await assert.doesNotReject(context.reloadAll(), 'cancelled bootstrap still initializes the live feed');
  const current = {instrument_id: 1, versions: {scenario: 7, zones: 3, quote: 9},
    last_closed_h1: 100, last_processed_h1: 100, context_id: 2};
  state.h1Layers = {snapshot: {instrument_id: 1}};
  context.deskData = {reqSeq: 0, current};
  let bundle = {assets: {instruments: []}, view: {...current, versions: {...current.versions, quote: 10}}};
  Object.assign(context, {api: async () => bundle, domainSync: {adopt() {}},
    renderDeskExtras() {}, renderDeskScenario() {}});
  vm.runInContext(source.slice(source.indexOf('async function fetchDeskExtras()'), source.indexOf('function scheduleDeskRefresh()')), context);
  await context.fetchDeskExtras();
  assert.equal(structureLoads, 3, 'price-age polling must preserve the existing chart');
  bundle.view = {...bundle.view, versions: {...bundle.view.versions, scenario: 8}};
  await context.fetchDeskExtras();
  assert.equal(structureLoads, 4, 'a changed scenario must refresh the chart');
  console.log('H1 loading: single flight, visible snapshot preservation, instrument isolation passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
