// Browser acceptance against the isolated redesign_browser_server fixture.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('../tools/node_modules/puppeteer-core');

(async () => {
  const output = path.resolve('tmp/scenario-workspace');
  fs.mkdirSync(output, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true,
  });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', err => errors.push(err.message));
  const report = [];
  try {
    for (const [width, theme, sidebar] of [[1920,'light','expanded'], [1440,'light','expanded'],
      [1440,'light','collapsed'], [1440,'dark','expanded'], [1024,'light','collapsed'],
      [768,'light','collapsed'], [768,'light','expanded'], [390,'light','collapsed'],
      [390,'dark','collapsed'], [360,'light','collapsed']]) {
      await page.setViewport({ width, height: 1000 });
      await page.goto('http://127.0.0.1:8877/?token=dev-token#now', { waitUntil: 'networkidle2' });
      await page.evaluate((t, s) => {
        document.documentElement.dataset.theme = t;
        document.documentElement.dataset.sidebar = s;
        localStorage.setItem('lf:theme', t);
        localStorage.setItem('lf:sidebar', s);
      }, theme, sidebar);
      await page.waitForSelector('#now-tbody tr');
      await page.waitForSelector('#now-market .market-hero-main');
      // Let the navigation color transition finish before visual QA.
      await new Promise(resolve => setTimeout(resolve, 200));
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `now overflow ${width}`);
      assert.equal(await page.$eval('#now-stat-review', el => el.closest('button').dataset.metric), 'awaiting');
      assert(await page.$('#now-focus .now-focus-asset'));
      assert.equal(await page.$$('.sidebar .nav-icon').then(items => items.length), 8);
      assert(await page.$('.mobile-nav a[href="/events.html"]'));
      assert(await page.$('#nav-more-panel a[href$="#review"]'));
      await page.screenshot({ path: path.join(output, `now-${width}-${theme}-${sidebar}.png`) });
      await page.click('#now-tbody .asset-analyze');
      await page.waitForFunction(() => document.querySelector('#view-overview').classList.contains('active'));
      await page.waitForSelector('#desk-scenario .lf-scenario-summary');
      const desk = await page.evaluate(() => {
        const market = document.querySelector('#h1-events-context');
        const panel = document.querySelector('#desk-panel');
        return { inHeader: !!market.closest('.desk-head'), marketInSide: !!market.closest('.desk-side'),
          assetsInToolbar: !!document.querySelector('#desk-assets-toggle').closest('.workbar'),
          overflow: document.documentElement.scrollWidth > innerWidth + 1,
          sideTop: panel.getBoundingClientRect().top, marketTop: market.getBoundingClientRect().top };
      });
      assert(desk.inHeader && !desk.marketInSide && desk.assetsInToolbar);
      assert.equal(desk.overflow, false, `desk overflow ${width}`);
      await page.screenshot({ path: path.join(output, `desk-${width}-${theme}-${sidebar}.png`) });
      await page.click('#desk-assets-toggle');
      const assetVisibility = await page.$eval('#desk-assets', el => el.getBoundingClientRect().height > 0 && getComputedStyle(el).display !== 'none');
      if (width < 1440) assert(assetVisibility, `assets must open ${width}`);
      report.push({ width, theme, sidebar, desk, assetVisibility });
    }
    // Actual SVG/icon navigation on the standalone screens too.
    for (const route of ['events.html', 'alt.html']) {
      await page.setViewport({ width: 1440, height: 1000 });
      await page.goto(`http://127.0.0.1:8877/${route}?token=dev-token`, { waitUntil: 'networkidle2' });
      assert.equal(await page.$$('.sidebar .nav-icon').then(items => items.length), 8, route);
    }
    // Geometry: consumed bands and their outlines end at the correct candle.
    await page.setContent('<div id="overlay" style="width:900px;height:600px"></div>');
    await page.addStyleTag({ path: path.resolve('app/web/static/style.css') });
    await page.addScriptTag({ path: path.resolve('app/web/static/h1_layers.js') });
    const geometry = await page.evaluate(() => {
      const overlay = document.getElementById('overlay');
      const zone = { id: 1, type: 'OB', direction: 'bear', lower: 100, upper: 110,
        formed_at: 20, lifecycle: 'active', tested_slices: [
          { lower: 100, upper: 103, tested_at: 300 }, { lower: 103, upper: 106, tested_at: 600 }] };
      const fragments = H1Layers.zoneFragments(zone);
      H1Layers.draw(overlay, { layers: { detected_zones: [zone] },
        settings: H1Layers.DEFAULTS, xOf: x => x, yOf: y => (115 - y) * 20,
        paneRight: 900, height: 600 });
      return { fragments, bands: [...overlay.querySelectorAll('.h1-zone')].map(el => ({
        right: parseFloat(el.style.left) + parseFloat(el.style.width), tested: el.dataset.tested })) };
    });
    assert.deepEqual(geometry.bands.map(b => b.right), [300,600,900]);
    assert.deepEqual(geometry.bands.map(b => b.tested), ['true','true','false']);
    assert.equal(geometry.fragments.at(-1).lower, 106);
    assert.equal(errors.length, 0, errors.join('\n'));
    fs.writeFileSync(path.join(output, 'verification.json'), JSON.stringify({ report, geometry, errors }, null, 2));
    console.log(JSON.stringify({ ok: true, cases: report.length, geometry, errors, output }));
  } finally { await browser.close(); }
})().catch(err => { console.error(err); process.exitCode = 1; });
