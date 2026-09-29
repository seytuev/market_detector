// Контроль: фильтры по клику + статистика в шапке.
const puppeteer = require('puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu', '--window-size=1700,950'],
    defaultViewport: { width: 1700, height: 950 },
  });
  const page = await browser.newPage();
  page.on('pageerror', (e) => console.log('[pageerror]', e.message));
  await page.goto('http://127.0.0.1:8080/?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForFunction(() => document.querySelector('#zones-table tbody tr'), { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 2000));

  const stats = await page.evaluate(() => document.querySelector('#header-stats').textContent.trim());
  console.log('header stats:', stats);

  // клик по ячейке типа первой строки — фильтр
  const before = await page.evaluate(() => document.querySelectorAll('#zones-table tbody tr').length);
  await page.click('#zones-table tbody tr td[data-ftype]');
  await new Promise((r) => setTimeout(r, 500));
  const after = await page.evaluate(() => ({
    rows: document.querySelectorAll('#zones-table tbody tr').length,
    chips: document.querySelector('#zone-filters').textContent.trim(),
  }));
  console.log('rows before/after type filter:', before, '/', after.rows, '| chips:', after.chips);
  await page.screenshot({ path: 'C:/users/seytu/projects/htfdec/data/shot_filters.png' });

  // снятие фильтра чипом
  await page.click('#zone-filters .filter-chip');
  await new Promise((r) => setTimeout(r, 500));
  const restored = await page.evaluate(() => document.querySelectorAll('#zones-table tbody tr').length);
  console.log('rows after chip reset:', restored);
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
