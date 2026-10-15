"""The original STVTerminal webReader, with our storage and bounded job adapter."""
from __future__ import annotations
import re
from .contracts import ResearchError,decode,digest,encode,text
from .terminal_bridge import execute as terminal
from .store import local_id


class Channels:
    def __init__(self,store):
        self.s=store

    def list(self):
        return [{"handle":r["handle"],"enabled":bool(r["enabled"]),"last_id":r["last_id"],
                 **decode(r["payload"]),"source_state":self.s.state("channel:"+r["handle"])}
                for r in self.s.rows("SELECT * FROM research_channel ORDER BY handle")]

    def save(self,body):
        handle=text(body.get("handle",""),"handle",5,32).removeprefix("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}",handle):
            raise ResearchError("invalid_channel","Укажите имя публичного Telegram-канала без URL")
        handle=handle.lower()
        previous=self.s.one("SELECT * FROM research_channel WHERE handle=?",(handle,))
        value={"title":text(body.get("title",handle),"title",1,150)}
        with self.s.db.batch_writes():
            self.s.execute("INSERT INTO research_channel(handle,enabled,last_id,updated_at,payload) VALUES(?,?,?,?,?) "
                           "ON CONFLICT(handle) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at,payload=excluded.payload",
                           (handle,int(bool(body.get("enabled",False))),previous["last_id"] if previous else None,self.s.clock(),encode(value)))
            self.s.audit("channel",handle,{"action":"configure","enabled":bool(body.get("enabled",False))})
        return {"handle":handle,"enabled":bool(body.get("enabled",False)),**value}

    def collect(self,handle):
        row=self.s.one("SELECT * FROM research_channel WHERE handle=?",(handle,))
        if not row or not row["enabled"]:
            raise ResearchError("channel_disabled","Канал выключен",503)
        try:
            # Re-read a small tail so edits are recorded as material revisions.
            since=max(0,int(row["last_id"])-5) if row["last_id"] else None
            posts=terminal("collect_telegram",handle=handle,since=since)
            assets=self.s.rows("SELECT asset_uid,display_symbol,name FROM research_asset WHERE status='active'")
            added=0;last=int(row["last_id"] or 0)
            with self.s.db.batch_writes():
                for native in posts:
                    p=native["post"]
                    message=int(p.get("messageId",0))
                    original=p.get("text","")
                    if not original:
                        continue
                    refs=[]; ambiguous={}
                    symbol_counts={}
                    for asset in assets:
                        symbol_counts[asset["display_symbol"]]=symbol_counts.get(asset["display_symbol"],0)+1
                    for asset in assets:
                        symbol=asset["display_symbol"]
                        if not re.search(r"(?<![A-Za-z0-9])[$#]?"+re.escape(symbol)+r"(?![A-Za-z0-9])",original,re.I):
                            continue
                        role=terminal("identity",mention={"symbol":symbol},text=original,author=handle)
                        if role["is_asset_mention"]:
                            if symbol_counts[symbol]>1:
                                ambiguous.setdefault(symbol,[]).append(asset["asset_uid"])
                            else:
                                refs.append({"entity_id":asset["asset_uid"],"role":"mentioned"})
                    external="telegram:"+handle+":"+str(message)
                    payload={"source_kind":"telegram","source_id":handle,"author":handle,"canonical_url":"https://t.me/"+handle+"/"+str(message),
                             "original_text":original,"published_at":p.get("postedAt"),"source_observed_at":self.s.clock(),
                             "title":terminal("meaningful_text",text=original)[:160],"assets":refs,
                             "media_refs":[{"kind":photo["kind"],"url":photo["url"]} for photo in native.get("photos",[])]}
                    checksum=digest({k:v for k,v in payload.items() if k!="source_observed_at"})
                    mid=local_id("telegram-local","materials",external)
                    existing=self.s.one("SELECT r.payload FROM research_material_revision r JOIN research_material m "
                                        "ON m.current_revision_id=r.revision_id WHERE m.id=?",(mid,))
                    if existing and decode(existing["payload"]).get("source_content_hash")==checksum:
                        last=max(last,message);continue
                    # Existing local catalog IDs are explicitly mapped; no symbol creates a new asset.
                    for ref in refs:
                        self.s.execute("INSERT INTO research_asset_external_id(namespace,external_id,asset_uid,available_at,payload) "
                                       "VALUES('telegram-local',?,?,?,?) ON CONFLICT(namespace,external_id) DO NOTHING",
                                       (ref["entity_id"],ref["entity_id"],self.s.clock(),encode({"origin":"local-catalog"})))
                    payload["source_content_hash"]=checksum
                    prior=self.s.one("SELECT COUNT(*) AS n FROM research_material_revision WHERE material_id=?",(mid,))["n"]
                    self.s.ingest("telegram-local",{"entity_kind":"materials","entity_id":external,"revision_id":checksum,
                                  "operation":"upsert","change_seq":prior+1,"payload":payload})
                    for symbol,candidates in ambiguous.items():
                        cid=local_id("telegram-local","identity",external+":"+checksum+":"+symbol)
                        candidate={"material_id":mid,"material_revision_id":local_id("telegram-local","materials",external+":"+checksum),"source_content_hash":checksum,
                                   "symbol":symbol,"candidate_asset_uids":candidates,"source_url":payload["canonical_url"],
                                   "reason":"Несколько активов с одинаковым символом"}
                        self.s.execute("INSERT INTO research_identity_candidate VALUES(?,?,'pending',?,?) ON CONFLICT(id) DO NOTHING",
                                       (cid,checksum,self.s.clock(),encode(candidate)))
                    added+=1;last=max(last,message)
                self.s.execute("UPDATE research_channel SET last_id=?,updated_at=? WHERE handle=?",(last,self.s.clock(),handle))
                self.s.set_state("channel:"+handle,{"last_success_at":self.s.clock(),"new_or_edited":added,"error_code":None})
            return {"handle":handle,"new_or_edited":added}
        except ResearchError:
            with self.s.db.batch_writes():
                self.s.set_state("channel:"+handle,{**self.s.state("channel:"+handle),"error_code":"channel_unavailable",
                                                  "last_attempt_at":self.s.clock()})
            raise
