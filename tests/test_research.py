from __future__ import annotations
from datetime import datetime, timezone
import concurrent.futures
import json
import pytest
import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.models import Instrument
from app.web.api import create_app
from app.research.store import Store
from app.research.contracts import VERSION, ResearchError, digest
from app.research.catalysts import Catalysts, local_exact, period, DAY
from app.research.integration import Integration
from app.research.sources import deribit_rows, collect_deribit
from app.research.jobs import ResearchRunner
from app.research.terminal_bridge import execute as terminal

NOW=int(datetime(2026,10,15,12,tzinfo=timezone.utc).timestamp()*1000)


@pytest.fixture
def research(db):
    s=Store(db,clock=lambda:NOW)
    a=s.create_asset({"display_symbol":"BTC","name":"Bitcoin","external_ids":[{"namespace":"coingecko","external_id":"bitcoin"}]})
    iid=db.upsert_instrument(Instrument(None,"BTC","binance","spot","BTCUSDT","USDT"))
    s.link(iid,{"asset_uid":a["asset_uid"],"reason":"Проверен идентификатор CoinGecko"})
    return s,a["asset_uid"],iid


def event(asset,**changes):
    return {"title":"Обновление тестовой сети","summary":"План опубликован","kind":"upgrade","stage":"announced",
            "scope":"asset","asset_uids":[asset],"precision":"day","date_label":"2026-10-20","timezone":"UTC",**changes}


def item(kind,id,payload,seq=1,operation="upsert"):
    return {"entity_kind":kind,"entity_id":id,"revision_id":digest(payload),"operation":operation,
            "payload":payload,"payload_hash":digest(payload),"change_seq":seq}


def page(items,more=False,next="cursor"):
    return {"schema_version":VERSION,"dataset_id":"dataset","items":items,"next_cursor":next,"has_more":more}


def test_transferred_terminal_algorithms():
    assert terminal("identity",mention={"symbol":"HOOD"},text="",author="")["asset_class"]=="equity"
    assert not terminal("identity",mention={"symbol":"AIXBT"},text="@aixbt: NEAR update",author="aixbt")["is_asset_mention"]
    posts=[{"id":"1","kind":"telegram","postedAt":NOW,"source":"a","title":"a","summary":"","text":"","url":"https://news.test/a?utm_source=x","sentiment":"bullish","tickers":["BTC"],"mediaUrls":[],"narrative":""},
           {"id":"2","kind":"x","postedAt":NOW-1,"source":"b","title":"b","summary":"","text":"","url":"https://news.test/a","sentiment":"bullish","tickers":["BTC"],"mediaUrls":[],"narrative":""}]
    hub=terminal("hub",posts=posts,ticker="BTC",now=NOW)
    assert len(hub["posts"])==1
    assert hub["consensus"]["sources24h"]==1


def test_duplicate_symbols_and_revision_links(research):
    s,asset,iid=research
    second=s.create_asset({"display_symbol":"BTC","name":"Other BTC","external_ids":[{"namespace":"contract","external_id":"chain:other"}]})
    assert len(s.assets("BTC")["items"])==2
    original=s.instrument_link(iid)
    with pytest.raises(ResearchError,match="Связь изменена"):
        s.link(iid,{"asset_uid":second["asset_uid"],"reason":"Уточнение источника"})
    s.clock=lambda:NOW+1
    changed=s.link(iid,{"asset_uid":second["asset_uid"],"reason":"Исправлено после проверки","expected_revision":original["revision_id"]})
    assert s.instrument_link(iid,NOW)["asset_uid"]==asset
    assert s.instrument_link(iid,NOW+1)["asset_uid"]==second["asset_uid"]
    assert changed["revision_id"]!=original["revision_id"]


def test_external_identity_conflict(research):
    s,asset,_=research
    with pytest.raises(ResearchError) as error:
        s.create_asset({"display_symbol":"FAKE","name":"Bad","external_ids":[{"namespace":"coingecko","external_id":"bitcoin"}]})
    assert error.value.status==409


def test_idempotency_and_changed_request(research):
    s,asset,_=research
    c=Catalysts(s);body=event(asset)
    first=s.idempotent("create","key",body,lambda:c.save(body))
    second=s.idempotent("create","key",body,lambda:pytest.fail("must not create"))
    assert first==second
    with pytest.raises(ResearchError) as error:s.idempotent("create","key",{**body,"title":"changed"},lambda:None)
    assert error.value.status==409


