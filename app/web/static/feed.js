'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = (path, options) => HTF.api('/api/research/v1' + path, options);
  const date = value => value ? new Date(value).toLocaleString('ru-RU', {timeZone:'Europe/Moscow'}) + ' МСК' : '—';
  const link = (url,label) => /^https?:\/\//i.test(url || '') ? `<a class="btn small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>` : '';
  const errors = {model_not_configured:'Модель не настроена',terminal_validation:'Ответ модели не прошёл проверку',model_error:'Ошибка провайдера',disabled:'Обработка выключена'};
  let items = [], more = false, busy = false, selected = null, telegram = null, timer = null;
  let stars;
  let assetSearchSeq = 0;
  let classificationDirty = false;
  const classificationKey = item => JSON.stringify(item.classification || {});
  const autoLabel = c => c.origin==='manual'?'Ваш разбор':c.origin==='xai'?'xAI · '+({low:'низкая',medium:'средняя',high:'высокая'}[c.confidence]||'')+' уверенность':c.auto_error?(errors[c.auto_error]||'Ошибка xAI · можно повторить'):c.auto_status==='running'?'xAI обрабатывает':c.auto_status==='queued'?'В очереди xAI':c.origin==='model'?'ИИ-разбор':'Ожидает xAI';
  const directionName = d => ({bullish:'↗ Bullish',bearish:'↘ Bearish'}[d] || 'Нужна классификация');
  const signal = item => item.classification || {};
  const badges = item => {
    const c=signal(item),symbols=[...new Set([c.symbol,...(c.candidate_symbols||[])].filter(Boolean))];
    return `<div class="feed-signal-tags"><span class="feed-bias ${esc(c.direction || 'pending')}">${directionName(c.direction)}</span>${symbols.slice(0,5).map(t=>`<span class="feed-ticker">${esc(t)}</span>`).join('')}${!symbols.length?'<span class="feed-unmapped">Тикер не определён</span>':''}<span class="feed-signal-origin">${esc(autoLabel(c))}</span></div>`;
  };
  function metrics(){
    const bullish=items.filter(i=>signal(i).direction==='bullish').length,bearish=items.filter(i=>signal(i).direction==='bearish').length;
    $('feed-metrics').innerHTML=[['Публикации',items.length,'В загруженной ленте',''],['Bullish',bullish,'Позитивный тезис','bullish'],['Bearish',bearish,'Негативный тезис','bearish'],['Классифицировать',items.filter(i=>!signal(i).direction||!signal(i).primary_asset_uid).length,'Уточнить тикер или направление','']].map(([label,n,note,cls])=>`<div class="feed-metric ${cls}"><span>${label}</span><strong>${n}</strong><small>${note}</small></div>`).join('');
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
    return `<section class="feed-classification" id="feed-classification"><div class="feed-section-head"><h3>Основа идеи</h3><span class="research-muted">Тикер + направление</span></div>${auto}<label>Найти актив<input type="search" id="feed-asset-search" placeholder="Например, SOL или Solana" autocomplete="off"></label><label>Тикер<select id="feed-asset"><option value="">Выберите актив</option>${c.primary_asset_uid?`<option selected value="${esc(c.primary_asset_uid)}">${esc(c.symbol)}</option>`:''}</select></label><fieldset class="feed-direction-choice"><legend>Направление публикации</legend><label class="bullish"><input type="radio" name="post-direction" value="bullish" ${c.direction==='bullish'?'checked':''}>↗ Bullish</label><label class="bearish"><input type="radio" name="post-direction" value="bearish" ${c.direction==='bearish'?'checked':''}>↘ Bearish</label></fieldset><p class="research-muted" id="feed-classify-note" role="status" aria-live="polite">${c.candidate_symbols?.length?'Тикеры из разбора: '+esc(c.candidate_symbols.join(', '))+'. Проверьте соответствие активу.':'Укажите актив, к которому относится тезис публикации.'}</p><div class="feed-compose-actions"><button class="btn" id="feed-classify-save">Сохранить правку</button><button class="btn primary" id="feed-create-idea">+ Создать идею</button></div><div id="feed-existing-ideas"></div></section>`;
  }
  function bindClassification(item,story){
    const c=signal(item),asset=$('feed-asset'),search=$('feed-asset-search'),note=$('feed-classify-note');
    let searchTimer;
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
        asset.innerHTML='<option value="">Выберите актив</option>'+(previous&&!r.data.items.some(a=>a.asset_uid===previous)?`<option value="${esc(previous)}">${esc(previousLabel)}</option>`:'')+r.data.items.map(a=>`<option value="${esc(a.asset_uid)}">${esc(a.display_symbol)} · ${esc(a.name)}</option>`).join('');asset.value=previous;
      }catch(e){if(asset.isConnected)note.textContent=e.message;}
    }
    search.oninput=()=>{classificationDirty=true;clearTimeout(searchTimer);assetSearchSeq++;searchTimer=setTimeout(findAssets,250);};asset.onchange=()=>{classificationDirty=true;showExisting();};
    $('feed-classification').querySelectorAll('[name="post-direction"]').forEach(r=>r.onchange=()=>classificationDirty=true);
    if($('feed-classify-retry'))$('feed-classify-retry').onclick=async()=>{const b=$('feed-classify-retry');b.disabled=true;try{await api('/feed/'+encodeURIComponent(item.id)+'/classify',{method:'POST',body:'{}'});note.textContent='Классификация поставлена в очередь xAI.';await load(false,true);}catch(e){note.textContent=e.message;}finally{if(b.isConnected)b.disabled=false;}};
    async function save(andCreate){
      const direction=$('feed-classification').querySelector('[name="post-direction"]:checked')?.value;
      if(!asset.value||!direction){note.textContent='Выберите тикер и направление Bullish или Bearish.';return;}
      const saveButton=$('feed-classify-save'),createButton=$('feed-create-idea');saveButton.disabled=createButton.disabled=true;
      try{const r=await api('/feed/'+encodeURIComponent(item.id)+'/classification',{method:'PUT',headers:{'Idempotency-Key':crypto.randomUUID()},body:JSON.stringify({primary_asset_uid:asset.value,direction,source_revision_id:item.revision_id,expected_revision:item.classification?.revision_id})});
        item.classification={...item.classification,...r.data,origin:'manual',symbol:asset.selectedOptions[0].textContent.split(' · ')[0]};
        classificationDirty=false;const current=items.find(i=>i.id===item.id&&i.revision_id===item.revision_id);if(current)current.classification=item.classification;
        note.textContent='Тикер и направление сохранены.';render();if(andCreate)await createIdea(item,story);
      }catch(e){note.textContent=e.message;}finally{saveButton.disabled=createButton.disabled=false;}
    }
    $('feed-classify-save').onclick=()=>save(false);$('feed-create-idea').onclick=()=>save(true);
    findAssets();showExisting();
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
  function openPost(item) {
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
  }
  async function openStory(story) {
    selected={type:'story',id:story.id};
    const best=[...story.posts].sort((a,b)=>b.analysis.impact-a.analysis.impact)[0];
    $('feed-detail').innerHTML=`<span class="research-badge">Сюжет · ${story.posts.length} публикаций · ${story.channels.length} каналов</span><h2>${esc(story.title)}</h2><p class="research-muted">${esc(date(story.firstAt))} — ${esc(date(story.lastAt))}</p>${analysisHTML(best.analysis,best)}<h3>Все первоисточники</h3>${story.posts.map(p=>'<div class="feed-story-source"><strong>'+esc(p.post.channel)+'</strong><p>'+esc(p.analysis.summary_ru)+'</p>'+link(p.canonical_url,'Оригинал')+'<button class="btn small" data-source="'+esc(p.id)+'" data-source-revision="'+esc(p.revision_id)+'">Разбор и версии</button><details><summary>Перевод и оригинал</summary><div class="feed-text">'+esc(p.analysis.translation_ru)+'</div><hr><div class="feed-text">'+esc(p.post.text)+'</div></details></div>').join('')}`;
    let item=items.find(i=>i.id===best.id&&i.revision_id===best.revision_id);
    if(!item){try{item=(await api('/feed/'+encodeURIComponent(best.id))).data;}catch(e){$('feed-status').textContent=e.message;}}
    if(selected?.type!=='story'||selected.id!==story.id)return;
    if(item&&item.revision_id===best.revision_id){$('feed-detail').insertAdjacentHTML('afterbegin',classificationHTML(item));bindClassification(item,story);}
    $('feed-detail').querySelectorAll('[data-source]').forEach(b=>b.onclick=()=>ResearchUI.material(b.dataset.source,null,b.dataset.sourceRevision).catch(e=>$('feed-status').textContent=e.message));render();
  }
  function render() {
    metrics();
    const q = $('feed-search').value.trim().toLowerCase(), filter = $('feed-kind').value, storyMode=$('feed-view').value==='stories',direction=$('feed-direction').value;
    $('telegram-panel').hidden=filter==='x';
    if(storyMode){
      const shown=(telegram?.items || []).filter(s=>filter!=='x' && (!direction||(direction==='pending'?!['bullish','bearish'].includes(s.sentiment):s.sentiment===direction)) && (!$('feed-stars').checked || s.posts.some(p=>stars.has(p.id))) && [s.title,...s.allTickers,...s.channels,...s.posts.map(p=>p.analysis.summary_ru)].join(' ').toLowerCase().includes(q));
      shown.sort((a,b)=>b.priority.score-a.priority.score || b.lastAt-a.lastAt);
      $('feed-count').textContent=`Сюжетов: ${shown.length} · по важности · последние 7 дней${telegram?.capped ? ' · показаны сюжеты из 300 последних постов' : ''}`;
      $('feed-list').innerHTML=shown.map(s=>`<article class="research-card feed-post"><div class="feed-post-head"><span class="research-badge">${s.channels.length} каналов · ${s.posts.length} постов</span><span>${esc(s.priority.level==='important'?'Важно':s.priority.level==='interesting'?'Интересно':'Сюжет')}</span></div><button class="feed-open" data-story="${esc(s.id)}">${esc(s.title)}</button><p>${esc(s.posts[0].analysis.summary_ru)}</p><p class="research-muted">${s.priority.reasons.map(r=>esc(r.label)).join(' · ')} · ${esc(date(s.lastAt))}</p></article>`).join('') || '<div class="research-card"><h2>Сюжетов пока нет</h2><p class="research-muted">Сюжеты появятся после успешного разбора публикаций. Проверьте фильтры и состояние обработки.</p></div>';
      $('feed-list').querySelectorAll('[data-story]').forEach(b=>b.onclick=()=>openStory(telegram.items.find(s=>s.id===b.dataset.story)));
      $('feed-more').hidden=true;return;
    }
    const shown = items.filter(item => (!filter || (filter === 'x' ? kind(item) === 'X' : kind(item) === 'Telegram')) &&
      (!direction||(direction==='pending'?(!signal(item).direction||!signal(item).primary_asset_uid):signal(item).direction===direction)) &&
      (!$('feed-stars').checked || stars.has(item.id)) && [item.title,item.original_text,item.author,item.source_id,item.telegram?.analysis?.translation_ru,item.telegram?.analysis?.title_ru,signal(item).symbol,...(signal(item).candidate_symbols||[])].join(' ').toLowerCase().includes(q));
    $('feed-count').textContent = `Показано ${shown.length} · Загружено ${items.length}`;
    $('feed-list').innerHTML = shown.map(item => {
      const a=item.telegram?.analysis;
      const ready=signal(item).direction&&signal(item).primary_asset_uid;
      return `<article class="research-card feed-post${selected?.id === item.id ? ' selected' : ''}" data-bias="${esc(signal(item).direction||'pending')}"><div class="feed-post-head"><span class="feed-source-icon ${kind(item)==='X'?'source-x':''}">${kind(item)==='X'?'𝕏':'↗'}</span><span class="feed-author">${esc(item.author || item.source_id || 'Источник')}<small>${kind(item)} · ${esc(date(item.published_at))}</small></span><button class="btn small feed-star" data-star="${esc(item.id)}" aria-label="Избранное" aria-pressed="${stars.has(item.id)}">${stars.has(item.id) ? '★' : '☆'}</button></div><button class="feed-open" data-open="${esc(item.id)}">${esc(a?.title_ru || item.title || (item.original_text || 'Публикация').slice(0,160))}</button>${a?.summary_ru || item.original_text ? '<p class="feed-excerpt">'+esc(a?.summary_ru || item.original_text.slice(0,360))+'</p>' : ''}${badges(item)}<div class="feed-post-footer"><span class="research-muted">${item.telegram ? esc(a ? 'Разобрано · Важность '+a.impact+'/5' : errors[item.telegram.error_code] || (item.telegram.status==='error' ? 'Ошибка разбора' : 'Ожидает разбора')):'Публикация X'}</span><button class="btn small ${ready?'primary':''}" data-create="${esc(item.id)}">${ready?'+ Создать идею':'Классифицировать →'}</button></div></article>`;
    }).join('') || '<div class="research-card"><h2>Публикаций пока нет</h2><p class="research-muted">Проверьте фильтры и подключённые источники.</p></div>';
    $('feed-more').hidden = !more;
    $('feed-list').querySelectorAll('[data-star]').forEach(button => button.onclick = () => {
      const id = button.dataset.star; stars.has(id) ? stars.delete(id) : stars.add(id);
      try { localStorage.setItem('stv:feed:stars', JSON.stringify([...stars])); } catch { $('feed-status').textContent = 'Избранное сохранено только до закрытия страницы.'; }
      render();
    });
    $('feed-list').querySelectorAll('[data-open]').forEach(button => button.onclick = () => openPost(items.find(i=>i.id===button.dataset.open)));
    $('feed-list').querySelectorAll('[data-create]').forEach(b=>b.onclick=()=>{const item=items.find(i=>i.id===b.dataset.create);openPost(item);if(signal(item).direction&&signal(item).primary_asset_uid)createIdea(item).catch(e=>$('feed-status').textContent=e.message);else{$('feed-detail').scrollIntoView({block:'start',behavior:'smooth'});$('feed-asset-search').focus({preventScroll:true});}});
  }
  function renderTelegram() {
    const t=telegram;
    $('telegram-status').textContent=`За 7 дней: ${t.posts_loaded} постов · разобрано ${t.analyzed} · ожидают ${t.pending} · ошибок ${t.errors}${t.capped ? ' · ограничено 300 последними постами' : ''}`;
    $('feed-health').textContent=`Telegram: ${t.analyzed} разобрано · ${t.pending} в очереди${t.errors?' · '+t.errors+' требуют внимания':''}`;
    const m=t.model;
    $('telegram-budget').textContent=`${t.analysis_enabled ? 'Авторазбор включён' : 'Разбор выключен'} · ${m.configured ? m.provider+' / '+m.model : 'Модель не настроена'} · дайджест ${t.digest_enabled ? 'обновляется при изменениях, не чаще 30 минут' : 'выключен'}`;

    const d=t.digest;
    $('telegram-digest-view').innerHTML=d ? `<details open><summary>Дайджест за 24 часа · ${esc(date(d.created_at))}${d.superseded ? ' · появились изменения, требуется обновление' : ''}</summary><h3>${esc(d.headline_ru)}</h3><p class="research-muted">Период: ${esc(date(d.from_at))} — ${esc(date(d.to_at))} · разобрано ${d.coverage.analyzed} из ${d.coverage.posts_loaded} постов</p><ul>${d.bullets.map(b=>'<li>'+esc(b.text_ru)+' <button class="btn small" data-digest-story="'+esc(b.story_id)+'">Источники</button></li>').join('')}</ul>${d.funds_ru.length ? '<h4>Фонды</h4>'+list(d.funds_ru) : ''}<h4>Что отслеживать</h4>${list(d.watch_ru)}<p class="research-muted">${d.tickers_heat.map(t=>esc(t.symbol)+': '+t.mentions+' упоминаний / '+t.channels+' каналов').join(' · ')}</p></details>` : '<p class="research-muted">Дайджест ещё не собран. Он создаётся из успешно разобранных сюжетов за последние 24 часа.</p>';
    $('telegram-digest-view').querySelectorAll('[data-digest-story]').forEach(b=>b.onclick=()=>{
      const story=t.items.find(s=>s.id===b.dataset.digestStory);
      if(story && !d.superseded)openStory(story);else{
        const saved=d.stories.find(s=>s.story_id===b.dataset.digestStory);
        $('feed-detail').innerHTML='<h2>'+esc(saved?.title || 'Сохранённый сюжет')+'</h2><p>'+esc(saved?.summary || '')+'</p><p class="research-muted">Сюжет изменился или вышел из текущего окна. Версии первоисточников сохранены:</p>'+(d.story_sources?.[b.dataset.digestStory] || d.input_revisions).map(([id,rev])=>'<button class="btn small" data-saved-id="'+esc(id)+'" data-revision="'+esc(rev)+'">Источник '+esc(id.slice(-8))+'</button>').join('');
        $('feed-detail').querySelectorAll('[data-saved-id]').forEach(btn=>btn.onclick=()=>ResearchUI.material(btn.dataset.savedId,null,btn.dataset.revision).catch(e=>$('feed-status').textContent=e.message));
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
      const response = await api('/feed?limit=50' + (last ? `&before=${last.published_at}&before_id=${encodeURIComponent(last.id)}` : ''));
      const previousLast=items.at(-1);
      items = append ? [...items, ...response.data.items] : quiet ? [...response.data.items,...items.filter(i=>!response.data.items.some(n=>n.id===i.id))] : response.data.items;
      if(quiet && previousLast && items.length>50)items.sort((a,b)=>b.published_at-a.published_at || a.id.localeCompare(b.id));
      if(!quiet || !more)more = response.data.has_more;
      const results=await Promise.allSettled([api('/telegram'),api('/config'),api('/channels'),loadIdeas(),api('/feed-classifier')]);
      if(results[4].status==='fulfilled'){const c=results[4].value.data,m=c.model;$('feed-classifier-status').textContent=!c.enabled?'Автоклассификация выключена':!m.configured?'Автоклассификация: укажите ключ xAI в настройках ИИ':'Автоклассификация xAI · '+m.model+' · новые посты обрабатываются в фоне';}
      else $('feed-classifier-status').textContent='Не удалось проверить состояние xAI. Ручная классификация доступна.';
      if(results[0].status==='fulfilled'){telegram=results[0].value.data;renderTelegram();}else{$('telegram-status').textContent='Не удалось загрузить обработку Telegram: '+results[0].reason.message;}
      if(results[1].status==='fulfilled' && results[2].status==='fulfilled'){
        const terminal=results[1].value.data.sources.terminal,channels=results[2].value.data.items;
        $('feed-sources').textContent=`Импорт STVTerminal: ${terminal.last_success_at ? date(terminal.last_success_at) : 'нет успешных обновлений'} · Telegram: ${channels.filter(c=>c.enabled).length} каналов включено из ${channels.length}`;
      }
      if(selected?.type==='post'&&!classificationDirty&&!document.querySelector('dialog[open]')){const item=items.find(i=>i.id===selected.id);if(item&&(!quiet||classificationKey(item)!==selected.classificationKey))openPost(item);}else if(!quiet && selected?.type==='story'){const story=telegram?.items.find(s=>s.id===selected.id);if(story)openStory(story);}render();
      $('feed-status').textContent = 'Лента обновлена. Избранное хранится в этом браузере.';
    } catch (error) { $('feed-status').textContent = error.message || 'Не удалось загрузить ленту.'; }
    finally { busy = false; $('feed-refresh').disabled = $('feed-more').disabled = false;
      clearTimeout(timer);timer=setTimeout(()=>{if(!document.hidden)load(false,true);else timer=setTimeout(()=>load(false,true),30000);},15000);
    }
  }
  $('feed-refresh').onclick = () => load(); $('feed-more').onclick = () => load(true);
  ['feed-kind','feed-stars','feed-view','feed-direction'].forEach(id => $(id).onchange = () => {if(id==='feed-kind' && $('feed-kind').value==='x')$('feed-view').value='posts';render();});
  $('feed-search').oninput = render;
  document.addEventListener('research:ideas-changed',()=>{loadIdeas();$('feed-status').textContent='Идея сохранена. Все черновики и наблюдения доступны в разделе «Идеи».';});


  load();
})();
