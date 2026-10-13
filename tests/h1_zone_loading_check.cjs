// Real chart/browser check against the isolated fixture, never the user's DB.
const assert = require('node:assert/strict');
const puppeteer = require('../tools/node_modules/puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setViewport({ width: 1440, height: 900 });
    await page.goto((process.env.SYNC_TEST_URL || 'http://127.0.0.1:8101') + '/?token=dev-token&instrument=1#desk', { waitUntil: 'networkidle2' });
    await page.waitForFunction(() => state.chart && state.candles.length);
    const result = await page.evaluate(async () => {
      setChartMode('h1');
      await loadCandles();
      const formed = state.candles[5].time * 1000;
      const stub = { id: 901, type: 'FVG', direction: 'bull', timeframe: 'H1', lower: 99.2, upper: 99.7,
        formed_at: formed, confirmed_at: formed + 3600000, lifecycle: 'active', idea_links: [] };
      const original = HTF.api;
      let structureRequests = 0;
      HTF.api = async (url, options) => {
        if (!url.includes('/structure?')) return original(url, options);
        structureRequests += 1;
        await new Promise(resolve => setTimeout(resolve, 100));
        return { detected_zones: [stub], structural_events: [], setup: { movement_id: 1, frame_from: formed,
            origin_anchor: { at: formed }, direction: 'bull', lower: 99, upper: 101, eq: 100, range_status: 'confirmed' } }
      };
      state.zones = [{ id: 11, type: 'ob', direction: 'bull', timeframe: 'D1', lower: 98, upper: 100,
        formed_at: formed, status: 'active', relevant: true, mid: 99 }];
      state.groups = [];
      document.getElementById('tf-all').checked = true;
      refreshLayers();
      await Promise.all([loadH1Markers(), loadH1Markers(), loadH1Markers()]);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const beforeToggle = document.querySelectorAll('#zone-overlay .h1-zone').length;
      document.getElementById('tf-all').checked = false;
      refreshLayers();
      const afterToggle = document.querySelectorAll('#zone-overlay .h1-zone').length;
      return { beforeToggle, afterToggle, structureRequests };
    });
    console.log(JSON.stringify({ result, errors }));
    assert.ok(result.beforeToggle > 0, 'H1 zones must render while All timeframes is enabled');
    assert.equal(result.beforeToggle, result.afterToggle);
    assert.equal(result.structureRequests, 1, 'Concurrent H1 refreshes must share one request');
    const label = await page.$('.h1-pd-label');
    assert.equal(await page.evaluate(el => getComputedStyle(el).opacity, label), '0.25');
    await label.hover();
    await page.waitForFunction(() => getComputedStyle(document.querySelector('.h1-pd-label')).opacity === '1');
    await page.mouse.move(0, 0);
    await page.waitForFunction(() => getComputedStyle(document.querySelector('.h1-pd-label')).opacity === '0.25');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
