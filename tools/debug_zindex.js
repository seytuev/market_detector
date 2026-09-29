// Эксперимент: поднять z-index оверлею и проверить видимость зон.
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
  await page.waitForFunction(() => document.querySelector('#zone-overlay .zone-rect'), { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 2000));

  // что реально находится в точке первой зоны?
  const probe = await page.evaluate(() => {
    const zr = document.querySelector('#zone-overlay .zone-rect');
    const r = zr.getBoundingClientRect();
    const el = document.elementFromPoint(r.x + r.width / 2, r.y + Math.min(5, r.height / 2));
    return { rectCls: zr.className, atPoint: el ? (el.tagName + '.' + el.className) : 'null' };
  });
  console.log('до z-index:', JSON.stringify(probe));
  await page.screenshot({ path: 'C:/users/seytu/projects/htfdec/data/exp_before.png' });

  await page.evaluate(() => {
    document.querySelector('#zone-overlay').style.zIndex = '999';
  });
  await new Promise((r) => setTimeout(r, 500));
  const probe2 = await page.evaluate(() => {
    const zr = document.querySelector('#zone-overlay .zone-rect');
    const r = zr.getBoundingClientRect();
    const el = document.elementFromPoint(r.x + r.width / 2, r.y + Math.min(5, r.height / 2));
    return el ? (el.tagName + '.' + el.className) : 'null';
  });
  console.log('после z-index:', probe2);
  await page.screenshot({ path: 'C:/users/seytu/projects/htfdec/data/exp_after.png' });
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
