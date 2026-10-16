/* Offline notification surface shared by H1, HTF zones and altcoin D1. */
(async function () {
  try {
    // CLI headless rendering advances timers reliably even while rAF is paused.
    await new Promise(resolve => setTimeout(resolve, 100));
    const snapshot = window.h1Snapshot;
    const bars = snapshot.candles;
    const layers = snapshot.layers;
    const timeframe = snapshot.timeframe || 'H1';
    const period = { H1: 3600, H4: 14400, D1: 86400, W1: 604800 }[timeframe] || 3600;
    const $ = id => document.getElementById(id);
    $('title').textContent = snapshot.title;
    $('subtitle').textContent = snapshot.subtitle;
    const theme = HTF.chartTheme();
    const minPrice = Math.min(...bars.flatMap(bar => [bar.low, bar.close]).filter(value => value > 0));
    const precision = Math.min(12, Math.max(2, 4 - Math.floor(Math.log10(minPrice))));
    const fmtPrice = value => Number(value).toLocaleString('ru-RU', { maximumFractionDigits: precision });
    const fmtTime = value => new Date(value).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' });
    const chart = LightweightCharts.createChart($('chart'), {
      width: $('frame').clientWidth, height: $('frame').clientHeight,
      layout: { background: { color: theme.background }, textColor: theme.text },
      grid: { vertLines: { color: theme.grid }, horzLines: { color: theme.grid } },
      ...HTF.chartTimeOptions({ timeVisible: period < 86400, borderColor: theme.border }),
      rightPriceScale: { borderColor: theme.border },
      handleScroll: false, handleScale: false,
    });
    const series = chart.addCandlestickSeries({
      ...HTF.candleOptions(theme),
      priceFormat: { type: 'price', precision, minMove: 10 ** -precision },
      ...(snapshot.quote || snapshot.price_bounds ? {
        ...(snapshot.quote ? { lastValueVisible: false, priceLineVisible: false } : {}),
        autoscaleInfoProvider: original => {
          const info = original();
          if (!info) return info;
          return { ...info, priceRange: {
            minValue: Math.min(info.priceRange.minValue, snapshot.quote?.price ?? Infinity, snapshot.price_bounds?.min ?? Infinity),
            maxValue: Math.max(info.priceRange.maxValue, snapshot.quote?.price ?? -Infinity, snapshot.price_bounds?.max ?? -Infinity),
          } };
        },
      } : {}),
    });
    series.setData(bars);
    if (snapshot.quote) series.createPriceLine({
      price: snapshot.quote.price, color: theme.text, lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true,
      title: snapshot.quote.label || 'Котировка',
    });
    const first = bars[0].time, last = bars[bars.length - 1].time;
    const markers = layers && snapshot.layer_settings?.points === 'hidden' ? [] : layers ? H1Layers.seriesMarkers(layers, null, role =>
      ['HH', 'HL'].includes(role) ? theme.up : theme.down
    ) : (snapshot.markers || []);
    series.setMarkers(markers.filter(mark => mark.time >= first && mark.time <= last)
      .sort((a, b) => a.time - b.time));
    const ts = chart.timeScale();
    const paneWidth = () => $('frame').clientWidth - chart.priceScale('right').width();
    // Current-request charts reserve 2.5 times the original space after the last candle.
    ts.setVisibleLogicalRange(snapshot.fit_content
      ? { from: -1, to: bars.length - 1 + 2.5 * Math.max(2, Math.ceil(bars.length * .06)) }
      : H1Layers.currentFrame(bars.length, paneWidth()));
    // Wait for the chart's own canvas layout before projecting DOM overlays.
    await new Promise(resolve => setTimeout(resolve, 200));
    const paneHeight = $('frame').clientHeight - ts.height();
    const xOf = ms => {
      if (ms == null) return null;
      const sec = Math.floor(ms / 1000);
      const exact = ts.timeToCoordinate(sec);
      if (exact != null) return exact;
      let lo = 0, hi = bars.length - 1, best = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (bars[mid].time <= sec) { best = mid; lo = mid + 1; } else hi = mid - 1;
      }
      if (best < 0) return null;
      const next = bars[best + 1];
      const fraction = Math.min(1, (sec - bars[best].time) / (next ? next.time - bars[best].time : period));
      return ts.logicalToCoordinate(best + fraction);
    };
    const visible = ts.getVisibleRange();
    if (layers) H1Layers.draw($('zone-overlay'), {
      layers, settings: { ...H1Layers.DEFAULTS, ...snapshot.layer_settings }, xOf,
      yOf: price => series.priceToCoordinate(price), paneRight: paneWidth(), height: paneHeight,
      fmtPrice, fmtTime,
      view: { timeFrom: visible.from * 1000, timeTo: visible.to * 1000,
        priceMin: series.coordinateToPrice(paneHeight), priceMax: series.coordinateToPrice(0) },
      transitionEl: $('transition'), statusEl: $('status'), offscreenEl: $('offscreen'),
      selectedZoneId: (snapshot.event?.payload || {}).entry_zone_id,
    });
    // HTF and accumulation snapshots use the same chart, label placement and
    // website styles; their prices and native timeframe come from their source.
    if (!layers) {
      const overlay = $('zone-overlay');
      const right = paneWidth();
      const occupied = [];
      const add = (cls, left, top, width) => {
        const div = document.createElement('div');
        div.className = cls;
        Object.assign(div.style, { left: left + 'px', top: top + 'px', width: width + 'px' });
        overlay.appendChild(div);
        return div;
      };
      let outside = 0;
      (snapshot.overlays || []).forEach(zone => {
        const x = xOf(zone.from);
        const left = x == null ? 0 : Math.max(0, x);
        const end = zone.to == null ? right : Math.min(right, xOf(zone.to) ?? 0);
        const upper = series.priceToCoordinate(zone.upper);
        const lower = series.priceToCoordinate(zone.lower);
        const level = zone.lower === zone.upper;
        const type = String(zone.type || 'OB').toLowerCase();
        const direction = type === 'bsl' ? 'bull' : type === 'ssl' ? 'bear'
          : zone.direction === 'bear' ? 'bear' : 'bull';
        const color = zone.muted ? '#8b95a3' : zone.color || (direction === 'bull' ? theme.up : theme.down);
        if (upper == null || lower == null || lower < 0 || upper > paneHeight || left >= end) {
          outside += 1;
          return;
        }
        const box = level ? { top: upper, height: 0 } : H1Layers.clipZoneBox(upper, lower, paneHeight);
        if (!box) { outside += 1; return; }
        const div = add(level ? 'h1-liquidity ltf-level' : 'h1-zone zone-rect z-' + type,
          left, box.top, end - left);
        div.style.borderColor = color;
        div.style.color = color;
        if (!level) {
          div.style.height = box.height + 'px';
          div.style.background = zone.muted ? 'rgba(139,149,163,.12)'
            : zone.fill || (direction === 'bull' ? 'rgba(var(--zone-bull-rgb),.12)' : 'rgba(var(--zone-bear-rgb),.12)');
          const mid = series.priceToCoordinate(zone.mid ?? (zone.lower + zone.upper) / 2);
          if (mid > box.top && mid < box.top + box.height) {
            const line = document.createElement('span');
            line.className = 'h1-zone-mid';
            line.style.top = (mid - box.top) + 'px';
            div.appendChild(line);
          }
        }
        const label = H1Layers.placeZoneLabel({ left, right: end, top: box.top,
          bottom: box.top + box.height }, occupied, right, paneHeight);
        if (label) {
          const tag = add('h1-zone-tag type-' + type, label.left, label.top, label.width);
          const name = document.createElement('span');
          name.className = 'h1-zone-name'; name.textContent = zone.label;
          const price = document.createElement('span');
          price.className = 'h1-zone-price';
          price.textContent = zone.price_label || (level ? '' : '50% ') + fmtPrice(zone.mid ?? zone.lower);
          tag.append(name, price);
        }
      });
      (snapshot.levels || []).forEach(level => {
        const y = series.priceToCoordinate(level.price);
        if (y == null || y < 0 || y > paneHeight) { outside += 1; return; }
        const left = Math.max(0, xOf(level.from) ?? 0);
        if (left >= right) return;
        const line = add('ltf-expected', left, y, right - left);
        line.style.color = level.color || theme.accent;
        line.style.borderColor = level.color || theme.accent;
        const text = document.createElement('span');
        text.className = 'lbl-above';
        text.textContent = level.label + ' · ' + fmtPrice(level.price);
        line.appendChild(text);
      });
      $('status').textContent = snapshot.caption || '';
      $('status').classList.toggle('hidden', !snapshot.caption);
      $('offscreen').textContent = 'Вне видимого диапазона: ' + outside;
      $('offscreen').classList.toggle('hidden', !outside);
    }
    await document.fonts.ready;
    await new Promise(resolve => setTimeout(resolve, 200));
    document.documentElement.dataset.rendered = 'true';
  } catch (error) {
    document.documentElement.dataset.renderError = String(error);
  }
}());
