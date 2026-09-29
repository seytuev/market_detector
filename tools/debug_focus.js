// E2E: клик по строке зоны → фокус на графике + панель деталей с кнопками.
const puppeteer = require('puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu', '--window-size=1700,950'],
    defaultViewport: { width: 1700, height: 950 },
  });
  const page = await browser.newPage();
  await page.goto('http://127.0.0.1:8080/?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForFunction(() => document.querySelector('#zones-table tbody tr'), { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 2000));

  // кликаем первую строку таблицы зон
  page.on('pageerror', (e) => console.log('[pageerror]', e.message)); await page.click('#zones-table tbody tr');
  await new Promise((r) => setTimeout(r, 2500));

  const info = await page.evaluate(() => {
    const drawer = document.querySelector('#zone-detail');
    const selected = document.querySelector('#zone-overlay .zone-rect.selected');
    const btns = [...document.querySelectorAll('#zone-detail .review-block .btn')].map((b) => b.textContent.trim());
    const tf = document.querySelector('#tf-select').value;
    const range = window.state ? null : null;
    return {
      drawerOpen: !drawer.classList.contains('hidden'),
      hasSelectedRect: !!selected,
      reviewButtons: btns,
      timeframe: tf,
      title: drawer.querySelector('h3')?.textContent?.slice(0, 60),
    };
  });
  console.log(JSON.stringify(info, null, 2));
  await page.screenshot({ path: 'C:/users/seytu/projects/htfdec/data/shot_focus.png' });
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
