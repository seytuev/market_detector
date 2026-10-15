"""Shared immutable revisions, snapshots and stable pagination for research domains."""
from .contracts import ResearchError,uid,encode,decode,digest,text,cursor,uncursor


class Records:
    def __init__(self, store):
        self.s=store

    def get(self,kind,identifier,revision=None,as_of=None):
        obj=self.s.one("SELECT * FROM research_object WHERE id=? AND kind=?",(identifier,kind))
        if not obj:
            raise ResearchError("not_found","Объект исследования не найден",404)
        if revision:
            row=self.s.one("SELECT * FROM research_revision WHERE object_id=? AND id=?",(identifier,revision))
        else:
            row=self.s.one("SELECT * FROM research_revision WHERE object_id=? AND available_at<=? ORDER BY available_at DESC,id DESC LIMIT 1",
                           (identifier,self.s.clock() if as_of is None else as_of))
        if not row:
            raise ResearchError("revision_not_found","Версия ещё не была доступна",404)
        return decode(row["payload"])

    def save(self,kind,body,identifier=None,expected=None,reason="Создано пользователем",actor="owner",new_id=None):
        reason=text(reason,"reason",3,1000)
        with self.s.db.batch_writes():
            prior=None
            if identifier:
                self.s.one("SELECT id FROM research_object WHERE id=?"+(" FOR UPDATE" if self.s.db.is_postgres else ""),(identifier,))
                prior=self.get(kind,identifier)
                if expected!=prior["revision_id"]:
                    raise ResearchError("revision_conflict","Объект изменён. Черновик сохранён в форме; сравните версии.",409)
            identifier=identifier or new_id or uid(); revision=uid(); stamp=self.s.clock()
            value={**body,"id":identifier,"revision_id":revision,"parent_revision_id":prior["revision_id"] if prior else None,
                   "created_at":prior["created_at"] if prior else stamp,"available_at":stamp,"actor":actor,"reason":reason}
            value["content_hash"]=digest({k:v for k,v in value.items() if k not in {"id","revision_id","parent_revision_id","available_at","actor","reason","content_hash"}})
            self.s.execute("INSERT INTO research_object(id,kind,current_revision_id,status,asset_uid,title,created_at,updated_at,due_at,payload) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET current_revision_id=excluded.current_revision_id,status=excluded.status,"
                "asset_uid=excluded.asset_uid,title=excluded.title,updated_at=excluded.updated_at,due_at=excluded.due_at,payload=excluded.payload",
                (identifier,kind,revision,value.get("status","draft"),value.get("primary_asset_uid"),value.get("title",""),value["created_at"],stamp,value.get("review_due_at"),encode(value)))
            self.s.execute("INSERT INTO research_revision VALUES(?,?,?,?,?)",(revision,identifier,value["parent_revision_id"],stamp,encode(value)))
            self.s.audit(kind,identifier,{"revision_id":revision,"parent_revision_id":value["parent_revision_id"],"reason":reason},actor)
        return value

    def list(self,kind,*,status=None,asset_uid=None,text_query=None,due_before=None,after=None,limit=50):
        limit=min(100,max(1,int(limit))); pos=uncursor(after)
        filters=digest([kind,status,asset_uid,text_query,due_before])
        if pos and pos.get("scope")!=filters: raise ResearchError("cursor_scope","Изменились фильтры",400)
        moment=pos["at"] if pos else self.s.clock()
        where=["o.kind=?","r.available_at<=?","NOT EXISTS(SELECT 1 FROM research_revision n WHERE n.object_id=r.object_id AND n.available_at<=? "
               "AND (n.available_at>r.available_at OR (n.available_at=r.available_at AND n.id>r.id)))"]
        args=[kind,moment,moment]
        for name,value in (("status",status),("primary_asset_uid",asset_uid)):
            if value:
                if name=="status" and kind=="idea" and value=="active":where.append("json_extract(r.payload,'$.status') IN ('watching','supported','disputed')")
                else:where.append("json_extract(r.payload,'$."+name+"')=?");args.append(value)
        if text_query: where.append("lower(r.payload) LIKE ?");args.append("%"+text_query.lower()+"%")
        if due_before is not None: where.append("CAST(json_extract(r.payload,'$.review_due_at') AS BIGINT)<=?");args.append(due_before)
        count=self.s.one("SELECT COUNT(*) AS n FROM research_object o JOIN research_revision r ON r.object_id=o.id WHERE "+" AND ".join(where),args)["n"]
        if pos:
            where.append("(r.available_at<? OR (r.available_at=? AND o.id>?))");args.extend([pos["updated"],pos["updated"],pos["id"]])
        rows=self.s.rows("SELECT o.id,r.available_at,r.payload FROM research_object o JOIN research_revision r ON r.object_id=o.id WHERE "+
                         " AND ".join(where)+" ORDER BY r.available_at DESC,o.id LIMIT ?",(*args,limit+1))
        return {"items":[decode(r["payload"]) for r in rows[:limit]],"total":count,"as_of":moment,
                "next_cursor":cursor({"scope":filters,"at":moment,"updated":rows[limit-1]["available_at"],"id":rows[limit-1]["id"]}) if len(rows)>limit else None}

    def history(self,kind,identifier,after=None,limit=50):
        self.get(kind,identifier); pos=uncursor(after);limit=min(100,max(1,int(limit)))
        args=[identifier]; extra=""
        if pos:
            if pos.get("object")!=identifier: raise ResearchError("cursor_scope","Другая история",400)
            extra=" AND (available_at<? OR (available_at=? AND id<?))";args.extend([pos["at"],pos["at"],pos["id"]])
        rows=self.s.rows("SELECT * FROM research_revision WHERE object_id=?"+extra+" ORDER BY available_at DESC,id DESC LIMIT ?",(*args,limit+1))
        return {"items":[decode(r["payload"]) for r in rows[:limit]],"next_cursor":cursor({"object":identifier,"at":rows[limit-1]["available_at"],"id":rows[limit-1]["id"]}) if len(rows)>limit else None}

    def note(self,object_id,kind,payload,revision=None,key=None):
        key=key or uid(); identifier=digest([object_id,kind,key])
        previous=self.s.one("SELECT payload FROM research_note WHERE object_id=? AND kind=? AND dedup_key=?",(object_id,kind,key))
        if previous:
            if kind!="review_trigger" and previous["payload"]!=encode(payload):raise ResearchError("idempotency_conflict","Ключ записи уже использован с другим содержимым",409)
            return {"id":identifier,**decode(previous["payload"])}
        self.s.execute("INSERT INTO research_note VALUES(?,?,?,?,?,?,?) ON CONFLICT(object_id,kind,dedup_key) DO NOTHING",
                       (identifier,object_id,kind,revision,self.s.clock(),key,encode(payload)))
        return {"id":identifier,**payload}

    def notes(self,object_id,kind=None,after=None,limit=50):
        pos=uncursor(after);limit=min(100,max(1,int(limit)));args=[object_id]; where=["object_id=?"]
        scope=digest([object_id,kind])
        if kind:where.append("kind=?");args.append(kind)
        if pos:
            if pos.get("scope")!=scope:raise ResearchError("cursor_scope","Другая история",400)
            where.append("(available_at<? OR (available_at=? AND id<?))");args.extend([pos["at"],pos["at"],pos["id"]])
        rows=self.s.rows("SELECT * FROM research_note WHERE "+" AND ".join(where)+" ORDER BY available_at DESC,id DESC LIMIT ?",(*args,limit+1))
        return {"items":[{**decode(r["payload"]),"id":r["id"],"available_at":r["available_at"],"note_kind":r["kind"]} for r in rows[:limit]],
            "next_cursor":cursor({"scope":scope,"at":rows[limit-1]["available_at"],"id":rows[limit-1]["id"]}) if len(rows)>limit else None}

    def snapshot(self,kind,payload,cutoff=None):
        checksum=digest(payload); identifier=digest([kind,checksum])
        self.s.execute("INSERT INTO research_dataset VALUES(?,?,?,?,?,?) ON CONFLICT(kind,content_hash) DO NOTHING",
                       (identifier,kind,self.s.clock(),cutoff if cutoff is not None else self.s.clock(),checksum,encode(payload)))
        return {**payload,"id":identifier,"content_hash":checksum}

    def dataset(self,identifier,kind=None):
        row=self.s.one("SELECT * FROM research_dataset WHERE id=?",(identifier,))
        if not row or (kind and row["kind"]!=kind):raise ResearchError("dataset_not_found","Снимок не найден",404)
        data=decode(row["payload"])
        if digest(data)!=row["content_hash"]:raise ResearchError("snapshot_corrupt","Контрольная сумма снимка не совпала",409)
        return {**data,"id":identifier,"content_hash":row["content_hash"]}

    def source(self,ref):
        if not isinstance(ref,dict):raise ResearchError("invalid_reference","Источник должен содержать тип, ID и версию")
        kind=ref.get("kind"); identifier=ref.get("id"); revision=ref.get("revision_id")
        if not revision:raise ResearchError("revision_required","Источник должен ссылаться на конкретную версию")
        if kind=="material":
            row=self.s.one("SELECT * FROM research_material_revision WHERE material_id=? AND revision_id=?",(identifier,revision))
        elif kind=="catalyst":
            row=self.s.one("SELECT * FROM research_catalyst_revision WHERE catalyst_id=? AND revision_id=?",(identifier,revision))
        elif kind=="idea":
            return self.get("idea",identifier,revision)
        else:raise ResearchError("invalid_reference","Неизвестный тип исследовательского источника")
        if not row:raise ResearchError("source_not_found","Версия источника не найдена",422)
        value={**decode(row["payload"]),"available_at":row["available_at"],"revision_id":revision,"id":identifier}
        if kind=="material":
            analysis=self.s.one("SELECT payload FROM research_analysis WHERE material_revision_id=? AND available_at<=? ORDER BY available_at DESC,change_seq DESC,id DESC LIMIT 1",(revision,self.s.clock()))
            value["analyses"]=[decode(analysis["payload"])] if analysis else []
            if analysis:value["available_at"]=max(value["available_at"],value["analyses"][0]["available_at"])
        return value

    def sources(self,refs):
        if not isinstance(refs,list) or len(refs)>30:raise ResearchError("source_limit","Не более 30 источников")
        return [{"ref":ref,"snapshot":self.source(ref)} for ref in refs]
