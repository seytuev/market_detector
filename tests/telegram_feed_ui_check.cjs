'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const puppeteer=require('../tools/node_modules/puppeteer-core');
(async()=>{
 const browser=await puppeteer.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
 const out='tmp/telegram-ui';fs.mkdirSync(out,{recursive:true});const errors=[];
 try{const page=await browser.newPage();page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8088/favicon.svg');
 await page.evaluate(async()=>fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:'telegram-fixture'})}));
 for(const theme of ['light','dark'])for(const width of [1440,390]){
  await page.setViewport({width,height:1100});await page.evaluate(t=>localStorage.setItem('lf:theme',t),theme);
  await page.goto('http://127.0.0.1:8088/feed.html',{waitUntil:'networkidle0'});
  await page.waitForFunction(()=>document.querySelector('#telegram-digest-view').textContent.includes('Bitcoin'));
  await page.waitForSelector('[data-open]');
  const analyzed=await page.$eval('#feed-list',el=>el.textContent.includes('Разобрано'));assert.ok(analyzed);
  await page.select('#feed-view','stories');await page.waitForSelector('[data-story]');await page.click('[data-story]');
  assert.match(await page.$eval('#feed-detail',el=>el.textContent),/Перевод на русский/);
  assert.ok(await page.$$eval('.feed-story-source',els=>els.length)>=2);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,theme+' '+width+' overflow');
  await page.screenshot({path:`${out}/stories-${theme}-${width}.png`,fullPage:true});
  await page.click('[data-digest-story]');assert.match(await page.$eval('#feed-detail',el=>el.textContent),/Все первоисточники|Версии первоисточников/);
  await page.select('#feed-view','posts');await page.select('#feed-kind','telegram');
  await page.type('#feed-search','тестовой');assert.ok(await page.$$eval('[data-open]',els=>els.length)>=2);
  await page.click('#telegram-digest');await page.waitForFunction(()=>!document.querySelector('#telegram-digest').disabled);
  assert.ok(!(await page.$eval('#telegram-action',el=>el.textContent)).includes('Ошибка'));
 }
 assert.deepEqual(errors,[]);console.log('Telegram browser checks passed: light/dark, 1440/390, translation, merged sources, digest, search, no overflow or JS errors.');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
