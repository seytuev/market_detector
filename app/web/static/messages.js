/* Общие блоки карточки. Текст приходит с сервера: направление, отмена
   и причина завершения здесь не восстанавливаются. */
'use strict';

window.LFCopy = (() => {
  const esc = (s) => (window.HTF ? HTF.esc(s) : String(s ?? ''));

  function block(title, text) {
    if (!text) return '';
    return `<div class="msg-block"><div class="msg-k">${esc(title)}</div>` +
      `<div class="msg-v">${esc(text)}</div></div>`;
  }

  function shownZones(rows) {
    const all = rows || [];
    const blocking = all.filter((z) => z.blocking);
    const rest = all.filter((z) => !z.blocking);
    const shown = blocking.concat(rest).slice(0, 2);
    return { shown, more: all.length - shown.length };
  }

  function zoneLine(z, headline) {
    const title = z.headline === headline ? '' : z.headline;
    const text = [title, z.detail].filter(Boolean).join('. ');
    return text;
  }

  function card(p, opts) {
    if (!p) return '';
    opts = opts || {};
    const headline = (p.headline && p.headline.headline) || '—';
    const detail = (p.headline && p.headline.detail) || '';
    let html = '';
    if (p.instrument_line) {
      html += `<div class="now-card-head"><span>${esc(p.instrument_line)}</span></div>`;
    }
    html += `<h3>${esc(headline)}</h3>`;
    if (detail && detail !== headline) {
      html += `<p class="now-card-lead">${esc(detail)}</p>`;
    }
    (p.data_issues || []).forEach((issue) => {
      const text = [issue.headline, issue.detail].filter(Boolean).join('. ');
      if (text && text !== headline) html += `<p class="msg-issue">${esc(text)}</p>`;
    });
    if (p.conflict && p.conflict.headline) {
      html += block('Контексты', [p.conflict.headline, p.conflict.detail].filter(Boolean).join('. '));
    }
    if (p.context && p.context.text) html += block('Контекст', p.context.text);
    const location = p.location || {};
    const locationText = opts.suppressLocation
      ? 'Положение цены обновляется'
      : location.text;
    if (locationText) html += block('Положение цены', locationText);
    (p.notes || []).forEach((note) => {
      html += block(note.headline || 'Зона', note.detail || note.headline);
    });
    if (p.structure && p.structure.text && p.structure.text !== headline && p.structure.text !== detail) {
      html += block('H1', p.structure.text);
    }
    (p.next_conditions || []).forEach((cond) => {
      if (!cond.text || cond.text === detail) return;
      const title = cond.kind === 'either' ? 'Что откроет сценарий' : `Условие ${cond.kind || ''}`.trim();
      html += block(title, cond.text);
    });
    (p.termination_conditions || []).forEach((row) => {
      const text = [row.headline, row.detail].filter(Boolean).join('. ');
      if (text) html += block('Завершение', text);
    });
    const zones = shownZones(p.other_zones);
    zones.shown.forEach((z) => {
      const text = zoneLine(z, headline);
      if (text) html += block(z.blocking ? 'Нужна сверка' : 'Другая зона', text);
    });
    if (zones.more > 0) html += block('Другие зоны', `Ещё ${zones.more}`);
    (p.liquidity_facts || []).forEach((fact) => {
      const text = [fact.headline, fact.detail].filter(Boolean).join('. ');
      if (text) html += block('Ликвидность', text);
    });
    if (p.liquidity_more > 0) html += block('Ликвидность', `Ещё ${p.liquidity_more}`);
    if (p.review && p.review.count > 0 && p.review.text) {
      html += `<div class="msg-block"><a class="review-badge" href="#review">${esc(p.review.text)}</a>` +
        `<div class="msg-v">По этому активу. Общий счётчик — «По всем активам».</div></div>`;
    }
    const shownIds = new Set(zones.shown.map((zone) => String(zone.zone_id)));
    const contextId = p.context && p.context.zone_id != null ? String(p.context.zone_id) : '';
    const actions = (p.actions || []).filter((action) => {
      if (!action || action.zone_id == null || !action.href) return false;
      const id = String(action.zone_id);
      return id === contextId || shownIds.has(id);
    });
    if (actions.length) {
      html += '<div class="msg-actions">';
      actions.forEach((action) => {
        html += `<button type="button" class="btn msg-action" data-lf-action="${esc(action.kind || 'reconcile')}" data-zone-id="${esc(action.zone_id)}">${esc(action.label || 'Запустить сверку')}</button>`;
      });
      html += '</div>';
    }
    if (opts.extraHtml) html += opts.extraHtml;
    return html;
  }

  document.addEventListener('click', async (event) => {
    const btn = event.target.closest('[data-lf-action="reconcile"]');
    if (!btn || btn.disabled) return;
    const zoneId = btn.getAttribute('data-zone-id');
    if (!zoneId || !window.HTF) return;
    btn.disabled = true;
    btn.textContent = 'Сверка идёт';
    try {
      await window.HTF.api(`/api/zones/${encodeURIComponent(zoneId)}/reconcile`, { method: 'POST' });
      document.dispatchEvent(new CustomEvent('lf-reconciled', { detail: { zoneId: Number(zoneId) } }));
    } catch (err) {
      btn.disabled = false;
      btn.textContent = 'Повторить расчёт';
    }
  });

  return { card };
})();
