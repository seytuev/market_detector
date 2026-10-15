/* Full user workflows against the isolated fixture server, never the user DB. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path');
const puppeteer=require('../tools/node_modules/puppeteer-core');
const base=process.env.RESEARCH_TEST_URL||'http://127.0.0.1:8087';
(async()=>{
  const browser=await puppeteer.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe',headless:true});
  const out=path.resolve('tmp/research-workbench-ui');fs.mkdirSync(out,{recursive:true});const errors=[],results=[];
  try{
    const page=await browser.newPage();await page.setViewport({width:1440,height:1000});page.on('pageerror',e=>errors.push(e.message));
    await page.goto(base+'/favicon.svg');
    assert.equal(await page.evaluate(async()=> (await fetch('/api/auth/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:'research-test-token'})})).status),200);
    const assets=await page.evaluate(async()=> (await (await fetch('/api/research/v1/assets')).json()).data.items);const asset=assets[0].asset_uid;
    const fill=async(name,value)=>{await page.waitForSelector('dialog[open]:last-of-type [name="'+name+'"]');await page.$eval('dialog[open]:last-of-type [name="'+name+'"]',(e,v)=>{e.value=v;e.dispatchEvent(new Event('input',{bubbles:true}));},value);};
    const submit=async()=>{await page.click('dialog[open]:last-of-type [type=submit]');await page.waitForFunction(()=>!document.querySelector('dialog[open] [type=submit]'),{timeout:60000});await page.waitForSelector('#research-page [data-content]');};
    await page.goto(base+'/research.html?tab=ideas',{waitUntil:'networkidle0'});await page.click('[data-wb="new-idea"]');
    for(const [name,value] of Object.entries({title:'Проверяемая исследовательская идея',thesis:'Публичный запуск продукта может привести к росту использования сети.',mechanism:'Использование сети создаёт проверяемый спрос на её услуги.',invalidation:'Команда официально отменяет запуск продукта.',review_condition:'После следующего анонса команды',reason:'Сохранён тезис из первичного анонса'}))await fill(name,value);
    await page.select('dialog [name=asset]',asset);await page.select('dialog [name=status]','watching');await submit();
    await page.waitForFunction(()=>document.querySelector('[data-idea-list]')?.textContent.includes('Проверяемая исследовательская идея'));
    await page.click('[data-wb=idea]');await page.waitForSelector('dialog [data-wb=edit]');await page.click('dialog [data-wb=edit]');
    const idea=await page.evaluate(async()=> (await (await fetch('/api/research/v1/ideas')).json()).data.items[0]);
    await page.evaluate(async idea=>fetch('/api/research/v1/ideas/'+idea.id,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({expected_revision:idea.revision_id,author_note:'Параллельное изменение',reason:'Проверка конкурирующего сохранения'})}),idea);
    await fill('thesis','Несохранённый текст должен остаться в форме после конфликта версий.');await fill('reason','Проверяю сохранение собственного черновика');await page.click('dialog [type=submit]');
    await page.waitForFunction(()=>document.querySelector('dialog [data-error]')?.textContent.includes('изменён'));
    assert.ok((await page.$eval('dialog [name=thesis]',e=>e.value)).includes('Несохранённый'));await page.click('dialog [data-close]');
    await page.goto(base+'/research.html?tab=baskets',{waitUntil:'networkidle0'});await page.click('[data-wb=new-basket]');
    await fill('title','Исследовательская корзина проверки');await page.select('dialog [data-member] [name=asset]',asset);await fill('weight','50.00');await submit();
    await page.click('[data-wb=basket]');await page.waitForSelector('dialog [data-wb=start-basket]');await page.click('dialog [data-wb=start-basket]');await page.waitForFunction(()=>!document.querySelector('dialog[open]'));
    await page.goto(base+'/research.html?tab=playbook',{waitUntil:'networkidle0'});await page.click('[data-wb=new-section]');await fill('title','Публичные запуски');await fill('purpose','Проверяем подтверждённые анонсы реальных запусков.');await submit();
    const ref=await page.evaluate(async asset=>{const item=(await (await fetch('/api/research/v1/assets/'+asset+'/materials')).json()).data.items[0];return {kind:'material',id:item.id,revision_id:item.revision_id};},asset);
    await page.evaluate(ref=>ResearchWorkbench.exampleForm(ref),ref);await fill('explanation','Прямой первоисточник с проверяемым описанием запуска продукта.');await submit();
    await page.click('[data-wb=section]');await page.waitForSelector('dialog [data-wb=freeze]');await page.click('dialog [data-wb=freeze]');await page.waitForFunction(()=>document.querySelector('dialog [data-job]')?.textContent.includes('Зафиксировано'));
    await page.click('dialog [data-wb=manual-rule]');await fill('text','Ищем прямые анонсы команд с публичным запуском и проверяемым механизмом.');await fill('positive','Прямой автор\nПубличный запуск');await fill('negative','Пересказ без первичного анонса');
    await page.click('dialog[open]:last-of-type [type=submit]');await page.waitForFunction(()=>document.querySelectorAll('dialog[open]').length===1);await page.click('dialog [data-close]');
    await page.goto(base+'/research.html?tab=lab',{waitUntil:'networkidle0'});await page.click('[data-wb=strategy-new]');await fill('title','Формальная стратегия проверки');await submit();
    await page.click('[data-wb=experiment-new]');await fill('title','Реакция формальных правил на сохранённой истории');await fill('question','Как работают правила пробоя на фиксированном наборе закрытых свечей?');await page.select('dialog [name=mode]','simulation');
    const now=new Date(),from=new Date(Date.now()-5*86400000);await fill('from',from.toISOString().slice(0,10));await fill('to',now.toISOString().slice(0,10));await page.click('dialog [data-universe]');await submit();
    await page.click('[data-wb=experiment]');await page.waitForSelector('dialog [data-wb=prepare]');await page.click('dialog [data-wb=prepare]');await page.waitForSelector('[data-coverage] .research-table',{timeout:60000});
    await page.click('dialog [data-wb=start-run]');await page.click('dialog[open]:last-of-type [type=submit]');
    await page.waitForFunction(()=>[...document.querySelectorAll('dialog h2')].some(e=>e.textContent.includes('Результат исследования')),{timeout:60000});
    await page.goto(base+'/research.html?tab=ideas',{waitUntil:'networkidle0'});
    for(const theme of ['light','dark'])for(const width of [1440,900,390]){
      await page.setViewport({width,height:1000});await page.evaluate(theme=>localStorage.setItem('lf:theme',theme),theme);
      for(const tab of ['ideas','strength','baskets','playbook','lab']){
        await page.goto(base+'/research.html?tab='+tab,{waitUntil:'networkidle0'});await page.waitForSelector('#research-page .research-block');
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),true,'overflow '+tab+' '+theme+' '+width);
        assert.equal(await page.$eval('#research-page',e=>e.textContent.includes('Этот исследовательский сервис выключен')),false);
        await page.screenshot({path:path.join(out,tab+'-'+theme+'-'+width+'.png'),fullPage:true});results.push({tab,theme,width});
      }
    }
    assert.deepEqual(errors,[]);fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({workflows:['idea','conflict-preserves-draft','basket','playbook','lab-run'],views:results,errors},null,2));
    console.log(JSON.stringify({workflows:5,views:results.length,errors}));
  }catch(e){throw e;}finally{await browser.close();}
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
