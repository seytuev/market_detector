"""Durable page import; the remote cursor advances with its rows and quarantine."""
from __future__ import annotations

from urllib.parse import urlsplit
import httpx

from .contracts import ResearchError, VERSION, decode, digest, encode, uid, validate_item
from .store import local_id


def endpoint(value):
    try:
        parts = urlsplit(value)
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError()
        if parts.scheme != "https" and not (parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1", "::1"}):
            raise ValueError()
    except ValueError:
        raise ResearchError("invalid_endpoint", "Адрес терминала: HTTPS или локальный HTTP")
    return value.rstrip("/") + "/api/integration/v1"


class Integration:
    def __init__(self, store, settings, client=None):
        self.s, self.settings, self.client = store, settings, client

    def status(self):
        return {**self.s.state("terminal"),
                "configured": bool(self.settings.research_terminal_url and self.settings.research_integration_token),
                "enabled": self.settings.research_enabled,
                "quarantine_count": self.s.one("SELECT COUNT(*) AS n FROM research_quarantine WHERE status='pending'")["n"],
                "pending_jobs": self.s.one("SELECT COUNT(*) AS n FROM research_job WHERE status IN ('pending','running')")["n"]}

    def page(self, envelope, *, state, snapshot=False):
        if not isinstance(envelope, dict) or envelope.get("schema_version") != VERSION:
            raise ResearchError("schema_error", "Неподдерживаемая версия экспорта", 503)
        dataset = envelope.get("dataset_id")
        if not isinstance(dataset, str) or not dataset or not isinstance(envelope.get("items"), list):
            raise ResearchError("schema_error", "Некорректная страница экспорта", 503)
        if len(envelope["items"]) > 500:
            raise ResearchError("schema_error", "Слишком большая страница экспорта", 503)
        if state.get("dataset_id") and state["dataset_id"] != dataset:
            raise ResearchError("dataset_changed", "Изменился набор данных терминала", 410)
        if not envelope.get("next_cursor"):
            raise ResearchError("schema_error", "Отсутствует курсор страницы", 503)
        previous_seq = -1
        count = quarantined = 0
        with self.s.db.batch_writes():
            for raw in envelope["items"]:
                try:
                    with self.s.db.batch_writes():
                        item = validate_item(raw)
                        if not snapshot and item["change_seq"] <= previous_seq:
                            raise ResearchError("sequence_error", "Нарушен порядок изменений")
                        previous_seq = item["change_seq"]
                        prior = self.s.one("SELECT MAX(change_seq) AS seq FROM research_import_receipt "
                            "WHERE provider='terminal' AND dataset_id=? AND entity_kind=? AND entity_id=?",
                            (dataset, item["entity_kind"], item["entity_id"]))
                        receipt = self.s.one("SELECT 1 FROM research_import_receipt WHERE provider='terminal' AND dataset_id=? "
                            "AND entity_kind=? AND entity_id=? AND revision_id=?",
                            (dataset,item["entity_kind"],item["entity_id"],item["revision_id"]))
                        if receipt:
                            continue
                        known=self.s.one("SELECT 1 FROM research_import_receipt WHERE provider='terminal' AND entity_kind=? AND entity_id=? AND revision_id=? LIMIT 1",
                            (item["entity_kind"],item["entity_id"],item["revision_id"]))
                        if not known and (prior["seq"] is None or prior["seq"] < item["change_seq"]):
                            self.s.ingest("terminal", item)
                        self.s.execute("INSERT INTO research_import_receipt(provider,dataset_id,entity_kind,entity_id,revision_id,change_seq,available_at) "
                            "VALUES('terminal',?,?,?,?,?,?)", (dataset,item["entity_kind"],item["entity_id"],item["revision_id"],
                            item["change_seq"],self.s.clock()))
                        count += 1
                except (ResearchError, KeyError, TypeError, ValueError) as error:
                    if not isinstance(raw, dict):
                        raw = {"invalid": str(raw)[:1000]}
                    qid = local_id("terminal", "quarantine", dataset+":"+digest(raw))
                    self.s.execute("INSERT INTO research_quarantine(id,provider,dataset_id,entity_kind,entity_id,revision_id,reason,available_at,status,payload) "
                        "VALUES(?,'terminal',?,?,?,?,?,?,'pending',?) ON CONFLICT(id) DO NOTHING",
                        (qid,dataset,str(raw.get("entity_kind","")),str(raw.get("entity_id","")),str(raw.get("revision_id","")),
                         getattr(error,"code","schema_error"),self.s.clock(),encode(raw)))
                    quarantined += 1
            state = {**state, "dataset_id": dataset, "pending": bool(envelope.get("has_more")),
                     "updated_at": self.s.clock(), "history_complete": envelope.get("history_complete", False)}
            if snapshot:
                state["snapshot_cursor"] = envelope["next_cursor"]
                if not envelope.get("has_more"):
                    state["change_cursor"] = state["snapshot_watermark"]
                    state.pop("snapshot_id", None)
                    state.pop("snapshot_cursor", None)
            else:
                state["change_cursor"] = envelope["next_cursor"]
            if not envelope.get("has_more"):
                state["last_success_at"] = self.s.clock()
                state.pop("error_code", None)
            self.s.set_state("terminal", state)
        return count, quarantined, state

    def retry_quarantine(self, identifier):
        row = self.s.one("SELECT * FROM research_quarantine WHERE id=?", (identifier,))
        if not row:
            raise ResearchError("quarantine_not_found", "Запись карантина не найдена", 404)
        item = validate_item(decode(row["payload"]))
        with self.s.db.batch_writes():
            prior = self.s.one("SELECT MAX(change_seq) AS seq FROM research_import_receipt WHERE provider='terminal' "
                              "AND dataset_id=? AND entity_kind=? AND entity_id=?",
                              (row["dataset_id"],item["entity_kind"],item["entity_id"]))
            if prior["seq"] is None or prior["seq"] < item["change_seq"]:
                self.s.ingest("terminal", item)
                self.s.execute("INSERT INTO research_import_receipt(provider,dataset_id,entity_kind,entity_id,revision_id,change_seq,available_at) "
                               "VALUES('terminal',?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                               (row["dataset_id"],item["entity_kind"],item["entity_id"],item["revision_id"],item["change_seq"],self.s.clock()))
            self.s.execute("UPDATE research_quarantine SET status='resolved' WHERE id=?", (identifier,))
        return {"id": identifier, "status": "resolved"}

    def request(self, client, method, path, **kwargs):
        response = client.request(method, endpoint(self.settings.research_terminal_url) + path, **kwargs)
        if response.status_code in {401, 403}:
            raise ResearchError("auth_error", "Токен экспорта не принят терминалом", 503)
        if response.status_code == 410:
            raise ResearchError("cursor_expired", "Снимок или курсор истёк", 410)
        if response.status_code == 429:
            try:
                delay = min(1800, max(1, int(response.headers.get("retry-after", "60"))))
            except ValueError:
                delay = 60
            error = ResearchError("rate_limited", "Терминал ограничил частоту запросов", 429)
            error.retry_after = delay
            raise error
        if response.status_code != 200:
            raise ResearchError("source_unavailable", "Экспорт терминала временно недоступен", 503)
        if len(response.content) > 8_000_000:
            raise ResearchError("schema_error", "Превышен размер страницы экспорта", 503)
        try:
            return response.json()
        except ValueError:
            raise ResearchError("schema_error", "Экспорт вернул некорректный JSON", 503)

    def sync(self, job_id=None, should_stop=None):
        if not self.settings.research_enabled:
            raise ResearchError("disabled", "Исследовательский модуль выключен", 503)
        if not self.settings.research_terminal_url or not self.settings.research_integration_token:
            raise ResearchError("not_configured", "Настройте адрес терминала и токен экспорта", 503)
        headers = {"Authorization": "Bearer " + self.settings.research_integration_token}
        with httpx.Client(headers=headers, timeout=self.settings.research_http_timeout_seconds,
                          follow_redirects=False, trust_env=False) if self.client is None else _borrow(self.client) as client:
            caps = self.request(client, "GET", "/capabilities")
            if caps.get("schema_version") != VERSION:
                raise ResearchError("schema_error", "Неподдерживаемая версия терминала", 503)
            state = self.s.state("terminal")
            if state.get("dataset_id") != caps.get("dataset_id"):
                state = {"last_success_at": state.get("last_success_at"), "dataset_id": caps.get("dataset_id")}
            if not state.get("change_cursor") and not state.get("snapshot_id"):
                snapshot = self.request(client, "POST", "/snapshots", json={})
                state.update({"snapshot_id": snapshot["snapshot_id"], "snapshot_watermark": snapshot["change_cursor"],
                              "snapshot_cursor": None})
                with self.s.db.batch_writes():
                    self.s.set_state("terminal", state)
            for _ in range(50):
                if should_stop and should_stop():
                    return {"interrupted":True}
                if job_id:
                    job = self.s.one("SELECT status FROM research_job WHERE id=?", (job_id,))
                    if job and job["status"] == "cancelled":
                        return {"cancelled": True}
                snapshot = bool(state.get("snapshot_id"))
                params = {"limit": min(self.settings.research_sync_page_size, 500)}
                if snapshot:
                    path = "/snapshots/" + state["snapshot_id"]
                    if state.get("snapshot_cursor"):
                        params["cursor"] = state["snapshot_cursor"]
                else:
                    path = "/changes"
                    params["after"] = state["change_cursor"]
                envelope = self.request(client, "GET", path, params=params)
                _, _, state = self.page(envelope, state=state, snapshot=snapshot)
                if job_id:
                    self.s.execute("UPDATE research_job SET lease_until=?,updated_at=?,progress=progress+1 WHERE id=? AND status='running'",
                                   (self.s.clock()+120000,self.s.clock(),job_id))
                if not envelope.get("has_more") and not snapshot:
                    break
            return state


class _borrow:
    def __init__(self, client):
        self.client = client
    def __enter__(self):
        return self.client
    def __exit__(self, *args):
        return False
