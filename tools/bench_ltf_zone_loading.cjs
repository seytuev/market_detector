// Real LTF page against bench_ltf_zone_loading.py; identical snapshot per variant.
const fs = require('node:fs');
const assert = require('node:assert/strict');
const puppeteer = require('./node_modules/puppeteer-core');

(async () => {
  const label = process.argv[2] || 'after';
  const base = process.argv[3] || 'http://127.0.0.1:8123';
  const mainPage = process.argv[4] === 'main';
  const overlayId = mainPage ? 'zone-overlay' : 'ltf-overlay';
  const browser = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
  const results = [];
  try {
    for (let run = 0; run < 5; run++) {
      const page = await browser.newPage();
      await page.setViewport({ width: 1440, height: 900 });
      await page.setRequestInterception(true);
      page.on('request', request => {
        if (request.url().startsWith('http') && !request.url().startsWith(base)) request.abort();
        else request.continue();
      });
      const errors = [];
      page.on('pageerror', e => errors.push(e.message));
      const responses = [];
      page.on('response', response => {
        if (!response.url().includes('/workspace?') && !response.url().includes('/structure?')) return;
        responses.push(response.text().then(body => ({ path: new URL(response.url()).pathname, bytes: Buffer.byteLength(body), status: response.status() })));
      });
      const path = mainPage ? '/?mode=h1&token=zone-bench-token&instrument=1#desk' : '/ltf.html?stay=1&token=zone-bench-token&instrument=1';
      await page.goto(base + path, { waitUntil: 'domcontentloaded', timeout: 180000 });
      await page.waitForFunction(() => state.candles.length && state.h1Layers && !state.currentPending && !state.h1Pending, { timeout: 180000 });
      const result = await page.evaluate(async id => {
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        return { load_ms: performance.now(), zones: state.h1Layers.detected_zones.length,
          zone_elements: document.querySelectorAll(`#${id} .h1-zone, #${id} .h1-liquidity`).length,
          selection: state.h1Layers.zone_selection || null,
          pd: { lower: state.h1Layers.setup?.lower, upper: state.h1Layers.setup?.upper },
          api_timings: performance.getEntriesByType('resource').filter(r => r.name.includes('/api/'))
            .map(r => ({ path: new URL(r.name).pathname, duration_ms: r.duration })),
        };
      }, overlayId);
      const box = await page.$eval('#chart', el => {
        const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, width: r.width, height: r.height };
      });
      await page.evaluate(id => {
        window.zoneMutationCount = 0;
        window.zoneObserver = new MutationObserver(records => { window.zoneMutationCount += records.filter(r => r.type === 'childList').length; });
        zoneObserver.observe(document.getElementById(id), { childList: true, subtree: true });
      }, overlayId);
      const started = performance.now();
      for (let step = 0; step < 40; step++) await page.mouse.move(box.x + 20 + step * (box.width - 110) / 40, box.y + box.height * 0.4);
      result.mouse_ms = performance.now() - started;
      result.mouse_dom_mutations = await page.evaluate(() => { zoneObserver.disconnect(); return zoneMutationCount; });
      result.workspace = await Promise.all(responses);
      result.errors = errors;
      assert.deepEqual(errors, []);
      if (label.startsWith('after')) assert.equal(result.mouse_dom_mutations, 0, 'Hover must not rebuild zone DOM');
      results.push(result);
      console.log(JSON.stringify({ run, ...result }));
      // Let background asset/inspector reads finish before the next trial;
      // closing the page does not cancel an already running Python handler.
      await page.waitForNetworkIdle({ idleTime: 500, timeout: 60000 });
      await page.close();
    }
    fs.writeFileSync(`tmp/ltf-zone-bench/${label}.json`, JSON.stringify({ label, page: mainPage ? 'main_h1' : 'legacy_ltf', viewport: [1440, 900], results }, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
