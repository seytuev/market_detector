/* Read-only smoke check against a locally running integrated application. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const puppeteer=require('../tools/node_modules/puppeteer-core');
const base=process.env.RESEARCH_TEST_URL||'http://127.0.0.1:8086';
(async()=>{
  assert.ok(process.env.RESEARCH_TEST_TOKEN,'Set the local application token in the environment');
  const browser=await puppeteer.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  const errors=[],out=path.resolve('tmp/research-ui');fs.mkdirSync(out,{recursive:true});
  try{
    const page=await browser.newPage();await page.setViewport({width:1440,height:1100});
    page.on('pageerror',error=>errors.push(error.message));
    await page.goto(base+'/favicon.svg');
    assert.equal(await page.evaluate(async token=>(await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})})).status,process.env.RESEARCH_TEST_TOKEN),200);
    await page.goto(base+'/?instrument=1#desk',{waitUntil:'domcontentloaded'});
    await page.waitForSelector('#research-asset-context [data-archive]',{timeout:60000});
    await page.click('#research-asset-context summary');
    await page.click('[data-archive]');
    await page.waitForSelector('dialog[open] [data-archive-list] [data-material]');
    await page.click('dialog [data-material]');
    await page.waitForSelector('dialog[open] blockquote');
    await page.screenshot({path:path.join(out,'live-material.png'),fullPage:true});
    await page.goto(base+'/events.html?mode=catalysts',{waitUntil:'networkidle0'});
    await page.waitForSelector('[data-catalyst-list] .research-card');
    await page.screenshot({path:path.join(out,'live-calendar.png'),fullPage:true});
    await page.goto(base+'/research.html?tab=sources',{waitUntil:'networkidle0'});
    await page.waitForSelector('[data-channel-form]');
    assert.ok(await page.$('[data-channel="easychartgo"]'));
    assert.deepEqual(errors,[]);
    console.log(JSON.stringify({live_context:true,archive:true,material:true,calendar:true,sources:true,errors}));
  }finally{await browser.close();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
