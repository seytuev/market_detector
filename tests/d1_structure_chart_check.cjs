const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const puppeteer = require('../tools/node_modules/puppeteer-core');
const root = path.resolve(__dirname, '..');
const fixture = spawnSync(path.join(root, '.venv/Scripts/python.exe'), ['-c',
  'import json; from tests.test_d1_structure import daily_bars; from app.services.d1_structure import daily_structure; bars=daily_bars(); print(json.dumps({"daily":daily_structure(bars,bars[-1].close_time),"bars":[{"time":c.open_time//1000,"open":c.open,"high":c.high,"low":c.low,"close":c.close} for c in bars]}))'],
  {cwd: root, encoding:'utf8'});
assert.equal(fixture.status, 0, fixture.stderr);
const data = JSON.parse(fixture.stdout);
const source = fs.readFileSync(path.join(root, 'app/web/static/app.js'), 'utf8');
const draw = source.slice(source.indexOf('function drawDailyStructure('), source.indexOf('function drawH1Breaks('));

(async () => {
  const browser = await puppeteer.launch({executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe', headless:true});
  try {
    const page = await browser.newPage();
    await page.setViewport({width:1100, height:650});
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.setContent('<input id="layer-structure-d1" type="checkbox" checked><div style="position:relative;width:1000px;height:560px"><div id="chart" style="height:560px"></div><div id="overlay" style="position:absolute;inset:0;pointer-events:none"></div></div>');
    await page.addScriptTag({path:path.join(root, 'app/web/static/vendor/lightweight-charts.js')});
    await page.evaluate(data => {
      window.fixture = data;
      window.$ = id => document.getElementById(id);
      window.fmtPrice = String; window.fmtTime = ms => new Date(ms).toISOString();
      window.state = {dailyStructure:data.daily, candles:data.bars};
      state.chart = LightweightCharts.createChart($('chart'), {width:1000,height:560});
      state.candleSeries = state.chart.addCandlestickSeries();
      state.candleSeries.setData(state.candles);
      state.chart.timeScale().fitContent();
    }, data);
    await page.addScriptTag({content:draw});
    const render = async () => {
      await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      return page.evaluate(() => {
        $('overlay').innerHTML = '';
        drawDailyStructure($('overlay'), state.chart.timeScale(), 940, 560);
        return $('overlay').textContent;
      });
    };
    let text = await render();
    assert.match(text, /D1 HH/); assert.match(text, /D1 BOS/); assert.match(text, /D1 SMS/);
    await page.evaluate(() => {
      state.instrumentId = 1;
      window.deskData = {current:{instrument:{id:1},asset:{trade_setup:{daily_alignment:{target:{
        price:12, type:'OB', text:'Цель до OB D1 12'
      }}}}}};
    });
    assert.match(await render(), /Цель · OB D1/);
    await page.evaluate(() => $('layer-structure-d1').checked = false);
    assert.equal(await render(), '');
    await page.evaluate(() => {
      $('layer-structure-d1').checked = true;
      state.candles = fixture.bars.flatMap(b => Array.from({length:24}, (_, i) => ({...b,time:b.time+i*3600})));
      state.candleSeries.setData(state.candles); state.chart.timeScale().fitContent();
    });
    assert.match(await render(), /D1 BOS/);
    await page.evaluate(() => {
      state.candles = fixture.bars.filter((b,i) => i%7===0).map((b,i) => {
        const week = fixture.bars.slice(i*7, i*7+7);
        return {...b, high:Math.max(...week.map(c=>c.high)), low:Math.min(...week.map(c=>c.low)), close:week.at(-1).close};
      });
      state.candleSeries.setData(state.candles); state.chart.timeScale().fitContent();
    });
    assert.match(await render(), /D1 BOS/);
    // History view must filter before limiting the last 20 points / 50 events.
    await page.evaluate(() => {
      const day = 86400;
      state.candles = fixture.bars.concat(Array.from({length:80}, (_, i) => ({
        ...fixture.bars.at(-1), time:fixture.bars.at(-1).time+(i+1)*day
      })));
      state.dailyStructure = {...fixture.daily,
        pivots:fixture.daily.pivots.concat(Array.from({length:40}, (_, i) => ({
          ...fixture.daily.pivots[0], pivot_at:(fixture.bars.at(-1).time+(i+1)*day)*1000, role:'HH'
        }))),
        events:fixture.daily.events.concat(Array.from({length:60}, (_, i) => ({
          ...fixture.daily.events[0], anchor_at:(fixture.bars.at(-1).time+(i+1)*day)*1000,
          open_time:(fixture.bars.at(-1).time+(i+2)*day)*1000
        })))
      };
      state.candleSeries.setData(state.candles);
      state.chart.timeScale().setVisibleRange({from:fixture.bars[0].time, to:fixture.bars.at(-1).time});
    });
    text = await render();
    assert.match(text, /D1 HH/); assert.match(text, /D1 BOS/);
    await page.evaluate(() => state.dailyStructure = null);
    assert.match(await render(), /Структура D1 не загружена/);
    assert.deepEqual(errors, []);
    console.log('D1 chart: daily/hourly/weekly, history viewport, layer toggle and missing-data status passed');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode = 1; });
