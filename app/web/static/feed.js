'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api = (path, options) => HTF.api('/api/research/v1' + path, options);
  const date = value => value ? new Date(value).toLocaleString('ru-RU', {timeZone:'Europe/Moscow'}) + ' МСК' : '—';
  const link = (url,label) => /^https?:\/\//i.test(url || '') ? `<a class="btn small" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)} ↗</a>` : '';
  const errors = {budget_blocked:'Ожидает доступного бюджета',model_not_configured:'Модель не настроена',model_rate_limited:'Ожидает лимита запросов',terminal_validation:'Ответ модели не прошёл проверку',model_error:'Ошибка провайдера',disabled:'Обработка выключена'};
  let items = [], more = false, busy = false, selected = null, telegram = null, timer = null;
  let stars;
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
    selected = {type:'post',id:item.id};
    const result=item.telegram, a=result?.analysis;
    $('feed-detail').innerHTML = `<span class="research-badge">${kind(item)}</span><h2>${esc(a?.title_ru || item.title || item.author || 'Публикация')}</h2><p class="research-muted">${esc(item.author || item.source_id || 'Источник')} · ${esc(date(item.published_at))}</p>
      ${a ? analysisHTML(a,result) : item.source_kind==='telegram' ? '<p class="research-muted">'+esc(errors[result?.error_code] || (result?.status==='error' ? 'Разбор не выполнен. Можно повторить.' : 'Публикация ожидает ИИ-разбора.'))+'</p>' : ''}
      <details${a ? '' : ' open'}><summary>Оригинальный текст</summary><div class="feed-text">${esc(item.original_text || 'Текст отсутствует.')}</div></details>
      ${link(item.canonical_url,'Открыть оригинал')}<button class="btn small" id="feed-analysis">Разбор и версии</button>${item.source_kind==='telegram' && !a ? '<button class="btn small" id="feed-retry">Разобрать / повторить</button>' : ''}`;
    $('feed-analysis').onclick = () => ResearchUI.material(item.id).catch(error => { $('feed-status').textContent = error.message; });
    if($('feed-retry')) $('feed-retry').onclick=()=>action('/telegram/materials/'+encodeURIComponent(item.id)+'/analyze',$('feed-retry'));
    render();
  }
  function openStory(story) {
    selected={type:'story',id:story.id};
    const best=[...story.posts].sort((a,b)=>b.analysis.impact-a.analysis.impact)[0];
    $('feed-detail').innerHTML=`<span class="research-badge">Сюжет · ${story.posts.length} публикаций · ${story.channels.length} каналов</span><h2>${esc(story.title)}</h2><p class="research-muted">${esc(date(story.firstAt))} — ${esc(date(story.lastAt))}</p>${analysisHTML(best.analysis,best)}<h3>Все первоисточники</h3>${story.posts.map(p=>'<div class="feed-story-source"><strong>'+esc(p.post.channel)+'</strong><p>'+esc(p.analysis.summary_ru)+'</p>'+link(p.canonical_url,'Оригинал')+'<button class="btn small" data-source="'+esc(p.id)+'" data-source-revision="'+esc(p.revision_id)+'">Разбор и версии</button><details><summary>Перевод и оригинал</summary><div class="feed-text">'+esc(p.analysis.translation_ru)+'</div><hr><div class="feed-text">'+esc(p.post.text)+'</div></details></div>').join('')}`;
    $('feed-detail').querySelectorAll('[data-source]').forEach(b=>b.onclick=()=>ResearchUI.material(b.dataset.source,null,b.dataset.sourceRevision).catch(e=>$('feed-status').textContent=e.message));render();
  }
  function render() {
    const q = $('feed-search').value.trim().toLowerCase(), filter = $('feed-kind').value, storyMode=$('feed-view').value==='stories';
    $('telegram-panel').hidden=filter==='x';
    if(storyMode){
      const shown=(telegram?.items || []).filter(s=>filter!=='x' && (!$('feed-stars').checked || s.posts.some(p=>stars.has(p.id))) && [s.title,...s.allTickers,...s.channels,...s.posts.map(p=>p.analysis.summary_ru)].join(' ').toLowerCase().includes(q));
      shown.sort((a,b)=>b.priority.score-a.priority.score || b.lastAt-a.lastAt);
      $('feed-count').textContent=`Сюжетов: ${shown.length} · по важности · последние 7 дней${telegram?.capped ? ' · показаны сюжеты из 300 последних постов' : ''}`;
      $('feed-list').innerHTML=shown.map(s=>`<article class="research-card feed-post"><div class="feed-post-head"><span class="research-badge">${s.channels.length} каналов · ${s.posts.length} постов</span><span>${esc(s.priority.level==='important'?'Важно':s.priority.level==='interesting'?'Интересно':'Сюжет')}</span></div><button class="feed-open" data-story="${esc(s.id)}">${esc(s.title)}</button><p>${esc(s.posts[0].analysis.summary_ru)}</p><p class="research-muted">${s.priority.reasons.map(r=>esc(r.label)).join(' · ')} · ${esc(date(s.lastAt))}</p></article>`).join('') || '<div class="research-card"><h2>Сюжетов пока нет</h2><p class="research-muted">Сюжеты появятся после успешного разбора публикаций. Проверьте фильтры и состояние обработки.</p></div>';
      $('feed-list').querySelectorAll('[data-story]').forEach(b=>b.onclick=()=>openStory(telegram.items.find(s=>s.id===b.dataset.story)));
      $('feed-more').hidden=true;return;
    }
    const shown = items.filter(item => (!filter || (filter === 'x' ? kind(item) === 'X' : kind(item) === 'Telegram')) &&
      (!$('feed-stars').checked || stars.has(item.id)) && [item.title,item.original_text,item.author,item.source_id,item.telegram?.analysis?.translation_ru,item.telegram?.analysis?.title_ru].join(' ').toLowerCase().includes(q));
    $('feed-count').textContent = `Показано ${shown.length} · Загружено ${items.length}`;
    $('feed-list').innerHTML = shown.map(item => {
      const a=item.telegram?.analysis;
      return `<article class="research-card feed-post${selected?.id === item.id ? ' selected' : ''}"><div class="feed-post-head"><span class="research-badge">${kind(item)}</span><span>${esc(item.author || item.source_id || 'Источник')}</span><button class="btn small" data-star="${esc(item.id)}" aria-label="Избранное" aria-pressed="${stars.has(item.id)}">${stars.has(item.id) ? '★' : '☆'}</button></div><button class="feed-open" data-open="${esc(item.id)}">${esc(a?.title_ru || item.title || (item.original_text || 'Публикация').slice(0,160))}</button>${a ? '<p>'+esc(a.summary_ru)+'</p>' : ''}<p class="research-muted">${esc(date(item.published_at))}${item.telegram ? ' · '+esc(a ? 'Разобрано · '+a.impact+'/5' : errors[item.telegram.error_code] || (item.telegram.status==='error' ? 'Ошибка разбора' : 'Ожидает разбора')) : ''}</p></article>`;
    }).join('') || '<div class="research-card"><h2>Публикаций пока нет</h2><p class="research-muted">Проверьте фильтры и подключённые источники.</p></div>';
    $('feed-more').hidden = !more;
    $('feed-list').querySelectorAll('[data-star]').forEach(button => button.onclick = () => {
      const id = button.dataset.star; stars.has(id) ? stars.delete(id) : stars.add(id);
      try { localStorage.setItem('stv:feed:stars', JSON.stringify([...stars])); } catch { $('feed-status').textContent = 'Избранное сохранено только до закрытия страницы.'; }
      render();
    });
    $('feed-list').querySelectorAll('[data-open]').forEach(button => button.onclick = () => openPost(items.find(i=>i.id===button.dataset.open)));
  }
  function renderTelegram() {
    const t=telegram;
    $('telegram-status').textContent=`За 7 дней: ${t.posts_loaded} постов · разобрано ${t.analyzed} · ожидают ${t.pending} · ошибок ${t.errors}${t.capped ? ' · ограничено 300 последними постами' : ''}`;
    const m=t.model;
    $('telegram-budget').textContent=`${t.analysis_enabled ? 'Авторазбор включён' : 'Разбор выключен'} · ${m.configured ? m.provider+' / '+m.model : 'Модель не настроена'} · ${m.automatic_spending_enabled ? 'Остаток общего бюджета $'+m.remaining_usd+' / $'+m.daily_budget_usd+' за 24ч' : 'Бюджет модели не включён'} · дайджест ${t.digest_enabled ? 'обновляется при изменениях, не чаще 30 минут' : 'выключен'}`;

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
      const results=await Promise.allSettled([api('/telegram'),api('/config'),api('/channels')]);
      if(results[0].status==='fulfilled'){telegram=results[0].value.data;renderTelegram();}else{$('telegram-status').textContent='Не удалось загрузить обработку Telegram: '+results[0].reason.message;}
      if(results[1].status==='fulfilled' && results[2].status==='fulfilled'){
        const terminal=results[1].value.data.sources.terminal,channels=results[2].value.data.items;
        $('feed-sources').textContent=`Импорт STVTerminal: ${terminal.last_success_at ? date(terminal.last_success_at) : 'нет успешных обновлений'} · Telegram: ${channels.filter(c=>c.enabled).length} каналов включено из ${channels.length}`;
      }
      if(!quiet && selected?.type==='post'){const item=items.find(i=>i.id===selected.id);if(item)openPost(item);}else if(!quiet && selected?.type==='story'){const story=telegram?.items.find(s=>s.id===selected.id);if(story)openStory(story);}render();
      $('feed-status').textContent = 'Лента обновлена. Избранное хранится в этом браузере.';
    } catch (error) { $('feed-status').textContent = error.message || 'Не удалось загрузить ленту.'; }
    finally { busy = false; $('feed-refresh').disabled = $('feed-more').disabled = false;
      clearTimeout(timer);timer=setTimeout(()=>{if(!document.hidden)load(false,true);else timer=setTimeout(()=>load(false,true),30000);},15000);
    }
  }
  $('feed-refresh').onclick = () => load(); $('feed-more').onclick = () => load(true);
  ['feed-kind','feed-stars','feed-view'].forEach(id => $(id).onchange = () => {if(id==='feed-kind' && $('feed-kind').value==='x')$('feed-view').value='posts';render();});
  $('feed-search').oninput = render;


  load();
})();
