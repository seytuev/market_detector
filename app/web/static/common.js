/* Общие helper'ы фронтенда HTF/LTF: токен владельца, API с Bearer,
   WebSocket с переподключением, форматтеры, тема графика, диалоги. */

'use strict';

window.HTF = (() => {
  let currentSocket = null;
  const TOKEN_KEY = 'htf_token';
  let urlTokenRead = false;
  let tokenDialogPromise = null;
  let authCheckPromise = null;
  const modalStack = [];

  // Тема оформления LevelFrame: 'system' | 'dark' | 'light' (localStorage lf:theme).
  // data-theme на <html> ставит также inline-скрипт в <head> (до загрузки CSS),
  // здесь — API для настроек и подписка на смену системной темы.
  const THEME_KEY = 'lf:theme';
  const lightMq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;

  function themeGet() {
    const saved = localStorage.getItem(THEME_KEY);
    if (saved === 'dark' || saved === 'light' || saved === 'system') return saved;
    // Нет выбора — светлая тема. «Как в системе» только после явного сохранения.
    return 'light';
  }

  function themeApply(choice) {
    const mode = choice || themeGet();
    const resolved = mode === 'system'
      ? (lightMq && lightMq.matches ? 'light' : 'dark')
      : mode;
    document.documentElement.dataset.theme = resolved;
    window.dispatchEvent(new CustomEvent('lf-theme', { detail: { mode, resolved } }));
    return resolved;
  }

  function themeSet(choice) {
    localStorage.setItem(THEME_KEY, choice === 'dark' || choice === 'light' ? choice : 'system');
    return themeApply();
  }

  if (lightMq) {
    const onSystemTheme = () => { if (themeGet() === 'system') themeApply(); };
    if (lightMq.addEventListener) lightMq.addEventListener('change', onSystemTheme);
    else if (lightMq.addListener) lightMq.addListener(onSystemTheme);
  }
  themeApply();

  let sessionReady = false;
  let migrationToken = '';
  function getToken() { return ''; }
  async function exchangeToken(token) {
    const response = await fetch('/api/auth/session', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type':'application/json'}, body: JSON.stringify({token}),
    });
    if (!response.ok) throw new Error('Токен не подошёл. Проверьте значение.');
    localStorage.removeItem(TOKEN_KEY);
    sessionReady = true;
    return '';
  }

  function cssVar(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function chartTheme() {
    return {
      background: cssVar('--lf-surface', '#131B28'),
      text: cssVar('--lf-text2', '#A2B0C5'),
      grid: cssVar('--lf-border', '#2C3A4E'),
      border: cssVar('--lf-border', '#2C3A4E'),
      accent: cssVar('--lf-brand', '#4268DD'),
      up: cssVar('--lf-positive', '#62C9B0'),
      down: cssVar('--lf-negative', '#F08D98'),
    };
  }

  function setConnectionState(online) {
    // D02: индикатор отражает только транспорт (WS). Свежесть данных —
    // отдельные индикаторы data_state (котировка/свечи/обработка); открытие
    // WebSocket ничего не говорит о свежести котировок и расчётов.
    const dot = document.getElementById('ws-indicator');
    const label = document.getElementById('connection-label')
      || document.querySelector('.connection-label');
    const text = online ? 'Соединение установлено' : 'Нет соединения';
    if (dot) {
      dot.className = 'ws-dot ' + (online ? 'online' : 'offline');
      dot.title = online ? 'WebSocket подключён' : 'Нет соединения';
    }
    if (label) label.textContent = text;
  }

  function focusable(root) {
    return [...root.querySelectorAll(
      'button, [href], input, select, textarea, summary, [tabindex]:not([tabindex="-1"])'
    )].filter((el) => !el.disabled && el.getClientRects().length);
  }

  function openModal(el) {
    if (!el) return;
    modalStack.push({ el, prev: document.activeElement });
    el.classList.remove('hidden');
    const items = focusable(el);
    (items[0] || el).focus();
  }

  function closeModal(el) {
    if (!el || el.classList.contains('hidden')) return;
    el.classList.add('hidden');
    const idx = modalStack.map((item) => item.el).lastIndexOf(el);
    const rec = idx >= 0 ? modalStack.splice(idx, 1)[0] : null;
    if (rec && rec.prev && typeof rec.prev.focus === 'function') rec.prev.focus();
  }

  function ensureToken(error = '') {
    if (!error && sessionReady) return Promise.resolve('');
    if (authCheckPromise) return authCheckPromise;
    authCheckPromise = ensureTokenInner(error).finally(() => { authCheckPromise = null; });
    return authCheckPromise;
  }

  async function ensureTokenInner(error = '') {
    if (!error && sessionReady) return '';
    if (!error) {
      const check = await fetch('/api/auth/session', {credentials:'same-origin'});
      if (check.ok) { sessionReady = true; return ''; }
      const url = new URL(location.href);
      migrationToken = url.searchParams.get('token') || localStorage.getItem(TOKEN_KEY) || '';
      url.searchParams.delete('token');
      history.replaceState(null, '', url.pathname + url.search + url.hash);
      if (migrationToken) {
        const old = migrationToken; migrationToken = '';
        try { return await exchangeToken(old); } catch (_) { localStorage.removeItem(TOKEN_KEY); }
      }
    }
    sessionReady = false;
    if (tokenDialogPromise) return tokenDialogPromise;
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.innerHTML = '<form class="modal-card" role="dialog" aria-modal="true" aria-labelledby="auth-title"><h3 id="auth-title">Доступ к LevelFrame</h3><label>Токен владельца <input type="password" autocomplete="current-password" required></label><p class="form-error" role="alert"></p><div class="modal-actions"><button class="btn primary" type="submit">Продолжить</button></div></form>';
    document.body.appendChild(modal);
    modal.querySelector('.form-error').textContent = error;
    const input = modal.querySelector('input');
    input.focus();
    tokenDialogPromise = new Promise((resolve, reject) => {
      modal.querySelector('form').onsubmit = async (event) => {
        event.preventDefault();
        const token = input.value.trim();
        if (!token) { modal.querySelector('.form-error').textContent = 'Введите токен.'; return; }
        const button = modal.querySelector('button');
        button.disabled = true;
        try {
          await exchangeToken(token);
          input.value = '';
          modal.remove(); tokenDialogPromise = null; resolve('');
        } catch (err) {
          modal.querySelector('.form-error').textContent = err.message;
        } finally { button.disabled = false; }
      };
      modal.onkeydown = (event) => {
        if (event.key === 'Escape') {
          modal.remove();
          tokenDialogPromise = null;
          reject(new Error('Доступ отменён'));
        }
      };
    });
    return tokenDialogPromise;
  }

  async function api(path, options = {}) {
    await ensureToken();
    const resp = await fetch(path, {
      ...options,
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + getToken(),
        ...(options.headers || {}),
      },
    });
    if (resp.status === 401) {
      sessionReady = false;
      localStorage.removeItem(TOKEN_KEY);
      await ensureToken('Токен не подошёл. Проверьте значение и попробуйте снова.');
      return api(path, options);
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try { detail = (await resp.json()).detail || detail; } catch (e) { /* не JSON */ }
      if (typeof detail === 'object') detail = detail.fields ? Object.entries(detail.fields).map(([k,v]) => `${k}: ${v}`).join('\n') : JSON.stringify(detail);
      throw new Error(detail);
    }
    return resp.status === 204 ? null : resp.json();
  }

  function connectWs(onMessage, indicatorEl, onOpen) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    let opened = false;
    let attempts = 0;
    const connect = () => {
      const ws = new WebSocket(
        `${proto}://${location.host}/ws`);
      currentSocket = ws;
      ws.onopen = () => {
        attempts = 0;
        setConnectionState(true);
        // onOpen(isReconnect): после reconnect окно может запросить полный снимок
        if (onOpen) Promise.resolve(onOpen(opened)).catch((e) => console.warn('ws recovery:', e));
        opened = true;
      };
      ws.onclose = (event) => {
        setConnectionState(false);
        if (event.code === 4403) {
          const label = document.getElementById('connection-label');
          if (label) label.textContent = 'Доступ отклонён';
          return;
        }
        if (event.code === 4401) {
          sessionReady = false;
          ensureToken('Сессия завершена. Войдите снова.').then(connect).catch(console.warn);
          return;
        }
        const delay = Math.min(30000, 1000 * 2 ** Math.min(attempts++, 5));
        setTimeout(connect, delay * (0.8 + Math.random() * 0.4));
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (msg) => {
        let data;
        try { data = JSON.parse(msg.data); } catch (e) { return; }
        if (data.type === 'resync' || data.type === 'instrument') { ws.close(); return; }
        onMessage(data);
      };
      return ws;
    };
    return connect();
  }

  function captureChartRange(chart, candles) {
    const logical = chart.timeScale().getVisibleLogicalRange();
    if (!logical) return null;
    return { logical, time: chart.timeScale().getVisibleRange(),
      following: logical.to >= candles.length - 1,
      lastTime: candles.length ? candles[candles.length - 1].time : null,
      firstTime: candles.length ? candles[0].time : null };
  }

  function restoreChartRange(chart, candles, saved) {
    if (!saved) return;
    if (saved.following && saved.lastTime != null) {
      // Keep the same viewport width and right margin after new bars arrive.
      const index = candles.findIndex((c) => c.time === saved.lastTime);
      const shift = index >= 0 ? candles.length - 1 - index : 0;
      const first = candles.findIndex((c) => c.time === saved.firstTime);
      const offset = first >= 0 ? first : 0;
      chart.timeScale().setVisibleLogicalRange({ from: saved.logical.from + shift + offset,
        to: saved.logical.to + shift + offset });
    } else if (saved.time) {
      chart.timeScale().setVisibleRange(saved.time);
    } else chart.timeScale().setVisibleLogicalRange(saved.logical);
  }

  function fmtPrice(v) {
    if (v === null || v === undefined) return '—';
    return Number(v).toLocaleString('ru-RU', { maximumFractionDigits: 8 });
  }

  function fmtTime(ms) {
    if (!ms) return '—';
    return new Date(ms).toLocaleString('ru-RU', { hour12: false, timeZone: 'Europe/Moscow' }) + ' МСК';
  }

  // datetime-local → ms UTC, трактуя введённое время как московское
  // (ТЗ 07.10.2026 §6: весь UI — МСК; Москва без DST, фиксированный UTC+3)
  function parseMskLocal(raw) {
    if (!raw) return NaN;
    const withOffset = raw.length === 16 ? raw + ':00+03:00' : raw + '+03:00';
    return new Date(withOffset).getTime();
  }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function tradingviewUrl(ins) {
    if (!ins) return null;
    return `https://www.tradingview.com/chart/?symbol=${ins.venue.toUpperCase()}:${ins.symbol}`;
  }

  /* Доменные версии (§13/§14, этап 6b): WS-сообщения и снимки несут версии
     доменов; страница перечитывает только домен, чья версия выросла.
     Ключи: 'zones' | 'config' | 'quote:{iid}' | 'candles:{iid}:{tf}' |
     'scenario:{iid}' | 'history:{iid}'.
     Правила: версия равная/старше известной — без действия; первая известная
     версия (known == null) — только запоминается (первичную загрузку делает
     сама страница); сообщение без версии (старый сервер) — прежнее поведение
     (рефетч домена). В скрытой вкладке точечные рефетчи продолжаются (§14:
     серверные сигналы не останавливаем) — скрытие экономит только
     визуальную работу тиков; возврат делает один объединённый recovery. */
  class DomainSync {
    constructor() {
      this.known = new Map();     // ключ → последняя применённая версия
      this.handlers = new Map();  // домен → (key) => Promise|void
      this.timers = new Map();    // ключ → дебаунс-таймер (per-key: соседний
      // канал того же домена не должен отменять чужой отложенный рефетч)
      this.running = new Map();   // домен → Promise (single-flight рефетча)
      this.debounceMs = 350;
    }

    static wsEntries(msg) {
      const out = [];
      const dv = msg && msg.domain_versions;
      if (!dv) return out;
      const iid = msg.instrument_id;
      if (dv.quote != null && iid != null) out.push([`quote:${iid}`, dv.quote]);
      if (dv.candles != null && iid != null && msg.timeframe) {
        out.push([`candles:${iid}:${msg.timeframe}`, dv.candles]);
      }
      if (dv.zones != null) out.push(['zones', dv.zones]);
      if (dv.scenario != null && iid != null) out.push([`scenario:${iid}`, dv.scenario]);
      if (dv.config != null) out.push(['config', dv.config]);
      return out;
    }

    // Карта versions из /api/data-sync/status и снимков: скаляры (zones,
    // config) и вложенные {iid: seq} / {'iid:tf': seq} — в плоские ключи.
    static statusEntries(versions) {
      const out = [];
      if (!versions || typeof versions !== 'object') return out;
      for (const [domain, value] of Object.entries(versions)) {
        if (value == null) continue;
        if (typeof value === 'number') { out.push([domain, value]); continue; }
        if (typeof value === 'object') {
          for (const [sub, v] of Object.entries(value)) {
            if (typeof v === 'number') out.push([`${domain}:${sub}`, v]);
          }
        }
      }
      return out;
    }

    // Сообщение старого сервера без domain_versions: домен по типу.
    static legacyKey(msg) {
      const iid = msg.instrument_id;
      switch (msg.type) {
        case 'candle':
          return iid != null && msg.timeframe ? `candles:${iid}:${msg.timeframe}` : null;
        case 'zone': case 'event': return 'zones';
        case 'ltf': return iid != null ? `scenario:${iid}` : null;
        case 'instrument': return 'config';
        default: return null;
      }
    }

    on(domain, fn) { this.handlers.set(domain, fn); return this; }

    adopt(key, value) {
      if (value == null || !Number.isFinite(value)) return;
      const cur = this.known.get(key);
      if (cur == null || value > cur) this.known.set(key, value);
    }

    adoptAll(entries) { for (const [key, v] of entries) this.adopt(key, v); }

    knownVersion(key) { return this.known.get(key); }

    // Реакция на смену версии: отложенный (дебаунс) или немедленный рефетч
    // домена. Возвращает Promise для immediate-режима, иначе null.
    bump(key, value, { immediate = false } = {}) {
      if (value != null) {
        const cur = this.known.get(key);
        if (cur != null && value <= cur) return null; // уже применено
        this.adopt(key, value);
        if (cur == null) return null; // первое касание — покрывает загрузка страницы
      }
      const domain = key.split(':')[0];
      if (!this.handlers.has(domain)) return null;
      if (immediate) return this.fetchNow(domain, key);
      this.schedule(domain, key);
      return null;
    }

    notifyWs(msg, opts) {
      const entries = DomainSync.wsEntries(msg);
      if (!entries.length) {
        const key = msg && msg.type ? DomainSync.legacyKey(msg) : null;
        if (key) this.bump(key, null, opts);
        return;
      }
      for (const [key, value] of entries) this.bump(key, value, opts);
    }

    // Сверка карты versions из статуса/снимка; immediate — дождаться рефетчей.
    syncStatus(versions, { immediate = false } = {}) {
      const jobs = [];
      for (const [key, value] of DomainSync.statusEntries(versions)) {
        const r = this.bump(key, value, { immediate });
        if (r) jobs.push(r);
      }
      return Promise.all(jobs);
    }

    schedule(domain, key) {
      clearTimeout(this.timers.get(key));
      this.timers.set(key, setTimeout(() => {
        this.timers.delete(key);
        this.fetchNow(domain, key);
      }, this.debounceMs));
    }

    // Немедленно выполнить отложенные рефетчи и дождаться идущих — для точек,
    // где нужно «данные свежие прямо сейчас» (terminal job, recovery).
    async flush() {
      for (let guard = 0; guard < 8; guard += 1) {
        const keys = [...this.timers.keys()];
        for (const key of keys) {
          clearTimeout(this.timers.get(key));
          this.timers.delete(key);
          this.fetchNow(key.split(':')[0], key);
        }
        const running = [...this.running.values()];
        if (!running.length && !this.timers.size) return;
        await Promise.all(running);
      }
    }

    fetchNow(domain, key) {
      const fn = this.handlers.get(domain);
      if (!fn) return null;
      const prev = this.running.get(domain);
      if (prev) {
        // домен ушёл вперёд во время рефетча — повторный проход после него
        return prev.then(() => this.fetchNow(domain, key));
      }
      const p = Promise.resolve().then(() => fn(key))
        .catch((e) => console.warn(`domain refetch (${domain}):`, e));
      this.running.set(domain, p);
      p.finally(() => { if (this.running.get(domain) === p) this.running.delete(domain); });
      return p;
    }
  }

  return {
    getToken, ensureToken, api, connectWs, isWsConnected: () => !!currentSocket && currentSocket.readyState === 1,
    fmtPrice, fmtTime, parseMskLocal, esc, tradingviewUrl,
    captureChartRange, restoreChartRange,
    chartTheme, openModal, closeModal, setConnectionState,
    theme: { get: themeGet, set: themeSet, apply: themeApply },
    DomainSync,
  };
})();

