"""Import the user's API archive into Market Researcher, without a running terminal."""
from __future__ import annotations
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
from zipfile import ZipFile

from app.db import Database
from app.research.contracts import VERSION, digest
from app.research.integration import Integration
from app.research.store import Store
from app.research.terminal_bridge import execute as terminal
from app.config import Settings


def build_items(archive):
    with ZipFile(archive) as z:
        files = {n.split("/")[-1]:n for n in z.namelist() if n.endswith(".json") and "/media/" not in n}
        def read(name,default=None):
            return json.loads(z.read(files[name])) if name in files else default
        manifest = read("manifest.json",{})
        captured = manifest.get("capturedAt")
        captured_at = int(datetime.fromisoformat(captured.replace("Z","+00:00")).timestamp()*1000) if captured else 0
        feed = read("telegram-feed.json",{})
        assets, materials, analyses, candidates = {}, {}, {}, {}
        def remember(symbol,name="",asset_class=None):
            if not isinstance(symbol,str) or not re.fullmatch(r"[A-Z0-9]{1,12}",symbol.upper()):
                return None
            symbol=symbol.upper()
            key="ticker:"+symbol
            if key not in assets:
                classification=terminal("identity",mention={"symbol":symbol},text="",author="")["asset_class"]
                assets[key]={"display_symbol":symbol,"name":name or symbol,"asset_class":asset_class or classification,
                             "external_ids":[]}
            return key
        for story in feed.get("stories",[]):
            refs=[]
            for t in story.get("tickers",[]):
                symbol=t if isinstance(t,str) else t.get("symbol")
                key=remember(symbol, "" if isinstance(t,str) else t.get("name",""),
                             None if isinstance(t,str) else t.get("type") if t.get("type") in {"crypto","equity"} else None)
                if key:
                    refs.append({"entity_id":key,"role":"mentioned"})
            for source in story.get("sources",[]):
                original=source.get("text","")
                if not original:
                    continue
                native=str(source.get("channel",""))+":"+str(source.get("messageId") or digest(source))
                key="telegram:"+native
                valid=[]
                for ref in refs:
                    symbol=ref["entity_id"].split(":",1)[1]
                    check=terminal("identity",mention={"symbol":symbol},text=original,author=source.get("channel",""))
                    if check["is_asset_mention"]:
                        valid.append(ref)
                materials[key]={"source_kind":"telegram","source_id":source.get("channel",""),"author":source.get("channelTitle",""),
                    "canonical_url":source.get("url",""),"original_text":original,"title":story.get("title",""),
                    "published_at":int(source.get("postedAt") or story.get("firstAt") or captured_at),
                    "source_observed_at":captured_at,"assets":valid,"original_language":"unknown","media_refs":[],
                    "origin_material_id":story["id"],"archive_captured_at":captured_at}
                analyses[key]={"material_id":key,"material_revision_id":digest(materials[key]),
                    "summary":story.get("summary",""),"claims":[],"opinions":[{"text":story.get("why",""),"sentiment":story.get("sentiment","neutral")}],
                    "unknowns":story.get("unclear",[])+story.get("checks",[]),"risks":[],"model":source.get("model","stored"),
                    "prompt_version":"archive-preserved","analyzed_at":captured_at}
        for name in ("research-articles.json","research-videos.json"):
            for material in read(name,{}).get("materials",[]):
                full=read("research-full-"+material["id"]+".json",{})
                record=full.get("material",full) if isinstance(full,dict) else {}
                original=record.get("sourceText","")
                if not original:
                    continue
                a=record.get("analysis") or {}
                refs=[]
                for project in a.get("projects",[]):
                    key=remember(project.get("ticker"),project.get("name",""))
                    if key:refs.append({"entity_id":key,"role":"mentioned"})
                key="research:"+material["id"]
                materials[key]={"source_kind":material.get("kind","article"),"source_id":material.get("url",""),
                    "author":record.get("sourceType",""),"canonical_url":material.get("url",""),"original_text":original,
                    "title":material.get("title",""),"published_at":int(material.get("createdAt") or captured_at),
                    "source_observed_at":captured_at,"assets":refs,"original_language":"unknown","media_refs":[],
                    "archive_captured_at":captured_at}
                analyses[key]={"material_id":key,"material_revision_id":digest(materials[key]),"summary":a.get("overview",""),
                    "claims":[{"text":p.get("thesis",p.get("name","")),"quote":p["quote"]} for p in a.get("projects",[]) if p.get("quote")],
                    "opinions":[],"unknowns":a.get("questions",[]),"risks":[],"model":record.get("model") or "stored",
                    "prompt_version":"archive-preserved","analyzed_at":captured_at}
        for event in read("calendar-events.json",{}).get("events",[]):
            asset=remember(event.get("ticker"))
            candidates["calendar:"+event["id"]]={"title":event["title"],"summary":event.get("sourceQuote","")[:6000],
                "kind":"other","stage":"announced","scope":"asset" if asset else "global","asset_ids":[asset] if asset else [],
                "date_label":event.get("eventDate") or event.get("dateText") or "","precision":"day" if event.get("eventDate") else "unknown",
                "timezone":None,"source_observed_at":captured_at,"verification":"needs_review",
                "sources":[{"namespace":"archive","external_id":event["id"],"url":event.get("sourceUrl",""),
                            "text":event.get("sourceQuote","")[:4000]}]}
        items=[]
        for kind,collection in (("assets",assets),("materials",materials),("analyses",analyses),("catalyst_candidates",candidates)):
            for key,payload in sorted(collection.items()):
                items.append({"entity_kind":kind,"entity_id":key,"revision_id":digest(payload),"operation":"upsert",
                              "change_seq":len(items)+1,"payload":payload,"payload_hash":digest(payload)})
        return items,captured_at


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("archive",type=Path)
    parser.add_argument("--database",required=True)
    args=parser.parse_args()
    checksum=hashlib.sha256(args.archive.read_bytes()).hexdigest()
    items,captured=build_items(args.archive)
    db=Database(args.database)
    try:
        s=Store(db);integration=Integration(s,Settings(research_enabled=True))
        state={"dataset_id":"archive:"+checksum,"snapshot_watermark":"archive:complete","source_kind":"archive",
               "archive_captured_at":captured,"last_import_at":s.clock()}
        imported=quarantined=0
        for start in range(0,len(items),100):
            count,bad,state=integration.page({"schema_version":VERSION,"dataset_id":state["dataset_id"],"items":items[start:start+100],
                "next_cursor":"archive:"+str(start+100),"has_more":start+100<len(items),"history_complete":False},state=state,snapshot=True)
            imported+=count;quarantined+=bad
        with db.batch_writes():
            state["last_success_at"]=captured
            state["last_import_at"]=s.clock()
            s.set_state("terminal",state)
        from app.research.channels import Channels
        with ZipFile(args.archive) as z:
            name=next((n for n in z.namelist() if n.endswith("/telegram-sources.json")),None)
            channels=json.loads(z.read(name)).get("sources",[]) if name else []
        for channel in channels:
            handle=str(channel.get("handle","")).removeprefix("@").lower()
            if re.fullmatch(r"[a-z][a-z0-9_]{4,31}",handle) and not s.one("SELECT 1 FROM research_channel WHERE handle=?",(handle,)):
                Channels(s).save({"handle":handle,"title":channel.get("title") or handle,"enabled":bool(channel.get("enabled",False))})
        print(json.dumps({"items":len(items),"imported":imported,"quarantined":quarantined,"archive_captured_at":captured}))
    finally:
        db.close()


if __name__=="__main__":
    main()
