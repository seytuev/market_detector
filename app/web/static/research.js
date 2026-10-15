/* Integrated STVTerminal research views. Technical chart state is never mutated. */
'use strict';
window.ResearchUI = (() => {
  const BASE = '/api/research/v1';
  const esc = value => String(value == null ? '' : value).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const KINDS = {unlock:'Разлок',listing:'Листинг',delisting:'Делистинг',launch:'Запуск',upgrade:'Обновление сети',governance:'Голосование',tokenomics:'Токеномика',security:'Безопасность',regulatory:'Регулирование',macro:'Макро',expiry:'Экспирация',other:'Другое'};
  const STAGES = {rumor:'Слух',proposal:'Предложение',announced:'Объявлено',live:'Запущено',denied:'Опровергнуто',cancelled:'Отменено'};
  const QUALITY = {ok:'Данные обновлены',partial:'Данные неполны',stale:'Последние сохранённые данные',unavailable:'Источник не подключён',disabled:'Раздел выключен'};
  const STATE = {scheduled:'Запланировано',postponed:'Перенесено',unscheduled:'Дата не определена',passed_unconfirmed:'Дата прошла, исполнение не подтверждено',completed:'Исполнение подтверждено',cancelled:'Отменено'};
  const SOURCE_ERROR = {auth_error:'Токен экспорта не принят',source_unavailable:'Источник временно недоступен',not_configured:'Источник не настроен',schema_error:'Формат источника изменился',rate_limited:'Ожидаем снятия ограничения частоты'};
  const fmt = ms => ms ? new Date(ms).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',dateStyle:'short',timeStyle:'short'})+' МСК' : '—';
  const dateMSK = ms => new Date(ms).toLocaleDateString('sv-SE',{timeZone:'Europe/Moscow'});
  const options = (map,selected) => Object.entries(map).map(([id,label])=>'<option value="'+esc(id)+'"'+(id===selected?' selected':'')+'>'+esc(label)+'</option>').join('');
  let configPromise, contextSeq=0, selectedInstrument=null, calendarState, calendarSeq=0;
  let overviewData=[],overviewAt=0,overviewPending=null;
  const api = (path,options={}) => HTF.api(BASE+path,options);
  const send = (path,body,method='POST') => api(path,{method,headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(body)});
  const config = () => configPromise || (configPromise=api('/config').then(r=>r.data));
  const safeLink = (url,label) => /^https?:\/\//i.test(url||'') ? '<a href="'+esc(url)+'" target="_blank" rel="noopener noreferrer">'+esc(label)+'</a>' : esc(label);
  function badge(value){return '<span class="research-badge">'+esc(value)+'</span>';}
  function dateLabel(event){
    if(event.precision==='unknown') return event.date_label || 'Дата уточняется';
    if(event.precision==='exact') return fmt(event.starts_at);
    return (event.date_label||'Период уточняется')+(event.timezone?' · '+event.timezone:' · время и пояс не указаны');
  }
  function notice(meta){return '<p class="research-muted">'+esc(QUALITY[meta.quality]||'Проверяем данные')+' · Источник обновлён: '+esc(fmt(meta.last_success_at))+'</p>';}
  function dialog(title,html){
    const d=document.createElement('dialog');d.className='research-dialog';
    d.innerHTML='<h2>'+esc(title)+'</h2>'+html+'<div class="research-toolbar"><button type="button" class="btn" data-close>Закрыть</button></div>';
    d.querySelector('[data-close]').onclick=()=>d.close();
    d.addEventListener('close',()=>d.remove());document.body.appendChild(d);d.showModal();return d;
  }
  function errorBox(container,error){container.innerHTML='<p class="research-error" role="alert">'+esc(error.message||'Не удалось загрузить данные')+'</p>';}
  async function material(id){
    const response=await api('/materials/'+encodeURIComponent(id)),m=response.data;
    const d=dialog(m.title||'Исходный материал','<p>'+safeLink(m.canonical_url,'Открыть оригинал')+'</p>'+
      '<p class="research-muted">Опубликовано: '+esc(fmt(m.published_at))+' · Получено: '+esc(fmt(m.available_at))+'</p>'+
      '<blockquote>'+esc(m.original_text)+'</blockquote>'+
      (m.analyses||[]).map(a=>'<h3>Сохранённый разбор</h3><p>'+esc(a.summary||'')+'</p>'+
        (a.claims||[]).map(c=>'<p>'+badge(c.verified?'Цитата проверена':'Нужна проверка')+esc(c.text)+'</p><blockquote>'+esc(c.quote)+'</blockquote>').join('')).join('')+
      '<details><summary>История версий</summary><ul>'+m.versions.map(v=>'<li>'+esc(fmt(v.available_at))+' · '+esc(v.revision_id)+'</li>').join('')+'</ul></details>');
    window.ResearchWorkbench?.sourceActions(d,{kind:'material',id:m.id,revision_id:m.revision_id}).catch(()=>{});
    return d;
  }
  async function refreshContext(instrumentId){
    selectedInstrument=Number(instrumentId)||null;
    const box=document.getElementById('research-asset-context');if(!box)return;
    const seq=++contextSeq;box.innerHTML='';box.hidden=true;
    if(!selectedInstrument)return;
    try{
      const cfg=await config();if(seq!==contextSeq||!cfg.enabled||!cfg.context_ui_enabled)return;
      box.hidden=false;box.innerHTML='<p class="research-muted">Загружаем информацию по активу…</p>';
      const response=await api('/instruments/'+selectedInstrument+'/context');
      if(seq!==contextSeq)return;
      const data=response.data;
      if(data.identity!=='verified'){
        box.innerHTML='<h3>Информация по активу</h3><p>Нужно сопоставить актив с источниками.</p><a href="/research.html?tab=identity">Сопоставить актив →</a>';return;
      }
      box.innerHTML='<h3>Информация по активу</h3>'+notice(response.meta)+
        '<p class="research-muted">Срез информации: '+esc(fmt(response.meta.as_of))+'</p>'+
        (data.facts.length?'<ul class="research-detail-list">'+data.facts.slice(0,3).map(f=>'<li>'+esc(f.text)+' <button class="btn small" data-material="'+esc(f.material_id)+'">Источник</button></li>').join('')+'</ul>':
        '<p>В выбранных источниках за последние сутки новых проверенных фактов нет.</p>')+
        (data.summary&&!data.summary.retracted?'<p>'+esc(data.summary.headline||'')+(data.summary.superseded?' · Сводка требует обновления':'')+'</p>':'')+
        (data.consensus?'<p class="research-muted">'+esc(data.consensus.label)+' · '+data.consensus.sources24h+' источников в подборке</p>':'')+
        (data.upcoming.length?'<h4>Ближайшие события</h4>'+data.upcoming.map(e=>'<p><button class="btn small" data-catalyst="'+esc(e.id)+'">'+esc(e.title)+'</button><br><span class="research-muted">'+esc(dateLabel(e))+'</span></p>').join(''):'')+
        '<details><summary>Источники и подробности</summary>'+
        (data.materials.length?data.materials.map(m=>'<p><button class="btn small" data-material="'+esc(m.id)+'">'+esc(m.title||m.author||'Материал')+'</button></p>').join(''):'<p>Свежих материалов нет. Архив доступен отдельно.</p>')+
        (data.risks.length?'<h4>Риски</h4><ul>'+data.risks.map(r=>'<li>'+esc(typeof r==='string'?r:r.text)+'</li>').join('')+'</ul>':'')+
        (data.checks.length?'<h4>Что проверить</h4><ul>'+data.checks.map(r=>'<li>'+esc(typeof r==='string'?r:r.text)+'</li>').join('')+'</ul>':'')+
        '<button class="btn small" type="button" data-archive="'+esc(data.asset_uid)+'">Материалы за 30 дней</button></details>';
      bindDetails(box);
      box.querySelector('[data-archive]').onclick=()=>archive(data.asset_uid).catch(e=>dialog('Ошибка','<p>'+esc(e.message)+'</p>'));
      window.ResearchWorkbench?.context(box,data).catch(()=>{});
    }catch(e){if(seq===contextSeq){box.hidden=false;errorBox(box,e);}}
  }
  async function archive(assetUid,after=null,container=null){
    const fromAt=container?.dataset.archiveFrom||String(Date.now()-30*86400000);
    const response=await api('/assets/'+assetUid+'/materials?limit=30&from='+fromAt+(after?'&cursor='+encodeURIComponent(after):''));
    const html=response.data.items.map(m=>'<article class="research-card"><button type="button" class="btn small" data-material="'+esc(m.id)+'">'+esc(m.title||m.author||'Материал')+'</button><p class="research-muted">'+esc(fmt(m.published_at))+'</p></article>').join('')||'<p>Материалов за выбранный период нет.</p>';
    const d=container||dialog('Материалы актива','<div data-archive-list></div>');
    d.dataset.archiveFrom=fromAt;
    const list=d.querySelector('[data-archive-list]');list.insertAdjacentHTML('beforeend',html);bindDetails(d);
    d.querySelector('[data-more]')?.remove();
    if(response.data.next_cursor){
      const b=document.createElement('button');b.className='btn';b.dataset.more='1';b.textContent='Ещё материалы';
      b.onclick=async()=>{b.disabled=true;await archive(assetUid,response.data.next_cursor,d);};list.appendChild(b);
    }
  }
  function bindDetails(container){
    container.querySelectorAll('[data-material]').forEach(b=>b.onclick=()=>material(b.dataset.material).catch(e=>dialog('Ошибка','<p>'+esc(e.message)+'</p>')));
    container.querySelectorAll('[data-catalyst]').forEach(b=>b.onclick=()=>detail(b.dataset.catalyst).catch(e=>dialog('Ошибка','<p>'+esc(e.message)+'</p>')));
  }
  async function detail(id){
    const response=await api('/catalysts/'+encodeURIComponent(id)),e=response.data;
    const d=dialog(e.title,'<p>'+badge(KINDS[e.kind]||e.kind)+badge(STAGES[e.stage]||e.stage)+badge(STATE[e.date_status]||e.date_status)+'</p>'+
      '<p>'+esc(dateLabel(e))+'</p><p>'+esc(e.summary)+'</p>'+
      (e.conflicts?.length?'<p class="research-warning">Есть расхождения источников. Требуется проверка.</p>':'')+
      '<p class="research-muted">'+esc((e.priority_reasons||[]).join(' · '))+'</p>'+
      '<h3>Источники</h3>'+e.sources.map(s=>'<article class="research-card">'+safeLink(s.url,s.namespace||'Источник')+'<blockquote>'+esc(s.quote||s.text||'')+'</blockquote></article>').join('')+
      (e.split_into?'<p>Событие разделено:</p>'+e.split_into.map(id=>'<p><button class="btn small" data-catalyst="'+esc(id)+'">'+esc(id)+'</button></p>').join(''):'')+
      '<div class="research-toolbar"><button class="btn" data-edit>Исправить</button><button class="btn" data-history>История изменений</button>'+
      (e.sources.length>=2?'<button class="btn" data-split>Разделить событие</button>':'')+'</div><div data-history-list></div>');
    d.querySelector('[data-edit]').onclick=()=>{d.close();edit(e);};
    d.querySelector('[data-history]').onclick=async()=>{
      const r=await api('/catalysts/'+e.id+'/history');
      d.querySelector('[data-history-list]').innerHTML=r.data.items.map(v=>'<article class="research-card"><p>'+esc(fmt(v.available_at))+' · '+esc(v.reason)+'</p><p>'+esc(v.title)+' · '+esc(dateLabel(v))+' · '+esc(STAGES[v.stage])+'</p></article>').join('');
    };
    if(d.querySelector('[data-split]')) d.querySelector('[data-split]').onclick=()=>{
      const form=dialog('Разделить источники','<p>Распределите все источники между двумя событиями.</p><form data-split-form>'+
        e.sources.map((s,i)=>'<label class="research-card" style="display:block">'+esc(s.text||s.url||s.id)+'<select name="source-'+i+'"><option value="0">Событие 1</option><option value="1">Событие 2</option></select></label>').join('')+
        '<label>Причина <input required name="reason" minlength="5"></label><p class="research-error" data-error></p><button class="btn primary">Разделить</button></form>');
      form.querySelector('form').onsubmit=async event=>{
        event.preventDefault();const fd=new FormData(event.target),groups=[{source_ids:[]},{source_ids:[]}];
        e.sources.forEach((s,i)=>groups[Number(fd.get('source-'+i))].source_ids.push(s.id));
        try{await send('/catalysts/'+e.id+'/split',{expected_revision:e.revision_id,reason:fd.get('reason'),groups});form.close();d.close();reloadCalendar();}
        catch(error){form.querySelector('[data-error]').textContent=error.message;}
      };
    };
    window.ResearchWorkbench?.sourceActions(d,{kind:'catalyst',id:e.id,revision_id:e.revision_id},e.asset_uids[0]||null).catch(()=>{});
    bindDetails(d);return d;
  }
  async function edit(value={},candidate=null){
    const assets=(await api('/assets?limit=100')).data.items;
    const existing=candidate?.existing_catalyst_id?(await api('/catalysts/'+encodeURIComponent(candidate.existing_catalyst_id))).data:null;
    const initial=value.precision||'unknown';
    const d=dialog(candidate?'Проверить кандидат':value.id?'Изменить событие':'Новый катализатор',
      (candidate?'<blockquote>'+esc(candidate.quote||candidate.summary||'')+'</blockquote>':'')+
      '<form class="research-form"><label class="wide">Название<input name="title" required minlength="5" maxlength="180" value="'+esc(value.title||'')+'"></label>'+
      '<label>Тип<select name="kind">'+options(KINDS,value.kind||'other')+'</select></label>'+
      '<label>Стадия<select name="stage">'+options(STAGES,value.stage||'announced')+'</select></label>'+
      '<label>Область<select name="scope">'+options({asset:'Выбранный актив',global:'Общий контекст'},value.scope||'asset')+'</select></label>'+
      '<label>Активы<select name="assets" multiple size="4">'+assets.map(a=>'<option value="'+esc(a.asset_uid)+'"'+((value.asset_uids||[]).includes(a.asset_uid)?' selected':'')+'>'+esc(a.display_symbol+' · '+a.name)+'</option>').join('')+'</select></label>'+
      '<label>Точность даты<select name="precision">'+options({unknown:'Дата неизвестна',exact:'Точное время',day:'День',week:'Неделя',month:'Месяц',quarter:'Квартал'},initial)+'</select></label>'+
      '<label>Дата или период<input name="date_label" placeholder="2026-10-20 / 2026-10 / 2026-Q4" value="'+esc(value.date_label||'')+'"></label>'+
      '<label>Время (для точной даты)<input name="time_label" type="time" value="'+esc(value.precision==='exact'&&value.starts_at?new Date(value.starts_at).toISOString().slice(11,16):'')+'"></label>'+
      '<label>Часовой пояс<select name="timezone">'+options({'':'Не указан',UTC:'UTC','Europe/Moscow':'Москва','America/New_York':'Нью-Йорк'},value.timezone||'')+'</select></label>'+
      '<label class="wide">Описание<textarea name="summary" maxlength="6000">'+esc(value.summary||'')+'</textarea></label>'+
      '<label class="wide">Новый источник (URL)<input type="url" name="source_url" placeholder="https://..."></label>'+
      '<label class="wide">Что подтверждает источник<textarea name="source_text"></textarea></label>'+
      '<label class="wide">Причина сохранения<input name="reason" required minlength="5" value="'+esc(value.id?'':'Сохранено после проверки')+'"></label>'+
      '<p class="research-error wide" data-error role="alert"></p><div class="research-toolbar wide"><button class="btn primary" type="submit">'+(candidate?'Принять после проверки':'Сохранить')+'</button></div></form>');
    d.querySelector('form').onsubmit=async event=>{
      event.preventDefault();const form=event.target,fd=new FormData(form),button=form.querySelector('[type=submit]');button.disabled=true;
      const body={title:fd.get('title'),kind:fd.get('kind'),stage:fd.get('stage'),scope:fd.get('scope'),
        asset_uids:fd.getAll('assets'),precision:fd.get('precision'),date_label:fd.get('date_label'),
        timezone:fd.get('timezone')||null,summary:fd.get('summary'),reason:fd.get('reason'),sources:[...(value.sources||[])]};
      if(body.precision==='exact')body.time_label=fd.get('time_label');
      if(fd.get('source_url')||fd.get('source_text'))body.sources.push({namespace:'manual',url:fd.get('source_url'),text:fd.get('source_text')});
      try{
        if(candidate)await send('/catalyst-candidates/'+candidate.id+'/review',{action:'accept',expected_revision:candidate.revision_id,reason:body.reason,correction:body,
          ...(existing?{catalyst_id:existing.id,catalyst_revision:existing.revision_id}:{})});
        else await send('/catalysts'+(value.id?'/'+value.id:''),{...body,...(value.id?{expected_revision:value.revision_id}:{})},value.id?'PATCH':'POST');
        d.close();await reloadCalendar();
      }catch(error){form.querySelector('[data-error]').textContent=error.message;}
      finally{button.disabled=false;}
    };
    return d;
  }
  function eventCard(e,candidate=false){
    return '<article class="research-card">'+(!candidate?'<label class="research-muted"><input type="checkbox" data-merge="'+esc(e.id)+'"> Выбрать для объединения</label>':'')+
      '<h3>'+esc(e.title)+'</h3><p>'+badge(KINDS[e.kind]||'Другое')+badge(STAGES[e.stage]||'Требует проверки')+'</p>'+
      '<p>'+esc(dateLabel(e))+'</p><p>'+esc(e.summary||'')+'</p>'+
      (e.conflicts?.length?'<p class="research-warning">Есть расхождения источников</p>':'')+
      '<div class="research-toolbar">'+(candidate?'<button class="btn small" data-review="'+esc(e.id)+'">Проверить</button><button class="btn small" data-reject="'+esc(e.id)+'">Отклонить</button>':
      '<button class="btn small" data-catalyst="'+esc(e.id)+'">Источники и история</button>')+'</div></article>';
  }
  async function mountCalendar(container,reset=false){
    if(reset||!calendarState||calendarState.container!==container||!container.querySelector('[data-catalyst-list]')){
      const start=dateMSK(Date.now());calendarState={container,view:'list',day:start,range:'month',filter:'all',asset:'',items:[],after:null,selected:new Map()};
      container.innerHTML='<section class="research-block"><div class="research-toolbar">'+
        '<button class="btn primary" data-create>Добавить событие</button><button class="btn" data-merge-events>Объединить выбранное</button>'+
        '<button class="btn" data-refresh>Обновить</button></div>'+
        '<div class="research-toolbar"><label>Показать<select data-filter><option value="all">Предстоящие</option><option value="review">На проверке</option><option value="undated">Без точной даты</option><option value="macro">Макро</option></select></label>'+
        '<label>Вид<select data-view><option value="list">Список</option><option value="week">Неделя</option><option value="month">Месяц</option></select></label>'+
        '<label>Дата<input data-day type="date" value="'+start+'"></label><label>Актив<select data-asset><option value="">Все доступные</option></select></label></div>'+
        '<p class="research-status" data-status role="status"></p><div data-catalyst-list></div><button class="btn" data-more hidden>Ещё события</button></section>';
      container.querySelector('[data-create]').onclick=()=>edit().catch(e=>errorBox(container,e));
      container.querySelector('[data-refresh]').onclick=()=>reloadCalendar();
      for(const [selector,key] of [['[data-filter]','filter'],['[data-view]','view'],['[data-day]','day'],['[data-asset]','asset']]){
        container.querySelector(selector).onchange=event=>{calendarState[key]=event.target.value;reloadCalendar();};
      }
      container.querySelector('[data-more]').onclick=()=>reloadCalendar(true);
      container.querySelector('[data-merge-events]').onclick=()=>{
        const selected=[...calendarState.selected.values()];
        if(selected.length<2){container.querySelector('[data-status]').textContent='Выберите минимум два события.';return;}
        const d=dialog('Объединить события','<p>Проверьте, что это один катализатор, а не разные раунды или релизы.</p><ul>'+selected.map(e=>'<li>'+esc(e.title)+' · '+esc(dateLabel(e))+'</li>').join('')+'</ul>'+
          '<form><label>Причина <input name="reason" minlength="5" required></label><p class="research-error" data-error></p><button class="btn primary">Объединить</button></form>');
        d.querySelector('form').onsubmit=async event=>{
          event.preventDefault();try{await send('/catalysts/merge',{ids:selected.map(e=>e.id),expected_revisions:Object.fromEntries(selected.map(e=>[e.id,e.revision_id])),reason:new FormData(event.target).get('reason')});d.close();calendarState.selected.clear();await reloadCalendar();}
          catch(e){d.querySelector('[data-error]').textContent=e.message;}
        };
      };
      const assets=(await api('/assets?limit=100')).data.items;
      const select=container.querySelector('[data-asset]');
      if(!select)return;
      select.insertAdjacentHTML('beforeend',assets.map(a=>'<option value="'+esc(a.asset_uid)+'">'+esc(a.display_symbol+' · '+a.name)+'</option>').join(''));
    }
    await reloadCalendar();
  }
  async function reloadCalendar(append=false){
    const state=calendarState;if(!state||!document.contains(state.container))return;
    const seq=++calendarSeq,container=state.container,status=container.querySelector('[data-status]'),list=container.querySelector('[data-catalyst-list]');
    status.textContent='Читаем сохранённые события…';
    try{
      const cfg=await config();if(seq!==calendarSeq)return;
      if(!cfg.enabled){status.textContent='Исследовательский раздел выключен. Включение доступно в серверных настройках.';return;}
      const params=new URLSearchParams({limit:'100'});
      if(state.asset)params.set('asset_uid',state.asset);
      if(state.filter==='macro')params.set('kind','macro');
      if(state.filter==='undated')params.set('unscheduled','true');
      if(state.filter!=='review'&&state.filter!=='undated'){
        const from=new Date((state.view==='month'?state.day.slice(0,7)+'-01':state.day)+'T00:00:00+03:00');
        const until=new Date(from);until.setUTCDate(until.getUTCDate()+(state.view==='week'?7:state.view==='month'?42:60));
        params.set('from',String(from.getTime()));params.set('to',String(until.getTime()));
      }
      if(append&&state.after)params.set('cursor',state.after);
      const response=await api((state.filter==='review'?'/catalyst-candidates?':'/catalysts?')+params);
      if(seq!==calendarSeq||state!==calendarState)return;
      state.items=append?state.items.concat(response.data.items):response.data.items;state.after=response.data.next_cursor;
      status.textContent=(state.filter==='review'?'Кандидатов: ':'Событий в подборке: ')+state.items.length+' · '+(QUALITY[response.meta.quality]||'Данные сохранены');
      if(state.view==='list'||state.filter==='review'||state.filter==='undated'){
        list.innerHTML=state.items.map(e=>eventCard(e,state.filter==='review')).join('')||'<p>Для выбранных условий событий нет.</p>';
      }else{
        const start=new Date((state.view==='month'?state.day.slice(0,7)+'-01':state.day)+'T00:00:00+03:00');
        const days=state.view==='week'?7:42,periods=state.items.filter(e=>!['exact','day'].includes(e.precision));
        let grid='<div class="research-calendar">';
        for(let i=0;i<days;i++){
          const stamp=start.getTime()+i*86400000,key=dateMSK(stamp);
          const dayItems=state.items.filter(e=>['exact','day'].includes(e.precision)&&(e.precision==='day'?e.date_label===key:dateMSK(e.starts_at)===key));
          grid+='<div class="research-day"><span>'+esc(key.slice(8)+' '+new Date(stamp).toLocaleDateString('ru-RU',{timeZone:'Europe/Moscow',month:'short'}))+'</span>'+
            dayItems.map(e=>'<button class="research-day-entry" type="button" data-catalyst="'+esc(e.id)+'">'+esc(e.title)+'</button>').join('')+
            (dayItems.length?'<button type="button" class="research-day-count" data-day-events="'+key+'" aria-label="События '+key+': '+dayItems.length+'">'+dayItems.length+'</button>':'')+'</div>';
        }
        list.innerHTML=grid+'</div><div class="research-mobile-events"><h3>События периода</h3>'+state.items.filter(e=>['exact','day'].includes(e.precision)).map(e=>eventCard(e)).join('')+'</div>'+
          (periods.length?'<h3>События с точностью до периода</h3>'+periods.map(e=>eventCard(e)).join(''):'');
        list.querySelectorAll('[data-day-events]').forEach(b=>b.onclick=()=>{
          const items=state.items.filter(e=>e.precision==='day'?e.date_label===b.dataset.dayEvents:e.precision==='exact'&&dateMSK(e.starts_at)===b.dataset.dayEvents);
          const d=dialog('События '+b.dataset.dayEvents,items.map(e=>eventCard(e)).join(''));bindDetails(d);
        });
      }
      container.querySelector('[data-more]').hidden=!state.after;
      bindDetails(list);
      list.querySelectorAll('[data-merge]').forEach(b=>{b.checked=state.selected.has(b.dataset.merge);b.onchange=()=>{
        if(b.checked)state.selected.set(b.dataset.merge,state.items.find(e=>e.id===b.dataset.merge));else state.selected.delete(b.dataset.merge);
      };});
      list.querySelectorAll('[data-review]').forEach(b=>b.onclick=()=>{const e=state.items.find(e=>e.id===b.dataset.review);edit(e,e);});
      list.querySelectorAll('[data-reject]').forEach(b=>b.onclick=async()=>{
        const e=state.items.find(e=>e.id===b.dataset.reject);
        const d=dialog('Отклонить кандидат','<form><label>Причина<input required minlength="5" name="reason"></label><p data-error class="research-error"></p><button class="btn">Отклонить</button></form>');
        d.querySelector('form').onsubmit=async event=>{event.preventDefault();try{
          await send('/catalyst-candidates/'+e.id+'/review',{action:'reject',expected_revision:e.revision_id,reason:new FormData(event.target).get('reason')});d.close();reloadCalendar();
        }catch(error){d.querySelector('[data-error]').textContent=error.message;}};
      });
    }catch(error){if(seq===calendarSeq)status.textContent=error.message;}
  }
  async function identity(container){
    const [assets,instruments,links,candidates]=await Promise.all([api('/assets?limit=100'),HTF.api('/api/instruments'),api('/identity/instruments'),api('/identity/candidates')]);
    container.innerHTML='<section class="research-block"><h2>Сопоставление активов</h2><p>Один актив может торговаться на нескольких рынках. Одинаковые тикеры не объединяются автоматически.</p>'+
      '<button class="btn" data-new-asset>Добавить подтверждённый актив</button><div data-links></div><h3>Упоминания и связи на проверке</h3><div data-identity-review></div><p class="research-status" data-status></p></section>';
    const review=container.querySelector('[data-identity-review]');
    review.innerHTML=candidates.data.items.length?candidates.data.items.map(c=>'<p>'+esc(c.symbol||instruments.find(i=>i.id===c.instrument_id)?.symbol||'Актив')+' · '+esc(c.reason)+
      ' <button class="btn small" data-identity-review-id="'+esc(c.id)+'">Проверить</button></p>').join(''):'<p class="research-muted">Нет неразобранных упоминаний.</p>';
    review.querySelectorAll('[data-identity-review-id]').forEach(button=>button.onclick=()=>{
      const candidate=candidates.data.items.find(c=>c.id===button.dataset.identityReviewId);
      const allowed=candidate.candidate_asset_uids||candidate.candidate_assets||[];
      const choices=assets.data.items.filter(a=>allowed.includes(a.asset_uid));
      const current=links.data.items.find(l=>l.instrument_id===candidate.instrument_id)?.link;
      const d=dialog('Проверить упоминание', '<p>'+esc(candidate.reason)+'</p>'+(candidate.source_url?'<p>'+safeLink(candidate.source_url,'Оригинал публикации')+'</p>':'')+
        '<form><label>Решение<select name="action"><option value="resolve">Подтвердить актив</option><option value="reject">Отклонить</option></select></label><label>Актив<select name="asset_uid">'+
        choices.map(a=>'<option value="'+esc(a.asset_uid)+'">'+esc(a.display_symbol+' · '+a.name)+'</option>').join('')+
        '</select></label><label>Основание<input name="reason" required minlength="5"></label><p class="research-error" data-error></p><button class="btn primary">Сохранить решение</button></form>');
      d.querySelector('form').onsubmit=async event=>{event.preventDefault();const fd=new FormData(event.target);try{
        await send('/identity/candidates/'+encodeURIComponent(candidate.id)+'/resolve',{action:fd.get('action'),asset_uid:fd.get('asset_uid'),
          expected_revision:candidate.revision_id,link_revision:current?.revision_id||null,reason:fd.get('reason')});d.close();await identity(container);
      }catch(e){d.querySelector('[data-error]').textContent=e.message;}};
    });
    const list=container.querySelector('[data-links]');
    instruments.forEach((i,index)=>{
      const current=links.data.items.find(l=>l.instrument_id===i.id)?.link?.asset_uid||'';
      list.insertAdjacentHTML('beforeend','<div class="research-identity"><span>'+esc(i.symbol+' · '+i.venue+' · '+i.market_type)+'</span><select data-link="'+i.id+'"><option value="">Не сопоставлен</option>'+
        assets.data.items.map(a=>'<option value="'+esc(a.asset_uid)+'"'+(a.asset_uid===current?' selected':'')+'>'+esc(a.display_symbol+' · '+a.name)+'</option>').join('')+'</select><button class="btn small" data-save="'+i.id+'">Сохранить связь</button></div>');
    });
    list.querySelectorAll('[data-save]').forEach(b=>b.onclick=async()=>{
      const id=Number(b.dataset.save),asset=list.querySelector('[data-link="'+id+'"]').value;
      const a=await api('/assets/'+encodeURIComponent(asset)).catch(()=>null);
      const current=links.data.items.find(l=>l.instrument_id===id)?.link;
      const d=dialog('Подтвердить связь','<p>'+esc(instruments.find(i=>i.id===id).symbol)+' → '+esc(a?.data.name||'Без связи')+'</p><form><label>Основание<input name="reason" required minlength="5"></label><p data-error class="research-error"></p><button class="btn primary">Сохранить</button></form>');
      d.querySelector('form').onsubmit=async event=>{
        event.preventDefault();try{
          await send('/instruments/'+id+'/asset-link',{asset_uid:asset||null,expected_revision:current?.revision_id||null,reason:new FormData(event.target).get('reason')});d.close();await identity(container);
        }catch(e){d.querySelector('[data-error]').textContent=e.message;}
      };
    });
    container.querySelector('[data-new-asset]').onclick=()=>{
      const d=dialog('Подтверждённый актив','<form class="research-form"><label>Тикер<input name="symbol" required></label><label>Название<input name="name" required></label>'+
        '<label>Класс<select name="asset_class">'+options({crypto:'Криптоактив',equity:'Акция',index:'Индекс',cash:'Денежная единица'},'crypto')+'</select></label>'+
        '<label>Каталог<select name="namespace">'+options({coingecko:'CoinGecko',cmc:'CoinMarketCap',contract:'Контракт токена'},'coingecko')+'</select></label>'+
        '<label class="wide">Идентификатор (для контракта: сеть:адрес)<input name="external" required></label><p data-error class="research-error wide"></p><button class="btn primary">Создать</button></form>');
      d.querySelector('form').onsubmit=async event=>{event.preventDefault();const fd=new FormData(event.target);try{
        await send('/assets',{display_symbol:fd.get('symbol'),name:fd.get('name'),asset_class:fd.get('asset_class'),external_ids:[{namespace:fd.get('namespace'),external_id:fd.get('external')}]});d.close();await identity(container);
      }catch(e){d.querySelector('[data-error]').textContent=e.message;}};
    };
  }
  async function sources(container){
    const [r,q,channels]=await Promise.all([api('/catalysts/source-status'),api('/integration/quarantine'),api('/channels')]);
    const source=r.data.terminal;
    container.innerHTML='<section class="research-block"><h2>Данные и обновление</h2>'+
      '<p>Сохранённые материалы и внешние источники. Отсутствие новых публикаций отличается от недоступности источника.</p>'+
      '<p>Архив терминала: '+esc(fmt(source.last_success_at))+'</p>'+
      (source.error_code?'<p class="research-warning">'+esc(SOURCE_ERROR[source.error_code]||'Обновление источника требует проверки')+'</p>':'')+
      '<div class="research-toolbar">'+(source.configured?'<button class="btn" data-sync>Импортировать изменения терминала</button>':'')+
      (r.data.deribit.enabled?'<button class="btn" data-collect>Обновить каталог экспираций</button>':'')+'</div>'+
      '<h3>Deribit</h3>'+['BTC','ETH'].map(k=>'<p>'+k+': '+esc(fmt(r.data.deribit[k].last_success_at))+' · '+
        esc(r.data.deribit[k].coverage_note||'Источник ещё не проверен')+'</p>').join('')+
      '<p>Записей на проверке импорта: '+q.data.items.length+'</p><div data-quarantine>'+
      q.data.items.map(item=>'<p>'+esc(item.entity_kind+' · '+item.reason)+' <button class="btn small" data-retry="'+esc(item.id)+'">Повторить проверку</button></p>').join('')+
      '</div><p class="research-status" data-status></p>'+
      '<h3>Публичные Telegram-каналы</h3><p class="research-muted">Читаются через веб-превью. Закрытые каналы требуют отдельного подключения. Оригиналы сохраняются без платного ИИ-разбора.</p>'+
      '<form data-channel-form class="research-toolbar"><label>Имя канала<input name="handle" placeholder="channel_name" required></label><label><input type="checkbox" name="enabled"> Читать новые публикации</label><button class="btn">Сохранить канал</button></form>'+
      channels.data.items.map(c=>'<p>'+esc(c.title||c.handle)+' · '+(c.enabled?'включён':'выключен')+' · '+esc(fmt(c.source_state.last_success_at))+
        (c.source_state.error_code?' · веб-превью недоступно':'')+
        (c.enabled?' <button class="btn small" data-channel="'+esc(c.handle)+'">Получить публикации</button>':'')+'</p>').join('')+
      '<h3>Напоминания</h3><p class="research-muted">Изначально выключены. Доставка включается отдельно в серверных настройках; подписки задают только будущие напоминания.</p>'+
      '<button class="btn" data-subscribe>Подписаться на общие события за сутки</button></section>';
    const status=container.querySelector('[data-status]');
    async function job(path,body){
      const result=await send(path,body||{});status.textContent='Задание поставлено в очередь.';
      const timer=setInterval(async()=>{try{const r=await api('/jobs/'+result.data.job_id);status.textContent=({pending:'Ожидает запуска',running:'Обновляем данные',succeeded:'Обновление завершено',failed:'Источник недоступен',cancelled:'Задание отменено'})[r.data.status]||r.data.status;
        if(!['pending','running'].includes(r.data.status))clearInterval(timer);}catch{clearInterval(timer);}},1500);
    }
    if(container.querySelector('[data-sync]'))container.querySelector('[data-sync]').onclick=()=>job('/integration/sync').catch(e=>status.textContent=e.message);
    if(container.querySelector('[data-collect]'))container.querySelector('[data-collect]').onclick=()=>job('/catalysts/collect').catch(e=>status.textContent=e.message);
    container.querySelector('[data-channel-form]').onsubmit=async event=>{
      event.preventDefault();const fd=new FormData(event.target);
      try{await send('/channels',{handle:fd.get('handle'),enabled:fd.has('enabled')});await sources(container);}
      catch(error){status.textContent=error.message;}
    };
    container.querySelectorAll('[data-channel]').forEach(b=>b.onclick=()=>job('/channels/'+encodeURIComponent(b.dataset.channel)+'/collect').catch(e=>status.textContent=e.message));
    container.querySelectorAll('[data-retry]').forEach(b=>b.onclick=()=>job('/integration/quarantine/'+b.dataset.retry+'/retry').catch(e=>status.textContent=e.message));
    container.querySelector('[data-subscribe]').onclick=async()=>{try{await send('/subscriptions',{include_global:true,offsets_minutes:[1440],asset_uids:[]});status.textContent='Подписка сохранена. Фактическая доставка зависит от включения напоминаний.';}catch(e){status.textContent=e.message;}};
  }
  async function page(tab){
    const container=document.getElementById('research-page');if(!container)return;
    try{await HTF.ensureToken();if(['ideas','strength','baskets','playbook','lab'].includes(tab)&&window.ResearchWorkbench)await ResearchWorkbench.mount(container,tab);else if(tab==='identity')await identity(container);else if(tab==='sources')await sources(container);else await mountCalendar(container,true);}
    catch(e){errorBox(container,e);}
  }
  document.querySelectorAll('[data-research-tab]').forEach(b=>b.onclick=()=>{const url=new URL(location.href);url.searchParams.set('tab',b.dataset.researchTab);history.pushState(null,'',url);page(b.dataset.researchTab);});
  window.addEventListener('popstate',()=>{if(document.getElementById('research-page'))page(new URLSearchParams(location.search).get('tab'));});
  document.addEventListener('research-instrument',event=>refreshContext(event.detail.instrument_id));
  async function overview(){
    try{
      if(!(await config()).enabled)return;
      if(Date.now()-overviewAt>60000){
        overviewPending=overviewPending||api('/overview').then(r=>{overviewData=r.data.items;overviewAt=Date.now();}).finally(()=>overviewPending=null);
        await overviewPending;
      }
      document.querySelectorAll('#now-tbody tr[data-iid]').forEach(row=>{
        const item=overviewData.find(i=>i.instrument_id===Number(row.dataset.iid));
        row.querySelector('.research-overview-note')?.remove();
        if(item?.material_count){
          const note=document.createElement('div');note.className='research-overview-note research-muted';
          note.textContent='Новых материалов за сутки: '+item.material_count;
          row.querySelector('td')?.appendChild(note);
        }
      });
    }catch{/* The research indicator never replaces technical overview errors. */}
  }
  document.addEventListener('research-overview',overview);
  if(document.getElementById('now-tbody'))overview();
  if(document.getElementById('research-page'))page(new URLSearchParams(location.search).get('tab'));
  if(document.getElementById('research-asset-context')){
    refreshContext(new URLSearchParams(location.search).get('instrument')||document.getElementById('instrument-select')?.value);
    setInterval(()=>{if(!document.hidden&&selectedInstrument)refreshContext(selectedInstrument);},300000);
  }
  function leaveCalendar(){calendarSeq++;calendarState=null;}
  return {mountCalendar,leaveCalendar,refreshContext,detail,material,edit};
})();