/* Consistent mobile navigation on every workspace route. Keep existing links,
   listeners and current-page markers; the icons are decorative. */
(() => {
  const icons = {
    now: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    desk: '<path d="M4 4v16h16M7 14l4-5 4 3 5-7"/>',
    review: '<rect x="5" y="4" width="14" height="17" rx="3"/><path d="M9 3h6v4H9zM9 14l2 2 4-4"/>',
    alt: '<path d="M5 4v16M12 4v16M19 4v16M2 8h6M9 15h6M16 10h6"/>',
    more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
  };
  const review = [...document.querySelectorAll('.mobile-nav a')].find(item => (item.getAttribute('href') || '').endsWith('#review'));
  if (review) {
    const menu = document.getElementById('nav-more-panel');
    menu?.querySelector('a[href="/events.html"]')?.remove();
    if (menu && !menu.querySelector('a[href$="#review"]')) {
      const link = document.createElement('a');
      link.href = review.getAttribute('href'); link.textContent = 'Проверка зон';
      link.setAttribute('role', 'menuitem');
      if (review.dataset.view) link.dataset.view = 'review';
      menu.appendChild(link);
    }
    review.href = '/events.html'; review.removeAttribute('data-view');
    review.classList.toggle('active', location.pathname === '/events.html');
    review.innerHTML = '<svg width="21" height="21" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="m3 15 5-5 4 4 8-9M15 5h5v5M3 21h18"/></svg><span>Рынок</span>';
  }
  document.querySelectorAll('.mobile-nav a[href$="#now"] span').forEach(span => { span.textContent = 'Обзор'; });
  document.querySelectorAll('.mobile-nav > a, .mobile-nav > button').forEach(item => {
    if (item.querySelector('svg')) return;
    const href = item.getAttribute('href') || '';
    const key = item.id === 'nav-more' ? 'more' : href.includes('alt.html') ? 'alt' : href.split('#')[1];
    if (!icons[key]) return;
    const text = document.createElement('span'); text.textContent = item.textContent;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('width', '21'); svg.setAttribute('height', '21');
    svg.setAttribute('viewBox', '0 0 24 24'); svg.setAttribute('fill', 'none');
    svg.setAttribute('aria-hidden', 'true'); svg.innerHTML = icons[key];
    item.replaceChildren(svg, text);
  });
})();

