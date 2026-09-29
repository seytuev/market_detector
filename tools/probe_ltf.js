// Живая проверка окна LTF: /ltf.html на демо-БД (tools/seed_ltf_demo.py).
// Запуск: node tools/probe_ltf.js  (сервер должен слушать 127.0.0.1:8099)
const puppeteer = require('puppeteer-core');
(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new', args: ['--disable-gpu', '--window-size=1700,950'],
    defaultViewport: { width: 1700, height: 950 },
  });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (e) => errors.push('[pageerror] ' + e.message));
  page.on('console', (m) => { if (m.type() === 'error') errors.push('[console.error] ' + m.text()); });
  page.on('requestfailed', (r) => errors.push('[requestfailed] ' + r.url()));

  await page.goto('http://127.0.0.1:8099/ltf.html?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForFunction(() => document.querySelector('#ltf-obs-list .ltf-obs-item'), { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 2500));

  const info = await page.evaluate(() => ({
    obsItems: document.querySelectorAll('#ltf-obs-list .ltf-obs-item').length,
    entryRows: document.querySelectorAll('#ltf-entries-table tbody tr').length,
    journalItems: document.querySelectorAll('#ltf-journal li').length,
    overlayChildren: document.querySelector('#ltf-overlay').children.length,
    overlayClasses: [...document.querySelector('#ltf-overlay').children].map((d) => d.className).slice(0, 12),
    cardText: document.querySelector('#ltf-card').textContent.slice(0, 300),
    candles: !!document.querySelector('#chart canvas'),
  }));
  console.log('DESKTOP', JSON.stringify(info, null, 1));
  await page.screenshot({ path: '../data/shot_ltf.png' });

  // мобильный viewport (§3.5): панель списка — за бургером
  const mob = await browser.newPage();
  mob.on('pageerror', (e) => errors.push('[mobile pageerror] ' + e.message));
  mob.on('console', (m) => { if (m.type() === 'error') errors.push('[mobile console.error] ' + m.text()); });
  await mob.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
  await mob.goto('http://127.0.0.1:8099/ltf.html?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await mob.waitForFunction(() => document.querySelector('#ltf-obs-list .ltf-obs-item'), { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 1500));
  await mob.tap('#ltf-burger');
  await new Promise((r) => setTimeout(r, 600));
  const mobInfo = await mob.evaluate(() => ({
    burgerVisible: getComputedStyle(document.querySelector('#ltf-burger')).display !== 'none',
    panelOpen: document.querySelector('#ltf-obs-panel').classList.contains('open'),
    cardOrder: getComputedStyle(document.querySelector('#ltf-card-panel')).order,
    chartOrder: getComputedStyle(document.querySelector('.ltf-layout .chart-block')).order,
  }));
  console.log('MOBILE', JSON.stringify(mobInfo));
  await mob.screenshot({ path: '../data/shot_ltf_mobile.png' });

  console.log('ERRORS:', errors.length ? errors : 'none');
  await browser.close();
  if (errors.length) process.exit(1);
})().catch((e) => { console.error(e); process.exit(1); });
