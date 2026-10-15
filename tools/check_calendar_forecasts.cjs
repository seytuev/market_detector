// Browser checks use explicit synthetic responses; not a market backtest.
const puppeteer = require('puppeteer-core');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

const TODAY = Date.parse('2026-10-15T00:00:00+03:00');
function response(url) {
  const date = url.searchParams.get('day') || '2026-10-15';
  const scope = url.searchParams.get('scope') || 'BTC';
  const target = Date.parse(date + 'T00:00:00+03:00');
  const future = target > TODAY;
  const base = { n: 200, up: 110, down: 85, flat: 5, available: true, up_frequency: .55,
    period: { from: TODAY - 200 * 86400000, to: target - 86400000 } };
  const sample = { id: 'weekday', label: 'День недели: четверг', condition_known: true,
    n: 100, up: scope === 'BTC' ? 60 : 40, down: scope === 'BTC' ? 40 : 60, flat: 0,
    available: true, min_n: 100, up_frequency: scope === 'BTC' ? .6 : .4,
    down_frequency: scope === 'BTC' ? .4 : .6, up_interval: { low: .5, high: .69 },
    mean_return: .0023, median_return: .001, mean_range: scope === 'BTC' ? .045 : null,
    range_n: scope === 'BTC' ? 100 : 0, period: base.period, lift_pp: 5 };
  const previous = { ...sample, id: 'previous_day', label: 'После дня, закрытого снижением',
    condition_known: !future, reason: 'Предыдущий день ещё не закрыт.', available: !future };
  const small = { ...sample, id: 'streak', label: 'После серии 3 дней снижения', n: 21,
    up: 11, down: 10, range_n: 21, available: false, up_frequency: null };
  return { market: { upcoming: [], time_context: { completed_days: [
    { day_start_ms: TODAY - 86400000, sign: -1, median_return: -.02 },
    { day_start_ms: TODAY - 2 * 86400000, sign: null, median_return: null },
  ] }, forecast: { scope, scope_label: scope === 'BTC' ? 'BTCUSDT · Binance Spot' : 'Медиана исторической корзины рынка',
    target_day: target, mode: future ? 'future' : target < TODAY ? 'reconstruction' : 'current',
    baseline: base, history: { quality: scope === 'BTC' ? 'backfilling' : 'accumulating', days: 200, remaining_days: 100 },
    previous_days: [3, 2, 1].map(d => ({ day_start_ms: target - d * 86400000, sign: future && d === 1 ? null : -1 })),
    patterns: [sample, previous, small], method_note: 'Историческая частота, не вероятность сделки.',
    range_note: 'Пороги — адаптация проекта.' } } };
}

(async () => {
  const out = path.resolve(__dirname, '../tmp/calendar-forecasts');
  fs.mkdirSync(out, { recursive: true });
  const browser = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
  try {
    const page = await browser.newPage();
    const errors = [], requests = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.evaluateOnNewDocument(() => { Date.now = () => Date.parse('2026-10-15T14:00:00+03:00'); });
    await page.setRequestInterception(true);
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.pathname === '/api/events/calendar') {
        requests.push(url.search);
        return request.respond({ contentType: 'application/json', body: JSON.stringify(response(url)) });
      }
      if (url.pathname === '/common.js') return request.respond({ contentType: 'text/javascript', body: `
        window.HTF={getToken:()=>true,ensureToken:async()=>true,
          esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
          fmtTime:ms=>new Date(ms).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'}),
          api:async path=>{const r=await fetch(path);if(!r.ok)throw Error(r.status);return r.json()}};` });
      const file = path.resolve(__dirname, '../app/web/static', url.pathname.slice(1));
      if (fs.existsSync(file) && fs.statSync(file).isFile()) {
        const type = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html';
        return request.respond({ contentType: type, body: fs.readFileSync(file) });
      }
      return request.respond({ status: 404, body: '' });
    });
    const settle = () => page.waitForFunction(() => document.querySelector('#events-body').getAttribute('aria-busy') === 'false');
    for (const theme of ['light', 'dark']) for (const width of [1440, 900, 390]) {
      await page.setViewport({ width, height: 1100 });
      await page.goto('http://market.local/events.html?tab=calendar'); await settle();
      await page.evaluate(t => document.documentElement.dataset.theme = t, theme);
      assert.match(await page.$eval('.calendar-forecast', el => el.textContent), /Рост 60%/);
      assert.match(await page.$eval('.calendar-counts', el => el.textContent), /Совпадений: 100/);
      assert.match(await page.$eval('.calendar-forecast', el => el.textContent), /нужно не менее 100/);
      assert.match(await page.$eval('[data-calendar-day="2026-10-13"]', el => el.textContent), /—/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      await page.screenshot({ path: path.join(out, `${theme}-${width}.png`), fullPage: true });
      await page.$eval('.calendar-patterns', el => el.scrollIntoView({ block: 'start' }));
      await page.screenshot({ path: path.join(out, `${theme}-${width}-statistics.png`) });
      await page.click('[data-calendar-day="2026-10-14"]'); await settle();
      assert.match(await page.$eval('.calendar-forecast', el => el.textContent), /Реконструкция/);
      await page.click('.lf-calendar-agenda [data-calendar-day="2026-10-15"]'); await settle();
      assert.match(requests.at(-1), /day=2026-10-15/);
      await page.click('[data-calendar-scope="basket"]'); await settle();
      assert.match(requests.at(-1), /scope=basket/);
      assert.match(await page.$eval('.calendar-forecast', el => el.textContent), /Рост 40%/);
      await page.click('[data-calendar-day="2026-10-16"]'); await settle();
      assert.match(await page.$eval('.calendar-forecast', el => el.textContent), /Предыдущий день ещё не закрыт/);
      await page.click('[data-calendar-shift="-1"]'); await settle();
      assert.match(requests.at(-1), /day=2026-09-01/);
      assert.match(await page.$eval('.lf-calendar-head', el => el.textContent), /сентябрь/i);
      console.log(`${theme} ${width}: OK`);
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
