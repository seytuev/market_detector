/* Общие слои H1 для основного рабочего места и страницы LTF.
   Подписи точек ограничены ответом сервера. BOS/SMS рисуются только
   из событий машины пробоев, без выдуманного начала. */
(function (root) {
  const KEY = 'lf:h1-layers';
  const DEFAULTS = {
    points: 'recent',
    diagnostic: false,
    breaks: true,
    expected: true,
    zones: true,
    ob: true,
    fvg: true,
    bsl: true,
    ssl: true,
    eligibleOnly: false,
    candidates: false,
    historicalZones: false,
    htfContext: true,
  };

  function loadSettings() {
    let saved = {};
    try {
      saved = JSON.parse(root.localStorage.getItem(KEY) || '{}') || {};
    } catch (e) {
      saved = {};
    }
    const out = Object.assign({}, DEFAULTS);
    Object.keys(DEFAULTS).forEach((name) => {
      if (Object.prototype.hasOwnProperty.call(saved, name)) out[name] = saved[name];
    });
    if (out.points !== 'recent' && out.points !== 'history' && out.points !== 'hidden') {
      out.points = 'recent';
    }
    return out;
  }

  function saveSettings(next) {
    const merged = Object.assign(loadSettings(), next || {});
    try {
      root.localStorage.setItem(KEY, JSON.stringify(merged));
    } catch (e) { /* приватный режим */ }
    return merged;
  }

  function queryString(extra) {
    const settings = loadSettings();
    const params = new URLSearchParams();
    params.set('points', settings.points || 'recent');
    if (settings.diagnostic) params.set('diagnostic', 'true');
    if (extra && extra.context_id) params.set('context_id', String(extra.context_id));
    if (settings.points === 'history' && extra && extra.from != null && extra.to != null) {
      params.set('from', String(extra.from));
      params.set('to', String(extra.to));
    }
    if (settings.historicalZones) params.set('zone_history', 'true');
    return params.toString();
  }

  function caption(ev) {
    const arrow = ev.direction === 'bull' ? '↑' : '↓';
    return (ev.kind || 'BOS') + ' ' + arrow;
  }

  function breakPlan(ev, pivotById) {
    const pivots = pivotById || {};
    const pivot = ev.level_pivot_id != null ? pivots[ev.level_pivot_id] : null;
    const start = pivot && (pivot.pivot_at || pivot.candle_open_time);
    return {
      originKnown: !!start,
      startMs: start || null,
      endMs: ev.break_candle_open_time || ev.confirmed_at || ev.occurred_at,
      level: ev.break_level,
      caption: caption(ev),
    };
  }

  function zonePlan(zone) {
    const level = !!(zone.is_level || zone.type === 'BSL' || zone.type === 'SSL'
      || zone.lower === zone.upper);
    return {
      shape: level ? 'line' : 'rect',
      label: (zone.type || 'Зона') + ' H1',
    };
  }

  function mergeCaption(events) {
    const unique = [];
    events.forEach((ev) => {
      const text = caption(ev);
      if (unique.indexOf(text) === -1) unique.push(text);
    });
    const extra = events.length - unique.length;
    return extra > 0 ? unique.join(' · ') + ' · ещё ' + extra : unique.join(' · ');
  }

  function emptyZoneMessage(status, view) {
    const info = view || {};
    if (info.error || (status && status.state === 'error')) {
      return { text: 'Не удалось загрузить зоны H1', retry: true };
    }
    if (!status || status.state === 'no_data' || status.state === 'loading') {
      const reason = (status && status.reason) || 'нет данных';
      return { text: 'Зоны H1 ещё не рассчитаны: ' + reason };
    }
    if (info.eligibleOnly && !info.scenarioOpen) {
      return {
        text: 'Выбран фильтр пригодности, но сценарий не открыт',
        showAll: true,
      };
    }
    if ((info.hiddenByFilter || 0) > 0 && (info.visible || 0) === 0) {
      return { text: 'Скрыто фильтрами: ' + info.hiddenByFilter + ' зон', reset: true };
    }
    if ((info.visible || 0) === 0 && (info.outside || 0) > 0) {
      return { text: info.outside + ' зон вне видимой области' };
    }
    if (status.state === 'calculated' && (status.total || 0) === 0 && (info.visible || 0) === 0) {
      return { text: 'На выбранном участке подтверждённые зоны H1 не найдены' };
    }
    return null;
  }

  function typeOn(settings, zone) {
    const type = String(zone.type || '').toLowerCase();
    if (type === 'ob') return settings.ob;
    if (type === 'fvg') return settings.fvg;
    if (type === 'bsl') return settings.bsl;
    if (type === 'ssl') return settings.ssl;
    return true;
  }

  function passesFilters(zone, settings) {
    if (!settings.zones || !typeOn(settings, zone)) return false;
    if ((zone.candidate || zone.lifecycle === 'candidate') && !settings.candidates) return false;
    if (zone.lifecycle === 'ended' && !zone.recently_taken && !settings.historicalZones) return false;
    return true;
  }

  function admissionOf(layers, zone) {
    const rows = (layers && layers.scenario_admission) || [];
    for (let i = 0; i < rows.length; i += 1) {
      if (String(rows[i].zone_id) === String(zone.id)) return rows[i];
    }
    return null;
  }

  function inViewport(zone, view) {
    if (!view) return true;
    const from = zone.display_from || zone.formed_at || 0;
    const until = zone.display_until == null ? Infinity : zone.display_until;
    const timeOk = view.timeFrom == null || (from <= view.timeTo && until >= view.timeFrom);
    const priceOk = view.priceMin == null || (zone.upper >= view.priceMin && zone.lower <= view.priceMax);
    return timeOk && priceOk;
  }

  function markerList(layers, selectedId) {
    const settings = loadSettings();
    const points = settings.points === 'hidden' ? [] : ((layers && layers.pivot_markers) || []);
    const list = points.slice();
    const ev = ((layers && layers.structural_events) || []).find((item) => item.id === selectedId);
    if (!ev || ev.level_pivot_id == null) return list;
    if (list.some((p) => p.id === ev.level_pivot_id)) return list;
    const anchor = ((layers && layers.anchor_refs) || []).find((p) => p.id === ev.level_pivot_id);
    if (anchor) list.push(Object.assign({ temporary: true }, anchor));
    return list;
  }

  function seriesMarkers(layers, selectedId, colorOf) {
    return markerList(layers, selectedId).filter((p) => p.pivot_at || p.candle_open_time).map((p) => ({
      time: Math.floor((p.candle_open_time || p.pivot_at) / 1000),
      position: p.kind === 'high' ? 'aboveBar' : 'belowBar',
      shape: p.kind === 'high' ? 'arrowDown' : 'arrowUp',
      color: colorOf ? colorOf(p.role) : '#A2B0C5',
      text: p.role || (p.kind === 'high' ? 'H' : 'L'),
    }));
  }

  function shown(value, fmt, fallback) {
    if (value == null || value === '') return fallback;
    return fmt ? fmt(value) : value;
  }

  function eventSource(layers) {
    if (!layers) return [];
    if (Array.isArray(layers.structural_events)) return layers.structural_events;
    return layers.structure_events || [];
  }

  function eventCard(ev, format) {
    if (!ev) return '';
    const fmt = format || {};
    const dir = ev.direction === 'bull' ? 'вверх' : 'вниз';
    const before = ev.structure_before || '—';
    const after = ev.structure_after || '—';
    const scenarios = (ev.scenario_ids || []).join(', ') || 'нет';
    return [
      '<p><b>' + caption(ev) + '</b> · ' + dir + '</p>',
      '<p>Уровень ' + shown(ev.break_level, fmt.price, '—') + '</p>',
      '<p>Опора ' + (ev.level_pivot_id != null ? ev.level_pivot_id : 'не восстановлена') + '</p>',
      '<p>Свеча ' + shown(ev.break_candle_open_time, fmt.time, '—') + '</p>',
      '<p>Закрытие ' + shown(ev.confirmed_at || ev.occurred_at, fmt.time, '—') + '</p>',
      '<p>Обнаружено ' + shown(ev.detected_at, fmt.time, '—') + '</p>',
      '<p>Структура ' + before + ' → ' + after + '</p>',
      '<p>Сценарии: ' + scenarios + '</p>',
    ].join('');
  }

  function zoneCard(zone, admission, format) {
    const adm = admission || {};
    const fmt = format || {};
    const reason = adm.reason || 'Пригодность для сценария не оценивалась';
    return [
      '<p><b>' + (zone.type || '') + ' H1</b> · ' + (zone.direction || '') + '</p>',
      '<p>Подтверждение ' + shown(zone.confirmed_at, fmt.time, 'ещё нет') + '</p>',
      '<p>Состояние ' + (zone.lifecycle || '—') + (zone.level_state ? ' · ' + zone.level_state : '') + '</p>',
      '<p>Пригодность: ' + (adm.eligibility || 'not_evaluated') + '</p>',
      '<p>' + reason + '</p>',
      zone.display_window_label ? '<p>' + zone.display_window_label + '</p>' : '',
    ].join('');
  }

  function draw(overlay, ctx) {
    if (!overlay || !ctx || !ctx.layers) return;
    const layers = ctx.layers;
    const settings = ctx.settings || loadSettings();
    const xOf = ctx.xOf;
    const yOf = ctx.yOf;
    const paneRight = ctx.paneRight;
    const height = ctx.height;
    const fmtPrice = ctx.fmtPrice || String;
    const fmtTime = ctx.fmtTime || String;
    const view = ctx.view || {};
    const scenarioOpen = !!(layers.snapshot && layers.snapshot.scenario_open);
    const doc = overlay.ownerDocument || root.document;

    const add = (cls) => {
      const div = doc.createElement('div');
      div.className = cls;
      overlay.appendChild(div);
      return div;
    };

    if (ctx.transitionEl) {
      const transition = layers.structure_transition;
      const label = transition && transition.label;
      ctx.transitionEl.textContent = label || '';
      ctx.transitionEl.classList.toggle('hidden', !label);
    }

    const allZones = layers.detected_zones || [];
    let hiddenByFilter = 0;
    const filtered = [];
    if (!(settings.eligibleOnly && !scenarioOpen)) {
      allZones.forEach((zone) => {
        if (!passesFilters(zone, settings)) {
          hiddenByFilter += 1;
          return;
        }
        if (settings.eligibleOnly) {
          const adm = admissionOf(layers, zone);
          if (!adm || adm.eligibility !== 'eligible') {
            hiddenByFilter += 1;
            return;
          }
        }
        filtered.push(zone);
      });
    } else {
      hiddenByFilter = allZones.length;
    }
    const outside = [];
    const visible = [];
    filtered.forEach((zone) => {
      if (inViewport(zone, view)) visible.push(zone);
      else outside.push(zone);
    });

    if (settings.zones) {
      visible.forEach((zone) => {
        const plan = zonePlan(zone);
        const from = zone.display_from || zone.formed_at;
        let x1 = xOf(from);
        const clipped = x1 == null || x1 < 0;
        if (clipped) x1 = 0;
        let x2 = paneRight;
        if (zone.display_until != null) {
          const end = xOf(zone.display_until);
          if (end != null) x2 = Math.min(paneRight, end);
        }
        if (x2 <= x1) return;
        const adm = admissionOf(layers, zone);
        const title = plan.label + ' [' + fmtPrice(zone.lower) + '–' + fmtPrice(zone.upper) + ']'
          + ' · ' + (zone.lifecycle || '')
          + ' · ' + ((adm && adm.reason) || 'Пригодность для сценария не оценивалась')
          + (zone.display_window_label ? ' · ' + zone.display_window_label : '');
        const selected = ctx.selectedZoneId != null && String(ctx.selectedZoneId) === String(zone.id);
        if (plan.shape === 'line') {
          const y = yOf(zone.lower);
          if (y == null || y < -4 || y > height + 4) {
            outside.push(zone);
            return;
          }
          const div = add('ltf-level ltf-entry ltf-entry-' + String(zone.type).toLowerCase()
            + (zone.candidate ? ' status-candidate' : '') + (selected ? ' selected' : ''));
          div.style.top = y + 'px';
          div.style.left = x1 + 'px';
          div.style.width = Math.max(4, x2 - x1) + 'px';
          div.title = title;
          div.textContent = plan.label;
          div.onclick = () => ctx.onZone && ctx.onZone(zone, adm);
        } else {
          const y1 = yOf(zone.upper);
          const y2 = yOf(zone.lower);
          if (y1 == null && y2 == null) {
            outside.push(zone);
            return;
          }
          const top = Math.max(0, Math.min(y1 == null ? 0 : y1, y2 == null ? height : y2));
          const bottom = Math.min(height, Math.max(y1 == null ? 0 : y1, y2 == null ? height : y2));
          if (bottom <= 0 || top >= height) {
            outside.push(zone);
            return;
          }
          const div = add('h1-zone zone-rect z-' + String(zone.type).toLowerCase()
            + (zone.candidate ? ' status-candidate' : '')
            + (zone.lifecycle === 'ended' ? ' status-completed' : '')
            + (selected ? ' selected' : ''));
          div.style.top = top + 'px';
          div.style.height = Math.max(6, bottom - top) + 'px';
          div.style.left = x1 + 'px';
          div.style.width = Math.max(8, x2 - x1) + 'px';
          div.title = title;
          div.textContent = plan.label;
          div.onclick = () => ctx.onZone && ctx.onZone(zone, adm);
        }
      });
    }

    if (settings.breaks) {
      const pivots = {};
      (layers.anchor_refs || []).forEach((p) => { pivots[p.id] = p; });
      (layers.pivot_markers || []).forEach((p) => { pivots[p.id] = p; });
      (layers.pivots || []).forEach((p) => { if (!pivots[p.id]) pivots[p.id] = p; });
      const source = eventSource(layers);
      const placed = [];
      source.forEach((ev) => {
        if (!ev || ev.break_level == null) return;
        const plan = breakPlan(ev, pivots);
        const y = yOf(ev.break_level);
        const x2raw = xOf(plan.endMs);
        if (y == null || x2raw == null) return;
        if (!plan.originKnown) {
          if (x2raw < 0 || x2raw > paneRight) return;
          const mark = add('h1-origin-missing');
          mark.style.left = Math.max(0, x2raw) + 'px';
          mark.style.top = Math.max(0, y - 12) + 'px';
          mark.textContent = 'Исходная опора не восстановлена';
          mark.onclick = () => ctx.onEvent && ctx.onEvent([ev]);
          return;
        }
        let x1 = xOf(plan.startMs);
        const clipped = x1 == null || x1 < 0;
        if (clipped) x1 = 0;
        const x2 = Math.min(paneRight, x2raw);
        if (x2 <= x1 || x2 < 0) return;
        const div = add('ltf-break ltf-' + String(ev.kind || 'bos').toLowerCase()
          + (ev.accompanying ? ' accompanying' : '')
          + (clipped ? ' ltf-break-clip' : '')
          + (ev.direction === 'bull' ? ' ltf-up' : ' ltf-down'));
        div.style.top = y + 'px';
        div.style.left = x1 + 'px';
        div.style.width = Math.max(4, x2 - x1) + 'px';
        div.title = plan.caption + ' · ' + fmtPrice(ev.break_level)
          + ' · закрытие ' + fmtTime(ev.confirmed_at || ev.occurred_at)
          + ' · обнаружено ' + fmtTime(ev.detected_at);
        const label = doc.createElement('span');
        label.className = 'h1-break-label';
        label.textContent = plan.caption;
        div.appendChild(label);
        placed.push({ ev, div, label, y, x: x2 });
        div.onclick = () => ctx.onEvent && ctx.onEvent([ev]);
      });
      const buckets = new Map();
      placed.forEach((item) => {
        const key = Math.round(item.y / 16) + ':' + Math.round(item.x / 36);
        if (!buckets.has(key)) buckets.set(key, []);
        buckets.get(key).push(item);
      });
      buckets.forEach((items) => {
        if (items.length < 2) return;
        items.forEach((item, index) => {
          if (index === 0) {
            item.label.textContent = mergeCaption(items.map((row) => row.ev));
            item.div.onclick = () => ctx.onEvent && ctx.onEvent(items.map((row) => row.ev));
          } else {
            item.label.remove();
          }
        });
      });
    }

    if (settings.expected) {
      const exp = layers.expected_structure || null;
      const pairs = exp ? [['bos', exp.bos], ['sms', exp.sms]] : [];
      pairs.forEach(([key, row]) => {
        if (!row || row.level == null || row.confirmed) return;
        const pivot = row.ref_pivot || row.internal_pivot;
        if (!pivot || pivot.pivot_at == null) return;
        let x1 = xOf(pivot.pivot_at);
        if (x1 == null || x1 < 0) x1 = 0;
        const y = yOf(row.level);
        if (y == null || y < 0 || y > height || paneRight <= x1) return;
        const div = add('ltf-expected ltf-expected-' + key);
        div.style.top = y + 'px';
        div.style.left = x1 + 'px';
        div.style.width = Math.max(4, paneRight - x1) + 'px';
        const text = row.label || ('Ожидаемый ' + key.toUpperCase());
        div.title = text + ' · ' + fmtPrice(row.level);
        const span = doc.createElement('span');
        span.className = row.direction === 'bull' ? 'lbl-above' : 'lbl-below';
        span.textContent = text;
        div.appendChild(span);
      });
    }

    if (ctx.statusEl) {
      const status = layers.layer_status && layers.layer_status.zones;
      const message = emptyZoneMessage(status, {
        error: !!ctx.loadError || (status && status.state === 'error'),
        scenarioOpen,
        eligibleOnly: settings.eligibleOnly,
        hiddenByFilter,
        outside: outside.length,
        visible: settings.zones ? visible.length : 0,
      });
      ctx.statusEl.classList.toggle('hidden', !message);
      ctx.statusEl.innerHTML = '';
      if (message) {
        const span = doc.createElement('span');
        span.textContent = message.text;
        ctx.statusEl.appendChild(span);
        if (message.retry) actionButton(doc, ctx.statusEl, 'Повторить', ctx.onRetry);
        if (message.reset) actionButton(doc, ctx.statusEl, 'Сбросить фильтры', ctx.onResetFilters);
        if (message.showAll) actionButton(doc, ctx.statusEl, 'Показать все зоны H1', ctx.onShowAllZones);
      }
    }
    if (ctx.offscreenEl) {
      const count = outside.length;
      ctx.offscreenEl.classList.toggle('hidden', count === 0);
      ctx.offscreenEl.textContent = 'Вне экрана: ' + count;
      ctx.offscreenEl.onclick = () => {
        if (outside[0] && ctx.onShowZone) ctx.onShowZone(outside[0]);
      };
    }
  }

  function actionButton(doc, parent, text, handler) {
    const button = doc.createElement('button');
    button.type = 'button';
    button.className = 'btn small';
    button.textContent = text;
    button.onclick = () => handler && handler();
    parent.appendChild(button);
  }

  function bindControls(onChange) {
    const doc = root.document;
    if (!doc) return;
    const settings = loadSettings();
    const points = doc.getElementById('h1-points');
    if (points) points.value = settings.points;
    const flags = {
      'h1-diagnostic': 'diagnostic',
      'h1-breaks': 'breaks',
      'h1-expected': 'expected',
      'h1-zones': 'zones',
      'h1-ob': 'ob',
      'h1-fvg': 'fvg',
      'h1-bsl': 'bsl',
      'h1-ssl': 'ssl',
      'h1-eligible-only': 'eligibleOnly',
      'h1-candidates': 'candidates',
      'h1-zone-history': 'historicalZones',
      'h1-htf': 'htfContext',
    };
    Object.keys(flags).forEach((id) => {
      const el = doc.getElementById(id);
      if (!el) return;
      el.checked = !!settings[flags[id]];
      el.onchange = () => {
        const patch = {};
        patch[flags[id]] = el.checked;
        saveSettings(patch);
        if (onChange) onChange(loadSettings());
      };
    });
    if (points) {
      points.onchange = () => {
        saveSettings({ points: points.value });
        if (onChange) onChange(loadSettings());
      };
    }
  }

  root.H1Layers = {
    DEFAULTS,
    loadSettings,
    saveSettings,
    queryString,
    caption,
    breakPlan,
    zonePlan,
    mergeCaption,
    emptyZoneMessage,
    markerList,
    seriesMarkers,
    eventSource,
    eventCard,
    zoneCard,
    draw,
    bindControls,
  };
}(typeof window !== 'undefined' ? window : globalThis));
