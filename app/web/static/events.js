/* Раздел «Контекст рынка» и компактная карточка «События и время · Крипторынок».
   Итог, стрелка, причины и конфликт приходят с сервера одним снимком
   (overview.market, assessment_id). Когда снимка нет — старые экраны
   (legacy BTC / прежняя методика). */
'use strict';

(function () {
  const esc = (s) => (window.HTF ? HTF.esc(s) : String(s ?? ''));

  // ---- словари русских подписей -------------------------------------------
  const ARROW_GLYPH = { up: '↑', down: '↓', flat: '↔', conflict: '！', unknown: '—' };
  const DIRECTION_WORD = {
    long: 'LONG', short: 'SHORT', neutral: 'нейтрально',
    mixed: 'противоречиво', unknown: 'неизвестно',
  };
  const GROUP_RU = { events: 'События', time: 'Время' };
  const QUALITY_RU = {
    ok: 'полное', partial: 'частичное', stale: 'устаревшее',
    unavailable: 'недоступно', unknown: 'неизвестно',
  };
  const FACT_STATE_RU = {
    provisional: 'предварительное', confirmed: 'подтверждено',
    invalidated: 'отменено', expired: 'истёк срок',
  };
  const RULE_CLASS_RU = {
    market_event: 'рыночное событие', time_condition: 'временное условие',
    combination: 'комбинация', service_change: 'сервисное изменение',
  };
  const CONFIRMATION_RU = {
    confirmed: 'подтверждено', unconfirmed: 'не подтверждено',
    experimental: 'экспериментальное',
  };
  const SOURCE_RU = {
    price: 'Цены', liquidations: 'Ликвидации', oi: 'Открытый интерес',
    funding: 'Фандинг', calendar: 'Календарь', basket: 'Корзина',
  };
  const BUNDLE_KIND_RU = { changes: 'Изменения', downtime_digest: 'Сводка после простоя' };
  const CHANNEL_RU = { feed: 'лента', telegram: 'telegram', push: 'push' };
  const DELIVERY_RU = {
    delivered: 'доставлено', sent: 'отправлено', pending: 'ожидает',
    unavailable: 'недоступна', failed: 'ошибка', shadow: 'теневая запись',
  };
  const UPCOMING_KIND_RU = { day_boundary: 'Граница дня', fact_expiry: 'Истечение факта' };

  // ---- форматтеры ----------------------------------------------------------
  function fmtMsk(ms) {
    if (!ms) return '—';
    // HTF.fmtTime уже выводит московское время с пометкой «МСК»
    return window.HTF && HTF.fmtTime ? HTF.fmtTime(ms) : '—';
  }

  function fmtAge(seconds) {
    if (seconds == null || !Number.isFinite(Number(seconds))) return '';
    const s = Math.max(0, Math.round(Number(seconds)));
    if (s < 90) return `${s} с назад`;
    if (s < 5400) return `${Math.round(s / 60)} мин назад`;
    return `${Math.round(s / 3600)} ч назад`;
  }

  function fmtNum(value) {
    if (value == null || value === '') return '—';
    const n = Number(value);
    if (!Number.isFinite(n)) return String(value);
    return n.toLocaleString('ru-RU', { maximumFractionDigits: 4 });
  }

  function signWord(sign) {
    const n = Number(sign);
    if (n > 0) return '+';
    if (n < 0) return '−';
    return '0';
  }

  function qualityWord(quality) {
    return QUALITY_RU[quality] || quality || 'неизвестно';
  }

  function directionClass(direction) {
    return { long: 'is-long', short: 'is-short', neutral: 'is-neutral', mixed: 'is-mixed' }[direction]
      || 'is-unknown';
  }

  // ---- общие блоки рыночного снимка ---------------------------------------
  // Итоговый индикатор: глиф стрелки + строка сервера. LONG/SHORT всегда
  // подписаны как направление контекста, цвет дублируется словами.
  function stateChip(market) {
    const direction = (market && market.direction) || 'unknown';
    const glyph = ARROW_GLYPH[(market && market.arrow)] || '';
    const raw = String((market && market.market_line) || 'Направление неизвестно');
    const line = glyph ? raw.replace(/^[↑↓↔！]+\s*/, '') : raw;
    const text = glyph ? `${glyph} ${line}` : line;
    const note = (direction === 'long' || direction === 'short')
      ? `<span class="side-state-note">контекст поддерживает ${direction === 'long' ? 'LONG' : 'SHORT'}</span>`
      : '';
    return `<span class="side-state ${directionClass(direction)}">${esc(text)}</span>${note}`;
  }

  function conflictHtml(market) {
    if (!market || !market.conflict_note) return '';
    return `<p class="events-conflict">${esc(market.conflict_note)}</p>`;
  }

  // До двух причин; при противоречии — по одной причине каждой стороны (AC11).
  function pickReasons(market) {
    const reasons = (market && market.reasons) || [];
    if (!market || !market.contradictory) return reasons.slice(0, 2);
    const long = reasons.find((item) => item && item.side === 'long');
    const short = reasons.find((item) => item && item.side === 'short');
    const both = [long, short].filter(Boolean);
    return both.length ? both : reasons.slice(0, 2);
  }

  function reasonLine(item) {
    const group = GROUP_RU[item.group] || 'Фактор';
    const side = item.side === 'long' ? ' · LONG' : item.side === 'short' ? ' · SHORT' : '';
    return `<p class="events-reason">${esc(group)}${esc(side)}: ${esc(item.text || '')}</p>`;
  }

  function factLine(fact) {
    const state = FACT_STATE_RU[fact.state] || fact.state || '—';
    const dir = fact.direction === 'long' ? 'LONG'
      : fact.direction === 'short' ? 'SHORT' : 'без направления';
    const until = fact.valid_until ? ` · учитывается до ${fmtMsk(fact.valid_until)}` : '';
    const summary = fact.summary ? ` — ${fact.summary}` : '';
    return `<li><strong>${esc(fact.rule_id || '—')}</strong> · направление контекста: ${esc(dir)} · ${esc(state)}<br>
      <span class="events-service">${esc(fmtMsk(fact.occurred_at))}${esc(until)}${esc(summary)}</span></li>`;
  }

  function factsListHtml(facts) {
    const list = facts || [];
    if (!list.length) return '<p>Активных фактов десятидневного окна нет.</p>';
    return `<ul>${list.map(factLine).join('')}</ul>`;
  }

  // Частота и калиброванная вероятность подписаны раздельно; калиброванная
  // показывается только при kind == 'calibrated_probability' (AC13).
  function statsHtml(stats) {
    if (!stats || !stats.available) return '<p>Статистика недостаточна.</p>';
    const label = stats.kind === 'calibrated_probability'
      ? 'калиброванная вероятность' : 'историческая частота';
    const n = stats.n != null ? ` · n=${fmtNum(stats.n)}` : '';
    const unc = stats.uncertainty
      ? ` · интервал ${fmtNum(stats.uncertainty.low)}–${fmtNum(stats.uncertainty.high)}` : '';
    return `<p>${esc(stats.text || '')}</p>
      <p class="events-service">${esc(label)}${esc(n)}${esc(unc)}${stats.period ? esc(` · период ${stats.period}`) : ''}</p>`;
  }

  function factorAgreementHtml(agreement) {
    if (!agreement) return '';
    const label = agreement.label || 'Согласованность факторов';
    return `<p>${esc(label)}: ${esc(fmtNum(agreement.long))}:${esc(fmtNum(agreement.short))}</p>`;
  }

  function unknownDepsHtml(deps) {
    const list = deps || [];
    if (!list.length) return '';
    const names = list.map((name) => SOURCE_RU[name] || name).join(', ');
    return `<p class="events-service">Не проверено: ${esc(names)}</p>`;
  }

  function btcBackgroundHtml(market) {
    const bg = market && market.btc_background;
    if (!bg || !bg.direction_note) return '';
    const note = bg.direction_note;
    const side = note.side === 'long' ? 'LONG' : note.side === 'short' ? 'SHORT' : 'без направления';
    return `<section class="events-card">
      <h2>${esc(bg.label || 'Фон BTC')}</h2>
      <p class="events-service">Компонент, а не вывод по всему рынку.</p>
      <p>Направление контекста: ${esc(side)}</p>
      ${note.text ? `<p>${esc(note.text)}</p>` : ''}
    </section>`;
  }

  // ---- компактная карточка (#h1-events-context, §8.1) ----------------------
  function renderSideMarket(data) {
    const box = document.getElementById('h1-events-context');
    if (!box) return;
    const market = data.market;
    if (!market) { renderSideLegacy(data); return; }

    const coverage = market.coverage;
    const coverageText = coverage && coverage.total != null
      ? ` · покрытие ${coverage.ok}/${coverage.total}` : '';
    const checked = `Проверено ${fmtMsk(market.evaluated_at)}${coverageText} · качество: ${qualityWord(market.quality)}`;
    const stale = data.market_stale
      ? '<p class="events-warn">Снимок устарел — показана последняя сохранённая оценка.</p>' : '';
    const nextReview = market.next_review_at
      ? `<p class="events-service">Следующий пересмотр: ${esc(fmtMsk(market.next_review_at))}</p>` : '';
    const reasons = pickReasons(market).map(reasonLine).join('');

    const details = `<details class="events-facts">
      <summary>Факты и статистика</summary>
      ${factsListHtml(market.active_facts)}
      ${statsHtml(market.statistics)}
      ${factorAgreementHtml(market.factor_agreement)}
      ${unknownDepsHtml(market.unknown_dependencies)}
      ${btcBackgroundHtml(market)}
    </details>`;

    box.innerHTML = `<div class="side-card-head"><strong>События и время · Крипторынок</strong></div>
      <div class="events-side-state">${stateChip(market)}</div>
      <p class="side-market-line">${esc(market.subtitle || 'Направление по событиям и времени')}</p>
      ${conflictHtml(market)}
      ${reasons}
      <p class="events-service">${esc(checked)}</p>
      ${stale}
      ${nextReview}
      ${details}
      <a class="btn small side-open" href="/events.html">Открыть контекст рынка</a>`;
  }

  // ---- полный раздел: «Сейчас» --------------------------------------------
  function groupDirectionHtml(label, direction) {
    const word = DIRECTION_WORD[direction] || 'неизвестно';
    const text = direction === 'long' || direction === 'short'
      ? `контекст поддерживает ${word}` : word;
    return `<p>${esc(label)}: ${esc(text)}</p>`;
  }

  function upcomingReviewsHtml(market) {
    const items = [];
    if (market.next_review_at) {
      items.push(`<li>Следующий пересмотр — ${esc(fmtMsk(market.next_review_at))}</li>`);
    }
    const expiries = (market.active_facts || [])
      .filter((fact) => fact && fact.valid_until)
      .sort((a, b) => a.valid_until - b.valid_until)
      .slice(0, 3)
      .map((fact) => `<li>${esc(fact.rule_id || 'Факт')}: больше не учитывается с ${esc(fmtMsk(fact.valid_until))}</li>`);
    items.push(...expiries);
    if (!items.length) return '';
    return `<section class="events-card"><h2>Ближайшие переключения</h2><ul>${items.join('')}</ul></section>`;
  }

  function renderNowMarket(data) {
    const market = data.market;
    const coverage = market.coverage;
    const coverageLine = coverage && coverage.total != null
      ? `<p>Покрытие корзины: ${coverage.ok}/${coverage.total} (${fmtNum(Math.round((coverage.ratio || 0) * 100))}%) — ${coverage.sufficient ? 'достаточно' : 'недостаточно'}</p>`
      : '<p>Покрытие корзины не оценено.</p>';
    const age = data.market_age_seconds != null ? ` · возраст ${fmtAge(data.market_age_seconds)}` : '';
    const stale = data.market_stale
      ? '<p class="events-warn">Снимок устарел — показана последняя сохранённая оценка.</p>' : '';
    const legacy = data.legacy_methodology
      ? `<section class="events-card"><h2>${esc(data.legacy_methodology)}</h2>${renderNowLegacy(data)}</section>` : '';

    const facts = market.active_facts || [];
    const ratio = coverage && coverage.total > 0
      ? Math.max(0, Math.min(100, Math.round(Number(coverage.ok || 0) / coverage.total * 100))) : null;
    const groupCard = (label, direction, symbol, description) => `<section class="events-card market-factor ${directionClass(direction)}">
      <span class="market-icon" aria-hidden="true">${symbol}</span><h2>${label}</h2>
      ${groupDirectionHtml('Направление', direction)}<p class="events-service">${description}</p></section>`;
    return `<section class="events-card market-hero ${directionClass(market.direction)}">
        <p class="market-eyebrow">Общая картина · Крипторынок</p>
        <div class="events-side-state">${stateChip(market)}</div>
        <p class="events-line">${esc(market.subtitle || 'Направление по событиям и времени')}</p>
        ${conflictHtml(market)}
        <div class="market-reasons"><h2>Почему такой контекст</h2>${pickReasons(market).map(reasonLine).join('') || '<p class="events-service">Причины пока не представлены.</p>'}</div>
        <p class="events-service">Проверено ${esc(fmtMsk(market.evaluated_at))}${esc(age)}</p>${stale}
      </section>
      <div class="market-metrics">
        ${groupCard('События', market.events_direction, '↗', 'Направление по рыночным событиям и активным фактам.')}
        ${groupCard('Время', market.time_direction, '◷', 'Влияние дня недели и временных условий по МСК.')}
        <section class="events-card market-quality"><span class="market-icon" aria-hidden="true">◉</span><h2>Качество данных</h2>
          <p class="market-quality-value">${esc(qualityWord(market.quality))}</p>
          ${coverageLine}${ratio != null ? `<meter min="0" max="100" value="${ratio}" aria-label="Покрытие корзины">${ratio}%</meter>` : ''}
          ${unknownDepsHtml(market.unknown_dependencies)}</section>
      </div>
      <div class="market-detail-grid">
        <section class="events-card market-facts"><p class="market-eyebrow">Основания оценки</p><h2>Активные факты <span class="market-count">${facts.length}</span></h2>
          <p class="events-service">События, которые учитываются в окне последних 10 дней.</p>${factsListHtml(facts)}</section>
        <div>${upcomingReviewsHtml(market)}${btcBackgroundHtml(market)}
          <section class="events-card"><h2>Статистика и согласованность</h2>${statsHtml(market.statistics)}${factorAgreementHtml(market.factor_agreement)}</section></div>
      </div>
      <p class="market-footnote">LONG и SHORT обозначают направление контекста. Контекст помогает оценить условия рынка и не является сигналом для входа.</p>
      ${legacy}`;
  }

  // ---- полный раздел: «Лента» ----------------------------------------------
  function bundleCard(bundle) {
    const kind = BUNDLE_KIND_RU[bundle.kind] || bundle.kind || 'Изменения';
    const publications = (bundle.publications || []).map((pub) => {
      const link = pub.assessment_id
        ? ` · <a href="/events.html?assessment=${encodeURIComponent(pub.assessment_id)}">оценка</a>` : '';
      return `<li>${esc(pub.text || '')} <span class="events-service">${esc(fmtMsk(pub.created_at))}</span>${link}</li>`;
    }).join('');
    const deliveries = (bundle.deliveries || []).map((delivery) =>
      `<span class="events-delivery">${esc(CHANNEL_RU[delivery.channel] || delivery.channel || '—')}: ${esc(DELIVERY_RU[delivery.status] || delivery.status || '—')}</span>`
    ).join(' · ');
    return `<article class="events-card events-bundle">
      <h2><span class="badge">${esc(kind)}</span> ${esc(fmtMsk(bundle.opened_at))}</h2>
      ${publications ? `<ul>${publications}</ul>` : '<p>Публикаций нет.</p>'}
      ${deliveries ? `<p class="events-service">${deliveries}</p>` : ''}
    </article>`;
  }

  function renderFeedBundles(data) {
    const bundles = [...(data.bundles || [])]
      .sort((a, b) => (b.opened_at || 0) - (a.opened_at || 0));
    if (!bundles.length) return '';
    return bundles.map(bundleCard).join('');
  }

  // ---- глубокая ссылка на сохранённую оценку (?assessment=<id>) -------------
  // Карточка только для чтения поверх текущего вида; текущая оценка ниже
  // может отличаться. При 404 — честное «Оценка не найдена».
  function savedAssessmentCard(assessment) {
    const word = DIRECTION_WORD[assessment.direction] || 'неизвестно';
    return `<section class="events-card events-saved-assessment">
      <h2>Сохранённая оценка</h2>
      <p class="events-line">${esc(assessment.market_line || '')}</p>
      <p>Направление: ${esc(word)} · проверено ${esc(fmtMsk(assessment.evaluated_at))}</p>
      <p class="events-service">Текущая оценка может отличаться.</p>
    </section>`;
  }

  async function loadSavedAssessment(requested) {
    const body = document.getElementById('events-body');
    if (!body) return;
    let html;
    try {
      const assessment = await HTF.api(
        `/api/events/market/assessment/${encodeURIComponent(requested)}`);
      html = savedAssessmentCard(assessment);
    } catch (err) {
      html = '<section class="events-card events-saved-assessment">'
        + '<h2>Сохранённая оценка</h2><p>Оценка не найдена.</p></section>';
    }
    body.insertAdjacentHTML('beforebegin', html);
  }

  // ---- полный раздел: «Календарь» ------------------------------------------
  function renderCalendarMarket(market) {
    if (!market) return '';
    const tc = market.time_context || {};
    const days = (tc.completed_days || []).map((day) =>
      `<li>${esc(fmtMsk(day.day_start_ms))} · ${esc(day.weekday_name || day.weekday || '')} · знак ${esc(signWord(day.sign))}${day.median_return != null ? esc(` · медиана ${fmtNum(day.median_return)}`) : ''}</li>`
    ).join('');
    const seq = tc.week_seq;
    const seqText = seq
      ? `Понедельник ${signWord(seq.mon_sign)}, вторник ${signWord(seq.tue_sign)} — условие комбинации ${seq.condition_met ? 'выполнено' : 'не выполнено'}.`
      : 'Недельная комбинация пока не оценивается.';
    const upcoming = (market.upcoming || []).map((item) =>
      `<li>${esc(UPCOMING_KIND_RU[item.kind] || item.kind || '—')}: ${esc(item.label || '')} — ${esc(fmtMsk(item.at_ms))}</li>`
    ).join('');
    const hour = tc.hour_msk != null ? ` · час МСК: ${tc.hour_msk}` : '';
    return `<article class="events-card">
      <h2>Текущие условия МСК</h2>
      <p>День недели: ${esc(tc.weekday_name || '—')}${esc(hour)}</p>
      ${days ? `<h3>Завершённые дни</h3><ul>${days}</ul>` : ''}
      <p>${esc(seqText)}</p>
      ${upcoming ? `<h3>Ближайшие переключения</h3><ul>${upcoming}</ul>` : ''}
      <p class="events-service">Будущие ликвидации в календаре не отмечаются.</p>
    </article>`;
  }

  // ---- полный раздел: «Правила и статистика» --------------------------------
  function ruleRegistryCard(rule) {
    const cls = RULE_CLASS_RU[rule.rule_class] || rule.rule_class || '—';
    const confirmation = CONFIRMATION_RU[rule.confirmation] || rule.confirmation || '—';
    const enabled = rule.enabled === false ? 'выключено' : 'включено';
    const direction = rule.direction === 'long' ? 'контекст LONG'
      : rule.direction === 'short' ? 'контекст SHORT' : (rule.direction || '—');
    return `<article class="events-card">
      <h2>${esc(rule.rule_id || '—')}</h2>
      <p>${esc(cls)} · ${esc(confirmation)} · ${esc(enabled)}</p>
      <p>Направление: ${esc(direction)} · окно: ${esc(rule.window || '—')}</p>
      ${rule.origin ? `<p>Происхождение: ${esc(rule.origin)}</p>` : ''}
      ${rule.formula ? `<p>Формула: ${esc(rule.formula)}</p>` : ''}
      ${rule.explanation ? `<p class="events-service">${esc(rule.explanation)}</p>` : ''}
    </article>`;
  }

  // ---- полный раздел: «Источники» -------------------------------------------
  function renderSources(data) {
    const sources = data.sources || {};
    const rows = Object.keys(sources).map((name) => {
      const src = sources[name] || {};
      const capability = String(src.detail || '').includes('CAPABILITY');
      const age = src.age_seconds != null ? ` · ${fmtAge(src.age_seconds)}` : '';
      return `<div class="events-src-row">
        <strong>${esc(SOURCE_RU[name] || name)}</strong>
        <span>${esc(qualityWord(src.quality))}</span>
        <span class="events-service${capability ? ' events-warn' : ''}">${esc(src.detail || '')}</span>
        <span class="events-service">обновлено ${esc(fmtMsk(src.updated_at))}${esc(age)}</span>
      </div>`;
    }).join('');
    return `<article class="events-card">
      <h2>Источники данных</h2>
      <p>Качество: ${esc(qualityWord(data.quality))}</p>
      <p class="events-service">${esc(data.detail || '')}</p>
      ${rows || '<p>Детализация по источникам пока недоступна.</p>'}
    </article>`;
  }

  // ---- legacy-экраны (когда overview.market отсутствует) ---------------------
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

  function renderNowLegacy(data) {
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

  function renderSideLegacy(data) {
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

  function renderFeedLegacy(data) {
    const items = data.items || [];
    return items.length
      ? items.map((item) => {
        const payload = JSON.stringify(item.payload || {}, null, 2);
        return `<article class="events-card">
          <h2>${esc(eventTitle(item))}</h2>
          <p>Код: ${esc(item.rule_id || '—')}</p>
          <p>${esc(transitionText(item.transition))}</p>
          <p class="events-service">Возникло: ${esc(fmtMsk(item.occurred_at))}</p>
          <details><summary>Исходная запись</summary><pre class="events-payload">${esc(payload)}</pre></details>
        </article>`;
      }).join('')
      : '<p>Записей пока нет. Это не значит, что оценка прошла и событий не нашлось.</p>';
  }

  function renderCalendarLegacy(data) {
    if (data.empty_reason || !data.utc_date) return '';
    const local = new Date().toLocaleDateString('en-CA', { timeZone: 'Europe/Moscow' });
    const both = local !== data.utc_date
      ? `<p>Местный день МСК ${esc(local)} отличается от даты источника UTC ${esc(data.utc_date)}. Дата источника не исправляется.</p>`
      : `<p>Дата источника UTC совпадает с календарным днём МСК: ${esc(data.utc_date)}.</p>`;
    return `<div class="cal-layout">${monthGrid(data.utc_date)}
      <article class="events-card">
        ${both}
        <p>Неделя месяца ${esc(data.week_of_month)}.</p>
        <p>${data.month_end_card ? 'Карточка конца месяца включена.' : 'Карточка конца месяца выключена.'}</p>
        <p>${esc((data.chain || {}).text || '')}</p>
        <p class="events-service">${esc(data.note || '')}</p>
      </article></div>`;
  }

  // ---- загрузка режимов ------------------------------------------------------
  let currentMode = 'now';
  let requestVersion = 0;

  async function loadSide() {
    const box = document.getElementById('h1-events-context');
    if (!box || !window.HTF || document.getElementById('events-root')) return;
    if (!HTF.getToken()) {
      box.innerHTML = '<p>События и время появятся после входа.</p>';
      return;
    }
    try {
      renderSideMarket(await HTF.api('/api/events/overview'));
    } catch (err) {
      box.innerHTML = '<p>События и время сейчас недоступны.</p>';
    }
  }

  async function show(mode) {
    const body = document.getElementById('events-body');
    if (!body) return;
    const version = ++requestVersion;
    const status = document.getElementById('market-status');
    body.setAttribute('aria-busy', 'true');
    if (status) status.textContent = 'Обновляем данные…';
    const endpoint = { now: 'overview', feed: 'journal', calendar: 'calendar', rules: 'rules', sources: 'health' }[mode] || 'overview';
    try {
      if (!HTF.getToken()) await HTF.ensureToken('Нужен токен владельца.');
      if (version !== requestVersion) return;
      const data = await HTF.api(`/api/events/${endpoint}`);
      if (version !== requestVersion) return;
      let html;
      if (mode === 'feed') html = renderFeedBundles(data) || renderFeedLegacy(data);
      else if (mode === 'calendar') html = renderCalendarMarket(data.market) + renderCalendarLegacy(data)
        || `<p>${esc(data.empty_reason || 'Нет данных календаря.')}</p>`;
      else if (mode === 'sources') html = renderSources(data);
      else if (mode === 'rules') {
        const registry = data.registry || [];
        html = (data.note ? `<p class="events-service">${esc(data.note)}</p>` : '')
          + (registry.length ? registry.map(ruleRegistryCard).join('') : (data.rules || []).map((rule) =>
            `<article class="events-card"><h2>${esc(rule.text)}</h2><p>ID ${esc(rule.id)} · ${esc(CLASS_RU[rule.class] || rule.class || '—')}</p></article>`).join(''))
          + `<section class="events-card"><h2>Статистика</h2>${statsHtml(data.statistics)}</section>`;
      } else html = data.market ? renderNowMarket(data) : renderNowLegacy(data);
      body.innerHTML = html;
      body.dataset.mode = mode;
      if (status) status.textContent = 'Данные обновлены · время указано по МСК';
    } catch (err) {
      if (version !== requestVersion) return;
      if (body.dataset.mode !== mode) body.innerHTML = '<section class="events-card"><h2>Данные недоступны</h2><p>Повторите загрузку кнопкой «Обновить».</p></section>';
      if (status) status.textContent = `Не удалось обновить данные. ${err.message || 'Проверьте подключение.'}`;
    } finally {
      if (version === requestVersion) body.setAttribute('aria-busy', 'false');
    }
  }

  function refresh() {
    if (document.getElementById('events-root')) {
      show(currentMode).catch(() => {});
    } else {
      loadSide();
    }
  }

  function scheduleRefresh() {
    setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, 60000);
    document.addEventListener('visibilitychange', () => {
      if (document.visibilityState === 'visible') refresh();
    });
  }

  document.getElementById('market-refresh')?.addEventListener('click', refresh);

  // ---- инициализация ----------------------------------------------------------
  document.querySelectorAll('[data-mode]').forEach((button) => {
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', () => {
      document.querySelectorAll('[data-mode]').forEach((item) => {
        const on = item === button;
        item.classList.toggle('active', on);
        item.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      currentMode = button.getAttribute('data-mode') || 'now';
      show(currentMode).catch((err) => {
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
    // Глубокая ссылка на сохранённую оценку: /events.html?assessment=<id>
    const requested = new URLSearchParams(location.search).get('assessment');
    if (requested) loadSavedAssessment(requested).catch(() => {});
    show('now').catch((err) => {
      const body = document.getElementById('events-body');
      if (body) body.textContent = err.message || 'Не удалось загрузить события';
    });
    scheduleRefresh();
  } else if (document.getElementById('h1-events-context')) {
    loadSide();
    scheduleRefresh();
  }
})();
