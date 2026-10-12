/* Скриншоты двух рамок LINK и сверка экрана с PNG / PNG×2.
   Проекция — app/web/static/alt_chart.js (collectBoxes, boxRectPx, buildScene).
   Запуск из корня: node tools/link_structural_shots.js
   Живой скринер и порт 8000 не используются. */
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const ROOT = path.resolve(__dirname, '..');
const OUT = path.join(ROOT, 'docs', 'screener_structural_link');
const DAY = 86400000;

function utc(day) {
  return Date.parse(day + 'T00:00:00.000Z');
}

function fail(message) {
  throw new Error(message);
}

function episodeFrom(spec, id) {
  return {
    id,
    range_id: id,
    lower: spec.lower,
    upper: spec.upper,
    mid: (spec.lower + spec.upper) / 2,
    width: spec.upper - spec.lower,
    base_start_open_time: utc(spec.anchor_start),
    base_end_open_time: utc(spec.base_end),
    rules_version: 'alt-0.3',
    geometry_source: 'episode',
  };
}

const markup = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'tests', 'fixtures', 'alt_v2', 'link_structural.json'),
  'utf8',
));
const candles = JSON.parse(fs.readFileSync(
  path.join(ROOT, 'tests', 'fixtures', 'alt_v2', 'ohlc', 'LINK.json'),
  'utf8',
));
const base2022 = episodeFrom(markup.episodes[0], 161);
const base2026 = episodeFrom(markup.episodes[1], 407);

const VIEWS = [
  {
    name: 'd1_2022',
    timeframe: 'D1',
    eventMode: 'history',
    fromMs: utc('2022-03-01'),
    toMs: utc('2023-12-01'),
    caption: 'LINK · D1 · история · база 30.04.2022–22.10.2023',
    expect: [base2022],
    shot: true,
    png: true,
  },
  {
    name: 'd1_2026',
    timeframe: 'D1',
    eventMode: 'history',
    fromMs: utc('2025-12-01'),
    toMs: utc('2026-10-11'),
    caption: 'LINK · D1 · история · база 31.01.2026–19.08.2026',
    expect: [base2026],
    shot: true,
    png: true,
  },
  {
    name: 'w1_2022',
    timeframe: 'W1',
    eventMode: 'history',
    fromMs: utc('2022-03-01'),
    toMs: utc('2023-12-01'),
    caption: 'LINK · W1 · история · те же границы, что на D1',
    expect: [base2022],
    shot: true,
    png: false,
  },
  {
    name: 'w1_2026',
    timeframe: 'W1',
    eventMode: 'history',
    fromMs: utc('2025-12-01'),
    toMs: utc('2026-10-11'),
    caption: 'LINK · W1 · история · те же границы, что на D1',
    expect: [base2026],
    shot: true,
    png: false,
  },
  {
    name: 'd1_both',
    timeframe: 'D1',
    eventMode: 'history',
    fromMs: utc('2022-01-01'),
    toMs: utc('2026-10-11'),
    caption: 'LINK · D1 · история · две отдельные базы',
    expect: [base2022, base2026],
    shot: true,
    png: false,
  },
  {
    name: 'd1_2022_setup',
    timeframe: 'D1',
    eventMode: 'setup',
    fromMs: utc('2022-03-01'),
    toMs: utc('2023-12-01'),
    caption: 'LINK · D1 · сетап · выбранная рамка #161',
    expect: [base2022],
    shot: true,
    png: false,
  },
];

const PAGE = `<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<style>
  html, body { margin: 0; background: #f6f3ec; color: #241f1a; font-family: "Segoe UI", sans-serif; }
  #cap { height: 36px; display: flex; align-items: center; padding: 0 12px; font-size: 14px; font-weight: 600; }
  #wrap { position: relative; width: 1280px; height: 680px; }
  #chart, #alt-overlay { position: absolute; inset: 0; }
  #alt-overlay { pointer-events: none; z-index: 4; }
  .alt-zone { position: absolute; pointer-events: none; }
  .alt-range-box {
    background: rgba(227, 179, 65, 0.22);
    border-top: 1px solid rgba(166, 118, 18, 0.95);
    border-bottom: 1px solid rgba(166, 118, 18, 0.95);
    border-left: 1px solid rgba(166, 118, 18, 0.55);
  }
</style>
</head>
<body>
<div id="cap"></div>
<div id="wrap"><div id="chart"></div><div id="alt-overlay"></div></div>
</body>
</html>`;

