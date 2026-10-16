"""Shared provider cooldowns derived from real HTTP 429s, without hourly quotas."""
import math
from email.utils import parsedate_to_datetime
from .contracts import ResearchError, digest


class ProviderCooldown:
    def __init__(self, store, config, model=None):
        self.s = store
        self.key = 'model-cooldown:' + digest([config['provider'], model or config.get('cheap'),
                                               digest(config.get('key', ''))])

    def status(self):
        state = self.s.state(self.key)
        until = state.get('until', 0)
        return {'source': 'provider', 'retry_at': until if until > self.s.clock() else None,
                'retry_after': max(0, math.ceil((until - self.s.clock()) / 1000))}

    def error(self, seconds):
        error = ResearchError('model_rate_limited', 'xAI временно ограничил запросы. Повтор запланирован автоматически.', 429)
        error.retry_after = seconds
        error.http_status = 429
        error.rate_limit_source = 'provider'
        return error

    def check(self):
        seconds = self.status()['retry_after']
        if seconds:
            raise self.error(seconds)

    def limited(self, response):
        now = self.s.clock()
        raw = response.headers.get('retry-after', '')
        seconds = None
        try:
            seconds = float(raw)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(raw).timestamp() - now / 1000
            except (ValueError, TypeError, OverflowError):
                pass
        with self.s.db.batch_writes():
            if self.s.db.is_postgres:
                self.s.execute('SELECT pg_advisory_xact_lock(?)', (int(digest(self.key)[:15], 16),))
            prior = self.s.state(self.key)
            strikes = min(6, prior.get('strikes', 0) + 1) if now - prior.get('last_429', 0) < 300000 else 1
            if seconds is None or not math.isfinite(seconds) or seconds < 0:
                seconds = min(60, 2 ** strikes)
            until = max(prior.get('until', 0), now + max(1, math.ceil(seconds)) * 1000)
            self.s.set_state(self.key, {'until': until, 'last_429': now, 'strikes': strikes})
        return self.error(max(1, math.ceil((until - now) / 1000)))

    def success(self):
        with self.s.db.batch_writes():
            if self.s.db.is_postgres:
                self.s.execute('SELECT pg_advisory_xact_lock(?)', (int(digest(self.key)[:15], 16),))
            prior = self.s.state(self.key)
            if prior and prior.get('until', 0) <= self.s.clock():
                self.s.set_state(self.key, {'until': 0, 'strikes': 0})


class CooldownClient:
    def __init__(self, client, cooldown):
        self.client, self.cooldown = client, cooldown

    def post(self, *args, **kwargs):
        response = self.client.post(*args, **kwargs)
        if response.status_code == 429:
            raise self.cooldown.limited(response)
        if response.is_success:
            self.cooldown.success()
        return response

    def close(self):
        self.client.close()
