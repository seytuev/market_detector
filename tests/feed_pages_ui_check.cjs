'use strict';
// Server: .venv/Scripts/python.exe -m uvicorn tests.information_flow_fixture_server:app --port 8894
const assert = require('node:assert/strict');
const fs = require('node:fs');
const puppeteer = require('../tools/node_modules/puppeteer-core');
const base = process.env.RESEARCH_TEST_URL || 'http://127.0.0.1:8894';

(async () => {
  const browser = await puppeteer.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  const errors = [], sourceWrites = [];
  fs.mkdirSync('tmp/feed-pages', {recursive:true});
  try {
    const page = await browser.newPage();
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => {
      if (/\/api\/research\/v1\/(channels|x-accounts)(\/|$)/.test(request.url()) && request.method() !== 'GET') sourceWrites.push(request.url());
    });
    await page.goto(base + '/favicon.svg');
    await page.evaluate(async () => fetch('/api/auth/session', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:'telegram-fixture'})}));
    for (const theme of (process.env.FEED_TEST_THEME ? [process.env.FEED_TEST_THEME] : ['light', 'dark'])) for (const width of (process.env.FEED_TEST_WIDTH ? [Number(process.env.FEED_TEST_WIDTH)] : [1440, 768, 390])) {
      await page.setViewport({width, height:1000});
      await page.evaluate(theme => localStorage.setItem('lf:theme', theme), theme);
      for (const path of ['/feed.html', '/telegram-feed.html']) {
        const telegram = path.includes('telegram');
        await page.goto(base + path, {waitUntil:'networkidle0'});
        await page.waitForSelector(telegram ? '[data-story]' : '[data-open]');
        assert.equal(await page.$('#flow-source-form'), null);
        assert.equal(await page.$('[data-source-toggle]'), null);
        assert.equal(await page.$eval('.feed-platform-tabs [aria-current=page]', el => new URL(el.href).pathname), path);
        assert.equal(await page.$eval('.feed-header-actions a', el => new URL(el.href).search + new URL(el.href).hash), '?section=imports#settings');
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, `${path} ${theme} ${width}: overflow`);
        await page.screenshot({path:`tmp/feed-pages/${telegram?'telegram':'x'}-${theme}-${width}.png`, fullPage:true});
        if (!telegram) {
          const ids = await page.$$eval('[data-open]', els => els.map(el => el.dataset.open));
          assert.equal(ids.length, new Set(ids).size, 'Each X publication appears once');
          await page.click('[data-feed-collection=starred]');
          // Reset stars so each viewport run exercises an empty collection and saving a new star.
          await page.evaluate(() => localStorage.removeItem('stv:feed:stars'));
          await page.reload({waitUntil:'networkidle0'});
          await page.click('[data-feed-collection=starred]');
          assert.match(await page.$eval('#feed-list', el => el.textContent), /Ничего не найдено/);
          await page.click('[data-feed-collection=all]');
          const id = await page.$eval('[data-star]', el => el.dataset.star);
          await page.click('[data-star]');
          await page.click('[data-feed-collection=starred]');
          assert.deepEqual(await page.$$eval('[data-open]', els => els.map(el => el.dataset.open)), [id]);
          await page.click('[data-feed-collection=ready]');
          assert.ok(await page.$$eval('[data-open]', els => els.length) > 0);
          await page.select('#feed-direction', 'bearish');
          assert.equal(await page.$$eval('.feed-post[data-bias=bullish]', els => els.length), 0);
          await page.select('#feed-direction', '');
          await page.click('[data-feed-collection=all]');
          await page.type('#feed-search', 'Solana');
          assert.equal(await page.$$eval('[data-open]', els => els.length), 1);
          await page.click('[data-open]');
          assert.match(await page.$eval('#feed-detail', el => el.textContent), /Solana/);
        } else {
          assert.equal(await page.$eval('.telegram-digest', el => el.open), false);
          await page.click('[data-story]');
          await page.waitForSelector('.feed-story-source');
          assert.ok(await page.$$eval('.feed-story-source', els => els.length) >= 2);
          assert.match(await page.$eval('#feed-detail', el => el.textContent), /Перевод на русский/);
          await page.select('#feed-view', 'posts');
          assert.equal(await page.$eval('#feed-list-title', el => el.textContent), 'Публикации Telegram');
          await page.waitForSelector('[data-open]');
          await page.click('[data-star]');
          await page.click('#feed-stars');
          assert.ok(await page.$$eval('[data-open]', els => els.length) > 0);
          await page.click('#feed-stars');
          await page.click('.telegram-digest > summary');
          await page.click('#telegram-digest');
          await page.waitForFunction(() => !document.querySelector('#telegram-digest').disabled);
          assert.doesNotMatch(await page.$eval('#telegram-action', el => el.textContent), /Ошибка|Не удалось/);
          await page.waitForSelector('[data-digest-story]');
          await page.click('[data-digest-story]');
        }
        if (width <= 850) {
          await page.waitForFunction(() => document.activeElement.id === 'feed-detail').catch(async error => {
            await page.screenshot({path:'tmp/feed-pages/failure.png',fullPage:true});
            console.error(path, theme, width, await page.evaluate(() => ({focus:document.activeElement.outerHTML.slice(0,300),status:document.querySelector('#feed-status').textContent,detail:document.querySelector('#feed-detail').textContent.slice(0,150)})));
            throw error;
          });
          assert.equal(await page.$eval('#feed-back', el => getComputedStyle(el).display), 'inline-flex');
          await page.click('#feed-back');
          assert.equal(await page.evaluate(() => document.activeElement.id), 'feed-search');
        }
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true, 'Opened detail has no overflow');
        await page.screenshot({path:`tmp/feed-pages/${telegram?'telegram':'x'}-detail-${theme}-${width}.png`, fullPage:true});
      }
      console.log(`Feed layout and interactions passed: ${theme}, ${width}px`);
    }
    assert.deepEqual(sourceWrites, [], 'Feed pages never mutate source accounts');
    assert.deepEqual(errors, []);
    console.log('Both feeds: navigation, filters, stars, unique posts, stories, digest and mobile detail passed.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error);process.exitCode = 1;});
