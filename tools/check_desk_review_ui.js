/* UI-проверка рабочего места (desk) и «Проверки» (этапы 4–5 ребрендинга):
   табы режима, список активов, карточка сценария, очередь/инспектор
   проверки, отсутствие горизонтального скролла на 390/768/1024/1440.
   Запуск из tools/: node check_desk_review_ui.js — нужен сервер на :8877
   с HTF_AUTH_TOKEN=dev-token. Скриншоты пишутся в tools/. */
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
    await page.goto(BASE + '#desk', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#desk-assets-list .desk-asset, #desk-assets-list .ltf-empty',
      { timeout: 20000 });
    await new Promise((r) => setTimeout(r, 1200));

    const info = await page.evaluate(() => {
      const visible = (el) => !!el && el.getClientRects().length > 0;
      const ltfTab = document.getElementById('lnk-ltf');
      const scenario = document.getElementById('desk-scenario');
      return {
        hash: location.hash,
        deskActive: document.getElementById('view-overview').classList.contains('active'),
        symbol: (document.getElementById('desk-symbol') || {}).textContent || '',
        modeTabs: document.querySelectorAll('.desk-modebar .mode-tab').length,
        modeTabsVisible: visible(document.querySelector('.desk-modebar')),
        ltfHref: ltfTab ? ltfTab.getAttribute('href') : null,
        assets: document.querySelectorAll('#desk-assets-list .desk-asset').length,
        scenarioHidden: scenario.classList.contains('hidden'),
        scenarioH3: (scenario.querySelector('h3') || {}).textContent || '',
        watchBtn: (document.getElementById('desk-watch-btn') || {}).textContent || '',
        entries: (document.getElementById('desk-entries') || {}).textContent || '',
        zoneRail: !!document.getElementById('zone-rail'),
        scrollW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
      };
    });
    console.log(w, JSON.stringify(info));
    if (!info.deskActive) failures.push(`${w}: desk не активен`);
    if (!info.symbol || info.symbol === '—') failures.push(`${w}: нет символа актива`);
    if (info.modeTabs !== 2 || !info.modeTabsVisible) {
      failures.push(`${w}: табы режима не видны (${info.modeTabs})`);
    }
    if (!info.ltfHref || !info.ltfHref.includes('/ltf.html') ||
        !info.ltfHref.includes('instrument=')) {
      failures.push(`${w}: таб «Структура H1» без instrument: ${info.ltfHref}`);
    }
    if (!info.assets) failures.push(`${w}: список активов пуст`);
    if (!info.scenarioHidden && !info.scenarioH3) {
      failures.push(`${w}: карточка сценария без заголовка`);
    }
    if (info.scrollW > info.innerW) {
      failures.push(`${w}: горизонтальный скролл ${info.scrollW} > ${info.innerW}`);
    }
    await page.screenshot({ path: `desk_${w}.png`, fullPage: w === 390 });
  }

  // клик по табу «Структура H1» ведёт на /ltf.html с инструментом
  await page.setViewport({ width: 1440, height: 900 });
  await page.goto(BASE + '#desk', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#lnk-ltf', { timeout: 20000 });
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }),
    page.click('#lnk-ltf'),
  ]);
  await new Promise((r) => setTimeout(r, 800));
  const ltf = await page.evaluate(() => ({
    path: location.pathname,
    instrument: new URLSearchParams(location.search).get('instrument')
      || localStorage.getItem('htf:instrument'),
    deskTab: (document.getElementById('lnk-desk') || {}).href || '',
    deskTabActive: !!(document.querySelector('.mode-tab.active')),
  }));
  console.log('ltf:', JSON.stringify(ltf));
  if (ltf.path !== '/ltf.html') failures.push(`таб H1 ведёт на ${ltf.path}`);
  if (!ltf.instrument) failures.push('ltf.html без instrument');
  if (!ltf.deskTab.includes('#desk')) failures.push(`«Контекст» без #desk: ${ltf.deskTab}`);

  // возврат на desk по табу «Контекст»
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle2', timeout: 60000 }),
    page.click('#lnk-desk'),
  ]);
  await new Promise((r) => setTimeout(r, 800));
  const back = await page.evaluate(() => ({
    hash: location.hash,
    deskActive: document.getElementById('view-overview').classList.contains('active'),
  }));
  console.log('back:', JSON.stringify(back));
  if (back.hash !== '#desk' || !back.deskActive) failures.push('«Контекст» не вернул desk');

  // клик по второму активу списка переключает инструмент рабочего места
  await page.waitForSelector('#desk-assets-list .desk-asset', { timeout: 20000 });
  await new Promise((r) => setTimeout(r, 500));
  const assets = await page.$$('#desk-assets-list .desk-asset');
  if (assets.length > 1) {
    await assets[1].click();
    await new Promise((r) => setTimeout(r, 1500));
    const sel = await page.evaluate(() => ({
      symbol: document.getElementById('desk-symbol').textContent,
      selVal: document.getElementById('instrument-select').value,
      selected: document.querySelectorAll('#desk-assets-list .desk-asset.selected').length,
      saved: localStorage.getItem('htf:instrument'),
    }));
    console.log('asset-click:', JSON.stringify(sel));
    if (sel.selected !== 1) failures.push('клик по активу не выделил строку');
    if (sel.saved !== sel.selVal) failures.push('инструмент не синхронизирован');
  }

  // Проверка: шапка, очередь, карточка задачи
  await page.goto(BASE + '#review', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#candidates-list .candidate-card, #review-empty-back',
    { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 2500));
  const review = await page.evaluate(() => ({
    hash: location.hash,
    over: (document.querySelector('.review-over') || {}).textContent || '',
    title: (document.querySelector('.review-title') || {}).textContent || '',
    count: (document.getElementById('review-count') || {}).textContent || '',
    cards: document.querySelectorAll('#candidates-list .candidate-card').length,
    taskOver: (document.querySelector('#review-inspector .rv-task-over') || {}).textContent || '',
    taskH3: (document.querySelector('#review-inspector h3') || {}).textContent || '',
    confirmBtn: !!(document.querySelector('#review-inspector [data-review="correct"]')),
    rejectBtn: !!(document.querySelector('#review-inspector [data-review="wrong_type"]')),
    fixBtn: !!(document.querySelector('#review-inspector [data-review="fix_boundaries"]')),
    openDeskBtn: !!document.getElementById('rv-open-desk'),
    scrollW: document.documentElement.scrollWidth,
    innerW: window.innerWidth,
  }));
  console.log('review:', JSON.stringify(review));
  if (review.hash !== '#review') failures.push('review: hash');
  if (review.over !== 'Контроль разметки') failures.push('review: надзаголовок');
  if (review.title !== 'Проверка') failures.push('review: заголовок');
  if (review.cards > 0) {
    if (!review.taskOver.startsWith('Задача #')) failures.push('review: нет «ЗАДАЧА #id»');
    if (review.taskH3 !== 'Проверьте границы зоны') failures.push('review: H3 задачи');
    if (!review.confirmBtn || !review.rejectBtn) failures.push('review: основные кнопки');
    if (!review.fixBtn) failures.push('review: нет «Исправить границы»');
    if (!review.openDeskBtn) failures.push('review: нет «Открыть актив»');
  }
  if (review.scrollW > review.innerW) {
    failures.push(`review: горизонтальный скролл ${review.scrollW} > ${review.innerW}`);
  }
  await page.screenshot({ path: 'review_1440.png' });

  // «Открыть актив» из карточки задачи ведёт в desk того же инструмента
  if (review.cards > 0 && review.openDeskBtn) {
    await page.click('#rv-open-desk');
    await new Promise((r) => setTimeout(r, 1200));
    const opened = await page.evaluate(() => ({
      hash: location.hash,
      deskActive: document.getElementById('view-overview').classList.contains('active'),
    }));
    console.log('open-desk:', JSON.stringify(opened));
    if (opened.hash !== '#desk' || !opened.deskActive) {
      failures.push('«Открыть актив» не открыл desk');
    }
  }

  await browser.close();
  if (failures.length) {
    console.log('FAILURES:\n' + failures.join('\n'));
    process.exit(1);
  }
  console.log('OK');
}

main().catch((e) => { console.error(e); process.exit(1); });
