const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../app/web/static/common.js'), 'utf8');
const sandbox = {};
vm.createContext(sandbox);
vm.runInContext(source.slice(source.indexOf('  function chartPriceFormat('),
  source.indexOf('  // Форматируем только подписи:'))
  + ';this.display = zoneDisplay; this.priceFormat = chartPriceFormat;', sandbox);
const d = sandbox.display;
const now = 1800000000000, day = 86400000;
const live = { type: 'fvg', direction: 'bull', status: 'active', relevant: true,
  lower: 0.0492, upper: 0.05532, formed_at: now - 10 * day, display_until: null };
for (const timeframe of ['D1', 'W1', 'H1']) {
  for (const type of ['fvg', 'FVG', 'ob', 'breaker', 'bsl', 'ssl']) {
    const z = { ...live, type, timeframe };
    assert.ok(d.visible(z));
    assert.ok(d.visible({ ...z, status: 'candidate' }), 'Confirmed review candidates remain current');
    assert.ok(!d.visible({ ...z, relevant: false }), 'Canonical server relevance is authoritative');
    const ended = { ...z, display_until: now - day };
    assert.ok(!d.visible(ended), 'A stale active status must not revive a completed object');
    assert.ok(!d.current(ended));
    assert.ok(d.visible(ended, { history: true }));
  }
}
assert.ok(d.visible({ ...live, status: 'candidate', relevant: false }, { candidates: true }));
assert.ok(!d.visible({ ...live, status: 'rejected' }, { history: true, candidates: true }));
for (const type of ['ssl', 'bsl', 'SSL', 'BSL']) {
  const z = { ...live, type, status: 'taken', display_until: now - day };
  assert.ok(!d.visible(z));
  assert.ok(d.visible(z, { swept: true, now }));
  assert.ok(d.visible({ ...z, display_until: now - 7 * day }, { swept: true, now }));
  assert.ok(!d.visible({ ...z, display_until: now - 7 * day - 1 }, { swept: true, now }));
  assert.ok(!d.visible({ ...z, display_until: now + 1 }, { swept: true, now }));
  assert.ok(d.visible({ ...z, display_until: now - 8 * day }, { history: true, now }));
}
for (const [a, b] of [[-20, -1], [101, 150], [null, 80], [NaN, 90]]) {
  assert.equal(d.clipBand(a, b, 100), null, 'Offscreen or unknown coordinates do not create edge stripes');
}
assert.equal(d.clipBand(-20, 50, 100).height, 50);
assert.equal(d.clipBand(20, 150, 100).height, 80);
const spanOptions = { xOf: ms => ms < 100 ? null : ms, firstTime: 100, paneRight: 1000 };
assert.equal(d.timeSpan({ ...live, formed_at: 10, display_until: 50 }, spanOptions), null);
assert.equal(d.timeSpan({ ...live, formed_at: 200, display_until: 190 }, spanOptions), null);
assert.equal(d.timeSpan({ ...live, formed_at: 200, status: 'archived' }, spanOptions), null);
assert.equal(d.timeSpan({ ...live, formed_at: 200, display_until: 250 }, spanOptions).right, 250);
assert.equal(d.timeSpan({ ...live, formed_at: 10 }, spanOptions).left, 0);
assert.equal(d.timeSpan({ ...live, formed_at: 1200 }, spanOptions), null);

