/* UI-проверка журнала и настроек (этапы 6–7 ребрендинга).
   Запуск из tools/: node check_journal_settings_ui.js
   Нужен сервер на :8877 с HTF_AUTH_TOKEN=dev-token. */
const puppeteer = require('puppeteer-core');

const BASE = 'http://127.0.0.1:8877/?token=dev-token';
const WIDTHS = [320, 390, 768, 1024, 1440];

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu'],
  });
  const failures = [];
  const page = await browser.newPage();
  page.on('pageerror', (error) => failures.push('pageerror: ' + error.message));

  for (const w of WIDTHS) {
    await page.setViewport({ width: w, height: 900 });
    await page.goto(BASE + '#journal', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#journal-list .journal-row, #journal-list .journal-empty', { timeout: 20000 });
    const info = await page.evaluate(() => {
      const row = document.querySelector('.journal-row');
      return {
        hash: location.hash,
        title: (document.querySelector('#view-journal .review-title') || {}).textContent || '',
        filters: [...document.querySelectorAll('#journal-filters button')].map((b) => b.textContent),
        active: (document.querySelector('#journal-filters .now-filter.active') || {}).dataset.kind || '',
        rows: document.querySelectorAll('.journal-row').length,
        empty: (document.querySelector('.journal-empty') || {}).textContent || '',
        hasOpenOrStatus: !row || !!(row.querySelector('.journal-open, .journal-status')),
        scrollW: document.documentElement.scrollWidth,
        innerW: window.innerWidth,
      };
    });
    console.log('journal', w, JSON.stringify(info));
    if (info.hash !== '#journal') failures.push(`${w}: hash ${info.hash}`);
    if (info.title !== 'Журнал') failures.push(`${w}: заголовок`);
    if (info.filters.join('|') !== 'Все|Рынок|Решения|Доставка') failures.push(`${w}: фильтры`);
    if (!info.hasOpenOrStatus) failures.push(`${w}: строка без действия`);
    if (info.scrollW > info.innerW + 1) failures.push(`${w}: journal scroll ${info.scrollW}>${info.innerW}`);
  }

  await page.setViewport({ width: 1440, height: 900 });
  await page.click('#journal-filters [data-kind="delivery"]');
  await page.waitForFunction(() => {
    const rows = [...document.querySelectorAll('.journal-row')];
    const empty = document.querySelector('.journal-empty');
    if (empty) return true;
    return rows.length > 0 && rows.every((r) => r.querySelector('.journal-status'));
  }, { timeout: 15000 });
  const delivery = await page.evaluate(() => ({
    kind: (document.querySelector('#journal-filters .now-filter.active') || {}).dataset.kind,
    opens: document.querySelectorAll('.journal-open').length,
    statuses: document.querySelectorAll('.journal-status').length,
  }));
  console.log('delivery', JSON.stringify(delivery));
  if (delivery.kind !== 'delivery') failures.push('фильтр доставки не включился');
  if (delivery.opens) failures.push('у доставки есть «Открыть событие»');

  await page.click('#journal-filters [data-kind="market"]');
  await new Promise((r) => setTimeout(r, 600));
  await page.goto(BASE + '#events', { waitUntil: 'networkidle2', timeout: 60000 });
  await new Promise((r) => setTimeout(r, 400));
  const alias = await page.evaluate(() => ({
    hash: location.hash,
    journal: document.getElementById('view-journal').classList.contains('active'),
  }));
  console.log('alias', JSON.stringify(alias));
  if (alias.hash !== '#journal' || !alias.journal) failures.push('#events не открыл журнал');

  for (const w of [390, 1024, 1440]) {
    await page.setViewport({ width: w, height: 900 });
    await page.goto(BASE + '#settings', { waitUntil: 'networkidle2', timeout: 60000 });
    await page.waitForSelector('#settings-notify .settings-field, #settings-rules .settings-field', { timeout: 20000 });
    const set = await page.evaluate(() => ({
      hash: location.hash,
      title: (document.querySelector('#view-settings .review-title') || {}).textContent || '',
      notify: !!document.getElementById('set-notify-title'),
      look: !!document.getElementById('set-look-title'),
      rules: !!document.getElementById('set-rules-title'),
      themes: [...document.querySelectorAll('[data-theme-choice]')].map((b) => b.dataset.themeChoice),
      tz: (document.querySelector('.settings-tz-value') || {}).textContent || '',
      recalc: (document.getElementById('settings-recalc') || {}).textContent || '',
      fields: document.querySelectorAll('#view-settings [data-key]').length,
      scrollW: document.documentElement.scrollWidth,
      innerW: window.innerWidth,
    }));
    console.log('settings', w, JSON.stringify(set));
    if (set.hash !== '#settings') failures.push(`${w}: settings hash`);
    if (set.title !== 'Настройки') failures.push(`${w}: settings title`);
    if (!set.notify || !set.look || !set.rules) failures.push(`${w}: нет трёх групп`);
    if (set.themes.join(',') !== 'system,dark,light') failures.push(`${w}: темы`);
    if (!set.tz.includes('Москва')) failures.push(`${w}: пояс`);
    if (!set.recalc) failures.push(`${w}: нет текста пересчёта`);
    if (!set.fields) failures.push(`${w}: нет полей`);
    if (set.scrollW > set.innerW + 1) failures.push(`${w}: settings scroll ${set.scrollW}>${set.innerW}`);
  }

  await page.click('[data-theme-choice="light"]');
  await new Promise((r) => setTimeout(r, 200));
  const light = await page.evaluate(() => ({
    theme: document.documentElement.dataset.theme,
    saved: localStorage.getItem('lf:theme'),
    pressed: document.querySelector('[data-theme-choice="light"]').getAttribute('aria-pressed'),
  }));
  console.log('light', JSON.stringify(light));
  if (light.theme !== 'light' || light.saved !== 'light' || light.pressed !== 'true') {
    failures.push('светлая тема не применилась');
  }
  await page.click('[data-theme-choice="dark"]');
  const dark = await page.evaluate(() => document.documentElement.dataset.theme);
  if (dark !== 'dark') failures.push('тёмная тема не вернулась');

  await browser.close();
  if (failures.length) {
    console.log('FAILURES:\n' + failures.join('\n'));
    process.exit(1);
  }
  console.log('OK');
}

main().catch((e) => { console.error(e); process.exit(1); });
