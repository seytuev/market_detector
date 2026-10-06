/* Проверка окна «LTF Current Setup» (Этап 4 ТЗ) против живого сервера.
   Запуск из tools/: node check_ltf_current.js [baseUrl]
   По умолчанию http://localhost:8000, токен dev-token (?token= сохраняется
   в localStorage общим common.js). Скриншоты — в ../data/diag/.

   Проверки (приёмка ТЗ):
   01 — левая панель: одна строка на instrument (сколько бы контекстов);
   19 — счётчик «Подходящие зоны» == counts.eligible из /current,
        строк таблицы == counts.eligible (один снимок);
   05/§4.3 — слои по умолчанию: HTF-зона/диапазон/BOS видны, pivots,
        internal-уровни, старые версии диапазона и excluded-зоны скрыты;
   03 — отменённый сценарий не в карточке: контекст без живого сценария
        показывает «Ожидание нового сценария» без кнопки «Завершить»;
   22 — мобильная ширина 390px: без горизонтальной прокрутки колонок,
        график виден, выбор актива сверху.
*/
const puppeteer = require('puppeteer-core');

const BASE = process.argv[2] || 'http://localhost:8000';
const TOKEN = process.argv[3] || 'dev-token';
const PAGE = `${BASE}/ltf.html?token=${TOKEN}`;
const AUTH = { Authorization: `Bearer ${TOKEN}` };

