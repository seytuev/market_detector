/**
 * Склейка: одно событие — много источников. Без модели, детерминированно.
 *
 * Кандидат совпадает с событием, если:
 *  - вид совместим (одинаковый или один из них «other»/«launch»/«deadline» — родственные);
 *  - даты совместимы: обе точные/дневные — расхождение ≤ 1 день; неделя — ≤ 4 дня; месяц — тот же месяц;
 *    у одного даты нет — совпадение только при общем тикере и похожем названии;
 *  - и по смыслу: общий тикер (+ для разлоков достаточно тикера и даты) или похожее название (Жаккар по словам);
 *    макро — по ключу (FOMC / CPI / NFP / GDP / PCE) и дате.
 * При склейке: источник и запись в ленту контекста добавляются, тикеры объединяются, точность растёт,
 * структурный источник (Tokenomist, ФРС, Finnhub…) важнее соцсетей. Разные даты → conflicts и статус «на проверку».
 */
import type { CalEvent, EventCandidate, EventKind, EventStatus, Precision } from "./types";
import { KIND_GROUP, STRUCTURED } from "./types";
import { diffDays, toUtcMs } from "./time";
import { scoreImportance } from "./importance";

const STOP = new Set(["the", "and", "for", "with", "of", "on", "in", "to", "a", "на", "в", "и", "с", "по", "для", "из", "от", "за"]);
export function words(s: string): Set<string> {
  return new Set(s.toLowerCase().replace(/\$/g, "").replace(/[^\p{L}\p{N}\s]/gu, " ").split(/\s+/).filter((w) => w.length > 1 && !STOP.has(w)));
}
export function jaccard(a: string, b: string): number {
  const A = words(a), B = words(b);
  if (!A.size || !B.size) return 0;
  let n = 0;
  for (const w of A) if (B.has(w)) n++;
  return n / (A.size + B.size - n);
}

const MACRO_KEYS: [string, RegExp][] = [["fomc", /fomc|фрс.*ставк|ставк.*фрс|fed rate|interest rate decision|процентная ставка/i], ["cpi", /\bcpi\b|инфляц|consumer price/i], ["nfp", /non.?farm|nfp|payrolls|занятост/i], ["gdp", /\bgdp\b|ввп/i], ["pce", /\bpce\b/i]];
const macroKey = (t: string) => MACRO_KEYS.find(([, re]) => re.test(t))?.[0] ?? null;

const FAMILY: Partial<Record<EventKind, string>> = { launch: "launch", deadline: "launch", sale: "launch", other: "*" };
function kindsCompatible(a: EventKind, b: EventKind): boolean {
  if (a === b) return true;
  if (FAMILY[a] === "*" || FAMILY[b] === "*") return true;
  return !!FAMILY[a] && FAMILY[a] === FAMILY[b];
}

function datesCompatible(e: CalEvent, c: EventCandidate): boolean | "undated" {
  if (!e.date || !c.date) return "undated";
  const d = Math.abs(diffDays(e.date, c.date));
  const p = (x: Precision) => (x === "exact" || x === "day" ? 1 : x === "week" ? 4 : x === "month" ? 31 : 92);
  const tol = Math.max(p(e.precision), p(c.precision));
  if (tol >= 31) return e.date.slice(0, 7) === c.date.slice(0, 7) || d <= 3;
  return d <= tol;
}

export function matches(e: CalEvent, c: EventCandidate): boolean {
  if (e.status === "cancelled") return false;
  if (!kindsCompatible(e.kind, c.kind)) return false;
  // разлок одного тикера в пределах недели — это одно событие с расхождением дат (а не два разных)
  if (e.kind === "unlock" && c.kind === "unlock" && e.date && c.date && c.tickers.some((t) => e.tickers.includes(t)) && Math.abs(diffDays(e.date, c.date)) <= 7) return true;
  const dates = datesCompatible(e, c);
  if (dates === false) return false;
  if (e.kind === "macro" || c.kind === "macro") {
    const a = macroKey(e.title), b = macroKey(c.title);
    return !!a && a === b && dates === true;
  }
  const shared = c.tickers.some((t) => e.tickers.includes(t));
  const sim = jaccard(e.title + " " + e.projects.join(" "), c.title + " " + c.projects.join(" "));
  if (dates === "undated") return shared && sim >= 0.3;
  if (e.kind === "unlock" && c.kind === "unlock") return shared;
  if (e.kind === "expiry" && c.kind === "expiry") return true;
  if (shared) return sim >= 0.15 || e.kind === c.kind;
  if (!e.tickers.length || !c.tickers.length) return sim >= 0.45;
  return false;
}

const PREC_RANK: Record<Precision, number> = { exact: 5, day: 4, week: 3, month: 2, quarter: 1, unknown: 0 };
const isStructured = (k: string) => (STRUCTURED as string[]).includes(k);

export function newEvent(c: EventCandidate, now: number, myTickers: Set<string>): CalEvent {
  const e: CalEvent = {
    id: eventId(c),
    title: c.title,
    kind: c.kind,
    group: KIND_GROUP[c.kind],
    date: c.date,
    time: c.time,
    startsAt: c.date ? toUtcMs(c.date, c.time, "UTC") : null,
    endDate: c.endDate ?? null,
    precision: c.precision,
    tickers: [...new Set(c.tickers)],
    projects: [...new Set(c.projects)],
    importance: 1,
    status: "review",
    valueUsd: c.valueUsd ?? null,
    pctOfMcap: c.pctOfMcap ?? null,
    allocation: c.allocation ?? null,
    country: c.country ?? null,
    icon: c.icon ?? null,
    summary: c.summary ?? null,
    context: null,
    contextModel: null,
    contextSources: 0,
    sources: [c.source],
    timeline: [{ at: c.source.at, source: c.source.kind, author: c.source.author, text: c.source.text ?? c.summary ?? c.title, url: c.source.url }],
    conflicts: [],
    pinned: false,
    userConfirmed: false,
    createdAt: now,
    updatedAt: now,
  };
  e.importance = Math.max(c.importance ?? 1, scoreImportance(e, myTickers, now)) as 1 | 2 | 3;
  e.status = statusOf(e);
  return e;
}

