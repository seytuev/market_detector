/* Repro 3: инструментированный клик «Подтвердить».
   Логирует все request/requestfailed, ловит unhandledrejection в странице,
   и вызывает reviewCandidate напрямую для получения ошибки. */
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
  await page.evaluateOnNewDocument(() => {
    window.addEventListener('unhandledrejection', (e) => {
      console.error('UNHANDLED_REJECTION: ' + (e.reason && (e.reason.stack || e.reason.message || e.reason)));
    });
  });
  page.on('console', (msg) => console.log('[console.' + msg.type() + ']', msg.text().slice(0, 500)));
  page.on('pageerror', (err) => console.log('[pageerror]', err.message));
  page.on('request', (req) => {
    if (req.url().includes('/api/zones/')) console.log('[req]', req.method(), req.url(), req.postData());
  });
  page.on('requestfailed', (req) => console.log('[reqfail]', req.method(), req.url(), req.failure() && req.failure().errorText));
  page.on('response', async (res) => {
    if (res.url().includes('/api/zones/') && res.request().method() === 'POST') {
      let body = '';
      try { body = (await res.text()).slice(0, 800); } catch (e) {}
      console.log('[resp]', res.status(), res.url(), '\n   body:', body);
    }
  });

  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#candidates-list .candidate-card [data-act="confirmed"]', { timeout: 20000 });

  // Прямой вызов обработчика — поймать ошибку синхронно
  const direct = await page.evaluate(async () => {
    const card = document.querySelector('#candidates-list .candidate-card');
    const id = Number(card.dataset.zoneId);
    try {
      const r = await fetch(`/api/zones/${id}/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: 'Bearer dev-token' },
        body: JSON.stringify({ decision: 'confirmed', text: '' }),
      });
      return { viaFetch: r.status, body: (await r.text()).slice(0, 800) };
    } catch (e) {
      return { viaFetchError: String(e) };
    }
  });
  console.log('direct fetch result:', JSON.stringify(direct, null, 1));

  await browser.close();
}

main().catch((e) => { console.error(e); process.exit(1); });
