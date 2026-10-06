/* UI-проверка окна LTF (ожидаемые уровни, журнал без активного сценария,
   топбар). Запуск из tools/: node check_ltf_ui.js — нужен сервер на :8123
   с копией data/htf_zones.db. Наблюдение 1 — bear D1 OB в waiting_structure. */
const puppeteer = require('puppeteer-core');

const BASE = 'http://127.0.0.1:8123/ltf.html?token=dev-token&obs=1';

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu'],
  });
  const failures = [];
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  page.on('pageerror', (error) => failures.push('pageerror: ' + error.message));
  await page.goto(BASE, { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#chart canvas', { timeout: 20000 });
  await page.waitForSelector('.scenario-stage strong', { timeout: 20000 });

  // 1) блок ожидаемых уровней на карточке наблюдения в ожидании
  const stageText = await page.$eval('.scenario-stage strong', (el) => el.textContent);
  console.log('stage:', stageText);
  const stageLines = await page.$$eval('.scenario-stage .stage-line', (els) => els.map((e) => e.textContent));
  console.log('stage lines:', JSON.stringify(stageLines, null, 1));
  if (!stageLines.some((l) => l.includes('Ожидаемый BOS: закрытие H1 строго ниже'))) {
    failures.push('нет строки «Ожидаемый BOS: закрытие H1 строго ниже …»');
  }
  if (!stageLines.some((l) => l.includes('опорный HL от'))) {
    failures.push('нет дат опорного HL в строке BOS');
  }
  if (!stageLines.some((l) => l.includes('Ожидаемый SMS'))) {
    failures.push('нет строки «Ожидаемый SMS»');
  }

  // ожидаемые уровни на графике — пунктирные линии
  const expectedLines = await page.$$eval('#ltf-overlay .ltf-expected', (els) => els.map((e) => ({
    cls: e.className, text: e.textContent, title: e.title,
  })));
  console.log('expected overlay lines:', JSON.stringify(expectedLines, null, 1));
  if (!expectedLines.some((l) => l.cls.includes('ltf-expected-bos'))) {
    failures.push('на графике нет линии «Ожидаемый BOS»');
  }

  // 2) журнал без активного сценария
  const journalCount = await page.$eval('#ltf-journal-count', (el) => el.textContent.trim());
  const journalItems = await page.$$('#ltf-journal li');
  console.log('journal count:', journalCount, 'items:', journalItems.length);
  // у наблюдения 1 может не быть событий вовсе — проверяем, что блок отрисован
  // и не показывает ошибку/заглушку «0» при наличии событий в API
  const apiJournal = await page.evaluate(async () => {
    const r = await fetch('/api/ltf/observations/1/journal', {
      headers: { Authorization: 'Bearer dev-token' },
    });
    return r.status === 200 ? await r.json() : null;
  });
  if (!apiJournal) failures.push('GET /api/ltf/observations/1/journal не 200');
  else if (String(apiJournal.events.length) !== journalCount) {
    failures.push(`журнал: API=${apiJournal.events.length}, UI=${journalCount}`);
  }

  // ожидаемый блок карточки: даты зоны подписаны
  const cardHtml = await page.$eval('#ltf-card', (el) => el.textContent);
  for (const label of ['Основание зоны', 'Подтверждение зоны', 'Активация LTF']) {
    if (!cardHtml.includes(label)) failures.push(`в карточке нет подписи «${label}»`);
  }

  // 3) топбар: последняя закрытая свеча + «Обновлено»
  const lastCandle = await page.$eval('#ltf-last-candle', (el) => el.textContent);
  const updated = await page.$eval('#ltf-updated', (el) => el.textContent);
  console.log('topbar:', lastCandle, '|', updated);
  if (!lastCandle.startsWith('H1: ') || lastCandle.includes('—')) failures.push('нет подписи последней закрытой H1');
  if (!updated.startsWith('Обновлено: ') || updated.includes('—')) failures.push('нет индикатора «Обновлено»');
  // сверяем с последней закрытой свечой API
  const apiCandles = await page.evaluate(async () => {
    const r = await fetch('/api/candles?instrument_id=1&timeframe=H1&limit=10', {
      headers: { Authorization: 'Bearer dev-token' },
    });
    return r.json();
  });
  const closed = apiCandles.filter((c) => c.closed);
  const fmt = (ms) => new Date(ms).toLocaleString('ru-RU', { hour12: false, timeZone: 'Europe/Moscow' }) + ' МСК';
  const want = 'H1: ' + fmt(closed[closed.length - 1].time * 1000);
  if (lastCandle !== want) failures.push(`H1 label: «${lastCandle}» ≠ последняя закрытая «${want}»`);

  // 4) вкладка «Активные»: закрытых наблюдений нет, отменённый сценарий
  //    не превращает наблюдение в «отменён»
  await page.click('#ltf-tabs [data-tab="active"]');
  await page.waitForNetworkIdle();
  const activeItems = await page.$$eval('#ltf-obs-list .ltf-obs-item', (els) => els.map((e) => e.textContent));
  if (activeItems.some((t) => t.includes('закрыто'))) failures.push('в «Активные» попали закрытые наблюдения');
  if (activeItems.some((t) => t.includes('отменён'))) failures.push('в «Активные» наблюдение в ожидании подписано «отменён»');
  console.log('active items:', activeItems.length);

  // 5) наблюдение 3: ожидание после отмены — заголовок, журнал, оба уровня
  await page.goto(BASE.replace('obs=1', 'obs=3'), { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('.scenario-stage strong', { timeout: 20000 });
  const stage3 = await page.$eval('.scenario-stage strong', (el) => el.textContent);
  const lines3 = await page.$$eval('.scenario-stage .stage-line', (els) => els.map((e) => e.textContent));
  console.log('obs3 stage:', stage3);
  console.log('obs3 lines:', JSON.stringify(lines3, null, 1));
  if (!stage3.includes('Ожидаем новый сценарий; предыдущий отменён (обратный BOS H1')) {
    failures.push('obs3: нет заголовка об отмене предыдущего сценария');
  }
  if (!lines3.some((l) => l.includes('Ожидаемый BOS: закрытие H1 строго выше'))) {
    failures.push('obs3: нет строки бычьего «Ожидаемый BOS»');
  }
  if (!lines3.some((l) => l.includes('Ожидаемый SMS: закрытие H1 строго выше') && l.includes('внутренний максимум от'))) {
    failures.push('obs3: нет строки «Ожидаемый SMS» с внутренним максимумом');
  }
  const j3 = await page.$eval('#ltf-journal-count', (el) => el.textContent.trim());
  const j3items = await page.$$eval('#ltf-journal li', (els) => els.map((e) => e.textContent));
  console.log('obs3 journal:', j3, 'последнее:', j3items[0]);
  if (j3 === '0' || !j3items.some((t) => t.includes('отмена сценария'))) {
    failures.push('obs3: журнал без активного сценария не показывает историю отмены');
  }
  const exp3 = await page.$$eval('#ltf-overlay .ltf-expected', (els) => els.map((e) => e.className));
  if (!exp3.some((c) => c.includes('ltf-expected-bos')) || !exp3.some((c) => c.includes('ltf-expected-sms'))) {
    failures.push('obs3: на графике нет обеих линий ожидаемых уровней');
  }

  await page.screenshot({ path: '../data/ui_check_ltf_expected.png' });
  await browser.close();
  if (failures.length) {
    console.error('FAILURES:\n' + failures.join('\n'));
    process.exit(1);
  }
  console.log('OK: все проверки прошли');
}

main().catch((error) => { console.error(error); process.exit(1); });