async function boot(page) {
  await page.setViewport({ width: 1280, height: 740, deviceScaleFactor: 1 });
  await page.setContent(PAGE, { waitUntil: 'domcontentloaded' });
  await page.addScriptTag({
    url: 'https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js',
  });
  await page.addScriptTag({ path: path.join(ROOT, 'app', 'web', 'static', 'alt_chart.js') });
  await page.evaluate((rows) => {
    if (!window.LightweightCharts || !window.AltChart) {
      throw new Error('график или AltChart не загрузились');
    }
    const chart = window.LightweightCharts.createChart(document.getElementById('chart'), {
      width: 1280,
      height: 680,
      layout: { background: { color: '#f6f3ec' }, textColor: '#3a342c' },
      grid: { vertLines: { color: '#e7e1d6' }, horzLines: { color: '#e7e1d6' } },
      rightPriceScale: { borderColor: '#d9d1c4' },
      timeScale: { borderColor: '#d9d1c4' },
    });
    const series = chart.addCandlestickSeries({
      upColor: '#1f7a4d',
      downColor: '#a33b3b',
      borderUpColor: '#1f7a4d',
      borderDownColor: '#a33b3b',
      wickUpColor: '#1f7a4d',
      wickDownColor: '#a33b3b',
    });
    window.__chart = chart;
    window.__series = series;
    window.__d1 = rows;
  }, candles);
}

async function render(page, spec) {
  const measured = await page.evaluate(async (spec) => {
    const AC = window.AltChart;
    const chart = window.__chart;
    const series = window.__series;
    const tf = spec.timeframe;
    const source = tf === 'W1' ? AC.aggregateW1(window.__d1) : window.__d1;
    series.setData(source.map((candle) => ({
      time: Math.floor(candle.open_time / 1000),
      open: candle.open,
      high: candle.high,
      low: candle.low,
      close: candle.close,
    })));
    chart.timeScale().setVisibleRange({
      from: Math.floor(spec.fromMs / 1000),
      to: Math.floor(spec.toMs / 1000),
    });
    let visible = null;
    for (let i = 0; i < 8; i += 1) {
      await new Promise((resolve) => requestAnimationFrame(resolve));
      visible = chart.timeScale().getVisibleRange();
      if (visible && series.coordinateToPrice(0) != null) break;
    }
    if (!visible) return { error: 'нет видимого диапазона' };
    const anchor = (ms) => (tf === 'W1' ? AC.weekStartMs(ms) : ms);
    const boxEnd = (ms) => (tf === 'W1' ? AC.weekStartMs(ms) + 7 * 86400000 : ms);
    const selected = spec.episodes[0];
    const detail = {
      effective_range: selected,
      range_episodes: spec.episodes,
      candles: window.__d1,
      as_of_ms: window.__d1[window.__d1.length - 1].open_time + 86400000,
    };
    const last = window.__d1[window.__d1.length - 1].open_time;
    const collected = AC.collectBoxes(detail, {
      range: true,
      manipulation: false,
      entries: false,
      shelf: false,
      eventMode: spec.eventMode,
    }, last);
    const maxPrice = series.coordinateToPrice(0);
    const minPrice = series.coordinateToPrice(680);
    const ctx = {
      viewFromMs: visible.from * 1000,
      viewToMs: visible.to * 1000,
      candles: source,
      mapTime: (ms) => chart.timeScale().timeToCoordinate(Math.floor(anchor(ms) / 1000)),
      mapPrice: (price) => series.priceToCoordinate(price),
      priceRange: (minPrice != null && maxPrice != null) ? { min: minPrice, max: maxPrice } : null,
      chartHeight: 680,
    };
    const overlay = document.getElementById('alt-overlay');
    overlay.innerHTML = '';
    const painted = [];
    collected.boxes.forEach((box) => {
      const startMs = anchor(box.startMs);
      const endMs = boxEnd(box.endMs);
      const rect = AC.boxRectPx(
        { startMs, endMs, upper: box.upper, lower: box.lower },
        ctx,
      );
      if (!rect) return;
      const node = document.createElement('div');
      node.className = 'alt-zone alt-range-box';
      node.dataset.lower = String(box.lower);
      node.dataset.upper = String(box.upper);
      node.style.left = rect.x + 'px';
      node.style.top = rect.y + 'px';
      node.style.width = rect.width + 'px';
      node.style.height = rect.height + 'px';
      overlay.appendChild(node);
      painted.push({
        lower: box.lower,
        upper: box.upper,
        kind: box.kind,
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
      });
    });
    const scene = AC.buildScene({
      candles: source,
      viewFromMs: ctx.viewFromMs,
      viewToMs: ctx.viewToMs,
      mapTime: ctx.mapTime,
      mapPrice: ctx.mapPrice,
      priceRange: ctx.priceRange,
      chartHeight: 680,
      paneWidth: Math.max(0, 1280 - chart.priceScale('right').width()),
      boxes: collected.boxes.map((box) => Object.assign({}, box, {
        startMs: anchor(box.startMs),
        endMs: boxEnd(box.endMs),
      })),
      lines: AC.collectLevels(detail, {
        range: true, targets: 'off', cancel: false,
      }, null).levels,
      markers: [],
      fmtPrice: (price) => String(price),
    });
    document.getElementById('cap').textContent = spec.caption;
    const shot = chart.takeScreenshot();
    function compose(zoom) {
      const ratio = shot.width / 1280;
      const k = ratio * zoom;
      const out = document.createElement('canvas');
      out.width = Math.round(shot.width * zoom);
      out.height = Math.round((shot.height + 36) * zoom);
      const g = out.getContext('2d');
      g.fillStyle = '#f6f3ec';
      g.fillRect(0, 0, out.width, out.height);
      g.fillStyle = '#241f1a';
      g.font = `${Math.round(14 * k)}px Segoe UI, sans-serif`;
      g.fillText(spec.caption, Math.round(12 * k), Math.round(24 * k));
      g.drawImage(shot, 0, Math.round(36 * k), shot.width * zoom, shot.height * zoom);
      g.save();
      g.translate(0, Math.round(36 * k));
      g.scale(k, k);
      scene.rects.forEach((rect) => {
        g.fillStyle = 'rgba(227, 179, 65, 0.22)';
        g.fillRect(rect.x, rect.y, rect.width, rect.height);
        g.strokeStyle = 'rgba(166, 118, 18, 0.95)';
        g.strokeRect(rect.x + 0.5, rect.y + 0.5, rect.width - 1, rect.height - 1);
      });
      g.restore();
      return out.toDataURL('image/png');
    }
    return {
      painted,
      scene: scene.rects.map((rect) => ({
        kind: rect.kind, x: rect.x, y: rect.y, width: rect.width, height: rect.height,
      })),
      levels: scene.lines.map((line) => line.label),
      png: spec.png ? compose(1) : null,
      png2: spec.png ? compose(2) : null,
    };
  }, Object.assign({}, spec, { episodes: [base2022, base2026] }));
  if (!measured || measured.error) fail(spec.name + ': ' + (measured && measured.error));
  return measured;
}

