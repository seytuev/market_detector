/* Repro: страница «Проверка» — кнопка «Размечено верно».
   Открывает #review, выбирает первого кандидата из очереди, жмёт
   data-review="correct", ловит сетевые запросы и ошибки консоли.
   Запуск из tools/: node probe_review_correct.js — сервер на :8124. */
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
    if (url.includes('/api/')) {
      let body = '';
      try { body = (await res.text()).slice(0, 500); } catch (e) {}
      console.log('[net]', res.request().method(), res.status(), url.replace('http://127.0.0.1:8124', ''), '\n   body:', body);
    }
  });

  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#candidates-list .candidate-card', { timeout: 20000 });
  const count = await page.$$eval('#candidates-list .candidate-card', (els) => els.length);
  console.log('candidates in queue:', count);

  // выбрать первого кандидата — клик по карточке открывает инспектор
  await page.click('#candidates-list .candidate-card');
  await new Promise((r) => setTimeout(r, 1500));
  const hasCorrect = await page.$('#detail-content [data-review="correct"]');
  console.log('correct button present:', !!hasCorrect);
  if (!hasCorrect) {
    const html = await page.$eval('#detail-content', (el) => el.innerHTML.slice(0, 800));
    console.log('detail-content html:', html);
    await browser.close();
    return;
  }
  const zoneId = await page.evaluate(() => document.querySelector('#candidates-list .candidate-card')?.dataset.zoneId);
  console.log('zone id:', zoneId);

  await page.click('#detail-content [data-review="correct"]');
  await new Promise((r) => setTimeout(r, 3000));

  // что показал UI после клика
  const verdict = await page.$eval('#review-verdict', (el) => el.textContent).catch(() => null);
  console.log('verdict text:', JSON.stringify(verdict));
  const reviewsBlock = await page.evaluate(() => {
    const el = document.querySelector('#detail-content');
    return el ? el.innerText.slice(-600) : null;
  });
  console.log('detail tail:', JSON.stringify(reviewsBlock));

  // проверить через API, записался ли review
  const apiCheck = await page.evaluate(async (id) => {
    const r = await fetch(`/api/zones/${id}`, { headers: { Authorization: 'Bearer dev-token' } });
    const d = await r.json();
    return { status: r.status, reviews: d.reviews, zone_status: d.zone?.status };
  }, zoneId);
  console.log('api check:', JSON.stringify(apiCheck, null, 1));

  await browser.close();
}

main().catch((e) => { console.error(e); process.exit(1); });
