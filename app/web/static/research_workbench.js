/* Versioned research workflows inside the existing Market Researcher shell. */
'use strict';
window.ResearchWorkbench=(()=>{
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api=(p,o={})=>HTF.api('/api/research/v1'+p,o);
  const send=(p,body,method='POST')=>api(p,{method,headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify(body)});
  const date=v=>v?new Date(v).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',dateStyle:'short',timeStyle:'short'})+' МСК':'—';
  const num=(v,suffix='')=>v==null?'Нет данных':Number(v).toLocaleString('ru-RU',{maximumFractionDigits:2})+suffix;
  const statuses={draft:'Черновик',watching:'Наблюдение',supported:'Поддерживается фактами',disputed:'Есть противоречия',resolved:'Исследование завершено',invalidated:'Опровергнуто',archived:'Архив',observing:'Наблюдение',paused:'Пауза',closed:'Закрыта',pending_baseline:'Ожидаем общие цены',active:'Активна',baseline_failed:'Не удалось зафиксировать базу',shadow:'Теневой поиск',retired:'Выключено',ready:'Готово к запуску',running:'Выполняется',succeeded:'Готово',incomplete:'Неполные данные',failed:'Ошибка',cancelled:'Отменено'};
  let currentTab=null,container=null,sequence=0;
  const id=v=>encodeURIComponent(v);
  function dialog(title,html){const d=document.createElement('dialog');d.className='research-dialog';d.innerHTML='<h2>'+esc(title)+'</h2>'+html+'<div class="research-toolbar"><button class="btn" data-close>Закрыть</button></div>';d.querySelector('[data-close]').onclick=()=>d.close();d.onclose=()=>d.remove();document.body.append(d);d.showModal();return d;}
  function fail(e){return dialog('Не удалось выполнить действие','<p class="research-error">'+esc(e.message)+'</p>');}
  function button(label,action,extra=''){return '<button type="button" class="btn small" data-wb="'+action+'" '+extra+'>'+esc(label)+'</button>';}
  function table(headers,rows){return '<div class="research-table-wrap"><table class="research-table"><thead><tr>'+headers.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(value=>'<td>'+value+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';}
  function sourceLink(url){return /^https?:\/\//i.test(url||'')?'<a target="_blank" rel="noopener noreferrer" href="'+esc(url)+'">Оригинальная публикация ↗</a>':'';}
  function externalIdea(value){
    const origin=value.external_origin;if(!origin)return '';
    const snapshot=origin.snapshot||{};
    return '<details><summary>Оригинал из STVTerminal</summary><p class="research-muted">Внешний архив. Локально получено '+esc(date(value.created_at))+'.</p>'+table(['Создано в терминале','Внешняя база','Дата внешней базы','Источник цены'],[[esc(date(origin.created_at)),num(origin.baseline?.price),esc(date(origin.baseline?.observed_at)),esc(origin.baseline?.source||'—')]])+sourceLink(snapshot.sourceUrl)+'<blockquote>'+esc(snapshot.body||'')+'</blockquote><p>Катализаторы: '+esc((snapshot.catalysts||[]).join('; ')||'Не указаны')+'</p><p>Риски: '+esc((snapshot.risks||[]).join('; ')||'Не указаны')+'</p></details>';
  }
  function chart(rows,key,maxGap=5400000){
    const complete=rows.filter(r=>Number.isFinite(r[key]));if(!complete.length)return '<p class="research-muted">Полных наблюдений для графика пока нет.</p>';
    const from=Math.min(...complete.map(r=>r.at??r.observed_at)),to=Math.max(...complete.map(r=>r.at??r.observed_at));const low=Math.min(...complete.map(r=>r[key])),high=Math.max(...complete.map(r=>r[key]));
    const x=t=>50+(t-from)/Math.max(1,to-from)*550,y=v=>170-(v-low)/Math.max(.01,high-low)*140;let paths='',path='',last=null;
    for(const row of rows){const at=row.at??row.observed_at;if(row[key]==null||row.quality==='stale'||last!=null&&at-last>maxGap){if(path)paths+='<path d="'+path+'"/>';path='';}if(row[key]!=null){path+=(path?' L':'M')+x(at).toFixed(2)+' '+y(row[key]).toFixed(2);last=at;}}
    if(path)paths+='<path d="'+path+'"/>';
    return '<svg class="research-line-chart" viewBox="0 0 640 205" role="img" aria-label="Сохранённая динамика"><g fill="none" stroke="currentColor" stroke-width="2">'+paths+'</g><text x="4" y="25">'+num(high)+'</text><text x="4" y="175">'+num(low)+'</text><text x="50" y="200">'+esc(new Date(from).toLocaleDateString('ru-RU'))+'</text><text x="500" y="200">'+esc(new Date(to).toLocaleDateString('ru-RU'))+'</text></svg>';
  }
  function form(title,fields,onSave){
    const d=dialog(title,'<form class="research-form">'+fields.map(f=>{
      let input='';const attrs=' name="'+f.name+'" '+(f.required?'required ':'')+(f.min!=null?'min="'+f.min+'" ':'')+(f.step?'step="'+f.step+'" ':'');
      if(f.type==='select')input='<select'+attrs+'>'+f.options.map(o=>'<option value="'+esc(o[0])+'"'+(String(o[0])===String(f.value??'')?' selected':'')+'>'+esc(o[1])+'</option>').join('')+'</select>';
      else if(f.type==='textarea')input='<textarea'+attrs+' rows="'+(f.rows||4)+'">'+esc(f.value||'')+'</textarea>';
      else input='<input type="'+(f.type||'text')+'"'+attrs+' value="'+esc(f.value??'')+'"'+(f.type==='checkbox'&&f.value?' checked':'')+'>';
      return '<label class="'+(f.wide?'wide':'')+'">'+esc(f.label)+input+(f.note?'<span class="research-muted">'+esc(f.note)+'</span>':'')+'</label>';
    }).join('')+'<p data-error role="alert" class="research-error wide"></p><button type="submit" class="btn primary">Сохранить</button></form>');
    d.querySelector('form').onsubmit=async event=>{event.preventDefault();const b=event.target.querySelector('[type=submit]');b.disabled=true;try{await onSave(new FormData(event.target));d.close();if(container?.isConnected)await mount(container,currentTab);}catch(e){d.querySelector('[data-error]').textContent=e.message;}finally{b.disabled=false;}};
    return d;
  }
  async function options(){const [a,i]=await Promise.all([api('/assets?limit=100'),HTF.api('/api/instruments')]);return {assets:a.data.items,instruments:i.filter(v=>v.market_type==='spot'&&v.quote_asset==='USDT')};}
  function lines(value){return String(value||'').split('\n').map(s=>s.trim()).filter(Boolean);}
  function millis(value){return value?new Date(value).getTime():null;}
  function localInput(value){if(!value)return '';const d=new Date(value);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);}
  async function waitJob(jobId,node,onDone){
    let stopped=false;const token=sequence;
    for(let attempt=0;attempt<240&&!stopped;attempt++){
      const job=(await api('/jobs/'+id(jobId))).data;
      if(node?.isConnected)node.textContent=(statuses[job.status]||job.status)+(job.progress?' · '+job.progress+'%':'');
      if(job.status==='succeeded'){await onDone?.(job.result);return job.result;}
      if(['failed','cancelled'].includes(job.status))throw new Error(job.error_code||statuses[job.status]);
      if(token!==sequence||!node?.isConnected)stopped=true;
      if(!stopped)await new Promise(resolve=>setTimeout(resolve,1000));
    }
  }
  function header(title,description,actions=''){return '<section class="research-block"><h2>'+esc(title)+'</h2><p>'+esc(description)+'</p><div class="research-toolbar">'+actions+'</div><p data-job class="research-status" aria-live="polite"></p><div data-content></div></section>';}
  async function mount(root,tab){
    container=root;currentTab=tab;const seq=++sequence;
    root.innerHTML='<p class="research-muted">Загружаем исследование…</p>';
    try{
      const cfg=(await api('/workbench/config')).data;if(seq!==sequence)return;
      const flag=tab==='ideas'?'ideas':tab==='strength'?'strength':tab==='baskets'?'baskets':tab==='playbook'?'playbook':'lab';
      if(!cfg.enabled||!cfg.features[flag]){root.innerHTML='<section class="research-block"><h2>Раздел выключен</h2><p>Включите этот сервис в серверных настройках Market Researcher.</p></section>';return;}
      if(tab==='ideas')await renderIdeas(root,seq);else if(tab==='strength')await renderStrength(root,seq);else if(tab==='baskets')await renderBaskets(root,seq);else if(tab==='playbook')await renderPlaybook(root,seq,cfg);else await renderLab(root,seq);
    }catch(e){if(seq===sequence)root.innerHTML='<p class="research-error">'+esc(e.message)+'</p>';}
  }
  async function ideaForm(value={},prefill={}){
    const opts=await options();const all={...value,...prefill};let technical=[];
    if(all.instrument_id)technical=(await api('/instruments/'+all.instrument_id+'/research-links')).data.htf_ideas.map(i=>['htf_idea:'+i.id,'HTF-идея №'+i.id]);
    return form(value.id?'Новая версия исследовательской идеи':'Исследовательская идея',[
      {name:'title',label:'Заголовок',value:all.title,required:true,wide:true},
      {name:'asset',label:'Актив',type:'select',options:[['','Выбрать позже'],...opts.assets.map(a=>[a.asset_uid,a.display_symbol+' · '+a.name])],value:all.primary_asset_uid},
      {name:'instrument',label:'Расчётный рынок',type:'select',options:[['','Без ценового наблюдения'],...opts.instruments.map(i=>[i.id,i.symbol+' · '+i.venue])],value:all.instrument_id},
      {name:'thesis',label:'Гипотеза',type:'textarea',value:all.thesis,wide:true},
      {name:'mechanism',label:'Почему это может повлиять на актив',type:'textarea',value:all.mechanism,wide:true},
      {name:'confirmation',label:'Что подтвердит тезис — по одному условию в строке',type:'textarea',value:(all.criteria||[]).filter(c=>c.type==='confirmation').map(c=>c.description).join('\n')},
      {name:'invalidation',label:'Что опровергнет тезис — по одному условию в строке',type:'textarea',value:(all.criteria||[]).filter(c=>c.type==='invalidation').map(c=>c.description).join('\n')},
      {name:'direction',label:'Исследовательское направление',type:'select',options:[['watch','Наблюдать'],['positive','Положительное'],['negative','Отрицательное'],['neutral','Нейтральное']],value:all.direction||'watch'},
      {name:'due',label:'Следующий пересмотр — местное время браузера',type:'datetime-local',value:localInput(all.review_due_at)},
      {name:'review_condition',label:'Либо условие пересмотра',value:all.review_condition},
      ...(technical.length?[{name:'technical',label:'Приложить техническое основание',type:'select',options:[['','Не добавлять'],...technical]}]:[]),
      ...(!value.id?[{name:'status',label:'Сохранить как',type:'select',options:[['draft','Черновик'],['watching','Наблюдение']],value:'draft'}]:[]),
      {name:'reason',label:'Пояснение изменения',value:value.id?'':'Создана исследовательская гипотеза',required:true,wide:true}
    ],async fd=>{
      const criteria=[...lines(fd.get('confirmation')).map(description=>({type:'confirmation',description,evaluator:'manual'})),...lines(fd.get('invalidation')).map(description=>({type:'invalidation',description,evaluator:'manual'}))];
      const linked=fd.get('technical');const refs=[...(all.technical_refs||[])];if(linked){const [kind,identifier]=linked.split(':');refs.push({kind,id:Number(identifier)});}
      await send('/ideas'+(value.id?'/'+id(value.id):''),{...all,title:fd.get('title'),primary_asset_uid:fd.get('asset')||null,instrument_id:fd.get('instrument')?Number(fd.get('instrument')):null,
        thesis:fd.get('thesis'),mechanism:fd.get('mechanism'),criteria,direction:fd.get('direction'),review_due_at:millis(fd.get('due')),review_condition:fd.get('review_condition'),
        status:value.status||fd.get('status'),source_refs:all.source_refs||[],technical_refs:refs,expected_revision:value.revision_id,reason:fd.get('reason')},value.id?'PATCH':'POST');
    });
  }
  async function renderIdeas(root,seq){
    const [response,opts]=await Promise.all([api('/ideas?limit=50'),options()]);if(seq!==sequence)return;
    root.innerHTML=header('Исследовательские идеи','Сохраняйте тезис, основания и условия пересмотра. Изменения создают новую версию.',button('Создать идею','new-idea'));
    const body=root.querySelector('[data-content]');
    body.innerHTML='<form class="research-toolbar" data-idea-filter><label>Поиск<input name="q"></label><label>Состояние<select name="status"><option value="">Все</option><option value="active">Действующие</option>'+Object.entries(statuses).filter(([k])=>['draft','resolved','invalidated','archived'].includes(k)).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join('')+'</select></label><button class="btn">Показать</button></form><p data-count></p><div data-idea-list></div>';
    let filters='',cursor=null;
    function show(data,append=false){
      cursor=data.next_cursor;body.querySelector('[data-count]').textContent='Найдено: '+data.total;
      const list=body.querySelector('[data-idea-list]');if(!append)list.innerHTML='';
      list.insertAdjacentHTML('beforeend',data.items.map(v=>'<article class="research-card"><h3>'+esc(v.title)+'</h3><p>'+esc(opts.assets.find(a=>a.asset_uid===v.primary_asset_uid)?.display_symbol||'Актив не выбран')+' · '+esc(statuses[v.status])+'</p><p>'+esc(v.thesis)+'</p><p class="research-muted">Пересмотр: '+esc(date(v.review_due_at))+'</p>'+button('Открыть','idea','data-id="'+v.id+'"')+'</article>').join('')||(!append?'<p>Идей пока нет.</p>':''));
      body.querySelector('[data-more]')?.remove();if(cursor)list.insertAdjacentHTML('afterend','<button class="btn" data-more>Ещё</button>');
      list.querySelectorAll('[data-wb="idea"]').forEach(b=>b.onclick=()=>ideaDetail(b.dataset.id).catch(fail));
      body.querySelector('[data-more]')?.addEventListener('click',async()=>show((await api('/ideas?limit=50'+filters+'&cursor='+id(cursor))).data,true));
    }
    show(response.data);root.querySelector('[data-wb="new-idea"]').onclick=()=>ideaForm().catch(fail);
    body.querySelector('form').onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);filters='&'+new URLSearchParams(fd).toString();show((await api('/ideas?limit=50'+filters)).data);};
  }
  async function ideaDetail(identifier){
    const [r,o,h,n]=await Promise.all([api('/ideas/'+id(identifier)),api('/ideas/'+id(identifier)+'/outcomes'),api('/ideas/'+id(identifier)+'/history'),api('/ideas/'+id(identifier)+'/reviews')]);const v=r.data;
    const d=dialog(v.title,'<p>'+esc(statuses[v.status])+'</p><h3>Гипотеза</h3><p>'+esc(v.thesis)+'</p><h3>Механизм</h3><p>'+esc(v.mechanism)+'</p><h3>Критерии</h3><ul>'+v.criteria.map(c=>'<li>'+esc((c.type==='invalidation'?'Опровергнет: ':'Подтвердит: ')+c.description)+'</li>').join('')+'</ul>'+
      '<div class="research-toolbar">'+button('Редактировать','edit')+button('Пересмотреть','review')+button('Изменить состояние','transition')+button('В учебник','example')+'</div>'+
      '<h3>Источники решения</h3>'+v.source_refs.map((ref,index)=>button(({material:'Материал',catalyst:'Катализатор',idea:'Идея'})[ref.kind]||'Источник','source','data-index="'+index+'"')).join('')+externalIdea(v)+
      '<h3>Задачи пересмотра</h3>'+n.data.items.filter(x=>x.note_kind==='review_trigger').map(x=>'<p>'+esc(x.type==='due'?'Наступил срок пересмотра':x.type==='data_unavailable'?'Технические данные недоступны':'Связанное свидетельство изменилось')+'</p>').join('')+
      '<h3>Описательный результат цены</h3><p class="research-muted">Динамика цены и вывод по гипотезе оцениваются отдельно.</p>'+o.data.items.map(e=>'<p>Старт '+esc(date(e.started_at))+' · '+esc(e.baseline?'База '+num(e.baseline.price):e.quality==='pending'?'Ждём котировку':'База недоступна')+'</p>'+table(['Горизонт','Цена','Доходность','К BTC','Данные'],e.outcomes.map(x=>[x.days+' дн',num(x.price),num(x.return_pct,'%'),num(x.excess_return_pct,' п.п.'),esc(x.quality)]))).join('')+
      '<details><summary>История версий</summary>'+h.data.items.map(rev=>'<article class="research-card"><p>'+esc(date(rev.available_at))+' · '+esc(rev.reason)+'</p><p>'+esc(rev.thesis)+'</p></article>').join('')+'</details>');
    d.querySelector('[data-wb="edit"]').onclick=()=>{d.close();ideaForm(v).catch(fail);};
    d.querySelector('[data-wb="example"]').onclick=()=>exampleForm({kind:'idea',id:v.id,revision_id:v.revision_id}).catch(fail);
    d.querySelector('[data-wb="review"]').onclick=()=>form('Пересмотр исследовательской идеи',[{name:'verdict',label:'Что изменилось',type:'select',options:[['supports','Поддерживает тезис'],['weakens','Ослабляет тезис'],['refutes','Опровергает'],['inconclusive','Недостаточно данных']]},{name:'explanation',label:'Объяснение',type:'textarea',required:true,wide:true}],async fd=>{await send('/ideas/'+v.id+'/reviews',{expected_revision:v.revision_id,verdict:fd.get('verdict'),explanation:fd.get('explanation')});d.close();});
    d.querySelector('[data-wb="transition"]').onclick=()=>form('Изменить состояние',[{name:'status',label:'Новое состояние',type:'select',options:Object.entries(statuses).filter(([k])=>['draft','watching','supported','disputed','resolved','invalidated','archived'].includes(k))},{name:'reason',label:'Причина и заключение',type:'textarea',required:true,wide:true},{name:'manual',label:'Это моё ручное решение по тезису',type:'checkbox'}],async fd=>{await send('/ideas/'+v.id+'/transition',{expected_revision:v.revision_id,status:fd.get('status'),reason:fd.get('reason'),manual_decision:fd.has('manual')});d.close();});
    d.querySelectorAll('[data-wb="source"]').forEach(b=>b.onclick=()=>{const ref=v.source_refs[Number(b.dataset.index)],source=v.source_snapshots.find(s=>s.ref.kind===ref.kind&&s.ref.id===ref.id&&s.ref.revision_id===ref.revision_id)?.snapshot;if(!source)return fail(new Error('Сохранённый снимок источника недоступен'));dialog(source.title||'Источник решения','<p class="research-muted">Зафиксированная версия '+esc(ref.revision_id)+' · Доступна '+esc(date(source.available_at))+'</p>'+sourceLink(source.canonical_url)+'<blockquote>'+esc(source.original_text||source.thesis||source.summary||'')+'</blockquote><p>'+esc(source.summary||'')+'</p>');});return d;
  }
  async function renderStrength(root,seq){
    const response=await api('/strength?limit=100');if(seq!==sequence)return;
    root.innerHTML=header('Относительная сила','Закрытые дни UTC. «К BTC» — разница доходностей в процентных пунктах. Отсутствующие значения не заменяются нулём.',button('Пересчитать сохранённую историю','calculate-strength')+button('Матрица корреляций','correlations'));
    const body=root.querySelector('[data-content]');body.innerHTML='<form class="research-toolbar"><label>Сортировать<select name="metric"><option value="excess_30">К BTC · 30 дней</option><option value="excess_7">К BTC · 7 дней</option><option value="excess_90">К BTC · 90 дней</option><option value="correlation_60">Корреляция · 60 дней</option><option value="volatility_20">Волатильность · 20 дней</option></select></label><label>Направление<select name="sort"><option value="desc">По убыванию</option><option value="asc">По возрастанию</option></select></label><label>К BTC 30 дней от, п.п.<input name="minimum" type="number" step="any"></label><button class="btn">Показать</button></form><div data-table></div>';
    let query='';
    const show=data=>{
      body.querySelector('[data-table]').innerHTML='<p class="research-muted">Срез: '+esc(date(data.as_of))+' · Найдено: '+(data.total||0)+'</p>'+table(['Рынок','Доходность 7д','К BTC 7д','К BTC 30д','К BTC 90д','Корреляция 60д','Волатильность 20д','Методика'],data.items.map(row=>[
        esc(row.symbol+' · '+row.venue),num(row.metrics.return_7.value,'%'),num(row.metrics.excess_7.value,' п.п.'),num(row.metrics.excess_30.value,' п.п.'),num(row.metrics.excess_90.value,' п.п.'),num(row.metrics.correlation_60.value),num(row.metrics.volatility_20.value,'%'),button('Периоды и качество','metric-detail','data-id="'+esc(row.instrument_id)+'"')]))+
        (!data.items.length?'<p>Расчёт ещё не выполнен либо нет сопоставимой истории.</p>':'')+(data.next_cursor?button('Следующая страница','strength-more'):'');
      body.querySelectorAll('[data-wb="metric-detail"]').forEach(b=>b.onclick=()=>{const row=data.items.find(r=>String(r.instrument_id)===b.dataset.id);dialog('Метрики · '+row.symbol,table(['Показатель','Значение','Начало','Конец','Качество'],Object.entries(row.metrics).map(([name,m])=>[esc(name),num(m.value),esc(date(m.from_at)),esc(date(m.to_at)),esc(m.reason_codes.join(', ')||'Данные полные')]))+'<p>Excess = доходность актива − доходность BTC. RS ratio = отношение нормированных цен − 1. Корреляция и beta используют 60 общих дневных интервалов.</p>');});
      body.querySelector('[data-wb="strength-more"]')?.addEventListener('click',async()=>show((await api('/strength?limit=100'+query+'&cursor='+id(data.next_cursor))).data));
    };show(response.data);
    body.querySelector('form').onsubmit=async e=>{e.preventDefault();const fd=new FormData(e.target);query='&metric='+id(fd.get('metric'))+'&sort='+fd.get('sort')+(fd.get('minimum')!==''?'&window=30&excess_min='+id(fd.get('minimum')):'');try{show((await api('/strength?limit=100'+query)).data);}catch(error){fail(error);}};
    root.querySelector('[data-wb="calculate-strength"]').onclick=async()=>{try{const j=await send('/strength/recalculate',{});await waitJob(j.data.job_id,root.querySelector('[data-job]'),()=>mount(root,'strength'));}catch(e){fail(e);}};
    root.querySelector('[data-wb="correlations"]').onclick=async()=>{
      const opts=await options();const d=form('Корреляция выбранных рынков',[{name:'note',label:'Окно',value:'60 общих дневных интервалов UTC'}],async fd=>{
        const selected=[...d.querySelectorAll('[data-correlation-id]:checked')].map(e=>Number(e.value));if(!selected.length)throw new Error('Выберите рынки');
        const job=await send('/correlations',{instrument_ids:selected});await waitJob(job.data.job_id,root.querySelector('[data-job]'),async out=>{const data=(await api('/correlations/'+out.snapshot_id)).data;dialog('Матрица корреляций',table(['Рынок A','Рынок B','Корреляция','Интервалы','Качество'],data.matrix.map(v=>[esc(opts.instruments.find(i=>i.id===v.left)?.symbol),esc(opts.instruments.find(i=>i.id===v.right)?.symbol),num(v.value),v.sample_n,esc(v.reason_codes.join(', ')||'Полные данные')])));});
      });d.querySelector('form').insertAdjacentHTML('afterbegin','<fieldset class="wide"><legend>Рынки — не более 30</legend>'+opts.instruments.map(i=>'<label><input type="checkbox" data-correlation-id value="'+i.id+'">'+esc(i.symbol+' · '+i.venue)+'</label>').join('')+'</fieldset>');
    };
  }
  async function basketForm(value={}){
    const opts=await options();let d;
    d=form(value.id?'Новая версия состава корзины':'Модельная корзина',[
      {name:'title',label:'Название',required:true,value:value.title,wide:true},{name:'thesis',label:'Гипотеза корзины',type:'textarea',value:value.thesis,wide:true},
      {name:'benchmark',label:'Сравнение',type:'select',options:[['','BTC spot USDT по политике выбора'],...opts.instruments.map(i=>[i.id,i.symbol+' · '+i.venue])],value:value.benchmark_instrument_id},
      {name:'reason',label:'Основание состава',required:true,value:value.id?'':'Сохранён исследовательский состав'}
    ],async fd=>{
      const members=[...d.querySelectorAll('[data-member]')].map(row=>({asset_uid:row.querySelector('[name=asset]').value,instrument_id:Number(row.querySelector('[name=market]').value),weight_pct:row.querySelector('[name=weight]').value.replace(',','.'),sector:row.querySelector('[name=sector]').value})).filter(m=>m.asset_uid);
      await send('/baskets'+(value.id?'/'+value.id:''),{...value,title:fd.get('title'),thesis:fd.get('thesis'),members,benchmark_instrument_id:fd.get('benchmark')?Number(fd.get('benchmark')):null,reason:fd.get('reason'),expected_revision:value.revision_id},value.id?'PATCH':'POST');
    });
    const grid=document.createElement('fieldset');grid.className='wide';grid.innerHTML='<legend>Состав и стартовые веса, %</legend><div data-members></div>'+button('Добавить актив','member-add')+'<p class="research-muted">Свободная доля — условный USDT без процентов. Веса не перераспределяются при отсутствии цены.</p>';
    d.querySelector('form').insertBefore(grid,d.querySelector('[data-error]'));
    function add(m={}){const row=document.createElement('div');row.className='research-member';row.dataset.member='';row.innerHTML='<label>Актив<select name="asset"><option value="">Выбрать</option>'+opts.assets.map(a=>'<option value="'+a.asset_uid+'"'+(m.asset_uid===a.asset_uid?' selected':'')+'>'+esc(a.display_symbol+' · '+a.name)+'</option>').join('')+'</select></label><label>Рынок<select name="market">'+opts.instruments.map(i=>'<option value="'+i.id+'"'+(m.instrument_id===i.id?' selected':'')+'>'+esc(i.symbol+' · '+i.venue)+'</option>').join('')+'</select></label><label>Вес, %<input name="weight" type="number" min="0.01" step="0.01" value="'+esc(m.weight_pct||'')+'"></label><label>Сектор<input name="sector" value="'+esc(m.sector||'')+'"></label>'+button('Убрать','remove-member');grid.querySelector('[data-members]').append(row);row.querySelector('button').onclick=()=>row.remove();}
    (value.members?.length?value.members:[{}]).forEach(add);grid.querySelector('[data-wb="member-add"]').onclick=()=>add();return d;
  }
  async function renderBaskets(root,seq){
    const data=(await api('/baskets?limit=50')).data;if(seq!==sequence)return;
    root.innerHTML=header('Исследовательские корзины','Модельные индексы с фиксированной базой и отдельной историей каждого состава.',button('Создать корзину','new-basket'));
    root.querySelector('[data-content]').innerHTML=data.items.map(v=>'<article class="research-card"><h3>'+esc(v.title)+'</h3><p>'+esc(v.thesis)+'</p><p>'+esc(statuses[v.status])+' · '+(v.external_origin?.requires_market_resolution?'Внешний архив: '+v.external_origin.state.assets.length+' активов; выберите локальный состав':v.members.length+' активов · Cash '+num(v.cash_pct,'%'))+'</p>'+button('Состав и наблюдения','basket','data-id="'+v.id+'"')+'</article>').join('')||'<p>Корзин пока нет.</p>';
    root.querySelector('[data-wb="new-basket"]').onclick=()=>basketForm().catch(fail);root.querySelectorAll('[data-wb="basket"]').forEach(b=>b.onclick=()=>basketDetail(b.dataset.id).catch(fail));
  }
  async function basketDetail(identifier){
    const [a,b,c,opts]=await Promise.all([api('/baskets/'+identifier),api('/baskets/'+identifier+'/segments'),api('/baskets/'+identifier+'/history'),options()]);const v=a.data;
    const d=dialog(v.title,'<p>'+esc(v.thesis)+'</p>'+(v.external_origin?'<h3>Архив STVTerminal</h3><p>Исходный состав сохранён как внешняя реконструкция. Локальное наблюдение начинается после сопоставления рынков.</p>'+table(['Исходный актив','Вес','База в архиве','Цена в архиве'],v.external_origin.state.assets.map(a=>[esc(a.ticker),num(a.weight,'%'),num(a.base),num(a.price)])):'')+'<p>'+esc(statuses[v.status])+' · Cash '+num(v.cash_pct,'%')+'</p>'+table(['Актив','Стартовый вес','Рынок'],v.members.map(m=>[esc(opts.assets.find(a=>a.asset_uid===m.asset_uid)?.display_symbol||m.asset_uid),num(m.weight_pct,'%'),esc(opts.instruments.find(i=>i.id===m.instrument_id)?.symbol||'Рынок недоступен')]))+
      '<div class="research-toolbar">'+button('Изменить состав','edit-basket')+button('Начать новый сегмент','start-basket')+button('Состояние','basket-state')+button('Обновить наблюдение','observe-basket')+'</div><p data-job></p><h3>Сегменты наблюдения</h3><div data-segments>'+b.data.items.map(s=>'<article class="research-card"><p>'+esc(statuses[s.status]||s.status)+' · Старт '+esc(date(s.started_at))+'</p>'+button('Показать наблюдения','segment','data-id="'+s.id+'"')+'</article>').join('')+'</div><div data-observations></div><details><summary>Версии состава</summary>'+c.data.items.map(h=>'<p>'+esc(date(h.available_at)+' · '+h.reason)+'</p>').join('')+'</details>');
    d.querySelector('[data-wb="edit-basket"]').onclick=()=>{d.close();basketForm(v).catch(fail);};
    d.querySelector('[data-wb="start-basket"]').onclick=async()=>{try{await send('/baskets/'+identifier+'/start',{expected_revision:v.revision_id,reason:'Начат новый сегмент наблюдения'});d.close();await mount(container,'baskets');}catch(e){fail(e);}};
    d.querySelector('[data-wb="basket-state"]').onclick=()=>form('Состояние корзины',[{name:'status',label:'Действие',type:'select',options:[['paused','Пауза'],['observing','Продолжить прежний сегмент'],['closed','Завершить'],['archived','В архив']]},{name:'reason',label:'Причина',required:true}],async fd=>{await send('/baskets/'+identifier+'/transition',{expected_revision:v.revision_id,status:fd.get('status'),reason:fd.get('reason')});d.close();});
    d.querySelector('[data-wb="observe-basket"]').onclick=async()=>{try{const job=await send('/baskets/'+identifier+'/observe',{});await waitJob(job.data.job_id,d.querySelector('[data-job]'),async()=>{d.close();await basketDetail(identifier);});}catch(e){fail(e);}};
    d.querySelectorAll('[data-wb="segment"]').forEach(b=>b.onclick=async()=>{const rows=(await api('/baskets/'+identifier+'/observations?segment_id='+b.dataset.id)).data;d.querySelector('[data-observations]').innerHTML=chart(rows.items,'index_value')+table(['Время','Индекс','Доходность','К BTC','Покрытие'],rows.items.map(o=>[esc(date(o.observed_at)),num(o.index_value),num(o.return_pct,'%'),num(o.excess_return_pct,' п.п.'),num(o.coverage_weight_pct,'%')]))+'<p class="research-muted">График отражает сохранённые наблюдения; паузы и неполные точки сохраняются.</p>';});return d;
  }
  async function exampleForm(ref){
    const sections=(await api('/playbook/sections?limit=100')).data.items;if(!sections.length)throw new Error('Сначала создайте рубрику в учебнике');
    return form('Добавить пример в учебник',[
      {name:'section',label:'Рубрика',type:'select',options:sections.map(s=>[s.id,s.title]),required:true},
      {name:'label',label:'Соответствие рубрике',type:'select',options:[['positive','Подходит'],['negative','Антипример — не подходит'],['partial','Частично'],['uncertain','Не уверен']]},
      {name:'direction',label:'Направление информации',type:'select',options:[['none','Не задано'],['positive','Положительное'],['negative','Отрицательное'],['neutral','Нейтральное'],['watch','Наблюдать']]},
      {name:'focus',label:'Главный признак',type:'select',options:[['whole','Весь материал'],['author','Автор'],['wording','Формулировка'],['event','Событие'],['numbers','Числа'],['chart','График'],['asset','Актив'],['mechanism','Механизм']]},
      {name:'explanation',label:'Почему подходит или не подходит',type:'textarea',required:true,wide:true,note:'Для примера и антипримера — не менее 20 символов. Исходник сохраняется отдельно от пояснения.'}
    ],fd=>send('/playbook/examples',{section_id:fd.get('section'),source_ref:ref,label:fd.get('label'),information_direction:fd.get('direction'),focus:fd.get('focus'),explanation:fd.get('explanation')}));
  }
  async function ruleForm(section,dataset,value={}){
    let d;d=form(value.id?'Новая версия правила':'Ручное правило рубрики',[
      {name:'text',label:'Когда материал относится к этой рубрике',type:'textarea',required:true,wide:true,value:value.text},
      {name:'positive',label:'Признаки — по одному в строке',type:'textarea',value:(value.positive_signs||[]).join('\n')},
      {name:'negative',label:'Антипризнаки',type:'textarea',value:(value.negative_signs||[]).join('\n')},
      {name:'method',label:'Способ сопоставления',type:'select',options:[['lexical','Текстовое сходство, без модели'],['model','Текстовый отбор и проверка моделью']],value:value.retrieval_method||'lexical'},
      {name:'threshold',label:'Порог текстового отбора, 0–1',type:'number',min:0,step:.01,value:value.retrieval_threshold??.3},
      {name:'independent',label:'Правило составлено только по train; отложенные примеры не использованы',type:'checkbox',value:value.independence_declaration},
      {name:'reason',label:'Основание изменения',required:true,value:value.id?'':'Сохранено ручное правило'}
    ],async fd=>{
      const conditions=[...d.querySelectorAll('[data-condition]')].map(row=>{const kind=row.querySelector('[name=kind]').value,raw=row.querySelector('[name=values]').value;return {kind,mode:row.querySelector('[name=mode]').value,origin:'manual',...(kind==='min_material_freshness'?{value:Number(raw)}:['source_in','author_in','catalyst_kind','catalyst_stage'].includes(kind)?{values:raw.split(',').map(s=>s.trim()).filter(Boolean)}:{value:true})};});
      if(!dataset)throw new Error('Сначала зафиксируйте выборку рубрики');
      await send('/playbook/rules'+(value.id?'/'+value.id:''),{section_id:section.id,text:fd.get('text'),positive_signs:lines(fd.get('positive')),negative_signs:lines(fd.get('negative')),
        retrieval_method:fd.get('method'),retrieval_threshold:Number(fd.get('threshold')),code_conditions:conditions,dataset_id:dataset.id,
        evidence_example_revision_ids:dataset.partitions.train,independence_declaration:fd.has('independent'),reason:fd.get('reason'),expected_revision:value.revision_id},value.id?'PATCH':'POST');
    });
    const field=document.createElement('fieldset');field.className='wide';field.innerHTML='<legend>Проверяемые условия</legend><div data-conditions></div>'+button('Добавить условие','add-condition');d.querySelector('form').insertBefore(field,d.querySelector('[data-error]'));
    function add(c={}){const row=document.createElement('div');row.className='research-member';row.dataset.condition='';const kinds=[['source_in','Тип источника'],['author_in','Автор'],['explicit_asset','Есть подтверждённый актив'],['asset_in_watchlist','Актив в наблюдении на дату источника'],['catalyst_kind','Тип катализатора'],['catalyst_stage','Стадия события'],['min_material_freshness','Свежесть, часы'],['has_verified_quote','Есть проверенная цитата']];row.innerHTML='<label>Условие<select name="kind">'+kinds.map(([k,v])=>'<option value="'+k+'"'+(c.kind===k?' selected':'')+'>'+esc(v)+'</option>').join('')+'</select></label><label>Применение<select name="mode"><option value="prefer"'+(c.mode==='prefer'?' selected':'')+'>Желательно</option><option value="require"'+(c.mode==='require'?' selected':'')+'>Обязательно</option><option value="off"'+(c.mode==='off'?' selected':'')+'>Выключено</option></select></label><label>Значения через запятую / часы<input name="values" value="'+esc(Array.isArray(c.values)?c.values.join(', '):c.values??'')+'"></label>'+button('Убрать','remove');field.querySelector('[data-conditions]').append(row);row.querySelector('button').onclick=()=>row.remove();}
    (value.code_conditions||[]).forEach(add);field.querySelector('[data-wb="add-condition"]').onclick=()=>add();return d;
  }
  async function renderPlaybook(root,seq,cfg){
    const data=(await api('/playbook/sections?limit=100')).data;if(seq!==sequence)return;
    root.innerHTML=header('Учебник исследователя','Размечайте примеры и антипримеры, сохраняйте правила и проверяйте находки. Направление новости не определяет метку примера.',button('Создать рубрику','new-section'));
    root.querySelector('[data-content]').innerHTML='<p class="research-muted">Модель: '+esc(cfg.model.provider)+' · '+esc(cfg.model.model||'не выбрана')+' · '+(cfg.model.configured?'ключ настроен':'ключ не доступен в этом окружении')+' · Бюджет $'+esc(cfg.model.daily_budget_usd)+'/24 часа · Учтено $'+esc(cfg.model.accounted_usd)+' · Остаток $'+esc(cfg.model.remaining_usd)+'</p>'+data.items.map(s=>'<article class="research-card"><h3>'+esc(s.title)+'</h3><p>'+esc(s.purpose)+'</p>'+button('Примеры и правила','section','data-id="'+s.id+'"')+'</article>').join('');
    root.querySelector('[data-wb="new-section"]').onclick=()=>form('Рубрика учебника',[{name:'title',label:'Название',required:true},{name:'purpose',label:'Что ищем и зачем',type:'textarea',wide:true},{name:'track',label:'Направление',type:'select',options:[['research','Исследования'],['catalyst','События'],['narrative','Нарративы'],['attention','Внимание'],['risk','Риски'],['trade_hypothesis','Торговые гипотезы']]}],fd=>send('/playbook/sections',{title:fd.get('title'),purpose:fd.get('purpose'),track:fd.get('track')}));
    root.querySelectorAll('[data-wb="section"]').forEach(b=>b.onclick=()=>sectionDetail(b.dataset.id).catch(fail));
  }
  async function sectionDetail(identifier){
    const [a,b,c,e]=await Promise.all([api('/playbook/sections/'+identifier),api('/playbook/examples?q='+identifier+'&limit=100'),api('/playbook/rules?q='+identifier+'&limit=100'),api('/playbook/sections/'+identifier+'/dataset')]);const section=a.data;let dataset=e.data;
    const d=dialog(section.title,'<p>'+esc(section.purpose)+'</p><div class="research-toolbar">'+button('Зафиксировать выборку','freeze')+button('Ручное правило','manual-rule')+button('Подготовить правило моделью','distill')+'</div><p data-job class="research-muted">'+(dataset?'Снимок создан. Train: '+dataset.partitions.train.length+', validation: '+dataset.partitions.validation.length+', holdout: '+dataset.partitions.holdout.length:'Снимок выборки ещё не создан')+'</p><h3>Примеры и антипримеры</h3>'+b.data.items.map(v=>'<article class="research-card"><p><b>'+esc({positive:'Пример',negative:'Антипример',partial:'Частично',uncertain:'Не уверен'}[v.label])+'</b> · '+esc(v.source_snapshot.author||'')+'</p><blockquote>'+esc(v.source_snapshot.text.slice(0,800))+'</blockquote><p>'+esc(v.explanation)+'</p></article>').join('')+'<h3>Правила и версии</h3>'+c.data.items.map(v=>'<article class="research-card"><p>'+esc(statuses[v.status]||v.status)+' · '+esc(v.retrieval_method==='lexical'?'Текстовый поиск':'Проверка моделью')+'</p><p>'+esc(v.text)+'</p>'+button('Правило, оценка и находки','rule','data-id="'+v.id+'"')+'</article>').join('')+'<p class="research-muted">Новый пример добавляется кнопкой «В учебник» из материала, события или исследовательской идеи.</p>');
    d.querySelector('[data-wb="freeze"]').onclick=async()=>{try{dataset=(await send('/playbook/datasets',{section_id:identifier})).data;d.querySelector('[data-job]').textContent='Зафиксировано: train '+dataset.partitions.train.length+', validation '+dataset.partitions.validation.length+', holdout '+dataset.partitions.holdout.length;}catch(error){fail(error);}};
    d.querySelector('[data-wb="manual-rule"]').onclick=()=>ruleForm(section,dataset).catch(fail);
    d.querySelector('[data-wb="distill"]').onclick=async()=>{try{if(!dataset)throw new Error('Сначала зафиксируйте выборку');const job=await send('/playbook/sections/'+identifier+'/distill',{dataset_id:dataset.id});await waitJob(job.data.job_id,d.querySelector('[data-job]'),async()=>{d.close();await sectionDetail(identifier);});}catch(error){fail(error);}};
    d.querySelectorAll('[data-wb="rule"]').forEach(b=>b.onclick=()=>ruleDetail(b.dataset.id).catch(fail));return d;
  }
  async function ruleDetail(identifier){
    const [r,m]=await Promise.all([api('/playbook/rules/'+identifier),api('/playbook/matches?q='+identifier+'&limit=100')]);const rule=r.data;const dataset=rule.dataset_id?(await api('/playbook/datasets/'+rule.dataset_id)).data:null;
    const d=dialog('Правило · '+rule.title,'<p>'+esc(rule.text)+'</p><p>'+esc(statuses[rule.status])+'</p><h3>Требования</h3><ul>'+rule.code_conditions.map(c=>'<li>'+esc(c.kind+' · '+c.mode+' · '+(Array.isArray(c.values)?c.values.join(', '):c.values??''))+'</li>').join('')+'</ul><div class="research-toolbar">'+button('Новая версия','edit-rule')+button('Проверить на holdout','evaluate')+button('Теневой поиск','shadow')+button('Выключить поиск','retire')+button('Проверить историю до 7 дней','backfill')+'</div><p data-job></p><div data-evaluation></div><h3>Найденное и ошибки проверки</h3>'+m.data.items.map(v=>'<article class="research-card"><p>'+esc(v.title)+'</p><p>'+esc({match:'Найден кандидат',no_match:'Не соответствует',abstain:'Недостаточно данных',error:'Ошибка проверки'}[v.decision])+' · '+esc(v.mode)+'</p><p>'+esc(v.rationale||v.error_code||'')+'</p>'+button('Оригинал','match-source','data-id="'+v.id+'"')+button('В идею','match-idea','data-id="'+v.id+'"')+(v.decision==='error'?button('Повторить проверку','retry-match','data-id="'+v.id+'"'):'')+button('Оценить находку','feedback','data-id="'+v.id+'"')+'</article>').join(''));
    d.querySelector('[data-wb="edit-rule"]').onclick=async()=>ruleForm((await api('/playbook/sections/'+rule.section_id)).data,dataset,rule).catch(fail);
    d.querySelector('[data-wb="evaluate"]').onclick=async()=>{try{if(!dataset)throw new Error('Нужен снимок выборки');const job=await send('/playbook/rules/'+identifier+'/evaluate',{dataset_id:dataset.id,partition:'holdout',retry_key:crypto.randomUUID()});await waitJob(job.data.job_id,d.querySelector('[data-job]'),async out=>{const report=(await api('/playbook/evaluations/'+out.evaluation_id)).data;d.querySelector('[data-evaluation]').innerHTML='<h3>Соответствие рубрике по разметке</h3><p>'+esc(report.independent&&report.sufficient_sample?'Независимая проверка':'Разработочная проверка; независимых примеров недостаточно')+'</p>'+table(['Precision','Recall','Coverage','Ошибки','Отказ от решения'],[[num(report.precision==null?null:report.precision*100,'%'),num(report.recall==null?null:report.recall*100,'%'),num(report.coverage==null?null:report.coverage*100,'%'),report.counts.failed,report.counts.abstained]])+table(['TP','FP','FN','TN','Положительных','Отрицательных'],[[report.counts.tp,report.counts.fp,report.counts.fn,report.counts.tn,report.counts.positive,report.counts.negative]])+(report.independent&&report.sufficient_sample?button('Активировать проверенную версию','activate-rule'):'');d.querySelector('[data-wb="activate-rule"]')?.addEventListener('click',async()=>{await send('/playbook/rules/'+identifier+'/transition',{status:'active',expected_revision:rule.revision_id,evaluation_id:report.id,reason:'Принята независимая оценка правила'});d.close();});});}catch(error){fail(error);}};
    for(const [selector,status] of [['shadow','shadow'],['retire','retired']])d.querySelector('[data-wb="'+selector+'"]').onclick=async()=>{try{await send('/playbook/rules/'+identifier+'/transition',{status,expected_revision:rule.revision_id,reason:status==='shadow'?'Начат теневой поиск с ручной проверкой':'Поиск выключен владельцем'});d.close();}catch(error){fail(error);}};
    d.querySelector('[data-wb="backfill"]').onclick=()=>form('Поиск в сохранённых материалах',[{name:'from',label:'С даты UTC',type:'date',required:true,value:new Date(Date.now()-7*86400000).toISOString().slice(0,10)},{name:'to',label:'До даты UTC, не включая',type:'date',required:true,value:new Date().toISOString().slice(0,10)}],async fd=>{const job=await send('/playbook/rules/'+identifier+'/backfill',{from_at:Date.parse(fd.get('from')+'T00:00:00Z'),to_at:Date.parse(fd.get('to')+'T00:00:00Z')});await waitJob(job.data.job_id,d.querySelector('[data-job]'));});
    d.querySelectorAll('[data-wb="match-source"]').forEach(b=>b.onclick=()=>{const match=m.data.items.find(v=>v.id===b.dataset.id);ResearchUI.material(match.source_ref.id,match.snapshot_as_of??match.evaluated_at,match.source_ref.revision_id).catch(fail);});
    d.querySelectorAll('[data-wb="match-idea"]').forEach(b=>b.onclick=()=>{const match=m.data.items.find(v=>v.id===b.dataset.id);ideaForm({},{title:match.title,source_refs:[match.source_ref]}).catch(fail);});
    d.querySelectorAll('[data-wb="retry-match"]').forEach(b=>b.onclick=async()=>{try{const job=await send('/playbook/matches/'+b.dataset.id+'/retry',{});await waitJob(job.data.job_id,d.querySelector('[data-job]'),async()=>{d.close();await ruleDetail(identifier);});}catch(error){fail(error);}});
    d.querySelectorAll('[data-wb="feedback"]').forEach(b=>b.onclick=()=>form('Оценка находки',[{name:'verdict',label:'Соответствие правилу',type:'select',options:[['correct','Верно'],['incorrect','Не соответствует'],['uncertain','Не уверен']]},{name:'explanation',label:'Объяснение',type:'textarea',required:true}],fd=>send('/playbook/matches/'+b.dataset.id+'/feedback',{verdict:fd.get('verdict'),explanation:fd.get('explanation')})));return d;
  }
  async function experimentForm(value={}){
    const opts=await options();let d;d=form(value.id?'Новая версия вопроса':'Исследовательский вопрос',[
      {name:'title',label:'Название',required:true,value:value.title,wide:true},{name:'question',label:'Что проверяем',type:'textarea',required:true,value:value.question,wide:true},
      {name:'mode',label:'Режим',type:'select',options:[['descriptive','Реакция цены после события'],['simulation','Симуляция формальной стратегии']],value:value.experiment_type||'descriptive'},
      {name:'from',label:'Начало периода UTC',type:'date',required:true,value:value.from_at?new Date(value.from_at).toISOString().slice(0,10):''},
      {name:'to',label:'Конец UTC, не включая',type:'date',required:true,value:value.to_at?new Date(value.to_at).toISOString().slice(0,10):''},
      {name:'kind',label:'Тип события',type:'select',options:[['','Все'],['launch','Запуск'],['upgrade','Обновление'],['unlock','Разлок'],['listing','Листинг'],['expiry','Экспирация'],['macro','Макро']],value:value.catalyst_kind||''},
      {name:'stage',label:'Стадия',type:'select',options:[['announced','Объявлено'],['live','Запущено'],['cancelled','Отменено'],['denied','Опровергнуто']],value:value.catalyst_stage||'announced'},
      {name:'technical',label:'Техническая группа',type:'select',options:[['none','Без технического условия'],['in_htf_zone','Сравнить внутри / вне HTF-зоны через replay']],value:value.technical_filter||'none'},
      {name:'overlap',label:'Перекрывающиеся события',type:'select',options:[['independent','Исключить из независимой группы'],['overlapping','Сохранить как зависимые наблюдения']],value:value.episode_policy||'independent'},
      {name:'reason',label:'Пояснение',required:true,value:value.id?'':'Сохранён исследовательский вопрос'}
    ],async fd=>{
      const instrument_ids=[...d.querySelectorAll('[data-universe]:checked')].map(e=>Number(e.value));
      await send('/lab/experiments'+(value.id?'/'+value.id:''),{...value,title:fd.get('title'),question:fd.get('question'),experiment_type:fd.get('mode'),
        from_at:Date.parse(fd.get('from')+'T00:00:00Z'),to_at:Date.parse(fd.get('to')+'T00:00:00Z'),instrument_ids,catalyst_kind:fd.get('kind')||null,catalyst_stage:fd.get('stage'),
        technical_filter:fd.get('technical'),episode_policy:fd.get('overlap'),data_mode:'observed_local',expected_revision:value.revision_id,reason:fd.get('reason')},value.id?'PATCH':'POST');
    });
    d.querySelector('form').insertAdjacentHTML('afterbegin','<fieldset class="wide"><legend>Фиксированный состав — spot USDT, до 20 рынков</legend>'+opts.instruments.map(i=>'<label><input type="checkbox" data-universe value="'+i.id+'"'+((value.instrument_ids||[]).includes(i.id)?' checked':'')+'>'+esc(i.symbol+' · '+i.venue)+'</label>').join('')+'</fieldset>');return d;
  }
  function strategyForm(value={}){
    const config=value.config||{},risk=config.risk||{},costs=config.costs||{},entry=config.entry?.rules?.[0]||{};
    return form(value.id?'Новая версия стратегии':'Формальная стратегия',[
      {name:'title',label:'Название',required:true,value:value.title,wide:true},{name:'entry',label:'Вход по закрытым D1',type:'select',options:[['relative_strength','Опережение BTC'],['breakout','Пробой максимума'],['dip_buy','Откат выше средней']],value:entry.template||'relative_strength'},
      {name:'lookback',label:'Окно сигнала, дней',type:'number',min:2,value:entry.params?.lookback_days||20},{name:'threshold',label:'Опережение BTC / откат, %',type:'number',step:.1,min:0,value:entry.params?.threshold_pct||15},
      {name:'capital',label:'Начальный капитал, USDT',type:'number',min:1,value:value.initial_capital||10000},{name:'position',label:'Максимальная доля позиции, %',type:'number',min:.01,step:.01,value:risk.max_position_pct||20},
      {name:'maxpositions',label:'Максимум позиций',type:'number',min:1,value:risk.max_positions||5},{name:'stop',label:'Стоп, %',type:'number',min:.1,step:.1,value:risk.stop_loss_pct||10},
      {name:'target',label:'Тейк-профит, %',type:'number',min:.1,step:.1,value:config.exit?.rules?.find(r=>r.template==='take_profit')?.params.take_profit_pct||20},{name:'hold',label:'Максимальное удержание, H1',type:'number',min:1,value:risk.time_stop||168},
      {name:'drawdown',label:'Остановка новых входов при просадке, %',type:'number',min:.1,step:.1,value:risk.max_drawdown_kill_pct||30},
      {name:'fee',label:'Комиссия на сторону, bps',type:'number',min:0,step:.1,value:costs.fee_bps??10},{name:'slippage',label:'Проскальзывание на сторону, bps',type:'number',min:0,step:.1,value:costs.slippage_bps??5},
      {name:'order',label:'Исполнение следующей H1',type:'select',options:[['market','Market по open'],['limit','Limit с trade-through']],value:value.order_type||'market'},
      {name:'offset',label:'Отступ лимитного входа, %',type:'number',min:0,step:.1,value:value.limit_offset_pct||0},
      {name:'idealized',label:'Явно допускаю идеализированные нулевые издержки',type:'checkbox',value:value.idealized_costs},
      {name:'reason',label:'Пояснение версии',required:true,value:value.id?'':'Сохранены формальные правила стратегии'}
    ],async fd=>{
      const number=n=>Number(fd.get(n)),template=fd.get('entry'),period=number('lookback');
      const params=template==='relative_strength'?{lookback_days:period,threshold_pct:number('threshold')}:template==='breakout'?{lookback_days:period}:{drop_pct:number('threshold'),sma_period:period,peak_lookback_days:period};
      await send('/lab/strategies'+(value.id?'/'+value.id:''),{...value,title:fd.get('title'),initial_capital:number('capital'),idealized_costs:fd.has('idealized'),order_type:fd.get('order'),limit_offset_pct:number('offset'),
        config:{universe:{type:'manual_list',tickers:[]},entry:{op:'AND',rules:[{template,params}]},exit:{op:'OR',rules:[{template:'stop_loss',params:{stop_loss_pct:number('stop')}},{template:'take_profit',params:{take_profit_pct:number('target')}}]},
          risk:{max_position_pct:number('position'),max_positions:number('maxpositions'),stop_loss_pct:number('stop'),time_stop:number('hold'),max_drawdown_kill_pct:number('drawdown'),cooldown_days:0},
          costs:{fee_bps:number('fee'),slippage_bps:number('slippage')},params:{}},expected_revision:value.revision_id,reason:fd.get('reason')},value.id?'PATCH':'POST');
    });
  }
  async function renderLab(root,seq){
    const [a,b,c]=await Promise.all([api('/lab/experiments?limit=50'),api('/lab/strategies?limit=100'),api('/lab/runs?limit=50')]);if(seq!==sequence)return;
    root.innerHTML=header('Лаборатория исследований','Зафиксируйте вопрос, состав, данные и метод. Реконструкция истории отмечается отдельно от доступных тогда информационных фактов.',button('Создать вопрос','experiment-new')+button('Создать стратегию','strategy-new'));
    root.querySelector('[data-content]').innerHTML='<h3>Вопросы</h3>'+a.data.items.map(e=>'<article class="research-card"><h4>'+esc(e.title)+'</h4><p>'+esc(e.question)+'</p><p>'+esc(e.experiment_type==='simulation'?'Симуляция':'Описательное исследование')+' · '+esc(date(e.from_at))+' — '+esc(date(e.to_at))+'</p>'+button('Данные и запуск','experiment','data-id="'+e.id+'"')+'</article>').join('')+'<h3>Стратегии</h3>'+b.data.items.map(s=>'<p>'+esc(s.title)+' '+button('Новая версия','strategy','data-id="'+s.id+'"')+'</p>').join('')+'<h3>Запуски</h3>'+c.data.items.map(r=>'<article class="research-card"><p>'+esc(a.data.items.find(e=>e.id===r.experiment_id)?.title||'Исследование')+' · '+esc(statuses[r.status])+'</p>'+button('Результат и источники','run','data-id="'+r.id+'"')+'</article>').join('');
    root.querySelector('[data-wb="experiment-new"]').onclick=()=>experimentForm().catch(fail);root.querySelector('[data-wb="strategy-new"]').onclick=()=>strategyForm();
    root.querySelectorAll('[data-wb="experiment"]').forEach(btn=>btn.onclick=()=>experimentDetail(btn.dataset.id).catch(fail));root.querySelectorAll('[data-wb="strategy"]').forEach(btn=>btn.onclick=()=>strategyForm(b.data.items.find(s=>s.id===btn.dataset.id)));
    root.querySelectorAll('[data-wb="run"]').forEach(btn=>btn.onclick=()=>runDetail(btn.dataset.id).catch(fail));
  }
  async function experimentDetail(identifier){
    const [a,b,c]=await Promise.all([api('/lab/experiments/'+identifier),api('/lab/experiments/'+identifier+'/datasets'),api('/lab/strategies?limit=100')]);const e=a.data;let datasets=b.data.items;
    const d=dialog(e.title,'<p>'+esc(e.question)+'</p><p>'+esc(date(e.from_at))+' — '+esc(date(e.to_at))+'</p><div class="research-toolbar">'+button('Изменить вопрос','edit-experiment')+button('Подготовить снимок данных','prepare')+button('Запустить','start-run')+button('Подбор параметров','sweep')+'</div><p data-job></p><div data-coverage></div><h3>Сохранённые снимки</h3><div data-datasets>'+datasets.map(row=>'<p>'+esc(date(row.created_at))+' · '+esc(row.content_hash.slice(0,12))+'</p>').join('')+'</div>');
    d.querySelector('[data-wb="edit-experiment"]').onclick=()=>{d.close();experimentForm(e).catch(fail);};
    d.querySelector('[data-wb="prepare"]').onclick=async()=>{try{const j=await send('/lab/datasets/prepare',{experiment_id:identifier,revision_id:e.revision_id});await waitJob(j.data.job_id,d.querySelector('[data-job]'),async out=>{
      const data=(await api('/lab/datasets/'+out.dataset_id)).data;datasets=(await api('/lab/experiments/'+identifier+'/datasets')).data.items;
      d.querySelector('[data-coverage]').innerHTML='<h3>Покрытие данных</h3>'+table(['Рынок','H1','D1','Пропуски H1','Некорректные H1'],Object.entries(data.coverage).map(([key,v])=>[esc(data.resolvers[key].instrument.symbol),v.h1_bars,v.d1_bars,v.missing_h1,v.invalid_h1]))+'<p>Режим: '+esc(data.data_mode)+'</p><p class="research-muted">Исторические свечи сохранены как реконструкция. События используются со времени их фактического получения приложением.</p>';
      d.querySelector('[data-datasets]').textContent='Снимков: '+datasets.length+' · Последний '+date(datasets[0]?.created_at);
    });}catch(error){fail(error);}};
    d.querySelector('[data-wb="start-run"]').onclick=()=>{
      if(!datasets.length){fail(new Error('Сначала подготовьте снимок'));return;}
      form('Запуск на сохранённых данных',[{name:'dataset',label:'Снимок',type:'select',options:datasets.map(row=>[row.id,date(row.created_at)+' · '+row.content_hash.slice(0,8)])},
        ...(e.experiment_type==='simulation'?[{name:'strategy',label:'Версия стратегии',type:'select',options:c.data.items.map(s=>[s.id,s.title]),required:true}]:[])],async fd=>{
        const j=await send('/lab/runs',{dataset_id:fd.get('dataset'),strategy_id:fd.get('strategy')});await waitJob(j.data.job_id,d.querySelector('[data-job]'),()=>runDetail(j.data.id));
      });
    };
    d.querySelector('[data-wb="sweep"]').onclick=()=>{
      if(e.experiment_type!=='simulation'||!datasets.length||!c.data.items.length){fail(new Error('Нужны вопрос симуляции, снимок и стратегия'));return;}
      form('Подбор параметров без использования holdout',[
        {name:'strategy',label:'Базовая стратегия',type:'select',options:c.data.items.map(s=>[s.id,s.title])},
        {name:'stops',label:'Значения стопа, % через запятую',required:true},{name:'train',label:'Конец train UTC',type:'date',required:true},{name:'validation',label:'Конец validation UTC',type:'date',required:true}
      ],async fd=>{const variants=fd.get('stops').split(',').map(v=>({'risk.stop_loss_pct':Number(v.trim())}));const j=await send('/lab/sweeps',{dataset_id:datasets[0].id,strategy_id:fd.get('strategy'),variants,
        train_to:Date.parse(fd.get('train')+'T00:00:00Z'),validation_to:Date.parse(fd.get('validation')+'T00:00:00Z'),embargo_hours:24});await waitJob(j.data.job_id,d.querySelector('[data-job]'),async()=>{
        const report=(await api('/lab/sweeps/'+j.data.id)).data;dialog('Результаты подбора',table(['Вариант','Train, %','Validation, %','Состояние'],report.variants.map((v,index)=>[String(index+1)+(index===report.selected_variant?' · выбран':''),num(v.train_result?.metrics.return_pct,'%'),num(v.validation_result?.metrics.return_pct,'%'),esc(v.validation_result?.status||'')]))+'<p>Holdout выбранного варианта: '+num(report.holdout_result?.metrics.return_pct,'%')+'</p><p>Число попыток: '+report.attempts+'. Выбор сделан только по validation.</p>');});});
    };return d;
  }
  async function runDetail(identifier){
    const run=(await api('/lab/runs/'+identifier)).data;const report=run.result||{},m=report.metrics||{};
    const d=dialog('Результат исследования','<p>'+esc(statuses[run.status]||run.status)+' · '+esc(date(run.created_at))+'</p><p>Режим данных: '+esc(report.data_mode||'Снимок подготовлен')+'</p>'+
      (run.error_code?'<p class="research-error">'+esc(run.error_code)+'</p>':'')+
      (report.groups?table(['Группа','Горизонт','n / всего','Среднее','Медиана','К BTC','Пропуски'],report.groups.map(g=>[esc(g.cohort),g.horizon_days+' дн',g.n+' / '+g.denominator,num(g.mean,'%'),num(g.median,'%'),num(g.mean_excess,' п.п.'),g.missing])):
        table(['Капитал','Итоговая стоимость','Доходность','Просадка','Закрытых сделок','Комиссии'],[[num(m.initial_capital,' USDT'),num(m.final_equity,' USDT'),num(m.return_pct,'%'),num(m.max_drawdown_pct,'%'),m.closed_trades??'—',num(m.fees_usdt,' USDT')]]))+
      '<div class="research-toolbar">'+button('Показать эпизоды','episodes')+button('Показать сделки','trades')+button('Остановить','cancel-run')+'<a class="btn small" href="/api/research/v1/lab/runs/'+id(identifier)+'/export">Скачать CSV и manifest</a></div><div data-run-rows></div><p class="research-muted">Данные, параметры, версии и все входы сохранены в manifest. При неполных данных итог не считается полным результатом.</p>');
    if(!report.groups&&['succeeded','incomplete'].includes(run.status)){const points=(await api('/lab/runs/'+identifier+'/chart')).data;d.querySelector('[data-run-rows]').innerHTML=chart(points.points,'equity',points.sampled?Infinity:5400000)+(points.sampled?'<p class="research-muted">График сокращён до 500 точек; полный ряд доступен в экспорте.</p>':'');}
    for(const kind of ['episodes','trades'])d.querySelector('[data-wb="'+kind+'"]').onclick=async()=>{const data=(await api('/lab/runs/'+identifier+'/'+kind+'?limit=100')).data;d.querySelector('[data-run-rows]').innerHTML=kind==='trades'?table(['Рынок','Сигнал','Вход','Выход','Net P&L','Причина'],data.items.map(t=>[String(t.instrument_id),esc(date(t.signal_time)),esc(date(t.entry_at))+' · '+num(t.entry_price),esc(date(t.exit_at))+' · '+num(t.exit_price),num(t.net_pnl,' USDT'),esc(t.exit_reason)])):table(['Рынок','Обнаружено','База после решения','Группа','Исключение'],data.items.map(v=>[String(v.instrument_id),esc(date(v.decision_at)),esc(date(v.baseline_at)),esc(v.cohort),esc(v.exclusion_reason||'Включён')]))+'<p>Показано '+data.items.length+' из '+data.total+'</p>';};
    d.querySelector('[data-wb="cancel-run"]').onclick=async()=>{await send('/lab/runs/'+identifier+'/cancel',{});d.close();await mount(container,'lab');};return d;
  }
  async function sourceActions(node,ref,assetUid=null){
    const cfg=(await api('/workbench/config')).data;if(!node.isConnected)return;
    const actions=document.createElement('div');actions.className='research-toolbar';
    if(cfg.features.ideas){const b=document.createElement('button');b.className='btn small';b.textContent='Создать исследовательскую идею';b.onclick=()=>ideaForm({}, {primary_asset_uid:assetUid,source_refs:[ref]}).catch(fail);actions.append(b);
      if(cfg.model.configured){const ai=document.createElement('button');ai.className='btn small';ai.textContent='Подготовить ИИ-черновик';ai.onclick=async()=>{const status=document.createElement('p');status.className='research-status';actions.after(status);ai.disabled=true;try{const job=await send('/ideas/prepare-draft',{primary_asset_uid:assetUid,source_refs:[ref]});await waitJob(job.data.job_id,status,out=>ideaDetail(out.idea_id));}catch(e){fail(e);}finally{ai.disabled=false;}};actions.append(ai);}}
    if(cfg.features.playbook){const b=document.createElement('button');b.className='btn small';b.textContent='В учебник';b.onclick=()=>exampleForm(ref).catch(fail);actions.append(b);}
    node.querySelector('h2')?.after(actions);
  }
  async function context(node,data){
    node.dataset.researchAsset=data.asset_uid;const [cfg,r]=await Promise.all([api('/workbench/config'),api('/ideas?status=active&asset_uid='+data.asset_uid+'&limit=3').catch(()=>null)]);
    if(!node.isConnected||node.dataset.researchAsset!==data.asset_uid||!cfg.data.features.ideas)return;
    const block=document.createElement('section');block.className='research-ideas-compact';block.innerHTML='<h4>Исследовательские идеи</h4>'+(r?.data.items||[]).map(v=>'<p>'+button(v.title,'context-idea','data-id="'+v.id+'"')+'<br><span class="research-muted">'+esc(statuses[v.status])+'</span></p>').join('')+button('Сохранить исследовательскую идею','context-new');node.append(block);
    block.querySelector('[data-wb="context-new"]').onclick=()=>ideaForm({}, {primary_asset_uid:data.asset_uid,instrument_id:data.instrument_id}).catch(fail);
    block.querySelectorAll('[data-wb="context-idea"]').forEach(b=>b.onclick=()=>ideaDetail(b.dataset.id).catch(fail));
  }
  return {mount,ideaForm,ideaDetail,exampleForm,sourceActions,context};
})();
