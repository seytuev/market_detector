/* Проекция графика «Альткоины»: нормализация, отбор, группировка.
   Чистые функции. Не меняют сетап, события БД и уведомления.
   Контракт: docs/Altcoins_Chart_Declutter_Spec_RU.md, CH-01…CH-08.
   D1: граница закрытия = открытие + 86 400 000 мс (close_boundary_ms). */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.AltChart = api;
})(typeof window !== 'undefined' ? window : this, function () {
  'use strict';

  const DAY_MS = 86400000;
  const WINDOW_MS = 180 * DAY_MS;
  const EXTRA_BUDGET = 8;
  const HISTORY_CLUSTER_BUDGET = 24;

  const STRUCT_LABEL = {
    BOS: 'BOS', SMS: 'SMS', SSL: 'SSL',
    BOS_REV: 'BOS обр.', SMS_REV: 'SMS обр.',
  };
  const POS = {
    BOS: 'belowBar', SMS: 'belowBar', SSL: 'aboveBar',
    BOS_REV: 'aboveBar', SMS_REV: 'aboveBar',
    entry_a: 'belowBar', entry_b: 'belowBar',
    breakout: 'aboveBar', retest: 'aboveBar',
    target_hit: 'aboveBar',
    cancelled: 'aboveBar', expired_no_retest: 'aboveBar',
    targets_completed: 'aboveBar',
  };
  const KEY_ROLES = ['confirmation', 'entry', 'breakout', 'retest', 'terminal', 'target'];
  const EXTRA_STRUCT = { BOS: 1, SMS: 1, SSL: 1 };
  const REVERSE = { BOS_REV: 1, SMS_REV: 1 };

  function closeBoundaryMs(openTime) {
    return openTime + DAY_MS;
  }

  function candleFromSourceId(sourceEventId) {
    if (!sourceEventId || sourceEventId.indexOf(':') < 0) return null;
    const tail = sourceEventId.slice(sourceEventId.lastIndexOf(':') + 1);
    const value = Number(tail);
    if (!Number.isFinite(value) || value < 1000000000000) return null;
    return value;
  }

  function candleOfLifecycle(event) {
    const payload = event.payload || {};
    if (payload.candle_open_time != null) return payload.candle_open_time;
    const fromId = candleFromSourceId(event.source_event_id);
    if (fromId != null) return fromId;
    if (event.event_time_ms) return event.event_time_ms - DAY_MS;
    return null;
  }

  function chartTypeOf(eventType) {
    const map = {
      bos_confirmed: 'BOS', sms_confirmed: 'SMS', ssl_taken: 'SSL',
      entry_a: 'entry_a', entry_b: 'entry_b',
      breakout: 'breakout', retest: 'retest', target_hit: 'target_hit',
      cancelled: 'cancelled', expired_no_retest: 'expired_no_retest',
      targets_completed: 'targets_completed',
    };
    return map[eventType] || eventType;
  }

  function shortLabel(type, event) {
    if (STRUCT_LABEL[type]) return STRUCT_LABEL[type];
    if (type === 'entry_a') return 'вход A';
    if (type === 'entry_b') return 'вход B';
    if (type === 'breakout') return 'выход';
    if (type === 'retest') return 'ретест';
    if (type === 'target_hit') {
      const levels = event && event.payload && event.payload.levels;
      if (Array.isArray(levels) && levels.length) return 'TP ' + levels.join(',');
      return 'TP';
    }
    if (type === 'cancelled') return 'отмена';
    if (type === 'expired_no_retest') return 'срок';
    if (type === 'targets_completed') return 'цели';
    return type;
  }

  function rolesFor(eventType, payload) {
    if (eventType === 'entry_a' || eventType === 'entry_b') return ['entry'];
    if (eventType === 'breakout') return ['breakout'];
    if (eventType === 'target_hit') return ['target'];
    if (eventType === 'cancelled' || eventType === 'expired_no_retest' || eventType === 'targets_completed') {
      return ['terminal'];
    }
    if (eventType === 'retest') return (payload && payload.journal) ? ['retest_journal'] : ['retest_candidate'];
    return [];
  }

  function blankEvent(fields) {
    return {
      key: fields.key,
      source_ids: fields.source_ids.slice(),
      setup_id: fields.setup_id,
      source_id: fields.source_id,
      type: fields.type,
      candle_open_time_ms: fields.candle_open_time_ms,
      available_at_ms: fields.available_at_ms,
      level_price: fields.level_price == null ? null : fields.level_price,
      roles: fields.roles.slice(),
      importance: 'secondary',
      label: fields.label,
      position: fields.position || 'aboveBar',
      details: fields.details || {},
    };
  }

  function addFact(byKey, events, fact) {
    const prev = byKey.get(fact.key);
    if (!prev) {
      byKey.set(fact.key, fact);
      events.push(fact);
      return fact;
    }
    fact.source_ids.forEach((id) => {
      if (prev.source_ids.indexOf(id) < 0) prev.source_ids.push(id);
    });
    fact.roles.forEach((role) => {
      if (prev.roles.indexOf(role) < 0) prev.roles.push(role);
    });
    if (prev.level_price == null && fact.level_price != null) prev.level_price = fact.level_price;
    if (prev.candle_open_time_ms == null) prev.candle_open_time_ms = fact.candle_open_time_ms;
    if (prev.available_at_ms == null) prev.available_at_ms = fact.available_at_ms;
    prev.details = Object.assign({}, fact.details, prev.details);
    return prev;
  }

  function markImportance(events) {
    events.forEach((event) => {
      event.importance = event.roles.some((role) => KEY_ROLES.indexOf(role) >= 0)
        ? 'key' : 'secondary';
    });
  }

  function normalizeDetail(detail) {
    const events = [];
    const byKey = new Map();
    if (!detail) {
      return { events, asOfMs: null, setupId: null, history: null };
    }
    const setupId = detail.setup_id == null ? null : detail.setup_id;

    (detail.structure_events || []).forEach((row) => {
      const anchors = row.anchors || {};
      const sourceEventId = row.source_event_id || null;
      addFact(byKey, events, blankEvent({
        key: sourceEventId ? 'fact:' + sourceEventId : 'struct:' + row.id,
        source_ids: ['structure:' + row.id],
        setup_id: setupId,
        source_id: row.id,
        type: row.kind,
        candle_open_time_ms: row.candle_open_time,
        available_at_ms: row.candle_open_time == null ? null : closeBoundaryMs(row.candle_open_time),
        level_price: row.level_price,
        roles: ['structure'],
        label: STRUCT_LABEL[row.kind] || row.kind,
        position: POS[row.kind] || 'aboveBar',
        details: {
          historical: !!(row.historical || anchors.historical),
          close_price: row.close_price,
          structure_id: row.id,
          source_event_id: sourceEventId,
        },
      }));
    });

    (detail.events || []).forEach((row) => {
      const sourceEventId = row.source_event_id || null;
      const candle = candleOfLifecycle(row);
      const type = chartTypeOf(row.event_type);
      addFact(byKey, events, blankEvent({
        key: sourceEventId ? 'fact:' + sourceEventId : 'event:' + row.id,
        source_ids: ['event:' + row.id],
        setup_id: setupId,
        source_id: row.id,
        type,
        candle_open_time_ms: candle,
        available_at_ms: row.event_time_ms,
        level_price: row.payload && row.payload.level_price != null ? row.payload.level_price : null,
        roles: rolesFor(row.event_type, row.payload),
        label: shortLabel(type, row),
        position: POS[type] || 'aboveBar',
        details: {
          event_id: row.id,
          event_type: row.event_type,
          payload: row.payload || {},
          source_event_id: sourceEventId,
          label_ru: row.label_ru || null,
        },
      }));
    });

    const confirmation = detail.confirmation;
    if (confirmation) {
      const sourceEventId = confirmation.source_event_id || null;
      let fact = sourceEventId ? byKey.get('fact:' + sourceEventId) : null;
      if (!fact && confirmation.event_id != null) {
        fact = events.find((event) => event.source_ids.indexOf('event:' + confirmation.event_id) >= 0);
      }
      if (!fact) {
        const candle = confirmation.candle_open_time != null
          ? confirmation.candle_open_time
          : (confirmation.event_time_ms ? confirmation.event_time_ms - DAY_MS : null);
        fact = addFact(byKey, events, blankEvent({
          key: sourceEventId ? 'fact:' + sourceEventId : 'confirm:' + confirmation.event_id,
          source_ids: confirmation.event_id != null ? ['event:' + confirmation.event_id] : [],
          setup_id: setupId,
          source_id: confirmation.event_id,
          type: chartTypeOf(confirmation.event_type),
          candle_open_time_ms: candle,
          available_at_ms: confirmation.event_time_ms,
          level_price: null,
          roles: [],
          label: shortLabel(chartTypeOf(confirmation.event_type), confirmation),
          position: POS[chartTypeOf(confirmation.event_type)] || 'belowBar',
          details: { event_type: confirmation.event_type, source_event_id: sourceEventId },
        }));
      }
      if (fact.roles.indexOf('confirmation') < 0) fact.roles.push('confirmation');
      const link = confirmation.structure_link || { status: 'unknown' };
      fact.details.structure_link = link.status || 'unknown';
      fact.details.confirmation_event_id = confirmation.event_id;
    }

    (detail.entries || []).forEach((row) => {
      const sourceEventId = 'entry_' + String(row.kind || '').toLowerCase() + ':' + setupId;
      let fact = byKey.get('fact:' + sourceEventId);
      const candle = row.event_time_ms ? row.event_time_ms - DAY_MS : null;
      if (!fact) {
        fact = addFact(byKey, events, blankEvent({
          key: 'fact:' + sourceEventId,
          source_ids: ['entry:' + row.id],
          setup_id: setupId,
          source_id: row.id,
          type: row.kind === 'B' ? 'entry_b' : 'entry_a',
          candle_open_time_ms: candle,
          available_at_ms: row.event_time_ms,
          level_price: row.price,
          roles: ['entry'],
          label: row.kind === 'B' ? 'вход B' : 'вход A',
          position: 'belowBar',
          details: {},
        }));
      } else if (fact.source_ids.indexOf('entry:' + row.id) < 0) {
        fact.source_ids.push('entry:' + row.id);
      }
      if (fact.roles.indexOf('entry') < 0) fact.roles.push('entry');
      fact.details.entry_kind = row.kind;
      fact.details.entry_price = row.price;
      fact.details.entry_zone = row.zone || null;
      fact.details.entry_id = row.id;
    });

    if (detail.breakout && detail.breakout.closed_at != null) {
      const open = detail.breakout.closed_at - DAY_MS;
      const sourceEventId = 'breakout:' + setupId + ':' + open;
      let fact = byKey.get('fact:' + sourceEventId);
      if (!fact) {
        fact = events.find((event) => event.details && event.details.event_type === 'breakout');
      }
      if (!fact) {
        fact = addFact(byKey, events, blankEvent({
          key: 'fact:' + sourceEventId,
          source_ids: ['breakout'],
          setup_id: setupId,
          source_id: sourceEventId,
          type: 'breakout',
          candle_open_time_ms: open,
          available_at_ms: detail.breakout.closed_at,
          level_price: detail.breakout.close,
          roles: ['breakout'],
          label: 'выход',
          position: 'aboveBar',
          details: { source_event_id: sourceEventId },
        }));
      }
      if (fact.roles.indexOf('breakout') < 0) fact.roles.push('breakout');
    }

    const retests = events.filter((event) => event.roles.indexOf('retest_candidate') >= 0);
    retests.sort((a, b) => (a.available_at_ms || 0) - (b.available_at_ms || 0) || tieId(a) - tieId(b));
    retests.forEach((event, index) => {
      event.roles = event.roles.filter((role) => role !== 'retest_candidate');
      event.roles.push(index === 0 ? 'retest' : 'retest_journal');
    });

    markImportance(events);
    return {
      events,
      asOfMs: detail.as_of_ms == null ? null : detail.as_of_ms,
      setupId,
      history: detail.candle_history || null,
    };
  }

  function tieId(event) {
    const numeric = Number(event.source_id);
    if (Number.isFinite(numeric)) return numeric;
    return 0;
  }

  function allowedEvent(event, view) {
    if (view.setupId != null && event.setup_id != null && event.setup_id !== view.setupId) return false;
    if (view.asOfMs == null || event.available_at_ms == null) return false;
    return event.available_at_ms <= view.asOfMs;
  }

  function inWindow(event, asOfMs) {
    return event.available_at_ms >= asOfMs - WINDOW_MS && event.available_at_ms <= asOfMs;
  }

  function inVisible(event, view) {
    if (view.visibleFromMs == null || view.visibleToMs == null) return true;
    const time = event.candle_open_time_ms;
    if (time == null) return false;
    return time >= view.visibleFromMs && time <= view.visibleToMs;
  }

  function latestOfType(pool) {
    if (!pool.length) return null;
    return pool.slice().sort((a, b) =>
      (b.available_at_ms || 0) - (a.available_at_ms || 0) || tieId(b) - tieId(a)
    )[0];
  }

  function collapseTargets(events) {
    const out = [];
    const byCandle = new Map();
    events.forEach((event) => {
      if (event.type !== 'target_hit' || event.candle_open_time_ms == null) {
        out.push(event);
        return;
      }
      const prev = byCandle.get(event.candle_open_time_ms);
      if (!prev) {
        const copy = Object.assign({}, event, {
          source_ids: event.source_ids.slice(),
          roles: event.roles.slice(),
          details: Object.assign({}, event.details, { parts: [event.details] }),
        });
        byCandle.set(event.candle_open_time_ms, copy);
        out.push(copy);
        return;
      }
      event.source_ids.forEach((id) => {
        if (prev.source_ids.indexOf(id) < 0) prev.source_ids.push(id);
      });
      prev.details.parts.push(event.details);
    });
    return out;
  }

  function selectChartEvents(events, view) {
    const mode = view.mode || 'setup';
    const freshnessKnown = view.asOfMs != null;
    const allowed = (events || []).filter((event) => allowedEvent(event, view));
    const structure = allowed.filter((event) => EXTRA_STRUCT[event.type]);
    const latestStructure = latestOfType(structure);
    const fresh = structure.filter((event) => freshnessKnown && inWindow(event, view.asOfMs));
    const freshNote = mode === 'setup' && freshnessKnown && !fresh.length
      ? 'Свежих структурных событий за 180 дней нет'
      : null;

    if (!freshnessKnown || mode === 'none') {
      return {
        markers: [],
        offscreenKeys: [],
        freshNote: null,
        latestStructureMs: latestStructure ? latestStructure.available_at_ms : null,
        freshnessKnown,
      };
    }

    if (mode === 'history') {
      if (view.visibleFromMs == null || view.visibleToMs == null) {
        return {
          markers: [],
          offscreenKeys: [],
          freshNote: null,
          latestStructureMs: latestStructure ? latestStructure.available_at_ms : null,
          freshnessKnown,
        };
      }
      const types = view.historyTypes;
      const markers = collapseTargets(allowed.filter((event) => {
        if (!view.includeReverse && REVERSE[event.type]) return false;
        if (types && types.indexOf(event.type) < 0) return false;
        return inVisible(event, view);
      }));
      return {
        markers,
        offscreenKeys: [],
        freshNote: null,
        latestStructureMs: latestStructure ? latestStructure.available_at_ms : null,
        freshnessKnown,
      };
    }

    const keys = allowed.filter((event) => event.importance === 'key');
    const types = ['BOS', 'SMS', 'SSL'];
    if (view.includeReverse) types.push('BOS_REV', 'SMS_REV');
    const extras = [];
    types.forEach((type) => {
      const pool = allowed.filter((event) =>
        event.type === type && event.importance !== 'key' && inWindow(event, view.asOfMs)
      );
      const latest = latestOfType(pool);
      if (latest) extras.push(latest);
    });
    let extraVisible = extras.filter((event) => inVisible(event, view));
    extraVisible.sort((a, b) =>
      (b.available_at_ms || 0) - (a.available_at_ms || 0) || tieId(b) - tieId(a)
    );
    if (extraVisible.length > EXTRA_BUDGET) extraVisible = extraVisible.slice(0, EXTRA_BUDGET);

    const selected = view.selectedEventKey
      ? allowed.find((event) => event.key === view.selectedEventKey)
      : null;
    const chosen = [];
    const seen = new Set();
    keys.concat(extraVisible).concat(selected ? [selected] : []).forEach((event) => {
      if (!event || seen.has(event.key)) return;
      seen.add(event.key);
      chosen.push(event);
    });
    const collapsed = collapseTargets(chosen);
    return {
      markers: collapsed.filter((event) => inVisible(event, view)),
      offscreenKeys: collapsed.filter((event) =>
        !inVisible(event, view) && (event.importance === 'key' || event.key === view.selectedEventKey)
      ),
      freshNote,
      latestStructureMs: latestStructure ? latestStructure.available_at_ms : null,
      freshnessKnown,
    };
  }

  function eventPriority(event, selectedKey) {
    if (selectedKey && event.key === selectedKey) return 100;
    const roles = event.roles || [];
    if (roles.indexOf('terminal') >= 0) return 80;
    if (roles.indexOf('entry') >= 0 || roles.indexOf('retest') >= 0 || roles.indexOf('breakout') >= 0) return 60;
    if (roles.indexOf('confirmation') >= 0) return 40;
    if (roles.indexOf('target') >= 0) return 20;
    return 0;
  }

  function primaryOf(packed, selectedKey) {
    return packed.slice().sort((a, b) => {
      const diff = eventPriority(b.event, selectedKey) - eventPriority(a.event, selectedKey);
      if (diff) return diff;
      return (a.event.available_at_ms || 0) - (b.event.available_at_ms || 0) || tieId(a.event) - tieId(b.event);
    })[0];
  }

  function fullLabel(packed, selectedKey) {
    const primary = primaryOf(packed, selectedKey);
    const extra = packed.length - 1;
    return extra > 0 ? primary.event.label + ' +' + extra : primary.event.label;
  }

  function hasKey(packed) {
    return packed.some((item) => item.event.importance === 'key');
  }

  function groupPriority(group, selectedKey) {
    return Math.max.apply(null, group.packed.map((item) => eventPriority(item.event, selectedKey)));
  }

  function paintLabel(group, measure, badgeWidth, selectedKey) {
    group.labelMode = 'full';
    group.label = fullLabel(group.packed, selectedKey);
    group.width = measure(group.label);
    group.x = primaryOf(group.packed, selectedKey).x;
    if (group.labelMode === 'badge') group.width = Math.max(badgeWidth, measure(group.label));
  }

  function groupMarkers(items, options) {
    const opts = options || {};
    const gap = opts.gap == null ? 8 : opts.gap;
    const extraBudget = opts.extraBudget == null ? HISTORY_CLUSTER_BUDGET : opts.extraBudget;
    const badgeWidth = opts.badgeWidth == null ? 16 : opts.badgeWidth;
    const measure = opts.measure || ((text) => String(text).length * 7);
    const selectedKey = opts.selectedKey || null;
    const buckets = { aboveBar: [], belowBar: [] };

    (items || []).forEach((item) => {
      const event = item.event || item;
      if (item.x == null || event.candle_open_time_ms == null) return;
      const position = item.position || event.position || 'aboveBar';
      const list = position === 'belowBar' ? buckets.belowBar : buckets.aboveBar;
      let group = null;
      for (let i = 0; i < list.length; i += 1) {
        if (list[i].candle === event.candle_open_time_ms) { group = list[i]; break; }
      }
      if (!group) {
        group = { candle: event.candle_open_time_ms, position: position === 'belowBar' ? 'belowBar' : 'aboveBar', packed: [] };
        list.push(group);
      }
      group.packed.push({ event, x: item.x });
    });

    const result = [];
    ['aboveBar', 'belowBar'].forEach((position) => {
      let groups = buckets[position];
      groups.forEach((group) => paintLabel(group, measure, badgeWidth, selectedKey));
      groups = resolveCollisions(groups, measure, gap, badgeWidth, selectedKey);
      groups = mergeExtraBudget(groups, extraBudget, measure, badgeWidth, selectedKey);
      groups.forEach((group) => {
        const times = group.packed.map((item) => item.event.candle_open_time_ms);
        result.push({
          position: group.position,
          x: group.x,
          label: group.label,
          labelMode: group.labelMode,
          hasKey: hasKey(group.packed),
          eventKeys: group.packed.map((item) => item.event.key),
          fromMs: Math.min.apply(null, times),
          toMs: Math.max.apply(null, times),
          count: group.packed.length,
          width: group.width,
        });
      });
    });
    return result;
  }

  function overlaps(prev, cur, gap) {
    const prevRight = prev.x + prev.width / 2;
    const curLeft = cur.x - cur.width / 2;
    return curLeft < prevRight + gap;
  }

  function absorb(winner, loser, measure, badgeWidth, selectedKey) {
    loser.packed.forEach((item) => winner.packed.push(item));
    paintLabel(winner, measure, badgeWidth, selectedKey);
  }

  function resolveCollisions(groups, measure, gap, badgeWidth, selectedKey) {
    let guard = 0;
    let changed = true;
    while (changed && groups.length > 1 && guard < 5000) {
      guard += 1;
      changed = false;
      groups.sort((a, b) => a.x - b.x || a.candle - b.candle);
      for (let i = 1; i < groups.length; i += 1) {
        if (!overlaps(groups[i - 1], groups[i], gap)) continue;
        const prev = groups[i - 1];
        const cur = groups[i];
        const loser = groupPriority(prev, selectedKey) >= groupPriority(cur, selectedKey) ? cur : prev;
        if (loser.labelMode !== 'badge') {
          loser.labelMode = 'badge';
          loser.label = String(loser.packed.length);
          loser.width = Math.max(badgeWidth, measure(loser.label));
          changed = true;
          break;
        }
        const winner = loser === cur ? prev : cur;
        absorb(winner, loser, measure, badgeWidth, selectedKey);
        groups.splice(groups.indexOf(loser), 1);
        changed = true;
        break;
      }
    }
    return groups;
  }

  function mergeExtraBudget(groups, budget, measure, badgeWidth, selectedKey) {
    let guard = 0;
    while (guard < 5000) {
      guard += 1;
      groups.sort((a, b) => a.x - b.x || a.candle - b.candle);
      const extraCount = groups.filter((group) => !hasKey(group.packed)).length;
      if (extraCount <= budget) break;
      let best = null;
      for (let i = 1; i < groups.length; i += 1) {
        const left = groups[i - 1];
        const right = groups[i];
        if (hasKey(left.packed) && hasKey(right.packed)) continue;
        const distance = Math.abs(right.x - left.x);
        if (!best || distance < best.distance - 1e-9 ||
            (Math.abs(distance - best.distance) <= 1e-9 && left.x < best.leftX)) {
          best = { index: i, distance, leftX: left.x };
        }
      }
      if (!best) break;
      const left = groups[best.index - 1];
      const right = groups[best.index];
      const loser = groupPriority(left, selectedKey) >= groupPriority(right, selectedKey) ? right : left;
      const winner = loser === right ? left : right;
      absorb(winner, loser, measure, badgeWidth, selectedKey);
      groups.splice(groups.indexOf(loser), 1);
    }
    return groups;
  }

  function initialTimeRange(candles, asOfMs) {
    const closed = (candles || []).filter((candle) =>
      asOfMs == null || candle.open_time + DAY_MS <= asOfMs
    );
    if (!closed.length) return null;
    const slice = closed.slice(-180);
    return { fromMs: slice[0].open_time, toMs: slice[slice.length - 1].open_time };
  }

  function rangeTimeRange(candles, anchorOpenMs) {
    if (!candles || !candles.length) {
      return { missing: true, reason: 'Нет свечей' };
    }
    const end = candles[candles.length - 1].open_time;
    if (anchorOpenMs == null) {
      return {
        fromMs: candles[0].open_time,
        toMs: end,
        note: 'Стартовая опора не задана — показаны доступные свечи',
      };
    }
    const span = Math.max(end - anchorOpenMs, DAY_MS);
    const pad = Math.max(span * 0.1, 5 * DAY_MS);
    return { fromMs: anchorOpenMs - pad, toMs: end + pad };
  }

  function jumpTimeRange(candleOpenMs, candles) {
    if (candleOpenMs == null) {
      return { missing: true, reason: 'У события нет свечи' };
    }
    const has = (candles || []).some((candle) => candle.open_time === candleOpenMs);
    if (!has) {
      return {
        missing: true,
        reason: 'Свечи нет в загруженном фрагменте',
        loadedFromMs: candles && candles.length ? candles[0].open_time : null,
        loadedToMs: candles && candles.length ? candles[candles.length - 1].open_time : null,
      };
    }
    return { fromMs: candleOpenMs - 30 * DAY_MS, toMs: candleOpenMs + 30 * DAY_MS };
  }

  function priceRange(candles, fromSec, toSec) {
    let low = Infinity;
    let high = -Infinity;
    let count = 0;
    (candles || []).forEach((candle) => {
      if (fromSec != null && candle.time < fromSec) return;
      if (toSec != null && candle.time > toSec) return;
      if (candle.low < low) low = candle.low;
      if (candle.high > high) high = candle.high;
      count += 1;
    });
    if (!count || !isFinite(low) || !isFinite(high)) return { min: 0, max: 1 };
    if (!(high > low)) {
      const step = Math.max(Math.abs(low) * 0.01, 1e-8);
      return { min: Math.max(0, low - step), max: low + step };
    }
    const pad = (high - low) * 0.08;
    return { min: Math.max(0, low - pad), max: high + pad };
  }

  function levelPlacement(price, scale) {
    if (!(price > 0) || !scale) return 'hidden';
    if (price < scale.min) return 'below';
    if (price > scale.max) return 'above';
    return 'inside';
  }

  function nearestTarget(targets, lastClose) {
    if (!(lastClose > 0)) {
      return { target: null, reason: 'Нет цены закрытия D1' };
    }
    const waiting = (targets || []).filter((target) =>
      target && target.price != null && !target.hit && !target.passed_at_confirmation && target.price > lastClose
    );
    if (!waiting.length) {
      return { target: null, reason: 'Нет ожидающей цели над последним закрытием D1' };
    }
    waiting.sort((a, b) => (a.price - lastClose) - (b.price - lastClose) || a.tp - b.tp);
    return { target: waiting[0], reason: null };
  }

  function manipulationEpisodes(episodes, mode, asOfMs) {
    const list = episodes || [];
    if (mode === 'history') return list.slice();
    const active = list.filter((episode) => episode.ended_candle_open_time == null);
    if (active.length) {
      const picked = latestOfType(active.map((episode) => ({
        available_at_ms: episode.started_candle_open_time,
        source_id: episode.id,
        episode,
      })));
      return [picked.episode];
    }
    if (asOfMs == null) return [];
    const windowStart = asOfMs - WINDOW_MS;
    const finished = list.filter((episode) => {
      if (episode.ended_candle_open_time == null) return false;
      const end = episode.ended_candle_open_time + DAY_MS;
      return episode.started_candle_open_time <= asOfMs && end >= windowStart;
    });
    if (!finished.length) return [];
    return [finished.slice().sort((a, b) =>
      b.ended_candle_open_time - a.ended_candle_open_time || (b.id || 0) - (a.id || 0)
    )[0]];
  }

  function firstRetest(detail) {
    const rows = (detail.events || []).filter((event) =>
      event.event_type === 'retest' && !(event.payload && event.payload.journal)
    );
    rows.sort((a, b) => (a.event_time_ms || 0) - (b.event_time_ms || 0) || (a.id || 0) - (b.id || 0));
    return rows[0] || null;
  }

  function retestSpan(detail, lastCandleOpenMs) {
    const breakout = detail && detail.breakout;
    if (!breakout || breakout.closed_at == null) return null;
    const startMs = breakout.closed_at - DAY_MS;
    const received = !!(detail.flags && detail.flags.retest_received);
    if (received) {
      const retest = firstRetest(detail);
      let candle = null;
      if (retest) {
        candle = retest.payload && retest.payload.candle_open_time != null
          ? retest.payload.candle_open_time
          : (retest.event_time_ms ? retest.event_time_ms - DAY_MS : null);
      }
      if (candle == null) {
        return { missing: true, reason: 'Время принятого ретеста не найдено — область не рисуется' };
      }
      return { startMs, endMs: candle + DAY_MS };
    }
    if (breakout.retest_deadline_ms == null || lastCandleOpenMs == null) {
      return { missing: true, reason: 'Граница ожидания ретеста не задана — область не рисуется' };
    }
    return { startMs, endMs: Math.min(lastCandleOpenMs + DAY_MS, breakout.retest_deadline_ms) };
  }

  function intervalOnScreen(startMs, endMs, viewFromMs, viewToMs) {
    if (viewFromMs == null || viewToMs == null) return null;
    if (!(endMs > startMs)) return null;
    if (endMs <= viewFromMs || startMs >= viewToMs) return null;
    return { startMs: Math.max(startMs, viewFromMs), endMs: Math.min(endMs, viewToMs) };
  }

  return {
    DAY_MS, WINDOW_MS, EXTRA_BUDGET, HISTORY_CLUSTER_BUDGET,
    closeBoundaryMs, normalizeDetail, selectChartEvents, groupMarkers,
    initialTimeRange, rangeTimeRange, jumpTimeRange, priceRange, levelPlacement,
    nearestTarget, manipulationEpisodes, retestSpan, intervalOnScreen, firstRetest,
  };
});
