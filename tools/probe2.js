const puppeteer = require('puppeteer-core');
(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new', args: ['--disable-gpu', '--window-size=1700,950'],
    defaultViewport: { width: 1700, height: 950 },
  });
  const page = await browser.newPage();
  page.on('pageerror', (e) => console.log('[pageerror]', e.message));
  await page.goto('http://127.0.0.1:8080/?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForFunction(() => document.querySelector('#zones-table tbody tr'), { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 1500));
  await page.click('#zones-table tbody tr');
  await new Promise((r) => setTimeout(r, 2500));
  const info = await page.evaluate(() => {
    const sel = state.zones.find((z) => z.id === state.selectedZoneId);
    const out = {
      selectedZoneId: state.selectedZoneId,
      found: !!sel,
      candles: state.candles.length,
      timeframe: state.timeframe,
    };
    if (sel) {
      out.zoneTf = sel.timeframe;
      out.lower = sel.lower; out.upper = sel.upper;
      out.display_from = sel.display_from; out.display_until = sel.display_until;
      try {
        out.y1 = state.candleSeries.priceToCoordinate(sel.upper);
        out.y2 = state.candleSeries.priceToCoordinate(sel.lower);
        out.x1 = state.chart.timeScale().timeToCoordinate(Math.floor((sel.display_from || sel.formed_at) / 1000));
        out.x2 = sel.display_until ? state.chart.timeScale().timeToCoordinate(Math.floor(sel.display_until / 1000)) : null;
      } catch (e) { out.err = e.message; }
    }
    return out;
  });
  console.log(JSON.stringify(info, null, 1));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
