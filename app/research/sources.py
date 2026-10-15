"""A verified public adapter: actual listed Deribit option expirations."""
from __future__ import annotations

import httpx
from .contracts import ResearchError, digest, encode
from .catalysts import Catalysts, DAY
from .store import local_id


def deribit_rows(rows, currency, now):
    if not isinstance(rows, list):
        raise ResearchError("schema_error", "Некорректный ответ Deribit")
    expirations = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != "option":
            continue
        expires = row.get("expiration_timestamp")
        if not isinstance(expires, int) or not now < expires <= now+93*DAY:
            continue
        # Product/currency stay in identity; BTC and ETH are not merged.
        expirations.setdefault(expires, []).append(str(row.get("instrument_name", "")))
    return [{"external_id": f"{currency}:option:{expires}", "title": f"Deribit · экспирация опционов {currency}",
             "kind": "expiry", "stage": "announced", "scope": "global", "asset_uids": [],
             "precision": "exact", "starts_at": expires, "ends_at": expires+1, "timezone": "UTC",
             "summary": f"В каталоге Deribit представлены {len(names)} опционных инструментов с этой датой экспирации. "
                        "Это расписание перечисленных контрактов, без оценки объёма и направления цены.",
             "verification": "structured", "review_status": "accepted",
             "sources": [{"namespace": "deribit", "external_id": f"{currency}:option:{expires}",
                          "url": "https://www.deribit.com/api/v2/public/get_instruments?currency=" + currency + "&kind=option",
                          "text": "expiration_timestamp=" + str(expires), "instrument_count": len(names),
                          "sample_instruments": names[:3]}]} for expires,names in sorted(expirations.items())]


def collect_deribit(store, settings, client=None):
    if not settings.research_deribit_enabled:
        raise ResearchError("source_disabled", "Источник Deribit выключен", 503)
    own = client is None
    client = client or httpx.Client(timeout=settings.research_http_timeout_seconds, trust_env=settings.market_trust_env)
    count = 0
    successful_currencies, failed = [], []
    try:
        for currency in ("BTC", "ETH"):
            try:
                response = client.get("https://www.deribit.com/api/v2/public/get_instruments",
                                      params={"currency":currency,"kind":"option","expired":"false"})
                response.raise_for_status()
                payload = response.json()
                if payload.get("error"):
                    raise ResearchError("provider_error", "Deribit вернул ошибку")
                events = deribit_rows(payload.get("result"), currency, store.clock())
                with store.db.batch_writes():
                    for event in events:
                        fingerprint = digest(event)
                        identifier = local_id("deribit", "catalyst", event["external_id"])
                        prior = store.one("SELECT id FROM research_catalyst WHERE id=?", (identifier,))
                        service = Catalysts(store)
                        if prior:
                            old = service.get(identifier)
                            # Human changes are never silently overwritten by a provider.
                            if old.get("actor") != "deribit":
                                continue
                            if old.get("provider_hash") == fingerprint:
                                continue
                            service.save({**event,"provider_hash":fingerprint}, identifier, old["revision_id"], "Обновлён каталог контрактов", actor="deribit")
                        else:
                            value = service.save({**event,"provider_hash":fingerprint}, reason="Подтверждено каталогом Deribit", actor="deribit", new_id=identifier)
                            # External identity is separate from a generated local UUID.
                            store.set_state("deribit:event:" + event["external_id"], {"id": value["id"]})
                        count += 1
                    store.set_state("deribit:" + currency, {"last_success_at":store.clock(),"count":len(events),
                                    "coverage_from":store.clock(),"coverage_to":max([e["ends_at"] for e in events],default=store.clock()),
                                    "coverage_note":"Только фактически перечисленные текущие опционные контракты, максимум 93 дня"})
                successful_currencies.append(currency)
            except (httpx.HTTPError, ValueError, ResearchError) as error:
                failed.append(currency)
                with store.db.batch_writes():
                    previous = store.state("deribit:" + currency)
                    store.set_state("deribit:" + currency, {**previous,"error_code":getattr(error,"code","source_unavailable"),
                                    "last_attempt_at":store.clock()})
    finally:
        if own:
            client.close()
    if not successful_currencies:
        raise ResearchError("source_unavailable", "Каталог Deribit недоступен; сохранённые события оставлены", 503)
    return {"added_or_updated":count,"currencies":successful_currencies,"failed":failed}
