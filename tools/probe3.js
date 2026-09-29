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
  const t1 = await page.evaluate(() => {
    const ts = state.chart.timeScale();
    return { vr: ts.getVisibleRange(), first: state.candles[0].time, last: state.candles[state.candles.length-1].time };
  });
  console.log('visible before:', JSON.stringify(t1));
  await page.evaluate(() => {
    state.chart.timeScale().setVisibleRange({ from: 1775260800, to: 1781827200 });
  });
  await new Promise((r) => setTimeout(r, 800));
  const t2 = await page.evaluate(() => {
    const ts = state.chart.timeScale();
    return { vr: ts.getVisibleRange(), x: ts.timeToCoordinate(1776556800) };
  });
  console.log('visible after manual set:', JSON.stringify(t2));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
