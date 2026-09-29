// Живая диагностика зон входа на LTF-графике.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const path = require('path');

const BASE = process.argv[2] || 'http://127.0.0.1:8080/ltf.html?token=dev-token&obs=3';
const OUT = path.join(__dirname, '..', 'data', 'shot_ltf_entries.png');
const OUT2 = path.join(__dirname, '..', 'data', 'shot_ltf_entries_focus.png');

function dump(page) {
  return page.evaluate(() => {
    const overlay = document.querySelector('#ltf-overlay');
    const chart = document.querySelector('#chart');
    const kids = overlay ? [...overlay.children] : [];
    const entries = kids.filter((d) => d.className.includes('ltf-entry'));
    const rect = (el) => {
      if (!el) return null;
      const r = el.getBoundingClientRect();
      const cs = getComputedStyle(el);
      return {
        cls: el.className,
        text: (el.textContent || '').slice(0, 40),
        title: (el.title || '').slice(0, 80),
        x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
        top: el.style.top, left: el.style.left, width: el.style.width, height: el.style.height,
        bg: cs.backgroundColor, border: cs.borderTopColor, op: cs.opacity, z: cs.zIndex,
        disp: cs.display, vis: cs.visibility,
      };
    };
    const or = overlay ? overlay.getBoundingClientRect() : null;
    const cr = chart ? chart.getBoundingClientRect() : null;
    return {
      obsSelected: document.querySelector('.ltf-obs-item.selected')?.textContent?.slice(0, 120),
      entryRows: document.querySelectorAll('#ltf-entries-table tbody tr.entry-row').length,
      entryCountLabel: document.querySelector('#ltf-entries-count')?.textContent,
      overlayChildren: kids.length,
      overlayClasses: kids.map((d) => d.className),
      entryOverlay: entries.length,
      sampleEntries: entries.slice(0, 8).map(rect),
      overlayRect: or && { x: or.x, y: or.y, w: or.width, h: or.height },
      chartRect: cr && { x: cr.x, y: cr.y, w: cr.width, h: cr.height },
      empty: document.querySelector('#ltf-chart-empty')?.textContent,
      emptyHidden: document.querySelector('#ltf-chart-empty')?.classList.contains('hidden'),
      cardStage: document.querySelector('.scenario-stage strong')?.textContent,
      fvg: rect(entries.find((d) => d.className.includes('ltf-entry-fvg'))),
      selected: rect(entries.find((d) => d.className.includes('selected'))),
      parent: rect(kids.find((d) => d.className.includes('ltf-parent'))),
    };
  });
}

(async () => {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu', '--window-size=1700,950'],
    defaultViewport: { width: 1700, height: 950 },
  });
  const page = await browser.newPage();
  await page.setCacheEnabled(false);
  const errors = [];
  page.on('pageerror', (e) => errors.push('[pageerror] ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('[console.error] ' + m.text());
  });
  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#chart canvas', { timeout: 30000 });
  await page.waitForFunction(
    () => document.querySelector('#ltf-entries-count') &&
          document.querySelector('#ltf-entries-count').textContent !== '',
    { timeout: 30000 },
  );
  await new Promise((r) => setTimeout(r, 2500));

  const before = await dump(page);
  console.log('BEFORE', JSON.stringify(before, null, 2));
  await page.screenshot({ path: OUT, fullPage: true });

  const clicked = await page.evaluate(() => {
    const btn = document.querySelector('#ltf-entries-table [data-act="chart"]');
    if (!btn) return { ok: false, reason: 'no chart button' };
    const row = btn.closest('tr');
    btn.click();
    return { ok: true, row: row ? row.textContent.slice(0, 160) : '' };
  });
  console.log('CLICK', JSON.stringify(clicked));
  await new Promise((r) => setTimeout(r, 2000));
  const after = await dump(page);
  console.log('AFTER', JSON.stringify(after, null, 2));
  await page.screenshot({ path: OUT2, fullPage: true });

  console.log('ERRORS', errors.length ? errors : 'none');
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
