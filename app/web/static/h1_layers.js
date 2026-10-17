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
    if (extra && extra.nearby_zones) params.set('nearby_zones', 'true');
    if (extra && extra.selected_zone_id != null) params.set('selected_zone_id', String(extra.selected_zone_id));
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

  function needsZoneRefresh(layers, price) {
    const selection = layers && layers.zone_selection;
    if (!selection || selection.mode !== 'current_pd' || price == null
        || selection.reference_price == null || price === selection.reference_price) return false;
    return (selection.price_from != null && price <= selection.price_from)
      || (selection.price_to != null && price >= selection.price_to);
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
    const type = String(zone.type || '').toUpperCase();
    const level = !!(zone.is_level || type === 'BSL' || type === 'SSL'
      || zone.lower === zone.upper);
    return {
      shape: level ? 'line' : 'rect',
      label: (type || 'Зона') + ' H1',
    };
  }

  // Labels occupy separate lanes; thin zones keep their true price height.
  function placeZoneLabel(box, occupied, paneRight, height, maxWidth = 180) {
    const width = Math.min(maxWidth, paneRight - box.left - 8);
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

  function structureLabel(el, options) {
    el.classList.add('structure-caption');
    el._structureLabel = options;
    el._structureTitle = el.title;
    return el;
  }

  // One bounded layout for D1 and H1. Move event captions along their own
  // levels, never move the levels themselves or build a tower of label lanes.
  function layoutStructureLabels(overlay, paneRight, height) {
    const bounds = overlay.getBoundingClientRect();
    const relative = (rect) => ({ left: rect.left - bounds.left, right: rect.right - bounds.left,
      top: rect.top - bounds.top, bottom: rect.bottom - bounds.top });
    const occupied = Array.from(overlay.querySelectorAll(
      '.d1-structure-status, .h1-zone-tag, .zone-group-badge, .h1-pd-label, .h1-origin-missing'
    )).filter(el => el.getClientRects().length).map(el => relative(el.getBoundingClientRect()));
    const labels = Array.from(overlay.querySelectorAll('.structure-caption'));
    labels.forEach(el => {
      el.classList.remove('structure-caption-dot');
      el.style.visibility = '';
      el.title = el._structureTitle;
    });
    // Read dimensions once before placing the batch; re-layout is idempotent.
    const rows = labels.map((el, index) => {
      const parent = el.offsetParent || overlay;
      const rect = relative(parent.getBoundingClientRect());
      return { el, ...el._structureLabel, index, width: el.offsetWidth, height: el.offsetHeight,
        parentX: rect.left + parent.clientLeft, parentY: rect.top + parent.clientTop };
    }).sort((a, b) => b.priority - a.priority || (b.time || 0) - (a.time || 0) || b.index - a.index);
    const free = box => box.left >= 2 && box.right <= paneRight - 2
      && box.top >= 2 && box.bottom <= height - 2
      && !occupied.some(p => box.left < p.right + 4 && box.right + 4 > p.left
        && box.top < p.bottom + 3 && box.bottom + 3 > p.top);
    const placed = [];
    const apply = (row, box) => {
      Object.assign(row.el.style, { left: (box.left - row.parentX) + 'px',
        top: (box.top - row.parentY) + 'px', bottom: 'auto', right: 'auto' });
      occupied.push(box);
      placed.push({ row, box });
    };
    rows.forEach(row => {
      const { el, x, y, side = 'above', width, height: h, kind } = row;
      // Current conditions stay readable even when their anchor is only a
      // few candles from the right edge; the dashed line keeps its origin.
      const min = kind === 'expected' ? 2 : Math.max(2, row.minX ?? 2);
      const max = Math.min(paneRight - width - 2, (row.maxX ?? paneRight - 2) - width);
      const clamp = value => Math.max(min, Math.min(max, value));
      const xs = kind === 'pivot' ? [clamp(x - width / 2)]
        : kind === 'expected' ? [max, clamp(x), min]
        : [clamp((min + max) / 2), max, min];
      const gap = row.gap || 5;
      const preferred = side === 'above' ? y - h - gap : y + gap;
      const ys = kind === 'pivot' ? [preferred, preferred + (side === 'above' ? -16 : 16)]
        : [preferred, side === 'above' ? y + 5 : y - h - 5];
      if (max >= min) {
        for (const top of ys) for (const left of xs) {
          const box = { left, right: left + width, top, bottom: top + h };
          if (free(box)) { apply(row, box); return; }
        }
      }
      if (kind === 'pivot') {
        const left = Math.max(2, Math.min(paneRight - 8, x - 3));
        const top = side === 'above' ? y - 10 : y + 4;
        const box = { left, right: left + 6, top, bottom: top + 6 };
        if (free(box)) {
          el.classList.add('structure-caption-dot');
          apply(row, box);
          return;
        }
        // A coincident higher-priority caption remains a hover target for
        // the omitted pivot, so its role/price are still discoverable.
        const other = placed.find(({ box }) => x >= box.left - 12 && x <= box.right + 12
          && y >= box.top - 20 && y <= box.bottom + 20);
        if (other && !other.row.el.title.includes(el.title)) other.row.el.title += '\n' + el.title;
      }
      el.style.visibility = 'hidden';
    });
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
    if ((zone.lifecycle === 'ended' || zone.display_until != null) && !settings.historicalZones) return false;
    return true;
  }

  function selectedZones(layers, settings) {
    const modern = Array.isArray(layers.htf_ideas);
    return (layers.detected_zones || []).filter((zone) => {
      if (!passesFilters(zone, settings)) return false;
      if (!settings.eligibleOnly && !settings.ideaId) return true;
      if (!modern) {
        const adm = admissionOf(layers, zone);
        return !!adm && (adm.eligibility === 'eligible' || adm.eligibility === 'eligible_provisional');
      }
      const links = (zone.idea_links || []).filter((link) =>
        !settings.ideaId || String(link.scenario_id) === String(settings.ideaId));
      if (settings.historicalZones) return links.length > 0;
      return !!(zone.relevance && zone.relevance.relevant && links.some((link) => link.state === 'active'));
    });
  }

  // Нижняя панель — участки входа текущего PD, а не весь архив разметки.
  function currentEntryZones(layers, settings, price) {
    const setup = layers && layers.setup;
    if (!setup || setup.history || setup.lower == null || setup.upper == null || setup.eq == null) return [];
    const bull = setup.direction === 'bull';
    const half = bull ? 'discount' : 'premium';
    const halfLow = bull ? setup.lower : setup.eq;
    const halfHigh = bull ? setup.eq : setup.upper;
    const zones = selectedZones(layers, settings);
    let regions = Array.isArray(setup.eligible_regions) && setup.eligible_regions.length
      ? setup.eligible_regions : setup.candidates;
    if (!Array.isArray(regions)) {
      if (Array.isArray(setup.eligible_regions)) regions = [];
      else {
        const origin = setup.origin_anchor && setup.origin_anchor.at;
        const start = setup.window_start ?? (origin == null ? Infinity : origin - 20 * 3600000);
        regions = zones.filter(z => z.formed_at >= start && z.direction === setup.direction)
          .map(z => ({ ...z, admitted: [Math.max(z.lower, halfLow), Math.min(z.upper, halfHigh)], reason: 'ok' }));
      }
    }
    const seen = new Set();
    const rows = [];
    regions.forEach(region => {
      if (!typeOn(settings, region) || !settings.zones || region.status === 'forming'
          || !['ok', 'eligible_provisional', undefined].includes(region.reason)) return;
      const admitted = region.admitted;
      if (!admitted || admitted[0] > admitted[1]) return;
      const same = z => String(z.type).toLowerCase() === String(region.type).toLowerCase() && z.direction === (region.direction || setup.direction)
        && z.lower === region.lower && z.upper === region.upper
        && (region.formed_at == null || z.formed_at === region.formed_at);
      const zone = zones.find(z => same(z) && z.lifecycle !== 'ended' && !z.candidate
        && (!z.relevance || z.relevance.relevant));
      // Текущий расчёт может ещё не иметь отдельной записи в архиве.
      const source = zone || region;
      if (source.lifecycle === 'ended' || source.display_until != null || source.lifecycle === 'candidate' || source.candidate
          || source.confirmed_at == null
          || source.relevance && !source.relevance.relevant
          || String(source.type).toLowerCase() === 'fvg' && source.max_test_depth >= 1) return;
      const key = [source.type, admitted[0], admitted[1]].join(':');
      if (seen.has(key)) return;
      seen.add(key);
      rows.push({ ...source, ...region, id: source.id ?? ('pd:' + setup.movement_id + ':' + key),
        direction: setup.direction, lower: region.lower, upper: region.upper,
        lifecycle: source.lifecycle || (region.confirmed_at != null ? 'active' : 'candidate'),
        admitted, pdHalf: half, entryReady: setup.setup_status === 'trade' });
    });
    const distance = row => price == null ? 0
      : Math.max(row.admitted[0] - price, price - row.admitted[1], 0);
    return rows.sort((a, b) => distance(a) - distance(b));
  }

  function currentFrame(candleCount, paneWidth) {
    const history = paneWidth >= 1000 ? 96 : paneWidth >= 600 ? 72 : 48;
    const last = Math.max(0, candleCount - 1);
    return { from: Math.max(0, last - history), to: last + Math.round(history * .65) };
  }

  function ideaLabel(idea) {
    return (idea.direction === 'bull' ? '↑ Покупки' : '↓ Продажи') + ' · '
      + (idea.structure_sync ? idea.structure_sync.status + ' · ' : '')
      + String(idea.parent_type || '').toUpperCase() + ' ' + idea.parent_timeframe
      + ' #' + idea.parent_zone_id + ' · BOS/SMS ' + new Date(idea.trigger_at).toLocaleString('ru-RU', {
        timeZone: 'Europe/Moscow', hour12: false,
      }) + ' МСК';
  }

  function updateIdeaOptions(layers, settings) {
    const select = root.document && root.document.getElementById('h1-idea');
    if (!select || !Array.isArray(layers.htf_ideas)) return;
    const choices = layers.htf_ideas.filter((i) => settings.historicalZones || i.state !== 'closed');
    const signature = JSON.stringify(choices.map((i) => [i.scenario_id, i.state, i.trigger_at, i.structure_sync?.status]));
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
    historically_worked: 'Отработана до активации (≥ 90% глубины)',
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

  // Прямоугольник в координатах панели. Край шкалы (зона целиком выше или
  // ниже, либо только кромка в поле допуска) — не зона на экране: её место
  // в счётчике «Вне экрана», иначе на широкой шкале остаются полоски.
  function clipZoneBox(y1, y2, height) {
    if (y1 == null || y2 == null || !(height > 0)) return null;
    const rawTop = Math.min(y1, y2);
    const rawBottom = Math.max(y1, y2);
    if (rawBottom <= 0 || rawTop >= height) return null;
    const top = Math.max(0, rawTop);
    const bottom = Math.min(height, rawBottom);
    if (bottom - top < 6 && (rawTop < 0 || rawBottom > height)) return null;
    return { top, height: Math.max(1, bottom - top) };
  }

  function zoneFragments(zone) {
    if (zonePlan(zone).shape === 'line' || zone.candidate) return [zone];
    const parts = [];
    let lower = zone.lower, upper = zone.upper;
    let slices = zone.tested_slices;
    // Compatibility with snapshots produced before the price-band history.
    if (!Array.isArray(slices)) {
      const depth = Math.max(0, Math.min(1, Number(zone.max_test_depth) || 0));
      const size = (upper - lower) * depth;
      slices = size > 0 && zone.first_test_at != null
        ? [{ lower: zone.direction === 'bull' ? upper - size : lower,
             upper: zone.direction === 'bull' ? upper : lower + size,
             tested_at: zone.first_test_at }] : [];
    }
    slices.forEach(slice => {
      const until = zone.display_until == null ? slice.tested_at : Math.min(zone.display_until, slice.tested_at);
      parts.push({ ...zone, lower: slice.lower, upper: slice.upper, display_until: until, tested: true });
      if (zone.direction === 'bull') upper = Math.min(upper, slice.lower);
      else lower = Math.max(lower, slice.upper);
    });
    if (upper > lower) parts.push({ ...zone, lower, upper, tested: false });
    return parts;
  }

  function inViewport(zone, view) {
    if (!view) return true;
    const from = zone.display_from || zone.formed_at || 0;
    const until = zone.display_until == null ? Infinity : zone.display_until;
    const timeOk = view.timeFrom == null || (from <= view.timeTo && until >= view.timeFrom);
    const priceOk = view.priceMin == null || (zone.upper >= view.priceMin && zone.lower <= view.priceMax);
    return timeOk && priceOk;
  }

  function markerList(layers, selectedId, settings = loadSettings()) {
    // У опоры, от которой нарисована линия пробоя (BOS/SMS), свинг-роль
    // дублируется подписью события и накладывается на неё текстом.
    const anchored = new Set();
    if (settings.breaks) {
      eventSource(layers).forEach((ev) => {
        if (ev && ev.break_level != null && ev.level_pivot_id != null) anchored.add(ev.level_pivot_id);
      });
    }
    const points = settings.points === 'hidden'
      ? []
      : ((layers && layers.pivot_markers) || []).filter((p) => !anchored.has(p.id));
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
      // Text lives in the DOM layout shared with BOS/SMS and D1 captions.
      text: '',
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
    const bounds = zone.lower === zone.upper
      ? shown(zone.lower, fmt.price, '—')
      : shown(zone.lower, fmt.price, '—') + '–' + shown(zone.upper, fmt.price, '—');
    const type = (zone.type || 'Зона') + ' H1';
    const state = (zone.lifecycle || '—') + (zone.level_state ? ' · ' + zone.level_state : '');
    const relevance = zone.relevance ? relevanceText(zone.relevance) : (adm.eligibility || reason);
    return '<p><b>' + type + '</b> · ' + (zone.direction === 'bull' ? '↑ покупки' : '↓ продажи') + '</p>'
      + '<dl class="h1-zone-facts"><dt>Границы</dt><dd>' + bounds + '</dd>'
      + '<dt>Состояние</dt><dd>' + state + '</dd>'
      + '<dt>Тест</dt><dd>' + Math.round((zone.max_test_depth || 0) * 100) + '%</dd>'
      + '<dt>Актуальность</dt><dd>' + relevance + '</dd></dl>';
  }

  function pdCaption(setup, fmtPrice) {
    if (!setup) return '';
    const fmt = fmtPrice || String;
    if (setup.lower == null || setup.upper == null) {
      return 'PD H1 текущего движения не построен: ' + (setup.reason || 'range_pending');
    }
    const history = setup.history ? ' · история' : '';
    const end = setup.endpoint || {};
    const origin = setup.origin_anchor || {};
    return 'PD H1 текущего движения ' + fmt(origin.price) + ' → ' + fmt(end.price)
      + ' · 50% ' + fmt(setup.eq) + ' · ' + (setup.pd_label || setup.range_status || '')
      + history;
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

    markerList(layers, ctx.selectedEventId, settings).forEach(p => {
      if (p.price == null || !(p.candle_open_time || p.pivot_at)) return;
      const x = xOf(p.candle_open_time || p.pivot_at), y = yOf(p.price);
      if (x == null || y == null || x < 0 || x > paneRight || y < 0 || y > height) return;
      const role = p.role || (p.kind === 'high' ? 'H' : 'L');
      const el = add('ltf-pivot role-' + role);
      el.textContent = role;
      el.title = 'H1 ' + role + ' · ' + fmtPrice(p.price) + ' · экстремум ' + fmtTime(p.pivot_at);
      structureLabel(el, { kind: 'pivot', x, y, side: p.kind === 'high' ? 'above' : 'below',
        priority: p.temporary ? 50 : 20, time: p.pivot_at, gap: 18 });
    });

    const setup = layers.setup;
    if (setup && (!setup.history || settings.historicalZones)
        && setup.lower != null && setup.upper != null && typeof yOf === 'function') {
      const originAt = setup.origin_anchor && setup.origin_anchor.at;
      let x1 = originAt != null ? xOf(originAt) : 0;
      if (x1 == null || x1 < 0) x1 = 0;
      const provisional = setup.range_status !== 'confirmed';
      const klass = 'h1-pd-line' + (provisional ? ' provisional' : ' confirmed');
      if (paneRight > x1 && setup.eq != null) {
        [[setup.upper, setup.eq, 'premium'], [setup.eq, setup.lower, 'discount']]
          .forEach(([hi, lo, half]) => {
            const box = clipZoneBox(yOf(hi), yOf(lo), height);
            if (!box) return;
            const band = add('h1-pd-band ' + half);
            band.style.left = x1 + 'px';
            band.style.top = box.top + 'px';
            band.style.width = (paneRight - x1) + 'px';
            band.style.height = box.height + 'px';
            band.title = 'Локальный PD · ' + half + ' · ' + fmtPrice(lo) + '–' + fmtPrice(hi);
          });
      }
      const rows = [
        [setup.upper, 'Локальный PD · Premium', false],
        [setup.eq, 'Локальный EQ · 50%', true],
        [setup.lower, 'Локальный PD · Discount', false],
      ];
      rows.forEach(([price, text, mid]) => {
        const y = yOf(price);
        if (y == null || y < -8 || y > height + 8 || paneRight <= x1) return;
        const line = add(klass + (mid ? ' mid' : ''));
        line.style.top = y + 'px';
        line.style.left = x1 + 'px';
        line.style.width = Math.max(4, paneRight - x1) + 'px';
        line.title = (setup.label || 'PD H1') + ' · ' + text + ' ' + fmtPrice(price)
          + (provisional ? ' · пунктир, предварительный PD' : ' · сплошная линия');
        const span = doc.createElement('span');
        span.className = 'h1-pd-label';
        span.textContent = text + ' · ' + fmtPrice(price)
          + (provisional && !mid && price === setup.upper ? ' · предварительный' : '');
        line.appendChild(span);
      });
    }

    const main = layers.main_structure;
    if (main && main.lower != null && main.upper != null && typeof yOf === 'function') {
      let left = xOf(main.origin_at);
      if (left == null || left < 0) left = 0;
      const bull = main.direction === 'bull';
      [[main.upper, bull ? 'HH' : 'LH'], [main.eq, '50%'], [main.lower, bull ? 'HL' : 'LL']]
        .forEach(([price, role]) => {
          const y = yOf(price);
          if (y == null || y < -8 || y > height + 8 || paneRight <= left) return;
          const line = add('h1-pd-line main ' + (role === '50%' ? 'mid' : 'confirmed'));
          line.style.top = y + 'px';
          line.style.left = left + 'px';
          line.style.width = Math.max(4, paneRight - left) + 'px';
          const label = 'Основной PD · ' + role + ' · ' + fmtPrice(price);
          line.title = label;
          const span = doc.createElement('span');
          span.className = 'h1-pd-label';
          span.textContent = label;
          line.appendChild(span);
        });
    }

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
          group.name.textContent = group.caption;
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
          if (end == null) return;
          x2 = Math.min(paneRight, end);
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
            + ' dir-' + (zone.direction === 'bear' ? 'bear' : 'bull')
            + (zone.candidate ? ' status-candidate' : '') + (selected ? ' selected' : ''));
          div.style.top = y + 'px';
          div.style.left = x1 + 'px';
          div.style.width = Math.max(4, x2 - x1) + 'px';
          div.title = title;
          div.onclick = () => ctx.onZone && ctx.onZone(zone, adm);
          decorate(div, zone, adm, { left: x1, right: x2, top: y, bottom: y }, title, selected);
        } else {
          const fragments = zoneFragments(zone);
          let labelBox = null, labelDiv = null;
          fragments.forEach(fragment => {
            let right = x2;
            if (fragment.display_until != null) {
              const end = xOf(fragment.display_until);
              // A consumed band whose end is left of the viewport is invisible.
              if (end == null && ctx.view && fragment.display_until < ctx.view.timeFrom) return;
              if (end != null) right = Math.min(right, end);
            }
            if (right <= x1) return;
            const box = clipZoneBox(yOf(fragment.upper), yOf(fragment.lower), height);
            if (!box) {
              return;
            }
            const top = box.top;
            const bottom = top + box.height;
            const div = add('h1-zone zone-rect z-' + String(zone.type).toLowerCase()
              + ' dir-' + (zone.direction === 'bear' ? 'bear' : 'bull')
              + ' tf-' + (['D1', 'W1'].includes(zone.timeframe) ? 'htf' : 'ltf')
              + (zone.candidate ? ' status-candidate' : '')
              + (zone.lifecycle === 'ended' ? ' status-completed' : '')
              + (selected ? ' selected' : ''));
            div.style.top = top + 'px';
            div.style.height = box.height + 'px';
            div.style.left = x1 + 'px';
            div.style.width = (right - x1) + 'px';
            div.dataset.tested = String(!!fragment.tested);
            div.title = title;
            div.onclick = () => ctx.onZone && ctx.onZone(zone, adm);
            // Заливку пишет один канвас на цвет: пересечения одного типа не
            // темнят дважды. Div остаётся прозрачным и держит только рамку.
            const rgb = (name, fallback) =>
              (String(getComputedStyle(doc.documentElement).getPropertyValue(name).trim()) || fallback);
            const bull = rgb('--zone-bull-rgb', '22, 146, 100');
            const bear = rgb('--zone-bear-rgb', '214, 69, 77');
            const alpha = String(zone.type || '').toLowerCase() === 'fvg' ? 0.22 : 0.28;
            // BSL/SSL: цвет инвертирован относительно направления движения
            const colorDir = (['bsl', 'ssl'].includes(String(zone.type).toLowerCase()))
              ? (zone.direction === 'bear' ? bull : bear)
              : (zone.direction === 'bear' ? bear : bull);
            const color = `rgba(${colorDir},${alpha})`;
            const fillKey = zone.candidate || zone.lifecycle === 'ended' ? 'rgba(138,148,166,0.025)' : color;
            if (!fills[fillKey]) fills[fillKey] = [];
            fills[fillKey].push({
              left: x1, top, width: right - x1, height: bottom - top,
            });
            const mid = yOf(zone.mid != null ? zone.mid : (zone.lower + zone.upper) / 2);
            if (mid != null && mid > top + 8 && mid < bottom - 8) {
              const line = doc.createElement('span');
              line.className = 'h1-zone-mid';
              line.style.top = (mid - top) + 'px';
              div.appendChild(line);
            }
            labelBox = { left: x1, right, top, bottom };
            labelDiv = div;
          });
          if (labelBox) decorate(labelDiv, zone, adm, labelBox, title, selected);
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
        label.title = div.title;
        structureLabel(label, { kind: 'event', x: x1, y, minX: x1, maxX: x2,
          priority: 30, time: plan.endMs, side: ev.direction === 'bull' ? 'above' : 'below' });
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
            item.label.title = items.map(row => row.div.title).join('\n');
            item.label._structureTitle = item.label.title;
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
        span.title = div.title;
        structureLabel(span, { kind: 'expected', x: x1, y, minX: x1, maxX: paneRight - 4,
          priority: 40, side: row.direction === 'bull' ? 'above' : 'below' });
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
      const selection = layers.zone_selection;
      if (selection && selection.mode === 'current_pd') {
        ctx.statusEl.classList.remove('hidden');
        const cap = doc.createElement('span');
        cap.className = 'h1-zone-selection';
        cap.textContent = selection.pd_available
          ? 'Зоны внутри PD + 5 ближайших выше и ниже · показано ' + selection.returned
          : 'PD ещё не сформирован · показаны обязательные актуальные зоны';
        ctx.statusEl.appendChild(cap);
      }
      const setupCap = layers.setup;
      if (setupCap && (setupCap.lower != null || setupCap.range_status)) {
        ctx.statusEl.classList.remove('hidden');
        const cap = doc.createElement('span');
        cap.className = 'h1-reversal-caption';
        cap.textContent = pdCaption(setupCap, fmtPrice);
        ctx.statusEl.appendChild(cap);
      }
      const rev = layers.reversal;
      if (!setupCap && rev && rev.pd) {
        ctx.statusEl.classList.remove('hidden');
        const cap = doc.createElement('span');
        cap.className = 'h1-reversal-caption';
        const allowed = (rev.zones || []).filter((z) => z.allowed);
        cap.textContent = 'PD LL ' + fmtPrice(rev.pd.L) + ' → high ' + fmtPrice(rev.pd.H)
          + ' · 50% ' + fmtPrice(rev.pd.EQ) + ' · ' + (rev.pd.label || rev.pd.status)
          + (allowed.length ? ' · допущенных зон ' + allowed.length : '');
        ctx.statusEl.appendChild(cap);
      }
    }
    if (ctx.offscreenEl) {
      const count = outside.length;
      ctx.offscreenEl.classList.toggle('hidden', count === 0);
      ctx.offscreenEl.textContent = 'Вне видимого диапазона: ' + count + ' ' + zonesWord(count);
      ctx.offscreenEl.onclick = () => {
        if (outside[0] && ctx.onShowZone) ctx.onShowZone(outside[0]);
      };
    }
    if (!ctx.deferLayout) layoutStructureLabels(overlay, paneRight, height);
  }

  function zonesWord(n) {
    const n100 = n % 100;
    const n10 = n % 10;
    if (n10 === 1 && n100 !== 11) return 'зона';
    if (n10 >= 2 && n10 <= 4 && (n100 < 12 || n100 > 14)) return 'зоны';
    return 'зон';
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
    needsZoneRefresh,
    caption,
    breakPlan,
    zonePlan,
    clipZoneBox,
    placeZoneLabel,
    mergeCaption,
    structureLabel,
    layoutStructureLabels,
    emptyZoneMessage,
    markerList,
    seriesMarkers,
    eventSource,
    eventCard,
    zoneCard,
    pdCaption,
    selectedZones,
    zoneFragments,
    currentEntryZones,
    currentFrame,
    ideaLabel,
    reasonText,
    relevanceText,
    draw,
    bindControls,
  };
}(typeof window !== 'undefined' ? window : globalThis));
