// Real DOM/chart regression for dense HTF + LTF captions, without market feeds.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('../tools/node_modules/puppeteer-core');
const root = path.resolve(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'app/web/static/app.js'), 'utf8');
const drawDaily = app.slice(app.indexOf('function drawDailyStructure('), app.indexOf('function drawH1Breaks('));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true,
  });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setViewport({ width: 1200, height: 640 });
    await page.setContent('<input id="layer-structure-d1" type="checkbox" checked>'
      + '<div id="frame" style="position:relative;width:1160px;height:550px">'
      + '<div id="chart" style="height:550px"></div>'
      + '<div id="zone-overlay" style="position:absolute;inset:0;pointer-events:none"></div></div>');
    for (const css of ['style.css', 'redesign.css']) {
      await page.addStyleTag({ path: path.join(root, 'app/web/static', css) });
    }
    for (const js of ['vendor/lightweight-charts.js', 'h1_layers.js']) {
      await page.addScriptTag({ path: path.join(root, 'app/web/static', js) });
    }
    await page.evaluate(() => {
      window.$ = id => document.getElementById(id);
      window.fmtPrice = p => Number(p).toFixed(2);
      window.fmtTime = ms => String(ms);
      const start = 1700000000, day = 86400;
      const bars = Array.from({ length: 120 }, (_, i) => {
        const price = 100 + 1.5 * Math.sin(i / 5);
        return { time: start + i * day, open: price, close: price + Math.sin(i),
          high: price + 3, low: price - 3 };
      });
      const at = i => bars[i].time * 1000;
      const pivots = [12, 25, 40, 62, 76, 83, 85, 87, 89, 91, 93, 95, 97, 99, 102, 104]
        .map((i, n) => ({ id: n + 1, pivot_at: at(i), confirmed_at: at(i + 3),
          role: n % 2 ? 'LH' : 'HH', kind: 'high', price: bars[i].high }));
      // Coincident labels must become dots/hover details rather than overlap.
      pivots.push(...Array.from({ length: 4 }, (_, i) => ({ ...pivots.at(-1), id: 30 + i })));
      const events = [20, 44, 66, 88, 96, 104].map((i, n) => ({
        kind: n % 2 ? 'SMS' : 'BOS', direction: 'bull', anchor_at: at(i - 12),
        open_time: at(i), occurred_at: at(i), level: bars[i - 12].high,
      }));
      window.state = { candles: bars, dailyStructure: { text: 'Структура D1 · SMS', data_quality: 'ok',
        pivots, events, expected: { bull: {
          bos: { kind: 'BOS', status: 'ready', level: bars[104].high, pivot: pivots.at(-1) },
          sms: { kind: 'SMS', status: 'ready', level: bars[104].high - .04, pivot: pivots.at(-1) },
        } } } };
      state.chart = LightweightCharts.createChart($('chart'), { width: 1160, height: 550 });
      state.candleSeries = state.chart.addCandlestickSeries();
      state.candleSeries.setData(bars);
      state.chart.timeScale().fitContent();
      window.h1 = { pivot_markers: pivots.slice(-10),
        anchor_refs: [{ id: 100, pivot_at: at(67), price: 98, role: 'LL', kind: 'low' }],
        structural_events: [{ id: 'h1-event', kind: 'SMS', direction: 'bear',
          level_pivot_id: 100, break_level: 98, break_candle_open_time: at(98) }],
        expected_structure: { bos: { level: 97, direction: 'bear',
          label: 'H1 ждём BOS ↓', ref_pivot: { pivot_at: at(94) } } }, detected_zones: [] };
      window.eventClicks = 0;
      state.candleSeries.setMarkers(H1Layers.seriesMarkers(h1, null));
    });
    await page.addScriptTag({ content: drawDaily });
    const settle = () => page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const render = async () => {
      await settle();
      return page.evaluate(() => {
        const overlay = $('zone-overlay'), ts = state.chart.timeScale();
        overlay.replaceChildren();
        const width = state.chart.paneSize().width, height = state.chart.paneSize().height;
        H1Layers.draw(overlay, { layers: h1, settings: H1Layers.DEFAULTS, deferLayout: true,
          xOf: ms => ts.timeToCoordinate(ms / 1000), yOf: p => state.candleSeries.priceToCoordinate(p),
          paneRight: width, height, onEvent: () => { eventClicks += 1; } });
        drawDailyStructure(overlay, ts, width, height, true);
        const lines = () => Array.from(overlay.querySelectorAll('.ltf-break, .ltf-expected'))
          .map(el => [el.style.left, el.style.top, el.style.width]);
        const before = lines();
        H1Layers.layoutStructureLabels(overlay, width, height);
        window.labelFixture = { overlay, width, height };
        return { before, after: lines() };
      });
    };
    const check = async label => {
      const geometry = await render();
      assert.deepEqual(geometry.after, geometry.before, `${label}: price/time anchors changed`);
      const result = await page.evaluate(() => {
        const { overlay, width, height } = labelFixture;
        const origin = overlay.getBoundingClientRect();
        const rect = el => {
          const b = el.getBoundingClientRect();
          return { text: el.textContent, title: el.title, left: b.left - origin.left,
            right: b.right - origin.left, top: b.top - origin.top, bottom: b.bottom - origin.top };
        };
        const visible = Array.from(overlay.querySelectorAll('.structure-caption'))
          .filter(el => getComputedStyle(el).visibility !== 'hidden').map(rect);
        const overlaps = [];
        for (let i = 0; i < visible.length; i++) for (let j = i + 1; j < visible.length; j++) {
          const a = visible[i], b = visible[j];
          if (a.left < b.right && a.right > b.left && a.top < b.bottom && a.bottom > b.top) overlaps.push([a, b]);
        }
        const outside = visible.filter(r => r.left < 0 || r.right > width + .1 || r.top < 0 || r.bottom > height + .1);
        const dots = Array.from(overlay.querySelectorAll('.structure-caption-dot')).map(rect);
        const hoverableDots = Array.from(overlay.querySelectorAll('.structure-caption-dot')).every(el => {
          const b = el.getBoundingClientRect();
          return document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2) === el;
        });
        const before = visible;
        H1Layers.layoutStructureLabels(overlay, width, height);
        const after = Array.from(overlay.querySelectorAll('.structure-caption'))
          .filter(el => getComputedStyle(el).visibility !== 'hidden').map(rect);
        return { visible, overlaps, outside, dots, hoverableDots, before, after,
          expected: visible.filter(r => /D1 ждём/.test(r.text)),
          blankMarkerText: H1Layers.seriesMarkers(h1, null).every(m => m.text === '') };
      });
      assert.deepEqual(result.overlaps, [], `${label}: labels overlap`);
      assert.deepEqual(result.outside, [], `${label}: caption crosses chart axes`);
      assert.deepEqual(result.after, result.before, `${label}: repeated layout moves labels or grows tooltips`);
      assert.ok(result.visible.length > 4, `${label}: must preserve readable structure`);
      assert.ok(result.expected.length, `${label}: current expectations have priority`);
      assert.ok(result.blankMarkerText, `${label}: native text must not duplicate DOM labels`);
      assert.ok(result.hoverableDots, `${label}: dots must remain reachable for hover details`);
      assert.ok(result.dots.every(dot => dot.title.includes('подтверждение') || dot.title.includes('экстремум')));
      console.log(`${label}: ${result.visible.length} visible, ${result.dots.length} dots, no overlaps`);
      return result;
    };
    await check('Desktop');
    await page.evaluate(() => state.chart.timeScale().setVisibleLogicalRange({ from: 58, to: 125 }));
    await check('Zoom');
    fs.mkdirSync(path.join(root, 'tmp'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'tmp/structure-labels-desktop.png') });
    await page.evaluate(() => document.querySelector('.h1-break-label.structure-caption')?.click());
    assert.equal(await page.evaluate(() => eventClicks), 1, 'Event caption still opens the event');
    await page.setViewport({ width: 390, height: 640 });
    await page.evaluate(() => {
      $('frame').style.width = '374px';
      state.chart.resize(374, 550);
      state.chart.timeScale().fitContent();
    });
    const compact = await check('Mobile / compressed history');
    assert.ok(compact.dots.length > 0, 'Compressed extrema should keep hover dots');
    await page.screenshot({ path: path.join(root, 'tmp/structure-labels-mobile.png') });
    await page.evaluate(() => document.documentElement.dataset.theme = 'dark');
    await check('Dark theme');
    await page.evaluate(() => {
      $('layer-structure-d1').checked = false;
      H1Layers.saveSettings({ points: 'hidden' });
      h1.pivot_markers = [];
    });
    await render();
    assert.equal(await page.$$eval('.d1-structure-label', els => els.length), 0, 'D1 toggle remains effective');
    const selected = await page.evaluate(() => {
      const { overlay, width, height } = labelFixture;
      overlay.replaceChildren();
      H1Layers.draw(overlay, { layers: h1, settings: { ...H1Layers.DEFAULTS, points: 'hidden' },
        selectedEventId: 'h1-event', paneRight: width, height,
        xOf: ms => state.chart.timeScale().timeToCoordinate(ms / 1000),
        yOf: p => state.candleSeries.priceToCoordinate(p) });
      return Array.from(overlay.querySelectorAll('.ltf-pivot')).map(el => el.textContent);
    });
    assert.deepEqual(selected, ['LL'], 'Selected event preserves its temporary anchor label with points hidden');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