/* Secondary mobile controls start folded. Retain their values and keep all
   controls visible on desktop, including when the viewport changes. */
(() => {
  const mobile = window.matchMedia('(max-width: 767px)');
  const panels = [...document.querySelectorAll('details[data-mobile-fold]')];
  const adapt = () => panels.forEach(panel => { panel.open = !mobile.matches; });
  const count = panel => {
    const badge = panel.querySelector('[data-filter-count]');
    if (!badge) return;
    const active = [...panel.querySelectorAll('input, select')].filter(el =>
      el.type === 'checkbox' || el.type === 'radio' ? el.checked : String(el.value).trim()).length;
    badge.textContent = active ? String(active) : '';
  };
  panels.forEach(panel => {
    panel.addEventListener('input', () => count(panel));
    panel.addEventListener('change', () => count(panel));
    // Deep links may fill the controls after the initial paint.
    panel.addEventListener('toggle', () => count(panel));
    count(panel);
  });
  adapt();
  mobile.addEventListener('change', adapt);
  document.addEventListener('lf-filters-changed', () => panels.forEach(count));
})();

/* Панель «Ещё» мобильной навигации. Общая для страниц со shell. */
(() => {
  const btn = document.getElementById('nav-more');
  const panel = document.getElementById('nav-more-panel');
  if (!btn || !panel) return;
  const close = () => {
    panel.classList.add('hidden');
    btn.setAttribute('aria-expanded', 'false');
  };
  btn.addEventListener('click', () => {
    const open = panel.classList.contains('hidden');
    panel.classList.toggle('hidden', !open);
    btn.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
  panel.querySelectorAll('a').forEach((link) => link.addEventListener('click', close));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') close();
  });
})();

