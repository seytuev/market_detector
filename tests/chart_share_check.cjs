// Exercise the real chart screenshot and DOM overlay rasterization in Chrome.
const assert = require('node:assert/strict');
const path = require('node:path');
const puppeteer = require('../tools/node_modules/puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
  try {
    const page = await browser.newPage();
    await page.setRequestInterception(true);
    page.on('request', request => {
      if (request.url().startsWith('data:')) return request.continue();
      if (request.url().endsWith('/api/chart-images')) return request.respond({status: 200, contentType: 'application/json', body: JSON.stringify({url: 'https://chart.test/charts/abc.png'})});
      return request.respond({status: 200, contentType: 'text/html', body: '<!doctype html><body></body>'});
    });
    await page.goto('https://chart.test');
    await page.addScriptTag({path: path.resolve('app/web/static/vendor/lightweight-charts.js')});
    await page.addScriptTag({path: path.resolve('app/web/static/common.js')});
    await page.addScriptTag({path: path.resolve('app/web/static/chart_share.js')});
    await page.addStyleTag({path: path.resolve('app/web/static/chart_share.css')});
    for (const ratio of [1, 2, 3]) {
      await page.setViewport({width: 1100, height: 700, deviceScaleFactor: ratio});
      const results = await page.evaluate(async (ratio) => {
        const results = [];
        for (const id of ['zone-overlay', 'ltf-overlay']) {
          document.body.innerHTML = `<div style="display:flex;gap:10px"><button id="share">Ссылка на график</button></div><div style="position:relative;width:900px;height:450px"><div id="chart" style="width:900px;height:450px"></div><div id="${id}" style="position:absolute;inset:0;pointer-events:none"><div style="position:absolute;left:100px;top:100px;width:200px;height:70px;background:rgb(255,0,255);color:white">Зона HTF / LTF</div></div></div>`;
          const chart = LightweightCharts.createChart(document.getElementById('chart'), {width:900, height:450});
          const series = chart.addCandlestickSeries();
          series.setData(Array.from({length:20}, (_, i) => ({time:1700000000+i*3600, open:100, high:110, low:90, close:105})));
          chart.timeScale().fitContent();
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          const blob = await ChartShare.capture(chart, document.getElementById(id), 'SOLUSDT · ' + id);
          const bitmap = await createImageBitmap(blob);
          const canvas = document.createElement('canvas');
          canvas.width = bitmap.width; canvas.height = bitmap.height;
          const ctx = canvas.getContext('2d'); ctx.drawImage(bitmap, 0, 0);
          // Both edges must align in CSS coordinates at every pixel density.
          const pixelAt = (x, y) => Array.from(ctx.getImageData(Math.round(x * ratio), Math.round((y + 48) * ratio), 1, 1).data);
          const pixel = pixelAt(150, 125);
          const farPixel = pixelAt(290, 160);
          const outsidePixel = pixelAt(310, 160);
          results.push({type:blob.type, width:bitmap.width, height:bitmap.height, pixel, farPixel, outsidePixel});
          ChartShare.bind(document.getElementById('share'), () => ({chart, overlay:document.getElementById(id), title:'SOLUSDT', ready:true}));
          await document.getElementById('share').onclick();
          results.push({url:document.querySelector('.chart-share-result input')?.value, disabled:document.getElementById('share').disabled, error:document.querySelector('.chart-share-error')?.textContent});
          chart.remove();
        }
        return results;
      }, ratio);
      for (let i=0; i<results.length; i+=2) {
        assert.equal(results[i].type, 'image/png');
        assert.equal(results[i].width, 900 * ratio);
        assert.equal(results[i].height, 498 * ratio);
        assert.deepEqual(results[i].pixel, [255,0,255,255]);
        assert.deepEqual(results[i].farPixel, [255,0,255,255]);
        assert.notDeepEqual(results[i].outsidePixel, [255,0,255,255]);
        assert.equal(results[i+1].url, 'https://chart.test/charts/abc.png');
        assert.equal(results[i+1].disabled, false);
        assert.equal(results[i+1].error, undefined);
      }
    }
    console.log('HTF/LTF: PNG overlays align at 1×, 2× and 3×; link creation and result UI pass.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
