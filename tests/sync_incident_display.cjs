const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const path = require('path');
const source = fs.readFileSync(path.join(__dirname, '../app/web/static/data_sync.js'), 'utf8');
async function render(channels, recovery, jobStatus = 'partial') {
  const elements = Object.fromEntries(['btn-data-sync', 'data-sync-status', 'data-sync-details'].map(id => [id, {
    dataset: {}, setAttribute() {}, replaceChildren() {}, append() {}
  }]));
  const status = {available: true, channels, recovery, job: {
    status: jobStatus, finished_at: 1792093147213, channels_total: 16,
    results: [{status: 'lagging', source_status: 'ok'}]
  }};
  const context = {window: {}, document: {
    getElementById: id => elements[id], createElement: () => ({dataset: {}})
  }, HTF: {api: async () => status}, Date, setTimeout: () => 0, clearTimeout() {}};
  vm.runInNewContext(source, context);
  await context.window.LFDataSync.readStatus();
  return elements['data-sync-status'];
}
(async () => {
  const current = Array.from({length: 16}, () => ({status: 'ok'}));
  const ready = await render(current, []);
  assert.match(ready.textContent, /Данные актуальны: 16 из 16/);
  assert.equal(ready.dataset.state, 'succeeded');
  assert.match((await render(current, [], 'succeeded')).textContent, /Данные обновлены в/);
  const recovery = await render(current, [{instrument_id: 2}]);
  assert.match(recovery.textContent, /расчёт LTF продолжается/);
  assert.doesNotMatch(recovery.textContent, /0 из 16/);
  const failed = await render([{status: 'lagging'}], []);
  assert.match(failed.textContent, /Последняя попытка в/);
})().catch(error => {console.error(error); process.exitCode = 1;});
