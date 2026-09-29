// Проверка: конкретная зона (по data-zone-id) следует за вертикальным масштабом.
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
  await page.waitForFunction(() => document.querySelector('#zone-overlay .zone-rect'), { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 1500));

  const measure = () => page.evaluate(() => {
    const rect = document.querySelector('#zone-overlay .zone-rect');
    const zid = Number(rect.dataset.zoneId);
    const z = state.zones.find((zz) => zz.id === zid);
    const chartTop = document.querySelector('#chart').getBoundingClientRect().top;
    return {
      zid,
      rectTop: rect.getBoundingClientRect().top,
      expectedTop: state.candleSeries.priceToCoordinate(z.upper) + chartTop,
    };
  });
  const before = await measure();
  const chart = await page.evaluate(() => document.querySelector('#chart').getBoundingClientRect().toJSON());
  await page.mouse.move(chart.right - 20, chart.top + chart.height / 2);
  await page.mouse.down();
  await page.mouse.move(chart.right - 20, chart.top + chart.height / 2 - 200, { steps: 12 });
  await page.mouse.up();
  await new Promise((r) => setTimeout(r, 500));
  const after = await measure();
  const fmt = (m) => `rectTop=${m.rectTop.toFixed(1)} expected=${m.expectedTop.toFixed(1)} Δ=${(m.rectTop - m.expectedTop).toFixed(1)}`;
  console.log('зона', before.zid, '| до:', fmt(before), '| после drag:', fmt(after));
  await page.screenshot({ path: 'C:/users/seytu/projects/htfdec/data/shot_drag.png' });
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
