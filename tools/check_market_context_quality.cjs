// Browser regression for event memory and source coverage, using explicit fixtures.
const puppeteer = require('puppeteer-core');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');

(async () => {
  const out = path.resolve(__dirname, '../tmp/market-context-quality');
  fs.mkdirSync(out, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true,
  });
  const fixture = {
    market_age_seconds: 300, market_stale: false,
    market: {
      direction: 'long', arrow: 'up', market_line: '↑ LONG', events_direction: 'long',
      time_direction: 'neutral', time_rules_active: false, quality: 'partial',
      evaluated_at: 1792084168688, coverage: { ok: 20, total: 20, sufficient: true },
      liquidations: { coverage: { ok: 9, total: 20 }, baseline_ok: true },
      memory_policy: { hours: 240 },
      methodology: { label: 'Экспериментальная модель корзины · Binance · окна 4ч',
        study_scope: 'Исследование: BTC, дневные ликвидации на 10 биржах',
        limitations: ['OI не участвует в общей оценке направления этой модели.'] },
      reasons: [{ group: 'events', side: 'long', rule_id: 'LIQ_RECOVERY', text: 'События: Каскад завершён, рынок восстанавливается.' }],
      active_facts: Array.from({ length: 5 }, (_, i) => ({
        fact_key: String(i), rule_id: 'LIQ_PRESSURE', effective_rule_id: i ? 'LIQ_PRESSURE' : 'LIQ_RECOVERY',
        direction: i ? 'short' : 'long', state: 'confirmed', occurred_at: 1791306000000,
        valid_until: 1792170000000, phase_at: 1792000000000,
        context_status: i ? 'background' : 'driving', used_for_direction: !i,
      })),
      factor_agreement: { long: 1, short: 0 }, time_context: {},
      statistics: { available: true, text: 'Частота роста', n: 100,
        uncertainty: { low: 0.5, high: 0.7 }, period: { from: 1791306000000, to: 1792084168688 } },
      btc_background: { evidence: { summary: 'Крупные лонг-ликвидации совпали с ростом OI.',
        scope_note: 'BTC · ликвидации Binance · OI в монетах · дни UTC',
        method_note: 'Проектная адаптация', recent_events: [] } },
    },
  };
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setRequestInterception(true);
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.pathname === '/common.js') return request.respond({ contentType: 'text/javascript', body: `
        window.fixture=${JSON.stringify(fixture)};
        window.HTF={getToken:()=>true, ensureToken:async()=>true,
          esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
          fmtTime:ms=>new Date(ms).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'}),
          api:async(url)=>{(window.apiCalls??=[]).push(url);return url.includes('/market/assessment/') ? fixture.market : fixture}};` });
      const file = path.resolve(__dirname, '../app/web/static', url.pathname.slice(1));
      if (fs.existsSync(file) && fs.statSync(file).isFile()) {
        const type = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css'
          : file.endsWith('.png') ? 'image/png' : file.endsWith('.svg') ? 'image/svg+xml' : 'text/html';
        return request.respond({ contentType: type, body: fs.readFileSync(file) });
      }
      return request.respond({ status: 404, body: '' });
    });
    const settle = () => page.waitForFunction(() => document.querySelector('#events-body').getAttribute('aria-busy') === 'false');
    for (const theme of ['light', 'dark']) for (const width of [1440, 900, 390]) {
      await page.setViewport({ width, height: 1000 });
      await page.goto('http://market.local/events.html');
      await settle();
      await page.evaluate(t => document.documentElement.dataset.theme = t, theme);
      assert.ok(await page.$('.market-hero'));
      assert.doesNotMatch(await page.$eval('.market-hero', el => el.textContent), /мин назад|Оценка устарела/);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
      assert.match(await page.$eval('.market-quality', el => el.textContent), /9 из 20 монет/);
      assert.match(await page.$eval('.market-methodology', el => el.textContent), /OI не участвует/);
      assert.match(await page.$eval('.market-time', el => el.textContent), /пока не включены/);
      const first = await page.$eval('.market-fact', el => el.textContent);
      assert.match(first, /Восстановление|восстановлен/);
      assert.match(first, /Учтён в показанной оценке/);
      assert.match(first, /В памяти модели до/);
      assert.doesNotMatch(first, /Влияет на оценку до/);
      assert.doesNotMatch(await page.$eval('.market-statistics', el => el.textContent), /\[object Object\]/);
      assert.match(await page.$eval('.market-statistics', el => el.textContent), /50.*70%/);
      assert.match(await page.$eval('.market-bitcoin', el => el.textContent), /ростом OI/);
      await page.click('.market-more summary');
      await page.click('#market-refresh'); await settle();
      assert.equal(await page.$eval('.market-more', el => el.open), true);
      if (theme === 'light' && width === 1440) {
        await page.evaluate(() => {
          fixture.market_age_seconds = 14400; fixture.market_stale = false;
          document.querySelector('#market-refresh').click();
        });
        await settle();
        assert.doesNotMatch(await page.$eval('.market-hero', el => el.textContent), /ч назад|Оценка устарела/);
        await page.evaluate(() => {
          fixture.market_age_seconds = 14401; fixture.market_stale = true;
          document.querySelector('#market-refresh').click();
        });
        await settle();
        assert.match(await page.$eval('.market-hero', el => el.textContent), /4 ч назад/);
        assert.match(await page.$eval('.market-hero', el => el.textContent), /Оценка устарела/);
        await page.evaluate(() => {
          fixture.market_age_seconds = 300; fixture.market_stale = false;
          document.querySelector('#market-refresh').click();
        });
        await settle();
      }
      if (width !== 900) {
        await page.$eval('.market-header', el => el.scrollIntoView({ block: 'start' }));
        await page.screenshot({ path: path.join(out, `${theme}-${width}.png`), fullPage: true });
        await page.$eval('.market-facts', el => el.scrollIntoView({ block: 'start' }));
        await page.screenshot({ path: path.join(out, `${theme}-${width}-memory.png`) });
      }
      // Saved assessments from before this change must remain readable.
      await page.evaluate(() => {
        delete fixture.market.methodology; delete fixture.market.memory_policy;
        delete fixture.market.liquidations;
        document.querySelector('#market-refresh').click();
      });
      await settle();
      assert.ok(await page.$('.market-hero'));
      await page.evaluate(data => { window.fixture.market = data.market; }, fixture);
      console.log(`${theme} ${width}: OK`);
    }
    for (const reference of ['?assessment=notice-snapshot', '#assessment=notice-legacy']) {
      await page.goto('http://market.local/events.html' + reference); await settle();
      await page.waitForSelector('.events-saved-assessment');
      const id = reference.split('=')[1];
      assert.ok(await page.evaluate(id => window.apiCalls.includes('/api/events/market/assessment/' + id), id));
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