@pytest.mark.parametrize("precision,label,end",[("day","2026-10-20","2026-10-21"),("week","2026-10-20","2026-10-27"),
    ("month","2026-10","2026-11-01"),("quarter","2026-Q4","2027-01-01")])
def test_periods(precision,label,end):
    p=period({"precision":precision,"date_label":label,"timezone":"UTC"})
    assert datetime.fromtimestamp(p["ends_at"]/1000,timezone.utc).date().isoformat()==end
    unknown=period({"precision":precision,"date_label":label})
    assert unknown["starts_at"] is None
    assert unknown["ends_at"] is None


@pytest.mark.parametrize("label",["2026-02-30","not-date","2026-13-01"])
def test_invalid_dates(label):
    with pytest.raises(ResearchError):period({"precision":"day","date_label":label})


def test_dst_and_ambiguous_times():
    winter=local_exact("2026-01-15","08:30","America/New_York")
    summer=local_exact("2026-07-15","08:30","America/New_York")
    assert datetime.fromtimestamp(winter/1000,timezone.utc).hour==13
    assert datetime.fromtimestamp(summer/1000,timezone.utc).hour==12
    with pytest.raises(ResearchError):local_exact("2026-03-08","02:30","America/New_York")
    with pytest.raises(ResearchError):local_exact("2026-11-01","01:30","America/New_York")


def test_catalyst_lifecycle_history_and_no_auto_completion(research):
    s,asset,_=research;c=Catalysts(s)
    first=c.save(event(asset,date_label="2026-10-14"))
    c.expire_dates()
    latest=c.get(first["id"])
    assert latest["date_status"]=="passed_unconfirmed"
    assert latest["stage"]=="announced"
    assert len(c.history(first["id"])["items"])==2
    with pytest.raises(ResearchError):c.save({**latest,"stage":"live"},latest["id"],latest["revision_id"],"Запущено")


def test_overlapping_period_is_in_calendar(research):
    s,asset,_=research;c=Catalysts(s)
    quarter=c.save(event(asset,precision="quarter",date_label="2026-Q4"))
    rows=c.list(from_at=NOW,to_at=NOW+DAY)["items"]
    assert [r["id"] for r in rows]==[quarter["id"]]
    with pytest.raises(ResearchError):c.list(from_at=NOW,to_at=NOW+94*DAY)


def test_catalyst_pagination_equal_timestamps_and_stable_slice(research):
    s,asset,_=research;c=Catalysts(s)
    created=[c.save(event(asset,title="Событие "+str(i))) for i in range(4)]
    first=c.list(limit=2)
    s.clock=lambda:NOW+1
    c.save(event(asset,title="Событие новое"))
    second=c.list(limit=2,after=first["next_cursor"])
    assert len({x["id"] for x in first["items"]+second["items"]})==4
    assert {x["id"] for x in created}=={x["id"] for x in first["items"]+second["items"]}


def test_merge_split_history_and_alias(research):
    s,asset,_=research;c=Catalysts(s)
    a=c.save(event(asset,sources=[{"namespace":"a","url":"https://example.com/a","text":"Анонс 1"}]))
    b=c.save(event(asset,sources=[{"namespace":"b","url":"https://example.com/b","text":"Анонс 2"}]))
    merged=c.merge({"ids":[a["id"],b["id"]],"expected_revisions":{a["id"]:a["revision_id"],b["id"]:b["revision_id"]},"reason":"Один первичный анонс"})
    assert c.get(a["id"])["id"]==c.get(b["id"])["id"]
    split=c.split(merged["id"],{"expected_revision":merged["revision_id"],"reason":"Это разные раунды","groups":[
        {"source_ids":[merged["sources"][0]["id"]]},{"source_ids":[merged["sources"][1]["id"]]}]})
    assert len(split["items"])==2
    assert c.get(a["id"])["split_into"]==[x["id"] for x in split["items"]]
    assert len(c.list()["items"])==3  # rejected preserved original + two children, without review filter


def test_source_changes_do_not_override_human_calendar(research):
    s,asset,_=research;c=Catalysts(s)
    c.candidate("candidate","v1",event(asset))
    saved=c.review("candidate",{"expected_revision":"v1","action":"accept","reason":"Проверен анонс"})
    c.candidate("candidate","v2",event(asset,date_label="2026-10-25"))
    latest=c.get(saved["id"])
    assert latest["date_label"]=="2026-10-20"
    assert latest["conflicts"][0]["date_label"]=="2026-10-25"
    assert c.candidates()["items"][0]["revision_id"]=="v2"


