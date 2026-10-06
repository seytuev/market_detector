/* Общие helper'ы фронтенда HTF/LTF: токен владельца, API с Bearer,
   WebSocket с переподключением, форматтеры, тема графика, диалоги. */

'use strict';

window.HTF = (() => {
  const TOKEN_KEY = 'htf_token';
  let urlTokenRead = false;
  let tokenDialogPromise = null;
  const modalStack = [];

  function getToken() {
    if (!urlTokenRead) {
      urlTokenRead = true;
      const url = new URL(location.href);
      const fromUrl = url.searchParams.get('token');
      if (fromUrl) {
        localStorage.setItem(TOKEN_KEY, fromUrl);
        url.searchParams.delete('token');
        history.replaceState(null, '', url.pathname + url.search + url.hash);
      }
    }
    return localStorage.getItem(TOKEN_KEY) || '';
  }

  function cssVar(name, fallback) {
    const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function chartTheme() {
    return {
      background: cssVar('--bg-panel', '#171B22'),
      text: cssVar('--text-dim', '#9BA6B5'),
      grid: cssVar('--border', '#2B323D'),
      border: cssVar('--border', '#2B323D'),
      accent: cssVar('--accent', '#5B8DEF'),
      up: cssVar('--green', '#45B9A5'),
      down: cssVar('--red', '#EF7573'),
    };
  }

  function setConnectionState(online) {
    // D02: индикатор отражает только транспорт (WS). Свежесть данных —
    // отдельные индикаторы data_state (котировка/свечи/обработка); открытие
    // WebSocket ничего не говорит о свежести котировок и расчётов.
    const dot = document.getElementById('ws-indicator');
    const label = document.getElementById('connection-label')
      || document.querySelector('.connection-label');
    const text = online ? 'Соединение установлено' : 'Переподключение…';
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

  async function ensureToken(error = '') {
    if (getToken()) return getToken();
    if (tokenDialogPromise) return tokenDialogPromise;
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.innerHTML = '<form class="modal-card" role="dialog" aria-modal="true" aria-labelledby="auth-title"><h3 id="auth-title">Доступ к HTF Zones</h3><label>Токен владельца <input type="password" autocomplete="current-password" required></label><p class="form-error" role="alert"></p><div class="modal-actions"><button class="btn primary" type="submit">Продолжить</button></div></form>';
    document.body.appendChild(modal);
    modal.querySelector('.form-error').textContent = error;
    const input = modal.querySelector('input');
    input.focus();
    tokenDialogPromise = new Promise((resolve, reject) => {
      modal.querySelector('form').onsubmit = (event) => {
        event.preventDefault();
        const token = input.value.trim();
        if (!token) { modal.querySelector('.form-error').textContent = 'Введите токен.'; return; }
        localStorage.setItem(TOKEN_KEY, token);
        modal.remove();
        tokenDialogPromise = null;
        resolve(token);
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
    const resp = await fetch(path, {
      ...options,
      headers: {
        'Content-Type': 'application/json',
        'Authorization': 'Bearer ' + getToken(),
        ...(options.headers || {}),
      },
    });
    if (resp.status === 401) {
      localStorage.removeItem(TOKEN_KEY);
      await ensureToken('Токен не подошёл. Проверьте значение и попробуйте снова.');
      return api(path, options);
    }
    if (!resp.ok) {
      let detail = resp.statusText;
      try { detail = (await resp.json()).detail || detail; } catch (e) { /* не JSON */ }
      throw new Error(detail);
    }
    return resp.status === 204 ? null : resp.json();
  }

  function connectWs(onMessage, indicatorEl, onOpen) {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    let opened = false;
    const connect = () => {
      const ws = new WebSocket(
        `${proto}://${location.host}/ws?token=${encodeURIComponent(getToken())}`);
      ws.onopen = () => {
        setConnectionState(true);
        // onOpen(isReconnect): после reconnect окно может запросить полный снимок
        if (onOpen) onOpen(opened);
        opened = true;
      };
      ws.onclose = () => {
        setConnectionState(false);
        setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
      ws.onmessage = (msg) => {
        let data;
        try { data = JSON.parse(msg.data); } catch (e) { return; }
        onMessage(data);
      };
      return ws;
    };
    return connect();
  }

  function fmtPrice(v) {
    if (v === null || v === undefined) return '—';
    return Number(v).toLocaleString('ru-RU', { maximumFractionDigits: 8 });
  }

  function fmtTime(ms) {
    if (!ms) return '—';
    return new Date(ms).toLocaleString('ru-RU', { hour12: false, timeZone: 'Europe/Moscow' }) + ' МСК';
  }

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function tradingviewUrl(ins) {
    if (!ins) return null;
    return `https://www.tradingview.com/chart/?symbol=${ins.venue.toUpperCase()}:${ins.symbol}`;
  }

  return {
    getToken, ensureToken, api, connectWs, fmtPrice, fmtTime, esc, tradingviewUrl,
    chartTheme, openModal, closeModal, setConnectionState,
  };
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
      makeSplitter({
        layout: overview, panel: overview.querySelector('#zone-rail'), axis: 'x', prop: 'gridTemplateColumns',
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
