// Isolated browser regression: real layer/card renderers, no user's database.
const assert = require('node:assert/strict');
const path = require('node:path');
const puppeteer = require('../tools/node_modules/puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
  });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 1250, height: 650 });
    await page.setContent('<div id="zone-overlay" style="width:900px;height:600px"></div>'
      + '<div id="card" style="position:absolute;left:930px;width:290px"></div>');
    await page.addStyleTag({ path: path.resolve('app/web/static/style.css') });
    await page.addScriptTag({ path: path.resolve('app/web/static/h1_layers.js') });
    await page.addScriptTag({ path: path.resolve('app/web/static/messages.js') });
    const result = await page.evaluate(() => {
      const overlay = document.getElementById('zone-overlay');
      const setup = { lower: 81033.66, upper: 84168, eq: 82600.83,
        direction: 'bull', range_status: 'confirmed', origin_anchor: { at: 100 } };
      const zone = { type: 'FVG', direction: 'bull', lower: 83100, upper: 83255,
        formed_at: 200, display_until: 550, lifecycle: 'ended' };
      const draw = (history = false) => {
        overlay.replaceChildren();
        H1Layers.draw(overlay, {
          layers: { setup: { ...setup, history }, detected_zones: [zone, { ...zone, id: 2 }] },
          settings: { ...H1Layers.DEFAULTS, historicalZones: !history },
          xOf: (ms) => ms, yOf: (price) => (84500 - price) / 8,
          paneRight: 900, height: 600,
          fmtPrice: (p) => Number(p).toLocaleString('ru-RU'),
        });
      };
      draw();
      const lines = [...overlay.querySelectorAll('.h1-pd-line')];
      const pdPositions = lines.map((line) => getComputedStyle(line).position);
      const endedRight = [...overlay.querySelectorAll('.h1-zone')].map((z) =>
        parseFloat(z.style.left) + parseFloat(z.style.width));
      const captions = [...overlay.querySelectorAll('.h1-zone-name')].map((el) => el.textContent);
      const pdCount = overlay.querySelectorAll('.h1-pd-band').length;
      const asset = { governs: true, asset_state: {
        title: 'LONG · ждём откат', direction: 'long', code: 'long_wait',
      }, pd: setup, compact_rows: [
        { kind: 'entry', k: 'Вход', v: 'FVG: 81 959–82 148', note: 'в discount' },
        { kind: 'entry', k: '', v: 'OB: 81 033–81 959', note: 'в discount' },
      ] };
      const card = document.getElementById('card');
      card.innerHTML = LFCopy.card({ asset }, { hideDetails: true, simpleTradeTitle: true })
        + '<button class="btn desk-watch is-on">✓ Наблюдение включено</button>';
      const title = card.querySelector('h3').textContent.replace(/\s/g, '');
      const details = !!card.querySelector('.asset-more');
      const visual = !!card.querySelector('.sc-visual');
      const on = getComputedStyle(card.querySelector('button')).backgroundColor;
      card.querySelector('button').className = 'btn desk-watch is-off';
      const off = getComputedStyle(card.querySelector('button')).backgroundColor;
      card.querySelector('button').className = 'btn desk-watch is-on';
      const notes = card.querySelectorAll('.msg-note').length;
      draw(true);
      const historicalPdCount = overlay.querySelectorAll('.h1-pd-band, .h1-pd-line').length;
      draw();
      return { pdPositions, endedRight, captions, pdCount, title, details, visual,
        on, off, notes, historicalPdCount };
    });
    assert.deepEqual(result.pdPositions, ['absolute', 'absolute', 'absolute']);
    assert.deepEqual(result.endedRight, [550, 550]);
    assert.ok(result.captions.length && result.captions.every((c) => !c.includes('×')));
    assert.equal(result.pdCount, 2);
    assert.equal(result.title, 'LONG(81033–82600)');
    assert.equal(result.details, false);
    assert.equal(result.visual, false);
    assert.equal(result.notes, 2);
    assert.equal(result.on, 'rgb(40, 119, 84)');
    assert.equal(result.off, 'rgb(173, 63, 73)');
    assert.equal(result.historicalPdCount, 0);
    await page.screenshot({ path: 'tmp/desk-zone-display-check.png' });
    console.log('Desk zone display: passed');
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
