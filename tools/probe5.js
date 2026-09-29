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
  await page.evaluate(() => {
    const ts = state.chart.timeScale();
    const orig = ts.setVisibleRange.bind(ts);
    window.__svr = [];
    ts.setVisibleRange = (r) => { window.__svr.push({ ...r }); return orig(r); };
    const origFit = ts.fitContent.bind(ts);
    ts.fitContent = () => { window.__svr.push({ fitContent: true }); return origFit(); };
  });
  await page.click('#zones-table tbody tr');
  await new Promise((r) => setTimeout(r, 2500));
  const calls = await page.evaluate(() => window.__svr);
  console.log('setVisibleRange calls:', JSON.stringify(calls));
  const vr = await page.evaluate(() => state.chart.timeScale().getVisibleRange());
  console.log('final range:', JSON.stringify(vr));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
