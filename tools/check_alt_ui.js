/* Проверка рабочего места «Альткоины».
   Запуск из tools/: node check_alt_ui.js
   Сервер с данными: http://127.0.0.1:8891/?token=dev-token */
const puppeteer = require('puppeteer-core');

const BASE = process.env.ALT_UI_BASE || 'http://127.0.0.1:8891/alt.html?token=dev-token';
const WIDTHS = [
  [1440, 900], [1920, 1080], [1366, 768], [1024, 768], [768, 1024], [390, 844],
];

function fail(failures, message) {
  failures.push(message);
  console.log('FAIL', message);
}

async function clickVisible(page, failures, selector) {
  const hit = await page.evaluate((sel) => {
    const node = document.querySelector(sel);
    if (!node) return 'missing';
    const rect = node.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) return 'zero';
    const top = document.elementFromPoint(rect.left + Math.min(8, rect.width / 2), rect.top + rect.height / 2);
    const ok = top === node || (top && node.contains(top));
    if (ok) node.click();
    return ok ? 'hit' : ((top && (top.id || top.className || top.tagName)) || 'none');
  }, selector);
  if (hit !== 'hit') {
    fail(failures, selector + ' не нажимается: ' + hit);
    await page.evaluate((sel) => {
      const node = document.querySelector(sel);
      if (node) node.click();
    }, selector);
  }
  return hit === 'hit';
}

async function openFresh(browser, theme) {
  const page = await browser.newPage();
  const posts = [];
  page.on('request', (request) => {
    if (request.method() === 'POST') posts.push(request.url());
  });
  page.on('pageerror', (error) => posts.push('pageerror: ' + error.message));
  await page.evaluateOnNewDocument(() => {
    localStorage.removeItem('lf:alt:prefs');
    if (!sessionStorage.getItem('lf-ui-theme-lock')) {
      localStorage.setItem('lf:theme', 'dark');
      sessionStorage.setItem('lf-ui-theme-lock', '1');
    }
  });
  return { page, posts };
}

