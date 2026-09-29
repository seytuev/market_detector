const puppeteer = require('puppeteer-core');
(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new', args: ['--disable-gpu', '--window-size=1700,950'],
    defaultViewport: { width: 1700, height: 950 },
  });
  const page = await browser.newPage();
  page.on('pageerror', (e) => console.log('[pageerror]', e.message));
  page.on('console', (m) => { if (m.type() === 'error') console.log('[console.error]', m.text()); });
  await page.goto('http://127.0.0.1:8080/?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForFunction(() => document.querySelector('#zones-table tbody tr'), { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 1500));
  await page.click('#zones-table tbody tr');
  await new Promise((r) => setTimeout(r, 2500));
  const info = await page.evaluate(() => {
    const overlay = document.querySelector('#zone-overlay');
    return {
      selectedId: window.__state ? 'n/a' : undefined,
      overlayChildren: overlay.children.length,
      classes: [...overlay.children].map((d) => d.className).slice(0, 5),
      selected: !!overlay.querySelector('.selected'),
    };
  });
  console.log(JSON.stringify(info));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
