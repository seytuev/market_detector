// Run against tests.mobile_alt_browser_server:app on port 8878.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const puppeteer = require('../tools/node_modules/puppeteer-core');

(async () => {
  fs.mkdirSync('tmp/mobile-alt', {recursive: true});
  const browser = await puppeteer.launch({executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  try {
    for (const width of [320, 390, 430, 1440]) {
      await page.setViewport({width, height: 844});
      await page.goto('http://127.0.0.1:8878/alt.html?token=dev-token', {waitUntil: 'networkidle2'});
      await page.waitForSelector('#alt-tbody tr[data-kind]');
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      if (width < 768) {
        assert.equal(await page.$eval('#alt-table', el => getComputedStyle(el).display), 'block');
        assert(await page.$eval('#alt-tbody td', el => !!el.dataset.label));
        await page.click('#alt-filters-toggle');
        assert(await page.$eval('#alt-filter-panel', el => {
          const r = el.getBoundingClientRect();
          return r.left >= 0 && r.right <= innerWidth && r.bottom <= innerHeight - 60;
        }));
        await page.click('#alt-filter-close');
      } else {
        assert.equal(await page.$eval('#alt-table', el => getComputedStyle(el).display), 'table');
      }
      await page.screenshot({path: `tmp/mobile-alt/list-${width}.png`});
      await page.click('#alt-tbody tr[data-kind="setup"]');
      await page.waitForFunction(() => document.getElementById('alt-page').dataset.screen === 'analysis');
      await page.waitForFunction(() => document.getElementById('alt-chart-title').textContent !== 'Выберите монету');
      await page.click('[data-tf="W1"]');
      await page.evaluate(() => {
        document.getElementById('alt-chart-note').textContent = 'Загружен хвост истории источника: более ранние свечи в этот фрагмент не вошли. Это не вся рыночная история пары. Последняя свеча W1 — незавершённая неделя: она изменится по мере закрытия D1. Часть истории не загружена.';
        document.getElementById('alt-chart-notes').hidden = false;
      });
      const dimensions = await page.evaluate(() => {
        const plot = document.getElementById('chart-container').getBoundingClientRect();
        const notes = document.getElementById('alt-chart-notes');
        return {overflow: document.documentElement.scrollWidth > innerWidth, open: notes.open,
          noteHeight: notes.getBoundingClientRect().height, plotHeight: plot.height,
          toolbarInside: [...document.querySelectorAll('#alt-toolbar > :not(.hidden)')].filter(el => el.getClientRects().length)
            .every(el => { const r = el.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth; })};
      });
      assert.equal(dimensions.overflow, false);
      assert(dimensions.toolbarInside);
      assert.equal(dimensions.open, width >= 768);
      if (width < 768) {
        assert(dimensions.noteHeight <= 40 && dimensions.plotHeight >= 320);
        await page.click('#alt-chart-notes summary');
        assert(await page.$eval('#alt-chart-notes', el => el.open));
        await page.click('#alt-chart-notes summary');
      }
      await page.screenshot({path: `tmp/mobile-alt/chart-${width}.png`});
      await page.click('#alt-expand');
      if (width < 768) assert(await page.$eval('#chart-container', el => el.getBoundingClientRect().height > 600));
      await page.screenshot({path: `tmp/mobile-alt/expanded-${width}.png`});
      await page.click('#alt-expand');
      await page.click('#alt-back');
      assert.equal(await page.$eval('#alt-page', el => el.dataset.screen), 'list');
    }
    assert.deepEqual(errors, []);
    console.log('PASS: mobile screener cards, filters, W1 chart, notes, expand/back and desktop at 320/390/430/1440px.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
