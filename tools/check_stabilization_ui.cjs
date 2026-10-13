const puppeteer = require('puppeteer-core');
const fs = require('fs');
(async () => {
  const browser = await puppeteer.launch({executablePath:process.env.CHROME_BIN || '/usr/bin/google-chrome',headless:true,args:['--no-sandbox']});
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror',error => errors.push(String(error)));
  const base = 'http://127.0.0.1:8765';
  for (let attempt=0;attempt<30;attempt++) {
    try { const response = await fetch(base+'/api/live'); if(response.ok) break; } catch (_) {}
    await new Promise(resolve=>setTimeout(resolve,1000));
  }
  fs.mkdirSync('tmp/stabilization-ui',{recursive:true});
  await page.goto(base,{waitUntil:'networkidle0'});
  await page.waitForSelector('#auth-title');
  await page.type('input[type="password"]','browser-fixture-token');
  await page.click('.modal button[type="submit"]');
  await page.waitForFunction(()=>!document.getElementById('auth-title'));
  for (const [width,height] of [[1440,900],[1280,720],[390,844]]) {
    await page.setViewport({width,height});
    for (const view of ['now','desk','settings','journal']) {
      await page.goto(base+'/#'+view,{waitUntil:'networkidle0'});
      await page.screenshot({path:`tmp/stabilization-ui/${view}-${width}.png`,fullPage:true});
      const leaked=await page.evaluate(()=>[location.href,...[...document.querySelectorAll('a[href]')].map(a=>a.href)].some(url=>url.includes('token=')));
      if(leaked) throw Error('Credential found in a browser link');
    }
  }
  const cookies=await page.cookies();
  if(!cookies.some(c=>c.name==='lf_session' && c.httpOnly)) throw Error('HttpOnly session missing');
  fs.writeFileSync('tmp/stabilization-ui/errors.json',JSON.stringify(errors,null,2));
  await browser.close();
  if(errors.length) throw Error(errors.join('\n'));
})().catch(error=>{console.error(error);process.exit(1);});
