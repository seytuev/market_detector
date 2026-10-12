/* Run against tests.data_sync_browser_server, never the user's database. */
const assert = require('node:assert/strict');
const path = require('node:path');
const puppeteer = require('../tools/node_modules/puppeteer-core');
const base = process.env.SYNC_TEST_URL || 'http://127.0.0.1:8097';
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true, args: ['--disable-gpu'],
  });
  const errors = [];
  const pages = [];
  try {
    for (const route of ['/?token=dev-token&mode=h1#desk', '/ltf.html?token=dev-token&stay=1&instrument=1']) {
      console.log('Opening', route);
      const page = await browser.newPage();
      await page.bringToFront();
      await page.setViewport({ width: 1440, height: 900 });
      page.on('pageerror', (e) => errors.push(`${route}: ${e.message}`));
      await page.goto(base + route, { waitUntil: 'networkidle2' });
      await page.waitForFunction(() => window.LFDataSync && state.chart && state.candles.length && !document.getElementById('btn-data-sync').disabled, { polling: 100 });
      await page.waitForNetworkIdle();
      assert.equal(await page.evaluate(() => state.instrumentId), 1);
      await page.evaluate(() => {
        state.chart.timeScale().setVisibleLogicalRange({ from: 3, to: 15 });
      });
      pages.push(page);
    }
    // Two independent windows request the same durable job.
    console.log('Starting synchronization');
    await Promise.all(pages.map((page) => page.evaluate(() => document.getElementById('btn-data-sync').click())));
    await Promise.all(pages.map((page) => page.waitForFunction(() =>
      document.getElementById('data-sync-status').textContent.includes('Данные обновлены'), { timeout: 15000, polling: 100 })));
    console.log('Checking completed job and viewport');
    for (const page of pages) {
      await page.bringToFront();
      const data = await page.evaluate(() => ({
        candles: state.candles.length,
        range: state.chart.timeScale().getVisibleLogicalRange(),
        online: HTF.isWsConnected(),
        overflow: document.documentElement.scrollWidth > innerWidth + 2,
      }));
      assert.equal(data.candles, 33);
      assert.ok(Math.abs(data.range.from - 3) < .01 && Math.abs(data.range.to - 15) < .01, JSON.stringify(data));
      assert.ok(data.online && !data.overflow);
      await page.click('.data-sync-bar summary');
      assert.ok((await page.$eval('#data-sync-details', (e) => e.textContent)).includes('H1: актуально'));
      await page.click('.data-sync-bar summary');
    }
    const status = await pages[0].evaluate(() => HTF.api('/api/data-sync/status'));
    assert.equal(status.job.channels_total, 8);
    assert.equal(status.job.status, 'succeeded');

    // Miss a mutation while offline, reconnect, and recover the actual OHLC.
    console.log('Checking reconnect');
    for (const page of pages) {
      await page.bringToFront();
      await page.evaluate(() => state.ws.close());
      await page.waitForFunction(() => !HTF.isWsConnected(), { polling: 100 });
      await page.evaluate(() => HTF.api('/__test/change/1', { method: 'POST' }));
      await page.waitForFunction(() => HTF.isWsConnected() && state.candles[state.candles.length - 2].close === 110, { timeout: 12000, polling: 100 });
      const range = await page.evaluate(() => state.chart.timeScale().getVisibleLogicalRange());
      assert.ok(Math.abs(range.from - 3) < .01 && Math.abs(range.to - 15) < .01);
    }

    console.log('Checking return to hidden tab and connected-socket fallback');
    const recoveryPage = pages[0];
    await recoveryPage.evaluate(() => {
      window.syncTestOriginalNow = Date.now;
      Date.now = () => window.syncTestOriginalNow() + 20000;
    });
    const temporary = await browser.newPage();
    await temporary.bringToFront();
    await fetch(base + '/__test/change/1?price=107', { method: 'POST' });
    await recoveryPage.bringToFront();
    await recoveryPage.waitForFunction(() => state.candles[state.candles.length - 2].close === 107, { timeout: 10000, polling: 100 });
    await recoveryPage.evaluate(() => { Date.now = window.syncTestOriginalNow; });
    await temporary.close();
    await fetch(base + '/__test/change/1?price=106', { method: 'POST' });
    assert.ok(await recoveryPage.evaluate(() => HTF.isWsConnected()));
    await recoveryPage.waitForFunction(() => state.candles[state.candles.length - 2].close === 106, { timeout: 35000, polling: 100 });

    // Snapshot requests from simultaneous triggers are coalesced.
    console.log('Checking snapshot coalescing and stale responses');
    const page = pages[0];
    let candleReads = 0;
    page.on('request', (request) => { if (request.url().includes('/api/candles?')) candleReads++; });
    await page.evaluate(() => Promise.all([LFDataSync.snapshot({ force: true }), LFDataSync.snapshot({ force: true })]));
    assert.ok(candleReads <= 2, `overlapping snapshot reads: ${candleReads}`);

    // Selected asset switches while an earlier candle response is delayed.
    await page.setRequestInterception(true);
    const held = [];
    const intercept = (request) => {
      if (request.url().includes('/api/candles?instrument_id=1')) held.push(request);
      else request.continue();
    };
    page.on('request', intercept);
    await page.evaluate(() => { loadCandles(); });
    for (let i = 0; i < 50 && !held.length; i++) await delay(20);
    assert.ok(held.length);
    await page.evaluate(async () => { state.instrumentId = 2; await reloadAll(); });
    for (const request of held) await request.continue();
    await delay(300);
    assert.equal(await page.evaluate(() => state.loadedCandleInstrument), 2);
    page.off('request', intercept);
    await page.setRequestInterception(false);

    for (const [i, page] of pages.entries()) {
      await page.setViewport({ width: 390, height: 844, isMobile: true });
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 2);
      assert.equal(overflow, false, 'mobile overflow');
      if (process.env.SYNC_SCREENSHOT_DIR) await page.screenshot({ path: path.join(process.env.SYNC_SCREENSHOT_DIR, `sync-${i}-mobile.png`) });
    }
    assert.deepEqual(errors, []);
    console.log('Browser synchronization acceptance passed: manual job, both screens, OHLC recovery, range preservation, snapshot coalescing, late response, mobile layout.');
  } catch (error) {
    console.error('Browser acceptance failed:', error);
    throw error;
  } finally {
    await Promise.race([browser.close(), delay(5000).then(() => { if (browser.process()) browser.process().kill(); })]);
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