export function mergeInto(e: CalEvent, c: EventCandidate, now: number, myTickers: Set<string>): CalEvent {
  const x: CalEvent = { ...e, sources: [...e.sources], timeline: [...e.timeline], conflicts: [...e.conflicts], tickers: [...e.tickers], projects: [...e.projects] };
  if (x.sources.some((s) => s.kind === c.source.kind && s.ref === c.source.ref)) return e; // уже учтено
  x.sources.push(c.source);
  x.timeline.push({ at: c.source.at, source: c.source.kind, author: c.source.author, text: c.source.text ?? c.summary ?? c.title, url: c.source.url });
  x.timeline.sort((a, b) => a.at - b.at);
  x.tickers = [...new Set([...x.tickers, ...c.tickers])];
  x.projects = [...new Set([...x.projects, ...c.projects])];
  const cStruct = isStructured(c.source.kind);
  const eStruct = x.sources.some((s) => s !== c.source && isStructured(s.kind)) || x.userConfirmed;
  // дата и время: структурный источник и пользователь важнее; иначе — более точный
  if (c.date) {
    if (!x.date || (!eStruct && (cStruct || PREC_RANK[c.precision] > PREC_RANK[x.precision]))) {
      if (x.date && x.date !== c.date && Math.abs(diffDays(x.date, c.date)) > 1) x.conflicts.push({ date: x.date, source: x.sources[0].kind, author: x.sources[0].author });
      x.date = c.date;
      x.time = c.time ?? (x.date === c.date ? x.time : null);
      x.precision = c.precision;
    } else if (x.date !== c.date && Math.abs(diffDays(x.date, c.date)) > 1) {
      x.conflicts.push({ date: c.date, source: c.source.kind, author: c.source.author });
    } else if (!x.time && c.time && x.date === c.date) {
      x.time = c.time;
      x.precision = "exact";
    }
  }
  if (cStruct && c.kind !== "other" && x.kind === "other") {
    x.kind = c.kind;
    x.group = KIND_GROUP[c.kind];
  }
  if (cStruct) x.title = c.title; // название из API аккуратнее
  x.valueUsd = c.valueUsd ?? x.valueUsd;
  x.pctOfMcap = c.pctOfMcap ?? x.pctOfMcap;
  x.allocation = c.allocation ?? x.allocation;
  x.country = c.country ?? x.country;
  x.icon = x.icon ?? c.icon ?? null;
  if (!x.summary && c.summary) x.summary = c.summary;
  x.startsAt = x.date ? toUtcMs(x.date, x.time, "UTC") : null;
  x.importance = Math.max(x.importance, c.importance ?? 1, scoreImportance(x, myTickers, now)) as 1 | 2 | 3;
  x.status = statusOf(x);
  x.updatedAt = now;
  return x;
}

/** Статус: подтверждено — структурный источник / пользователь / 2+ независимых автора без расхождений. */
export function statusOf(e: CalEvent): EventStatus {
  if (e.status === "hidden" || e.status === "cancelled") return e.status;
  if (!e.date) return "undated";
  if (e.userConfirmed) return "verified";
  const structured = e.sources.some((s) => isStructured(s.kind));
  const openConflicts = e.conflicts.filter((c) => c.date !== e.date && Math.abs(diffDays(c.date, e.date!)) > 1);
  if (structured && !openConflicts.length) return "verified";
  const authors = new Set(e.sources.filter((s) => !s.date || s.date === e.date || Math.abs(diffDays(s.date, e.date!)) <= 1).map((s) => `${s.kind}:${s.author ?? s.ref}`));
  if (authors.size >= 2 && !openConflicts.length) return "verified";
  return "review";
}

export function eventId(c: EventCandidate): string {
  const key = [c.kind, [...c.tickers].sort().join(","), c.date ?? "nodate", [...words(c.title)].sort().slice(0, 6).join("-")].join("|");
  let h = 5381;
  for (const ch of key) h = ((h << 5) + h + ch.charCodeAt(0)) >>> 0;
  return `ev_${h.toString(36)}`;
}

/** Склеить пачку кандидатов с уже известными событиями. Возвращает изменённые/новые события. */
export function mergeAll(existing: CalEvent[], candidates: EventCandidate[], now: number, myTickers: Set<string>): { events: CalEvent[]; changed: Set<string>; added: number; merged: number } {
  const list = [...existing];
  const changed = new Set<string>();
  let added = 0, merged = 0;
  for (const c of candidates) {
    const idx = list.findIndex((e) => matches(e, c));
    if (idx >= 0) {
      const next = mergeInto(list[idx], c, now, myTickers);
      if (next !== list[idx]) {
        list[idx] = next;
        changed.add(next.id);
        merged++;
      }
    } else {
      const e = newEvent(c, now, myTickers);
      list.push(e);
      changed.add(e.id);
      added++;
    }
  }
  return { events: list, changed, added, merged };
}
