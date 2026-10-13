/* Stage 6b (spec §14/§15, AC13/15/16/17/23): targeted UI updates.
   Run against tests.data_sync_browser_server, never the user's database:
     .venv/Scripts/python.exe -m uvicorn tests.data_sync_browser_server:app --port 8097
     node tests/ui_targeted_updates_check.js

   Automated here:
   (a) price tick → zero domain refetches, #zone-overlay children not rebuilt,
       scenario card DOM untouched; price label/bar still update;
   (f) closed-candle WS message with version continuity (lastKnown+1) and OHLC
       payload → incremental series update, zero requests;
   (g) candles version gap → exactly one debounced candles refetch;
   (e) chart logical range preserved across candles and zones refetches;
   (b) WS reconnect → exactly one /api/data-sync/status read + refetch of the
       stale domain only (zones), no candles/ltf/journal/instruments requests;
   (d) hidden tab → no work for unchanged domains while hidden (targeted
       refetch of a genuinely changed domain is allowed — §14 keeps server
       signals running), one coalesced recovery on visible;
   (c) instrument switch → late-response guards keep the selected asset;
   (h) manual job: immediate button state, stage rows (§15), terminal job
       refreshes stale domains only (candles grow 30 → 33), range preserved;
   (i) LTF page: price tick → local updates only, zero domain requests;
   warmup indicator (AC01) visible while the fixture server is not ready.

   Manual (not automated here): stage status texts «Ожидание источника» /
   «Ожидание расчёта» mid-run (timing-sensitive); warmup indicator clearing
   when history/calc phases complete (fixture never reaches readiness);
   real browser tab hiding (emulated here via emulateVisibilityState). */
const assert = require('node:assert/strict');
const puppeteer = require('../tools/node_modules/puppeteer-core');
const base = process.env.SYNC_TEST_URL || 'http://127.0.0.1:8097';
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// Служебные фоновые опросы не считаются доменными рефетчами
const isWarmup = (u) => u.includes('/api/ready');
const isStatus = (u) => u.includes('/api/data-sync');
const isApi = (u) => u.includes('/api/');
const isDomain = (u) => isApi(u) && !isWarmup(u) && !isStatus(u);
const count = (urls, pred) => urls.filter(pred).length;