/* Кнопка сворачивания бокового меню. Выбор хранится в lf:sidebar и
   применяется до загрузки стилей inline-скриптом в <head>. */
(() => {
  const btn = document.getElementById('sidebar-toggle');
  if (!btn) return;
  const apply = (state) => {
    if (state === 'collapsed') document.documentElement.dataset.sidebar = 'collapsed';
    else document.documentElement.dataset.sidebar = 'expanded';
    localStorage.setItem('lf:sidebar', state);
    const collapsed = state === 'collapsed';
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    btn.title = collapsed ? 'Развернуть меню' : 'Свернуть меню';
    btn.setAttribute('aria-label', btn.title);
  };
  btn.addEventListener('click', () => {
    apply(document.documentElement.dataset.sidebar === 'collapsed' ? 'expanded' : 'collapsed');
  });
  if (document.documentElement.dataset.sidebar === 'collapsed') {
    btn.setAttribute('aria-expanded', 'false');
    btn.title = 'Развернуть меню';
    btn.setAttribute('aria-label', btn.title);
  }
})();

/* Лента «События актива» сворачивается, освобождая место графику и «Зонам H1».
   Выбор хранится в lf:eventstrip и применяется до загрузки стилей
   inline-скриптом в <head> (см. index.html). */
(() => {
  const btn = document.getElementById('events-strip-toggle');
  if (!btn) return;
  const apply = (state) => {
    const collapsed = state === 'collapsed';
    if (collapsed) document.documentElement.dataset.eventstrip = 'collapsed';
    else delete document.documentElement.dataset.eventstrip;
    localStorage.setItem('lf:eventstrip', state);
    btn.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    btn.title = collapsed ? 'Развернуть ленту событий' : 'Свернуть ленту событий';
  };
  btn.addEventListener('click', () => {
    apply(document.documentElement.dataset.eventstrip === 'collapsed' ? 'expanded' : 'collapsed');
  });
  if (document.documentElement.dataset.eventstrip === 'collapsed') {
    btn.setAttribute('aria-expanded', 'false');
    btn.title = 'Развернуть ленту событий';
  }
})();

