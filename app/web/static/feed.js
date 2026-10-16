'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const isX = location.pathname !== '/telegram-feed.html';
  const sourceKind = isX ? 'x' : 'telegram';
  $('feed-kind').value = sourceKind;
  $('feed-view').value = isX ? 'posts' : 'stories';
  if(isX){
    $('feed-view-label').hidden = true;
    $('feed-stars').closest('label').hidden = true;
  }
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = (path, options) => HTF.api('/api/research/v1' + path, options);
  const date = value => value ? new Date(value).toLocaleString('ru-RU', {timeZone:'Europe/Moscow'}) + ' МСК' : '—';
  const link = (url,label) => /^https?:\/\//i.test(url || '') ? `<a class="btn small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>` : '';
  const errors = {model_not_configured:'Модель не настроена',terminal_validation:'Ответ модели не прошёл проверку',model_error:'Ошибка провайдера',disabled:'Обработка выключена'};
  let items = [], more = false, busy = false, selected = null, telegram = null, timer = null;
  let collection = 'all';
  let stars;
  let assetSearchSeq = 0;
  let classificationDirty = false;
  const classificationKey = item => JSON.stringify(item.classification || {});
  const autoLabel = c => c.origin==='manual'?'Ваш разбор':c.origin==='xai'?'xAI · '+({low:'низкая',medium:'средняя',high:'высокая'}[c.confidence]||'')+' уверенность':c.auto_error?(errors[c.auto_error]||'Ошибка xAI · можно повторить'):c.auto_status==='running'?'xAI обрабатывает':c.auto_status==='queued'?'В очереди xAI':c.origin==='model'?'ИИ-разбор':'Ожидает xAI';
  const directionName = d => ({bullish:'↗ Рост · Bullish',bearish:'↘ Снижение · Bearish'}[d] || 'Направление не определено');
  const signal = item => item.classification || {};
  const badges = item => {
    const c=signal(item),symbols=[...new Set([c.symbol,...(c.candidate_symbols||[])].filter(Boolean))];
    return `<div class="feed-signal-tags"><span class="feed-bias ${esc(c.direction || 'pending')}">${directionName(c.direction)}</span>${symbols.slice(0,5).map(t=>`<span class="feed-ticker">${esc(t)}</span>`).join('')}${!symbols.length?'<span class="feed-unmapped">Тикер не определён</span>':''}<span class="feed-signal-origin">${esc(autoLabel(c))}</span></div>`;
  };
  function metrics(){
    const bullish=items.filter(i=>signal(i).direction==='bullish').length,bearish=items.filter(i=>signal(i).direction==='bearish').length;
    $('feed-metrics').innerHTML=[['Загружено',items.length,'Публикации в этой ленте',''],['За рост',bullish,'Позитивный тезис · Bullish','bullish'],['За снижение',bearish,'Негативный тезис · Bearish','bearish'],['Уточнить',items.filter(i=>!signal(i).direction||!signal(i).primary_asset_uid).length,'Не определён актив или направление','']].map(([label,n,note,cls])=>`<div class="feed-metric ${cls}" title="${note}"><span>${label}</span><strong>${n}</strong></div>`).join('');
  }
  async function loadIdeas(){
    try {
      const r=await api('/ideas?limit=4');
      $('feed-ideas-count').textContent=r.data.total;
      $('feed-ideas-list').innerHTML=r.data.items.map(v=>`<button class="feed-idea-row" data-idea="${esc(v.id)}"><span class="feed-bias ${v.direction==='positive'?'bullish':v.direction==='negative'?'bearish':'pending'}">${v.direction==='positive'?'↗ Bullish':v.direction==='negative'?'↘ Bearish':'Наблюдение'}</span><strong>${esc(v.title)}</strong><small>${esc(v.horizon_label || 'Горизонт не определён')} · ${esc({draft:'Черновик',watching:'Наблюдение',supported:'Подтверждается',disputed:'Есть противоречия',resolved:'Завершена',invalidated:'Опровергнута',archived:'Архив'}[v.status]||v.status)}</small></button>`).join('') || '<div class="feed-ideas-empty"><span>Пока нет сохранённых идей</span><p>Создайте из публикации черновик и задайте условия наблюдения.</p><a href="/ideas.html">Открыть раздел идей →</a></div>';
      $('feed-ideas-list').querySelectorAll('[data-idea]').forEach(b=>b.onclick=()=>ResearchWorkbench.ideaDetail(b.dataset.idea).catch(e=>$('feed-status').textContent=e.message));
    }catch(e){$('feed-ideas-list').innerHTML='<p class="research-muted">'+esc(e.message)+'</p><a href="/ideas.html">Открыть раздел идей →</a>';}
  }
  function sourceRefs(item,story){
    return (story?story.posts:[item]).map(p=>({kind:'material',id:p.id,revision_id:p.revision_id})).filter(p=>p.id&&p.revision_id).slice(0,30);
  }
  async function createIdea(item,story){
    const c=signal(item),a=item.telegram?.analysis;
    if(!c.direction||!c.primary_asset_uid){openPost(item);$('feed-classification')?.scrollIntoView({block:'center',behavior:'smooth'});$('feed-classify-note').textContent='Сначала выберите тикер и Bullish / Bearish.';return;}
    await ResearchWorkbench.ideaForm({}, {title:(a?.title_ru||item.title||item.original_text||'Идея').slice(0,200),primary_asset_uid:c.primary_asset_uid,
      direction:c.direction==='bullish'?'positive':'negative',thesis:(a?.what_happened_ru||a?.summary_ru||item.original_text||'').slice(0,8000),mechanism:(a?.why_matters_ru||c.reason_ru||'').slice(0,4000),
      criteria:(a?.check_ru||[]).filter(t=>t.length>=5).slice(0,20).map(description=>({type:'confirmation',description,evaluator:'manual'})),source_refs:sourceRefs(item,story)}, {feed:true,symbol:c.symbol,sourceCount:story?.posts.length||1});
  }
  function classificationHTML(item){
    const c=signal(item);
    const auto=`<div class="feed-auto-explanation"><strong>${esc(autoLabel(c))}</strong>${c.origin==='xai'?'<p>'+esc(c.reason_ru)+'</p>':''}${c.text_truncated?'<p class="research-muted">Разобраны первые 16 000 символов публикации.</p>':''}${c.origin==='manual'?'<p>Ручная правка имеет приоритет. xAI её не перезапишет.</p>':''}${c.origin!=='manual'&&c.auto_status!=='succeeded'?'<button class="btn small" id="feed-classify-retry" '+(['running','queued'].includes(c.auto_status)?'disabled':'')+'>'+(['running','queued'].includes(c.auto_status)?'Обрабатывается в фоне':'Определить через xAI')+'</button>':''}</div>`;
    return `<section class="feed-classification" id="feed-classification"><div class="feed-section-head"><h3>Основа идеи</h3><span class="research-muted">Тикер + направление</span></div>${auto}<label>Актив<input type="search" id="feed-asset-search" placeholder="Например, SOL или Solana" autocomplete="off" value="${esc(c.asset_name || c.symbol || (c.candidate_symbols?.length===1?c.candidate_symbols[0]:''))}"></label><label>Тикер<select id="feed-asset"><option value="">Выберите актив</option>${c.primary_asset_uid?`<option selected value="${esc(c.primary_asset_uid)}">${esc(c.symbol+(c.asset_name&&c.asset_name!==c.symbol?' · '+c.asset_name:''))}</option>`:''}</select></label><fieldset class="feed-direction-choice"><legend>Направление публикации</legend><label class="bullish"><input type="radio" name="post-direction" value="bullish" ${c.direction==='bullish'?'checked':''}>↗ Bullish</label><label class="bearish"><input type="radio" name="post-direction" value="bearish" ${c.direction==='bearish'?'checked':''}>↘ Bearish</label></fieldset><p class="research-muted" id="feed-classify-note" role="status" aria-live="polite">${c.candidate_symbols?.length?'Тикеры из разбора: '+esc(c.candidate_symbols.join(', '))+'. Проверьте соответствие активу.':'Укажите актив, к которому относится тезис публикации.'}</p><div class="feed-compose-actions"><button class="btn" id="feed-classify-save">Сохранить правку</button><button class="btn primary" id="feed-create-idea">+ Создать идею</button></div><div id="feed-existing-ideas"></div></section>`;
  }
  function bindClassification(item,story){
    const c=signal(item),asset=$('feed-asset'),search=$('feed-asset-search'),note=$('feed-classify-note');
    let searchTimer,assetTouched=false;
    const showExisting=async()=>{
      const selectedAsset=asset.value;if(!selectedAsset){$('feed-existing-ideas').innerHTML='';return;}
      try{const r=await api('/ideas?limit=20&asset_uid='+encodeURIComponent(selectedAsset));r.data.items=r.data.items.filter(v=>['draft','watching','supported','disputed'].includes(v.status));
        if(!asset.isConnected||asset.value!==selectedAsset)return;
        $('feed-existing-ideas').innerHTML=r.data.items.length?'<p class="research-muted">Уже наблюдаете этот актив</p>'+r.data.items.map(v=>`<div class="feed-existing"><span>${esc(v.title)}</span><button class="btn small" data-attach-idea="${esc(v.id)}">+ Добавить источник</button></div>`).join(''):'';
        $('feed-existing-ideas').querySelectorAll('[data-attach-idea]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const idea=r.data.items.find(v=>v.id===b.dataset.attachIdea);let revision=idea.revision_id;for(const ref of sourceRefs(item,story).filter(ref=>!idea.source_refs.some(old=>old.id===ref.id&&old.revision_id===ref.revision_id))){const result=await api('/ideas/'+encodeURIComponent(idea.id)+'/links',{method:'POST',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({source_ref:ref,expected_revision:revision,reason:'Источник из информационной ленты'})});revision=result.data.revision_id;idea.source_refs.push(ref);}idea.revision_id=revision;note.textContent='Источник добавлен в существующую идею.';}catch(e){note.textContent=e.message;}finally{b.disabled=false;}});
      }catch(e){if(asset.isConnected)$('feed-existing-ideas').textContent=e.message;}
    };
    async function findAssets(){
      const seq=++assetSearchSeq,q=search.value.trim()||c.candidate_symbols?.[0]||c.symbol||'';
      try{const r=await api('/assets?limit=30&q='+encodeURIComponent(q));if(seq!==assetSearchSeq||!asset.isConnected)return;
        const previous=asset.value,previousLabel=asset.selectedOptions[0]?.textContent;
        asset.innerHTML='<option value="">Выберите актив</option>'+(previous&&!r.data.items.some(a=>a.asset_uid===previous)?`<option value="${esc(previous)}">${esc(previousLabel)}</option>`:'')+r.data.items.map(a=>`<option value="${esc(a.asset_uid)}">${esc(a.display_symbol+(a.name&&a.name!==a.display_symbol?' · '+a.name:''))}</option>`).join('');asset.value=previous;
      }catch(e){if(asset.isConnected)note.textContent=e.message;}
    }
    search.oninput=()=>{assetTouched=true;classificationDirty=true;clearTimeout(searchTimer);assetSearchSeq++;searchTimer=setTimeout(findAssets,250);};asset.onchange=()=>{assetTouched=true;classificationDirty=true;const label=asset.selectedOptions[0]?.textContent;if(asset.value&&label)search.value=label.split(' · ')[1]||label;showExisting();};
    $('feed-classification').querySelectorAll('[name="post-direction"]').forEach(r=>r.onchange=()=>classificationDirty=true);
    if($('feed-classify-retry'))$('feed-classify-retry').onclick=async()=>{const b=$('feed-classify-retry');b.disabled=true;try{await api('/feed/'+encodeURIComponent(item.id)+'/classify',{method:'POST',body:'{}'});note.textContent='Классификация поставлена в очередь xAI.';await load(false,true);}catch(e){note.textContent=e.message;}finally{if(b.isConnected)b.disabled=false;}};
    async function save(andCreate){
      const direction=$('feed-classification').querySelector('[name="post-direction"]:checked')?.value;
      if(!asset.value||!direction){note.textContent='Выберите тикер и направление Bullish или Bearish.';return;}
      const saveButton=$('feed-classify-save'),createButton=$('feed-create-idea');saveButton.disabled=createButton.disabled=true;
      try{const r=await api('/feed/'+encodeURIComponent(item.id)+'/classification',{method:'PUT',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({primary_asset_uid:asset.value,direction,source_revision_id:item.revision_id,expected_revision:item.classification?.revision_id})});
        item.classification={...item.classification,...r.data,origin:'manual',symbol:asset.selectedOptions[0].textContent.split(' · ')[0],asset_name:asset.selectedOptions[0].textContent.split(' · ')[1]||asset.selectedOptions[0].textContent};
        classificationDirty=false;const current=items.find(i=>i.id===item.id&&i.revision_id===item.revision_id);if(current)current.classification=item.classification;
        note.textContent='Тикер и направление сохранены.';render();if(andCreate)await createIdea(item,story);
      }catch(e){note.textContent=e.message;}finally{saveButton.disabled=createButton.disabled=false;}
    }
    $('feed-classify-save').onclick=()=>save(false);$('feed-create-idea').onclick=()=>save(true);
    async function initializeAsset(){
      if(!c.primary_asset_uid&&(c.symbol||c.candidate_symbols?.length===1)){
        note.textContent='Подставляем актив и тикер из разбора…';
        try{
          const r=await api('/feed/'+encodeURIComponent(item.id)+'/resolve-asset',{method:'POST',body:JSON.stringify({source_revision_id:item.revision_id})}),a=r.data;
          if(!asset.isConnected||assetTouched)return;
          Object.assign(c,{primary_asset_uid:a.asset_uid,symbol:a.display_symbol,asset_name:a.name});
          item.classification=c;const current=items.find(i=>i.id===item.id&&i.revision_id===item.revision_id);if(current)Object.assign(current.classification,c);
          asset.innerHTML='<option value="">Выберите актив</option><option selected value="'+esc(a.asset_uid)+'">'+esc(a.display_symbol+(a.name&&a.name!==a.display_symbol?' · '+a.name:''))+'</option>';
          search.value=a.name||a.display_symbol;note.textContent='Актив и тикер подставлены автоматически.';render();
        }catch(e){if(asset.isConnected&&!assetTouched)note.textContent=e.message;}
      }
      if(asset.isConnected&&!assetTouched){await findAssets();showExisting();}
    }
    initializeAsset();
  }
  try { stars = new Set(JSON.parse(localStorage.getItem('stv:feed:stars') || '[]')); } catch { stars = new Set(); }
  const kind = item => item.source_kind === 'telegram' ? 'Telegram' : 'X';
  const list = values => values?.length ? '<ul>' + values.map(v => '<li>'+esc(v)+'</li>').join('') + '</ul>' : '';
  function analysisHTML(a,meta={}) {
    return `<p class="research-muted">ИИ-разбор · важность ${esc(a.impact)}/5 · ${esc({bullish:'позитивный',bearish:'негативный',neutral:'нейтральный'}[a.sentiment] || a.sentiment)} · уверенность ${esc({low:"низкая",medium:"средняя",high:"высокая"}[a.confidence] || a.confidence)}</p>
      <h3>Кратко</h3><p>${esc(a.summary_ru)}</p><h3>Что произошло</h3><p>${esc(a.what_happened_ru)}</p>
      ${a.why_matters_ru ? '<h3>Почему это может быть важно</h3><p>'+esc(a.why_matters_ru)+'</p>' : ''}
      ${a.tickers.length ? '<p>Тикеры-кандидаты: '+a.tickers.map(t=>esc(t.symbol)).join(', ')+'</p>' : ''}
      ${a.entities.length ? '<p>Участники: '+a.entities.map(e=>esc(e.name)+(e.action ? ' — '+esc(e.action) : '')).join('; ')+'</p>' : ''}
      <h3>Что проверить</h3>${list(a.check_ru)}${a.unclear.length ? '<h3>Неясности</h3>'+list(a.unclear) : ''}
      ${a.event ? '<p>Возможное событие: '+esc(a.event.title)+' · '+esc(a.event.date || a.event.date_text || 'дата не определена')+'</p>' : ''}
      <details open><summary>Перевод на русский${meta.text_truncated ? ' · первые 6000 символов' : ''}</summary><div class="feed-text">${esc(a.translation_ru)}</div></details>
      <p class="research-muted">Выводы модели требуют проверки. Содержимое ссылок и изображения в этом разборе не анализировались.</p>`;
  }
  function revealDetail() {
    if(matchMedia('(max-width: 850px)').matches){
      $('feed-detail').scrollIntoView({block:'start',behavior:'smooth'});
      $('feed-detail').focus({preventScroll:true});
    }
  }
  function openPost(item, reveal = false) {
    classificationDirty=false;
    selected = {type:'post',id:item.id,classificationKey:classificationKey(item)};
    const result=item.telegram, a=result?.analysis;
    $('feed-detail').innerHTML = `<span class="feed-eyebrow">Разбор публикации · ${kind(item)}</span><h2>${esc(a?.title_ru || item.title || item.author || 'Публикация')}</h2><p class="research-muted">${esc(item.author || item.source_id || 'Источник')} · ${esc(date(item.published_at))}</p>${badges(item)}${classificationHTML(item)}
      ${a ? analysisHTML(a,result) : item.source_kind==='telegram' ? '<p class="research-muted">'+esc(errors[result?.error_code] || (result?.status==='error' ? 'Разбор не выполнен. Можно повторить.' : 'Публикация ожидает ИИ-разбора.'))+'</p>' : ''}
      <details${a ? '' : ' open'}><summary>Оригинальный текст</summary><div class="feed-text">${esc(item.original_text || 'Текст отсутствует.')}</div></details>
      ${link(item.canonical_url,'Открыть оригинал')}<button class="btn small" id="feed-analysis">Разбор и версии</button>${item.source_kind==='telegram' && !a ? '<button class="btn small" id="feed-retry">Разобрать / повторить</button>' : ''}`;
    $('feed-analysis').onclick = () => ResearchUI.material(item.id).catch(error => { $('feed-status').textContent = error.message; });
    if($('feed-retry')) $('feed-retry').onclick=()=>action('/telegram/materials/'+encodeURIComponent(item.id)+'/analyze',$('feed-retry'));
    bindClassification(item);
    render();
    if(reveal)revealDetail();
  }
  async function openStory(story, reveal = false) {
    selected={type:'story',id:story.id};
    const best=[...story.posts].sort((a,b)=>b.analysis.impact-a.analysis.impact)[0];
    $('feed-detail').innerHTML=`<span class="research-badge">Сюжет · ${story.posts.length} публикаций · ${story.channels.length} каналов</span><h2>${esc(story.title)}</h2><p class="research-muted">${esc(date(story.firstAt))} — ${esc(date(story.lastAt))}</p>${analysisHTML(best.analysis,best)}<h3>Все первоисточники</h3>${story.posts.map(p=>'<div class="feed-story-source"><strong>'+esc(p.post.channel)+'</strong><p>'+esc(p.analysis.summary_ru)+'</p>'+link(p.canonical_url,'Оригинал')+'<button class="btn small" data-source="'+esc(p.id)+'" data-source-revision="'+esc(p.revision_id)+'">Разбор и версии</button><details><summary>Перевод и оригинал</summary><div class="feed-text">'+esc(p.analysis.translation_ru)+'</div><hr><div class="feed-text">'+esc(p.post.text)+'</div></details></div>').join('')}`;
    let item=items.find(i=>i.id===best.id&&i.revision_id===best.revision_id);
    if(!item){try{item=(await api('/feed/'+encodeURIComponent(best.id))).data;}catch(e){$('feed-status').textContent=e.message;}}
    if(selected?.type!=='story'||selected.id!==story.id)return;
    if(item&&item.revision_id===best.revision_id){$('feed-detail').insertAdjacentHTML('afterbegin',classificationHTML(item));bindClassification(item,story);}
    $('feed-detail').querySelectorAll('[data-source]').forEach(b=>b.onclick=()=>ResearchUI.material(b.dataset.source,null,b.dataset.sourceRevision).catch(e=>$('feed-status').textContent=e.message));render();
    if(reveal)revealDetail();
  }
  function emptyState(filtered, stories = false) {
    const title=filtered?'Ничего не найдено':stories?'Сюжетов пока нет':'Публикаций пока нет';
    const message=filtered?(collection==='starred'?'Отмечайте публикации звездой, чтобы вернуться к ним позже.':collection==='ready'?'Здесь появятся публикации с определённым активом и направлением.':'Измените поисковый запрос или выберите другие фильтры.'):stories?'Сюжеты появятся после разбора публикаций. Можно переключиться на отдельные посты.':'Подключите источники в настройках. Новые публикации появятся здесь после загрузки.';
    return `<div class="research-card feed-empty"><h2>${title}</h2><p class="research-muted">${message}</p>${!filtered?'<a class="feed-text-link" href="/?section=imports#settings">Настроить источники →</a>':''}</div>`;
  }
  function render() {
    metrics();
    document.querySelector('.feed-rail').classList.toggle('has-selection',Boolean(selected));
    const q = $('feed-search').value.trim().toLowerCase(), filter = $('feed-kind').value, storyMode=$('feed-view').value==='stories',direction=$('feed-direction').value;
    $('feed-list-title').textContent=storyMode?'Сюжеты Telegram':isX?'Публикации X':'Публикации Telegram';
    $('telegram-panel').hidden=filter==='x';
    if(storyMode){
      const shown=(telegram?.items || []).filter(s=>filter!=='x' && (!direction||(direction==='pending'?!['bullish','bearish'].includes(s.sentiment):s.sentiment===direction)) && (!$('feed-stars').checked || s.posts.some(p=>stars.has(p.id))) && [s.title,...s.allTickers,...s.channels,...s.posts.map(p=>p.analysis.summary_ru)].join(' ').toLowerCase().includes(q));
      shown.sort((a,b)=>b.priority.score-a.priority.score || b.lastAt-a.lastAt);
      $('feed-count').textContent=`Сюжетов: ${shown.length} · по важности · последние 7 дней${telegram?.capped ? ' · показаны сюжеты из 300 последних постов' : ''}`;
      $('feed-list').innerHTML=shown.map(s=>`<article class="research-card feed-post${selected?.id===s.id?' selected':''}"><div class="feed-post-head"><span class="research-badge">${s.channels.length} каналов · ${s.posts.length} постов</span><span>${esc(s.priority.level==='important'?'Важно':s.priority.level==='interesting'?'Интересно':'Сюжет')}</span></div><button class="feed-open" data-story="${esc(s.id)}">${esc(s.title)}</button><p class="feed-excerpt">${esc(s.posts[0].analysis.summary_ru)}</p><p class="research-muted">${s.priority.reasons.map(r=>esc(r.label)).join(' · ')} · ${esc(date(s.lastAt))}</p></article>`).join('') || emptyState(Boolean(q||direction||$('feed-stars').checked),true);
      $('feed-list').querySelectorAll('[data-story]').forEach(b=>b.onclick=()=>openStory(telegram.items.find(s=>s.id===b.dataset.story),true));
      $('feed-more').hidden=true;return;
    }
    const shown = items.filter(item => (!filter || (filter === 'x' ? kind(item) === 'X' : kind(item) === 'Telegram')) &&
      (!direction||(direction==='pending'?(!signal(item).direction||!signal(item).primary_asset_uid):signal(item).direction===direction)) &&
      (collection!=='starred'||stars.has(item.id)) && (collection!=='ready'||(signal(item).direction&&signal(item).primary_asset_uid)) &&
      (!$('feed-stars').checked || stars.has(item.id)) && [item.title,item.original_text,item.author,item.source_id,item.telegram?.analysis?.translation_ru,item.telegram?.analysis?.title_ru,signal(item).symbol,...(signal(item).candidate_symbols||[])].join(' ').toLowerCase().includes(q));
    $('feed-count').textContent = `Показано ${shown.length} · Загружено ${items.length}`;
    const card = item => {
      const a=item.telegram?.analysis;
      const ready=signal(item).direction&&signal(item).primary_asset_uid;
      return `<article class="research-card feed-post${selected?.id === item.id ? ' selected' : ''}" data-bias="${esc(signal(item).direction||'pending')}"><div class="feed-post-head"><span class="feed-source-icon ${kind(item)==='X'?'source-x':''}">${kind(item)==='X'?'𝕏':'↗'}</span><span class="feed-author">${esc(item.author || item.source_id || 'Источник')}<small>${kind(item)} · ${esc(date(item.published_at))}</small></span><button class="btn small feed-star" data-star="${esc(item.id)}" aria-label="Избранное" aria-pressed="${stars.has(item.id)}">${stars.has(item.id) ? '★' : '☆'}</button></div><button class="feed-open" data-open="${esc(item.id)}">${esc(a?.title_ru || item.title || (item.original_text || 'Публикация').slice(0,160))}</button>${a?.summary_ru || item.original_text ? '<p class="feed-excerpt">'+esc(a?.summary_ru || item.original_text.slice(0,360))+'</p>' : ''}${badges(item)}<div class="feed-post-footer"><span class="research-muted">${item.telegram ? esc(a ? 'Разобрано · Важность '+a.impact+'/5' : errors[item.telegram.error_code] || (item.telegram.status==='error' ? 'Ошибка разбора' : 'Ожидает разбора')):'Публикация X'}</span><button class="btn small ${ready?'primary':''}" data-create="${esc(item.id)}">${ready?'+ Создать идею':'Классифицировать →'}</button></div></article>`;
    };
    $('feed-list').innerHTML = shown.map(card).join('') || emptyState(Boolean(q||direction||$('feed-stars').checked||collection!=='all'));
    if(isX){const projects=items.filter(i=>signal(i).symbol&&signal(i).reason_ru).slice(0,3);$('feed-projects').innerHTML=projects.length?projects.map(i=>`<p><strong>${esc(signal(i).symbol)}</strong> · ${esc(signal(i).reason_ru)} <button class="btn small" data-project="${esc(i.id)}">Открыть</button></p>`).join(''):'Свежих разобранных тезисов пока нет. Новые публикации подключённых аккаунтов проходят классификацию автоматически.';$('feed-projects').querySelectorAll('[data-project]').forEach(b=>b.onclick=()=>{openPost(items.find(i=>i.id===b.dataset.project));$('feed-detail').scrollIntoView({block:'start',behavior:'smooth'});});}

    $('feed-more').hidden = !more;
    $('feed-list').querySelectorAll('[data-star]').forEach(button => button.onclick = () => {
      const id = button.dataset.star; stars.has(id) ? stars.delete(id) : stars.add(id);
      try { localStorage.setItem('stv:feed:stars', JSON.stringify([...stars])); } catch { $('feed-status').textContent = 'Избранное сохранено только до закрытия страницы.'; }
      render();
    });
    $('feed-list').querySelectorAll('[data-open]').forEach(button => button.onclick = () => openPost(items.find(i=>i.id===button.dataset.open),true));
    $('feed-list').querySelectorAll('[data-create]').forEach(b=>b.onclick=()=>{const item=items.find(i=>i.id===b.dataset.create);openPost(item);if(signal(item).direction&&signal(item).primary_asset_uid)createIdea(item).catch(e=>$('feed-status').textContent=e.message);else{$('feed-detail').scrollIntoView({block:'start',behavior:'smooth'});$('feed-asset-search').focus({preventScroll:true});}});
  }
  function digestHTML(d) {
    if(!d)return '<div class="digest-empty"><strong>Главное за сутки — в одном месте</strong><p>Дайджест ещё не собран. Обновите его, чтобы увидеть сводку по успешно разобранным сюжетам за последние 24 часа.</p></div>';
    const stamp = value => value ? new Date(value).toLocaleString('ru-RU',{timeZone:'Europe/Moscow',day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'}) : '—';
    const analyzed=d.coverage.analyzed,loaded=d.coverage.posts_loaded;
    const coverage=loaded>0 ? Math.min(100,Math.max(0,Math.round(analyzed/loaded*100))) : 0;
    const notes=(values,cls)=>'<ul class="'+cls+'">'+values.map(v=>'<li>'+esc(v)+'</li>').join('')+'</ul>';
    return `<article class="digest-report" aria-labelledby="digest-headline">
      <header class="digest-hero">
        <div class="digest-meta"><span class="digest-eyebrow">Главное за сутки</span><span class="digest-freshness ${d.superseded?'is-stale':''}">${d.superseded?'Есть изменения':'Сводка актуальна'}</span><span class="digest-created">Сформирован ${esc(stamp(d.created_at))} МСК</span></div>
        <h3 id="digest-headline">${esc(d.headline_ru)}</h3>
        ${d.superseded?'<p class="digest-stale-note">После создания сводки данные изменились. Обновите дайджест, чтобы учесть изменения.</p>':''}
        <div class="digest-context"><div class="digest-period"><span>Период сводки · МСК</span><strong>${esc(stamp(d.from_at))} — ${esc(stamp(d.to_at))}</strong></div><div class="digest-coverage"><div><span>Разобрано публикаций</span><strong>${esc(analyzed)} <span>/ ${esc(loaded)}</span></strong></div><div class="digest-progress" role="meter" aria-label="Доля разобранных публикаций" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${coverage}"><span style="width:${coverage}%"></span></div></div></div>
      </header>
      <div class="digest-columns"><section class="digest-events" aria-labelledby="digest-events-heading"><div class="digest-section-heading"><h4 id="digest-events-heading">Ключевые события</h4><span>${d.bullets.length}</span></div>
        <ol class="digest-stories">${d.bullets.map((b,i)=>`<li><span class="digest-story-number" aria-hidden="true">${String(i+1).padStart(2,'0')}</span><div class="digest-story-copy"><p>${esc(b.text_ru)}</p><div class="digest-story-footer">${(b.tickers||[]).length?'<div class="digest-story-tickers">'+b.tickers.map(t=>'<span>'+esc(t)+'</span>').join('')+'</div>':''}<button type="button" class="digest-source" data-digest-story="${esc(b.story_id)}" aria-label="Источники: ${esc(b.text_ru)}">Источники <span aria-hidden="true">↗</span></button></div></div></li>`).join('')}</ol>
      </section>${d.watch_ru.length||d.funds_ru.length?`<aside class="digest-sidebar">${d.watch_ru.length?'<section class="digest-watch" aria-labelledby="digest-watch-heading"><div class="digest-section-heading"><h4 id="digest-watch-heading"><span class="digest-watch-dot" aria-hidden="true"></span>Что отслеживать</h4></div>'+notes(d.watch_ru,'digest-watch-list')+'</section>':''}${d.funds_ru.length?'<section class="digest-funds" aria-labelledby="digest-funds-heading"><div class="digest-section-heading"><h4 id="digest-funds-heading">Фонды</h4></div>'+notes(d.funds_ru,'digest-watch-list')+'</section>':''}</aside>`:''}</div>
      ${d.tickers_heat.length?`<section class="digest-heat" aria-labelledby="digest-heat-heading"><div class="digest-section-heading"><h4 id="digest-heat-heading">Тикеры в публикациях</h4><span class="digest-heat-note">Упоминания · каналы</span></div><ul class="digest-tickers">${d.tickers_heat.map(t=>`<li><strong>${esc(t.symbol)}</strong><span><b>${esc(t.mentions)}</b> упом. <span aria-hidden="true">·</span> <b>${esc(t.channels)}</b> кан.</span></li>`).join('')}</ul></section>`:''}
    </article>`;
  }
  function renderTelegram() {
    const t=telegram;
    const priority=t.items.filter(s=>s.priority.level==='important').slice(0,3);
    $('telegram-priority-block').hidden=!priority.length;
    document.querySelector('.telegram-overview').classList.toggle('has-priority',Boolean(priority.length));
    $('telegram-priority').innerHTML=priority.map(s=>`<button class="telegram-priority-story" data-priority="${esc(s.id)}"><strong>${esc(s.title)}</strong><span>${s.channels.length} каналов · ${esc(s.priority.reasons.map(r=>r.label).join(' · '))}</span></button>`).join('');
    $('telegram-priority').querySelectorAll('[data-priority]').forEach(b=>b.onclick=()=>openStory(t.items.find(s=>s.id===b.dataset.priority),true));
    $('telegram-status').textContent=`За 7 дней: ${t.posts_loaded} постов · разобрано ${t.analyzed} · ожидают ${t.pending} · ошибок ${t.errors}${t.capped ? ' · ограничено 300 последними постами' : ''}`;
    $('feed-health').textContent=`Telegram: ${t.analyzed} разобрано · ${t.pending} в очереди${t.errors?' · '+t.errors+' требуют внимания':''}`;
    const m=t.model;
    $('telegram-budget').textContent=`${t.analysis_enabled ? 'Авторазбор включён' : 'Разбор выключен'} · ${m.configured ? m.provider+' / '+m.model : 'Модель не настроена'} · дайджест ${t.digest_enabled ? 'обновляется при изменениях, не чаще 30 минут' : 'выключен'}`;

    const d=t.digest;
    $('telegram-digest-view').innerHTML=digestHTML(d);
    $('telegram-digest-view').querySelectorAll('[data-digest-story]').forEach(b=>b.onclick=()=>{
      const story=t.items.find(s=>s.id===b.dataset.digestStory);
      if(story && !d.superseded)openStory(story,true);else{
        const saved=d.stories.find(s=>s.story_id===b.dataset.digestStory);
        $('feed-detail').innerHTML='<h2>'+esc(saved?.title || 'Сохранённый сюжет')+'</h2><p>'+esc(saved?.summary || '')+'</p><p class="research-muted">Сюжет изменился или вышел из текущего окна. Версии первоисточников сохранены:</p>'+(d.story_sources?.[b.dataset.digestStory] || d.input_revisions).map(([id,rev])=>'<button class="btn small" data-saved-id="'+esc(id)+'" data-revision="'+esc(rev)+'">Источник '+esc(id.slice(-8))+'</button>').join('');
        $('feed-detail').querySelectorAll('[data-saved-id]').forEach(btn=>btn.onclick=()=>ResearchUI.material(btn.dataset.savedId,null,btn.dataset.revision).catch(e=>$('feed-status').textContent=e.message));
        revealDetail();
      }
    });
    const active=t.jobs.find(j=>['pending','running'].includes(j.status));
    if(active)$('telegram-action').textContent=errors[active.error_code] || 'Фоновая обработка выполняется. Страница обновится автоматически.';
    else if(t.jobs[0]?.status==='failed')$('telegram-action').textContent=errors[t.jobs[0].error_code] || 'Фоновая обработка завершилась с ошибкой. Повторите задание.';
    else if(t.jobs[0]?.status==='succeeded')$('telegram-action').textContent='Последнее задание завершено.';
  }
  async function action(path,button) {
    button.disabled=true;
    try { const r=await api(path,{method:'POST',body:'{}'});$('telegram-action').textContent=r.data.cached ? 'Сохранённый разбор уже доступен.' : 'Задание поставлено в очередь.';await load(); }
    catch(e){$('telegram-action').textContent=e.message;}
    finally{button.disabled=false;}
  }
  async function load(append = false, quiet = false) {
    if (busy) return; busy = true;
    $('feed-refresh').disabled = $('feed-more').disabled = true;
    if(!quiet)$('feed-status').textContent = 'Загружаем публикации…';
    try {
      await HTF.ensureToken();
      const last = append && items.at(-1);
      const response = await api('/feed?limit=50&source_kind='+sourceKind + (last ? `&before=${last.published_at}&before_id=${encodeURIComponent(last.id)}` : ''));
      const previousLast=items.at(-1);
      items = append ? [...items, ...response.data.items] : quiet ? [...response.data.items,...items.filter(i=>!response.data.items.some(n=>n.id===i.id))] : response.data.items;
      if(quiet && previousLast && items.length>50)items.sort((a,b)=>b.published_at-a.published_at || a.id.localeCompare(b.id));
      if(!quiet || !more)more = response.data.has_more;
      const results=await Promise.allSettled([isX?Promise.resolve(null):api('/telegram'),api(isX?'/x-accounts':'/channels'),quiet?Promise.resolve():loadIdeas(),api('/feed-classifier')]);
      if(results[3].status==='fulfilled'){const c=results[3].value.data,m=c.model,q=c.queue;$('feed-classifier-status').textContent=!c.enabled?'Автоматический разбор выключен':!m.configured?'Для автоматического разбора укажите ключ xAI в настройках ИИ':'Разбор через xAI · '+m.model+(q?' · в очереди: '+q.ready+' · обрабатываются: '+q.running+' · отложены: '+q.deferred:'')+(m.rate_limit?.retry_after?' · пауза: '+m.rate_limit.retry_after+' сек.':' · работает в фоне');}
      else $('feed-classifier-status').textContent='Не удалось проверить состояние xAI. Ручная классификация доступна.';
      if(results[0].status==='fulfilled'&&results[0].value){telegram=results[0].value.data;renderTelegram();}else if(!isX){$('telegram-status').textContent='Не удалось загрузить обработку Telegram: '+results[0].reason.message;}
      if(results[1].status==='fulfilled'){
        const data=results[1].value.data,sources=data.items,enabled=sources.filter(s=>s.enabled);
        const summary=isX?`Аккаунты X: ${enabled.length} включено из ${sources.length}. ${data.configured?'X API подключён.':'Для загрузки новых постов подключите X API в настройках.'}`:`Каналы Telegram: ${enabled.length} включено из ${sources.length}.`;
        $('feed-sources').textContent=summary+' '+(enabled.map(s=>'@'+s.handle).join(', ')||'Добавьте источники в настройках.');
        if(isX)$('feed-health').textContent=data.configured?`X API подключён · активных аккаунтов: ${enabled.length}`:'X API не подключён';
      }else{
        $('feed-sources').textContent='Не удалось проверить источники. Откройте настройки или обновите ленту.';
        if(isX)$('feed-health').textContent='Не удалось проверить подключение';
      }
      if(selected?.type==='post'&&!classificationDirty&&!document.querySelector('dialog[open]')){const item=items.find(i=>i.id===selected.id);if(item&&(!quiet||classificationKey(item)!==selected.classificationKey))openPost(item);}else if(!quiet && selected?.type==='story'){const story=telegram?.items.find(s=>s.id===selected.id);if(story)openStory(story);}render();
      if(!quiet)$('feed-status').textContent = 'Обновлено в '+new Date().toLocaleTimeString('ru-RU',{timeZone:'Europe/Moscow',hour:'2-digit',minute:'2-digit'})+' МСК · избранное сохраняется в этом браузере';
      const requested=new URLSearchParams(location.search).get('post');if(requested&&!selected){let post=items.find(i=>i.id===requested);if(!post){try{post=(await api('/feed/'+encodeURIComponent(requested))).data;}catch(e){$('feed-status').textContent=e.message;}}if(post&&post.source_kind===sourceKind)openPost(post);}
    } catch (error) { $('feed-status').textContent = error.message || 'Не удалось загрузить ленту.'; }
    finally { busy = false; $('feed-refresh').disabled = $('feed-more').disabled = false;
      clearTimeout(timer);timer=setTimeout(()=>{if(!document.hidden)load(false,true);else timer=setTimeout(()=>load(false,true),30000);},15000);
    }
  }
  $('feed-refresh').onclick = () => load(); $('feed-more').onclick = () => load(true);
  if(!isX){$('telegram-digest').onclick=()=>action('/telegram/digest',$('telegram-digest'));$('telegram-process').onclick=()=>action('/telegram/process',$('telegram-process'));}
  ['feed-kind','feed-stars','feed-view','feed-direction'].forEach(id => $(id).onchange = () => {if(id==='feed-kind' && $('feed-kind').value==='x')$('feed-view').value='posts';render();});
  $('feed-search').oninput = render;
  document.querySelectorAll('[data-feed-collection]').forEach(button=>button.onclick=()=>{
    collection=button.dataset.feedCollection;
    document.querySelectorAll('[data-feed-collection]').forEach(b=>{const active=b===button;b.classList.toggle('active',active);b.setAttribute('aria-pressed',String(active));});
    render();
  });
  $('feed-back').onclick=()=>{document.querySelector('.feed-stream').scrollIntoView({block:'start',behavior:'smooth'});$('feed-search').focus({preventScroll:true});};
  document.addEventListener('research:ideas-changed',()=>{loadIdeas();$('feed-status').textContent='Идея сохранена. Все черновики и наблюдения доступны в разделе «Идеи».';});
  load();
})();
