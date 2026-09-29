const puppeteer = require('puppeteer-core');

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu'],
  });
  const failures = [];
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  page.on('pageerror', (error) => failures.push(error.message));
  await page.goto('http://127.0.0.1:8091/ltf.html?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#chart canvas', { timeout: 15000 });
  await page.click('#ltf-tabs [data-tab="all"]');
  await page.waitForNetworkIdle();
  const observations = await page.$$('#ltf-obs-list .ltf-obs-item');
  if (!observations.length) failures.push('нет наблюдений в демо-данных');
  else {
    await observations[0].click();
    await page.waitForNetworkIdle();
    const stage = await page.$eval('.scenario-stage strong', (el) => el.textContent);
    const entries = await page.$$('#ltf-entries-table tbody tr.entry-row');
    console.log('stage', stage, 'entries', entries.length);
    if (!entries.length) failures.push('нет зон входа');
    else {
      await page.$eval('#ltf-entries-table [data-act="expand"]', (el) => {
        el.scrollIntoView({ block: 'center', inline: 'nearest' });
        el.click();
      });
      const detail = await page.$('#ltf-entries-table tr.entry-detail:not(.hidden)');
      if (!detail) failures.push('нет раскрытия строки');
    }
  }
  await page.screenshot({ path: '../data/redesign_ltf_data_1440.png' });
  await page.setViewport({ width: 1024, height: 768 });
  await page.reload({ waitUntil: 'networkidle2' });
  await page.click('#ltf-burger');
  await page.waitForSelector('#ltf-obs-panel.open');
  await page.$eval('#ltf-tabs [data-tab="all"]', (el) => el.click());
  await page.waitForSelector('#ltf-obs-list .ltf-obs-item', { timeout: 15000 });
  await page.$eval('#ltf-obs-list .ltf-obs-item', (el) => el.click());
  await page.waitForSelector('#ltf-inspector.open', { timeout: 5000 }).catch(() => failures.push('на 1024 инспектор не открылся диалогом'));
  await page.screenshot({ path: '../data/redesign_ltf_1024.png' });
  await page.setViewport({ width: 390, height: 844, isMobile: true, hasTouch: true });
  await page.reload({ waitUntil: 'networkidle2' });
  await page.screenshot({ path: '../data/redesign_ltf_390.png' });
  await browser.close();
  if (failures.length) {
    console.error(failures.join('\n'));
    process.exit(1);
  }
}

main().catch((error) => { console.error(error); process.exit(1); });
