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
  const WEEK_MS = 7 * DAY_MS;
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

  function weekStartMs(openTime) {
    const day = Math.floor(openTime / DAY_MS);
    const offset = (day + 3) % 7; // 01.01.1970 — четверг; понедельник = 0
    return (day - offset) * DAY_MS;
  }

  /* W1 из D1 по неделям понедельник 00:00 UTC (ТЗ §6 UI-01).
     Open первой свечи, High максимум, Low минимум, Close последней, Volume сумма.
     partial — последняя неделя не завершена (последняя свеча раньше воскресенья);
     gaps — внутри недели пропущены D1 между её первой и последней свечой. */
  function aggregateW1(candles) {
    const weeks = [];
    let current = null;
    (candles || []).forEach((candle) => {
      const start = weekStartMs(candle.open_time);
      if (!current || current.open_time !== start) {
        current = {
          open_time: start, open: candle.open, high: candle.high,
          low: candle.low, close: candle.close, volume: 0,
          partial: false, gaps: false,
          _count: 0, _first: null, _last: null, _seen: {},
        };
        weeks.push(current);
      }
      const offset = Math.round((candle.open_time - start) / DAY_MS);
      if (current._count === 0) current.open = candle.open;
      if (candle.high > current.high) current.high = candle.high;
      if (candle.low < current.low) current.low = candle.low;
      current.close = candle.close;
      current.volume += Number(candle.volume) || 0;
      current._seen[offset] = true;
      if (current._first == null || offset < current._first) current._first = offset;
      if (current._last == null || offset > current._last) current._last = offset;
      current._count += 1;
    });
    weeks.forEach((week, index) => {
      for (let d = week._first; d <= week._last; d += 1) {
        if (!week._seen[d]) { week.gaps = true; break; }
      }
      if (index === weeks.length - 1 && week._last != null && week._last < 6) week.partial = true;
      delete week._count;
      delete week._first;
      delete week._last;
      delete week._seen;
    });
    return weeks;
  }

  function percentile(values, p) {
    if (!values.length) return null;
    const sorted = values.slice().sort((a, b) => a - b);
    const i = (sorted.length - 1) * p;
    const lo = Math.floor(i);
    const hi = Math.min(sorted.length - 1, Math.ceil(i));
    if (lo === hi) return sorted[lo];
    return sorted[lo] + (sorted[hi] - sorted[lo]) * (i - lo);
  }

  /* База у дна: низ — пол, на котором цена стоит, верх — потолок этой
     консолидации. Одиночная тень и поздний импульс в рамку не входят.
     Мало свечей — null, график остаётся на сохранённых L/U. */
  function shelfRange(candles, startMs, lastMs) {
    const series = [];
    (candles || []).forEach((candle) => {
      if (!candle || candle.open_time == null) return;
      if (candle.open_time < startMs) return;
      if (lastMs != null && candle.open_time > lastMs) return;
      if (!(candle.high > candle.low) && candle.high !== candle.low) return;
      series.push(candle);
    });
    if (series.length < 15) return null;
    const earlyN = Math.min(series.length, Math.max(15, Math.floor(series.length * 0.3)));
    const early = series.slice(0, earlyN);
    const floor0 = percentile(early.map((candle) => candle.low), 0.2);
    const ceiling0 = percentile(early.map((candle) => candle.high), 0.8);
    if (!(ceiling0 > floor0)) return null;
    let end = series.length - 1;
    let run = 0;
    for (let i = earlyN; i < series.length; i += 1) {
      if (series[i].close > ceiling0) {
        run += 1;
        if (run >= 3) {
          end = i - 3;
          break;
        }
      } else {
        run = 0;
      }
    }
    if (end < earlyN - 1) end = earlyN - 1;
    const kept = series.slice(0, end + 1);
    if (kept.length < 10) return null;
    const lows = kept.map((candle) => candle.low);
    const absLow = Math.min.apply(null, lows);
    const cluster = lows.filter((value) => value <= absLow * 1.08);
    const lower = cluster.length >= 3 && cluster.length >= lows.length * 0.12
      ? percentile(cluster, 0.5)
      : percentile(lows, 0.15);
    const touch = kept.filter((candle) => candle.low <= lower * 1.18);
    const pool = touch.length >= 8 ? touch : kept;
    const upper = Math.max(
      percentile(pool.map((candle) => candle.high), 0.8),
      percentile(pool.map((candle) => candle.close), 0.9),
    );
    if (!(upper > lower) || !(lower > 0)) return null;
    return { lower, upper, endMs: kept[kept.length - 1].open_time + DAY_MS };
  }

  /* Рамка на графике. Сохранённые L/U остаются внешним пределом: цели и
     отмена считаются по ним. Заливка кончается на выходе из базы. */
  function chartRange(detail, lastCandleOpenMs) {
    const range = detail && (detail.frozen_range || detail.range);
    if (!range || !(range.upper > range.lower)) return null;
    const startMs = detail.anchors && detail.anchors.start
      ? detail.anchors.start.open_time : null;
    const asOf = detail.as_of_ms;
    const candles = (detail.candles || []).filter((candle) =>
      asOf == null || candle.open_time + DAY_MS <= asOf);
    const lastFromCandles = candles.length ? candles[candles.length - 1].open_time : null;
    const last = lastCandleOpenMs != null ? lastCandleOpenMs : lastFromCandles;
    let endMs = last != null ? last + DAY_MS : null;
    if (detail.breakout && detail.breakout.closed_at != null) {
      endMs = endMs == null ? detail.breakout.closed_at : Math.min(endMs, detail.breakout.closed_at);
    }
    let lower = range.lower;
    let upper = range.upper;
    let shelf = false;
    if (startMs != null) {
      const found = shelfRange(candles, startMs, last);
      if (found) {
        const nextLower = Math.max(found.lower, range.lower);
        const nextUpper = Math.min(found.upper, range.upper);
        if (nextUpper > nextLower) {
          lower = nextLower;
          upper = nextUpper;
          shelf = true;
          if (endMs == null || found.endMs < endMs) endMs = found.endMs;
        }
      }
    }
    return {
      lower, upper, mid: (lower + upper) / 2,
      startMs, endMs, shelf,
    };
  }

  /* Слои графика в виде данных для экрана и экспорта (ТЗ §6 UI-04). */
  function collectBoxes(detail, view, lastCandleOpenMs) {
    const result = { boxes: [], retestNote: '' };
    const range = detail && (detail.frozen_range || detail.range);
    if (!detail || !range) return result;
    const shown = chartRange(detail, lastCandleOpenMs);
    const last = lastCandleOpenMs == null ? null : lastCandleOpenMs;
    if (view.range && shown && shown.startMs != null && shown.endMs != null && shown.endMs > shown.startMs) {
      result.boxes.push({
        kind: 'range',
        startMs: shown.startMs, endMs: shown.endMs,
        upper: shown.upper, lower: shown.lower, shelf: shown.shelf,
      });
    }
    if (view.manipulation) {
      manipulationEpisodes(detail.manipulation_episodes, view.eventMode || 'setup', detail.as_of_ms)
        .forEach((episode) => {
          const end = episode.ended_candle_open_time != null
            ? episode.ended_candle_open_time + DAY_MS
            : (last != null ? last + DAY_MS : null);
          if (end == null) return;
          result.boxes.push({
            kind: 'manip',
            startMs: episode.started_candle_open_time, endMs: end,
            upper: shown ? shown.lower : range.lower,
            lower: episode.min_price, minPrice: episode.min_price,
          });
        });
    }
    if (view.entries) {
      const span = retestSpan(detail, last);
      if (span && span.missing) result.retestNote = span.reason;
      else if (span) {
        result.boxes.push({
          kind: 'retest', startMs: span.startMs, endMs: span.endMs,
          upper: range.upper, lower: range.mid,
        });
      }
    }
    return result;
  }

  function collectLevels(detail, view, lastClosePrice) {
    const result = { levels: [], targetNote: '' };
    const range = detail && (detail.frozen_range || detail.range);
    const shown = chartRange(detail, null);
    if (view.range && shown) {
      [['L', shown.lower], ['U', shown.upper], ['M', shown.mid]].forEach(([name, price]) => {
        if (price != null) result.levels.push({ name, price, cls: 'range' });
      });
    } else if (view.range && range) {
      [['L', range.lower], ['U', range.upper], ['M', range.mid]].forEach(([name, price]) => {
        if (price != null) result.levels.push({ name, price, cls: 'range' });
      });
    }
    if (view.targets === 'nearest') {
      const found = nearestTarget(detail && detail.targets, lastClosePrice);
      result.targetNote = found.target ? '' : (found.reason || '');
      if (found.target) result.levels.push({ name: 'TP' + found.target.tp, price: found.target.price, cls: 'tp' });
    } else if (view.targets === 'all') {
      ((detail && detail.targets) || []).forEach((target) => {
        if (target.price != null) result.levels.push({ name: 'TP' + target.tp, price: target.price, cls: 'tp' });
      });
    }
    if (view.cancel && detail && detail.cancel && detail.cancel.price > 0) {
      result.levels.push({ name: 'K', price: detail.cancel.price, cls: 'k' });
    }
    return result;
  }

  /* Пиксельная проекция областей и линий: общая для DOM-оверлея и PNG. */
  function boxSpanPx(startMs, endMs, candles, mapTime) {
    const x1 = mapTime(startMs);
    const x2 = mapTime(endMs);
    if (x1 != null && x2 != null) return { x1, x2 };
    const xs = [];
    (candles || []).forEach((candle) => {
      const open = candle.open_time;
      if (open + DAY_MS <= startMs || open >= endMs) return;
      const x = mapTime(open);
      if (x != null) xs.push(x);
    });
    if (xs.length < 2) return null;
    return { x1: Math.min.apply(null, xs), x2: Math.max.apply(null, xs) };
  }

  function boxRectPx(box, ctx) {
    const clipped = intervalOnScreen(box.startMs, box.endMs, ctx.viewFromMs, ctx.viewToMs);
    if (!clipped) return null;
    const span = boxSpanPx(clipped.startMs, clipped.endMs, ctx.candles, ctx.mapTime);
    if (!span) return null;
    const chartHeight = ctx.chartHeight;
    const scale = ctx.priceRange;
    const priceY = (price) => {
      if (scale && price > scale.max) return 0;
      if (scale && price < scale.min) return chartHeight;
      return ctx.mapPrice(price);
    };
    let y1 = priceY(box.upper);
    let y2 = priceY(box.lower);
    if (y1 == null && y2 == null) return null;
    if (y1 == null) y1 = box.upper >= box.lower ? 0 : chartHeight;
    if (y2 == null) y2 = box.lower <= box.upper ? chartHeight : 0;
    const left = Math.min(span.x1, span.x2);
    const width = Math.abs(span.x2 - span.x1);
    const top = Math.max(0, Math.min(y1, y2));
    const height = Math.min(chartHeight, Math.max(y1, y2)) - top;
    if (width < 1 || height < 1) return null;
    return { x: left, y: top, width, height };
  }

  /* Единая модель сцены для экспорта: примитивы в пикселях панели графика.
     Координатные преобразования приходят снаружи (mapTime/mapPrice). */
  function buildScene(opts) {
    const scene = { rects: [], lines: [], markers: [] };
    if (!opts) return scene;
    const fmt = opts.fmtPrice || ((price) => String(price));
    (opts.boxes || []).forEach((box) => {
      const rect = boxRectPx(box, {
        viewFromMs: opts.viewFromMs, viewToMs: opts.viewToMs,
        candles: opts.candles, mapTime: opts.mapTime, mapPrice: opts.mapPrice,
        priceRange: opts.priceRange, chartHeight: opts.chartHeight,
      });
      if (rect) scene.rects.push({ kind: box.kind || 'range', x: rect.x, y: rect.y, width: rect.width, height: rect.height });
    });
    const placed = [];
    (opts.lines || []).forEach((line) => {
      if (levelPlacement(line.price, opts.priceRange) !== 'inside') return;
      const y = opts.mapPrice(line.price);
      if (y == null) return;
      placed.push({ line, y });
    });
    placed.sort((a, b) => a.y - b.y);
    const groups = [];
    placed.forEach((item) => {
      const prev = groups[groups.length - 1];
      if (prev && Math.abs(item.y - prev.y) < 14) prev.items.push(item);
      else groups.push({ y: item.y, items: [item] });
    });
    groups.forEach((group) => {
      scene.lines.push({
        kind: group.items[0].line.cls || 'range',
        y: group.y,
        width: opts.paneWidth || 0,
        label: group.items.length === 1
          ? group.items[0].line.name + ' ' + fmt(group.items[0].line.price)
          : group.items.length + ' уровня',
      });
    });
    (opts.markers || []).forEach((marker) => {
      if (marker.x == null || marker.price == null) return;
      const base = opts.mapPrice(marker.price);
      if (base == null) return;
      scene.markers.push({
        x: marker.x,
        y: marker.position === 'belowBar' ? base + 4 : base - 20,
        label: marker.label,
        key: !!marker.hasKey,
        position: marker.position === 'belowBar' ? 'belowBar' : 'aboveBar',
      });
    });
    return scene;
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
    DAY_MS, WEEK_MS, WINDOW_MS, EXTRA_BUDGET, HISTORY_CLUSTER_BUDGET,
    closeBoundaryMs, normalizeDetail, selectChartEvents, groupMarkers,
    initialTimeRange, rangeTimeRange, jumpTimeRange, priceRange, levelPlacement,
    nearestTarget, manipulationEpisodes, retestSpan, intervalOnScreen, firstRetest,
    weekStartMs, aggregateW1, shelfRange, chartRange, collectBoxes, collectLevels,
    boxSpanPx, boxRectPx, buildScene,
  };
});
