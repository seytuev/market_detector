// Exercise the production desk/review renderers against a real chart, offline.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('../tools/node_modules/puppeteer-core');
const root = path.resolve(__dirname, '..');
const app = fs.readFileSync(path.join(root, 'app/web/static/app.js'), 'utf8');
const part = (start, end) => app.slice(app.indexOf(start), app.indexOf(end, app.indexOf(start)));

(async () => {
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME_BIN || (process.platform === 'win32'
      ? 'C:/Program Files/Google/Chrome/Application/chrome.exe' : '/usr/bin/google-chrome'),
    headless: true, args: ['--no-sandbox'],
  });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewport({ width: 1200, height: 800 });
    await page.setRequestInterception(true);
    page.on('request', request => request.respond({ status: 200, contentType: 'text/html', body: '<!doctype html><body></body>' }));
    await page.goto('http://chart.test');
    await page.addStyleTag({ path: path.join(root, 'app/web/static/style.css') });
    await page.addScriptTag({ path: path.join(root, 'app/web/static/vendor/lightweight-charts.js') });
    await page.addScriptTag({ path: path.join(root, 'app/web/static/common.js') });
    await page.evaluate(() => {
      document.body.innerHTML = '<label><input type="checkbox" id="layer-history">История HTF</label>'
        + '<label><input type="checkbox" id="layer-swept">Снятые BSL/SSL · 7 дней</label>'
        + '<input type="checkbox" id="layer-mids" checked><input type="checkbox" id="layer-labels" checked>'
        + '<div style="position:relative;width:1100px;height:650px"><div id="chart" style="height:650px"></div>'
        + '<div id="zone-overlay"></div><div id="review-zone-overlay"></div></div>';
      window.$ = id => document.getElementById(id);
      window.fmtPrice = HTF.fmtPrice; window.fmtTime = HTF.fmtTime;
      window.STATUS_RU = { active: 'активна', taken: 'снята', weakened: 'ослаблена' };
      window.TF_SECONDS = { D1: 86400, W1: 604800 };
      window.isHtf = z => ['D1', 'W1'].includes(z.timeframe);
      window.drawH1Breaks = window.drawDailyStructure = window.openZoneDetail = () => {};
      window.state = { timeframe: 'D1', chartMode: 'context', showAllTf: false,
        showCandidates: false, maxChartZones: 80, innerLevels: [], groups: [] };
      const start = Math.floor(Date.now() / 86400000) * 86400 - 100 * 86400;
      state.candles = Array.from({ length: 100 }, (_, i) => ({
        time: start + i * 86400, open: .050 + .006 * Math.sin(i / 8),
        high: .061, low: .035, close: .051 + .005 * Math.sin(i / 8),
      }));
      state.lastPrice = .05444;
      state.chart = LightweightCharts.createChart($('chart'), { width: 1100, height: 650,
        layout: { background: { color: '#ffffff' }, textColor: '#333333' } });
      state.candleSeries = state.chart.addCandlestickSeries({ priceFormat: HTF.chartPriceFormat(state.candles) });
      state.candleSeries.setData(state.candles);
      state.chart.timeScale().fitContent();
      const zone = { id: 1, type: 'fvg', direction: 'bull', timeframe: 'D1',
        lower: .0492, upper: .05532, mid: .05226, formed_at: (start + 20 * 86400) * 1000,
        status: 'weakened', relevant: true, max_test_depth: 0, fill_depth: .5, display_until: null };
      state.zones = [zone,
        { ...zone, id: 2, type: 'breaker', lower: .04764, upper: .05219, mid: .049915 },
        { ...zone, id: 3, lower: .050, upper: .051, display_until: (start + 30 * 86400) * 1000, relevant: false },
        { ...zone, id: 4, type: 'ssl', is_level: true, lower: .040, upper: .040,
          status: 'taken', display_until: Date.now() - 86400000, relevant: false },
        { ...zone, id: 5, type: 'bsl', is_level: true, lower: .058, upper: .058, status: 'active' },
      ];
      state.groups = [{ id: 1, zone_ids: [1, 3] }];
      window.reviewState = { zone, chart: state.chart, candleSeries: state.candleSeries, candles: state.candles };
    });
    // Source extraction keeps fixtures isolated from auth, feeds and databases.
    await page.addScriptTag({ content: [
      part('function sweptLevelVisible(', 'function structurePointColor('),
      part('function barCoordinate(', '// HTF Zones: в интерфейсе'),
      part('function swatchDirClass(', 'function safeSwatchClass('),
      part('function h1PaneHeight(', 'function h1VisibleView('),
      part('function distanceToZone(', 'function zoneRole('),
      part('function filteredZones(', '// Мини-схема положения цены'),
      part('function drawZones(', 'function hideH1Chrome('),
      part('function drawReviewZone(', 'const VERDICT_GEOM_RU'),
    ].join('\n') });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const result = await page.evaluate(() => {
      const ids = () => [...$('zone-overlay').querySelectorAll('.zone-rect')].map(el => Number(el.dataset.zoneId));
      const rect = id => $('zone-overlay').querySelector(`[data-zone-id="${id}"]`);
      drawZones();
      const initial = ids();
      const counts = zoneBucketCounts();
      const currentRows = filteredZones().map(z => z.id);
      state.zoneBucket = 'archive';
      const archivedRows = filteredZones().map(z => z.id);
      state.zoneBucket = 'live';
      const height = parseFloat(rect(1).style.height);
      const expectedHeight = Math.abs(state.candleSeries.priceToCoordinate(.05532) - state.candleSeries.priceToCoordinate(.0492));
      const fillHeight = parseFloat(rect(1).querySelector('.zone-fill').style.height);
      state.selectedZoneId = 1; drawZones();
      const selectedHeight = parseFloat(rect(1).style.height);
      state.selectedZoneId = null;
      $('layer-swept').checked = true; drawZones();
      const swept = ids();
      const level = rect(4), levelStyle = getComputedStyle(level);
      const levelHeight = level.getBoundingClientRect().height;
      const borderBottom = levelStyle.borderBottomWidth;
      const levelEnd = parseFloat(level.style.left) + parseFloat(level.style.width);
      const expectedEnd = Math.min($('chart').clientWidth - state.chart.priceScale('right').width(),
        barCoordinate(state.chart.timeScale(), state.zones[3].display_until));
      $('layer-history').checked = true; drawZones();
      const history = ids();
      const historicEnd = parseFloat(rect(3).style.left) + parseFloat(rect(3).style.width);
      const historyExpectedEnd = barCoordinate(state.chart.timeScale(), state.zones[2].display_until);
      $('layer-mids').checked = false; $('layer-labels').checked = false; drawZones();
      const hiddenDecorations = !$('zone-overlay').querySelector('.zone-mid, .zone-label');
      // History must never evict live context when the chart budget is full.
      state.maxChartZones = 2; drawZones(); const budget = ids(); state.maxChartZones = 80;
      const chartEl = $('chart'); chartEl.id = 'review-chart';
      drawReviewZone();
      const review = $('review-zone-overlay').querySelector('.zone-rect');
      const reviewHeight = parseFloat(review.style.height);
      const reviewFillHeight = parseFloat(review.querySelector('.zone-fill').style.height);
      chartEl.id = 'chart'; $('review-zone-overlay').replaceChildren();
      $('layer-history').checked = false; $('layer-swept').checked = false; drawZones();
      return { initial, counts, currentRows, archivedRows, height, expectedHeight, fillHeight, selectedHeight, swept,
        history, levelHeight, borderBottom, levelEnd, expectedEnd, historicEnd, historyExpectedEnd,
        hiddenDecorations, budget, reviewHeight, reviewFillHeight,
        precision: state.candleSeries.options().priceFormat.precision,
        swatches: ['bsl', 'BSL', 'ssl', 'SSL'].map(type => swatchDirClass({type, direction:type.toLowerCase() === 'bsl' ? 'bear' : 'bull'})) };
    });
    assert.deepEqual(result.initial.sort(), [1, 2, 5]);
    assert.deepEqual(result.counts, { live: 3, archive: 2 });
    assert.deepEqual(result.currentRows.sort(), [1, 2, 5]);
    assert.deepEqual(result.archivedRows.sort(), [3, 4]);
    assert.ok(Math.abs(result.height - result.expectedHeight) < .02, 'Overlapping zones retain full bounds');
    assert.equal(result.selectedHeight, result.height, 'Selecting a zone must not change its geometry');
    assert.ok(Math.abs(result.fillHeight - result.height / 2) < .02);
    assert.ok(result.swept.includes(4) && !result.swept.includes(3));
    assert.ok(result.history.includes(3));
    assert.equal(result.levelHeight, 1);
    assert.equal(result.borderBottom, '0px');
    assert.ok(Math.abs(result.levelEnd - result.expectedEnd) < .02);
    assert.ok(Math.abs(result.historicEnd - result.historyExpectedEnd) < .02);
    assert.ok(result.hiddenDecorations);
    assert.ok(!result.budget.includes(3) && !result.budget.includes(4));
    assert.equal(result.reviewHeight, result.height);
    assert.equal(result.reviewFillHeight, result.fillHeight);
    assert.equal(result.precision, 6);
    assert.deepEqual(result.swatches, ['dir-bull', 'dir-bull', 'dir-bear', 'dir-bear']);
    assert.deepEqual(errors, []);
    fs.mkdirSync(path.join(root, 'tmp/stabilization-ui'), { recursive: true });
    await page.screenshot({ path: path.join(root, 'tmp/stabilization-ui/htf-zones.png') });
    console.log('HTF browser: full geometry, history, sweeps, groups, review, colors, precision and budget passed');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
