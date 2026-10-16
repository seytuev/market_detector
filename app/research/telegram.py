"""STVTerminal Telegram analysis/stories/digest over the shared research store."""
from __future__ import annotations
import re
from .contracts import ResearchError,decode,encode,digest,uid
from .model import Model
from .terminal_bridge import execute as terminal

DAY=86_400_000
METHOD='stv-telegram-1'


class TelegramFeed:
    def __init__(self,store,settings,model=None):
        self.s,self.settings=store,settings
        self.model=model or Model(store)

    def check(self,digest_mode=False):
        flag=self.settings.research_telegram_digest_enabled if digest_mode else self.settings.research_telegram_analysis_enabled
        if not self.settings.research_enabled or not flag:
            raise ResearchError('disabled','Обработка Telegram выключена в настройках сервера',503)

    def rows(self,days=7,limit=301):
        return self.s.rows("SELECT r.material_id AS id,r.revision_id,r.payload,r.available_at,"
            "COALESCE(r.published_at,r.available_at) AS published_at,a.status AS analysis_status,a.error_code,a.payload AS analysis_payload "
            "FROM research_material m JOIN research_material_revision r ON r.revision_id=m.current_revision_id "
            "LEFT JOIN research_telegram_analysis a ON a.revision_id=r.revision_id "
            "WHERE r.source_kind='telegram' AND r.lifecycle='active' AND r.available_at<=? "
            "AND COALESCE(r.published_at,r.available_at)>=? AND COALESCE(r.published_at,r.available_at)<=? "
            "ORDER BY published_at DESC,m.id LIMIT ?",(self.s.clock(),self.s.clock()-days*DAY,self.s.clock(),limit))

    def attach(self,items):
        if not items:return items
        rows=self.s.rows('SELECT revision_id,status,error_code,payload FROM research_telegram_analysis WHERE revision_id IN ('+
            ','.join('?' for _ in items)+')',tuple(i['revision_id'] for i in items))
        mapping={r['revision_id']:r for r in rows}
        for item in items:
            if item['source_kind']!='telegram':continue
            row=mapping.get(item['revision_id'])
            item['telegram']={'status':row['status'] if row else 'pending','error_code':row['error_code'] if row else None,
                **(decode(row['payload']) if row and row['status']=='succeeded' else {})}
        return items

    def queue(self,runner,material_id,manual=False):
        self.check()
        row=self.s.one("SELECT r.* FROM research_material m JOIN research_material_revision r ON r.revision_id=m.current_revision_id WHERE m.id=?",(material_id,))
        if not row or row['source_kind']!='telegram' or row['lifecycle']!='active':
            raise ResearchError('telegram_not_found','Публикация Telegram не найдена',404)
        revision=row['revision_id'];old=self.s.one('SELECT * FROM research_telegram_analysis WHERE revision_id=?',(revision,))
        if old and (old['status']=='succeeded' or (old['status']=='error' and not manual)):return None
        with self.s.db.batch_writes():
            job=runner.enqueue('telegram_analysis',{'material_id':material_id,'revision_id':revision},key=revision)
            self.s.execute("INSERT INTO research_telegram_analysis VALUES(?,'queued',?,NULL,'{}') ON CONFLICT(revision_id) DO UPDATE SET status='queued',error_code=NULL,updated_at=excluded.updated_at",
                (revision,self.s.clock()))
        return job

    def analyze(self,material_id,revision_id):
        self.check()
        row=self.s.one('SELECT r.*,m.current_revision_id FROM research_material_revision r JOIN research_material m ON m.id=r.material_id WHERE r.material_id=? AND r.revision_id=?',(material_id,revision_id))
        if not row or row['source_kind']!='telegram':raise ResearchError('telegram_not_found','Публикация Telegram не найдена',404)
        if row['current_revision_id']!=revision_id or row['lifecycle']!='active':return {'superseded':True}
        old=self.s.one('SELECT * FROM research_telegram_analysis WHERE revision_id=?',(revision_id,))
        if old and old['status']=='succeeded':return {'cached':True,'revision_id':revision_id}
        source=decode(row['payload']);original=source['original_text']
        post={'channel':str(source.get('source_id') or source.get('author') or 'telegram')[:128],'messageId':0,
            'postedAt':row['published_at'] or row['available_at'],'text':original[:6000],
            'links':[str(u)[:512] for u in (source.get('links') or re.findall(r'https?://[^\s<>]+',original))[:10]], 'hasMedia':bool(source.get('media_refs'))}
        spec=terminal('telegram_prompt',post=post,channel_title=post['channel'])
        try:
            response=self.model.call('telegram_analysis',{'source':spec['user'],'hasMedia':post['hasMedia']},spec=spec,max_output_tokens=6000)
            analysis=terminal('telegram_validate',value=response['output'])
            result={'analysis':analysis,'usage':response['usage'],'usage_id':response['usage_id'],
                'source_revision_id':revision_id,'method_version':METHOD,'text_truncated':len(original)>6000,
                'media_analyzed':False,'links_fetched':False}
            stamp=self.s.clock()
            # The result is attached to the exact source revision, even if it changed during the request.
            with self.s.db.batch_writes():
                self.s.execute("INSERT INTO research_telegram_analysis VALUES(?,'succeeded',?,NULL,?) ON CONFLICT(revision_id) DO UPDATE SET status='succeeded',updated_at=excluded.updated_at,error_code=NULL,payload=excluded.payload",
                    (revision_id,stamp,encode(result)))
                self.s.execute('INSERT INTO research_analysis(id,material_revision_id,available_at,change_seq,payload) VALUES(?,?,?,?,?)',
                    (uid(),revision_id,stamp,1,encode({'summary':analysis['summary_ru'],'claims':[],
                        'opinions':[analysis['why_matters_ru']] if analysis['why_matters_ru'] else [],
                        'risks':analysis['unclear'],'unknowns':analysis['check_ru'],'telegram':result,'available_at':stamp})))
            return {'revision_id':revision_id,'status':'succeeded'}
        except ResearchError as error:
            with self.s.db.batch_writes():
                self.s.execute("INSERT INTO research_telegram_analysis VALUES(?,'error',?,?, '{}') ON CONFLICT(revision_id) DO UPDATE SET status='error',updated_at=excluded.updated_at,error_code=excluded.error_code",
                    (revision_id,self.s.clock(),error.code))
            raise

    def stories(self,days=7):
        rows=self.rows(days=days);posts=[]
        for row in rows[:300]:
            if row['analysis_status']!='succeeded':continue
            source=decode(row['payload']);result=decode(row['analysis_payload']);analysis=result['analysis']
            posts.append({'id':row['id'],'revision_id':row['revision_id'],
                'post':{'channel':source.get('source_id') or source.get('author') or 'telegram',
                    'postedAt':row['published_at'],'text':source['original_text'],
                    'links':source.get('links') or re.findall(r'https?://[^\s<>]+',source['original_text'])[:20]},
                'analysis':analysis,'model':result['usage']['provider'],'starred':False,
                'canonical_url':source.get('canonical_url'),'text_truncated':result.get('text_truncated',False)})
        stories=terminal('telegram_stories',posts=posts,now=self.s.clock()) if posts else []
        return {'items':stories,'window_days':days,'posts_loaded':min(300,len(rows)),'analyzed':len(posts),
            'pending':sum(r['analysis_status'] in (None,'queued') for r in rows[:300]),
            'errors':sum(r['analysis_status']=='error' for r in rows[:300]),'capped':len(rows)>300,
            'method_version':METHOD}

    def digest_input(self):
        now=self.s.clock();stories=self.stories(days=1)
        value={'from':now-DAY,'to':now,'stories':stories['items']}
        spec=terminal('telegram_digest_prompt',value=value)
        bounded=[]
        for story in spec['payload']:
            if len(encode(bounded+[story]))>40000:break
            bounded.append(story)
        spec['payload']=bounded
        # Revision IDs invalidate a digest on edits even when generated summaries stay equal.
        refs=sorted((p['id'],p['revision_id']) for story in stories['items'] for p in story['posts'])
        signature=digest({'method':METHOD,'refs':refs,'payload':spec['payload']})
        return value,spec,signature,stories

    def latest_digest(self):
        row=self.s.one('SELECT * FROM research_telegram_digest ORDER BY created_at DESC,id DESC LIMIT 1')
        if not row:return None
        return {**{k:row[k] for k in ('id','created_at','from_at','to_at')},**decode(row['payload'])}

    def digest(self):
        self.check(True)
        value,spec,signature,coverage=self.digest_input()
        if not spec['payload']:return {'empty':True}
        existing=self.s.one('SELECT id FROM research_telegram_digest WHERE input_hash=?',(signature,))
        if existing:return {'id':existing['id'],'cached':True}
        response=self.model.call('telegram_digest',{'from':value['from'],'to':value['to'],'stories':spec['payload']},spec=spec,max_output_tokens=3500)
        output=terminal('telegram_digest_validate',value=response['output'],story_ids=[s['story_id'] for s in spec['payload']])
        identifier=uid();stamp=self.s.clock()
        payload={**output,'tickers_heat':spec['heat'],'usage':response['usage'],'usage_id':response['usage_id'],
            'stories':spec['payload'],'story_sources':{story['id']:[[p['id'],p['revision_id']] for p in story['posts']] for story in value['stories'] if story['id'] in {v['story_id'] for v in spec['payload']}},'input_revisions':[[p['id'],p['revision_id']] for s in value['stories'] for p in s['posts']],
            'coverage':{**{k:coverage[k] for k in ('posts_loaded','analyzed','pending','errors','capped')},'stories_in_digest':len(spec['payload']),'stories_available':len(value['stories'])},'method_version':METHOD}
        with self.s.db.batch_writes():
            self.s.execute('INSERT INTO research_telegram_digest VALUES(?,?,?,?,?,?) ON CONFLICT(input_hash) DO NOTHING',
                (identifier,signature,stamp,value['from'],value['to'],encode(payload)))
        return {'id':self.s.one('SELECT id FROM research_telegram_digest WHERE input_hash=?',(signature,))['id']}

    def maintenance(self,runner):
        if not self.settings.research_telegram_analysis_enabled:return
        status=self.model.status()
        if not status['configured'] or not status['automatic_spending_enabled']:return
        queued=0
        for row in self.rows():
            if row['analysis_status'] is None:
                self.queue(runner,row['id']);queued+=1
                if queued>=10:break
        if not self.settings.research_telegram_digest_enabled:return
        state=self.s.state('telegram-digest-schedule');now=self.s.clock()
        if now-state.get('last_attempt_at',0)<1800000:return
        if not self.s.one("SELECT revision_id FROM research_telegram_analysis WHERE status='succeeded' LIMIT 1"):return
        _,spec,signature,_=self.digest_input()
        if spec['payload'] and not self.s.one('SELECT id FROM research_telegram_digest WHERE input_hash=?',(signature,)):
            runner.enqueue('telegram_digest',key='scheduled')
            self.s.set_state('telegram-digest-schedule',{'last_attempt_at':now})
