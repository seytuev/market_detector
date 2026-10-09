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
    ideaId: '',
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

  // Labels occupy separate lanes; thin zones keep their true price height.
  function placeZoneLabel(box, occupied, paneRight, height) {
    const width = Math.min(180, paneRight - box.left - 8);
    if (width < 72 || height < 22) return null;
    const preferred = box.bottom - box.top < 30 ? box.top - 23 : box.top + 5;
    const candidates = [preferred];
    if (box.bottom - box.top < 30) candidates.push(box.bottom + 4);
    if (box.bottom - box.top >= 52) candidates.push(box.bottom - 23);
    for (let lane = 0; lane < 3; lane += 1) {
      const left = box.right - width - 8 - lane * (width + 8);
      if (left < box.left + 4) continue;
      for (const y of candidates) {
        const top = Math.max(2, Math.min(height - 22, y));
        const label = { left, top, right: left + width, bottom: top + 20, width };
        if (!occupied.some((p) => label.left < p.right + 4 && label.right + 4 > p.left
          && label.top < p.bottom + 4 && label.bottom + 4 > p.top)) {
          occupied.push(label);
          return label;
        }
      }
    }
    return null;
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
        text: 'Нет действующих HTF-идей с подтверждёнными зонами',
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
    if (zone.lifecycle === 'ended' && !settings.historicalZones) return false;
    return true;
  }

  function selectedZones(layers, settings) {
    const modern = Array.isArray(layers.htf_ideas);
    return (layers.detected_zones || []).filter((zone) => {
      if (!passesFilters(zone, settings)) return false;
      if (!settings.eligibleOnly && !settings.ideaId) return true;
      if (!modern) {
        const adm = admissionOf(layers, zone);
        return !!adm && adm.eligibility === 'eligible';
      }
      const links = (zone.idea_links || []).filter((link) =>
        !settings.ideaId || String(link.scenario_id) === String(settings.ideaId));
      if (settings.historicalZones) return links.length > 0;
      return !!(zone.relevance && zone.relevance.relevant && links.some((link) => link.state === 'active'));
    });
  }

  function ideaLabel(idea) {
    return (idea.direction === 'bull' ? '↑ Покупки' : '↓ Продажи') + ' · '
      + String(idea.parent_type || '').toUpperCase() + ' ' + idea.parent_timeframe
      + ' #' + idea.parent_zone_id + ' · BOS/SMS ' + new Date(idea.trigger_at).toLocaleString('ru-RU');
  }

  function updateIdeaOptions(layers, settings) {
    const select = root.document && root.document.getElementById('h1-idea');
    if (!select || !Array.isArray(layers.htf_ideas)) return;
    const choices = layers.htf_ideas.filter((i) => settings.historicalZones || i.state !== 'closed');
    const signature = JSON.stringify(choices.map((i) => [i.scenario_id, i.state, i.trigger_at]));
    if (select.dataset.ideas !== signature) {
      select.replaceChildren();
      const all = root.document.createElement('option');
      all.value = ''; all.textContent = 'Все HTF-идеи'; select.appendChild(all);
      choices.forEach((i) => {
        const option = root.document.createElement('option');
        option.value = String(i.scenario_id); option.textContent = ideaLabel(i);
        select.appendChild(option);
      });
      select.dataset.ideas = signature;
    }
    if (settings.ideaId && !choices.some((i) => String(i.scenario_id) === String(settings.ideaId))) {
      settings.ideaId = ''; saveSettings({ ideaId: '' });
    }
    select.value = settings.ideaId || '';
    const close = root.document.getElementById('h1-close-idea');
    const chosen = choices.find((i) => String(i.scenario_id) === String(settings.ideaId));
    if (close) {
      close.hidden = !chosen || chosen.state === 'closed';
      close.disabled = !chosen || !chosen.id;
      close.dataset.ideaId = chosen && chosen.id ? String(chosen.id) : '';
    }
  }

  const REASONS = {
    ok: 'Готова по правилам входа', structure_pending: 'Ждём подтверждения структуры',
    range_pending: 'Диапазон ещё не подтверждён', outside_pd: 'Вне нужной половины диапазона',
    data_gap: 'Недостаточно истории для проверки', invalid: 'Зона невалидна',
    fvg_filled: 'FVG полностью перекрыт', tested_too_deep: 'Достигнут порог глубины теста',
    swept_level: 'Ликвидность снята', level_broken: 'Уровень пробит',
    parent_invalid: 'HTF-родитель невалиден', manual: 'Идея закрыта вручную',
    zones_exhausted: 'Зоны исчерпаны', discovery_pending: 'Зоны ещё определяются',
    origin_unresolved: 'Причинная связь не подтверждена', type_disabled: 'Тип зоны отключён',
  };

  function reasonText(reason) { return REASONS[reason] || reason || '—'; }

  function relevanceText(fact) {
    if (!fact) return 'Актуальность не оценивалась';
    return fact.reason === 'ok' ? 'Актуальна' : reasonText(fact.reason);
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
      '<p>Максимальная глубина теста: ' + Math.round((zone.max_test_depth || 0) * 100) + '%</p>',
      zone.relevance ? '<p>Актуальность: ' + relevanceText(zone.relevance) + '</p>' : '',
      zone.relevance && zone.relevance.excluded_at != null ? '<p>Исключена: ' + shown(zone.relevance.excluded_at, fmt.time, '—') + '</p>' : '',
      (zone.idea_links || []).map((i) => '<p>' + ideaLabel(i) + '<br>' + reasonText(i.entry_reason) + '</p>').join(''),
      zone.relevance ? ((zone.idea_links || []).length ? '' : '<p>Связь с HTF-идеей не подтверждена</p>')
        : '<p>Пригодность: ' + (adm.eligibility || 'not_evaluated') + '</p><p>' + reason + '</p>',
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
    const scenarioOpen = Array.isArray(layers.htf_ideas)
      ? layers.htf_ideas.some((i) => settings.historicalZones || i.state !== 'closed')
      : !!(layers.snapshot && layers.snapshot.scenario_open);
    updateIdeaOptions(layers, settings);
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
    const filtered = selectedZones(layers, settings);
    const hiddenByFilter = allZones.length - filtered.length;
    const outside = [];
    const visible = [];
    filtered.forEach((zone) => {
      if (inViewport(zone, view)) visible.push(zone);
      else outside.push(zone);
    });

    if (settings.zones) {
      const fills = {};
      const labels = [];
      const labelGroups = {};
      const decorate = (div, zone, adm, box, title, selected) => {
        const groupKey = [zone.type, zone.direction, zone.lifecycle, zone.candidate,
          box.left, box.right, box.top, box.bottom].join(':');
        if (labelGroups[groupKey]) {
          const group = labelGroups[groupKey];
          group.count += 1;
          group.name.textContent = group.caption + ' ×' + group.count;
          group.tag.title = title + ' · Совпадающих зон: ' + group.count;
          return;
        }
        const position = placeZoneLabel(box, labels, paneRight, height);
        if (!position) return;
        const tag = add('h1-zone-tag type-' + String(zone.type).toLowerCase()
          + (selected ? ' selected' : '') + (zone.lifecycle === 'ended' ? ' historical' : ''));
        tag.style.left = position.left + 'px';
        tag.style.top = position.top + 'px';
        tag.style.width = position.width + 'px';
        tag.title = title;
        if (position.bottom < box.top || position.top > box.bottom) {
          const boundary = position.bottom < box.top ? box.top : box.bottom;
          const labelEdge = position.bottom < box.top ? position.bottom : position.top;
          const leader = add('h1-zone-leader type-' + String(zone.type).toLowerCase());
          leader.style.left = (position.right - 12) + 'px';
          leader.style.top = Math.min(boundary, labelEdge) + 'px';
          leader.style.height = Math.abs(boundary - labelEdge) + 'px';
        }
        const name = doc.createElement('span');
        name.className = 'h1-zone-name';
        const isLevel = zonePlan(zone).shape === 'line';
        name.textContent = zonePlan(zone).label + (isLevel ? '' : zone.direction === 'bull' ? ' ↑' : ' ↓');
        const price = doc.createElement('span');
        price.className = 'h1-zone-price';
        price.textContent = (isLevel ? '' : '50% ') + fmtPrice(isLevel ? zone.lower : zone.mid != null ? zone.mid : (zone.lower + zone.upper) / 2);
        tag.appendChild(name); tag.appendChild(price);
        labelGroups[groupKey] = { tag, name, caption: name.textContent, count: 1 };
        tag.onclick = () => ctx.onZone && ctx.onZone(zone, adm);
        tag.onmouseenter = () => div.classList.add('highlighted');
        tag.onmouseleave = () => div.classList.remove('highlighted');
      };
      // A selected zone receives the first label lane.
      visible.sort((a, b) => Number(String(b.id) === String(ctx.selectedZoneId))
        - Number(String(a.id) === String(ctx.selectedZoneId)));
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
          const div = add('h1-liquidity ltf-level ltf-entry ltf-entry-' + String(zone.type).toLowerCase()
            + (zone.candidate ? ' status-candidate' : '') + (selected ? ' selected' : ''));
          div.style.top = y + 'px';
          div.style.left = x1 + 'px';
          div.style.width = Math.max(4, x2 - x1) + 'px';
          div.title = title;
          div.onclick = () => ctx.onZone && ctx.onZone(zone, adm);
          decorate(div, zone, adm, { left: x1, right: x2, top: y, bottom: y }, title, selected);
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
          div.style.height = Math.max(1, bottom - top) + 'px';
          div.style.left = x1 + 'px';
          div.style.width = Math.max(8, x2 - x1) + 'px';
          div.title = title;
          div.onclick = () => ctx.onZone && ctx.onZone(zone, adm);
          const color = zone.type === 'OB' ? 'rgba(76,141,255,0.075)' : 'rgba(38,166,154,0.075)';
          const fillKey = zone.candidate || zone.lifecycle === 'ended' ? 'rgba(138,148,166,0.025)' : color;
          if (!fills[fillKey]) fills[fillKey] = [];
          fills[fillKey].push({ left: x1, top, width: x2 - x1, height: bottom - top });
          const mid = yOf(zone.mid != null ? zone.mid : (zone.lower + zone.upper) / 2);
          if (mid != null && mid > top + 8 && mid < bottom - 8) {
            const line = doc.createElement('span');
            line.className = 'h1-zone-mid';
            line.style.top = (mid - top) + 'px';
            div.appendChild(line);
          }
          decorate(div, zone, adm, { left: x1, right: x2, top, bottom }, title, selected);
        }
      });
      // Fill each type once as a union: overlapping OBs never become opaque.
      if (Object.keys(fills).length) {
        const canvas = doc.createElement('canvas');
        canvas.className = 'h1-zone-fills';
        const ratio = root.devicePixelRatio || 1;
        canvas.width = Math.ceil(paneRight * ratio);
        canvas.height = Math.ceil(height * ratio);
        canvas.style.width = paneRight + 'px'; canvas.style.height = height + 'px';
        const paint = canvas.getContext('2d');
        if (paint) {
          paint.scale(ratio, ratio);
          Object.keys(fills).forEach((color) => {
            paint.beginPath();
            fills[color].forEach((r) => paint.rect(r.left, r.top, r.width, r.height));
            paint.fillStyle = color; paint.fill();
          });
        }
        overlay.appendChild(canvas);
      }
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

  function bindControls(onChange, onCloseIdea) {
    const doc = root.document;
    if (!doc) return;
    const settings = loadSettings();
    const close = doc.getElementById('h1-close-idea');
    if (close) close.onclick = async () => {
      if (!onCloseIdea || !close.dataset.ideaId) return;
      close.disabled = true;
      const error = doc.getElementById('h1-idea-error');
      if (error) error.textContent = '';
      try { await onCloseIdea(Number(close.dataset.ideaId)); }
      catch (e) { if (error) error.textContent = 'Не удалось закрыть идею: ' + e.message; }
      finally { close.disabled = false; }
    };
    const idea = doc.getElementById('h1-idea');
    if (idea) idea.onchange = () => {
      saveSettings({ ideaId: idea.value, eligibleOnly: true });
      const flag = doc.getElementById('h1-eligible-only');
      if (flag) flag.checked = true;
      if (onChange) onChange(loadSettings());
    };
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
    placeZoneLabel,
    mergeCaption,
    emptyZoneMessage,
    markerList,
    seriesMarkers,
    eventSource,
    eventCard,
    zoneCard,
    selectedZones,
    ideaLabel,
    reasonText,
    relevanceText,
    draw,
    bindControls,
  };
}(typeof window !== 'undefined' ? window : globalThis));