/* Сплиттеры: перетаскивание границы меняет ширину правой панели и высоту
   нижней. Размеры действуют только в текущей сессии (без сохранения). */
(() => {
  function makeSplitter({ layout, panel, axis, prop, min, max, apply, enabled }) {
    const handle = document.createElement('div');
    handle.className = 'panel-splitter ' + (axis === 'x' ? 'v' : 'h');
    handle.setAttribute('aria-hidden', 'true');
    layout.appendChild(handle);

    const place = () => {
      if (!enabled()) {
        handle.style.display = 'none';
        layout.style[prop] = '';
        return;
      }
      handle.style.display = '';
      const lr = layout.getBoundingClientRect();
      const pr = panel.getBoundingClientRect();
      if (axis === 'x') {
        handle.style.left = (pr.left - lr.left - 6) + 'px';
        handle.style.top = (pr.top - lr.top) + 'px';
        handle.style.height = pr.height + 'px';
      } else {
        handle.style.top = (pr.top - lr.top - 6) + 'px';
        handle.style.left = (pr.left - lr.left) + 'px';
        handle.style.width = pr.width + 'px';
      }
    };

    handle.addEventListener('pointerdown', (e) => {
      if (!enabled()) return;
      e.preventDefault();
      handle.setPointerCapture(e.pointerId);
      const rect = panel.getBoundingClientRect();
      const startSize = axis === 'x' ? rect.width : rect.height;
      const startPos = axis === 'x' ? e.clientX : e.clientY;
      handle.classList.add('dragging');
      document.body.style.userSelect = 'none';
      const onMove = (ev) => {
        const pos = axis === 'x' ? ev.clientX : ev.clientY;
        const size = Math.max(min, Math.min(max(), startSize + (startPos - pos)));
        apply(Math.round(size));
        place();
      };
      const onDone = () => {
        handle.classList.remove('dragging');
        document.body.style.userSelect = '';
        handle.removeEventListener('pointermove', onMove);
        handle.removeEventListener('pointerup', onDone);
        handle.removeEventListener('pointercancel', onDone);
      };
      handle.addEventListener('pointermove', onMove);
      handle.addEventListener('pointerup', onDone);
      handle.addEventListener('pointercancel', onDone);
    });

    window.addEventListener('resize', place);
    new ResizeObserver(place).observe(layout);
    place();
  }

  function initLayoutSplitters() {
    const innerX = (el) => {
      const cs = getComputedStyle(el);
      return el.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight);
    };
    const innerY = (el) => {
      const cs = getComputedStyle(el);
      return el.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom);
    };
    const gapX = (el) => parseFloat(getComputedStyle(el).columnGap) || 0;
    const gapY = (el) => parseFloat(getComputedStyle(el).rowGap) || 0;

    const ltf = document.querySelector('.ltf-layout');
    if (ltf) {
      const ltfWide = () => window.matchMedia('(min-width: 1280px)').matches;
      makeSplitter({
        layout: ltf, panel: ltf.querySelector('#ltf-inspector'), axis: 'x', prop: 'gridTemplateColumns',
        min: 240, max: () => innerX(ltf) - gapX(ltf) * 2 - 240 - 400,
        apply: (w) => { ltf.style.gridTemplateColumns = `240px minmax(0,1fr) ${w}px`; },
        enabled: ltfWide,
      });
      makeSplitter({
        layout: ltf, panel: ltf.querySelector('.ltf-bottom'), axis: 'y', prop: 'gridTemplateRows',
        min: 120, max: () => innerY(ltf) - gapY(ltf) - 320,
        apply: (h) => { ltf.style.gridTemplateRows = `minmax(320px,1fr) ${h}px`; },
        enabled: () => ltfWide() && !ltf.classList.contains('bottom-collapsed'),
      });
    }

    const overview = document.querySelector('.overview-layout');
    if (overview) {
      // меряем объединённую панель «Актив / Зоны»: zone-rail скрыт,
      // когда активна вкладка сценария
      makeSplitter({
        layout: overview, panel: overview.querySelector('#desk-panel'), axis: 'x', prop: 'gridTemplateColumns',
        min: 280, max: () => innerX(overview) - gapX(overview) - 480,
        apply: (w) => { overview.style.gridTemplateColumns = `minmax(0,1fr) ${w}px`; },
        enabled: () => window.matchMedia('(min-width: 1200px)').matches,
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initLayoutSplitters);
  } else {
    initLayoutSplitters();
  }
})();

