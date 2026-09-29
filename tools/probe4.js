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
  for (const wait of [200, 800, 2000]) {
    await new Promise((r) => setTimeout(r, wait));
    const vr = await page.evaluate(() => state.chart.timeScale().getVisibleRange());
    console.log('after', wait, 'ms more:', JSON.stringify(vr));
  }
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