def test_import_repeats_quarantine_and_late_material(db):
    s=Store(db,clock=lambda:NOW)
    integration=Integration(s,Settings(research_enabled=True))
    asset=item("assets","ticker:BTC",{"display_symbol":"BTC","name":"Bitcoin"},1)
    material=item("materials","tg:1",{"original_text":"Bitcoin announced launch tomorrow","source_kind":"telegram",
        "published_at":NOW-DAY,"assets":[{"entity_id":"ticker:BTC","role":"subject"}]},2)
    analysis=item("analyses","analysis:1",{"material_id":"tg:1","claims":[
        {"text":"Announcement","quote":"announced launch"},{"text":"invented","quote":"invented quote"}]},3)
    count,bad,state=integration.page(page([asset,material,analysis]),state={})
    assert (count,bad)==(3,0)
    assert integration.page(page([asset,material,analysis]),state=state)[:2]==(0,0)
    m=s.material(s.one("SELECT id FROM research_material")["id"])
    assert m["available_at"]==NOW
    assert [c["verified"] for c in m["analyses"][0]["claims"]]==[True,False]
    with pytest.raises(ResearchError):s.material(m["id"],NOW-1)
    broken=item("analyses","missing",{"material_id":"not-present"},4)
    assert integration.page(page([broken],next="after-broken"),state=state)[:2]==(0,1)
    assert s.state("terminal")["change_cursor"]=="after-broken"
    assert s.one("SELECT COUNT(*) AS n FROM research_quarantine")["n"]==1


def test_material_edits_tombstone_asof_and_filters(db):
    s=Store(db,clock=lambda:NOW);integration=Integration(s,Settings(research_enabled=True))
    integration.page(page([item("assets","btc",{"display_symbol":"BTC","name":"Bitcoin"},1)]),state={})
    asset=s.external_asset("terminal","btc")
    p={"original_text":"original","source_kind":"telegram","published_at":NOW-1,"assets":[{"entity_id":"btc"}]}
    integration.page(page([item("materials","m",p,2)]),state=s.state("terminal"))
    s.clock=lambda:NOW+10
    integration.page(page([item("materials","m",{**p,"original_text":"edited"},3)]),state=s.state("terminal"))
    assert s.materials(asset,as_of=NOW)["items"][0]["revision_id"] != s.materials(asset)["items"][0]["revision_id"]
    assert s.materials(asset,kind="x")["items"]==[]
    s.clock=lambda:NOW+20
    integration.page(page([item("materials","m",{},4,"tombstone")]),state=s.state("terminal"))
    assert s.materials(asset)["items"]==[]
    assert len(s.materials(asset,as_of=NOW)["items"])==1


def test_import_cursor_does_not_advance_when_persistence_fails(db,monkeypatch):
    s=Store(db,clock=lambda:NOW);integration=Integration(s,Settings(research_enabled=True))
    original=s.set_state
    monkeypatch.setattr(s,"set_state",lambda *args: (_ for _ in ()).throw(RuntimeError("crash")))
    with pytest.raises(RuntimeError):integration.page(page([item("assets","btc",{"display_symbol":"BTC","name":"BTC"})]),state={})
    assert s.one("SELECT COUNT(*) AS n FROM research_asset")["n"]==0
    assert s.state("terminal")=={}


def test_public_adapter_deduplicates_contracts_without_merging_currencies():
    rows=[{"kind":"option","expiration_timestamp":NOW+DAY,"instrument_name":"BTC-A"},
          {"kind":"option","expiration_timestamp":NOW+DAY,"instrument_name":"BTC-B"}]
    a=deribit_rows(rows,"BTC",NOW);b=deribit_rows(rows,"ETH",NOW)
    assert len(a)==1 and a[0]["external_id"]!=b[0]["external_id"]
    assert a[0]["sources"][0]["instrument_count"]==2


