const puppeteer = require('puppeteer-core');

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu'],
  });
  const failures = [];
  for (const [width, height] of [[1920, 1080], [1440, 900], [1280, 800], [1024, 768], [390, 844]]) {
    for (const route of ['/', '/ltf.html']) {
      const page = await browser.newPage();
      await page.setViewport({ width, height, isMobile: width < 600, hasTouch: width < 600 });
      page.on('pageerror', (error) => failures.push(`${route} ${width}: ${error.message}`));
      await page.goto(`http://127.0.0.1:${process.env.HTF_PORT || 8090}${route}?token=dev-token`, { waitUntil: 'networkidle2', timeout: 60000 });
      await page.waitForSelector('#chart canvas', { timeout: 15000 }).catch(() => failures.push(`${route} ${width}: нет графика`));
      const geometry = await page.evaluate(() => {
        const chart = document.querySelector('#chart-container').getBoundingClientRect();
        const bottom = document.querySelector('.htf-bottom, .ltf-bottom').getBoundingClientRect();
        return { chartWidth: Math.round(chart.width), chartHeight: Math.round(chart.height), bottomWidth: Math.round(bottom.width), overflow: document.documentElement.scrollWidth > innerWidth + 2 };
      });
      if (geometry.chartWidth < 250 || geometry.chartHeight < 300 || geometry.overflow) failures.push(`${route} ${width}: ${JSON.stringify(geometry)}`);
      if (width === 1440 || width === 390) {
        await page.screenshot({ path: `../data/redesign_${route === '/' ? 'htf' : 'ltf'}_${width}.png` });
      }
      if (route === '/' && width === 1440) {
        await page.click('#zones-table tbody tr');
        await page.waitForSelector('#zone-detail:not(.hidden)');
        const detail = await page.evaluate(() => ({
          width: Math.round(document.querySelector('#zone-detail').getBoundingClientRect().width),
          chartWidth: Math.round(document.querySelector('#chart-container').getBoundingClientRect().width),
          selected: document.querySelectorAll('#zones-table tbody tr.selected').length,
        }));
        if (detail.width !== 320 || detail.chartWidth < 600 || detail.selected !== 1) failures.push(`HTF inspector: ${JSON.stringify(detail)}`);
        await page.screenshot({ path: '../data/redesign_htf_detail_1440.png' });
        await page.click('#detail-close');
        await page.click('#btn-new-zone');
        await page.waitForSelector('#zone-create-choice:not(.hidden)');
        await page.click('#choice-form');
        await page.waitForSelector('#manual-modal:not(.hidden)');
        await page.click('#mz-save');
        const validation = await page.$eval('#manual-status', (element) => element.textContent);
        if (!validation) failures.push('Ручная зона: нет локальной ошибки при пустых границах');
        await page.click('#mz-cancel');
      }
      if (route === '/ltf.html' && width === 1440) {
        await page.click('#ltf-tabs [data-tab="all"]');
        await page.waitForNetworkIdle();
        const observations = await page.$$('#ltf-obs-list .ltf-obs-item');
        if (observations.length) {
          await observations[0].click();
          await page.waitForNetworkIdle();
          const entries = await page.$$('#ltf-entries-table tbody tr.entry-row, #ltf-entries-table tbody tr');
          console.log('LTF data', observations.length, 'observations,', entries.length, 'entries');
          await page.screenshot({ path: '../data/redesign_ltf_data_1440.png' });
        }
      }
      console.log(route, width, JSON.stringify(geometry));
      await page.close();
    }
  }
  await browser.close();
  if (failures.length) {
    console.error(failures.join('\n'));
    process.exit(1);
  }
}

main().catch((error) => { console.error(error); process.exit(1); });