function closeEnough(a, b) {
  return Math.abs(a - b) <= 1.5;
}

function expectFrames(name, painted, expected) {
  if (painted.length !== expected.length) {
    fail(name + ': рамок ' + painted.length + ', ожидалось ' + expected.length + ' ' + JSON.stringify(painted));
  }
  expected.forEach((frame) => {
    const hit = painted.find((box) =>
      Math.abs(box.lower - frame.lower) < 1e-9 && Math.abs(box.upper - frame.upper) < 1e-9
    );
    if (!hit) fail(name + ': нет рамки ' + frame.lower + '–' + frame.upper);
    if (hit.width < 40 || hit.height < 24) {
      fail(name + ': рамка слишком мала ' + hit.width + '×' + hit.height);
    }
  });
}

function expectSceneMatches(name, painted, scene) {
  const ranges = scene.filter((rect) => rect.kind === 'range');
  if (ranges.length !== painted.length) {
    fail(name + ': PNG-сцена ' + ranges.length + ' против экрана ' + painted.length);
  }
  painted.forEach((box) => {
    const hit = ranges.find((rect) =>
      closeEnough(rect.x, box.x) && closeEnough(rect.y, box.y)
      && closeEnough(rect.width, box.width) && closeEnough(rect.height, box.height)
    );
    if (!hit) fail(name + ': пиксели экрана и PNG разошлись ' + JSON.stringify(box));
  });
}

function writeDataUrl(file, dataUrl) {
  const body = dataUrl.replace(/^data:image\/png;base64,/, '');
  fs.writeFileSync(file, Buffer.from(body, 'base64'));
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await puppeteer.launch({
    executablePath: 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: 'new',
    args: ['--disable-gpu', '--font-render-hinting=none'],
  });
  const page = await browser.newPage();
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await boot(page);
  const prices = {};
  for (const spec of VIEWS) {
    const measured = await render(page, spec);
    expectFrames(spec.name, measured.painted, spec.expect);
    expectSceneMatches(spec.name, measured.painted, measured.scene);
    prices[spec.name] = measured.painted.map((box) => [box.lower, box.upper]);
    if (spec.shot) {
      await page.screenshot({
        path: path.join(OUT, spec.name + '_screen.png'),
        clip: { x: 0, y: 0, width: 1280, height: 716 },
      });
    }
    if (measured.png) writeDataUrl(path.join(OUT, spec.name + '_png.png'), measured.png);
    if (measured.png2) writeDataUrl(path.join(OUT, spec.name + '_png2.png'), measured.png2);
    console.log('ok', spec.name, JSON.stringify(prices[spec.name]));
  }
  if (JSON.stringify(prices.d1_2022) !== JSON.stringify(prices.w1_2022)) {
    fail('W1 сдвинул границы базы 2022');
  }
  if (JSON.stringify(prices.d1_2026) !== JSON.stringify(prices.w1_2026)) {
    fail('W1 сдвинул границы базы 2026');
  }
  if (JSON.stringify(prices.d1_2022_setup) !== JSON.stringify(prices.d1_2022)) {
    fail('сетап показал не выбранную рамку 2022');
  }
  const both = prices.d1_both.map((pair) => pair.join('-')).sort();
  const wanted = [base2022, base2026].map((frame) => frame.lower + '-' + frame.upper).sort();
  if (JSON.stringify(both) !== JSON.stringify(wanted)) fail('общий вид смешал рамки: ' + both);
  if (errors.length) fail(errors.join('; '));
  await browser.close();
  console.log('shots', OUT);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
