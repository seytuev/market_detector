'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const puppeteer=require('../tools/node_modules/puppeteer-core');
const base=process.env.RESEARCH_TEST_URL||'http://127.0.0.1:8087';
const token=process.env.RESEARCH_TEST_TOKEN||'research-test-token';
const out=path.resolve('tmp/research-ui');fs.mkdirSync(out,{recursive:true});
(async()=>{
  const browser=await puppeteer.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  const errors=[],results=[];
  try{
    const page=await browser.newPage();
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base+'/favicon.svg');
    assert.equal(await page.evaluate(async token=>(await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})})).status,token),200);
    for(const theme of ['light','dark']){
      for(const width of [1440,900,390]){
        await page.setViewport({width,height:1000});
        await page.evaluate(theme=>localStorage.setItem('lf:theme',theme),theme);
        await page.goto(base+'/events.html?mode=catalysts',{waitUntil:'networkidle0'});
        await page.waitForSelector('[data-catalyst-list] .research-card');
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'overflow '+theme+' '+width);
        await page.select('[data-view]','month');
        await page.waitForSelector('.research-calendar');
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'calendar overflow');
        await page.screenshot({path:path.join(out,theme+'-'+width+'.png'),fullPage:true});
        await page.click('[data-mode="calendar"]');
        await page.waitForFunction(()=>!document.querySelector('[data-catalyst-list]'));
        await page.click('[data-mode="catalysts"]');
        await page.waitForSelector('[data-catalyst-list]');
        results.push({theme,width,mode_switch:true,overflow:false});
      }
    }
    await page.setViewport({width:1000,height:1000});
    await page.goto(base+'/research.html?tab=calendar',{waitUntil:'networkidle0'});
    await page.waitForSelector('[data-create]');
    await page.click('[data-create]');
    await page.waitForSelector('dialog[open] form');
    await page.type('dialog input[name="title"]','Новое событие браузерной проверки');
    await page.select('dialog select[name="scope"]','global');
    await page.type('dialog textarea[name="summary"]','Сохраняем через настоящее API изолированного стенда.');
    await page.click('dialog button[type="submit"]');
    await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
    await page.select('[data-filter]','undated');
    await page.waitForFunction(()=>document.querySelector('[data-catalyst-list]').textContent.includes('Новое событие браузерной проверки'));
    await page.click('[data-catalyst-list] [data-catalyst]');
    await page.waitForSelector('dialog[open] [data-history]');
    await page.click('dialog [data-history]');
    await page.waitForSelector('dialog [data-history-list] .research-card');
    await page.click('dialog [data-close]');
    await page.select('[data-filter]','review');
    await page.waitForSelector('[data-review]');
    await page.click('[data-review]');
    await page.waitForSelector('dialog[open] form');
    await page.click('dialog [data-close]');
    await page.select('[data-filter]','all');
    await page.waitForSelector('[data-merge]');
    const checkboxes=await page.$$('[data-merge]');
    await checkboxes[0].click();await checkboxes[1].click();
    await page.click('[data-merge-events]');
    await page.waitForSelector('dialog[open] input[name="reason"]');
    await page.type('dialog input[name="reason"]','Один анонс по двум источникам');
    await page.click('dialog form button');
    await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
    await page.goto(base+'/research.html?tab=identity',{waitUntil:'networkidle0'});
    await page.waitForSelector('[data-link]');
    await page.click('[data-identity-review-id]');
    await page.waitForSelector('dialog[open] input[name="reason"]');
    await page.type('dialog input[name="reason"]','Проверен внешний идентификатор актива');
    await page.click('dialog form button');
    await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
    await page.waitForFunction(()=>!document.querySelector('[data-identity-review-id]'));
    await page.goto(base+'/research.html?tab=sources',{waitUntil:'networkidle0'});
    await page.waitForSelector('[data-subscribe]');
    assert.deepEqual(errors,[]);
    fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({results,create:true,history:true,candidates:true,merge:true,identity:true,sources:true,errors},null,2));
    console.log(JSON.stringify({viewports:results.length,create:true,history:true,candidates:true,merge:true,identity:true,sources:true,errors}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