// Shared sidebar: identical icons, accessible names and utility placement on
// overview, analysis, market and screener pages.
(() => {
  const paths = {
    now: 'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
    desk: 'M3 20h18M6 15V9m0 2h0M12 17V4m6 10V7M4 11h4m2-4h4m2 4h4',
    review: 'm9 11 2 2 4-4M9 3H5v18h14V3h-4M9 2h6v4H9z',
    alt: 'M3 5h18M6 12h12M10 19h4',
    journal: 'M5 3h14v18H5zM9 7h6M9 11h6M9 15h4',
    events: 'm3 15 5-5 4 4 8-9M15 5h5v5M3 21h18',
    help: 'M9 9a3 3 0 1 1 5 2c-2 1-2 2-2 3M12 17h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
    settings: 'M4 7h16M4 17h16M9 4v6M15 14v6',
  };
  const sidebar = document.querySelector('.sidebar');
  if (!sidebar) return;
  sidebar.querySelectorAll('.app-tab').forEach(tab => {
    const href = tab.getAttribute('href') || (tab.getAttribute('aria-current') ? location.pathname : '');
    const key = Object.keys(paths).find(name => name === 'alt' || name === 'events'
      ? href.includes('/' + name + '.html') : href.endsWith('#' + name));
    if (!key) return;
    const label = key === 'now' ? 'Обзор рынка' : (tab.querySelector('.nav-full')?.textContent || tab.textContent).trim();
    if (key === 'now') tab.querySelector('.nav-full').textContent = label;
    tab.title = label;
    tab.setAttribute('aria-label', label);
    tab.insertAdjacentHTML('afterbegin', `<svg class="nav-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="${paths[key]}" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`);
    if (key === 'review') sidebar.querySelector('.sidebar-bottom')?.prepend(tab);
  });
  const label = sidebar.querySelector('.nav-label');
  if (label) label.textContent = 'Рабочее пространство';
})();

