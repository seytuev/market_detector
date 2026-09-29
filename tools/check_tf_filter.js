// Проверка: на HTF-графике отображаются зоны только выбранного таймфрейма.
const puppeteer = require('puppeteer-core');

async function countByTf(page) {
  return page.evaluate(() => {
    const out = { D1: 0, W1: 0, other: 0 };
    document.querySelectorAll('#zone-overlay .zone-rect').forEach((el) => {
      const m = (el.title || '').match(/\b(D1|W1)\b/);
      out[m ? m[1] : 'other'] += 1;
    });
    return out;
  });
}

async function main() {
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu'],
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1440, height: 900 });
  const failures = [];
  page.on('pageerror', (e) => failures.push('pageerror: ' + e.message));

  await page.goto('http://127.0.0.1:8090/?token=dev-token', { waitUntil: 'networkidle2', timeout: 60000 });
  await page.waitForSelector('#chart canvas', { timeout: 15000 });
  await page.waitForFunction(() => document.querySelectorAll('#zone-overlay .zone-rect').length > 0, { timeout: 15000 })
    .catch(() => failures.push('нет зон на графике при загрузке'));

  // 1. По умолчанию выбран D1 — только зоны D1
  let c = await countByTf(page);
  console.log('default D1:', JSON.stringify(c));
  if (c.W1 !== 0 || c.other !== 0) failures.push(`при выбранном D1 видны другие ТФ: ${JSON.stringify(c)}`);

  // чекбоксы слоёв ТФ должны быть disabled без «Все ТФ»
  const disabled = await page.evaluate(() =>
    [document.querySelector('#layer-tf-d1').disabled, document.querySelector('#layer-tf-w1').disabled]);
  if (!disabled[0] || !disabled[1]) failures.push('чекбоксы слоёв не disabled по умолчанию');

  // 2. Переключаемся на W1 — только зоны W1
  await page.select('#tf-select', 'W1');
  await page.waitForFunction(() => document.querySelectorAll('#zone-overlay .zone-rect').length > 0, { timeout: 15000 })
    .catch(() => {});
  await new Promise((r) => setTimeout(r, 500));
  c = await countByTf(page);
  console.log('switched W1:', JSON.stringify(c));
  if (c.D1 !== 0 || c.other !== 0) failures.push(`при выбранном W1 видны другие ТФ: ${JSON.stringify(c)}`);

  // 3. «Все ТФ» — видны оба; чекбоксы активны и скрывают свой ТФ
  const setChecked = (sel, val) => page.evaluate((s, v) => {
    const el = document.querySelector(s);
    el.checked = v;
    el.dispatchEvent(new Event('change'));
  }, sel, val);
  await setChecked('#tf-all', true);
  await new Promise((r) => setTimeout(r, 300));
  c = await countByTf(page);
  console.log('all TF:', JSON.stringify(c));
  if (c.D1 === 0 && c.W1 === 0) failures.push('режим «Все ТФ» ничего не показывает');
  const disabled2 = await page.evaluate(() => document.querySelector('#layer-tf-d1').disabled);
  if (disabled2) failures.push('чекбоксы слоёв disabled в режиме «Все ТФ»');
  await setChecked('#layer-tf-d1', false); // снять D1
  await new Promise((r) => setTimeout(r, 300));
  c = await countByTf(page);
  console.log('all TF minus D1:', JSON.stringify(c));
  if (c.D1 !== 0) failures.push(`чекбокс D1 не скрывает зоны D1: ${JSON.stringify(c)}`);

  await browser.close();
  if (failures.length) {
    console.log('FAILURES:\n' + failures.join('\n'));
    process.exit(1);
  }
  console.log('OK: зоны на графике соответствуют выбранному таймфрейму');
}

main().catch((e) => { console.error(e); process.exit(1); });
