// Run against tests.redesign_browser_server:app on port 8877.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const puppeteer = require('../tools/node_modules/puppeteer-core');

(async () => {
  fs.mkdirSync('tmp/mobile-analysis', {recursive: true});
  const browser = await puppeteer.launch({executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    for (const width of [320, 360, 390, 430]) {
      await page.setViewport({width, height: 844, isMobile: true, hasTouch: true});
      await page.goto('http://127.0.0.1:8877/?token=dev-token#now', {waitUntil: 'networkidle2'});
      await page.waitForSelector('#now-tbody .asset-analyze');
      await page.click('#now-tbody .asset-analyze');
      await page.waitForSelector('#view-overview.active');
      await page.click('#lnk-ltf');
      await page.waitForFunction(() => document.querySelector('.tf-switch').classList.contains('hidden'));
      await page.evaluate(() => {
        const messages = {
          'h1-transition': 'Восходящая структура подтверждена: HL → HH',
          'h1-layer-status': 'Зоны внутри PD + 5 ближайших выше и ниже · показано 68. PD H1 текущего движения · 50% 82 217,15',
          'h1-offscreen': 'Вне видимого диапазона: 3 зоны',
        };
        for (const [id, text] of Object.entries(messages)) {
          const el = document.getElementById(id);
          el.textContent = text;
          el.classList.remove('hidden');
        }
        ChartShare.showLink(document.getElementById('chart-share'), 'https://market.example/charts/very-long-chart-image-url.png');
      });
      const geometry = await page.evaluate(() => {
        const box = selector => document.querySelector(selector).getBoundingClientRect();
        const chart = box('#chart-container');
        const notes = box('.mobile-chart-notes');
        const share = box('.chart-share-result');
        const toolbar = ['#instrument-select', '.layer-menu', '#btn-new-zone', '#desk-expand'].map(box);
        return {
          overflow: document.documentElement.scrollWidth > innerWidth,
          notesBelow: notes.top >= chart.bottom,
          shareAbove: share.bottom <= chart.top,
          shareHeight: share.height,
          controlsAligned: toolbar.every(r => Math.abs(r.top - toolbar[0].top) < 2),
          controlsInside: toolbar.every(r => r.left >= 0 && r.right <= innerWidth),
          chartHeight: chart.height,
        };
      });
      assert.equal(geometry.overflow, false, JSON.stringify({width, geometry}));
      assert(geometry.notesBelow && geometry.shareAbove && geometry.controlsAligned && geometry.controlsInside, JSON.stringify({width, geometry}));
      assert(geometry.shareHeight < 120 && geometry.chartHeight >= 320);
      await page.screenshot({path: `tmp/mobile-analysis/${width}-share.png`, fullPage: true});
      await page.click('.chart-share-result button:last-child');
      assert.equal(await page.$('.chart-share-result'), null);
      await page.click('[data-analysis-pane="list"]');
      assert(await page.$eval('#desk-assets', el => el.getBoundingClientRect().height > 0));
      await page.click('[data-analysis-pane="chart"]');
      await page.click('#desk-expand');
      assert(await page.$eval('#chart-container', el => el.getBoundingClientRect().height >= 500));
      await page.click('#desk-expand');
      await page.click('#mode-context');
      await page.waitForFunction(() => !document.querySelector('.tf-switch').classList.contains('hidden'));
      assert(await page.$eval('#tf-select', el => el.getBoundingClientRect().right < innerWidth));
      await page.screenshot({path: `tmp/mobile-analysis/${width}-htf.png`, fullPage: true});
    }
    await page.setViewport({width: 1440, height: 1000});
    assert.equal(await page.$eval('#h1-layer-status', el => el.parentElement.id), 'chart-container');
    assert.equal(errors.length, 0, errors.join('\n'));
    console.log('PASS: 320/360/390/430px, share panel, chart notes, asset list, expand, HTF/LTF, desktop resize.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
