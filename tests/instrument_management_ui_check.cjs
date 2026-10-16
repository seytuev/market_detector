'use strict';
// Server: python -m uvicorn tests.instrument_management_browser_server:app --port 8897
const assert = require('node:assert/strict');
const fs = require('node:fs');
const puppeteer = require('../tools/node_modules/puppeteer-core');
const base = process.env.ASSET_TEST_URL || 'http://127.0.0.1:8897';
const token = process.env.ASSET_TEST_TOKEN || 'asset-ui-test';
(async () => {
  const browser = await puppeteer.launch({executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true});
  try {
    const page = await browser.newPage(), errors = [];
    page.on('pageerror', e => errors.push(e.message));
    fs.mkdirSync('tmp/asset-settings', {recursive: true});
    for (const width of (process.env.ASSET_TEST_WIDTHS || '1440,390,320').split(',').map(Number)) {
      await page.setViewport({width, height: 960});
      await page.goto(base + '/?token=' + token + '&section=assets#settings', {waitUntil: 'networkidle2'});
      await page.waitForSelector('#settings-asset-add');
      const add = async symbol => {
        await page.focus('#settings-asset-add [name=symbol]');
        await page.keyboard.down('Control');
        await page.keyboard.press('A');
        await page.keyboard.up('Control');
        await page.keyboard.press('Backspace');
        await page.type('#settings-asset-add [name=symbol]', symbol);
        await page.click('#settings-asset-add [type=submit]');
        await page.waitForFunction(() => !document.querySelector('#settings-asset-add').dataset.busy);
      };
      await add('sol/usdt');
      await page.waitForSelector('[data-remove-asset]');
      const id = await page.$eval('[data-remove-asset]', el => el.dataset.removeAsset);
      assert.equal(await page.$$eval('.asset-switch', rows => rows.length), 1);
      await page.click('[data-active-id]');
      await page.waitForFunction(() => {const el = document.querySelector('[data-active-id]'); return !el.disabled && !el.checked;});
      await add('SOLUSDT');
      assert.equal(await page.$$eval('.asset-switch', rows => rows.length), 1);
      assert.equal(await page.$eval('[data-active-id]', el => el.checked), true);
      await page.screenshot({path: `tmp/asset-settings/settings-${width}.png`, fullPage: true});
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.click('[data-remove-asset]');
      await page.waitForFunction(() => document.querySelectorAll('.asset-switch').length === 0);
      assert.equal(await page.$eval('#instrument-select', el => el.options.length), 0);
      assert.equal(await page.evaluate(() => localStorage.getItem('htf:instrument')), null);
      await page.reload({waitUntil: 'networkidle2'});
      assert.equal(await page.$$eval('.asset-switch', rows => rows.length), 0);
      await add('SOL/USDT');
      assert.equal(await page.$eval('[data-remove-asset]', el => el.dataset.removeAsset), id);
      await add('UNKNOWNUSDT');
      assert.match(await page.$eval('#settings-assets-status', el => el.textContent), /не найдена/);
      assert.equal(await page.$eval('#settings-asset-add [type=submit]', el => el.disabled), false);
      await page.click('[data-remove-asset]');
      await page.waitForFunction(() => document.querySelectorAll('.asset-switch').length === 0);
      console.log('Pair add / toggle / duplicate / delete / reload / restore / invalid pair:', width);
    }
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
})().catch(e => {console.error(e); process.exitCode = 1;});