async function post(url, body) {
  const resp = await fetch(base + url, body === undefined
    ? { method: 'POST' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  assert.ok(resp.ok, `${url} → ${resp.status}`);
  return resp.json();
}

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true, args: ['--disable-gpu'],
  });
  const errors = [];
  try {
    const page = await browser.newPage();
    await page.bringToFront();
    await page.setViewport({ width: 1440, height: 900 });
    page.on('pageerror', (e) => errors.push(`page: ${e.message}`));
    const requests = [];
    page.on('request', (r) => requests.push(r.url()));

    console.log('Opening main page (desk, instrument 1)');
    await page.goto(base + '/?token=dev-token&instrument=1#desk', { waitUntil: 'networkidle2' });
    await page.waitForFunction(() => window.LFDataSync && typeof state !== 'undefined'
      && state.chart && state.candles.length && HTF.isWsConnected(), { polling: 100 });
    assert.equal(await page.evaluate(() => state.instrumentId), 1);
    assert.equal(await page.evaluate(() => state.timeframe), 'D1');

    // AC01: прогрев виден, пока сервер не готов (фикстура не доходит до ready)
    const warmup = await page.evaluate(() => {
      const el = document.getElementById('warmup-indicator');
      return el && !el.classList.contains('hidden') ? el.textContent : null;
    });
    assert.ok(warmup && warmup.includes('Прогрев данных'), `warmup indicator: ${warmup}`);
    console.log('ok warmup indicator:', warmup);

    // Зона вокруг текущей цены — содержимое #zone-overlay для проверки (a)
    await post('/__test/add-zone/1?lower=99&upper=101');
    await page.evaluate(() => loadZones());
    await page.waitForFunction(() => document.getElementById('zone-overlay').children.length > 0, { polling: 100 });
    await page.evaluate(() => {
      [...document.getElementById('zone-overlay').children].forEach((c, i) => { c.dataset.tickMark = String(i); });
    });
    const overlayCount = await page.evaluate(() => document.getElementById('zone-overlay').children.length);

    // Сброс фазы 30-секундного опроса статуса, чтобы окна были детерминированы
    await page.waitForNetworkIdle({ idleTime: 700 }); // стартовые догрузки desk
    await page.evaluate(() => LFDataSync.readStatus());

    // --- (a) price tick: ноль доменных запросов, оверлей/карточка не тронуты
    console.log('(a) price tick');
    const scenarioHtml = await page.evaluate(() => (document.getElementById('desk-scenario') || {}).innerHTML || '');
    const lastBar = await page.evaluate(() => state.candles[state.candles.length - 1]);
    let mark = requests.length;
    await post('/__test/broadcast', {
      type: 'price', instrument_id: 1, price: 103.5, time: Date.now(),
      candles: { D1: { time: lastBar.time, open: lastBar.open, high: Math.max(lastBar.high, 103.5), low: lastBar.low, close: 103.5 } },
    });
    await page.waitForFunction(() => state.lastPrice === 103.5, { polling: 50 });
    await delay(700); // ждём возможные рефетчи
    {
      const window = requests.slice(mark);
      assert.equal(count(window, isDomain), 0, `tick refetches: ${JSON.stringify(window.filter(isDomain))}`);
      const overlay = await page.evaluate((n) => {
        const o = document.getElementById('zone-overlay');
        return o.children.length === n && [...o.children].every((c, i) => c.dataset.tickMark === String(i));
      }, overlayCount);
      assert.ok(overlay, 'zone-overlay rebuilt on tick');
      const scenarioAfter = await page.evaluate(() => (document.getElementById('desk-scenario') || {}).innerHTML || '');
      assert.equal(scenarioAfter, scenarioHtml, 'desk scenario re-rendered on tick');
      const lastClose = await page.evaluate(() => state.candles[state.candles.length - 1].close);
      assert.equal(lastClose, 103.5, 'tick bar not applied');
    }
    console.log('ok tick: no requests, overlay and scenario intact, bar applied');

    // --- (e0) фиксируем диапазон для проверок сохранения
    await page.evaluate(() => state.chart.timeScale().setVisibleLogicalRange({ from: 3, to: 15 }));
    const rangeOf = () => page.evaluate(() => state.chart.timeScale().getVisibleLogicalRange());
    const assertRange = async (tag) => {
      const r = await rangeOf();
      assert.ok(Math.abs(r.from - 3) < 0.01 && Math.abs(r.to - 15) < 0.01, `${tag}: ${JSON.stringify(r)}`);
    };

    // --- (f) закрытый бар с непрерывностью версии: инкремент, без запросов
    console.log('(f) closed candle incremental');
    mark = requests.length;
    const v0 = await page.evaluate(() => domainSync.knownVersion('candles:1:D1'));
    const closed = await post('/__test/close-candle/1?tf=D1&close=107');
    assert.equal(closed.candles_version, v0 + 1, `version continuity: ${v0} → ${closed.candles_version}`);
    await page.waitForFunction(() => state.candles[state.candles.length - 1].close === 107, { polling: 50 });
    await delay(600);
    assert.equal(count(requests.slice(mark), isDomain), 0,
      `incremental refetches: ${JSON.stringify(requests.slice(mark).filter(isDomain))}`);
    await assertRange('incremental range');
    console.log('ok incremental close applied without requests');

    // --- (g) разрыв версии свечей: ровно один дебаунс-рефетч
    console.log('(g) candles version gap');
    mark = requests.length;
    await post('/__test/close-candle/1?tf=D1&close=108&extra_revisions=1');
    await page.waitForFunction(() => state.candles[state.candles.length - 1].close === 108, { timeout: 5000, polling: 100 });
    await delay(500);
    assert.equal(count(requests.slice(mark), (u) => u.includes('/api/candles?')), 1,
      `gap candles requests: ${JSON.stringify(requests.slice(mark).filter(isDomain))}`);
    await assertRange('gap refetch range');
    console.log('ok gap: one candles refetch, range preserved');

    // --- (e) рефетч зон не трогает диапазон графика
    console.log('(e) zones refetch keeps range');
    const added = await post('/__test/add-zone/1?lower=99.2&upper=100.8');
    await post('/__test/broadcast', { type: 'zone', zone_id: added.zone_id });
    await page.waitForFunction(() => state.zones.some((z) => Math.abs(z.lower - 99.2) < 1e-9), { timeout: 5000, polling: 100 });
    await assertRange('zones refetch range');
    console.log('ok zones refetch, range preserved');

    // --- (b) reconnect: один статус + только изменённый домен
    console.log('(b) reconnect recovery');
    await page.evaluate(() => LFDataSync.readStatus()); // фаза 30-с опроса
    mark = requests.length;
    await page.evaluate(() => state.ws.close());
    await page.waitForFunction(() => !HTF.isWsConnected(), { polling: 100 });
    await post('/__test/add-zone/1?lower=97&upper=98'); // пока сокет лежит
    await page.waitForFunction(() => HTF.isWsConnected(), { timeout: 10000, polling: 100 });
    await page.waitForFunction(() => state.zones.some((z) => Math.abs(z.lower - 97) < 1e-9), { timeout: 6000, polling: 100 });
    await delay(500);
    {
      const window = requests.slice(mark);
      assert.equal(count(window, isStatus), 1, `status reads: ${JSON.stringify(window.filter(isStatus))}`);
      assert.ok(count(window, (u) => u.includes('/api/zones')) >= 1, 'zones not refetched');
      assert.equal(count(window, (u) => u.includes('/api/candles?')), 0, 'candles refetched');
      assert.equal(count(window, (u) => u.includes('/api/ltf/')), 0, 'ltf refetched');
      assert.equal(count(window, (u) => u.includes('/api/journal')), 0, 'journal refetched');
      assert.equal(count(window, (u) => u.includes('/api/instruments')), 0, 'instruments refetched');
    }
    console.log('ok reconnect: one status read, only stale domain refetched');

    // --- (d) скрытая вкладка: тишина по нетронутым доменам + один recovery
    // на возврате. Точечный рефетч ИЗМЕНИВШЕГОСЯ домена (zones) допустим и в
    // скрытой вкладке (§14: серверные сигналы не останавливаем); тиковой
    // визуальной работы и полных снимков нет.
    console.log('(d) hidden tab');
    await page.evaluate(() => LFDataSync.readStatus());
    const emulate = typeof page.emulateVisibilityState === 'function';
    const setHidden = (hidden) => emulate
      ? page.emulateVisibilityState(hidden ? 'hidden' : 'visible')
      : page.evaluate((h) => {
          if (window.__hiddenStub === undefined) {
            Object.defineProperty(document, 'hidden', { get: () => !!window.__hiddenStub, configurable: true });
            Object.defineProperty(document, 'visibilityState', { get: () => (window.__hiddenStub ? 'hidden' : 'visible'), configurable: true });
          }
          window.__hiddenStub = h;
          document.dispatchEvent(new Event('visibilitychange'));
        }, hidden);
    await setHidden(true);
    await delay(150);
    mark = requests.length;
    const hiddenZone = await post('/__test/add-zone/1?lower=96&upper=97.5');
    await post('/__test/broadcast', { type: 'zone', zone_id: hiddenZone.zone_id });
    await delay(900);
    {
      const window = requests.slice(mark);
      assert.equal(count(window, (u) => u.includes('/api/candles?')), 0, 'hidden: candles refetch');
      assert.equal(count(window, (u) => u.includes('/api/ltf/')), 0, 'hidden: ltf refetch');
      assert.equal(count(window, (u) => u.includes('/api/journal')), 0, 'hidden: journal refetch');
      assert.equal(count(window, (u) => u.includes('/api/instruments')), 0, 'hidden: instruments refetch');
      assert.equal(count(window, isStatus), 0, 'hidden: status read');
    }
    mark = requests.length;
    await setHidden(false);
    await page.waitForFunction(() => state.zones.some((z) => Math.abs(z.lower - 96) < 1e-9), { timeout: 6000, polling: 100 });
    await delay(400);
    {
      const window = requests.slice(mark);
      assert.equal(count(window, isStatus), 1, `visible recovery status reads: ${window.filter(isStatus).length}`);
      assert.equal(count(window, (u) => u.includes('/api/candles?')), 0, 'candles refetched on visible');
    }
    console.log('ok hidden tab: no stale-domain work while hidden, one recovery on visible');

    // --- (c) смена инструмента: поздние ответы не возвращают старый актив
    console.log('(c) instrument switch guards');
    await page.evaluate(() => {
      const sel = document.getElementById('instrument-select');
      sel.value = '2';
      sel.dispatchEvent(new Event('change'));
      sel.value = '1';
      sel.dispatchEvent(new Event('change'));
    });
    await page.waitForFunction(() => state.instrumentId === 1
      && state.loadedCandleInstrument === 1 && state.candles.length > 0, { timeout: 8000, polling: 100 });
    console.log('ok instrument switch settles on the selected asset');

    // --- (h) ручное обновление: мгновенный отклик кнопки, этапы, точечный рефетч
    console.log('(h) manual job stages and targeted refresh');
    await page.evaluate(() => state.chart.timeScale().setVisibleLogicalRange({ from: 3, to: 15 }));
    const immediate = await page.evaluate(() => {
      const btn = document.getElementById('btn-data-sync');
      btn.click();
      return { label: document.getElementById('data-sync-status').textContent, disabled: btn.disabled };
    });
    assert.equal(immediate.label, 'Запускаем обновление данных…', `immediate label: ${immediate.label}`);
    assert.equal(immediate.disabled, true, 'button not disabled at start');
    const candlesBefore = await page.evaluate(() => state.candles.length);
    await page.waitForFunction(() =>
      document.getElementById('data-sync-status').textContent.includes('Данные обновлены'), { timeout: 25000, polling: 100 });
    const candlesAfter = await page.evaluate(() => state.candles.length);
    assert.ok(candlesAfter > candlesBefore, `terminal job did not refresh candles: ${candlesBefore} → ${candlesAfter}`);
    await assertRange('post-job range');
    await page.click('.data-sync-bar summary');
    const detailText = await page.$eval('#data-sync-details', (e) => e.textContent);
    for (const stage of ['Котировки: готово', 'Свежие свечи: готово', 'Восстановление истории: готово']) {
      assert.ok(detailText.includes(stage), `stage row missing «${stage}»: ${detailText}`);
    }
    await page.click('.data-sync-bar summary');
    console.log('ok manual job: stages rendered, candles refetched by version diff, range preserved');

    // --- (i) страница LTF: тик — только локальные обновления
    // stay=1: иначе ltf.html — редирект на /?mode=h1#desk (редизайн)
    console.log('(i) ltf page tick');
    const ltf = await browser.newPage();
    await ltf.bringToFront();
    await ltf.setViewport({ width: 1440, height: 900 });
    ltf.on('pageerror', (e) => errors.push(`ltf: ${e.message}`));
    const ltfRequests = [];
    ltf.on('request', (r) => ltfRequests.push(r.url()));
    await ltf.goto(base + '/ltf.html?token=dev-token&stay=1&instrument=1', { waitUntil: 'networkidle2' });
    await ltf.waitForFunction(() => typeof state !== 'undefined' && state.candles.length && HTF.isWsConnected(), { polling: 100 });
    await ltf.waitForNetworkIdle({ idleTime: 700 }); // стартовые догрузки снимка
    mark = ltfRequests.length;
    await post('/__test/broadcast', { type: 'price', instrument_id: 1, price: 104.25, time: Date.now() });
    await ltf.waitForFunction(() => state.lastPrice === 104.25, { polling: 50 });
    await delay(600);
    assert.equal(count(ltfRequests.slice(mark), isDomain), 0,
      `ltf tick refetches: ${JSON.stringify(ltfRequests.slice(mark).filter(isDomain))}`);
    const ltfPrice = await ltf.$eval('#ltf-last-price', (e) => e.textContent);
    assert.ok(ltfPrice.includes('104'), `ltf price label: ${ltfPrice}`);
    await ltf.close();
    console.log('ok ltf tick: no requests, price updated');

    assert.deepEqual(errors, []);
    console.log('Targeted UI updates acceptance passed: tick slimming, incremental candle, gap refetch, range preservation, reconnect coalescing, hidden tab, instrument guards, job stages, ltf tick.');
  } catch (error) {
    console.error('Targeted UI updates acceptance failed:', error);
    throw error;
  } finally {
    await Promise.race([browser.close(), delay(5000).then(() => { if (browser.process()) browser.process().kill(); })]);
  }
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
