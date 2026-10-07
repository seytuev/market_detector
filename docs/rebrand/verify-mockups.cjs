const fs = require('fs');
const path = require('path');
const { pathToFileURL } = require('url');
const puppeteer = require('../../tools/node_modules/puppeteer-core');
let browser;

(async () => {
  browser = await puppeteer.launch({ executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe', headless: true, args: ['--disable-gpu'] });
  const errors = [];
  const results = [];
  const page = await browser.newPage();
  // The concept uses local markup; optional host icons/tooltips need no network in QA.
  await page.setRequestInterception(true);
  page.on('request', request => /^https?:/.test(request.url()) ? request.abort() : request.continue());
  page.on('pageerror', error => errors.push(error.message));
  await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);
  const source = pathToFileURL(path.join(__dirname, 'levelframe-preview.html')).href;
  for (const width of [1024, 768, 390, 320]) {
    await page.setViewport({ width: width + 32, height: 1200 });
    await page.goto(source, { waitUntil: 'load' });
    const iframe = await page.waitForSelector('iframe');
    const frame = await iframe.contentFrame();
    await frame.waitForSelector('#levelframe-concept');
    for (const screen of ['overview', 'desk', 'review', 'journal', 'settings']) {
      if (screen === 'desk') await frame.click('[data-open-desk]');
      else await frame.click(`[data-screen="${screen}"]`);
      await frame.waitForFunction(() => document.querySelector('#levelframe-concept').getBoundingClientRect().width > 0);
      const geometry = await frame.evaluate(() => {
        const root = document.querySelector('#levelframe-concept');
        const rect = root.getBoundingClientRect();
        const outliers = [...root.querySelectorAll('*')].filter(el => {
          const r = el.getBoundingClientRect();
          return r.width && r.height && (r.right > rect.right + 2 || r.left < rect.left - 2);
        }).map(el => el.className?.baseVal || el.className || el.tagName).slice(0, 12);
        return { width: Math.round(rect.width), height: Math.round(rect.height), overflow: document.documentElement.scrollWidth > innerWidth + 2, outliers };
      });
      results.push({ screen, width, ...geometry });
      if (geometry.overflow || geometry.outliers.length) errors.push(`Layout ${screen} ${width}: ${JSON.stringify(geometry)}`);
      if (width === 1024 || (width === 390 && screen === 'desk')) {
        const root = await frame.$('#levelframe-concept');
        await root.screenshot({ path: path.join(__dirname, `levelframe-${screen}-${width}.png`) });
      }
    }
  }
  await page.setViewport({ width: 1056, height: 1200 });
  await page.goto(source, { waitUntil: 'load' });
  const frame = await (await page.$('iframe')).contentFrame();
  await frame.waitForSelector('[data-market-list] button');
  await frame.click('[data-screen="overview"]');
  await frame.click('[data-select="ETH"]');
  if (!await frame.$eval('[data-overview-insight]', el => el.textContent.includes('Нужен выбор контекста'))) errors.push('Asset selection did not update insight');
  await frame.click('[data-open-desk]');
  if (!await frame.$eval('[data-asset-header]', el => el.textContent.includes('ETHUSDT'))) errors.push('Desk did not preserve asset');
  await frame.click('[data-desk-select="BTC"]');
  await frame.click('[data-watch]');
  if (!await frame.$eval('[data-watch]', el => el.textContent.includes('Наблюдать'))) errors.push('Watch toggle failed');
  await frame.click('[data-analysis="context"]');
  if (!await frame.$eval('[data-chart-frame]', el => el.textContent.includes('D1'))) errors.push('Analysis toggle failed');
  await frame.click('[data-layer-button]');
  if (!await frame.$eval('[data-layer-button]', el => el.getAttribute('aria-pressed') === 'false')) errors.push('Layer toggle failed');
  await frame.click('[data-screen="review"]');
  await frame.click('[data-decision="confirmed"]');
  if (!await frame.$eval('[data-review-workspace]', el => el.hidden)) errors.push('Review decision failed');
  await frame.click('[data-screen="journal"]');
  await frame.click('[data-log-filter="decision"]');
  if (!await frame.$eval('[data-log-list]', el => el.textContent.includes('Разметка подтверждена'))) errors.push('Decision missing in journal');
  await frame.click('[data-screen="review"]');
  await frame.click('[data-undo]');
  if (!await frame.$eval('[data-review-done]', el => el.hidden)) errors.push('Undo failed');
  await frame.click('[data-screen="settings"]');
  await frame.click('[data-theme="light"]');
  if (!await frame.$eval('#levelframe-concept', el => getComputedStyle(el).colorScheme === 'light')) errors.push('Theme toggle failed');
  await frame.click('[data-screen="overview"]');
  await (await frame.$('#levelframe-concept')).screenshot({ path: path.join(__dirname, 'levelframe-overview-light-1024.png') });
  const verification = { results, errors, status: errors.length ? 'failed' : 'passed' };
  fs.writeFileSync(path.join(__dirname, 'verification.json'), JSON.stringify(verification, null, 2));
  console.log(JSON.stringify(verification));
  await browser.close();
  process.exitCode = errors.length ? 1 : 0;
})().catch(async error => { console.error(error); if (browser) await browser.close(); process.exitCode = 1; });
