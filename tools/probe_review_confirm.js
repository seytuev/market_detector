/* Repro 2: страница «Проверка» — кнопка «Подтвердить» (decision=confirmed→correct)
   в карточке кандидата. Ловит POST /api/zones/{id}/review, статус и тело.
   Запуск из tools/: node probe_review_confirm.js — сервер на :8124. */
const puppeteer = require('puppeteer-core');

const BASE = 'http://127.0.0.1:8124/?token=dev-token#review';

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  page.on('console', (msg) => {
    if (['error', 'warning'].includes(msg.type())) {
      console.log('[console.' + msg.type() + ']', msg.text());
    }
  });
  page.on('pageerror', (err) => console.log('[pageerror]', err.message));
  page.on('response', async (res) => {
    const url = res.url();
    if (url.includes('/review') || res.status() >= 400) {
      let body = '';
      try { body = (await res.text()).slice(0, 800); } catch (e) {}
      console.log('[net]', res.request().method(), res.status(), url.replace('http://127.0.0.1:8124', ''), '\n   body:', body);
    }
  });

  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#candidates-list .candidate-card', { timeout: 20000 });
  const first = await page.$('#candidates-list .candidate-card');
  const zoneId = await first.evaluate((el) => el.dataset.zoneId);
  console.log('first candidate zone id:', zoneId);

  const btn = await first.$('[data-act="confirmed"]');
  console.log('confirm button found:', !!btn);
  await btn.click();
  await new Promise((r) => setTimeout(r, 4000));

  // записался ли review / сменился ли статус
  const apiCheck = await page.evaluate(async (id) => {
    const r = await fetch(`/api/zones/${id}`, { headers: { Authorization: 'Bearer dev-token' } });
    const d = await r.json();
    return { http: r.status, zone_status: d.zone && d.zone.status,
             reviews: d.reviews, assessments: d.assessments };
  }, zoneId);
  console.log('api check:', JSON.stringify(apiCheck, null, 1));

  const queueText = await page.$eval('#candidates-list', (el) => el.innerText.slice(0, 200));
  console.log('queue after:', JSON.stringify(queueText));

  await browser.close();
}

main().catch((e) => { console.error(e); process.exit(1); });
