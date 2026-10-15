/* Shared synchronization control and snapshot recovery. */
window.LFDataSync = (() => {
  let reload = null;
  let selected = () => null;
  let syncStale = null;   // (versions, {immediate}) => Promise — точечный рефетч изменённых доменов (§14)
  let onRecover = null;   // хук после объединённого recovery (AC17)
  let activeRead = null;
  let lastRead = 0;
  let statusRead = null;
  let statusData = null;
  let timer = null;
  let nextAllowed = 0;
  let lastCompletedJob = null;
  let previousBusy = false;
  let recovering = null;   // single-flight объединённого восстановления (AC17)
  let lastStatusOkAt = 0;  // последняя успешная проверка свежести (§15)
  let displayError = null; // ошибка обновления экрана — отдельно от итога задания (§15)
  const reasons = {
    missing_close: 'ожидается закрытая свеча', missing_current: 'ожидается текущая свеча',
    history_gap: 'пропуски истории', processing_lag: 'обработка не завершена',
    source_error: 'источник недоступен', sync_stale: 'давно не было синхронизации',
    quote_stale: 'котировка устарела', no_quote: 'нет котировки',
    quote_processing_lag: 'цена получена; обработка котировок ещё идёт',
    h1_processor_unavailable: 'обработчик H1 недоступен', deadline_exceeded: 'превышено время ожидания',
    interrupted_by_restart: 'обновление прервано перезапуском',
    deadline_exceeded_processing_continues: 'превышено время ожидания; обработка продолжается',
  };
  // Этапы ручного задания (§15): job.stages из статуса
  const STAGE_RU = {
    quotes: 'Котировки', fresh_candles: 'Свежие свечи',
    calculation: 'Расчёт', history: 'Восстановление истории',
  };
  const STAGE_STATUS_RU = {
    pending: 'ожидает', running: 'выполняется…', completed: 'готово', failed: 'ошибка',
    waiting_source: 'Ожидание источника', waiting_calculation: 'Ожидание расчёта',
  };
  const describe = (value) => reasons[value] || value || 'нет данных';
  const format = (at) => new Date(at).toLocaleTimeString('ru-RU', { timeZone: 'Europe/Moscow', hour12: false }) + ' МСК';
  const busy = (job) => job && (['queued', 'running'].includes(job.status) || job.processing_continues);
  const stageText = (s) => `${STAGE_RU[s.name] || s.name}: ${STAGE_STATUS_RU[s.status] || s.status}`;

  function paint() {
    const button = document.getElementById('btn-data-sync');
    const label = document.getElementById('data-sync-status');
    const detail = document.getElementById('data-sync-details');
    if (!button || !label || !statusData) return;
    const job = statusData.job;
    const recovery = (statusData.recovery || []).filter((r) => r.instrument_id === selected());
    const running = busy(job);
    button.disabled = running || !statusData.available || Date.now() < nextAllowed;
    button.textContent = running ? 'Обновление…' : 'Обновить данные';
    button.setAttribute('aria-busy', String(!!running));
    let message = 'Свечи и котировки всех включённых активов';
    if (!statusData.available) message = 'Обновление недоступно: обработчик данных не работает';
    else if (running) {
      message = `Обновление: ${job.channels_completed} из ${job.channels_total}` +
        (job.processing_continues ? ' · обработка продолжается' : '');
      // §15: текущий этап задания в строке статуса
      const active = (job.stages || []).find((s) => !['completed', 'failed'].includes(s.status));
      if (active) message += ` · ${stageText(active)}`;
    }
    else if (job) {
      if (job.status === 'succeeded') message = `Данные обновлены в ${format(job.finished_at)}`;
      else if (job.status === 'partial') message = recovery.length && job.results.some((r) => r.source_status === 'ok') ?
        'Свечи и цены получены; расчёт LTF продолжается' :
        `Обновлено частично: ${job.results.filter((r) => r.status === 'ok').length} из ${job.channels_total}`;
      else if (job.status === 'failed') message = 'Не удалось обновить данные';
    }
    const iid = selected();
    const channels = (statusData.channels || []).filter((c) => c.instrument_id === iid);
    const instrument = (statusData.instruments || []).find((i) => i.instrument_id === iid);
    if (!running && iid && instrument) {
      const lag = channels.filter((c) => c.status !== 'ok');
      message += lag.length ? ' · есть задержка данных' : ' · выбранный актив актуален';
      if (instrument.last_full_success_at) message += ` · синхронизация ${format(instrument.last_full_success_at)}`;
    }
    if (recovery.length) {
      const r = recovery[0];
      message += ` · Исторический расчёт LTF` + (r.total ?
        `: ${Math.floor(100 * r.processed / r.total)}% (${r.processed.toLocaleString('ru-RU')} из ${r.total.toLocaleString('ru-RU')} H1)` : ' выполняется');
    }
    label.textContent = message;
    label.dataset.state = running ? 'running' : job && job.status || 'idle';
    if (detail) {
      detail.replaceChildren();
      for (const r of recovery) {
        const row = document.createElement('div');
        row.textContent = 'Свечи загружены. Пересчитываем структуру и сценарии LTF по истории.' +
          (r.through ? ` Обработано по ${new Date(r.through).toLocaleString('ru-RU', { timeZone: 'Europe/Moscow' })} МСК.` : '') +
          ' Новые условия входа появятся после завершения расчёта.';
        detail.append(row);
      }
      // §15: этапы задания (котировки → свежие свечи → восстановление истории)
      // с различением waiting_source / waiting_calculation
      for (const s of (job && job.stages) || []) {
        const row = document.createElement('div');
        row.dataset.stage = s.name;
        row.textContent = stageText(s);
        detail.append(row);
      }
      for (const c of channels) {
        const row = document.createElement('div');
        const syncTime = c.last_success_at ? format(c.last_success_at) : 'ещё не было';
        const error = c.error ? ` · ${describe(c.error)}` : '';
        row.textContent = `${c.symbol} ${c.timeframe}: ${c.status === 'ok' ? 'актуально' : describe(c.reason)} · синхронизация ${syncTime}${error}`;
        row.title = `Последняя попытка: ${c.last_attempt_at ? format(c.last_attempt_at) : '—'}\n` +
          `Ожидаемое закрытие: ${c.expected_close ? HTF.fmtTime(c.expected_close) : '—'}\n` +
          `Получено: ${c.received_close ? HTF.fmtTime(c.received_close) : '—'}\n` +
          `Обработано: ${c.processed_close ? HTF.fmtTime(c.processed_close) : '—'}`;
        detail.append(row);
      }
      for (const error of job && job.errors || []) {
        const row = document.createElement('div');
        row.textContent = `${error.symbol || ''} ${error.timeframe || ''}: ${describe(error.error)}`;
        detail.append(row);
      }
      if (displayError) {
        // сбой точечного обновления экрана — отдельно от итога задания
        const row = document.createElement('div');
        row.textContent = `Обновление экрана: ${displayError}`;
        detail.append(row);
      }
    }
  }

  async function snapshot({ force = false } = {}) {
    if (!reload || (!force && Date.now() - lastRead < 15000)) return;
    if (activeRead) return activeRead;
    activeRead = Promise.resolve().then(() => reload()).then(() => { lastRead = Date.now(); }).finally(() => { activeRead = null; });
    return activeRead;
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try { await readStatus(); } catch (e) { console.warn('sync status:', e); }
    }, busy(statusData && statusData.job) ? 2000 : 30000);
  }

  async function readStatus({ diff = true } = {}) {
    if (statusRead) return statusRead;
    statusRead = HTF.api('/api/data-sync/status').then(async (data) => {
      statusData = data;
      lastStatusOkAt = Date.now();
      const job = data.job;
      const running = busy(job);
      paint();
      if (diff && data.versions) {
        if (job && job.finished_at && !running && (previousBusy || job.job_id !== lastCompletedJob)) {
          lastCompletedJob = job.job_id;
          // §15: после terminal job — точечный рефетч доменов с изменившейся
          // версией (не полный reloadAll); ждём его до показа итога
          await refreshStale(data.versions, true);
        } else {
          // фоновая сверка доменов на каждом чтении статуса: idle-поллинг
          // заменяет прежний принудительный 30-секундный снимок (AC17)
          refreshStale(data.versions, false);
        }
      }
      previousBusy = running;
      paint();
      return data;
    }).finally(() => { statusRead = null; schedule(); });
    return statusRead;
  }

  async function refreshStale(versions, immediate) {
    if (!versions || !syncStale) return;
    try {
      await syncStale(versions, { immediate });
      if (displayError) { displayError = null; paint(); }
    } catch (e) {
      displayError = (e && e.message) || String(e);
      paint();
      console.warn('stale domains refresh:', e);
    }
  }

  // AC17: WS reconnect / visibilitychange→visible / online — ОДНО
  // объединённое восстановление: одно чтение статуса + рефетч только
  // доменов с изменившейся версией. Параллельные триггеры делят промис.
  function recover() {
    if (recovering) return recovering;
    recovering = (async () => {
      // текущее чтение статуса могло начаться до reconnect — дожидаемся его
      // и читаем заново, чтобы diff версий видел свежее состояние
      if (statusRead) await statusRead.catch(() => {});
      const data = await readStatus({ diff: false });
      await refreshStale(data && data.versions, true);
      if (onRecover) { try { onRecover(); } catch (e) { console.warn('recover hook:', e); } }
    })().finally(() => { recovering = null; });
    return recovering;
  }

  // Прогрев данных (§11, AC01): страница рендерит сохранённый снимок сразу;
  // пока сервер не прогрет — видимый индикатор фазы и прогресса истории.
  const WARMUP_PHASE_RU = {
    starting: 'запуск', web: 'запуск веба', realtime: 'подключение котировок',
    history: 'восстановление истории', calc: 'расчёт',
  };
  let warmupTimer = null;

  function warmupPaint(payload) {
    const el = document.getElementById('warmup-indicator');
    if (!el) {
      if (warmupTimer) { clearInterval(warmupTimer); warmupTimer = null; }
      return;
    }
    const phases = (payload && payload.phases) || {};
    const done = !!(payload && payload.ready && phases.history && phases.calc);
    if (done) {
      el.classList.add('hidden');
      if (warmupTimer) { clearInterval(warmupTimer); warmupTimer = null; }
      document.dispatchEvent(new CustomEvent('lf-warmup', { detail: { done: true, text: null } }));
      return;
    }
    const backfill = payload && payload.backfill;
    const progress = backfill && backfill.total ? ` · история ${backfill.completed}/${backfill.total}` : '';
    const text = `Прогрев данных: ${WARMUP_PHASE_RU[payload && payload.phase] || 'запуск'}${progress}`;
    el.textContent = text;
    el.classList.remove('hidden');
    document.dispatchEvent(new CustomEvent('lf-warmup', { detail: { done: false, text } }));
  }

  async function warmupPoll() {
    try { warmupPaint(await HTF.api('/api/ready')); }
    catch (e) { /* веб ещё не отвечает — индикатор остаётся как был */ }
  }

  function warmupStart() {
    if (warmupTimer || !document.getElementById('warmup-indicator')) return;
    warmupPoll();
    warmupTimer = setInterval(warmupPoll, 5000); // пока не ready — §11 п.5с
  }

  async function start() {
    const button = document.getElementById('btn-data-sync');
    button.disabled = true;
    button.textContent = 'Обновление…';
    document.getElementById('data-sync-status').textContent = 'Запускаем обновление данных…';
    try {
      const job = await HTF.api('/api/data-sync', { method: 'POST' });
      previousBusy = true;
      statusData = { ...statusData, job };
      paint();
      await readStatus();
    } catch (e) {
      await readStatus().catch(() => {});
      const label = document.getElementById('data-sync-status');
      // §15: ошибка API текстом + время последней успешной проверки
      label.textContent = (typeof e.message === 'string' && e.message !== '[object Object]' ? e.message : 'Повторите обновление через 15 секунд') +
        (lastStatusOkAt ? ` · последняя успешная проверка: ${format(lastStatusOkAt)}` : '');
      nextAllowed = Date.now() + 15000;
      button.disabled = true;
      setTimeout(paint, 15000);
    }
  }

  function init(options) {
    reload = options.reload;
    selected = options.selected || selected;
    syncStale = options.syncStale || syncStale;
    onRecover = options.onRecover || onRecover;
    lastRead = Date.now();
    document.getElementById('btn-data-sync').addEventListener('click', start);
    document.addEventListener('change', (event) => {
      if (['instrument-select', 'ltf-instrument'].includes(event.target.id)) {
        paint();
        readStatus().catch((e) => console.warn('selected freshness:', e));
      }
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden) recover().catch((e) => console.warn('visibility recovery:', e));
    });
    window.addEventListener('online', () => recover().catch((e) => console.warn('online recovery:', e)));
    warmupStart();
    readStatus().catch((e) => {
      console.warn('sync status:', e);
      document.getElementById('data-sync-status').textContent =
        `Не удалось получить свежесть данных: ${e.message}` +
        (lastStatusOkAt ? ` · последняя успешная проверка: ${format(lastStatusOkAt)}` : '');
    });
  }

  function onWs(data) {
    if (data.type !== 'data_sync') return;
    // Read the durable authoritative status; coalesce bursts of progress.
    readStatus().catch((e) => console.warn('sync event:', e));
  }
  return { init, onWs, snapshot, readStatus, paint, recover };
})();
