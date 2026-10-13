// Current H1 workspace layout on an isolated server, with deterministic bars/zones.
const assert = require('node:assert/strict');
const puppeteer = require('../tools/node_modules/puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewport({ width: 1800, height: 1000 });
    await page.goto((process.env.SYNC_TEST_URL || 'http://127.0.0.1:8101') + '/?token=dev-token&instrument=1#desk', { waitUntil: 'networkidle2' });
    await page.waitForFunction(() => state.chart && state.candles.length);
    const result = await page.evaluate(async () => {
      setChartMode('h1');
      const lastTime = state.candles.at(-1).time;
      const bars = Array.from({ length: 1500 }, (_, i) => {
        const close = i < 1400 ? 30000 + i * 35 : 82400 + Math.sin(i / 7) * 1300;
        return { time: lastTime - (1499 - i) * 3600, open: close - 100,
          high: close + 150, low: close - 250, close };
      });
      state.candles = bars;
      state.lastPrice = bars.at(-1).close;
      state.candleSeries.setData(bars);
      state.h1Layers = null;
      applyH1LegFrame(null);
      const initialFrame = state.chart.timeScale().getVisibleLogicalRange();
      const formed = bars[1484].time * 1000;
      const regions = [
        { type: 'FVG', lower: 81959.53, upper: 82418.47, admitted: [81959.53, 82418.47] },
        { type: 'FVG', lower: 82471.18, upper: 82783, admitted: [82471.18, 82600.83] },
        { type: 'OB', lower: 81033.66, upper: 81959.53, admitted: [81033.66, 81959.53] },
        { type: 'SSL', lower: 81033.66, upper: 81033.66, admitted: [81033.66, 81033.66] },
      ].map(z => ({ ...z, direction: 'bull', formed_at: formed,
        confirmed_at: formed + 3600000, reason: 'ok', status: 'confirmed' }));
      const current = regions.map((z, i) => ({ ...z, id: 'scan:' + z.type + ':' + i,
        lifecycle: 'active', max_test_depth: .15, relevance: { relevant: true, reason: 'ok' },
        idea_links: Array.from({ length: 20 }, (_, j) => ({ scenario_id: j, direction: 'bull',
          state: 'active', parent_type: 'SSL', parent_timeframe: 'D1', trigger_at: formed })) }));
      const archive = Array.from({ length: 535 }, (_, i) => ({ id: i, type: 'FVG', direction: 'bear',
        lower: 91000 + i, upper: 92000 + i, formed_at: bars[100].time * 1000, lifecycle: 'active' }));
      const setup = { movement_id: 2, direction: 'bull', lower: 81033.66, upper: 84168, eq: 82600.83,
        frame_from: bars[500].time * 1000, origin_anchor: { at: formed, price: 81033.66 },
        range_status: 'confirmed', setup_status: 'trade', eligible_regions: [...regions, regions[0]], candidates: regions };
      state.h1Layers = { setup, detected_zones: [...archive, ...current, ...current], structural_events: [] };
      deskData.current = { instrument: state.instruments[0], data_state: { state: 'ok' },
        presentation: { asset: { governs: true, asset_state: { code: 'long_wait', direction: 'long', title: 'LONG' },
          pd: setup, compact_rows: regions.map(z => ({ kind: 'entry', k: 'Вход', v: z.type + ': ' + fmtPrice(z.admitted[0]), note: 'в discount' })) } } };
      renderDeskScenario();
      renderDeskEntries();
      applyH1LegFrame(state.h1Layers);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const frame = state.chart.timeScale().getVisibleLogicalRange();
      const panel = document.getElementById('desk-entries');
      const body = document.getElementById('desk-scenario');
      const rows = panel.querySelectorAll('[data-analysis-zone]').length;
      const panelText = panel.textContent;
      const originalRegions = setup.eligible_regions;
      setup.eligible_regions = Array.from({ length: 12 }, (_, i) => ({ ...regions[0],
        lower: 81800 + i, upper: 81900 + i, admitted: [81800 + i, 81900 + i] }));
      renderDeskEntries();
      const cappedRows = panel.querySelectorAll('[data-analysis-zone]').length;
      setup.eligible_regions = originalRegions;
      renderDeskEntries();
      const dataBeforeBasis = !!(body.querySelector('#desk-data-details').compareDocumentPosition(body.querySelector('.desk-basis')) & Node.DOCUMENT_POSITION_FOLLOWING);
      const manualFrame = { from: 1450, to: 1530 };
      state.h1UserPanned = true;
      state.chart.timeScale().setVisibleLogicalRange(manualFrame);
      applyH1LegFrame(state.h1Layers);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      const held = state.chart.timeScale().getVisibleLogicalRange();
      setChartMode('context');
      setChartMode('h1');
      applyH1LegFrame(state.h1Layers);
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      return { initialFrame, frame, held, restored: state.chart.timeScale().getVisibleLogicalRange(),
        rows, cappedRows, panelText,
        dataBeforeBasis, hasWatchInHeader: !!document.getElementById('asset-active-btn'),
        hasCandleLabel: !!document.getElementById('candle-tf-label'),
        candleControlHidden: document.getElementById('tf-select').closest('.tf-switch').classList.contains('hidden'),
        hasSetupLink: !!document.querySelector('.lf-analysis-actions a[href="/alt.html"]') };
    });
    assert.equal(result.rows, 4);
    assert.equal(result.cappedRows, 8);
    assert.ok(!result.panelText.includes('535') && !result.panelText.includes('BOS/SMS'));
    assert.ok(result.initialFrame.from >= 1400, 'Current frame applies before structure finishes loading');
    assert.ok(result.frame.from >= 1400 && result.frame.to > 1499);
    assert.deepEqual(result.held, { from: 1450, to: 1530 });
    assert.deepEqual(result.restored, result.frame);
    assert.ok(result.dataBeforeBasis);
    assert.equal(result.hasWatchInHeader, false);
    assert.equal(result.hasCandleLabel, false);
    assert.ok(result.candleControlHidden);
    assert.equal(result.hasSetupLink, false);
    await page.screenshot({ path: 'tmp/h1-current-workspace-desktop.png' });
    await page.click('#desk-entries [data-analysis-zone]');
    await page.waitForSelector('.lf-drawer');
    assert.ok(await page.evaluate(() => String(state.h1SelectedZoneId).startsWith('scan:')));
    assert.ok(!(await page.$eval('.lf-drawer', el => el.textContent)).includes('BOS/SMS'));
    await page.keyboard.press('Escape');
    await page.waitForSelector('.lf-drawer', { hidden: true });
    await page.setViewport({ width: 390, height: 844 });
    await page.screenshot({ path: 'tmp/h1-current-workspace-mobile.png' });
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert.deepEqual(errors, []);
    console.log('Current H1 workspace: passed (535 archived zones -> 4 current entries)');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
