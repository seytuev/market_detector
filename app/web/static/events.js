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
    long: 'В пользу роста', short: 'В пользу снижения', neutral: 'Без явного перевеса',
    mixed: 'Есть аргументы в обе стороны', unknown: 'Недостаточно данных',
    none: 'Без направления', conditional: 'Зависит от условий',
  };
  const GROUP_RU = { events: 'События', time: 'Время' };
  const QUALITY_RU = {
    ok: 'Данные полные', partial: 'Есть пробелы в данных', stale: 'Данные устарели',
    unavailable: 'Данные недоступны', unknown: 'Ещё не проверено',
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
    experimental: 'правило пробное, статистически не доказано',
    validated: 'проверено', unvalidated: 'правило ещё не проверено',
  };
  const SOURCE_RU = {
    liquidations_baseline: 'История для сравнения ликвидаций с нормой',
    event_history: 'Повторная проверка сохранённого события',
    price: 'Цены', liquidations: 'Ликвидации — принудительное закрытие позиций', oi: 'Открытый интерес',
    funding: 'Ставка финансирования', calendar: 'Календарь', basket: 'Набор монет',
    basket_history: 'История цен набора монет',
  };
  const BUNDLE_KIND_RU = { changes: 'Изменения', downtime_digest: 'Сводка после простоя' };
  const CHANNEL_RU = { feed: 'лента', telegram: 'telegram', push: 'push' };
  const DELIVERY_RU = {
    delivered: 'доставлено', sent: 'отправлено', pending: 'ожидает',
    unavailable: 'недоступна', failed: 'ошибка', shadow: 'теневая запись',
  };
  const UPCOMING_KIND_RU = { h1_close: 'Закрытие часа · проверка цены', liquidation_window_close: 'Закрытие окна ликвидаций 4ч', msk_day_boundary:'Плановый пересчёт · новый день МСК', day_boundary: 'Начало нового дня', fact_expiry: 'Окончание учёта фактора' };
  const RULE_NAME = {
    LIQ_PRESSURE: 'Давление из-за закрытия сделок', LIQ_RECOVERY: 'Восстановление после массового закрытия сделок',
    LIQ_FACT: 'Массовое закрытие сделок', TIME_WEEKDAY: 'Влияние дня недели',
    TIME_WINDOW: 'Влияние времени суток', SEQ_WEEK_START: 'Рост в начале недели',
    SRC_DEGRADED: 'Данные стали менее полными', SRC_RECOVERED: 'Данные снова доступны',
  };
  const RULE_DESCRIPTION = {
    LIQ_PRESSURE: 'Когда позиции на рост массово закрываются принудительно, а цены продолжают падать, это условие учитывается в пользу снижения.',
    LIQ_RECOVERY: 'Когда массовое закрытие позиций стихает, а цены восстанавливаются, это условие учитывается в пользу роста.',
    LIQ_FACT: 'Записывает массовое принудительное закрытие позиций. Само по себе событие не указывает на рост или снижение.',
    TIME_WEEKDAY: 'Проверяет влияние дня недели. Пока методика не подтверждена, это правило не меняет оценку рынка.',
    TIME_WINDOW: 'Проверяет влияние времени суток. Для расчёта нужны заданные часы и проверенная методика.',
    SEQ_WEEK_START: 'Проверяет рост в завершённые понедельник и вторник. Правило пробное: влияние на следующие дни ещё не доказано.',
    SRC_DEGRADED: 'Отмечает ухудшение качества данных. Само по себе не указывает на рост или снижение цены.',
    SRC_RECOVERED: 'Отмечает восстановление источников данных. Само по себе не указывает на рост или снижение цены.',
  };
  function readable(value) {
    const plain = { LONG: 'Условия в пользу роста', SHORT: 'Условия в пользу снижения',
      'Нейтрально': 'Нет явного перевеса', 'Противоречивый контекст': 'Есть аргументы в обе стороны',
      'Направление неизвестно': 'Недостаточно данных для оценки',
      'Крипторынок: направление неизвестно. Доступен фон BTC': 'Для оценки всего рынка мало данных. Доступна оценка биткоина.' };
    return plain[value] || String(value ?? '').replace(/Контекст поддерживает LONG/g, 'Условия в пользу роста')
      .replace(/Контекст поддерживает SHORT/g, 'Условия в пользу снижения')
      .replace(/\bLONG\b/g, 'рост').replace(/\bSHORT\b/g, 'снижение')
      .replace(/лонг-ликвидаций/gi, 'принудительных закрытий позиций на рост')
      .replace(/Фон BTC/g, 'Ситуация с биткоином').replace(/Направление по событиям и времени/g, 'Оценка влияния событий и времени на рынок')
      .replace(/Контекст противоречивый/g, 'События и время дают разные оценки');
  }
  const copy = (value) => esc(readable(value));
  const ruleName = (id) => RULE_NAME[id] || RULE_TITLE[id] || 'Рыночное событие';

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

  function qualityWord(quality) {
    return QUALITY_RU[quality] || quality || 'неизвестно';
  }

  function directionClass(direction) {
    return { long: 'is-long', short: 'is-short', neutral: 'is-neutral', mixed: 'is-mixed' }[direction]
      || 'is-unknown';
  }

  // ---- общие блоки рыночного снимка ---------------------------------------
  // Итог приходит с сервера; меняем только подписи, сохраняя смысл оценки.
  // Цвет направления всегда дублируется словами.
  function stateChip(market) {
    const direction = (market && market.direction) || 'unknown';
    const glyph = ARROW_GLYPH[(market && market.arrow)] || '';
    const raw = String((market && market.market_line) || 'Направление неизвестно');
    const line = glyph ? raw.replace(/^[↑↓↔！]+\s*/, '') : raw;
    const text = glyph ? `${glyph} ${readable(line)}` : readable(line);
    return `<span class="side-state ${directionClass(direction)}">${copy(text)}</span>`;
  }

  function conflictHtml(market) {
    if (!market || !market.conflict_note) return '';
    return `<p class="events-conflict">${copy(market.conflict_note)}</p>`;
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
    const side = item.side === 'long' ? 'За рост' : item.side === 'short' ? 'За снижение' : 'Без перевеса';
    const text = readable(item.text || '').replace(/^(События|Время):\s*/, '');
    return `<div class="market-reason ${directionClass(item.side)}"><span class="market-reason-label">${esc(group)} · ${side}</span><p class="events-reason">${esc(text)}</p></div>`;
  }

  function factLine(fact) {
    const state = FACT_STATE_RU[fact.state] || fact.state || '—';
    const dir = DIRECTION_WORD[fact.direction] || 'Без направления';
    const status = { driving: 'Учтён в показанной оценке', background: 'Сохранено в контексте; сейчас не определяет направление',
      invalidated: 'Направленная трактовка отменена; факт события сохранён',
      awaiting_confirmation: 'Прежнее давление снято; новое направление пока не подтверждено',
      unverified: 'Сохранено; текущее влияние нельзя проверить', non_directional: 'Факт объёма; направление не задаёт' }[fact.context_status];
    return `<li class="market-fact ${directionClass(fact.direction)}"><div class="market-fact-head"><strong>${esc(ruleName(fact.effective_rule_id || fact.rule_id))}</strong><span class="market-tag">${esc(dir)}</span></div>
      ${fact.summary ? `<p>${copy(fact.summary)}</p>` : ''}
      <p class="events-service">${esc(state)} · ${esc(fmtMsk(fact.occurred_at))}</p>
      ${status ? `<p class="events-service">${esc(status)}</p>` : ''}
      ${fact.invalidation_reason ? `<p class="events-service">${esc(fact.invalidation_reason)}${fact.invalidated_at ? ` · ${esc(fmtMsk(fact.invalidated_at))}` : ''}</p>` : ''}
      ${fact.phase_at && fact.phase_at !== fact.occurred_at ? `<p class="events-service">Трактовка обновлена по данным на ${esc(fmtMsk(fact.phase_at))}</p>` : ''}
      ${fact.valid_until ? `<p class="events-service">В памяти модели до ${esc(fmtMsk(fact.valid_until))}</p>` : ''}
      <details class="market-technical" data-persist="fact-${esc(fact.fact_key || `${fact.rule_id}-${fact.occurred_at}`)}"><summary>Данные события</summary>
        ${fact.first_detected_at ? `<p>Первое обнаружение: ${esc(fmtMsk(fact.first_detected_at))}</p>` : ''}
        ${fact.available_at ? `<p>Последнее получение данных: ${esc(fmtMsk(fact.available_at))}</p>` : ''}
        ${fact.coverage && fact.coverage.total ? `<p>Покрытие события: ${esc(fmtNum(fact.coverage.ok))} из ${esc(fmtNum(fact.coverage.total))} монет</p>` : ''}
        ${fact.current_check && fact.current_check.as_of ? `<p>Текущие цены проверены на ${esc(fmtMsk(fact.current_check.as_of))}</p>` : ''}
        <p>Код правила: ${esc(fact.rule_id || '—')}</p></details></li>`;
  }

  function factsListHtml(facts) {
    const list = facts || [];
    if (!list.length) return '<p class="market-empty">В доступной памяти модели пока нет событий.</p>';
    const more = list.length > 3 ? `<details class="market-more" data-persist="more-facts"><summary>Все факторы · ещё ${list.length - 3}</summary><ul>${list.slice(3).map(factLine).join('')}</ul></details>` : '';
    return `<ul>${list.slice(0, 3).map(factLine).join('')}</ul>${more}`;
  }

  // Частота и калиброванная вероятность подписаны раздельно; калиброванная
  // показывается только при kind == 'calibrated_probability' (AC13).
  function statsHtml(stats) {
    if (!stats || !stats.available) {
      return `<p class="market-empty">${esc((stats && stats.text) || 'Статистика недостаточна')}</p>`;
    }
    const label = stats.kind === 'calibrated_probability'
      ? 'калиброванная вероятность' : 'историческая частота';
    const n = stats.n != null ? ` · наблюдений: ${fmtNum(stats.n)}` : '';
    const unc = stats.uncertainty
      ? ` · интервал ${fmtNum(stats.uncertainty.low * 100)}–${fmtNum(stats.uncertainty.high * 100)}%` : '';
    const period = stats.period && typeof stats.period === 'object'
      ? `${fmtMsk(stats.period.from)} — ${fmtMsk(stats.period.to)}` : stats.period;
    return `<p>${copy(stats.text || '')}</p>
      <p class="events-service">${esc(label)}${esc(n)}${esc(unc)}${period ? esc(` · период ${period}`) : ''}</p>`;
  }

  function factorAgreementHtml(agreement) {
    if (!agreement) return '';
    const label = agreement.label || 'Согласованность факторов';
    return `<div class="market-votes"><span class="events-service">${esc(label)}</span><span>За рост <strong>${esc(fmtNum(agreement.long))}</strong></span><span>За снижение <strong>${esc(fmtNum(agreement.short))}</strong></span></div><p class="events-service">Количество учтённых условий, а не вероятность движения цены.</p>`;
  }

  function unknownDepsHtml(deps) {
    const list = deps || [];
    if (!list.length) return '';
    const names = list.map((name) => SOURCE_RU[name] || name).join(', ');
    return `<p class="events-service">Не удалось проверить: ${esc(names)}</p>`;
  }

  function btcBackgroundHtml(market) {
    const bg = market && market.btc_background;
    if (!bg) return '';
    const note = bg.direction_note || {};
    const evidence = bg.evidence;
    const joint = evidence ? `<p>${esc(evidence.summary)}</p><p class="events-service">${esc(evidence.scope_note)}</p>
      <p class="events-service">${esc(evidence.method_note)} В общее направление корзины этот блок не голосует.</p>
      ${(evidence.recent_events || []).slice(-3).reverse().map(item => `<p class="events-service">${esc(fmtMsk(item.day_start))}: ${item.state === 'cascade_oi_rising' ? 'каскад и рост OI' : item.state === 'cascade_oi_falling' ? 'каскад и снижение OI' : 'каскад без изменения OI'}</p>`).join('')}` : '';
    const side = DIRECTION_WORD[note.side] || 'Без направления';
    return `<section class="events-card market-bitcoin">
      <h2>Биткоин: ликвидации и открытый интерес</h2>
      <p class="events-service">Оценка только биткоина. Другие монеты могут двигаться иначе.</p>
      ${bg.stale || bg.data_stale ? '<p class="events-service">Прежняя оценка BTC не подтверждена свежими сопоставимыми данными.</p>' : note.side ? `<p>${esc(side)}</p>${note.text ? `<p>${copy(note.text)}</p>` : ''}` : ''}
      ${joint}
    </section>`;
  }

  // ---- компактная карточка (#h1-events-context, §8.1) ----------------------
  function renderSideMarket(data) {
    const box = document.getElementById('h1-events-context');
    if (!box) return;
    const market = data.market;
    if (!market) { renderSideLegacy(data); return; }

    const stale = data.market_stale
      ? '<p class="events-warn">Снимок устарел — показана последняя сохранённая оценка.</p>' : '';
    box.innerHTML = `<div class="side-card-head"><span class="market-orbit" aria-hidden="true">↗</span><strong>Рыночный фон</strong><span class="market-snapshot-age">${data.market_age_seconds != null ? esc(fmtAge(data.market_age_seconds)) : ''}</span></div>
      <p class="side-market-line">${stateChip(market)}</p>
      ${stale}
      <a class="btn small side-open" href="/events.html">Открыть рынок</a>`;
    const overview = document.getElementById('now-market');
    if (overview) {
      const reasons = pickReasons(market);
      overview.dataset.direction = market.direction || 'unknown';
      overview.innerHTML = `<div class="market-hero-main"><div class="market-eyebrow">Рыночный фон · крипторынок</div>` +
        `<h2>${stateChip(market)}</h2>` +
        `<p class="market-hero-meta">${data.market_age_seconds != null ? 'Снимок ' + esc(fmtAge(data.market_age_seconds)) : 'Время снимка не получено'}` +
        (market.coverage?.total != null ? ` · Данные ${esc(market.coverage.ok)} из ${esc(market.coverage.total)} монет` : '') + `</p>${stale}` +
        `<a class="btn side-open" href="/events.html">Разобрать рынок <span aria-hidden="true">↗</span></a></div>` +
        `<div class="market-hero-reasons"><div class="market-eyebrow">Что влияет сейчас</div>` +
        (reasons.length ? reasons.map(reasonLine).join('') : '<p>Причин для направленной оценки пока недостаточно.</p>') +
        (market.conflict_note ? `<p class="events-conflict">${copy(market.conflict_note)}</p>` : '') + '</div>';
    }
  }

  // ---- полный раздел: «Сейчас» --------------------------------------------
  function groupDirectionHtml(direction) {
    const word = DIRECTION_WORD[direction] || 'неизвестно';
    return `<p class="market-direction">${esc(word)}</p>`;
  }

  function upcomingReviewsHtml(market) {
    const items = [];
    if (market.next_review_at) {
      items.push(`<li><strong>Следующая проверка</strong><span>${esc(fmtMsk(market.next_review_at))}</span></li>`);
    }
    const expiries = (market.active_facts || [])
      .filter((fact) => fact && fact.valid_until && fact.used_for_direction !== false)
      .sort((a, b) => a.valid_until - b.valid_until)
      .slice(0, 3)
      .map((fact) => `<li><strong>${esc(ruleName(fact.effective_rule_id || fact.rule_id))}</strong><span>Срок памяти модели до ${esc(fmtMsk(fact.valid_until))}</span></li>`);
    items.push(...expiries);
    if (!items.length) return '';
    return `<section class="events-card market-upcoming"><h2>Ближайший пересмотр оценки</h2><p class="events-service">Плановые проверки и сроки памяти модели.</p><ul class="market-timeline">${items.join('')}</ul></section>`;
  }

  function renderNowMarket(data) {
    const market = data.market;
    const coverage = market.coverage;
    const coverageLine = coverage && coverage.total != null
      ? `<p>Проверено монет: <strong>${esc(fmtNum(coverage.ok))} из ${esc(fmtNum(coverage.total))}</strong></p><p class="events-service">${coverage.sufficient ? 'Данных по монетам достаточно для общей оценки.' : 'Монет с данными слишком мало для полной картины рынка.'}</p>`
      : '<p class="events-service">Число проверенных монет пока неизвестно.</p>';
    const age = data.market_age_seconds != null ? ` · обновлено ${fmtAge(data.market_age_seconds)}` : '';
    const stale = data.market_stale
      ? '<p class="events-warn">Оценка устарела. Показан последний сохранённый результат.</p>' : '';
    const legacy = '';

    const facts = market.active_facts || [];
    const liqCoverage = market.liquidations && market.liquidations.coverage;
    const liqLine = liqCoverage && liqCoverage.total != null
      ? `<p>Ликвидации: <strong>${esc(fmtNum(liqCoverage.ok))} из ${esc(fmtNum(liqCoverage.total))} монет</strong></p><p class="events-service">Binance · завершённое окно 4ч. ${market.liquidations.baseline_ok ? 'Истории для сравнения с нормой достаточно.' : 'Истории для сравнения с нормой недостаточно.'}</p>` : '';
    const memoryDays = market.memory_policy && Number(market.memory_policy.hours) / 24;
    const method = market.methodology;
    const methodHtml = method ? `<section class="events-card market-methodology"><h2>Как получена оценка</h2><p>${esc(method.label)}</p><p class="events-service">${esc(method.study_scope)}</p>${(method.limitations || []).map(text => `<p class="events-service">${esc(text)}</p>`).join('')}</section>` : '';
    const ratio = coverage && coverage.ok != null && Number.isFinite(Number(coverage.ok)) && Number(coverage.total) > 0 && Number.isFinite(Number(coverage.total))
      ? Math.max(0, Math.min(100, Math.round(Number(coverage.ok) / Number(coverage.total) * 100))) : null;
    const groupCard = (label, direction, symbol, description, kind) => `<section class="events-card market-factor market-${kind} ${directionClass(direction)}">
      <div class="market-card-head"><span class="market-icon" aria-hidden="true">${symbol}</span><h2>${label}</h2></div>
      ${groupDirectionHtml(direction)}<p class="events-service">${description}</p></section>`;
    return `<section class="events-card market-hero ${directionClass(market.direction)}">
        <p class="market-eyebrow">${data.market_stale ? 'Сохранённая оценка' : 'Общая картина'} · Крипторынок</p>
        <div class="events-side-state">${stateChip(market)}</div>
        <p class="events-line">${copy(market.subtitle || 'Оценка влияния событий и времени на рынок')}</p>
        ${market.methodology ? '<p class="events-service">Направленные правила этой модели пока не прошли статистическую проверку.</p>' : ''}
        ${conflictHtml(market)}
        <div class="market-reasons"><h2>Что повлияло на оценку</h2>${pickReasons(market).map(reasonLine).join('') || `<p class="events-service">${market.direction === 'neutral' ? 'Учтённые условия не дают явного перевеса в пользу роста или снижения.' : 'Причины оценки пока недоступны.'}</p>`}</div>
        <p class="events-service">Проверено ${esc(fmtMsk(market.evaluated_at))}${esc(age)}</p>${stale}
      </section>
      <div class="market-metrics">
        ${groupCard('Что происходит на рынке', market.events_direction, '↗', 'Как недавние события влияют на общую оценку.', 'events')}
        ${groupCard('Влияние времени', market.time_direction, '◷', market.time_rules_active === false ? 'Правила направления по времени пока не включены. Календарь показан справочно.' : 'Что дают условия дня недели и времени суток по Москве.', 'time')}
        <section class="events-card market-quality"><div class="market-card-head"><span class="market-icon" aria-hidden="true">◉</span><h2>Насколько полны данные</h2></div>
          <p class="market-quality-value">${esc(qualityWord(market.quality))}</p>
          ${coverageLine}${ratio != null ? `<div class="market-coverage"><meter min="0" max="100" value="${ratio}" aria-label="Доля монет с данными">${ratio}%</meter><span>${ratio}%</span></div>` : ''}
          ${liqLine}${unknownDepsHtml(market.unknown_dependencies)}</section>
      </div>
      <div class="market-detail-grid">
        <section class="events-card market-facts"><p class="market-eyebrow">Память событий</p><h2>Что сохраняется в контексте <span class="market-count">${facts.length}</span></h2>
          <p class="events-service">${memoryDays ? `Память модели — ${esc(fmtNum(memoryDays))} дней от события.` : 'Показаны события в пределах настроенного срока памяти.'} Более поздняя фаза уточняет направление; исходное событие сохраняется. Срок памяти не доказывает исчезновение рыночного эффекта.</p>${factsListHtml(facts)}</section>
        <div>${upcomingReviewsHtml(market)}${btcBackgroundHtml(market)}
          <section class="events-card market-statistics"><h2>Что показывают прошлые наблюдения</h2>${statsHtml(market.statistics)}${factorAgreementHtml(market.factor_agreement)}</section>${methodHtml}</div>
      </div>
      <p class="market-footnote">Оценка показывает, какие условия сейчас в пользу роста или снижения. Это не прогноз цены и не команда открыть сделку.</p>
      ${legacy}`;
  }

  // ---- полный раздел: «Лента» ----------------------------------------------
  function bundleCard(bundle) {
    const kind = BUNDLE_KIND_RU[bundle.kind] || bundle.kind || 'Изменения';
    const publications = (bundle.publications || []).map((pub) => {
      const link = pub.assessment_id
        ? ` · <a href="/events.html?assessment=${encodeURIComponent(pub.assessment_id)}">оценка</a>` : '';
      return `<li>${copy(pub.text || '')} <span class="events-service">${esc(fmtMsk(pub.created_at))}</span>${link}</li>`;
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
      <p class="events-line">${copy(assessment.market_line || '')}</p>
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
  let calendarMonth = '', calendarDay = '', calendarData = null;
  const mskDate = ms => new Date(Number(ms)+10800000).toISOString().slice(0,10);
  function renderCalendarMarket(market) {
    if (!market) return '';
    calendarData = market;
    if (!calendarDay) calendarDay = mskDate(Date.now());
    if (!calendarMonth) calendarMonth = calendarDay.slice(0,7);
    const tc = market.time_context || {}, days = tc.completed_days || [];
    const upcoming = market.upcoming || [];
    const dayMove = sign => sign == null ? 'Нет данных' : Number(sign)>0 ? 'Рост' : Number(sign)<0 ? 'Снижение' : 'Без изменения';
    const [year,month] = calendarMonth.split('-').map(Number);
    const count = new Date(Date.UTC(year,month,0)).getUTCDate();
    const offset = (new Date(Date.UTC(year,month-1,1)).getUTCDay()+6)%7;
    const monthLabel = new Date(Date.UTC(year,month-1,1)).toLocaleDateString('ru-RU',{month:'long',year:'numeric',timeZone:'UTC'});
    const cells = Array.from({length:count},(_,i) => {
      const date = `${calendarMonth}-${String(i+1).padStart(2,'0')}`;
      const completed = days.find(day => day.day_start_ms && mskDate(day.day_start_ms) === date);
      const n = upcoming.filter(item => (item.at || item.at_ms) && mskDate(item.at || item.at_ms) === date).length;
      const move = completed ? dayMove(completed.sign) : '';
      const glyph = completed ? completed.sign > 0 ? '↑' : completed.sign < 0 ? '↓' : '↔' : '';
      return `<button type="button" class="btn" data-calendar-day="${date}" aria-pressed="${date===calendarDay}" aria-label="${date}${move ? ' · '+move : n ? ' · Событий: '+n : ''}">${i+1}${completed ? `<small title="${move}">${glyph}</small>` : n ? `<small title="Запланированные события">• ${n}</small>` : ''}</button>`;
    }).join('');
    const agenda = upcoming.filter(item => (item.at || item.at_ms) && mskDate(item.at || item.at_ms) === calendarDay);
    const completed = days.find(day => day.day_start_ms && mskDate(day.day_start_ms) === calendarDay);
    const list = agenda.map(item => `<li><strong>${esc(UPCOMING_KIND_RU[item.kind] || 'Пересмотр оценки')}</strong><p>${esc(fmtMsk(item.at || item.at_ms))}</p>${item.fact_key ? '<small>Область: рыночный фактор</small>' : ''}</li>`).join('');
    return `<article class="events-card"><div class="lf-calendar-head"><button type="button" class="btn" data-calendar-shift="-1" aria-label="Предыдущий месяц">←</button><h2>${esc(monthLabel)}</h2><button type="button" class="btn" data-calendar-shift="1" aria-label="Следующий месяц">→</button></div><div class="lf-calendar-grid">${['Пн','Вт','Ср','Чт','Пт','Сб','Вс'].map(day=>`<span>${day}</span>`).join('')}${'<span></span>'.repeat(offset)}${cells}</div><section class="lf-calendar-agenda"><h3>${esc(new Date(calendarDay+'T12:00:00+03:00').toLocaleDateString('ru-RU',{day:'numeric',month:'long',year:'numeric',timeZone:'Europe/Moscow'}))}</h3>${completed ? `<p>Завершённый день: ${dayMove(completed.sign)}</p>${completed.median_return != null ? `<p>Медианное изменение цены: ${esc(fmtNum(Number(completed.median_return)*100))}%</p>` : ''}` : ''}${list ? `<ul>${list}</ul>` : '<p>Нет запланированных изменений оценки на выбранный день.</p>'}</section><p class="events-service">Все даты и границы дня указаны по МСК. Будущие ликвидации заранее неизвестны.</p></article>`;
  }

  // ---- полный раздел: «Правила и статистика» --------------------------------
  function ruleRegistryCard(rule) {
    const cls = RULE_CLASS_RU[rule.rule_class] || rule.rule_class || '—';
    const confirmation = CONFIRMATION_RU[rule.confirmation] || rule.confirmation || '—';
    const enabled = rule.enabled === false ? 'выключено' : 'включено';
    const direction = DIRECTION_WORD[rule.direction] || 'Без направления';
    return `<article class="events-card market-rule">
      <h2>${esc(ruleName(rule.rule_id))}</h2>
      <p><span class="market-tag">${esc(direction)}</span> <span class="market-tag">${rule.enabled === false ? 'Не учитывается' : 'Включено'}</span></p>
      <p class="events-service">${esc(cls)} · ${esc(confirmation)}</p>
      <p>${copy(RULE_DESCRIPTION[rule.rule_id] || rule.explanation || 'Описание условия пока недоступно.')}</p>
      <details class="market-technical" data-persist="rule-${esc(rule.rule_id)}"><summary>Как проверяется условие</summary>
      <p>Код: ${esc(rule.rule_id || '—')} · ${esc(enabled)}</p>
      <p>Период проверки: ${copy(rule.window || '—')}</p>
      ${rule.explanation && RULE_DESCRIPTION[rule.rule_id] ? `<p>${copy(rule.explanation)}</p>` : ''}
      ${rule.origin ? `<p>Источник правила: ${esc(({project_default: 'Начальные настройки проекта', owner_answer: 'Методика владельца', previous_spec: 'Предыдущая методика'})[rule.origin] || rule.origin)}</p>` : ''}
      ${rule.formula ? `<p>Условие: ${copy(rule.formula)}</p>` : ''}</details>
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
        <span class="events-service">обновлено ${esc(fmtMsk(src.updated_at))}${esc(age)}</span>
        ${capability ? '<p class="events-warn">Источник не предоставляет необходимые данные.</p>' : ''}
        ${src.detail ? `<details class="market-technical" data-persist="source-${esc(name)}"><summary>Подробнее об источнике</summary><p>${esc(src.detail)}</p></details>` : ''}
      </div>`;
    }).join('');
    return `<article class="events-card market-sources">
      <h2>Источники данных</h2>
      <p>Качество: ${esc(qualityWord(data.quality))}</p>
      <p class="events-service">${esc(data.detail || '')}</p>
      ${rows || '<p>Детализация по источникам пока недоступна.</p>'}
    </article>`;
  }

  // ---- legacy-экраны (когда overview.market отсутствует) ---------------------
  const GATE_STATUS = {
    passed: 'выполнено',
    blocked: 'Проверка условия заблокирована',
    not_applicable: 'Условие не применимо',
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
      <a class="btn small side-open" href="/events.html">Открыть контекст рынка</a>`;
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
  const pendingRequests = new Map();
  function requestData(endpoint) {
    if (pendingRequests.has(endpoint)) return pendingRequests.get(endpoint);
    const request = HTF.api(`/api/events/${endpoint}`).finally(() => pendingRequests.delete(endpoint));
    pendingRequests.set(endpoint, request);
    return request;
  }

  async function loadSide() {
    const box = document.getElementById('h1-events-context');
    if (!box || !window.HTF || document.getElementById('events-root')) return;
    try {
      await HTF.ensureToken();
      renderSideMarket(await HTF.api('/api/events/overview'));
      const overview = document.getElementById('now-market');
      if (overview && !overview.querySelector('.market-hero-main')) overview.innerHTML = box.innerHTML;
    } catch (err) {
      box.innerHTML = '<p>Рыночный фон сейчас недоступен. <a href="/events.html">Открыть рынок</a></p>';
      const overview = document.getElementById('now-market');
      if (overview) overview.innerHTML = box.innerHTML;
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
    let data = null;
    try {
      await HTF.ensureToken();
      if (version !== requestVersion) return;
      data = await requestData(endpoint);
      if (version !== requestVersion) return;
      let html;
      if (mode === 'feed') html = renderFeedBundles(data) || renderFeedLegacy(data);
      else if (mode === 'calendar') html = (data.market ? renderCalendarMarket(data.market) : renderCalendarLegacy(data))
        || `<p>${esc(data.empty_reason || 'Нет данных календаря.')}</p>`;
      else if (mode === 'sources') html = renderSources(data);
      else if (mode === 'rules') {
        const registry = data.registry || [];
        html = (data.note ? `<p class="events-service">${esc(data.note)}</p>` : '')
          + (registry.length ? registry.map(ruleRegistryCard).join('') : (data.rules || []).map((rule) =>
            `<article class="events-card"><h2>${esc(rule.text)}</h2><p>ID ${esc(rule.id)} · ${esc(CLASS_RU[rule.class] || rule.class || '—')}</p></article>`).join(''))
          + `<section class="events-card"><h2>Статистика</h2>${statsHtml(data.statistics)}</section>`;
      } else html = data.market ? renderNowMarket(data) : '<section class="events-card"><h2>Оценка рынка недоступна</h2><p>Недостаточно данных для общей оценки. Прежняя методика BTC доступна в справке.</p><a href="/#help">Открыть справку</a></section>';
      // Сохраняем раскрытые детали при обновлении; одинаковый результат не перерисовываем.
      if (body.dataset.mode !== mode || body._marketHtml !== html) {
        const opened = body.dataset.mode === mode
          ? [...body.querySelectorAll('details[open][data-persist]')].map(item => item.dataset.persist) : [];
        body.innerHTML = html;
        body.querySelectorAll('details[data-persist]').forEach(item => { item.open = opened.includes(item.dataset.persist); });
        body._marketHtml = html;
      }
      body.dataset.mode = mode;
      if (status) status.textContent = data.market_stale ? 'Показана сохранённая оценка · устарела · время МСК' : 'Состояние загружено · время указано по МСК';
    } catch (err) {
      if (version !== requestVersion) return;
      if (body.dataset.mode !== mode) {
        body.innerHTML = '<section class="events-card"><h2>Данные недоступны</h2><p>Повторите загрузку кнопкой «Обновить».</p></section>';
        body._marketHtml = null;
      }
      const saved = data && data.market && data.market.evaluated_at ? fmtMsk(data.market.evaluated_at) : '';
      if (status) {
        status.textContent = saved
          ? `Обновление не завершено. Показаны данные на ${saved}. Повторить`
          : 'Обновление не завершено. Повторить';
      }
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
  document.getElementById('events-body')?.addEventListener('click', event => {
    const day = event.target.closest('[data-calendar-day]'), shift = event.target.closest('[data-calendar-shift]');
    if (!day && !shift) return;
    if (day) calendarDay = day.dataset.calendarDay;
    if (shift) { const [year,month] = calendarMonth.split('-').map(Number);
      calendarMonth = new Date(Date.UTC(year,month-1+Number(shift.dataset.calendarShift),1)).toISOString().slice(0,7); }
    const body = document.getElementById('events-body'); body.innerHTML = renderCalendarMarket(calendarData); body._marketHtml = body.innerHTML;
  });
  window.addEventListener('popstate', () => {
    if (!document.getElementById('events-root')) return;
    currentMode = new URLSearchParams(location.search).get('tab') === 'calendar' ? 'calendar' : 'now';
    document.querySelectorAll('[data-mode]').forEach(btn => { btn.classList.toggle('active',btn.dataset.mode === currentMode); btn.setAttribute('aria-pressed',String(btn.dataset.mode === currentMode)); });
    show(currentMode);
  });
  const referenceRequests = new Map();
  async function loadReference(id, endpoint, renderer) {
    const el = document.getElementById(id);
    if (!el || referenceRequests.has(id)) return;
    const request = (async () => {
      el.setAttribute('aria-busy','true');
      try { await HTF.ensureToken(); el.innerHTML = renderer(await requestData(endpoint)); }
      catch (err) { el.innerHTML = '<p>Не удалось прочитать данные. <button type="button" class="btn">Повторить</button></p>'; el.querySelector('button').onclick = () => loadReference(id,endpoint,renderer); }
      finally { el.setAttribute('aria-busy','false'); referenceRequests.delete(id); }
    })();
    referenceRequests.set(id,request); return request;
  }
  function loadMethodology() {
    return loadReference('market-methodology-body','rules', data => (data.registry || []).map(ruleRegistryCard).join('') || '<p>Описание правил пока недоступно.</p>');
  }
  function loadHealth() { return loadReference('market-sources-body','health',renderSources); }
  document.addEventListener('lf-view', event => {
    if (event.detail.name === 'help') loadMethodology();
    if (event.detail.name === 'settings') loadHealth();
  });
  document.getElementById('market-sources-refresh')?.addEventListener('click',loadHealth);
  document.getElementById('help-archive')?.addEventListener('toggle', event => {
    if (event.target.open) loadReference('market-archive-body','overview', data => `<p class="events-warn">Архивная методика BTC. Не входит в текущую общую оценку рынка.</p>${renderNowLegacy(data)}`);
  });
  if (document.getElementById('view-help')?.classList.contains('active')) loadMethodology();
  if (document.getElementById('view-settings')?.classList.contains('active')) loadHealth();

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
      const url = new URL(location.href); url.searchParams.set('tab',currentMode); history.pushState(null,'',url.pathname+url.search);
      show(currentMode).catch((err) => {
        const body = document.getElementById('events-body');
        if (body) body.textContent = err.message || 'Не удалось загрузить события';
      });
    });
  });

  if (document.getElementById('events-root')) {
    currentMode = new URLSearchParams(location.search).get('tab') === 'calendar' ? 'calendar' : 'now';
    const initial = document.querySelector(`[data-mode="${currentMode}"]`);
    if (initial) {
      initial.classList.add('active');
      initial.setAttribute('aria-pressed', 'true');
    }
    // Глубокая ссылка на сохранённую оценку: /events.html?assessment=<id>
    const requested = new URLSearchParams(location.search).get('assessment');
    if (requested) loadSavedAssessment(requested).catch(() => {});
    show(currentMode).catch((err) => {
      const body = document.getElementById('events-body');
      if (body) body.textContent = err.message || 'Не удалось загрузить события';
    });
    scheduleRefresh();
  } else if (document.getElementById('h1-events-context')) {
    loadSide();
    scheduleRefresh();
  }
})();
