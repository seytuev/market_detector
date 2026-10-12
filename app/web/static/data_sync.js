/* Shared synchronization control and snapshot recovery. */
window.LFDataSync = (() => {
  let reload = null;
  let selected = () => null;
  let activeRead = null;
  let lastRead = 0;
  let statusRead = null;
  let statusData = null;
  let timer = null;
  let nextAllowed = 0;
  let lastCompletedJob = null;
  let previousBusy = false;
  const reasons = {
    missing_close: 'ожидается закрытая свеча', missing_current: 'ожидается текущая свеча',
    history_gap: 'пропуски истории', processing_lag: 'обработка не завершена',
    source_error: 'источник недоступен', sync_stale: 'давно не было синхронизации',
    quote_stale: 'котировка устарела', no_quote: 'нет котировки',
    h1_processor_unavailable: 'обработчик H1 недоступен', deadline_exceeded: 'превышено время ожидания',
    interrupted_by_restart: 'обновление прервано перезапуском',
    deadline_exceeded_processing_continues: 'превышено время ожидания; обработка продолжается',
  };
  const describe = (value) => reasons[value] || value || 'нет данных';
  const format = (at) => new Date(at).toLocaleTimeString('ru-RU', { timeZone: 'Europe/Moscow', hour12: false }) + ' МСК';
  const busy = (job) => job && (['queued', 'running'].includes(job.status) || job.processing_continues);

  function paint() {
    const button = document.getElementById('btn-data-sync');
    const label = document.getElementById('data-sync-status');
    const detail = document.getElementById('data-sync-details');
    if (!button || !label || !statusData) return;
    const job = statusData.job;
    const running = busy(job);
    button.disabled = running || !statusData.available || Date.now() < nextAllowed;
    button.textContent = running ? 'Обновление…' : 'Обновить данные';
    button.setAttribute('aria-busy', String(!!running));
    let message = 'Свечи и котировки всех включённых активов';
    if (!statusData.available) message = 'Обновление недоступно: обработчик данных не работает';
    else if (running) message = `Обновление: ${job.channels_completed} из ${job.channels_total}` +
      (job.processing_continues ? ' · обработка продолжается' : '');
    else if (job) {
      if (job.status === 'succeeded') message = `Данные обновлены в ${format(job.finished_at)}`;
      else if (job.status === 'partial') message = `Обновлено частично: ${job.results.filter((r) => r.status === 'ok').length} из ${job.channels_total}`;
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
    label.textContent = message;
    label.dataset.state = running ? 'running' : job && job.status || 'idle';
    if (detail) {
      detail.replaceChildren();
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

  async function readStatus() {
    if (statusRead) return statusRead;
    statusRead = HTF.api('/api/data-sync/status').then(async (data) => {
      statusData = data;
      const job = data.job;
      const running = busy(job);
      if (job && job.finished_at && !running && (previousBusy || job.job_id !== lastCompletedJob)) {
        lastCompletedJob = job.job_id;
        await snapshot({ force: true });
      }
      previousBusy = running;
      paint();
      return data;
    }).finally(() => { statusRead = null; schedule(); });
    return statusRead;
  }

  async function start() {
    const button = document.getElementById('btn-data-sync');
    button.disabled = true;
    try {
      const job = await HTF.api('/api/data-sync', { method: 'POST' });
      previousBusy = true;
      statusData = { ...statusData, job };
      paint();
      await readStatus();
    } catch (e) {
      await readStatus().catch(() => {});
      const label = document.getElementById('data-sync-status');
      label.textContent = typeof e.message === 'string' && e.message !== '[object Object]' ? e.message : 'Повторите обновление через 15 секунд';
      nextAllowed = Date.now() + 15000;
      button.disabled = true;
      setTimeout(paint, 15000);
    }
  }

  function init(options) {
    reload = options.reload;
    selected = options.selected || selected;
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
    setInterval(() => {
      if (!document.hidden) snapshot({ force: true }).catch((e) => console.warn('snapshot recovery:', e));
    }, 30000);
    readStatus().catch((e) => console.warn('sync status:', e));
  }

  async function recover() {
    await Promise.all([snapshot(), readStatus()]);
  }

  function onWs(data) {
    if (data.type !== 'data_sync') return;
    // Read the durable authoritative status; coalesce bursts of progress.
    readStatus().catch((e) => console.warn('sync event:', e));
  }
  return { init, onWs, snapshot, readStatus, paint };
})();
