/* UI-проверка экрана «Сейчас» (этап 3 ребрендинга): рендер таблицы/карточки,
   отсутствие горизонтального скролла на 390/768/1024/1440. Запуск из tools/:
   node check_now_ui.js — нужен сервер на :8877 с HTF_AUTH_TOKEN=dev-token. */
const puppeteer = require('puppeteer-core');

const BASE = 'http://127.0.0.1:8877/?token=dev-token';
const WIDTHS = [390, 768, 1024, 1440];

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu'],
  });
  const failures = [];
  const page = await browser.newPage();
  page.on('pageerror', (error) => failures.push('pageerror: ' + error.message));
  page.on('console', (msg) => {
    if (msg.type() === 'error') failures.push('console: ' + msg.text());
  });

  for (const w of WIDTHS) {
    await page.setViewport({ width: w, height: 900 });
    await page.goto(BASE + '#now', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#now-tbody tr', { timeout: 20000 });
    await new Promise((r) => setTimeout(r, 800));

    const info = await page.evaluate(() => ({
      hash: location.hash,
      nowActive: document.getElementById('view-now').classList.contains('active'),
      rows: document.querySelectorAll('#now-tbody tr').length,
      statReview: document.getElementById('now-stat-review').textContent,
      statZone: document.getElementById('now-stat-zone').textContent,
      statEligible: document.getElementById('now-stat-eligible').textContent,
      cardH3: (document.querySelector('#now-card h3') || {}).textContent || '',
      scrollW: document.documentElement.scrollWidth,
      innerW: window.innerWidth,
      tabActive: (document.querySelector('.app-tab.active, .mobile-nav a.active') || {}).textContent || '',
    }));
    console.log(w, JSON.stringify(info));
    if (!info.nowActive) failures.push(`${w}: view-now не активен`);
    if (info.hash !== '#now') failures.push(`${w}: hash ${info.hash} != #now`);
    if (!info.rows) failures.push(`${w}: таблица активов пуста`);
    if (!info.cardH3) failures.push(`${w}: карточка без заголовка`);
    if (info.scrollW > info.innerW) {
      failures.push(`${w}: горизонтальный скролл ${info.scrollW} > ${info.innerW}`);
    }
    await page.screenshot({ path: `now_${w}.png` });
  }

  // клик по второй строке — выбор в карточке; кнопка → desk с инструментом
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(BASE + '#now', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#now-tbody tr', { timeout: 20000 });
  await new Promise((r) => setTimeout(r, 500));
  const rows = await page.$$('#now-tbody tr');
  if (rows.length > 1) {
    await rows[1].click();
    await new Promise((r) => setTimeout(r, 300));
    const sel = await page.evaluate(() => ({
      selected: document.querySelectorAll('#now-tbody tr.selected').length,
      cardHead: (document.querySelector('.now-card-head span') || {}).textContent,
    }));
    console.log('select:', JSON.stringify(sel));
    if (sel.selected !== 1) failures.push('клик по строке не выбрал актив');
  }
  await page.click('.now-open-desk');
  await new Promise((r) => setTimeout(r, 1200));
  const desk = await page.evaluate(() => ({
    hash: location.hash,
    deskActive: document.getElementById('view-overview').classList.contains('active'),
    saved: localStorage.getItem('htf:instrument'),
    selVal: document.getElementById('instrument-select').value,
  }));
  console.log('desk:', JSON.stringify(desk));
  if (desk.hash !== '#desk' || !desk.deskActive) failures.push('кнопка не открыла desk');
  if (desk.saved !== desk.selVal) failures.push('инструмент не синхронизирован с desk');

  // алиасы старых якорей
  for (const [legacy, expect] of [['#overview', '#desk'], ['#events', '#journal']]) {
    await page.goto(BASE + legacy, { waitUntil: 'networkidle2', timeout: 60000 });
    await new Promise((r) => setTimeout(r, 500));
    const h = await page.evaluate(() => location.hash);
    console.log('alias', legacy, '->', h);
    if (h !== expect) failures.push(`алиас ${legacy}: hash ${h} != ${expect}`);
  }

  await browser.close();
  if (failures.length) {
    console.log('FAILURES:\n' + failures.join('\n'));
    process.exit(1);
  }
  console.log('OK');
}

main().catch((e) => { console.error(e); process.exit(1); });
