const puppeteer = require('puppeteer-core');
(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new', args: ['--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  page.on('pageerror', (e) => console.log('PAGEERROR:', e.message));
  await page.goto('http://127.0.0.1:8000/?token=dev-token#review', { waitUntil: 'networkidle2', timeout: 60000 });
  await new Promise(r => setTimeout(r, 3000));

  // 1. Клик по карточке кандидата на вкладке Проверка
  const before = await page.evaluate(() => ({
    candidates: document.querySelectorAll('#candidates-list .candidate-card').length,
    detailHidden: document.querySelector('#zone-detail')?.classList.contains('hidden'),
    detailRect: JSON.stringify(document.querySelector('#zone-detail')?.getBoundingClientRect()),
    reviewInspectorText: (document.querySelector('#review-inspector-placeholder')?.innerText || '').slice(0, 80),
  }));
  console.log('REVIEW BEFORE:', JSON.stringify(before));

  await page.evaluate(() => {
    const card = document.querySelector('#candidates-list .candidate-card');
    card?.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
  await new Promise(r => setTimeout(r, 1500));
  const after = await page.evaluate(() => ({
    detailHidden: document.querySelector('#zone-detail')?.classList.contains('hidden'),
    detailContent: (document.querySelector('#detail-content')?.innerText || '').slice(0, 60),
    overviewActive: document.querySelector('#view-overview')?.classList.contains('active'),
  }));
  console.log('REVIEW AFTER CARD CLICK:', JSON.stringify(after));

  // 2. Вкладка События: клик по событию с зоной
  await page.evaluate(() => showView('events'));
  await new Promise(r => setTimeout(r, 1000));
  const evInfo = await page.evaluate(() => {
    const lis = [...document.querySelectorAll('#events-full li')];
    const withZone = lis.find(li => li.onclick);
    if (withZone) withZone.click();
    return { total: lis.length, clickable: lis.filter(li => li.onclick).length };
  });
  await new Promise(r => setTimeout(r, 1500));
  const evAfter = await page.evaluate(() => ({
    overviewActive: document.querySelector('#view-overview')?.classList.contains('active'),
    eventsActive: document.querySelector('#view-events')?.classList.contains('active'),
    detailHidden: document.querySelector('#zone-detail')?.classList.contains('hidden'),
    detailRect: JSON.stringify(document.querySelector('#zone-detail')?.getBoundingClientRect()),
  }));
  console.log('EVENTS CLICK:', JSON.stringify(evInfo), '=>', JSON.stringify(evAfter));
  await browser.close();
})().catch((e) => { console.error('FATAL:', e); process.exit(1); });
