/* Раздел «События» и короткая плашка на H1. Текст приходит с сервера. */
'use strict';

(function () {
  const esc = (s) => (window.HTF ? HTF.esc(s) : String(s ?? ''));

  function gateCard(gate) {
    const checks = (gate.checks || []).map((item) =>
      `<li><span>${esc(item.label)}</span><span class="events-status">${esc(item.text)}</span></li>`
    ).join('');
    const reasons = (gate.blocking_reasons || []).concat(gate.unknown_dependencies || []);
    return `<article class="events-card">
      <h2>Сетап ${esc(gate.setup)} · ${esc(gate.status)}</h2>
      <p class="events-line">${esc(gate.title || '')}</p>
      ${reasons.length ? `<p class="events-service">${esc(reasons.join(', '))}</p>` : ''}
      <ul class="events-checks">${checks}</ul>
    </article>`;
  }

  function renderNow(data) {
    const gates = (data.strategy_gates || []).map(gateCard).join('');
    const situations = (data.situations || []).map((item) =>
      `<li>${esc(item.title || item.rule_id)}</li>`
    ).join('');
    const funding = data.funding || {};
    const oi = data.oi_coin || {};
    const liq = data.liquidation || {};
    return `<section class="events-card">
      <p class="events-line">${esc(data.market_line)}</p>
      <p class="events-line events-service">${esc(data.service_line)}</p>
      <p class="events-service">Источник цены: ${esc(data.price_source || 'не указан')}</p>
    </section>
    <section class="events-card">
      <h2>Производные</h2>
      <p>Ликвидации: ${esc(liq.quality || 'нет')} · доля лонгов ${esc(liq.share || '—')}</p>
      <p>OI в монетах: ${esc(oi.direction || 'нет данных')} · качество ${esc(oi.quality || '—')}</p>
      <p>Funding: ${esc(funding.rate_kind || 'нет')} · знак ${esc(funding.sign || '—')}</p>
      <p class="events-service">${esc(data.risk_note || '')}</p>
    </section>
    ${situations ? `<section class="events-card"><h2>Ситуации</h2><ul>${situations}</ul></section>` : ''}
    ${gates}`;
  }

  function renderSide(data) {
    const box = document.getElementById('h1-events-context');
    if (!box) return;
    box.innerHTML = `<p><strong>События и время</strong></p>
      <p>${esc(data.market_line)}</p>
      <p class="events-service">${esc(data.service_line)}</p>
      <p><a href="/events.html">Открыть раздел</a></p>`;
  }

  async function loadSide() {
    const box = document.getElementById('h1-events-context');
    if (!box || !window.HTF || document.getElementById('events-root')) return;
    if (!HTF.getToken()) {
      box.innerHTML = '<p>События и время появятся после входа.</p>';
      return;
    }
    try {
      renderSide(await HTF.api('/api/events/overview'));
    } catch (err) {
      box.innerHTML = '<p>События и время сейчас недоступны.</p>';
    }
  }

  async function show(mode) {
    const body = document.getElementById('events-body');
    if (!body) return;
    if (!HTF.getToken()) await HTF.ensureToken('Нужен токен владельца.');
    if (mode === 'feed') {
      const data = await HTF.api('/api/events/journal');
      const items = data.items || [];
      body.innerHTML = items.length
        ? items.map((item) => `<article class="events-card"><h2>${esc(item.rule_id)}</h2><p>${esc(item.transition)}</p><p class="events-service">${esc(HTF.fmtTime(item.occurred_at))}</p></article>`).join('')
        : '<p>Записей пока нет. Это не значит, что оценка прошла и событий не нашлось.</p>';
      return;
    }
    if (mode === 'calendar') {
      const data = await HTF.api('/api/events/calendar');
      if (data.empty_reason) {
        body.innerHTML = `<p>${esc(data.empty_reason)}</p>`;
        return;
      }
      body.innerHTML = `<article class="events-card">
        <p>Дата UTC ${esc(data.utc_date)}. Неделя месяца ${esc(data.week_of_month)}.</p>
        <p>${data.month_end_card ? 'Карточка конца месяца включена.' : 'Карточка конца месяца выключена.'}</p>
        <p>${esc((data.chain || {}).text || '')}</p>
        <p class="events-service">${esc(data.note || '')}</p>
      </article>`;
      return;
    }
    if (mode === 'rules') {
      const data = await HTF.api('/api/events/rules');
      body.innerHTML = `<p class="events-service">${esc(data.note)}</p>` + (data.rules || []).map((rule) =>
        `<article class="events-card"><h2>${esc(rule.id)}</h2><p>${esc(rule.text)}</p><p class="events-service">${esc(rule.class)}</p></article>`
      ).join('');
      return;
    }
    body.innerHTML = renderNow(await HTF.api('/api/events/overview'));
  }

  document.querySelectorAll('[data-mode]').forEach((button) => {
    button.addEventListener('click', () => {
      show(button.getAttribute('data-mode')).catch((err) => {
        const body = document.getElementById('events-body');
        if (body) body.textContent = err.message || 'Не удалось загрузить события';
      });
    });
  });

  if (document.getElementById('events-root')) {
    show('now').catch((err) => {
      const body = document.getElementById('events-body');
      if (body) body.textContent = err.message || 'Не удалось загрузить события';
    });
  } else {
    loadSide();
  }
})();