def test_deribit_repeat_is_idempotent_and_preserves_on_error(research):
    s,_,_=research;settings=Settings(research_enabled=True,research_deribit_enabled=True)
    def handle(request):return httpx.Response(200,json={"result":[{"kind":"option","expiration_timestamp":NOW+DAY,"instrument_name":"A"}]})
    client=httpx.Client(transport=httpx.MockTransport(handle))
    assert collect_deribit(s,settings,client)["added_or_updated"]==2
    assert collect_deribit(s,settings,client)["added_or_updated"]==0
    bad=httpx.Client(transport=httpx.MockTransport(lambda request:httpx.Response(500)))
    with pytest.raises(ResearchError):collect_deribit(s,settings,bad)
    assert s.one("SELECT COUNT(*) AS n FROM research_catalyst")["n"]==2


def test_api_auth_origin_disabled_and_existing_calendar(research):
    s,asset,iid=research
    cfg=Settings(auth_token="test",research_enabled=True)
    client=TestClient(create_app(s.db,cfg))
    assert client.get("/api/research/v1/assets").status_code==401
    headers={"Authorization":"Bearer test"}
    r=client.post("/api/research/v1/catalysts",headers={**headers,"Origin":"https://evil.test"},json=event(asset))
    assert r.status_code==403
    result=client.post("/api/research/v1/catalysts",headers={**headers,"Idempotency-Key":"create"},json=event(asset))
    assert result.status_code==200,result.text
    assert "data" in result.json()
    assert client.get("/api/events/calendar",headers=headers).status_code==200
    assert client.get("/api/research/v1/instruments/99999/context",headers=headers).status_code==404
    off=TestClient(create_app(s.db,Settings(auth_token="test",research_enabled=False)))
    assert off.post("/api/research/v1/catalysts",headers=headers,json=event(asset)).status_code==503


def test_jobs_recover_and_cancel(research):
    s,asset,_=research
    runner=ResearchRunner(s.db,Settings(research_enabled=True))
    runner.s=s;runner.integration.s=s
    a=runner.enqueue("collect")
    assert runner.enqueue("collect")["id"]==a["id"]
    assert runner.cancel(a["id"])["status"]=="cancelled"
    b=runner.enqueue("collect")
    assert b["id"]!=a["id"]


def test_transferred_telegram_parser_and_local_collection(research,monkeypatch):
    from app.research.channels import Channels
    import app.research.channels as module
    s,asset,_=research
    html='<div class="tgme_widget_message_wrap"><div data-post="test_channel/12"><time datetime="2026-10-15T11:00:00Z"></time><div class="tgme_widget_message_text js-message_text">BTC announced an update<br>Original source</div></div></div>'
    parsed=terminal("parse_telegram",html=html,handle="test_channel")
    assert parsed[0]["post"]["messageId"]==12
    original=module.terminal
    monkeypatch.setattr(module,"terminal",lambda action,**kw: parsed if action=="collect_telegram" else original(action,**kw))
    channels=Channels(s)
    channels.save({"handle":"test_channel","enabled":True})
    assert channels.collect("test_channel")["new_or_edited"]==1
    assert channels.collect("test_channel")["new_or_edited"]==0
    assert s.materials(asset)["items"][0]["canonical_url"]=="https://t.me/test_channel/12"
    assert channels.list()[0]["last_id"]==12
    channels.save({"handle":"test_channel","enabled":False})
    with pytest.raises(ResearchError):channels.collect("test_channel")


def test_reminders_use_outbox_and_cancel_when_revision_changes(research):
    from app.research.reminders import Reminders
    from app.notify.outbox import Outbox
    from app.config import DetectorConfig
    s,asset,_=research
    class Sender:
        chat_id="test-owner"
    outbox=Outbox(s.db,Sender(),DetectorConfig())
    reminders=Reminders(s,Settings(research_enabled=True,research_notifications_enabled=True),outbox)
    reminders.subscribe({"asset_uids":[asset],"offsets_minutes":[60]})
    future=Catalysts(s).save(event(asset,precision="exact",date_label="",starts_at=NOW+3_600_000+1000,timezone="UTC"))
    reminders.plan()
    s.clock=lambda:NOW+1001
    reminders.plan()
    packet=s.one("SELECT * FROM notification_packet WHERE channel='research_catalyst'")
    members=[dict(r) for r in outbox.members(packet["id"])]
    assert reminders.validate(packet,members) is None
    assert isinstance(members[0]["event_id"],int)
    changed=Catalysts(s).save({**future,"starts_at":NOW+2*DAY,"ends_at":NOW+2*DAY+1},
                            future["id"],future["revision_id"],"Дата перенесена")
    assert reminders.validate(packet,members) is not None
    assert s.one("SELECT COUNT(*) AS n FROM notification_packet")["n"]==1


