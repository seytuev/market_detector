'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const puppeteer=require('../tools/node_modules/puppeteer-core');
const base=process.env.RESEARCH_TEST_URL||'http://127.0.0.1:8894';
const token=process.env.RESEARCH_TEST_TOKEN||'telegram-fixture';
(async()=>{
  const browser=await puppeteer.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  try {
    const page=await browser.newPage(),errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    fs.mkdirSync('tmp/integration-settings',{recursive:true});
    for(const width of [1440,390]) {
      await page.setViewport({width,height:960});
      await page.goto(base+'/?token='+token+'&section=ai#settings',{waitUntil:'networkidle2'});
      await page.waitForSelector('[data-model]');
      await page.evaluate(()=>{
        const original=HTF.api;let failed=false;
        HTF.api=async(path,options)=>{
          if(path==='/api/research/v1/x-accounts'&&!failed){failed=true;throw new Error('');}
          return original(path,options);
        };
      });
      await page.click('[data-settings-pane-btn="imports"]');
      await page.waitForSelector('#integration-settings [role=alert]');
      assert.match(await page.$eval('#integration-settings [role=alert]',el=>el.textContent),/Не удалось загрузить настройки/);
      await page.click('#integration-settings > button');
      await page.waitForSelector('[data-add="x"]');
      async function checkCredential(name) {
        const selector=`[data-credential="${name}"]`,secret='ui-fixture-secret-'+name;
        await page.type(selector+' input',secret);
        await page.click(selector+' [type=submit]');
        await page.waitForFunction(selector=>document.querySelector(selector+' [data-credential-status]')?.textContent.includes('Сохранено.'),{},selector);
        assert.equal(await page.$eval(selector+' input',el=>el.value),'');
        assert.equal(await page.evaluate(secret=>document.body.innerHTML.includes(secret),secret),false);
        await page.click(selector+' [data-credential-remove]');
        await page.waitForFunction(selector=>document.querySelector(selector+' [data-credential-status]')?.textContent.includes('отключено'),{},selector);
        assert.equal(await page.$eval(selector+' .integration-state',el=>el.textContent),'Не указан');
      }
      await checkCredential('x');
      for(const [platform,value,handle] of [['telegram','https://t.me/ExampleChannel','examplechannel'],['x','https://x.com/ExampleUser','exampleuser']]) {
        await page.type(`[data-add="${platform}"] input`,value);
        await page.click(`[data-add="${platform}"] button`);
        await page.waitForSelector(`[data-platform="${platform}"][data-toggle="${handle}"]`);
        await page.click(`[data-platform="${platform}"][data-toggle="${handle}"]`);
        await page.waitForFunction((platform,handle)=>{
          const input=document.querySelector(`[data-platform="${platform}"][data-toggle="${handle}"]`);
          return input&&!input.checked&&!input.disabled;
        },{},platform,handle);
      }
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      assert.equal(await page.$eval('.settings-sticky',el=>getComputedStyle(el).display),'none');
      await page.screenshot({path:`tmp/integration-settings/accounts-${width}.png`,fullPage:true});
      for(const [platform,handle] of [['telegram','examplechannel'],['x','exampleuser']]) {
        await page.click(`[data-platform="${platform}"][data-remove="${handle}"]`);
        await page.waitForFunction((platform,handle)=>!document.querySelector(`[data-platform="${platform}"][data-remove="${handle}"]`),{},platform,handle);
      }
      await page.click('[data-settings-pane-btn="ai"]');
      await page.waitForSelector('[data-model]');
      for(const name of ['xai','openai','anthropic'])await checkCredential(name);
      await page.screenshot({path:`tmp/integration-settings/credentials-${width}.png`,fullPage:true});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      console.log('Settings lifecycle and layout passed:',width);
    }
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
