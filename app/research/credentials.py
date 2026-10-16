"""Server-only credentials. API responses and audit events contain status only."""
from .contracts import ResearchError


NAMES={'x','xai','openai','anthropic'}


class Credentials:
    def __init__(self,store):self.s=store

    def check(self,name):
        if name not in NAMES:raise ResearchError('invalid_credential','Неизвестное подключение')

    def get(self,name,fallback=''):
        self.check(name)
        stored=self.s.state('private-credential:'+name)
        return stored.get('value','') if stored else fallback

    def status(self,name,fallback=''):
        self.check(name)
        stored=self.s.state('private-credential:'+name)
        configured=bool(stored.get('value','') if stored else fallback)
        return {'configured':configured,'source':('settings' if stored else 'environment') if configured else 'none'}

    def save(self,name,body):
        self.check(name)
        value=body.get('value')
        if set(body)!={'value'} or not isinstance(value,str) or not 1<=len(value.strip())<=8192 or any(ord(c)<33 or ord(c)>126 for c in value.strip()):
            raise ResearchError('invalid_credential','Введите ключ или токен без пробелов и переносов строк')
        self._write(name,value.strip(),'replace')
        return self.status(name)

    def remove(self,name):
        self.check(name)
        # An empty override also disables an older environment credential.
        self._write(name,'','remove')
        return self.status(name)

    def _write(self,name,value,action):
        with self.s.db.batch_writes():
            self.s.set_state('private-credential:'+name,{'value':value})
            self.s.audit('credential',name,{'action':action})
