// Run against the isolated server from tools/bench_ltf_zone_loading.py.
const assert = require('node:assert/strict');
const puppeteer = require('../tools/node_modules/puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
  const base = process.env.ZONE_TEST_URL || 'http://127.0.0.1:8124';
  try {
    for (const legacy of [false, true]) {
      const page = await browser.newPage();
      await page.setViewport({ width: 1440, height: 900 });
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      const path = legacy ? '/ltf.html?stay=1&token=zone-bench-token&instrument=1' : '/?mode=h1&token=zone-bench-token&instrument=1#desk';
      await page.goto(base + path, { waitUntil: 'domcontentloaded' });
      await page.waitForFunction(() => state.h1Layers && state.candles.length && !state.h1Pending && !state.currentPending, { timeout: 60000 });
      const info = await page.evaluate(() => state.h1Layers.zone_selection);
      assert.equal(info.mode, 'current_pd');
      assert.ok(info.above <= 5 && info.below <= 5);
      assert.ok(info.inside > 0);
      const overlay = legacy ? 'ltf-overlay' : 'zone-overlay';
      await page.waitForSelector(`#${overlay} .h1-zone-tag`);
      await page.click(`#${overlay} .h1-zone-tag`);
      await page.waitForFunction(() => state.h1SelectedZoneId != null);
      const zoneId = await page.evaluate(() => state.h1SelectedZoneId);
      await page.evaluate(async old => { if (old) await loadH1Structure(); else await loadH1Markers(); }, legacy);
      assert.ok(await page.evaluate(id => state.h1Layers.detected_zones.some(z => String(z.id) === String(id)), zoneId));
      await page.evaluate(old => {
        window.zoneDrawCalls = 0;
        if (old) {
          const original = drawLtfLayers;
          drawLtfLayers = (...args) => { zoneDrawCalls += 1; return original(...args); };
        } else {
          const original = drawZones;
          drawZones = (...args) => { zoneDrawCalls += 1; return original(...args); };
        }
      }, legacy);
      const box = await page.$eval('#chart', el => {
        const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height };
      });
      await page.mouse.move(box.x + box.width - 20, box.y + box.height / 2);
      await page.mouse.down();
      await page.mouse.move(box.x + box.width - 20, box.y + box.height / 2 + 90, { steps: 8 });
      await page.mouse.up();
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.ok(await page.evaluate(() => zoneDrawCalls > 0), 'Price-axis dragging must redraw overlays');
      // A hover after dragging must not continue drawing the overlay.
      await page.evaluate(() => { zoneDrawCalls = 0; });
      await page.mouse.move(box.x + 100, box.y + 100, { steps: 8 });
      assert.equal(await page.evaluate(() => zoneDrawCalls), 0);
      assert.deepEqual(errors, []);
      await page.screenshot({ path: `tmp/ltf-zone-bench/${legacy ? 'ltf' : 'main'}-after.png` });
      await page.waitForNetworkIdle({ idleTime: 500, timeout: 60000 });
      await page.close();
      console.log(`${legacy ? 'LTF' : 'Main H1'}: selection, pinning, clicks, scaling and hover passed`);
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
