"""Owner-managed X accounts and bounded collection through the X API."""
import os
import re
from datetime import datetime
from urllib.parse import urlsplit

import httpx

from .contracts import ResearchError, decode, digest
from .store import local_id


def normalize_handle(value, platform):
    if not isinstance(value,str):raise ResearchError('invalid_handle','Укажите имя аккаунта или ссылку на профиль')
    value=value.strip()
    if re.match(r'^(https?://)?(www\.)?(t\.me|telegram\.me|x\.com|twitter\.com)/',value,re.I):
        parts=urlsplit(value if '://' in value else 'https://'+value)
        hosts={'telegram':{'t.me','telegram.me'},'x':{'x.com','twitter.com'}}[platform]
        if parts.hostname.removeprefix('www.').lower() not in hosts or len(parts.path.strip('/').split('/'))!=1:
            raise ResearchError('invalid_handle','Нужна ссылка на профиль выбранной сети')
        value=parts.path.strip('/')
    value=value.removeprefix('@')
    pattern=r'[A-Za-z][A-Za-z0-9_]{4,31}' if platform=='telegram' else r'[A-Za-z0-9_]{1,15}'
    if not re.fullmatch(pattern,value):raise ResearchError('invalid_handle','Проверьте имя аккаунта: используйте латиницу, цифры и подчёркивание')
    return value.lower()


class XAccounts:
    def __init__(self,store):self.s=store

    def token(self):
        from .credentials import Credentials
        return Credentials(self.s).get('x',os.environ.get('X_BEARER_TOKEN',''))

    def configured(self):return bool(self.token())

    def list(self):
        return [decode(row['payload']) for row in self.s.rows("SELECT payload FROM research_source_state WHERE id LIKE 'x-account:%' ORDER BY id")]

    def save(self,body):
        handle=normalize_handle(body.get('handle'),'x')
        if not isinstance(body.get('enabled',True),bool):raise ResearchError('invalid_enabled','Некорректное состояние аккаунта')
        with self.s.db.batch_writes():
            value={**self.s.state('x-account:'+handle),'handle':handle,'enabled':body.get('enabled',True)}
            self.s.set_state('x-account:'+handle,value)
            self.s.audit('x-account',handle,{'action':'configure','enabled':value['enabled']})
        return value

    def remove(self,handle):
        handle=normalize_handle(handle,'x')
        with self.s.db.batch_writes():
            self.s.execute('DELETE FROM research_source_state WHERE id=?',('x-account:'+handle,))
            self.s.audit('x-account',handle,{'action':'remove'})
        return {'removed':True}

    def collect(self,handle,client=None):
        handle=normalize_handle(handle,'x');key='x-account:'+handle;state=self.s.state(key)
        if not state.get('enabled'):raise ResearchError('account_disabled','Аккаунт выключен или удалён',503)
        token=self.token()
        if not token:raise ResearchError('x_not_configured','Укажите токен X в настройках импорта',503)
        owns=client is None
        client=client or httpx.Client(base_url='https://api.x.com/2/',headers={'Authorization':'Bearer '+token},timeout=30,trust_env=False)
        try:
            def get(path,**kwargs):
                response=client.get(path,**kwargs)
                if response.status_code==429:raise ResearchError('x_rate_limited','X ограничил частоту запросов. Попробуйте позже.',429)
                if response.status_code in (401,403):raise ResearchError('x_auth_error','Проверьте токен и доступ к чтению X API',503)
                response.raise_for_status();data=response.json()
                if data.get('errors'):raise ResearchError('x_source_error','X не вернул полный ответ. Попробуйте позже.',503)
                return data
            user_id=state.get('user_id') or get('users/by/username/'+handle)['data']['id']
            params={'max_results':100,'tweet.fields':'created_at','exclude':'retweets,replies'}
            if state.get('since_id'):params['since_id']=state['since_id']
            if state.get('pagination_token'):params['pagination_token']=state['pagination_token']
            page=get('users/'+user_id+'/tweets',params=params)
            posts=page.get('data',[]);newest=state.get('pending_newest_id',state.get('since_id','0'));added=0
            with self.s.db.batch_writes():
                # A removed or disabled account must not be recreated by an in-flight request.
                current=self.s.state(key)
                if not current.get('enabled'):return {'skipped':True}
                for post in posts:
                    external='x:'+post['id'];original=post['text']
                    payload={'source_kind':'x','source_id':handle,'author':handle,'canonical_url':'https://x.com/'+handle+'/status/'+post['id'],
                        'original_text':original,'title':original[:160],'published_at':int(datetime.fromisoformat(post['created_at'].replace('Z','+00:00')).timestamp()*1000),
                        'assets':[],'links':[],'media_refs':[]}
                    checksum=digest(payload);mid=local_id('x-local','materials',external)
                    previous=self.s.one('SELECT COUNT(*) AS n FROM research_material_revision WHERE material_id=?',(mid,))['n']
                    if not previous:
                        self.s.ingest('x-local',{'entity_kind':'materials','entity_id':external,'revision_id':checksum,'operation':'upsert','change_seq':1,'payload':payload});added+=1
                    if int(post['id'])>int(newest):newest=post['id']
                next_page=page.get('meta',{}).get('next_token')
                value={**current,'user_id':user_id,'error_code':None,'last_success_at':self.s.clock(),'new_posts':added}
                if next_page:value.update(pagination_token=next_page,pending_newest_id=newest)
                else:
                    value.update(since_id=newest);value.pop('pagination_token',None);value.pop('pending_newest_id',None)
                self.s.set_state(key,value)
            return {'new_posts':added,'has_more':bool(next_page)}
        except (httpx.HTTPError,ValueError,KeyError,TypeError,ResearchError) as error:
            code=getattr(error,'code','x_source_error')
            with self.s.db.batch_writes():
                current=self.s.state(key)
                if current:self.s.set_state(key,{**current,'error_code':code,'last_attempt_at':self.s.clock()})
            if isinstance(error,ResearchError):raise
            raise ResearchError(code,'Не удалось получить публикации X',503) from None
        finally:
            if owns:client.close()
