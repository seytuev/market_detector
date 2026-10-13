/* Run only against tests.redesign_browser_server on port 8099. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const puppeteer = require('./node_modules/puppeteer-core');
const base = process.env.REDESIGN_TEST_URL || 'http://127.0.0.1:8099';
const output = path.resolve(__dirname, '../docs/audit/redesign-2026-10-13');
fs.mkdirSync(output, {recursive:true});
const pause = ms => new Promise(resolve => setTimeout(resolve,ms));
(async () => {
  const browser = await puppeteer.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true,args:['--disable-gpu']});
  const errors = [], measurements = [];
  try {
    for (const [width,height] of [[1440,900],[1366,768],[390,844]]) {
      const page = await browser.newPage();
      page.on('pageerror',e => errors.push(`${width}: ${e.message}`));
      page.on('response',r => { if (r.url().includes('/api/') && r.status() >= 500) errors.push(`${width}: ${r.status()} ${r.url()}`); });
      await page.setViewport({width,height,isMobile:width<600,hasTouch:width<600});
      for (const [name,route] of [['watch','/?token=dev-token#now'],['analysis','/?instrument=1#desk'],['review','/#review'],['journal','/#journal'],['screener','/alt.html'],['market','/events.html'],['calendar','/events.html?tab=calendar'],['help','/#help'],['sources','/?section=sources#settings']]) {
        await page.goto(base+route,{waitUntil:'networkidle2'});
        await pause(350);
        assert.equal(await page.$('input[autocomplete="current-password"]'),null,`${name}: unexpected authentication prompt`);
        const geometry = await page.evaluate(() => ({overflow:document.documentElement.scrollWidth > innerWidth+2,
          width:document.documentElement.scrollWidth, height:innerHeight,
          chart:document.querySelector('#chart-container')?.getBoundingClientRect().toJSON()}));
        measurements.push({name,width,...geometry});
        assert.equal(geometry.overflow,false,`${name} ${width}: horizontal overflow (${geometry.width})`);
        if (name === 'analysis') assert.ok(geometry.chart.width >= 260 && geometry.chart.height >= 220,JSON.stringify(geometry));
        if (name === 'review') assert.equal(await page.$eval('.review-chart-block',el => getComputedStyle(el).display),'none');
        if (name === 'screener') assert.equal(await page.$eval('#alt-work',el => getComputedStyle(el).display),'none');
        if (name === 'market') assert.equal(await page.evaluate(() => document.querySelector('#events-body').textContent.includes('legacy BTC')),false);
        if (name === 'calendar') {
          assert.ok(await page.$('.lf-calendar-grid'));
          assert.equal(await page.evaluate(() => document.querySelector('#events-body').textContent.includes('msk_day_boundary')),false);
          await page.click('[data-calendar-day]');
          assert.equal(await page.$$eval('[data-calendar-day][aria-pressed="true"]',els => els.length),1);
        }
        if (name === 'sources') assert.ok(await page.$eval('[data-settings-pane="sources"]',el => el.classList.contains('is-active')));
        await page.screenshot({path:path.join(output,`${name}-${width}.png`),fullPage:false});
      }
      // The same scenario stays distinct from data quality, even in a degraded fixture.
      await page.goto(base+'/?instrument=1#now',{waitUntil:'networkidle2'});
      await page.click('.asset-open');
      await page.waitForFunction(() => location.hash === '#desk');
      await page.goBack({waitUntil:'networkidle2'});
      assert.equal(await page.evaluate(() => location.hash),'#now');
      // Object-safe grouping and a focus-restoring detail panel.
      await page.goto(base+'/#journal',{waitUntil:'networkidle2'});
      if (await page.$('.journal-group')) await page.click('.journal-group > summary');
      await page.click('.journal-open');
      await page.waitForSelector('.lf-drawer');
      await page.keyboard.press('Escape');
      assert.equal(await page.$('.lf-drawer'),null);
      assert.ok(await page.evaluate(() => document.activeElement.classList.contains('journal-open')));
      // Screener -> analysis -> browser Back and explicit return preserve search.
      await page.goto(base+'/alt.html',{waitUntil:'networkidle2'});
      await page.$eval('#alt-search',el => { el.value=''; el.dispatchEvent(new Event('input',{bubbles:true})); });
      await page.type('#alt-search','AAA');
      await page.waitForFunction(() => document.querySelectorAll('#alt-tbody tr[data-kind]').length === 1);
      await page.click('#alt-tbody tr[data-kind]');
      await page.waitForFunction(() => document.querySelector('#alt-page').dataset.screen === 'analysis' && state.cardPhase === 'ready').catch(async err => {
        console.log('Setup failure',await page.evaluate(() => ({screen:state.screen,phase:state.cardPhase,error:state.cardError,url:location.href,rows:document.querySelector('#alt-tbody').textContent,body:document.querySelector('#alt-card-body').textContent})),errors); throw err;
      });
      await pause(250);
      await page.screenshot({path:path.join(output,`setup-${width}.png`)});
      await page.goBack({waitUntil:'networkidle2'});
      await page.waitForFunction(() => document.querySelector('#alt-page').dataset.screen === 'list');
      assert.equal(await page.$eval('#alt-search',el => el.value),'AAA');
      // Keyboard table opening is equivalent to pointer opening.
      await page.focus('#alt-tbody tr[data-kind]'); await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelector('#alt-page').dataset.screen === 'analysis');
      await page.click('#alt-back');
      assert.equal(await page.$eval('#alt-search',el => el.value),'AAA');
      await page.close();
    }
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(output,'browser-results.json'),JSON.stringify({errors,measurements},null,2));
    console.log(`PASS: 3 sizes, 9 pages, navigation, journal details, calendar, screener return. Screenshots: ${output}`);
  } finally { await browser.close(); }
})().catch(err => {console.error(err);process.exit(1);});
