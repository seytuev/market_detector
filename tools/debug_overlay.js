// Диагностика отрисовки зон: DOM, геометрия, видимость, скриншот.
const puppeteer = require('puppeteer-core');

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu', '--window-size=1700,950'],
    defaultViewport: { width: 1700, height: 950 },
  });
  const page = await browser.newPage();
  page.on('console', (m) => console.log('[console]', m.type(), m.text()));
  page.on('pageerror', (e) => console.log('[pageerror]', e.message));
  await page.goto('http://127.0.0.1:8080/?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForFunction(() => document.querySelector('#zones-table tbody tr'), { timeout: 30000 });
  await new Promise((r) => setTimeout(r, 3000));

  const info = await page.evaluate(() => {
    const overlay = document.querySelector('#zone-overlay');
    const chart = document.querySelector('#chart');
    const or = overlay.getBoundingClientRect();
    const cr = chart.getBoundingClientRect();
    const rects = [...overlay.querySelectorAll('.zone-rect')].slice(0, 5).map((d) => {
      const r = d.getBoundingClientRect();
      const cs = getComputedStyle(d);
      return { cls: d.className, x: r.x, y: r.y, w: r.width, h: r.height, bg: cs.backgroundColor, z: cs.zIndex, disp: cs.display, op: cs.opacity };
    });
    return {
      overlayChildren: overlay.children.length,
      zoneRects: overlay.querySelectorAll('.zone-rect').length,
      overlayRect: { x: or.x, y: or.y, w: or.width, h: or.height },
      chartRect: { x: cr.x, y: cr.y, w: cr.width, h: cr.height },
      overlayComputed: (() => { const cs = getComputedStyle(overlay); return { z: cs.zIndex, disp: cs.display, pos: cs.position }; })(),
      sample: rects,
      lcVersion: window.LightweightCharts ? 'ok' : 'missing',
    };
  });
  console.log(JSON.stringify(info, null, 2));
  await page.screenshot({ path: 'C:/users/seytu/projects/htfdec/data/shot3.png' });
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
