// Real chart and DOM hit testing, without the user's database or market feeds.
const assert = require('node:assert/strict');
const path = require('node:path');
const puppeteer = require('../tools/node_modules/puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true,
  });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.setViewport({ width: 1100, height: 700 });
    await page.setRequestInterception(true);
    page.on('request', request => request.respond({ status: 200, contentType: 'text/html', body: '<!doctype html><body></body>' }));
    await page.goto('http://chart.test');
    await page.addStyleTag({ path: path.resolve('app/web/static/style.css') });
    await page.addScriptTag({ path: path.resolve('app/web/static/vendor/lightweight-charts.js') });
    await page.addScriptTag({ path: path.resolve('app/web/static/common.js') });
    await page.addScriptTag({ path: path.resolve('app/web/static/h1_layers.js') });
    for (const overlayId of ['zone-overlay', 'ltf-overlay', 'review-zone-overlay']) {
      await page.evaluate(async id => {
        document.body.innerHTML = `<div style="position:relative;margin:40px;width:950px;height:540px">
          <div id="chart" style="width:100%;height:100%"></div><div id="${id}"></div></div>`;
        const el = document.getElementById('chart');
        const overlay = document.getElementById(id);
        const chart = LightweightCharts.createChart(el, {
          width: 950, height: 540, crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
          timeScale: { timeVisible: true },
        });
        const series = chart.addCandlestickSeries();
        const start = 1700000000;
        series.setData(Array.from({ length: 60 }, (_, i) => ({
          time: start + i * 3600, open: 100, high: 104, low: 96, close: 101,
        })));
        chart.timeScale().setVisibleLogicalRange({ from: 0, to: 85 });
        const zones = [
          { id: 1, type: 'FVG', direction: 'bull', lower: 99, upper: 102,
            formed_at: (start + 10 * 3600) * 1000, lifecycle: 'active' },
          { id: 2, type: 'OB', direction: 'bear', lower: 98, upper: 100,
            formed_at: (start + 20 * 3600) * 1000, lifecycle: 'active' },
        ];
        window.fixture = { chart, series, moves: [], clicks: 0, redraws: 0 };
        const draw = () => {
          overlay.replaceChildren();
          H1Layers.draw(overlay, {
            layers: { detected_zones: zones }, settings: H1Layers.DEFAULTS,
            xOf: ms => chart.timeScale().timeToCoordinate(ms / 1000),
            yOf: price => series.priceToCoordinate(price),
            paneRight: chart.paneSize().width, height: chart.paneSize().height,
            fmtPrice: String, onZone: () => { fixture.clicks += 1; },
          });
          const band = document.createElement('div');
          band.className = id === 'ltf-overlay' ? 'ltf-entry' : 'zone-rect';
          Object.assign(band.style, { position: 'absolute', left: '30px', top: '400px', width: '700px', height: '45px' });
          band.onclick = () => { fixture.clicks += 1; };
          overlay.appendChild(band);
        };
        chart.subscribeCrosshairMove(param => {
          fixture.moves.push(param.point ? { x: param.point.x, y: param.point.y, time: param.time } : null);
          // The LTF page replaces overlays in its crosshair subscription.
          if (id === 'ltf-overlay') { fixture.redraws += 1; draw(); }
        });
        HTF.bindChartOverlayCrosshair(el, overlay);
        await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        draw();
      }, overlayId);
      await page.waitForFunction(() => fixture.chart.timeScale().timeToCoordinate(1700000000) !== null);
      const origin = await page.$eval('#chart', el => ({ x: el.getBoundingClientRect().left, y: el.getBoundingClientRect().top }));
      const move = async (x, y) => {
        await page.mouse.move(origin.x + x, origin.y + y);
        await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const point = await page.evaluate(() => fixture.moves.at(-1));
        assert.ok(point, `${overlayId}: crosshair must stay visible at ${x}, ${y}`);
        assert.ok(Math.abs(point.x - x) < 7 && Math.abs(point.y - y) < 2, JSON.stringify({ point, x, y }));
        return point;
      };
      // Unobstructed plot, zone body, overlapping zones, and the future margin.
      await move(80, 70);
      const zoneY = await page.evaluate(() => fixture.series.priceToCoordinate(100.5));
      assert.ok((await move(400, zoneY)).time, 'Time label must be present over candles');
      await move(410, zoneY + 12);
      await move(800, zoneY);
      await move(300, 420);
      await page.mouse.click(origin.x + 300, origin.y + 420);
      assert.equal(await page.evaluate(() => fixture.clicks), 1, 'Zone click must still work');
      const tag = await page.$eval('.h1-zone-tag', el => {
        const box = el.getBoundingClientRect();
        return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
      });
      await move(tag.x - origin.x, tag.y - origin.y);
      await page.mouse.click(tag.x, tag.y);
      assert.equal(await page.evaluate(() => fixture.clicks), 2, 'Zone tag click must still work');
      await move(800, zoneY);
      await page.mouse.move(origin.x + 930, origin.y + zoneY);
      assert.equal(await page.evaluate(() => fixture.moves.at(-1)), null, 'Crosshair must clear on the price axis');
      await move(80, 70);
      await page.mouse.move(1050, 650);
      assert.equal(await page.evaluate(() => fixture.moves.at(-1)), null, 'Crosshair must clear outside the chart');
      await page.evaluate(() => fixture.chart.remove());
      console.log(`${overlayId}: crosshair and zone clicks passed`);
    }
    assert.deepEqual(errors, []);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
