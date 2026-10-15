"""External catalysts, separate from price-derived Events facts."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import calendar
import re

from .contracts import (ResearchError, choice, cursor, decode, digest, encode, finite,
                        quote_supported, safe_url, text, timestamp, uid, uncursor)

DAY = 86_400_000
KINDS = {"unlock", "listing", "delisting", "launch", "upgrade", "governance", "tokenomics",
         "security", "regulatory", "macro", "expiry", "other"}
STAGES = {"rumor", "proposal", "announced", "live", "denied", "cancelled"}
PRECISIONS = {"exact", "day", "week", "month", "quarter", "unknown"}


def period(body):
    precision = choice(body.get("precision", "unknown"), PRECISIONS, "precision")
    label = text(body.get("date_label", ""), "date_label", 0, 100)
    tz = body.get("timezone")
    if tz:
        try:
            zone = ZoneInfo(text(tz, "timezone", 1, 100))
        except (ZoneInfoNotFoundError, ValueError):
            raise ResearchError("invalid_timezone", "Неизвестный часовой пояс")
    else:
        zone = None
    if precision == "unknown":
        return {"precision": precision, "date_label": label, "timezone": tz, "starts_at": None, "ends_at": None}
    if precision == "exact":
        starts = (local_exact(label,body["time_label"],tz or "UTC") if body.get("time_label")
                  else timestamp(body.get("starts_at"), "starts_at", optional=False))
        ends = timestamp(starts+1 if body.get("time_label") else body.get("ends_at", starts+1), "ends_at", optional=False)
        if ends <= starts:
            raise ResearchError("invalid_range", "Конец события должен быть после начала")
        return {"precision": precision, "date_label": label, "timezone": tz or "UTC", "starts_at": starts, "ends_at": ends}
    try:
        if precision in {"day", "week"}:
            first = date.fromisoformat(label)
            last = first + timedelta(days=1 if precision == "day" else 7)
        elif precision == "month":
            if not re.fullmatch(r"\d{4}-\d{2}", label):
                raise ValueError()
            year, month = map(int, label.split("-"))
            first = date(year, month, 1)
            last = date(year + (month == 12), month % 12 + 1, 1)
        else:
            match = re.fullmatch(r"(\d{4})-Q([1-4])", label)
            if not match:
                raise ValueError()
            year, quarter = map(int, match.groups())
            first = date(year, (quarter-1)*3+1, 1)
            last = date(year + (quarter == 4), (quarter*3) % 12 + 1, 1)
        if not 2017 <= first.year <= 2100:
            raise ValueError()
    except ValueError:
        raise ResearchError("invalid_date", "Проверьте дату/период: ГГГГ-ММ-ДД, ГГГГ-ММ или ГГГГ-Q1…Q4")
    result = {"precision": precision, "date_label": label, "timezone": tz,
              "starts_at": None, "ends_at": None,
              "period_start_date": first.isoformat(), "period_end_date": last.isoformat()}
    if zone:
        result["starts_at"] = int(datetime.combine(first, datetime.min.time(), zone).timestamp()*1000)
        result["ends_at"] = int(datetime.combine(last, datetime.min.time(), zone).timestamp()*1000)
    return result


def local_exact(date_label, time_label, timezone_name):
    """Reject ambiguous/nonexistent wall time instead of guessing a DST fold."""
    try:
        naive = datetime.fromisoformat(date_label + "T" + time_label)
        zone = ZoneInfo(timezone_name)
        candidates = set()
        for fold in (0, 1):
            aware = naive.replace(tzinfo=zone, fold=fold)
            back = aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
            if back == naive:
                candidates.add(int(aware.timestamp()*1000))
        if len(candidates) != 1:
            raise ValueError()
        return candidates.pop()
    except (ValueError, ZoneInfoNotFoundError):
        raise ResearchError("ambiguous_time", "Местное время отсутствует или неоднозначно; укажите точный UTC-момент")


class Catalysts:
    def __init__(self, store):
        self.s = store

    def canonical(self, identifier, as_of=None):
        seen = set()
        while identifier not in seen:
            seen.add(identifier)
            row = self.s.one("SELECT canonical_id FROM research_catalyst_alias WHERE alias_id=? AND available_at<=?",
                             (identifier, as_of if as_of is not None else self.s.clock()))
            if not row:
                return identifier
            identifier = row["canonical_id"]
        raise ResearchError("alias_cycle", "Повреждена цепочка объединения", 409)

    def get(self, identifier, as_of=None, resolve=True):
        moment = self.s.clock() if as_of is None else as_of
        canonical = self.canonical(identifier, moment) if resolve else identifier
        row = self.s.one("SELECT * FROM research_catalyst_revision WHERE catalyst_id=? AND available_at<=? "
                         "ORDER BY available_at DESC,revision_id DESC LIMIT 1", (canonical, moment))
        if not row:
            raise ResearchError("catalyst_not_found", "Катализатор не найден", 404)
        result = decode(row["payload"])
        if canonical != identifier:
            result["alias_id"] = identifier
        return result

    def validate(self, body):
        result = dict(body)
        result["title"] = text(body.get("title"), "title", 5, 180)
        result["summary"] = text(body.get("summary", ""), "summary", 0, 6000)
        result["kind"] = choice(body.get("kind", "other"), KINDS, "kind")
        result["stage"] = choice(body.get("stage", "announced"), STAGES, "stage")
        result["scope"] = choice(body.get("scope", "asset"), {"asset", "global"}, "scope")
        result["review_status"] = choice(body.get("review_status", "accepted"), {"pending", "accepted", "rejected"}, "review_status")
        result.update(period(body))
        assets = body.get("asset_uids", [])
        if not isinstance(assets, list) or len(assets) > 30:
            raise ResearchError("invalid_assets", "Проверьте связанные активы")
        for asset in assets:
            self.s.require_asset(asset)
        if result["scope"] == "asset" and not assets:
            raise ResearchError("asset_required", "Для события актива выберите подтверждённый актив")
        result["asset_uids"] = sorted(set(assets))
        sources = body.get("sources", [])
        if not isinstance(sources, list) or len(sources) > 50:
            raise ResearchError("invalid_sources", "Слишком много источников")
        checked = []
        for src in sources:
            src = dict(src)
            src["url"] = safe_url(src.get("url"))
            src["text"] = text(src.get("text", ""), "source_text", 0, 4000)
            src["id"] = src.get("id") or digest([src.get("namespace", "manual"), src.get("external_id"), src["url"], src["text"]])
            if src.get("material_revision_id"):
                row = self.s.one("SELECT payload FROM research_material_revision WHERE revision_id=?", (src["material_revision_id"],))
                if not row:
                    raise ResearchError("missing_material", "Не найдена версия исходного материала")
                if src.get("quote") and not quote_supported(src["quote"], decode(row["payload"])["original_text"]):
                    raise ResearchError("unsupported_quote", "Цитата отсутствует в исходном материале")
            checked.append(src)
        result["sources"] = checked
        if result["stage"] in {"live", "denied", "cancelled"} and not checked:
            raise ResearchError("evidence_required", "Укажите свидетельство исполнения, опровержения или отмены")
        if result["stage"] == "live" and body.get("date_status") == "completed" and not checked:
            raise ResearchError("evidence_required", "Исполнение требует свидетельства")
        for field in ("amount", "pct_of_supply"):
            result[field] = finite(body.get(field), field)
        if result.get("pct_of_supply") is not None and not body.get("supply_basis"):
            raise ResearchError("supply_basis_required", "Укажите базу расчёта доли supply")
        if result.get("amount") is not None and not body.get("amount_unit"):
            raise ResearchError("unit_required", "Укажите единицу количества")
        if body.get("date_status") == "completed" and result["stage"] != "live":
            raise ResearchError("completion_stage", "Исполнение должно быть подтверждено стадией «Запущено»")
        result["date_status"] = ("cancelled" if result["stage"] == "cancelled" else "completed" if result["stage"] == "live"
                                else "unscheduled" if result["starts_at"] is None else body.get("date_status", "scheduled"))
        choice(result["date_status"], {"scheduled", "postponed", "unscheduled", "passed_unconfirmed", "completed", "cancelled"}, "date_status")
        # A model's confidence is never a trading direction.
        result["verification"] = body.get("verification", "manual")
        return result

    def save(self, body, identifier=None, expected=None, reason="Создано вручную", actor="owner", new_id=None):
        reason = text(reason, "reason", 3, 1000)
        with self.s.db.batch_writes():
            prior = None
            if identifier:
                if self.s.db.is_postgres:
                    self.s.execute("SELECT id FROM research_catalyst WHERE id=? FOR UPDATE", (identifier,))
                prior = self.get(identifier)
                if expected != prior["revision_id"]:
                    raise ResearchError("revision_conflict", "Событие изменено. Сравните новые данные.", 409)
                identifier = prior["id"]
            value = self.validate(body)
            identifier = identifier or new_id or uid()
            revision = uid()
            stamp = self.s.clock()
            value.update({"id": identifier, "revision_id": revision, "available_at": stamp,
                          "created_at": prior["created_at"] if prior else stamp, "reason": reason, "actor": actor})
            value["priority"], value["priority_reasons"] = self.priority(value)
            self.s.execute("INSERT INTO research_catalyst(id,current_revision_id,created_at,review_status) VALUES(?,?,?,?) "
                           "ON CONFLICT(id) DO UPDATE SET current_revision_id=excluded.current_revision_id,review_status=excluded.review_status",
                           (identifier, revision, value["created_at"], value["review_status"]))
            self.s.execute("INSERT INTO research_catalyst_revision(revision_id,catalyst_id,available_at,kind,scope,stage,precision,"
                           "starts_at,ends_at,review_status,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (revision, identifier, stamp, value["kind"], value["scope"], value["stage"], value["precision"],
                            value["starts_at"], value["ends_at"], value["review_status"], encode(value)))
            for asset_uid in value["asset_uids"]:
                self.s.execute("INSERT INTO research_catalyst_asset(revision_id,asset_uid,relation) VALUES(?,?,?)",
                               (revision, asset_uid, "direct"))
            for source in value["sources"]:
                self.s.execute("INSERT INTO research_catalyst_source(id,catalyst_id,available_at,payload) VALUES(?,?,?,?) "
                               "ON CONFLICT(id) DO NOTHING",
                               (digest([identifier, source["id"]]), identifier, stamp, encode(source)))
            transition = {"revision_id": revision, "previous_revision_id": prior["revision_id"] if prior else None,
                          "action": "updated" if prior else "created", "reason": reason, "actor": actor}
            self.s.execute("INSERT INTO research_catalyst_transition(id,catalyst_id,at_ms,payload) VALUES(?,?,?,?)",
                           (uid(), identifier, stamp, encode(transition)))
            self.s.execute("UPDATE research_reminder SET status='cancelled' WHERE catalyst_id=? AND status='pending'", (identifier,))
            self.s.audit("catalyst", identifier, transition, actor)
        return value

    def priority(self, value):
        from .terminal_bridge import execute as terminal
        native = terminal("calendar",candidate=value,watched=[],existing=[],now=self.s.clock()//60000*60000)
        reasons = []
        if value["kind"] in {"security", "delisting", "regulatory"}:
            reasons.append("Существенное изменение риска")
        if value["review_status"] == "accepted":
            reasons.append("Проверено")
        if value.get("starts_at") and 0 <= value["starts_at"] - self.s.clock() <= 2*DAY:
            reasons.append("В ближайшие двое суток")
        reasons.append("Значимость события: " + str(native["importance"]) + "/3")
        return ("high" if native["importance"]==3 or len(reasons)>=3 else "normal", reasons)

    def similar(self, value):
        from .terminal_bridge import execute as terminal
        existing = self.list(limit=100,review_status="accepted")["items"]
        proposal = terminal("calendar",candidate=value,existing=existing,watched=[],now=self.s.clock()//60000*60000)
        # Existing broad matching is advisory: versions, venues and event rounds
        # remain distinct until the owner confirms an explicit merge.
        return {"candidate_ids":proposal["matches"],"automatic_merge":False,"origin":"stvterminal:af1b0b3"}

    def list(self, *, from_at=None, to_at=None, asset_uid=None, scope=None, kind=None, stage=None,
             review_status=None, unscheduled=False, after=None, limit=50, as_of=None, include_global=False):
        limit = min(max(int(limit), 1), 100)
        if from_at is not None and to_at is not None and (to_at <= from_at or to_at-from_at > 93*DAY):
            raise ResearchError("invalid_range", "Выберите период от 1 до 93 дней", 400)
        filters = [from_at, to_at, asset_uid, scope, kind, stage, review_status, unscheduled, include_global]
        pos = uncursor(after)
        if pos and pos.get("scope") != digest(filters):
            raise ResearchError("cursor_scope", "Изменились фильтры календаря", 400)
        moment = pos["as_of"] if pos else (self.s.clock() if as_of is None else as_of)
        where = ["r.available_at<=?", "NOT EXISTS(SELECT 1 FROM research_catalyst_revision n WHERE n.catalyst_id=r.catalyst_id "
                 "AND n.available_at<=? AND (n.available_at>r.available_at OR (n.available_at=r.available_at AND n.revision_id>r.revision_id)))",
                 "NOT EXISTS(SELECT 1 FROM research_catalyst_alias a WHERE a.alias_id=r.catalyst_id AND a.available_at<=?)"]
        args = [moment, moment, moment]
        for field, value in (("scope", scope), ("kind", kind), ("stage", stage), ("review_status", review_status)):
            if value:
                where.append("r." + field + "=?")
                args.append(value)
        if asset_uid:
            self.s.require_asset(asset_uid)
            where.append("(EXISTS(SELECT 1 FROM research_catalyst_asset a WHERE a.revision_id=r.revision_id AND a.asset_uid=?)" +
                         (" OR r.scope='global')" if include_global else ")"))
            args.append(asset_uid)
        if unscheduled:
            where.append("r.starts_at IS NULL")
        else:
            if from_at is not None:
                where.append("r.ends_at>?")
                args.append(from_at)
            if to_at is not None:
                where.append("r.starts_at<?")
                args.append(to_at)
        if pos:
            where.append("(COALESCE(r.starts_at,4102444800000)>? OR (COALESCE(r.starts_at,4102444800000)=? AND r.catalyst_id>?))")
            args.extend([pos["time"], pos["time"], pos["id"]])
        rows = self.s.rows("SELECT r.* FROM research_catalyst_revision r WHERE " + " AND ".join(where) +
                          " ORDER BY COALESCE(r.starts_at,4102444800000),r.catalyst_id LIMIT ?", (*args, limit+1))
        return {"items": [decode(r["payload"]) for r in rows[:limit]],
                "next_cursor": cursor({"scope": digest(filters), "as_of": moment, "time": rows[limit-1]["starts_at"] or 4102444800000,
                                       "id": rows[limit-1]["catalyst_id"]}) if len(rows)>limit else None, "as_of": moment}

    def history(self, identifier, after=None, limit=50):
        identifier = self.canonical(identifier)
        self.get(identifier)
        limit = min(max(int(limit), 1), 100)
        pos = uncursor(after)
        where, args = "catalyst_id=?", [identifier]
        if pos:
            if pos.get("id") != identifier:
                raise ResearchError("cursor_scope", "Курсор другого события", 400)
            where += " AND (available_at<? OR (available_at=? AND revision_id<?))"
            args.extend([pos["at"], pos["at"], pos["revision"]])
        rows = self.s.rows("SELECT * FROM research_catalyst_revision WHERE " + where +
                          " ORDER BY available_at DESC,revision_id DESC LIMIT ?", (*args, limit+1))
        return {"items": [decode(r["payload"]) for r in rows[:limit]],
                "next_cursor": cursor({"id": identifier, "at": rows[limit-1]["available_at"],
                                       "revision": rows[limit-1]["revision_id"]}) if len(rows)>limit else None}

    def candidate(self, identifier, revision, body, withdrawn=False):
        prior = self.s.one("SELECT * FROM research_catalyst_candidate WHERE id=?", (identifier,))
        if prior and prior["revision_id"] == revision:
            return
        status = "rejected" if withdrawn else "pending"
        value = {**body, "id": identifier, "revision_id": revision, "withdrawn": withdrawn}
        if prior and prior["accepted_catalyst_id"]:
            value["existing_catalyst_id"] = prior["accepted_catalyst_id"]
            old = self.get(prior["accepted_catalyst_id"])
            conflicts = old.get("conflicts", []) + [{"type": "source_updated", "candidate_id": identifier,
                        "candidate_revision": revision, "date_label": body.get("date_label"), "stage": body.get("stage")}]
            self.save({**old, "conflicts": conflicts, "verification": "source_withdrawn" if withdrawn else "needs_review"},
                      old["id"], old["revision_id"], "Изменение источника требует проверки", actor=body.get("provider", "source"))
        self.s.execute("INSERT INTO research_catalyst_candidate(id,revision_id,review_status,accepted_catalyst_id,available_at,payload) "
                       "VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision_id=excluded.revision_id,"
                       "review_status=excluded.review_status,available_at=excluded.available_at,payload=excluded.payload",
                       (identifier, revision, status, prior["accepted_catalyst_id"] if prior else None, self.s.clock(), encode(value)))

    def candidates(self, status="pending", after=None, limit=50):
        pos = uncursor(after)
        limit = min(max(int(limit), 1), 100)
        where, args = "review_status=?", [status]
        if pos:
            if pos.get("status") != status:
                raise ResearchError("cursor_scope", "Изменились фильтры", 400)
            where += " AND (available_at>? OR (available_at=? AND id>?))"
            args.extend([pos["at"], pos["at"], pos["id"]])
        rows = self.s.rows("SELECT * FROM research_catalyst_candidate WHERE " + where + " ORDER BY available_at,id LIMIT ?", (*args, limit+1))
        return {"items": [{**decode(r["payload"]), "review_status": r["review_status"],
                           "accepted_catalyst_id": r["accepted_catalyst_id"]} for r in rows[:limit]],
                "next_cursor": cursor({"status": status, "at": rows[limit-1]["available_at"], "id": rows[limit-1]["id"]})
                if len(rows)>limit else None}

    def review(self, identifier, body):
        with self.s.db.batch_writes():
            if self.s.db.is_postgres:
                self.s.execute("SELECT id FROM research_catalyst_candidate WHERE id=? FOR UPDATE",(identifier,))
            row = self.s.one("SELECT * FROM research_catalyst_candidate WHERE id=?", (identifier,))
            if not row:
                raise ResearchError("candidate_not_found", "Кандидат не найден", 404)
            if row["revision_id"] != body.get("expected_revision"):
                raise ResearchError("revision_conflict", "Кандидат изменён", 409)
            action = choice(body.get("action"), {"accept", "reject", "link_existing"}, "action")
            reason = text(body.get("reason", ""), "reason", 3, 1000)
            original = decode(row["payload"])
            result = {"id": identifier, "review_status": "rejected"}
            catalyst_id = row["accepted_catalyst_id"]
            if action != "reject":
                value = {**original, **body.get("correction", {}), "review_status": "accepted"}
                if original.get("material_revision_id"):
                    value["sources"] = value.get("sources", []) + [{
                        "namespace": original.get("provider", "terminal"), "external_id": original.get("external_id", identifier),
                        "material_revision_id": original["material_revision_id"], "text": original.get("summary", ""),
                        "quote": original.get("quote", ""), "url": original.get("source_url", "")}]
                existing = body.get("catalyst_id") or original.get("existing_catalyst_id")
                if action == "link_existing" and not existing:
                    raise ResearchError("catalyst_required", "Выберите существующее событие")
                if existing:
                    prior = self.get(existing)
                    if prior["revision_id"] != body.get("catalyst_revision"):
                        raise ResearchError("revision_conflict", "Событие изменено", 409)
                    correction=body.get("correction",{}) if action=="accept" else {}
                    value = {**prior, **correction, "sources": prior.get("sources", []) + value.get("sources", []),
                             "conflicts": prior.get("conflicts", []) + ([{"candidate_id": identifier,
                              "date_label": value.get("date_label"), "stage": value.get("stage")}]
                              if value.get("date_label") != prior.get("date_label") or value.get("stage") != prior.get("stage") else [])}
                    result = self.save(value, prior["id"], prior["revision_id"], reason)
                else:
                    result = self.save(value, reason=reason)
                catalyst_id = result["id"]
            self.s.execute("UPDATE research_catalyst_candidate SET review_status=?,accepted_catalyst_id=? WHERE id=?",
                           ("rejected" if action == "reject" else "accepted", catalyst_id, identifier))
            self.s.audit("catalyst_candidate", identifier, {"action": action, "reason": reason, "catalyst_id": catalyst_id})
            return result

    def merge(self, body):
        ids = body.get("ids", [])
        if not isinstance(ids, list) or not 2 <= len(set(ids)) <= 20:
            raise ResearchError("merge_ids", "Выберите от 2 до 20 событий")
        reason = text(body.get("reason"), "reason", 5, 1000)
        with self.s.db.batch_writes():
            if self.s.db.is_postgres:
                for identifier in sorted(set(ids)):
                    self.s.execute("SELECT id FROM research_catalyst WHERE id=? FOR UPDATE",(identifier,))
            items = [self.get(identifier) for identifier in sorted(set(ids))]
            if len({x["id"] for x in items}) != len(items):
                raise ResearchError("already_merged", "События уже объединены", 409)
            for item in items:
                if body.get("expected_revisions", {}).get(item["id"]) != item["revision_id"]:
                    raise ResearchError("revision_conflict", "Одно из событий изменилось", 409)
            canonical = next((x for x in items if x["id"] == body.get("canonical_id")), items[0])
            sources = {source["id"]: source for item in items for source in item["sources"]}
            merged = self.save({**canonical, "sources": list(sources.values()),
                "asset_uids": sorted({a for x in items for a in x["asset_uids"]}),
                "merged_from": [x["id"] for x in items], "conflicts": canonical.get("conflicts", []) +
                [{"type": "merged_assertion", "id": x["id"], "date_label": x["date_label"], "stage": x["stage"]}
                 for x in items if x["id"] != canonical["id"] and (x["date_label"],x["stage"]) != (canonical["date_label"],canonical["stage"])]},
                canonical["id"], canonical["revision_id"], reason)
            for item in items:
                if item["id"] != canonical["id"]:
                    self.s.execute("INSERT INTO research_catalyst_alias(alias_id,canonical_id,available_at) VALUES(?,?,?)",
                                   (item["id"], canonical["id"], self.s.clock()))
                    self.s.execute("UPDATE research_reminder SET status='cancelled' WHERE catalyst_id=? AND status='pending'", (item["id"],))
                    self.s.audit("catalyst", item["id"], {"action": "merged", "canonical_id": canonical["id"], "reason": reason})
            return merged

    def split(self, identifier, body):
        reason = text(body.get("reason"), "reason", 5, 1000)
        groups = body.get("groups", [])
        if not isinstance(groups, list) or not 2 <= len(groups) <= 10:
            raise ResearchError("split_groups", "Укажите от 2 до 10 групп источников")
        with self.s.db.batch_writes():
            prior = self.get(identifier)
            if prior["revision_id"] != body.get("expected_revision"):
                raise ResearchError("revision_conflict", "Событие изменено", 409)
            all_ids = [sid for group in groups for sid in group.get("source_ids", [])]
            sources = {src["id"]: src for src in prior["sources"]}
            if len(set(all_ids)) != len(all_ids) or set(all_ids) != set(sources):
                raise ResearchError("source_partition", "Распределите каждый источник ровно в одну группу")
            results = []
            for group in groups:
                if not group.get("source_ids"):
                    raise ResearchError("empty_group", "Группа должна содержать источник")
                results.append(self.save({**prior, **group.get("correction", {}),
                    "sources": [sources[sid] for sid in group["source_ids"]], "split_from": prior["id"]}, reason=reason))
            self.save({**prior, "review_status": "rejected", "split_into": [x["id"] for x in results]},
                      prior["id"], prior["revision_id"], reason)
            # Existing links still resolve to the preserved split record and its children.
            return {"original_id": prior["id"], "items": results}

    def expire_dates(self):
        rows = self.s.rows("SELECT c.id FROM research_catalyst c JOIN research_catalyst_revision r "
                           "ON r.revision_id=c.current_revision_id WHERE r.ends_at<=? AND c.archived_at IS NULL", (self.s.clock(),))
        for row in rows:
            value = self.get(row["id"])
            if value["date_status"] in {"scheduled", "postponed"} and value["stage"] not in {"live", "denied", "cancelled"}:
                self.save({**value, "date_status": "passed_unconfirmed"}, value["id"], value["revision_id"],
                          "Дата прошла, исполнение не подтверждено", actor="scheduler")
