"""Research repositories. Mutations use short, thread-owned transactions."""
from __future__ import annotations

import uuid

from .contracts import (ResearchError, cursor, decode, digest, encode, quote_supported,
                        safe_url, text, choice, timestamp, uid, uncursor)
from ..models import now_ms


def local_id(provider, kind, external_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"research:{provider}:{kind}:{external_id}"))


class Store:
    def __init__(self, db, clock=now_ms):
        self.db, self.clock = db, clock

    def one(self, sql, args=()):
        row = self.db.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def rows(self, sql, args=()):
        return [dict(r) for r in self.db.conn.execute(sql, args).fetchall()]

    def execute(self, sql, args=()):
        return self.db.conn.execute(sql, args)

    def state(self, key):
        row = self.one("SELECT payload FROM research_source_state WHERE id=?", (key,))
        return decode(row["payload"]) if row else {}

    def set_state(self, key, value):
        self.execute("INSERT INTO research_source_state(id,updated_at,payload) VALUES(?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",
                     (key, self.clock(), encode(value)))

    def audit(self, kind, object_id, payload, actor="owner"):
        self.execute("INSERT INTO research_audit(id,object_kind,object_id,actor,at_ms,payload) VALUES(?,?,?,?,?,?)",
                     (uid(), kind, object_id, actor, self.clock(), encode(payload)))

    def envelope(self, data, settings=None, **meta):
        state = self.state("terminal")
        last = state.get("last_success_at")
        enabled = settings is None or settings.research_enabled
        quality = ("disabled" if not enabled else "unavailable" if not last
                   else "stale" if self.clock() - last > 900_000
                   else "partial" if state.get("error_code") or state.get("pending") else "ok")
        return {"data": data, "meta": {"as_of": self.clock(), "generated_at": self.clock(),
                "quality": quality, "reasons": [state["error_code"]] if state.get("error_code") else [],
                "last_success_at": last, "history_complete": state.get("history_complete", False), **meta}}

    def require_asset(self, asset_uid):
        row = self.one("SELECT * FROM research_asset WHERE asset_uid=?", (asset_uid,))
        if not row:
            raise ResearchError("asset_not_found", "Актив не найден", 404)
        return {**decode(row["payload"]), **{k: v for k, v in row.items() if k != "payload"}}

    def asset(self, asset_uid):
        result = self.require_asset(asset_uid)
        result["external_ids"] = [{**decode(r["payload"]), "namespace": r["namespace"], "external_id": r["external_id"]}
                                  for r in self.rows("SELECT * FROM research_asset_external_id WHERE asset_uid=?", (asset_uid,))]
        result["instrument_links"] = [decode(r["payload"]) for r in self.rows(
            "SELECT payload FROM research_instrument_asset WHERE asset_uid=? ORDER BY available_at DESC,revision_id DESC", (asset_uid,))]
        return result

    def assets(self, query="", after=None, limit=100):
        limit = min(max(int(limit), 1), 100)
        args, where = [], []
        if query:
            where.append("(lower(display_symbol) LIKE ? OR lower(name) LIKE ? OR asset_uid IN "
                         "(SELECT asset_uid FROM research_asset_external_id WHERE lower(external_id) LIKE ?))")
            args.extend(["%" + query.lower() + "%"] * 3)
        pos = uncursor(after)
        if pos:
            if pos.get("q") != query:
                raise ResearchError("cursor_scope", "Курсор относится к другому поиску", 400)
            where.append("asset_uid>?")
            args.append(pos["id"])
        rows = self.rows("SELECT asset_uid FROM research_asset " + ("WHERE " + " AND ".join(where) if where else "") +
                         " ORDER BY asset_uid LIMIT ?", (*args, limit + 1))
        more = len(rows) > limit
        rows = rows[:limit]
        return {"items": [self.asset(r["asset_uid"]) for r in rows],
                "next_cursor": cursor({"q": query, "id": rows[-1]["asset_uid"]}) if more else None}

    def create_asset(self, body, asset_uid=None, actor="owner"):
        symbol = text(body.get("display_symbol"), "display_symbol", 1, 30).upper()
        name = text(body.get("name"), "name", 1, 200)
        asset_class = choice(body.get("asset_class", "crypto"), {"crypto", "equity", "index", "cash"}, "asset_class")
        refs = body.get("external_ids", [])
        if not isinstance(refs, list) or len(refs) > 20 or not refs:
            raise ResearchError("identity_required", "Укажите внешний идентификатор актива")
        asset_uid = asset_uid or uid()
        existing = self.one("SELECT * FROM research_asset WHERE asset_uid=?", (asset_uid,))
        for ref in refs:
            namespace = text(ref.get("namespace"), "namespace", 1, 50)
            external = text(ref.get("external_id"), "external_id", 1, 240)
            conflict = self.one("SELECT asset_uid FROM research_asset_external_id WHERE namespace=? AND external_id=?", (namespace, external))
            if conflict and conflict["asset_uid"] != asset_uid:
                raise ResearchError("identity_conflict", "Этот внешний идентификатор принадлежит другому активу", 409)
        payload = {"asset_uid": asset_uid, "asset_class": asset_class, "display_symbol": symbol, "name": name,
                   "status": "active", "created_at": existing["created_at"] if existing else self.clock()}
        with self.db.batch_writes():
            for ref in sorted(refs,key=lambda value:(value["namespace"],value["external_id"])):
                if self.db.is_postgres:
                    self.execute("SELECT pg_advisory_xact_lock(?)",(int(digest([ref["namespace"],ref["external_id"]])[:15],16),))
                conflict=self.one("SELECT asset_uid FROM research_asset_external_id WHERE namespace=? AND external_id=?",(ref["namespace"],ref["external_id"]))
                if conflict and conflict["asset_uid"]!=asset_uid:raise ResearchError("identity_conflict","Этот внешний идентификатор уже связан с другим активом",409)
            self.execute("INSERT INTO research_asset(asset_uid,asset_class,display_symbol,name,status,created_at,payload) "
                         "VALUES(?,?,?,?,?,?,?) ON CONFLICT(asset_uid) DO NOTHING",
                         (asset_uid, asset_class, symbol, name, "active", payload["created_at"], encode(payload)))
            for ref in refs:
                self.execute("INSERT INTO research_asset_external_id(namespace,external_id,asset_uid,available_at,payload) "
                             "VALUES(?,?,?,?,?) ON CONFLICT(namespace,external_id) DO NOTHING",
                             (ref["namespace"], ref["external_id"], asset_uid, self.clock(), encode(ref)))
            self.audit("asset", asset_uid, {"action": "create", "external_ids": refs}, actor)
            if actor != "owner":
                for instrument in self.db.get_instruments():
                    if instrument.asset.upper() != symbol:
                        continue
                    candidate_id = local_id("identity","instrument",str(instrument.id))
                    prior = self.one("SELECT payload,status FROM research_identity_candidate WHERE id=?", (candidate_id,))
                    candidates = decode(prior["payload"]).get("candidate_assets",[]) if prior else []
                    if asset_uid not in candidates:
                        candidates.append(asset_uid)
                    payload = {"instrument_id":instrument.id,"candidate_assets":candidates,
                               "reason":"Совпадает тикер; проверьте внешний идентификатор и рынок"}
                    self.execute("INSERT INTO research_identity_candidate(id,revision_id,status,available_at,payload) VALUES(?,?,'pending',?,?) "
                                 "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision_id=excluded.revision_id",
                                 (candidate_id,uid(),self.clock(),encode(payload)))
        return self.asset(asset_uid)

    def external_asset(self, provider, external_id):
        row = self.one("SELECT asset_uid FROM research_asset_external_id WHERE namespace=? AND external_id=?",
                       (provider, str(external_id)))
        if not row:
            raise ResearchError("unresolved_asset", "Внешний актив ещё не сопоставлен")
        return row["asset_uid"]

    def link(self, instrument_id, body):
        ins = self.db.get_instrument(instrument_id)
        if not ins:
            raise ResearchError("instrument_not_found", "Инструмент не найден", 404)
        asset_uid = body.get("asset_uid")
        if asset_uid:
            self.require_asset(asset_uid)
        reason = text(body.get("reason"), "reason", 5, 1000)
        current = self.instrument_link(instrument_id)
        expected = body.get("expected_revision")
        if expected != (current or {}).get("revision_id"):
            raise ResearchError("revision_conflict", "Связь изменена. Обновите данные.", 409)
        revision = uid()
        moment = self.clock()
        valid_from = timestamp(body.get("valid_from", moment), "valid_from", optional=False, future=False)
        payload = {"revision_id": revision, "instrument_id": instrument_id, "asset_uid": asset_uid,
                   "available_at": moment, "valid_from": valid_from, "resolution": "manual", "evidence": reason,
                   "relation": ins.market_type}
        with self.db.batch_writes():
            if self.db.is_postgres:
                self.execute("SELECT id FROM instrument WHERE id=? FOR UPDATE", (instrument_id,))
            latest = self.instrument_link(instrument_id)
            if (latest or {}).get("revision_id") != expected:
                raise ResearchError("revision_conflict", "Связь изменена", 409)
            self.execute("INSERT INTO research_instrument_asset(revision_id,instrument_id,asset_uid,available_at,valid_from,payload) VALUES(?,?,?,?,?,?)",
                         (revision, instrument_id, asset_uid, moment, valid_from, encode(payload)))
            self.audit("identity", str(instrument_id), {"before": current, "after": payload})
        return payload

    def instrument_link(self, instrument_id, as_of=None):
        moment = self.clock() if as_of is None else as_of
        row = self.one("SELECT payload FROM research_instrument_asset WHERE instrument_id=? AND available_at<=? "
                       "AND valid_from<=? ORDER BY available_at DESC,revision_id DESC LIMIT 1",
                       (instrument_id, moment, moment))
        return decode(row["payload"]) if row else None

    def identity_candidates(self):
        return [{**decode(r["payload"]), "id": r["id"], "revision_id": r["revision_id"], "status": r["status"]}
                for r in self.rows("SELECT * FROM research_identity_candidate WHERE status='pending' ORDER BY available_at,id LIMIT 100")]

    def resolve_identity(self, candidate_id, body):
        row = self.one("SELECT * FROM research_identity_candidate WHERE id=?", (candidate_id,))
        if not row:
            raise ResearchError("candidate_not_found", "Кандидат не найден", 404)
        if row["revision_id"] != body.get("expected_revision"):
            raise ResearchError("revision_conflict", "Кандидат изменён", 409)
        payload = decode(row["payload"])
        action = choice(body.get("action"), {"resolve", "reject"}, "action")
        reason = text(body.get("reason"), "reason", 5, 1000)
        with self.db.batch_writes():
            current=self.one("SELECT * FROM research_identity_candidate WHERE id=?"+
                             (" FOR UPDATE" if self.db.is_postgres else ""),(candidate_id,))
            if current["status"]!="pending" or current["revision_id"]!=body.get("expected_revision"):
                raise ResearchError("revision_conflict","Решение по кандидату уже изменилось",409)
            if action == "resolve":
                asset_uid = body.get("asset_uid")
                self.require_asset(asset_uid)
                if payload.get("instrument_id"):
                    self.link(payload["instrument_id"], {"asset_uid": asset_uid, "reason": reason,
                              "expected_revision": body.get("link_revision")})
                elif payload.get("material_id"):
                    if asset_uid not in payload.get("candidate_asset_uids",[]):
                        raise ResearchError("invalid_identity","Выберите актив из исходных кандидатов")
                    material=self.one("SELECT * FROM research_material WHERE id=?"+
                                      (" FOR UPDATE" if self.db.is_postgres else ""),(payload["material_id"],))
                    source=self.one("SELECT * FROM research_material_revision WHERE revision_id=?",(material["current_revision_id"],))
                    value=decode(source["payload"])
                    if value.get("source_content_hash")!=payload.get("source_content_hash"):
                        raise ResearchError("revision_conflict","Материал обновлён. Проверьте новый вариант упоминания.",409)
                    self.execute("INSERT INTO research_asset_external_id VALUES(?,?,?,?,?) ON CONFLICT(namespace,external_id) DO NOTHING",
                                 (material["provider"],asset_uid,asset_uid,self.clock(),encode({"origin":"owner-review"})))
                    value["assets"]=[*value.get("assets",[]),{"entity_id":asset_uid,"role":"mentioned"}]
                    value["identity_review"]={"candidate_id":candidate_id,"reason":reason}
                    self.ingest(material["provider"],{"entity_kind":"materials","entity_id":material["external_id"],
                        "revision_id":uid(),"operation":"upsert","change_seq":source["change_seq"]+1,"payload":value})
                else:
                    raise ResearchError("unsupported_candidate", "Укажите инструмент для сопоставления")
            revision = uid()
            self.execute("UPDATE research_identity_candidate SET revision_id=?,status=?,payload=? WHERE id=? AND revision_id=?",
                         (revision, "resolved" if action == "resolve" else "rejected",
                          encode({**payload, "decision": body, "decided_at": self.clock()}), candidate_id, row["revision_id"]))
            self.audit("identity_candidate", candidate_id, {"action": action, "reason": reason})
        return {"id": candidate_id, "revision_id": revision}

    def material(self, material_id, as_of=None,revision_id=None):
        moment = self.clock() if as_of is None else as_of
        row = self.one("SELECT r.* FROM research_material_revision r WHERE r.material_id=? AND r.available_at<=? "
                       +("AND r.revision_id=? " if revision_id else "")+"ORDER BY r.available_at DESC,r.change_seq DESC,r.revision_id DESC LIMIT 1", (material_id, moment,revision_id) if revision_id else (material_id,moment))
        if not row:
            raise ResearchError("material_not_found", "Материал не найден", 404)
        result = decode(row["payload"])
        result.update({"id": material_id, "revision_id": row["revision_id"], "available_at": row["available_at"]})
        result["analyses"] = [decode(r["payload"]) for r in self.rows(
            "SELECT payload FROM research_analysis WHERE material_revision_id=? AND available_at<=? ORDER BY available_at DESC,change_seq DESC,id DESC",
            (row["revision_id"], moment))]
        result["versions"] = [{k: r[k] for k in ("revision_id", "available_at", "content_hash")} for r in self.rows(
            "SELECT revision_id,available_at,content_hash FROM research_material_revision WHERE material_id=? AND available_at<=? "
            "ORDER BY available_at DESC,revision_id DESC LIMIT 100", (material_id, moment))]
        return result

    def materials(self, asset_uid, *, as_of=None, from_at=None, to_at=None, kind=None, after=None, limit=50, include_analysis=False):
        self.require_asset(asset_uid)
        limit = min(max(int(limit), 1), 100)
        pos = uncursor(after)
        scope = digest([asset_uid, from_at, to_at, kind])
        if pos and pos.get("scope") != scope:
            raise ResearchError("cursor_scope", "Изменились фильтры списка", 400)
        moment = pos["as_of"] if pos else (self.clock() if as_of is None else as_of)
        args = [asset_uid, moment, moment]
        where = ["a.asset_uid=?", "a.resolution IN ('verified','manual')", "a.role!='beneficiary_hypothesis'",
                 "r.available_at<=?", "NOT EXISTS(SELECT 1 FROM research_material_revision newer WHERE newer.material_id=r.material_id "
                 "AND newer.available_at<=? AND (newer.available_at>r.available_at OR "
                 "(newer.available_at=r.available_at AND newer.change_seq>r.change_seq)))",
                 "r.lifecycle='active'"]
        if from_at is not None:
            where.append("COALESCE(r.published_at,m.first_available_at)>=?")
            args.append(from_at)
        if to_at is not None:
            where.append("COALESCE(r.published_at,m.first_available_at)<?")
            args.append(to_at)
        if pos:
            where.append("(COALESCE(r.published_at,m.first_available_at)<? OR (COALESCE(r.published_at,m.first_available_at)=? AND m.id>?))")
            args.extend([pos["published_at"], pos["published_at"], pos["id"]])
        if kind:
            where.append("r.source_kind=?")
            args.append(kind)
        projections=",".join("json_extract(r.payload,'$."+name+"') AS "+name for name in
                            ("title","source_kind","author","canonical_url","source_id","origin_material_id"))
        rows = self.rows("SELECT DISTINCT m.id,r.revision_id,r.available_at,"+projections+
                         ",COALESCE(r.published_at,m.first_available_at) AS published_at FROM research_material m "
                         "JOIN research_material_revision r ON r.material_id=m.id "
                         "JOIN research_material_asset a ON a.revision_id=r.revision_id WHERE " +
                         " AND ".join(where) + " ORDER BY published_at DESC,m.id LIMIT ?", (*args, limit + 1))
        items=rows[:limit]
        if include_analysis and items:
            placeholders=",".join("?" for _ in items)
            analyses=self.rows("SELECT a.material_revision_id,a.payload FROM research_analysis a WHERE a.material_revision_id IN ("+
                placeholders+") AND a.available_at<=? AND NOT EXISTS(SELECT 1 FROM research_analysis n "
                "WHERE n.material_revision_id=a.material_revision_id AND n.available_at<=? AND (n.available_at>a.available_at "
                "OR (n.available_at=a.available_at AND (n.change_seq>a.change_seq OR (n.change_seq=a.change_seq AND n.id>a.id)))))",
                (*[r["revision_id"] for r in items],moment,moment))
            by_revision={row["material_revision_id"]:decode(row["payload"]) for row in analyses}
            for item in items:
                item["analyses"]=[by_revision[item["revision_id"]]] if item["revision_id"] in by_revision else []
        return {"items": items, "next_cursor": cursor({"scope": scope, "as_of": moment, "id": rows[limit-1]["id"],
                 "published_at": rows[limit-1]["published_at"]}) if len(rows) > limit else None, "as_of": moment}

    def context(self, instrument_id, settings, as_of=None):
        if not self.db.get_instrument(instrument_id):
            raise ResearchError("instrument_not_found", "Инструмент не найден", 404)
        moment = self.clock() if as_of is None else as_of
        link = self.instrument_link(instrument_id, moment)
        if not link or not link.get("asset_uid"):
            return self.envelope({"instrument_id": instrument_id, "identity": "unresolved", "asset_uid": None,
                                  "facts": [], "materials": [], "upcoming": []}, settings, quality="unavailable" if settings.research_enabled else "disabled")
        asset_uid = link["asset_uid"]
        materials = self.materials(asset_uid, as_of=moment, from_at=moment-86_400_000, to_at=moment+1, limit=100, include_analysis=True)["items"]
        from .terminal_bridge import execute as terminal
        symbol = self.require_asset(asset_uid)["display_symbol"]
        posts = [{"id":item["id"],"kind":item["source_kind"],"postedAt":item.get("published_at") or item["available_at"],
                  "source":item.get("source_id") or item.get("author") or item["id"],"title":item.get("title",""),
                  "summary":(item.get("analyses") or [{}])[0].get("summary",""),"text":"","url":item.get("canonical_url",""),
                  "sentiment":next((p.get("sentiment","neutral") for p in (item.get("analyses") or [{}])[0].get("opinions",[])
                                    if isinstance(p,dict)),"neutral"),"tickers":[symbol],"mediaUrls":[],"narrative":""}
                 for item in materials]
        hub = terminal("hub",posts=posts,ticker=symbol,now=moment//60000*60000)
        included = {p["id"] for p in hub["posts"]}
        materials = [item for item in materials if item["id"] in included]
        facts, opinions, risks, checks = [], [], [], []
        sources, origins, revisions = set(), set(), set()
        for item in materials:
            revisions.add(item["revision_id"])
            sources.add(item.get("source_id") or item.get("author") or item["id"])
            origins.add(item.get("origin_material_id") or item.get("canonical_url") or item["id"])
            if item.get("analyses"):
                analysis = item["analyses"][0]
                for claim in analysis.get("claims", []):
                    if claim.get("verified"):
                        facts.append({**claim, "material_id": item["id"], "revision_id": item["revision_id"]})
                opinions.extend(analysis.get("opinions", []))
                risks.extend(analysis.get("risks", []))
                checks.extend(analysis.get("unknowns", []))
        summary_row = self.one("SELECT payload FROM research_summary WHERE asset_uid=? AND available_at<=? "
                               "ORDER BY available_at DESC,id DESC LIMIT 1", (asset_uid, moment))
        summary = decode(summary_row["payload"]) if summary_row else None
        if summary:
            summary["superseded"] = any(ref not in revisions for ref in summary.get("input_revision_ids", []))
        from .catalysts import Catalysts
        upcoming = Catalysts(self).list(from_at=moment, to_at=moment+30*86_400_000,
                  asset_uid=asset_uid, review_status="accepted", as_of=moment, limit=3, include_global=True)["items"]
        data = {"instrument_id": instrument_id, "asset_uid": asset_uid, "identity": "verified", "asset": self.require_asset(asset_uid),
                "facts": facts[:5], "opinions": opinions[:5], "risks": risks[:3], "checks": checks[:3],
                "materials": [{k:item.get(k) for k in ("id","revision_id","title","source_kind","author","published_at","canonical_url")}
                              for item in materials[:5]], "summary": summary, "source_count": len(sources), "story_count": len(origins),
                "upcoming": upcoming, "input_revision_ids": sorted(revisions), "method_version": "research-context-1.0",
                "consensus":hub["consensus"],"algorithm_origin":"stvterminal:af1b0b3"}
        fresh_sources=[self.state("channel:"+source) for source in sources]
        freshest=max([source.get("last_success_at",0) for source in fresh_sources],default=0)
        freshness={"quality":"ok","last_success_at":freshest} if freshest and moment-freshest<900000 else {}
        return self.envelope(data, settings, as_of=moment, data_revision=digest(data),**freshness)

    def overview(self):
        moment=self.clock()
        rows=self.rows("SELECT l.instrument_id,l.asset_uid,COUNT(DISTINCT m.id) AS material_count "
            "FROM research_instrument_asset l "
            "LEFT JOIN research_material_asset a ON a.asset_uid=l.asset_uid AND a.resolution IN ('verified','manual') AND a.role!='beneficiary_hypothesis' "
            "LEFT JOIN research_material_revision r ON r.revision_id=a.revision_id AND r.lifecycle='active' AND r.published_at>=? "
            "LEFT JOIN research_material m ON m.id=r.material_id AND m.current_revision_id=r.revision_id "
            "WHERE l.available_at<=? AND l.valid_from<=? AND NOT EXISTS(SELECT 1 FROM research_instrument_asset n "
            "WHERE n.instrument_id=l.instrument_id AND n.available_at<=? AND n.valid_from<=? "
            "AND (n.available_at>l.available_at OR (n.available_at=l.available_at AND n.revision_id>l.revision_id))) "
            "GROUP BY l.instrument_id,l.asset_uid ORDER BY l.instrument_id LIMIT 200",
            (moment-86_400_000,moment,moment,moment,moment))
        return {"items":rows,"as_of":moment}

    def idempotent(self, operation, key, body, fn):
        if key is not None:
            text(key, "Idempotency-Key", 1, 200)
        with self.db.batch_writes():
            if key and self.db.is_postgres:
                lock = int(digest([operation, key])[:15], 16)
                self.execute("SELECT pg_advisory_xact_lock(?)", (lock,))
            prior = self.one("SELECT * FROM research_idempotency WHERE operation=? AND key=?", (operation, key)) if key else None
            if prior:
                if prior["body_hash"] != digest(body):
                    raise ResearchError("idempotency_conflict", "Ключ уже использован для другого запроса", 409)
                return decode(prior["result"])
            result = fn()
            if key:
                self.execute("INSERT INTO research_idempotency(operation,key,body_hash,result,created_at) VALUES(?,?,?,?,?)",
                             (operation, key, digest(body), encode(result), self.clock()))
            return result

    def ingest(self, provider, item):
        """Called within page transaction; errors are quarantined by its caller."""
        kind, external, revision, p = item["entity_kind"], item["entity_id"], item["revision_id"], item["payload"]
        identifier = local_id(provider, kind, external)
        version = local_id(provider, kind, external + ":" + revision)
        stamp = self.clock()
        deleted = item["operation"] == "tombstone"
        if kind == "assets":
            if deleted:
                self.execute("UPDATE research_asset SET status='archived' WHERE asset_uid=?", (identifier,))
                return
            refs = [{"namespace": provider, "external_id": external}] + p.get("external_ids", [])
            self.create_asset({**p, "external_ids": refs}, identifier, actor=provider)
        elif kind == "materials":
            previous = self.one("SELECT * FROM research_material WHERE id=?", (identifier,))
            original = text(p.get("original_text", ""), "original_text", 0 if deleted else 1, 120000)
            published = timestamp(p.get("published_at"), "published_at", future=False)
            timestamp(p.get("source_observed_at"), "source_observed_at", future=False)
            payload = {**p, "canonical_url": safe_url(p.get("canonical_url")), "original_text": original,
                       "lifecycle": "deleted" if deleted else p.get("lifecycle", "active"),
                       "source_kind": choice(p.get("source_kind", "manual"), {"telegram", "x", "article", "video", "manual"}, "source_kind"),
                       "published_at": published, "late_import": bool(published and stamp-published>86_400_000)}
            self.execute("INSERT INTO research_material(id,provider,external_id,current_revision_id,lifecycle,published_at,first_available_at) "
                         "VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET current_revision_id=excluded.current_revision_id,"
                         "lifecycle=excluded.lifecycle,published_at=excluded.published_at",
                         (identifier, provider, external, version, payload["lifecycle"], published, previous["first_available_at"] if previous else stamp))
            self.execute("INSERT INTO research_material_revision(revision_id,material_id,available_at,change_seq,published_at,source_kind,lifecycle,content_hash,payload) "
                         "VALUES(?,?,?,?,?,?,?,?,?)", (version, identifier, stamp, item["change_seq"], published,
                         payload["source_kind"], payload["lifecycle"], digest(payload), encode(payload)))
            for ref in p.get("assets", []):
                asset_uid = self.external_asset(provider, ref["entity_id"])
                role = choice(ref.get("role", "mentioned"), {"subject", "mentioned", "beneficiary_hypothesis"}, "role")
                self.execute("INSERT INTO research_material_asset(revision_id,asset_uid,role,resolution,payload) VALUES(?,?,?,?,?)",
                             (version, asset_uid, role, "verified", encode(ref)))
                story_key = p.get("origin_material_id") or payload.get("canonical_url") or external
                story = local_id(provider, "story", asset_uid + ":" + story_key)
                self.execute("INSERT INTO research_story(id,asset_uid,topic_key,stage,available_at,payload) VALUES(?,?,?,?,?,?) "
                             "ON CONFLICT(id) DO UPDATE SET stage=excluded.stage,payload=excluded.payload",
                             (story, asset_uid, story_key, p.get("stage"), stamp, encode({"origin": story_key})))
                self.execute("INSERT OR IGNORE INTO research_story_member(story_id,revision_id) VALUES(?,?)", (story, version))
        elif kind == "analyses":
            if deleted:
                # A retraction is a new analysis version, never deletion of historical evidence.
                p = {**p, "claims": [], "summary": "", "retracted": True}
            material_id = local_id(provider, "materials", p.get("material_id", ""))
            material = self.one("SELECT * FROM research_material WHERE id=?", (material_id,))
            if not material:
                raise ResearchError("missing_material", "Материал анализа ещё не загружен")
            material_revision = (local_id(provider, "materials", p["material_id"] + ":" + p["material_revision_id"])
                                 if p.get("material_revision_id") else material["current_revision_id"])
            source = self.one("SELECT payload FROM research_material_revision WHERE revision_id=?", (material_revision,))
            if not source:
                raise ResearchError("missing_revision", "Версия материала ещё не загружена")
            original = decode(source["payload"])["original_text"]
            claims = []
            for claim in p.get("claims", [])[:30]:
                quote = text(claim.get("quote", ""), "quote", 0, 2000)
                claims.append({**claim, "text": text(claim.get("text", ""), "claim", 1, 3000),
                               "quote": quote, "verified": quote_supported(quote, original)})
            payload = {**p, "id": version, "claims": claims, "material_revision_id": material_revision,
                       "available_at": stamp, "validation_reasons": ["unsupported_claims"] if any(not c["verified"] for c in claims) else []}
            self.execute("INSERT INTO research_analysis(id,material_revision_id,available_at,change_seq,payload) VALUES(?,?,?,?,?)",
                         (version, material_revision, stamp, item["change_seq"], encode(payload)))
        elif kind == "catalyst_candidates":
            from .catalysts import Catalysts
            assets = [self.external_asset(provider, ref) for ref in p.get("asset_ids", [])]
            p = {**p, "asset_uids": assets, "provider": provider, "external_id": external}
            if p.get("material_id"):
                material = self.one("SELECT current_revision_id FROM research_material WHERE id=?",
                                    (local_id(provider, "materials", p["material_id"]),))
                if not material:
                    raise ResearchError("missing_material", "Материал катализатора ещё не загружен")
                p["material_revision_id"] = material["current_revision_id"]
            Catalysts(self).candidate(identifier, version, p, withdrawn=deleted)
        elif kind == "summaries":
            asset_uid = self.external_asset(provider, p["asset_id"])
            refs = [local_id(provider, "materials", ref["entity_id"] + ":" + ref["revision_id"]) for ref in p.get("source_refs", [])]
            if not refs or any(not self.one("SELECT 1 FROM research_material_revision WHERE revision_id=?", (ref,)) for ref in refs):
                raise ResearchError("missing_summary_sources", "Не подтверждены источники сводки")
            from .terminal_bridge import execute as terminal
            validated=terminal("summary",value=p)
            allowed={str(ref["entity_id"]) for ref in p["source_refs"]}
            validated["facts"]=[f for f in validated["facts"] if set(f["sourceIds"]).issubset(allowed)]
            payload = {**validated,"input_revision_ids": refs, "retracted": deleted}
            self.execute("INSERT INTO research_summary(id,asset_uid,available_at,payload) VALUES(?,?,?,?)",
                         (version, asset_uid, stamp, encode(payload)))
