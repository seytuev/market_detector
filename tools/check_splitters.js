// Проверка: сплиттеры меняют ширину правой панели и высоту нижней (Обзор и LTF).
const puppeteer = require('puppeteer-core');

async function drag(page, handle, dx, dy) {
  const r = await handle.boundingBox();
  await page.mouse.move(r.x + r.width / 2, r.y + r.height / 2);
  await page.mouse.down();
  await page.mouse.move(r.x + r.width / 2 + dx, r.y + r.height / 2 + dy, { steps: 10 });
  await page.mouse.up();
  await new Promise((res) => setTimeout(res, 300));
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new', args: ['--disable-gpu', '--window-size=1700,950'],
    defaultViewport: { width: 1700, height: 950 },
  });
  const page = await browser.newPage();
  page.on('pageerror', (e) => console.log('[pageerror]', e.message));

  // --- Обзор: правая колонка (zone-rail) ---
  await page.goto('http://127.0.0.1:8080/?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('.overview-layout > .panel-splitter.v', { timeout: 15000 });
  const railW = () => page.evaluate(() => document.querySelector('#zone-rail').getBoundingClientRect().width);
  const railBefore = await railW();
  await drag(page, await page.$('.overview-layout > .panel-splitter.v'), -120, 0);
  const railAfter = await railW();
  const htfCols = await page.evaluate(() => document.querySelector('.overview-layout').style.gridTemplateColumns);
  console.log('Обзор: rail %s -> %s, inline="%s" => %s',
    railBefore.toFixed(0), railAfter.toFixed(0), htfCols,
    railAfter > railBefore + 80 && htfCols ? 'ОК' : 'FAIL');

  // --- LTF: инспектор справа и нижняя панель ---
  await page.goto('http://127.0.0.1:8080/ltf.html?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('.ltf-layout > .panel-splitter.v', { timeout: 15000 });
  await page.waitForSelector('.ltf-layout > .panel-splitter.h', { timeout: 15000 });

  const inspW = () => page.evaluate(() => document.querySelector('#ltf-inspector').getBoundingClientRect().width);
  const inspBefore = await inspW();
  await drag(page, await page.$('.ltf-layout > .panel-splitter.v'), -100, 0);
  const inspAfter = await inspW();
  const ltfCols = await page.evaluate(() => document.querySelector('.ltf-layout').style.gridTemplateColumns);
  console.log('LTF: inspector %s -> %s, inline="%s" => %s',
    inspBefore.toFixed(0), inspAfter.toFixed(0), ltfCols,
    inspAfter > inspBefore + 60 && ltfCols ? 'ОК' : 'FAIL');

  const botH = () => page.evaluate(() => document.querySelector('.ltf-bottom').getBoundingClientRect().height);
  const botBefore = await botH();
  await drag(page, await page.$('.ltf-layout > .panel-splitter.h'), 0, -80);
  const botAfter = await botH();
  const ltfRows = await page.evaluate(() => document.querySelector('.ltf-layout').style.gridTemplateRows);
  console.log('LTF: bottom %s -> %s, inline="%s" => %s',
    botBefore.toFixed(0), botAfter.toFixed(0), ltfRows,
    botAfter > botBefore + 40 && ltfRows ? 'ОК' : 'FAIL');

  // --- Сворачивание нижней панели: сплиттер скрывается, inline-сброс ---
  await page.click('#ltf-bottom-collapse');
  await new Promise((r) => setTimeout(r, 300));
  const collapsed = await page.evaluate(() => {
    const h = document.querySelector('.ltf-layout > .panel-splitter.h');
    return {
      rowsInline: document.querySelector('.ltf-layout').style.gridTemplateRows,
      bottomH: document.querySelector('.ltf-bottom').getBoundingClientRect().height,
      handleHidden: getComputedStyle(h).display === 'none',
    };
  });
  console.log('LTF collapse: bottomH=%s inline="%s" handleHidden=%s => %s',
    collapsed.bottomH.toFixed(0), collapsed.rowsInline, collapsed.handleHidden,
    collapsed.bottomH < 60 && collapsed.rowsInline === '' && collapsed.handleHidden ? 'ОК' : 'FAIL');

  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
