// Exercise the real chart with browser timezones different from Moscow.
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
    await page.setRequestInterception(true);
    page.on('request', request => request.respond({
      status: 200, contentType: 'text/html', body: '<!doctype html><div id="chart"></div>',
    }));
    await page.goto('http://chart.test');
    await page.addScriptTag({ path: path.resolve('app/web/static/vendor/lightweight-charts.js') });
    await page.addScriptTag({ path: path.resolve('app/web/static/common.js') });
    for (const timezone of ['UTC', 'America/New_York', 'Asia/Tokyo']) {
      await page.emulateTimezone(timezone);
      const result = await page.evaluate(async () => {
        const options = HTF.chartTimeOptions({ rightOffset: 10 });
        const tick = options.timeScale.tickMarkFormatter;
        const format = options.localization.timeFormatter;
        const time = Date.UTC(2026, 11, 31, 21, 15, 7) / 1000;
        const labels = [];
        const fillText = CanvasRenderingContext2D.prototype.fillText;
        CanvasRenderingContext2D.prototype.fillText = function (text, ...args) {
          labels.push(String(text));
          return fillText.call(this, text, ...args);
        };
        let chart;
        try {
          chart = LightweightCharts.createChart(document.getElementById('chart'), {
            width: 900, height: 450, ...options,
          });
          const series = chart.addCandlestickSeries();
          const start = Date.UTC(2026, 9, 15) / 1000;
          const bars = Array.from({ length: 24 }, (_, i) => ({
            time: start + i * 3600, open: 100, high: 102, low: 99, close: 101,
          }));
          series.setData(bars);
          chart.timeScale().fitContent();
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          chart.setCrosshairPosition(101, start, series);
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          return {
            ticks: [0, 1, 2, 3, 4].map(type => tick(time, type)),
            crosshair: format(time),
            businessDay: format({ year: 2026, month: 10, day: 15 }),
            dateString: format('2026-10-15'),
            firstTime: series.data()[0].time,
            start, labels,
          };
        } finally {
          if (chart) chart.remove();
          CanvasRenderingContext2D.prototype.fillText = fillText;
        }
      });
      assert.deepEqual(result.ticks, ['2027', 'янв.', '01.01', '00:15', '00:15:07'], timezone);
      assert.equal(result.crosshair, '01.01.2027, 00:15 МСК', timezone);
      assert.equal(result.businessDay, '15.10.2026, 03:00 МСК', timezone);
      assert.equal(result.dateString, result.businessDay, timezone);
      assert.equal(result.firstTime, result.start, 'Source candle timestamps must remain unchanged');
      assert.ok(result.labels.includes('09:00'), `${timezone}: Moscow tick must be drawn`);
      assert.ok(result.labels.includes('15.10.2026, 03:00 МСК'), `${timezone}: Moscow crosshair must be drawn`);
    }
    assert.deepEqual(errors, []);
    console.log('Chart Moscow timezone: axis and crosshair passed in UTC, New York and Tokyo');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
