// Проверка: зона следует за вертикальным масштабом (drag по шкале цен).
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
    const r = document.querySelector('#zone-overlay .zone-rect').getBoundingClientRect();
    // истинная координата верхней границы первой видимой зоны по шкале графика
    const z = state.zones.find((zz) => zz.id === state.selectedZoneId) || state.zones.find((zz) => isHtf(zz) && zz.status !== 'candidate');
    return { rectTop: r.top, expectedTop: state.candleSeries.priceToCoordinate(z.upper) + document.querySelector('#chart').getBoundingClientRect().top, zoneUpper: z.upper };
  });
  const before = await measure();
  // drag по шкале цен (правый край графика) вверх — вертикальный zoom
  const chart = await page.evaluate(() => document.querySelector('#chart').getBoundingClientRect().toJSON());
  await page.mouse.move(chart.right - 20, chart.top + chart.height / 2);
  await page.mouse.down();
  await page.mouse.move(chart.right - 20, chart.top + chart.height / 2 - 200, { steps: 12 });
  await page.mouse.up();
  await new Promise((r) => setTimeout(r, 500));
  const after = await measure();
  console.log('до drag: rectTop=%s expected=%s', before.rectTop.toFixed(1), before.expectedTop.toFixed(1));
  console.log('после drag: rectTop=%s expected=%s (zone upper=%s)', after.rectTop.toFixed(1), after.expectedTop.toFixed(1), after.zoneUpper);
  console.log('смещение совпало:', Math.abs(after.rectTop - after.expectedTop) < 3 ? 'ДА' : 'НЕТ');
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
