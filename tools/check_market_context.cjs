// UI smoke check with deterministic API fixtures; no live market data required.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');

(async () => {
  const browser = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const fixture = {
      market: {
        direction: 'long', arrow: 'up', market_line: '↑ LONG', subtitle: 'Направление по событиям и времени',
        events_direction: 'long', time_direction: 'neutral', quality: 'partial', evaluated_at: 1791810000000,
        coverage: { ok: 18, total: 20, sufficient: true }, unknown_dependencies: ['oi'],
        reasons: [{ group: 'events', side: 'long', text: 'События: Восстановление после массового закрытия сделок.' }],
        active_facts: Array.from({ length: 6 }, (_, i) => ({ fact_key: String(i), rule_id: 'LIQ_RECOVERY', direction: 'long', state: 'confirmed', occurred_at: 1791810000000, valid_until: 1791820000000 })),
        factor_agreement: { long: 2, short: 1 }, next_review_at: 1791820000000,
      },
      registry: [{ rule_id: 'LIQ_RECOVERY', rule_class: 'market_event', direction: 'long', confirmation: 'experimental', enabled: true, explanation: 'Восстановление после массового закрытия сделок.', window: '4 часа', formula: 'Проверка цены и закрытия сделок' }],
      sources: { price: { quality: 'ok' } }, bundles: [],
    };
    await page.setRequestInterception(true);
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.pathname === '/common.js') return request.respond({ contentType: 'text/javascript', body: `
        window.fixture=${JSON.stringify(fixture)}; window.calls={}; window.failEndpoint='';
        window.HTF={getToken:()=>true, esc:s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
          fmtTime:ms=>new Date(ms).toLocaleString('ru-RU',{timeZone:'Europe/Moscow'}),
          api:async endpoint=>{calls[endpoint]=(calls[endpoint]||0)+1; await new Promise(r=>setTimeout(r,80)); if(failEndpoint===endpoint)throw Error('Нет соединения'); return fixture;}};` });
      const file = path.join(__dirname, '../app/web/static', url.pathname.slice(1));
      if (fs.existsSync(file) && fs.statSync(file).isFile()) return request.respond({ contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'text/html', body: fs.readFileSync(file) });
      return request.respond({ status: 404, body: '' });
    });
    const settle = () => page.waitForFunction(() => document.querySelector('#events-body').getAttribute('aria-busy') === 'false');
    for (const theme of ['light', 'dark']) for (const width of [1440, 900, 390]) {
      await page.setViewport({ width, height: 1000 });
      await page.goto('http://market.local/events.html');
      await settle();
      await page.evaluate(t => document.documentElement.dataset.theme = t, theme);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `overflow: ${theme} ${width}`);
      assert.equal(await page.$eval('.market-hero', element => /LONG|SHORT/.test(element.textContent)), false);
      assert.equal(await page.$eval('.market-coverage span', element => element.textContent.trim()), '90%');
      assert.equal(await page.$eval('.market-coverage meter', element => element.value), 90);
      await page.click('.market-more summary');
      await page.evaluate(() => { fixture.market.evaluated_at += 60000; document.querySelector('#market-refresh').click(); document.querySelector('#market-refresh').click(); });
      await settle();
      assert.equal(await page.$eval('.market-more', element => element.open), true, 'expanded facts preserved');
      assert.equal(await page.evaluate(() => calls['/api/events/overview']), 2, 'duplicate refresh coalesced');
      for (const mode of ['feed', 'calendar', 'rules', 'sources', 'now']) {
        await page.click(`[data-mode="${mode}"]`); await settle();
        assert.equal(await page.$eval('#events-body', element => element.dataset.mode), mode);
      }
      await page.evaluate(() => { document.querySelector('[data-mode="feed"]').click(); document.querySelector('[data-mode="sources"]').click(); });
      await settle();
      assert.equal(await page.$eval('#events-body', element => element.dataset.mode), 'sources', 'latest mode wins');
      await page.evaluate(() => { failEndpoint='/api/events/overview'; document.querySelector('[data-mode="now"]').click(); }); await settle();
      assert.match(await page.$eval('#events-body', e => e.textContent), /Данные недоступны/);
      await page.evaluate(() => { failEndpoint=''; document.querySelector('#market-refresh').click(); }); await settle();
      assert.ok(await page.$('.market-hero'), 'retry restores view');
      if (width === 1440 || width === 390) await page.screenshot({ path: path.join(__dirname, `../data/market-context-v2-${theme}-${width}.png`), fullPage: true });
      for (const direction of ['short', 'neutral', 'mixed', 'unknown']) {
        await page.evaluate(d => { const lines={short:'↓ SHORT',neutral:'Нейтрально',mixed:'Противоречивый контекст',unknown:'Направление неизвестно'}; fixture.market.direction=d; fixture.market.market_line=lines[d]; document.querySelector('#market-refresh').click(); }, direction);
        await settle();
        assert.equal(await page.$eval('.market-hero', e => /LONG|SHORT/.test(e.textContent)), false);
      }
      console.log(`${theme} ${width}: OK`);
    }
    assert.deepEqual(errors, [], 'no JavaScript errors');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