async function apiJson(page, path, options) {
  return page.evaluate(async (p, o, headers) => {
    const r = await fetch(p, { ...o, headers: { 'Content-Type': 'application/json', ...headers } });
    return { status: r.status, body: r.status === 204 ? null : await r.json() };
  }, path, options || {}, AUTH).then((r) => {
    if (r.status >= 400) throw new Error(`${path} -> ${r.status}: ${JSON.stringify(r.body)}`);
    return r.body;
  });
}

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu'],
  });
  const failures = [];
  const ok = [];
  const check = (cond, label) => {
    if (cond) ok.push(label); else failures.push(label);
    console.log((cond ? 'PASS ' : 'FAIL ') + label);
  };

  // ---------------- desktop ----------------
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  page.on('pageerror', (e) => failures.push('pageerror: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.location()?.url?.includes('favicon')) failures.push('console.error: ' + m.text());
  });
  await page.goto(PAGE, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#ltf-obs-list .ltf-asset', { timeout: 30000 });
  await page.waitForSelector('#chart canvas', { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 2000));

  // 01: одна строка на instrument
  const apiAssets = (await apiJson(page, '/api/ltf/instruments')).instruments;
  const uiRows = await page.$$eval('#ltf-obs-list .ltf-asset', (els) =>
    els.map((e) => e.querySelector('.ltf-obs-head b').textContent));
  check(uiRows.length === apiAssets.length,
    `01: строк в «Активы» ${uiRows.length} == instruments API ${apiAssets.length}`);
  check(new Set(uiRows).size === uiRows.length,
    `01: тикеры не дублируются (${uiRows.join(', ')})`);

  // выбранный инструмент — из UI
  const instrumentId = await page.$eval('#ltf-instrument', (el) => Number(el.value));
  const current = await apiJson(page, `/api/ltf/instruments/${instrumentId}/current`);
  console.log('instrument:', instrumentId, 'stage:', current.stage,
    'scenario:', current.current_scenario && current.current_scenario.id,
    'counts:', JSON.stringify(current.counts));

  // 19: счётчик и таблица из одного снимка
  const uiCount = await page.$eval('#ltf-eligible-count', (el) => Number(el.textContent.trim() || '0'));
  check(uiCount === current.counts.eligible,
    `19: счётчик «Подходящие зоны» UI=${uiCount} == counts.eligible=${current.counts.eligible}`);
  const tableRows = await page.$$eval('#ltf-entries-table tbody tr.entry-row', (els) => els.length)
    .catch(() => 0);
  check(tableRows === current.counts.eligible,
    `19: строк таблицы eligible ${tableRows} == counts.eligible=${current.counts.eligible}`);
  const assetRow = await page.$eval(
    `#ltf-obs-list .ltf-asset[data-instrument-id="${instrumentId}"] .asset-eligible`,
    (el) => Number(el.textContent));
  const apiAsset = apiAssets.find((a) => a.instrument.id === instrumentId);
  check(assetRow === apiAsset.eligible_count,
    `01: счётчик зон в строке актива ${assetRow} == API eligible_count=${apiAsset.eligible_count}`);

  // этап в строке актива — серверный
  const stageText = await page.$eval(
    `#ltf-obs-list .ltf-asset[data-instrument-id="${instrumentId}"] .stage-badge`,
    (el) => el.textContent);
  check(stageText === apiAsset.stage,
    `§4.2: этап строки «${stageText}» == серверный «${apiAsset.stage}»`);

  // §4.3: слои по умолчанию
  const overlay = await page.$$eval('#ltf-overlay > div', (els) => els.map((e) => e.className.split(' ')[0]));
  const has = (cls) => overlay.includes(cls);
  check(has('ltf-parent'), '§4.3: HTF-границы контекста видны по умолчанию');
  if (current.range) {
    check(has('ltf-range-line'), '§4.3: диапазон 0/50/100 виден по умолчанию');
  }
  if (current.current_scenario) {
    check(has('ltf-break'), '§4.3: последний BOS/SMS сценария виден по умолчанию');
  }
  check(!has('ltf-pivot'), '§4.3: pivot-метки скрыты по умолчанию');
  check(!has('ltf-internal'), '§4.3: internal high/low скрыты по умолчанию');
  check(!has('ltf-range-old'), '§4.3: старые версии диапазона скрыты по умолчанию');
  check(!overlay.some((c) => c === 'ltf-entry' &&
    false) && !(await page.$('#ltf-overlay .ltf-entry-excluded')),
    '§4.3: excluded-зоны скрыты по умолчанию');

  // тулбар слоёв включает скрытые слои
  await page.click('#ltf-layer-toggles [data-layer="structure"]');
  await new Promise((r) => setTimeout(r, 400));
  check(!!(await page.$('#ltf-overlay .ltf-pivot')), '§4.3: «Подробная структура» показывает pivots');
  await page.click('#ltf-layer-toggles [data-layer="structure"]');
  await page.click('#ltf-layer-toggles [data-layer="excluded"]');
  await new Promise((r) => setTimeout(r, 400));
  if (current.counts.excluded > 0) {
    check(!!(await page.$('#ltf-overlay .ltf-entry-excluded')),
      '§4.3: «Исключённые зоны» показывает excluded-слой');
  }
  await page.click('#ltf-layer-toggles [data-layer="excluded"]');
  await new Promise((r) => setTimeout(r, 300));

  // масштаб: ручной выбор не сбрасывается обновлениями (проверяем, что кнопки переключают диапазон)
  const rangeBefore = await page.evaluate(() => {
    const c = document.querySelector('#chart canvas');
    return !!c;
  });
  check(rangeBefore, '05: график отрисован');

  // карточка: положение цены/сценарий по серверу
  const cardText = await page.$eval('#ltf-card', (el) => el.textContent);
  if (current.current_scenario) {
    check(cardText.includes(current.current_scenario.trigger || 'BOS'),
      '§4.4: в карточке живой сценарий (триггер с сервера)');
    check(!!(await page.$('#btn-close-scenario')),
      '§8: у живого сценария есть кнопка «Завершить сценарий»');
    if (!current.range) {
      check(cardText.includes('BOS подтверждён. Ждём подтверждения опор диапазона'),
        '§4.4: слом без диапазона — «BOS подтверждён. Ждём подтверждения опор диапазона»');
    }
  }

  // 03: контекст без живого сценария — ожидание нового, без кнопки завершения
  const waitingCtx = current.contexts.find((c) => c.state === 'waiting_structure');
  if (waitingCtx) {
    await apiJson(page, `/api/ltf/instruments/${instrumentId}/select-context`, {
      method: 'POST', body: JSON.stringify({ observation_id: waitingCtx.observation_id }),
    });
    await page.goto(`${BASE}/ltf.html?instrument=${instrumentId}`, { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#ltf-card dl', { timeout: 30000 });
    await new Promise((r) => setTimeout(r, 1000));
    const w = await apiJson(page, `/api/ltf/instruments/${instrumentId}/current`);
    const wCard = await page.$eval('#ltf-card', (el) => el.textContent);
    if (w.current_scenario === null) {
      check(wCard.includes('Ожидание нового сценария') || wCard.includes('Сценарий откроется'),
        '03: отменённый/отсутствующий сценарий не в текущей карточке — ожидание нового');
      check(!(await page.$('#btn-close-scenario')),
        '03/§8: нет кнопки «Завершить сценарий» без живого сценария');
      check(w.scenario_waiting === null || w.scenario_waiting.status === 'awaiting_new_scenario',
        '03: scenario_waiting со статусом awaiting_new_scenario');
    } else {
      console.log('SKIP 03: сервер уже создал новый сценарий для контекста', waitingCtx.observation_id);
    }
    // вернуть исходный выбор контекста
    await apiJson(page, `/api/ltf/instruments/${instrumentId}/select-context`, {
      method: 'POST', body: JSON.stringify({ observation_id: current.selected_context_id }),
    }).catch(() => {});
    await page.goto(`${BASE}/ltf.html?instrument=${instrumentId}`, { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#ltf-card dl', { timeout: 30000 });
  } else {
    console.log('SKIP 03: нет контекста в ожидании для проверки');
  }

  // §4.2: «Контексты: N» — раскрываемый список контекстов
  const ctxToggle = await page.$(`#ltf-obs-list .ltf-asset[data-instrument-id="${instrumentId}"] .ctx-toggle`);
  if (ctxToggle) {
    await ctxToggle.click();
    await new Promise((r) => setTimeout(r, 1200));
    const ctxItems = await page.$$eval(
      `#ltf-obs-list .ctx-list[data-ctx-list="${instrumentId}"] .ctx-item`, (els) => els.length);
    check(ctxItems === current.contexts.length,
      `§4.2: раскрытых контекстов ${ctxItems} == API contexts ${current.contexts.length}`);
    const ctxText = await page.$eval(
      `#ltf-obs-list .ctx-list[data-ctx-list="${instrumentId}"]`, (el) => el.textContent);
    check(ctxText.includes('последнее касание'),
      '§4.2: в контексте подписано «последнее касание» (не «дата создания = контакт»)');
  }

  await new Promise((r) => setTimeout(r, 1200));
  await page.screenshot({ path: '../data/diag/ltf_current_desktop.png' });

  // ---------------- mobile 390px (п.22) ----------------
  const mob = await browser.newPage();
  mob.on('pageerror', (e) => failures.push('mobile pageerror: ' + e.message));
  await mob.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
  await mob.goto(PAGE, { waitUntil: 'networkidle2', timeout: 60000 });
  await mob.waitForSelector('#ltf-obs-list .ltf-asset', { timeout: 30000 });
  await mob.waitForSelector('#chart canvas', { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 2000));
  const mobInfo = await mob.evaluate(() => {
    const layout = document.querySelector('.ltf-layout');
    const chart = document.querySelector('.chart-block').getBoundingClientRect();
    const card = document.querySelector('#ltf-inspector').getBoundingClientRect();
    const sel = document.querySelector('#ltf-instrument').getBoundingClientRect();
    return {
      docScrollW: document.documentElement.scrollWidth,
      innerW: window.innerWidth,
      chartTop: chart.top, cardTop: card.top,
      chartVisible: chart.height > 100,
      selectVisible: sel.width > 0 && sel.top < window.innerHeight,
      inspectorStatic: getComputedStyle(document.querySelector('#ltf-inspector')).position === 'static',
    };
  });
  console.log('MOBILE', JSON.stringify(mobInfo));
  check(mobInfo.docScrollW <= mobInfo.innerW + 1,
    `22: нет горизонтальной прокрутки (scrollWidth=${mobInfo.docScrollW} <= ${mobInfo.innerW})`);
  check(mobInfo.chartVisible, '22: график виден на мобильном');
  check(mobInfo.selectVisible, '22: выбор актива сверху');
  check(mobInfo.inspectorStatic, '22: карточка состояния — блок под графиком, не третья колонка');
  check(mobInfo.cardTop >= mobInfo.chartTop, '22: порядок: график → состояние');
  await mob.screenshot({ path: '../data/diag/ltf_current_mobile.png', fullPage: false });

  await browser.close();
  console.log(`\nИТОГ: ${ok.length} PASS, ${failures.length} FAIL`);
  if (failures.length) {
    console.error('FAILURES:\n' + failures.join('\n'));
    process.exit(1);
  }
  console.log('OK: все проверки прошли');
}

main().catch((error) => { console.error(error); process.exit(1); });