async function waitReady(page) {
  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForFunction(() => {
    const rows = document.querySelectorAll('#alt-list .alt-row');
    const asof = document.getElementById('alt-asof').textContent || '';
    return rows.length > 0 && asof.includes('Данные на') && !asof.includes('…');
  }, { timeout: 20000 });
}

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu'],
  });
  const failures = [];
  const { page, posts } = await openFresh(browser, 'dark');
  await page.setViewport({ width: 1440, height: 900 });
  await waitReady(page);

  const shell = await page.evaluate(() => {
    const asof = document.getElementById('alt-asof').textContent;
    const tabs = [...document.querySelectorAll('.alt-tab')].map((button) => ({
      bucket: button.dataset.bucket,
      label: button.dataset.label || button.textContent,
      title: button.title,
      count: button.textContent,
    }));
    const venues = [...document.querySelectorAll('#flt-venue option')].map((option) => option.value);
    const toolbar = document.getElementById('alt-toolbar').getBoundingClientRect();
    const chart = document.getElementById('chart-container').getBoundingClientRect();
    const note = document.getElementById('alt-chart-note').getBoundingClientRect();
    return {
      asof,
      msk: (asof.match(/МСК/g) || []).length,
      tabs,
      venues,
      toolbarAbove: toolbar.bottom <= chart.top + 1,
      noteAbove: note.height === 0 || note.bottom <= chart.top + 1,
      chartH: chart.height,
      scrollW: document.documentElement.scrollWidth,
      innerW: window.innerWidth,
      theme: document.documentElement.dataset.theme,
    };
  });
  console.log('shell', JSON.stringify(shell));
  if (shell.msk !== 1) fail(failures, 'шапка свежести: МСК ' + shell.msk);
  if (!shell.venues.includes('bybit')) fail(failures, 'биржа bybit не в списке');
  const labels = shell.tabs.map((tab) => tab.label.trim());
  if (labels.join('|') !== 'Все активные|Новые входы|Ожидание ретеста|Требуют проверки|История') {
    fail(failures, 'вкладки: ' + labels.join('|'));
  }
  const eligible = shell.tabs.find((tab) => tab.bucket === 'eligible');
  if (!eligible || !eligible.title.includes('Не означает')) fail(failures, 'подсказка «все активные»');
  if (!shell.toolbarAbove) fail(failures, 'панель графика не над графиком');
  if (shell.chartH < 420) fail(failures, 'высота графика ' + shell.chartH);
  if (shell.scrollW > shell.innerW + 1) fail(failures, 'горизонтальная прокрутка 1440');
  if (shell.theme !== 'dark') fail(failures, 'тема не тёмная');

  await page.evaluate(() => {
    const row = [...document.querySelectorAll('.alt-row')].find((button) => button.textContent.includes('CNF'));
    row.click();
  });
  await page.waitForFunction(() => {
    const title = document.getElementById('alt-chart-title').textContent;
    const card = document.getElementById('alt-card-body').textContent;
    return title.startsWith('CNF') && card.includes('закрытие подтверждающей D1') && document.querySelector('#chart canvas');
  }, { timeout: 20000 });
  await page.waitForFunction(() => document.querySelectorAll('.alt-marker').length > 0, { timeout: 10000 });

  const setupView = await page.evaluate(() => {
    const card = document.getElementById('alt-card-body').textContent;
    return {
      markers: document.querySelectorAll('.alt-marker').length,
      levels: document.querySelectorAll('.alt-level').length,
      range: document.querySelectorAll('.alt-range-box').length,
      overlay: getComputedStyle(document.getElementById('alt-overlay')).pointerEvents,
      marker: getComputedStyle(document.querySelector('.alt-marker')).pointerEvents,
      card,
      buy: /покупать/i.test(card),
      historical: card.includes('не текущая заявка'),
    };
  });
  console.log('setup', setupView.markers, setupView.levels, setupView.range);
  if (setupView.markers > 12) fail(failures, 'в режиме сетапа слишком много меток: ' + setupView.markers);
  if (setupView.markers < 1) fail(failures, 'в режиме сетапа нет меток');
  if (!setupView.levels) fail(failures, 'нет линий диапазона');
  if (!setupView.range) fail(failures, 'нет области накопления');
  if (setupView.overlay !== 'none') fail(failures, 'оверлей перехватывает пан');
  if (setupView.marker !== 'auto') fail(failures, 'метка не нажимается');
  if (setupView.buy) fail(failures, 'в карточке есть «покупать»');
  if (!setupView.historical) fail(failures, 'вход A не назван исторической ценой');

  await clickVisible(page, failures, '#alt-mode-history');
  await page.waitForFunction((before) => document.querySelectorAll('.alt-marker').length > before, { timeout: 10000 }, setupView.markers);
  const historyMarkers = await page.evaluate(() => document.querySelectorAll('.alt-marker').length);
  console.log('history markers', historyMarkers);
  if (historyMarkers <= setupView.markers) fail(failures, 'история не показала больше меток');

  await clickVisible(page, failures, '#alt-mode-none');
  await page.waitForFunction(() => document.querySelectorAll('.alt-marker').length === 0, { timeout: 10000 });
  await clickVisible(page, failures, '#alt-mode-setup');
  await page.waitForFunction(() => document.querySelectorAll('.alt-marker').length > 0, { timeout: 10000 });

  /* UI-01/UI-03/UI-04: переключатель D1/W1, «Авто», кнопки PNG. */
  const actions = await page.evaluate(() => ({
    auto: !!document.getElementById('alt-auto'),
    png: !!document.getElementById('alt-png'),
    png2: !!document.getElementById('alt-png2'),
    tf: [...document.querySelectorAll('.alt-tf button')].map((button) => button.dataset.tf),
    actionsBelow: (() => {
      const bar = document.querySelector('.alt-chart-actions');
      const chart = document.getElementById('chart-container');
      if (!bar || !chart) return false;
      return bar.getBoundingClientRect().top >= chart.getBoundingClientRect().bottom - 1;
    })(),
  }));
  console.log('actions', JSON.stringify(actions));
  if (!actions.auto) fail(failures, 'нет кнопки «Авто»');
  if (!actions.png || !actions.png2) fail(failures, 'нет кнопок PNG/PNG 2×');
  if (actions.tf.join('|') !== 'D1|W1') fail(failures, 'переключатель таймфрейма: ' + actions.tf.join('|'));
  if (!actions.actionsBelow) fail(failures, 'панель «Авто/PNG» не под графиком');

  await clickVisible(page, failures, '.alt-tf button[data-tf="W1"]');
  await page.waitForFunction(() =>
    document.querySelector('.alt-tf button[data-tf="W1"]').getAttribute('aria-pressed') === 'true' &&
    document.querySelector('#chart canvas'), { timeout: 10000 });
  const w1note = await page.evaluate(() => document.getElementById('alt-chart-note').textContent);
  console.log('w1 note', w1note);
  await clickVisible(page, failures, '.alt-tf button[data-tf="D1"]');
  await page.waitForFunction(() =>
    document.querySelector('.alt-tf button[data-tf="D1"]').getAttribute('aria-pressed') === 'true' &&
    document.querySelectorAll('.alt-marker').length > 0, { timeout: 10000 });

  await clickVisible(page, failures, '#alt-auto');
  await new Promise((resolve) => setTimeout(resolve, 500));
  const autoState = await page.evaluate(() => ({
    canvas: !!document.querySelector('#chart canvas'),
    note: document.getElementById('alt-chart-note').textContent,
  }));
  if (!autoState.canvas) fail(failures, 'после «Авто» пропал график');

  /* UI-02: серия переключений активов (в бою TAO→PUMP→AAVE): шкала и серия
     не должны оставаться от прежнего актива; здесь — отсутствие ошибок
     страницы и смена заголовка на каждом шаге. */
  const switchSymbols = await page.evaluate(() =>
    [...document.querySelectorAll('#alt-list .alt-row')].slice(0, 3)
      .map((row) => row.querySelector('.alt-row-symbol').textContent));
  for (const symbol of switchSymbols) {
    await page.evaluate((sym) => {
      const row = [...document.querySelectorAll('#alt-list .alt-row')]
        .find((button) => button.querySelector('.alt-row-symbol').textContent === sym);
      if (row) row.click();
    }, symbol);
    await page.waitForFunction((sym) =>
      document.getElementById('alt-chart-title').textContent.startsWith(sym) &&
      document.querySelector('#chart canvas'), { timeout: 20000 }, symbol);
  }
  const switchErrors = posts.filter((url) => url.startsWith('pageerror'));
  if (switchErrors.length) fail(failures, 'переключение активов: ' + switchErrors.join('; '));

  await clickVisible(page, failures, '#alt-layers-toggle');
  await page.select('#layer-targets', 'all');
  await page.waitForFunction(() => [...document.querySelectorAll('.alt-edge')].some((button) => button.textContent.startsWith('TP4')), { timeout: 10000 });
  const beforeChip = await page.evaluate(() => document.querySelectorAll('.alt-edge').length);
  await page.evaluate(() => {
    [...document.querySelectorAll('.alt-edge')].find((button) => button.textContent.startsWith('TP4')).click();
  });
  await page.waitForFunction(() => ![...document.querySelectorAll('.alt-edge')].some((button) => button.textContent.startsWith('TP4')), { timeout: 10000 });
  console.log('edge chips', beforeChip, '-> tp4 shown');

  await page.evaluate(() => {
    const event = document.querySelector('#alt-journal-list .alt-event');
    if (event) event.scrollIntoView({ block: 'center' });
  });
  await clickVisible(page, failures, '#alt-journal-list .alt-event');
  await page.waitForFunction(() => {
    const note = document.getElementById('alt-chart-note').textContent;
    return !note.includes('соседнюю свечу');
  }, { timeout: 10000 });

  const postsAfterModes = posts.filter((url) => url.includes('/api/alt') || url.startsWith('pageerror'));
  if (postsAfterModes.length) fail(failures, 'режим или слои ушли в POST/ошибку: ' + postsAfterModes.join('; '));

  await clickVisible(page, failures, '#alt-filters-toggle');
  await page.type('#flt-age-min', '20');
  await page.type('#flt-age-max', '1');
  await clickVisible(page, failures, '#alt-apply');
  const ageError = await page.$eval('#err-age', (node) => node.textContent);
  if (!ageError) fail(failures, 'нет ошибки возраста');
  await page.click('#flt-age-min', { clickCount: 3 });
  await page.keyboard.press('Backspace');
  await page.click('#flt-age-max', { clickCount: 3 });
  await page.keyboard.press('Backspace');
  await page.type('#flt-rank-min', '9999');
  await clickVisible(page, failures, '#alt-apply');
  await page.waitForFunction(() => {
    const text = document.getElementById('alt-list-empty').textContent;
    return text.includes('не значит, что сетапов нет');
  }, { timeout: 10000 });
  const venuesAfter = await page.evaluate(() => [...document.querySelectorAll('#flt-venue option')].map((option) => option.value));
  if (!venuesAfter.includes('bybit')) fail(failures, 'пустой фильтр стёр биржи');
  const layersBeforeReset = await page.$eval('#layer-range', (node) => node.checked);
  await clickVisible(page, failures, '#alt-reset');
  await page.waitForFunction(() => document.querySelectorAll('#alt-list .alt-row').length > 0, { timeout: 10000 });
  const layersAfterReset = await page.$eval('#layer-range', (node) => node.checked);
  if (layersBeforeReset !== layersAfterReset) fail(failures, 'сброс фильтров изменил слои');

  await clickVisible(page, failures, '[data-bucket="review"]');
  await page.waitForFunction(() => [...document.querySelectorAll('.alt-row')].some((button) => button.textContent.includes('NOD')), { timeout: 10000 });
  await clickVisible(page, failures, '#alt-table-toggle');
  await page.waitForSelector('#alt-table-mode:not(.hidden) th');
  const columns = await page.evaluate(() => [...document.querySelectorAll('#alt-thead th')].map((cell) => cell.textContent));
  console.log('columns', columns.join('|'));
  if (columns.length !== 7) fail(failures, 'колонок по умолчанию ' + columns.length);
  await page.evaluate(() => {
    const row = [...document.querySelectorAll('#alt-tbody tr')].find((item) => item.textContent.includes('NOD'));
    row.click();
  });
  await page.waitForFunction(() => document.getElementById('alt-card-body').textContent.includes('проблема данных'), { timeout: 10000 });

  await clickVisible(page, failures, '[data-bucket="history"]');
  await page.waitForFunction(() => [...document.querySelectorAll('.alt-row')].some((button) => button.textContent.includes('HIS')), { timeout: 10000 });
  await page.evaluate(() => [...document.querySelectorAll('.alt-row')].find((button) => button.textContent.includes('HIS')).click());
  await page.waitForFunction(() => document.getElementById('alt-card-body').textContent.includes('Обратный отсчёт не показывается'), { timeout: 10000 });

  await clickVisible(page, failures, '[data-bucket="eligible"]');
  await page.waitForFunction(() => [...document.querySelectorAll('.alt-row')].some((button) => button.textContent.includes('MAT')), { timeout: 10000 });
  await page.evaluate(() => [...document.querySelectorAll('.alt-row')].find((button) => button.textContent.includes('MAT')).click());
  await page.waitForFunction(() => document.getElementById('alt-card-body').textContent.includes('Расчётный уровень отмены ≤ 0'), { timeout: 10000 });

  const filtersHidden = await page.$eval('#alt-filter-panel', (node) => node.classList.contains('hidden'));
  if (filtersHidden) await clickVisible(page, failures, '#alt-filters-toggle');
  await page.select('#flt-stage', 'forming');
  await clickVisible(page, failures, '#alt-apply');
  await page.waitForFunction(() => [...document.querySelectorAll('.alt-row')].some((button) => button.textContent.includes('FRM')), { timeout: 10000 });
  await page.evaluate(() => [...document.querySelectorAll('.alt-row')].find((button) => button.textContent.includes('FRM')).click());
  await page.waitForFunction(() => document.getElementById('alt-card-body').textContent.includes('подтверждения ещё нет'), { timeout: 10000 });

  const missing = await browser.newPage();
  missing.on('pageerror', (error) => failures.push('missing pageerror: ' + error.message));
  await missing.evaluateOnNewDocument(() => localStorage.removeItem('lf:alt:prefs'));
  await missing.setViewport({ width: 1440, height: 900 });
  await missing.goto(BASE + '&setup=999999', { waitUntil: 'networkidle2', timeout: 60000 });
  await missing.waitForFunction(() => (document.getElementById('alt-card-body').textContent || '').includes('не найден'), { timeout: 20000 });
  const missingText = await missing.evaluate(() => document.getElementById('alt-card-body').textContent);
  if (!missingText.includes('Другой сетап вместо него не открыт')) fail(failures, 'битая ссылка подменила сетап');
  await missing.close();

  for (const [width, height] of WIDTHS) {
    await page.setViewport({ width, height });
    await new Promise((resolve) => setTimeout(resolve, 300));
    if (width < 1100) {
      const onList = await page.$eval('#alt-page', (node) => node.dataset.pane === 'list');
      if (onList) {
        await page.evaluate(() => {
          const row = document.querySelector('#alt-list .alt-row');
          if (row) row.click();
        });
        await page.waitForFunction(() => document.getElementById('alt-page').dataset.pane === 'chart', { timeout: 10000 });
      }
    }
    const box = await page.evaluate((w, h) => {
      const chart = document.getElementById('chart-container').getBoundingClientRect();
      const card = getComputedStyle(document.getElementById('alt-card'));
      const back = getComputedStyle(document.getElementById('alt-back'));
      const mobile = getComputedStyle(document.querySelector('.mobile-nav'));
      const tabs = getComputedStyle(document.querySelector('.app-tabs'));
      return {
        scrollW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
        chartH: chart.height,
        cardPos: card.position,
        back: back.display,
        pane: document.getElementById('alt-page').dataset.pane,
        mobile: mobile.display,
        tabs: tabs.display,
        w, h,
      };
    }, width, height);
    console.log('layout', width, JSON.stringify(box));
    if ((width === 1440 || width === 1920) && box.scrollW > box.innerW + 1) {
      fail(failures, `горизонтальная прокрутка ${width}: ${box.scrollW}`);
    }
    if (width >= 1440 && height >= 800 && box.chartH < 420) fail(failures, `график ${width}x${height}: ${box.chartH}`);
    if (width <= 390 && box.chartH > 0 && box.chartH < 300 && box.pane === 'chart') {
      fail(failures, `мобильный график ${box.chartH}`);
    }
    if (width === 1366 && box.cardPos !== 'fixed') fail(failures, 'карточка на 1366 не выдвижная');
    if (width <= 1024 && box.pane === 'chart' && box.back === 'none') fail(failures, `нет кнопки назад на ${width}`);
    if (width === 390 && box.mobile === 'none') fail(failures, 'нет нижней навигации');
    if (width === 390 && box.tabs !== 'none') fail(failures, 'верхние вкладки видны на телефоне');
  }

  await page.evaluate(() => localStorage.setItem('lf:theme', 'light'));
  await page.reload({ waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForFunction(() => document.documentElement.dataset.theme === 'light' && document.querySelector('#chart canvas'), { timeout: 20000 });
  const light = await page.evaluate(() => {
    const bg = getComputedStyle(document.querySelector('.alt-page')).backgroundColor;
    return { theme: document.documentElement.dataset.theme, bg };
  });
  console.log('light', JSON.stringify(light));
  if (light.theme !== 'light') fail(failures, 'светлая тема не включилась');
  if (light.bg === 'rgb(0, 0, 0)' || light.bg === 'rgba(0, 0, 0, 0)') fail(failures, 'светлая тема без фона');

  await browser.close();
  if (failures.length) {
    console.log('FAILED', failures.length);
    failures.forEach((item) => console.log(' -', item));
    process.exit(1);
  }
  console.log('alt ui ok');
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
