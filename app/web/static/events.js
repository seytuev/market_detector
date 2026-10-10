/* Раздел «События» и короткая плашка на H1. Текст приходит с сервера. */
'use strict';

(function () {
  const esc = (s) => (window.HTF ? HTF.esc(s) : String(s ?? ''));

  const GATE_STATUS = {
    passed: 'выполнено',
    blocked: 'заблокировано',
    not_applicable: 'не применимо',
    watch_matched: 'условия наблюдения совпали',
    failed: 'не выполнено',
    unknown: 'неизвестно',
  };
  const RULE_TITLE = {
    MORNING_DIGEST: 'Утренняя сводка',
    POST_HIGH_LONG_CASCADE: 'После максимума: красный день и лонг-ликвидации',
    CASCADE_OI_RISING_RISK: 'Каскад при росте открытого интереса',
    RED_WIDE_DAY_REBOUND_WATCH: 'Красный широкий день, наблюдение отскока',
    RED_STREAK_REBOUND_WATCH: 'Серия снижения, наблюдение отскока',
  };
  const CLASS_RU = {
    strategy_rule: 'Правило наблюдения',
    engineering_default: 'Инженерный порог',
    source_fact: 'Факт источника',
  };
  const TRANSITION_RU = {
    appeared: 'появилось',
    digest: 'сводка',
    resolved: 'снято',
    invalidated: 'отменено',
  };

  function gateCard(gate) {
    const checks = (gate.checks || []).map((item) =>
      `<li><span>${esc(item.label)}</span><span class="events-status">${esc(item.text)}</span></li>`
    ).join('');
    const blocking = gate.blocking_reasons || [];
    const unknown = gate.unknown_dependencies || [];
    const statusWord = GATE_STATUS[gate.status] || 'неизвестное состояние';
    return `<article class="events-card">
      <h2>Сетап ${esc(gate.setup)} · ${esc(statusWord)}</h2>
      <p class="events-service">Код состояния: ${esc(gate.status || '—')}</p>
      <p class="events-line">${esc(gate.title || '')}</p>
      ${blocking.length ? `<p>Причина блокировки: ${esc(blocking.join(', '))}</p>` : ''}
      ${unknown.length ? `<p>Неизвестно: ${esc(unknown.join(', '))}</p>` : ''}
      <ul class="events-checks">${checks}</ul>
    </article>`;
  }

  function shareText(raw) {
    if (raw == null || raw === '') return { label: 'нет данных', exact: '' };
    const n = Number(raw);
    if (!Number.isFinite(n)) return { label: String(raw), exact: String(raw) };
    if (Math.abs(n) <= 1) {
      const pct = (n * 100).toLocaleString('ru-RU', { maximumFractionDigits: 2 });
      return { label: pct + ' %', exact: String(raw) };
    }
    return { label: String(raw), exact: String(raw) };
  }

  function renderNow(data) {
    const gates = (data.strategy_gates || []).map(gateCard).join('');
    const situations = (data.situations || []).map((item) =>
      `<li>${esc(item.title || item.rule_id)}</li>`
    ).join('');
    const funding = data.funding || {};
    const oi = data.oi_coin || {};
    const liq = data.liquidation || {};
    const health = data.health || {};
    const share = shareText(liq.share);
    const shareExact = share.exact
      ? `<details><summary>Исходная доля</summary><p>${esc(share.exact)}</p></details>`
      : '';
    return `<div class="events-split">
      <div class="events-main">
        <section class="events-card">
          <p class="events-line">${esc(data.market_line || 'Нет данных')}</p>
          <p class="events-line events-service">${esc(data.service_line || '')}</p>
          <p class="events-service">Источник цены: ${esc(data.price_source || 'не указан')}</p>
        </section>
        ${situations ? `<section class="events-card"><h2>Ситуации</h2><ul>${situations}</ul></section>` : ''}
        ${gates || '<p>Условий наблюдения нет. Это не выполненный допуск.</p>'}
      </div>
      <aside class="events-side">
        <section class="events-card">
          <h2>Производные</h2>
          <p>Ликвидации: ${esc(liq.quality || 'нет данных')}</p>
          <p>Доля лонгов: ${esc(share.label)}. Это не вероятность.</p>
          ${shareExact}
          <p>OI в монетах: ${esc(oi.direction || 'нет данных')} · качество ${esc(oi.quality || '—')}</p>
          <p>Funding: ${esc(funding.rate_kind || 'нет данных')} · знак ${esc(funding.sign || '—')}</p>
          <p class="events-service">${esc(data.risk_note || '')}</p>
        </section>
        <section class="events-card">
          <h2>Источник</h2>
          <p>Качество: ${esc(health.quality || 'неизвестно')}</p>
          <p class="events-service">${esc(health.detail || '')}</p>
        </section>
      </aside>
    </div>`;
  }

  function eventTitle(item) {
    const payload = item.payload || {};
    if (payload.title) return String(payload.title);
    if (RULE_TITLE[item.rule_id]) return RULE_TITLE[item.rule_id];
    return 'Неизвестное событие';
  }

  function transitionText(value) {
    if (!value) return '—';
    if (TRANSITION_RU[value]) return `${TRANSITION_RU[value]} · ${value}`;
    if (String(value).includes('->')) return `переход ${value}`;
    return String(value);
  }

  function monthGrid(iso) {
    const parts = String(iso || '').split('-').map(Number);
    const year = parts[0];
    const month = parts[1];
    const day = parts[2];
    if (!year || !month || !day) return '';
    const firstDow = (new Date(Date.UTC(year, month - 1, 1)).getUTCDay() + 6) % 7;
    const days = new Date(Date.UTC(year, month, 0)).getUTCDate();
    const names = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
    let html = names.map((name) => `<div class="cal-dow">${name}</div>`).join('');
    for (let i = 0; i < firstDow; i += 1) html += '<div class="cal-cell is-empty"></div>';
    for (let n = 1; n <= days; n += 1) {
      html += `<div class="cal-cell${n === day ? ' is-utc' : ''}">${n}</div>`;
    }
    return `<div class="cal-grid" aria-label="Месяц даты UTC">${html}</div>`;
  }

  function renderSide(data) {
    const box = document.getElementById('h1-events-context');
    if (!box) return;
    // Служебную строку источника здесь не показываем: это подробность раздела
    // «Контекст рынка». Индикатор — сторона контекста событий (direction_note
    // считает сервер по правилам наблюдения): она не зависит от графика
    // и сценария рабочего места.
    const note = data.direction_note || {};
    let state;
    if (note.side === 'long' || note.side === 'short') {
      const long = note.side === 'long';
      state = `<span class="side-state ${long ? 'is-long' : 'is-short'}">` +
        `<span class="state-dot ${long ? 'dot-positive' : 'dot-negative'}"></span>` +
        `${long ? '↑' : '↓'} ${long ? 'LONG' : 'SHORT'}</span>`;
    } else {
      state = '<span class="side-state is-neutral"><span class="state-dot dot-muted"></span>Без направления</span>';
    }
    box.innerHTML = `<div class="side-card-head"><strong>События и время</strong></div>
      <p class="side-market-line">${esc(data.market_line)}</p>
      <div class="events-side-state">${state}` +
      (note.side && note.text ? `<span class="side-state-note">${esc(note.text)}</span>` : '') +
      `</div>
      <a class="btn small side-open" href="/events.html">Открыть раздел</a>`;
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
        ? items.map((item) => {
          const payload = JSON.stringify(item.payload || {}, null, 2);
          return `<article class="events-card">
            <h2>${esc(eventTitle(item))}</h2>
            <p>Код: ${esc(item.rule_id || '—')}</p>
            <p>${esc(transitionText(item.transition))}</p>
            <p class="events-service">Возникло: ${esc(HTF.fmtTime(item.occurred_at))}</p>
            <details><summary>Исходная запись</summary><pre class="events-payload">${esc(payload)}</pre></details>
          </article>`;
        }).join('')
        : '<p>Записей пока нет. Это не значит, что оценка прошла и событий не нашлось.</p>';
      return;
    }
    if (mode === 'calendar') {
      const data = await HTF.api('/api/events/calendar');
      if (data.empty_reason || !data.utc_date) {
        body.innerHTML = `<p>${esc(data.empty_reason || 'Нет данных календаря.')}</p>`;
        return;
      }
      const local = new Date().toLocaleDateString('en-CA', { timeZone: 'Europe/Moscow' });
      const both = local !== data.utc_date
        ? `<p>Местный день МСК ${esc(local)} отличается от даты источника UTC ${esc(data.utc_date)}. Дата источника не исправляется.</p>`
        : `<p>Дата источника UTC совпадает с календарным днём МСК: ${esc(data.utc_date)}.</p>`;
      body.innerHTML = `<div class="cal-layout">${monthGrid(data.utc_date)}
        <article class="events-card">
          ${both}
          <p>Неделя месяца ${esc(data.week_of_month)}.</p>
          <p>${data.month_end_card ? 'Карточка конца месяца включена.' : 'Карточка конца месяца выключена.'}</p>
          <p>${esc((data.chain || {}).text || '')}</p>
          <p class="events-service">${esc(data.note || '')}</p>
        </article></div>`;
      return;
    }
    if (mode === 'rules') {
      const data = await HTF.api('/api/events/rules');
      body.innerHTML = `<p class="events-service">${esc(data.note)}</p>` + (data.rules || []).map((rule) =>
        `<article class="events-card"><h2>${esc(rule.text)}</h2><p>ID ${esc(rule.id)} · ${esc(CLASS_RU[rule.class] || rule.class || '—')}</p><p class="events-service">Класс: ${esc(rule.class || '—')}</p></article>`
      ).join('');
      return;
    }
    body.innerHTML = renderNow(await HTF.api('/api/events/overview'));
  }

  document.querySelectorAll('[data-mode]').forEach((button) => {
    button.addEventListener('click', () => {
      document.querySelectorAll('[data-mode]').forEach((item) => {
        const on = item === button;
        item.classList.toggle('active', on);
        item.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      show(button.getAttribute('data-mode')).catch((err) => {
        const body = document.getElementById('events-body');
        if (body) body.textContent = err.message || 'Не удалось загрузить события';
      });
    });
  });

  if (document.getElementById('events-root')) {
    const initial = document.querySelector('[data-mode="now"]');
    if (initial) {
      initial.classList.add('active');
      initial.setAttribute('aria-pressed', 'true');
    }
    show('now').catch((err) => {
      const body = document.getElementById('events-body');
      if (body) body.textContent = err.message || 'Не удалось загрузить события';
    });
  } else {
    loadSide();
  }
})();