function band(top = 0, height = 100) {
  return { style: { top: top + 'px', height: height + 'px' },
    classes: [], children: [], classList: { add(name) { this.owner.classes.push(name); } },
    ownerDocument: { createElement() { return { style: {} }; } },
    appendChild(child) { this.children.push(child); } };
}
for (const type of ['fvg', 'FVG']) for (const direction of ['bull', 'bear']) {
  const el = band(); el.classList.owner = el;
  const zone = { ...live, type, direction, lower: 0, upper: 100, max_test_depth: .25 };
  d.appendFill(el, zone, price => 100 - price);
  assert.equal(el.children.length, 1);
  assert.equal(el.children[0].style.height, '75px');
  assert.equal(el.children[0].style.top, direction === 'bull' ? '25px' : '0px');
  const full = band(); full.classList.owner = full;
  d.appendFill(full, { ...zone, max_test_depth: 1 }, price => 100 - price);
  assert.ok(full.classes.includes('fvg-partial'));
  assert.equal(full.children.length, 0, 'Fully consumed FVG has no unfilled area');
}
const clipped = band(0, 50); clipped.classList.owner = clipped;
d.appendFill(clipped, { ...live, lower: 0, upper: 100, max_test_depth: .75 }, price => 50 - price);
assert.equal(clipped.children[0].style.top, '25px');
assert.equal(clipped.children[0].style.height, '25px', 'Clipping does not change fill depth');
const legacy = band(); legacy.classList.owner = legacy;
d.appendFill(legacy, { ...live, lower: 0, upper: 100, max_test_depth: 0, fill_depth: .6 }, price => 100 - price);
assert.equal(legacy.children[0].style.height, '40px', 'FVG visit facts override missing OB statistics');
for (const [price, precision] of [[81704, 2], [.05444, 6], [.000004321, 10], [1e-12, 12]]) {
  const f = sandbox.priceFormat([{ open: price, high: price, low: price, close: price }]);
  assert.equal(f.precision, precision);
  assert.equal(f.minMove, 10 ** -precision);
}
assert.equal(sandbox.priceFormat([]).precision, 2);
assert.equal(sandbox.priceFormat([{ low: NaN, close: 0 }]).precision, 2);

// H1's shared renderer also treats a dated end as terminal even if a legacy
// snapshot still says "active". Both the desk and LTF use this renderer.
vm.runInContext(fs.readFileSync(path.join(__dirname, '../app/web/static/h1_layers.js'), 'utf8'), sandbox);
const h1 = sandbox.H1Layers;
for (const type of ['bsl', 'ssl', 'BSL', 'SSL']) {
  assert.equal(h1.zonePlan({ type, lower: 1, upper: 2 }).shape, 'line');
  assert.equal(h1.zonePlan({ type, lower: 1, upper: 2 }).label, type.toUpperCase() + ' H1');
}
const h1Layers = { detected_zones: [{ ...live, lifecycle: 'active', display_until: now - day }] };
assert.equal(h1.selectedZones(h1Layers, h1.DEFAULTS).length, 0);
assert.equal(h1.selectedZones(h1Layers, { ...h1.DEFAULTS, historicalZones: true }).length, 1);

// Focused H1 price scales must work for S and sub-cent assets on both screens.
const appSource = fs.readFileSync(path.join(__dirname, '../app/web/static/app.js'), 'utf8').replace(/\r\n/g, '\n');
const scaleStart = appSource.indexOf('autoscaleInfoProvider: (original) => {') + 'autoscaleInfoProvider: '.length;
const scaleEnd = appSource.indexOf('\n    },\n  });', scaleStart) + '\n    }'.length;
sandbox.state = { h1PriceFocus: { lower: .0492, upper: .05532 } };
vm.runInContext('this.deskScale = ' + appSource.slice(scaleStart, scaleEnd), sandbox);
const deskRange = sandbox.deskScale().priceRange;
assert.ok(deskRange.minValue > 0 && deskRange.maxValue < .1);
const ltfSource = fs.readFileSync(path.join(__dirname, '../app/web/static/ltf.js'), 'utf8');
sandbox.state.candleSeries = { applyOptions(options) { sandbox.ltfScale = options.autoscaleInfoProvider; } };
vm.runInContext(ltfSource.slice(ltfSource.indexOf('function applyLtfPriceScale('),
  ltfSource.indexOf('function structurePointColor(')), sandbox);
sandbox.applyLtfPriceScale({ lower: .0492, upper: .05532 });
const ltfRange = sandbox.ltfScale().priceRange;
assert.ok(ltfRange.minValue > 0 && ltfRange.maxValue < .1);
console.log('Zone display: relevance, history, sweep window, boundaries, FVG fill and small-price precision passed');