def test_ambiguous_local_mention_requires_review_with_history(research,monkeypatch):
    from app.research.channels import Channels
    import app.research.channels as module
    s,asset,_=research
    other=s.create_asset({"display_symbol":"BTC","name":"Other BTC","external_ids":[{"namespace":"contract","external_id":"other:btc"}]})
    original=module.terminal
    posts=[{"post":{"messageId":55,"text":"BTC published an update","postedAt":NOW-1000},"photos":[]}]
    monkeypatch.setattr(module,"terminal",lambda action,**kw: posts if action=="collect_telegram" else original(action,**kw))
    channels=Channels(s);channels.save({"handle":"test_channel","enabled":True});channels.collect("test_channel")
    assert s.materials(asset)["items"]==[]
    assert s.materials(other["asset_uid"])["items"]==[]
    candidate=s.identity_candidates()[0]
    assert len(candidate["candidate_asset_uids"])==2
    s.clock=lambda:NOW+1
    body={"action":"resolve","expected_revision":candidate["revision_id"],"asset_uid":asset,"reason":"Проверен оригинальный контекст"}
    s.resolve_identity(candidate["id"],body)
    assert len(s.materials(asset)["items"])==1
    assert s.materials(asset,as_of=NOW)["items"]==[]
    assert s.materials(other["asset_uid"])["items"]==[]
    assert channels.collect("test_channel")["new_or_edited"]==0
    with pytest.raises(ResearchError):s.resolve_identity(candidate["id"],body)


def test_compact_material_list_batches_latest_analysis(research):
    s,asset,_=research
    p={"source_kind":"telegram","title":"Source","original_text":"Evidence","published_at":NOW-1,
       "assets":[{"entity_id":"btc","role":"subject"}]}
    s.execute("INSERT INTO research_asset_external_id VALUES(?,?,?,?,?)",("test", "btc",asset,NOW,"{}"))
    s.ingest("test",item("materials","m",p))
    s.ingest("test",item("analyses","a1",{"material_id":"m","summary":"Earlier"},1))
    s.ingest("test",item("analyses","a2",{"material_id":"m","summary":"Latest"},2))
    compact=s.materials(asset)["items"][0]
    assert compact["title"]=="Source"
    assert "original_text" not in compact and "analyses" not in compact
    expanded=s.materials(asset,include_analysis=True)["items"][0]
    assert expanded["analyses"][0]["summary"]=="Latest"


def test_research_migration_from_v2_preserves_data_and_repeats(research):
    from app.research.migrations import migrate
    s,asset,_=research
    with s.db.batch_writes():
        s.execute("ALTER TABLE research_analysis DROP COLUMN change_seq")
        s.execute("DELETE FROM research_schema_version WHERE version>=3")
    migrate(s.db)
    migrate(s.db)
    assert s.require_asset(asset)["display_symbol"]=="BTC"
    assert s.one("SELECT MAX(version) AS version FROM research_schema_version")["version"]==5
    assert s.rows("SELECT change_seq FROM research_analysis")==[]

def test_combined_feed_unlinked_posts_pagination_and_latest_revision(research):
    s,_,_=research
    for n,kind in enumerate(['telegram','x','telegram']):
        s.ingest('feed-test',item('materials',str(n),{'source_kind':kind,'original_text':'post '+str(n),'published_at':NOW-1}))
    client=TestClient(create_app(s.db,Settings(auth_token='test',research_enabled=True)))
    headers={'Authorization':'Bearer test'}
    assert client.get('/api/research/v1/feed').status_code==401
    first=client.get('/api/research/v1/feed?limit=2',headers=headers)
    assert first.status_code==200,first.text
    data=first.json()['data']
    assert len(data['items'])==2 and data['has_more']
    last=data['items'][-1]
    second=client.get('/api/research/v1/feed',params={'limit':2,'before':last['published_at'],'before_id':last['id']},headers=headers).json()['data']
    assert len(second['items'])==1 and not second['has_more']
    assert len({r['id'] for r in data['items']+second['items']})==3
    s.ingest('feed-test',item('materials','0',{'source_kind':'telegram','original_text':'edited','published_at':NOW-1},seq=2))
    posts=client.get('/api/research/v1/feed',headers=headers).json()['data']['items']
    assert len(posts)==3
    assert any(p['original_text']=='edited' for p in posts)