/* Панель «Слои» не должна вылезать за низ окна и за нижнюю навигацию.
   Горизонтальный сдвиг — если кнопка прижата к краю экрана. */
(() => {
  function fitLayerMenus() {
    const nav = document.querySelector('.mobile-nav');
    let limit = window.innerHeight - 8;
    if (nav && getComputedStyle(nav).display !== 'none') {
      const top = nav.getBoundingClientRect().top;
      if (top > 80 && top < limit) limit = top - 8;
    }
    document.querySelectorAll('details.layer-menu').forEach((menu) => {
      const panel = menu.querySelector(':scope > .layer-options');
      if (!panel) return;
      if (!menu.open) {
        panel.style.maxHeight = '';
        panel.style.transform = '';
        return;
      }
      panel.style.transform = '';
      const top = panel.getBoundingClientRect().top;
      const room = Math.floor(limit - top);
      const cap = Math.min(room, Math.floor(window.innerHeight * 0.7));
      panel.style.maxHeight = cap >= 120 ? cap + 'px' : '';
      const box = panel.getBoundingClientRect();
      let shift = 0;
      if (box.right > window.innerWidth - 8) shift -= box.right - (window.innerWidth - 8);
      if (box.left + shift < 8) shift += 8 - (box.left + shift);
      panel.style.transform = shift ? 'translateX(' + Math.round(shift) + 'px)' : '';
    });
  }

  function bind() {
    document.querySelectorAll('details.layer-menu').forEach((menu) => {
      if (menu.dataset.layerFit) return;
      menu.dataset.layerFit = '1';
      new ResizeObserver(() => { if (menu.open) fitLayerMenus(); }).observe(menu);
    });
    fitLayerMenus();
  }

  document.addEventListener('toggle', (event) => {
    const menu = event.target;
    if (!menu || !menu.classList || !menu.classList.contains('layer-menu')) return;
    requestAnimationFrame(fitLayerMenus);
  }, true);
  window.addEventListener('resize', fitLayerMenus);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bind);
  else bind();
})();
