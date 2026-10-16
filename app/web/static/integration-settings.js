'use strict';
(() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = (path, options) => HTF.api('/api/research/v1' + path, options);
  const send = (path, body = {}, method = 'POST') => api(path, {method, body:JSON.stringify(body)});
  const date = value => value ? new Date(value).toLocaleString('ru-RU') : 'Ещё не обновлялся';
  const roots = {imports:document.getElementById('integration-settings'), ai:document.getElementById('ai-settings')};
  const loaded = new Set();
  function card(title, body) { return `<article class="integration-card"><h3>${title}</h3>${body}</article>`; }
  async function run(button, status, fn) {
    button.disabled = true; status.textContent = 'Выполняем…';
    try { await fn(); } catch (e) { status.textContent = e.message; }
    finally { button.disabled = false; }
  }
  async function job(path, button, status) {
    await run(button, status, async () => {
      const response = await send(path);
      status.textContent = 'Задание поставлено в очередь.';
      const poll = async () => {
        try {
          const {data} = await api('/jobs/' + encodeURIComponent(response.data.job_id));
          status.textContent = ({pending:'Ожидает запуска',running:'Обработка выполняется…',succeeded:'Готово. Данные обновлены.',failed:'Не удалось выполнить задание. Проверьте подключение источника и модели.',cancelled:'Задание отменено'})[data.status] || data.status;
          if (['pending','running'].includes(data.status) && status.isConnected) setTimeout(poll, 2000);
        } catch (e) { status.textContent = e.message; }
      };
      setTimeout(poll, 1500);
    });
  }
  async function imports(root) {
    const [{data:cfg},{data:channels},{data:x}] = await Promise.all([api('/config'),api('/channels'),api('/x-accounts')]);
    const source=cfg.sources.terminal;
    const errors={x_not_configured:'Нужно подключить X API',x_auth_error:'Проверьте доступ к X API',x_rate_limited:'Достигнут лимит X API',x_source_error:'Не удалось получить посты',channel_unavailable:'Канал временно недоступен'};
    function accountCard(platform,title,items,connected) {
      const telegram=platform==='telegram', domain=telegram?'t.me':'x.com';
      const count=items.filter(a=>a.enabled).length;
      return card(`<span class="account-platform ${platform}">${telegram?'➤':'𝕏'}</span>${title}<span class="account-count">${count} / ${items.length} включено</span>`,
        `<p class="field-desc">${telegram?'Публичные каналы и их новые публикации.':'Публикации выбранных аккаунтов X.'}</p><form data-add="${platform}" class="integration-add"><label>Имя аккаунта или ссылка<input name="handle" placeholder="${telegram?'@channel или t.me/channel':'@username или x.com/username'}" required maxlength="200" autocomplete="off" spellcheck="false" aria-label="${telegram?'Telegram-канал':'Аккаунт X'}"></label><button class="btn primary">Добавить</button></form>`+
        (!connected?'<p class="account-notice">Аккаунты можно сохранить сейчас. Чтобы получать посты, подключите X API на сервере (X_BEARER_TOKEN).</p>':'')+
        '<div class="integration-channels">'+items.map(a=>{
          const state=telegram?a.source_state:a;
          return `<div class="integration-channel"><span class="account-avatar" aria-hidden="true">${esc(a.handle.slice(0,2).toUpperCase())}</span><div class="account-info"><a href="https://${domain}/${esc(a.handle)}" target="_blank" rel="noopener noreferrer">@${esc(a.handle)}</a><p class="field-desc">${state.error_code?esc(errors[state.error_code]||'Ошибка обновления'):esc(date(state.last_success_at))}</p></div><label class="integration-switch"><input type="checkbox" data-platform="${platform}" data-toggle="${esc(a.handle)}" ${a.enabled?'checked':''}><span>${a.enabled?'Включён':'Выключен'}</span></label><div class="account-actions"><button class="btn small" data-platform="${platform}" data-channel="${esc(a.handle)}" ${a.enabled&&connected?'':'disabled'}>Получить посты</button><button class="btn small account-remove" data-platform="${platform}" data-remove="${esc(a.handle)}" aria-label="Удалить @${esc(a.handle)} из отслеживания">Удалить</button></div></div>`;
        }).join('')+(items.length?'':`<div class="account-empty"><strong>${telegram?'Добавьте первый канал':'Кого будем читать в X?'}</strong><p>Вставьте ссылку на профиль или укажите @имя выше.</p></div>`)+ '</div>');
    }
    root.innerHTML='<div class="account-overview"><div><strong>Ваши источники</strong><p class="field-desc">Новые публикации включённых аккаунтов собираются автоматически. Списки сохраняются сразу.</p></div><a class="btn" href="/feed.html">Открыть ленту →</a></div><div class="integration-grid account-grid">'+accountCard('telegram','Telegram',channels.items,true)+accountCard('x','X / Twitter',x.items,x.configured)+'</div><p data-status role="status" aria-live="polite"></p><details class="integration-card"><summary>Дополнительный импорт STVTerminal</summary><p class="field-desc">Состояние: '+(source.configured?'подключён':'не настроен')+' · '+esc(date(source.last_success_at))+'</p><button class="btn" data-sync '+(source.configured?'':'disabled')+'>Импортировать архив терминала</button></details>';
    const status=root.querySelector('[data-status]');
    const path=platform=>platform==='telegram'?'/channels':'/x-accounts';
    async function refresh(message) {await imports(root);root.querySelector('[data-status]').textContent=message;}
    root.querySelector('[data-sync]').onclick=e=>job('/integration/sync',e.target,status);
    root.querySelectorAll('[data-add]').forEach(form=>form.onsubmit=e=>{e.preventDefault();run(form.querySelector('button'),status,async()=>{await send(path(form.dataset.add),{handle:new FormData(form).get('handle').trim(),enabled:true});await refresh('Аккаунт добавлен в отслеживание.');});});
    root.querySelectorAll('[data-toggle]').forEach(input=>input.onchange=()=>{
      const platform=input.dataset.platform,items=platform==='telegram'?channels.items:x.items;
      const account=items.find(a=>a.handle===input.dataset.toggle),enabled=input.checked;
      run(input,status,async()=>{try{await send(path(platform),{handle:account.handle,...(platform==='telegram'?{title:account.title||account.handle}:{}),enabled});await refresh('Отслеживание '+(enabled?'включено.':'выключено.'));}catch(e){input.checked=!enabled;throw e;}});
    });
    root.querySelectorAll('[data-channel]').forEach(b=>b.onclick=()=>job(path(b.dataset.platform)+'/'+encodeURIComponent(b.dataset.channel)+'/collect',b,status));
    root.querySelectorAll('[data-remove]').forEach(b=>b.onclick=()=>run(b,status,async()=>{await send(path(b.dataset.platform)+'/'+encodeURIComponent(b.dataset.remove),{},'DELETE');await refresh('Аккаунт удалён из отслеживания. Сохранённые публикации остались в ленте.');}));
  }
  async function ai(root) {
    const {data:m} = await api('/model/settings');
    root.innerHTML = '<form data-model><div class="integration-grid">' + card('Подключение и модели',
      `<span class="integration-state">${m.configured ? 'Модель подключена' : 'Требуется настройка'}</span><label>Провайдер<select name="provider">${[['xai','xAI'],['openai','OpenAI'],['anthropic','Anthropic']].map(([v,label])=>`<option value="${v}" ${m.provider===v?'selected':''}>${label}</option>`).join('')}</select></label><label>Основная модель<input name="cheap" value="${esc(m.model)}" required maxlength="150" placeholder="Название модели у провайдера"></label><label>Модель для сложных задач<input name="strong" value="${esc(m.strong_model || m.model)}" required maxlength="150"></label><p class="field-desc">Ключ выбранного провайдера задаётся на сервере: XAI_API_KEY, OPENAI_API_KEY или ANTHROPIC_API_KEY. Ключи не передаются в браузер.</p>`) +
      card('Расходы за последние 24 часа', `<div class="integration-metrics"><div><strong>$${esc(m.accounted_usd)}</strong><span>Учтено</span></div><div><strong>$${esc(m.remaining_usd)}</strong><span>Доступно</span></div><div><strong>${esc(m.calls_24h)}</strong><span>Запросов</span></div></div><label>Общий бюджет на 24 часа, USD<input name="daily_usd" type="number" min="0" step="any" value="${esc(m.daily_budget_usd)}" required></label><label>Резерв на один запрос, USD<input name="reserve_usd" type="number" min="0" step="any" value="${esc(m.max_call_usd)}" required></label><p class="field-desc">Нулевой бюджет или резерв останавливает платные запросы. Если точная стоимость неизвестна, учитывается весь резерв.</p>`) +
      '</div><div class="integration-actions"><button class="btn primary" type="submit">Сохранить настройки ИИ</button><button class="btn" type="button" data-refresh>Обновить состояние</button></div><p data-status role="status" aria-live="polite"></p></form>' +
      card('Обработка Telegram', '<p>Запустите получение и разбор постов или соберите дайджест из готовых сюжетов.</p><div class="integration-actions"><button class="btn" data-process>Получить и разобрать посты</button><button class="btn" data-digest>Собрать дайджест</button></div><p data-job-status role="status" aria-live="polite"></p>');
    const form=root.querySelector('form'),status=root.querySelector('[data-status]');
    form.onsubmit=e=>{e.preventDefault();run(form.querySelector('[type=submit]'),status,async()=>{await send('/model/settings',Object.fromEntries(new FormData(form)),'PUT');await ai(root);root.querySelector('[data-status]').textContent='Настройки ИИ сохранены.';});};
    root.querySelector('[data-refresh]').onclick=()=>load('ai',true);
    root.querySelector('[data-process]').onclick=e=>job('/telegram/process',e.target,root.querySelector('[data-job-status]'));
    root.querySelector('[data-digest]').onclick=e=>job('/telegram/digest',e.target,root.querySelector('[data-job-status]'));
  }
  async function load(pane, force=false) {
    const root=roots[pane];if(!root || (loaded.has(pane)&&!force))return;
    loaded.add(pane);root.innerHTML='<p role="status">Загружаем настройки…</p>';
    try{await HTF.ensureToken();await (pane==='ai'?ai:imports)(root);}
    catch(e){loaded.delete(pane);root.textContent=e.message;const retry=document.createElement('button');retry.className='btn';retry.textContent='Повторить';retry.onclick=()=>load(pane);root.append(retry);}
  }
  document.getElementById('settings-sections')?.addEventListener('click',e=>{const b=e.target.closest('[data-settings-pane-btn]');if(b)load(b.dataset.settingsPaneBtn);});
  load(new URLSearchParams(location.search).get('section'));
})();
