'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = (path,options) => HTF.api('/api/research/v1'+path,options);
  const date = value => value ? new Date(value).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})+' МСК' : '—';
  const sourceLink = post => (post.source_kind==='telegram'?'/telegram-feed.html':'/feed.html')+'?post='+encodeURIComponent(post.id);
  const safeLink = (url,label) => /^https?:\/\//i.test(url||'') ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>` : '';
  const bias = direction => direction==='bullish'?'↗ Bullish':direction==='bearish'?'↘ Bearish':'Без направления';
  const messages = {model_not_configured:'Укажите ключ xAI в настройках ИИ',model_rate_limited:'Ожидает лимита запросов Grok',article_unavailable:'Не удалось прочитать ссылку. Вставьте текст статьи.',article_format:'Вставьте текст статьи или загрузите TXT / Markdown.',article_brief_invalid:'Некорректный ответ Grok. Повторите разбор.',model_error:'Ошибка Grok. Можно повторить разбор.'};
  const mutation = body => ({method:'PUT',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(body)});

  if($('tickers-root')){
    let items=[];
    function render(){
      const q=$('ticker-search').value.trim().toLowerCase(),source=$('ticker-source').value;
      const shown=items.map(i=>({...i,posts:i.posts.filter(p=>!source||p.source_kind===source)}))
        .filter(i=>i.posts.length&&[i.symbol,...i.posts.flatMap(p=>[p.title,p.author])].join(' ').toLowerCase().includes(q));
      const sort=$('ticker-sort').value;
      shown.sort((a,b)=>sort==='mentions'?b.posts.length-a.posts.length:sort==='symbol'?a.symbol.localeCompare(b.symbol):b.posts[0].published_at-a.posts[0].published_at);
      $('ticker-list').innerHTML=shown.map(i=>`<tr><td><button class="ticker-symbol" data-ticker="${esc(i.id)}">${esc(i.symbol)}</button><small>${i.asset_uid?'Актив сопоставлен':'Тикер требует проверки'}</small></td><td><strong>${esc(i.posts[0].title)}</strong><small>${esc(i.posts[0].author||'Источник')} · ${i.posts[0].source_kind==='x'?'X':'Telegram'}</small></td><td class="ticker-date">${esc(date(i.posts[0].published_at))}</td><td><span class="feed-bias ${esc(i.posts[0].direction||'pending')}">${bias(i.posts[0].direction)}</span><small>${i.posts.length} упоминаний</small></td><td>${[...new Set(i.posts.map(p=>p.source_kind==='x'?'X':'Telegram'))].join(' · ')}</td></tr>`).join('');
      $('ticker-empty').hidden=shown.length>0;
      $('ticker-list').querySelectorAll('[data-ticker]').forEach(b=>b.onclick=()=>open(items.find(i=>i.id===b.dataset.ticker)));
    }
    function open(item){
      $('ticker-detail').innerHTML=`<p class="now-over">Информационный поток · ${item.mentions} упоминаний</p><h2>${esc(item.symbol)}</h2><p class="research-muted">${item.asset_uid?'Сопоставлен с активом в реестре.':'Упоминание символа пока не подтверждает личность токена.'}</p><div class="ticker-bias-summary"><span class="feed-bias bullish">${item.bullish} Bullish</span><span class="feed-bias bearish">${item.bearish} Bearish</span></div>${item.posts.map(p=>`<article class="ticker-post"><small>${p.source_kind==='x'?'X':'Telegram'} · ${esc(p.author||'Источник')} · ${esc(date(p.published_at))}</small><h3>${esc(p.title)}</h3><span class="feed-bias ${esc(p.direction||'pending')}">${bias(p.direction)}</span><p><a href="${sourceLink(p)}">Открыть разбор →</a> ${safeLink(p.url,'Оригинал')}</p></article>`).join('')}`;
    }
    async function load(){
      $('tickers-refresh').disabled=true;$('flow-status').textContent='Собираем упоминания из X и Telegram…';
      try{await HTF.ensureToken();const r=await api('/feed-tickers');items=r.data.items;render();
        const repeated=[...items].sort((a,b)=>b.mentions-a.mentions).filter(i=>i.mentions>1).slice(0,5);
        const recent=[...items].sort((a,b)=>b.latest_at-a.latest_at).slice(0,4);
        const overview=(title,body)=>`<section class="research-card"><h2>${title}</h2>${body}</section>`;
        const chips=list=>list.map(i=>`<button class="btn small" data-overview="${esc(i.id)}">${esc(i.symbol)} <span>${i.mentions}</span></button>`).join('');
        $('ticker-overview').innerHTML=overview('Активы в потоке',`<strong class="flow-number">${items.length}</strong><p>${r.data.posts_loaded} публикаций из X и Telegram</p>`)+overview('Повторный интерес',chips(repeated)||'<p class="research-muted">Повторных упоминаний пока нет.</p>')+overview('Последние сигналы',chips(recent)||'<p class="research-muted">Появятся после разбора публикаций.</p>');
        $('ticker-overview').querySelectorAll('[data-overview]').forEach(b=>b.onclick=()=>open(items.find(i=>i.id===b.dataset.overview)));
        $('flow-status').textContent=(r.data.capped?'Показаны упоминания из 1000 последних публикаций.':'Все упоминания из сохранённых публикаций.')+' Котировки не подменяются оценками модели.';
      }catch(e){$('flow-status').textContent=e.message;}finally{$('tickers-refresh').disabled=false;}
    }
    $('ticker-search').oninput=render;$('ticker-source').onchange=render;$('ticker-sort').onchange=render;$('tickers-refresh').onclick=load;load();
    return;
  }

  if(!$('library-root'))return;
  let items=[],filter='all',cursor=null,selected=null,selectedRevision=null,refreshTimer=null,loading=false;
  const states={unread:'Не прочитано',read:'Прочитано',reread:'Перечитать',archived:'Архив'};
  function render(){
    const q=$('library-search').value.trim().toLowerCase();
    const shown=items.filter(i=>(filter==='all'?i.status!=='archived':filter==='pinned'?i.pinned&&i.status!=='archived':i.status===filter)&&[i.title,i.url,i.brief?.summary,...(i.brief?.theses||[])].join(' ').toLowerCase().includes(q));
    $('library-summary').innerHTML=`<span><strong>${items.length}</strong> загружено</span><span><strong>${items.filter(i=>i.status==='unread').length}</strong> прочитать</span><span><strong>${items.filter(i=>i.status==='reread').length}</strong> перечитать</span><span><strong>${items.filter(i=>i.brief).length}</strong> разобрано Grok</span>`;
    $('library-list').innerHTML=shown.length?shown.map(i=>`<article class="research-card library-card${i.id===selected?' selected':''}"><div class="feed-section-head"><span class="flow-label">${esc(states[i.status])}${i.pinned?' · Закреплено':''}</span><small>${esc(date(i.created_at))}</small></div><button class="library-title" data-article="${esc(i.id)}">${esc(i.title)}</button><p>${esc(i.brief?.summary||'Материал сохранён. Grok подготовит краткий разбор по существу.')}</p><div class="feed-section-head"><span class="research-muted">${i.brief?'Grok · разбор готов':esc(messages[i.job?.error_code]||({pending:'В очереди Grok',running:'Grok читает статью',failed:'Разбор не выполнен',cancelled:'Разбор остановлен'}[i.job?.status]||'Ожидает разбора'))}</span><button class="btn small" data-article="${esc(i.id)}">Читать →</button></div></article>`).join(''):`<div class="research-card flow-empty"><h2>${items.length?'Ничего не найдено':'Здесь начинается ваша библиотека'}</h2><p>${items.length?'Измените поиск или фильтр.':'Сохраните ссылку, вставьте текст или загрузите файл. Grok выделит тезисы и объяснит, чем материал может быть полезен.'}</p></div>`;
    $('library-list').querySelectorAll('[data-article]').forEach(b=>b.onclick=()=>open(b.dataset.article));
    $('library-more').hidden=!cursor;
  }
  async function open(id,quiet=false){
    if(!quiet){selected=id;selectedRevision=null;render();$('library-detail').innerHTML='<p class="research-muted">Открываем статью…</p>';}
    try{const r=await api('/library/'+encodeURIComponent(id));if(selected!==id)return;const a=r.data;
      if(quiet&&selectedRevision===a.revision_id)return;
      selectedRevision=a.revision_id;
      const b=a.brief,sections=b?[['Ключевые тезисы',b.theses],['Чем это полезно нам',b.usefulness],['Риски и ограничения',b.risks],['Что проверить',b.questions]]:[];
      $('library-detail').innerHTML=`<div class="feed-section-head"><p class="now-over">${b?'Grok · краткий разбор':'Материал'}</p><button class="btn small" id="article-pin">${a.pinned?'★ Открепить':'☆ Закрепить'}</button></div><h2>${esc(a.title)}</h2><p class="research-muted">${esc(date(a.created_at))} ${safeLink(a.url,'Первоисточник')}</p><div class="flow-toolbar"><label>Статус <select id="article-state">${Object.entries(states).map(([v,label])=>`<option value="${v}"${v===a.status?' selected':''}>${label}</option>`).join('')}</select></label><button class="btn" id="article-edit">Изменить</button>${b?'':'<button class="btn primary" id="article-analyze">Разобрать через Grok</button>'}</div><p id="article-status" role="status" class="research-muted"></p>${b?`<section class="article-brief"><h3>Суть</h3><p>${esc(b.summary)}</p>${sections.map(([title,lines])=>`<h3>${title}</h3>${lines.length?'<ul>'+lines.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul>':'<p class="research-muted">Не выделено в материале.</p>'}`).join('')}${b.tickers.length?'<h3>Упомянутые тикеры</h3><p>'+b.tickers.map(t=>'<span class="feed-ticker">'+esc(t)+'</span>').join(' ')+'</p>':''}${a.text_truncated?'<p class="research-muted">Разобраны первые 32 000 символов статьи.</p>':''}<p class="research-muted">Выводы Grok основаны на тексте источника.</p></section>`:'<p>Grok выделит суть, аргументы, применение и ограничения. Для разбора используется ключ xAI из настроек ИИ.</p>'}<details class="article-original"><summary>Текст статьи${a.filename?' · '+esc(a.filename):''}</summary><div class="feed-text">${esc(a.text||'Текст будет загружен по ссылке при разборе. Если сайт ограничивает доступ, вставьте текст через «Изменить».')}</div></details>`;
      const update=async body=>{try{await api('/library/'+id,mutation({...body,expected_revision:a.revision_id}));await open(id);await load();}catch(e){$('article-status').textContent=e.message;}};
      $('article-pin').onclick=()=>update({pinned:!a.pinned});$('article-state').onchange=e=>update({status:e.target.value});$('article-edit').onclick=()=>form(a);
      if($('article-analyze'))$('article-analyze').onclick=async e=>{const button=e.currentTarget;button.disabled=true;try{const r=await api('/library/'+id+'/analyze',{method:'POST',body:'{}'});$('article-status').textContent=r.data.cached?'Разбор уже сохранён.':'Разбор поставлен в очередь Grok.';await load();}catch(error){$('article-status').textContent=error.message;}finally{button.disabled=false;}};
    }catch(e){if(selected===id)$('library-detail').textContent=e.message;}
  }
  function form(article={}){
    const d=document.createElement('dialog');d.className='library-dialog';
    d.innerHTML=`<form id="article-form"><div class="feed-section-head"><h2>${article.id?'Изменить статью':'Добавить статью'}</h2><button class="btn small" type="button" id="article-close" aria-label="Закрыть">✕</button></div><label>Название<input name="title" required maxlength="240" value="${esc(article.title||'')}" placeholder="О чём этот материал"></label><label>Ссылка на статью<input name="url" type="url" value="${esc(article.url||'')}" placeholder="https://…"></label><label>Текст статьи<textarea name="text" rows="10" maxlength="120000" placeholder="Вставьте текст или оставьте ссылку выше">${esc(article.text||'')}</textarea></label><label class="article-upload">Или загрузите файл<input type="file" id="article-file" accept=".txt,.md,.markdown,.html,.htm,text/plain,text/markdown,text/html"><small>TXT, Markdown, HTML · до 1 МБ. Файл будет сохранён как текст.</small></label><p id="article-form-status" role="status" class="research-muted"></p><p class="research-muted">${article.id?'При изменении текста или ссылки сохранённый разбор сбросится.':'Grok начнёт разбор автоматически, когда подключён ключ xAI.'}</p><div class="feed-compose-actions"><button class="btn" type="button" id="article-cancel">Отмена</button><button class="btn primary" id="article-submit">${article.id?'Сохранить':'Сохранить и разобрать'}</button></div></form>`;
    document.body.append(d);d.showModal();d.onclose=()=>{d.remove();clearTimeout(refreshTimer);refreshTimer=setTimeout(()=>load(),15000);};d.querySelector('#article-close').onclick=()=>d.close();d.querySelector('#article-cancel').onclick=()=>d.close();
    let filename=article.filename||'',reading=false;
    d.querySelector('#article-file').onchange=async e=>{const file=e.target.files[0];if(!file)return;
      const note=d.querySelector('#article-form-status');if(file.size>1_000_000){note.textContent='Файл должен быть меньше 1 МБ.';return;}
      reading=true;d.querySelector('#article-submit').disabled=true;
      try{let value=await file.text();if(/\.html?$/i.test(file.name)){const parsed=new DOMParser().parseFromString(value,'text/html');parsed.querySelectorAll('script,style,nav,header,footer,noscript').forEach(n=>n.remove());value=parsed.body.textContent;}
        if(value.length>120000){note.textContent='В файле больше 120 000 символов. Сократите текст.';return;}
        d.querySelector('[name=text]').value=value;filename=file.name;if(!d.querySelector('[name=title]').value)d.querySelector('[name=title]').value=file.name.replace(/\.[^.]+$/,'').slice(0,240);note.textContent='Файл прочитан: '+file.name;
      }catch{note.textContent='Не удалось прочитать файл.';}finally{reading=false;d.querySelector('#article-submit').disabled=false;}
    };
    d.querySelector('form').onsubmit=async e=>{e.preventDefault();if(reading)return;
      const button=d.querySelector('#article-submit');button.disabled=true;
      const fields=new FormData(e.currentTarget);const body={title:fields.get('title'),url:fields.get('url')||null,text:fields.get('text'),filename};
      try{let r;if(article.id){r=await api('/library/'+article.id,mutation({...body,expected_revision:article.revision_id}));if(!r.data.brief)await api('/library/'+article.id+'/analyze',{method:'POST',body:'{}'});}else{r=await api('/library',{...mutation(body),method:'POST'});}
        d.close();filter='all';setFilter();$('library-search').value='';await load();await open(r.data.id);$('flow-status').textContent='Материал сохранён.';
      }catch(error){d.querySelector('#article-form-status').textContent=error.message;}finally{button.disabled=false;}
    };
  }
  function setFilter(){document.querySelectorAll('[data-library-filter]').forEach(b=>{const active=b.dataset.libraryFilter===filter;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});render();}
  async function load(append=false){
    if(loading)return;loading=true;clearTimeout(refreshTimer);
    try{await HTF.ensureToken();const previousCount=append?0:items.length;
      let r=await api('/library?limit=100'+(append&&cursor?'&cursor='+encodeURIComponent(cursor):'')),loaded=r.data.items;
      while(!append&&r.data.next_cursor&&loaded.length<previousCount){r=await api('/library?limit=100&cursor='+encodeURIComponent(r.data.next_cursor));loaded.push(...r.data.items);}
      items=append?[...items,...loaded]:loaded;cursor=r.data.next_cursor;render();
      $('flow-status').textContent=cursor?`Загружено ${items.length} из ${r.data.total} материалов.`:r.data.total?'Библиотека обновлена.':'Добавьте первый материал — ссылка, текст или файл.';
      if(selected)await open(selected,true);
    }catch(e){$('flow-status').textContent=e.message;}finally{loading=false;if(!document.querySelector('.library-dialog'))refreshTimer=setTimeout(()=>{if(!document.hidden)load();else refreshTimer=setTimeout(()=>load(),15000);},15000);}
  }
  $('library-add').onclick=()=>form();$('library-first').onclick=()=>form();$('library-more').onclick=()=>load(true);$('library-search').oninput=render;
  document.querySelectorAll('[data-library-filter]').forEach(b=>b.onclick=()=>{filter=b.dataset.libraryFilter;setFilter();});load();
})();
