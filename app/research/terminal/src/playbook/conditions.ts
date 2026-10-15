/**
 * v4: условия паттерна, которые проверяет КОД — по фактам терминала (snapshot.facts), без модели.
 *
 * Зачем: в заметке ты пишешь «хороший автор + токен уже в корзине». Модель это понимает, но
 *  - не знает, что лежит в твоих корзинах, и не помнит историю автора;
 *  - на сотнях постов в день дорого спрашивать модель про всё подряд.
 * Поэтому такие вещи превращаются в условия: код точно знает, в корзине ли $NEAR и сколько сигналов было у автора.
 *
 * Откуда берутся условия:
 *  1. Сильная модель при переписывании правила ставит флаги (rule.flags): важен автор / «наш» тикер / скрин —
 *     прочитав твои заметки и упор («Хороший автор…», «токен уже в корзине»).
 *  2. Код смотрит на сами примеры: у большинства сигналов тикер был «нашим»? одни и те же авторы? скрин?
 *  3. Предложенные условия включаются как «желательно» (origin auto) и пересчитываются после каждого правила.
 *     «Обязательно» ставишь только ты — кнопкой; твои условия (origin manual) код сам не меняет.
 */
import type { Condition, ConditionMode, Example, Match, Pattern, SourceSnapshot } from "./types";

export const MODE_RU: Record<ConditionMode, string> = { require: "обязательно", prefer: "желательно", off: "выкл" };

export function conditionRu(c: Condition): string {
  switch (c.kind) {
    case "author_in":
      return `автор из: ${c.authors.slice(0, 4).join(", ")}${c.authors.length > 4 ? ` +${c.authors.length - 4}` : ""}`;
    case "author_track":
      return `автор уже давал сигналы в учебнике (≥ ${c.minSignals})`;
    case "ticker_known":
      return "тикер уже наш: портфель, корзина или идея";
    case "ticker_in_portfolio":
      return "тикер в портфеле";
    case "ticker_in_basket":
      return "тикер в корзине";
    case "ticker_has_idea":
      return "по тикеру есть идея";
    case "has_ticker":
      return "в посте есть тикер";
    case "has_image":
      return "в посте есть график / скрин";
    case "words_any":
      return `есть слова: ${c.words.slice(0, 5).join(", ")}`;
  }
}

/** ключ автора: «The DeFi Investor (@TheDeFinvestor)» → «thedefinvestor»; «@kdp_dao» → «kdp_dao» */
export function authorKey(author: string | null | undefined): string | null {
  if (!author) return null;
  const h = author.match(/@([A-Za-z0-9_]{2,32})/);
  const k = (h ? h[1] : author).toLowerCase().replace(/[^\p{L}\p{N}_]+/gu, "");
  return k || null;
}

/** true / false — проверено; null — фактов нет (не блокируем и не штрафуем) */
export function checkCondition(c: Condition, s: SourceSnapshot): boolean | null {
  const f = s.facts;
  switch (c.kind) {
    case "author_in": {
      const k = authorKey(s.author);
      return k ? c.authors.map((a) => authorKey(a)).includes(k) : false;
    }
    case "author_track":
      return f ? (f.author?.signals ?? 0) >= c.minSignals : null;
    case "ticker_known":
      return f ? f.known : null;
    case "ticker_in_portfolio":
      return f ? f.tickers.some((t) => t.portfolios.length > 0) : null;
    case "ticker_in_basket":
      return f ? f.tickers.some((t) => t.baskets.length > 0) : null;
    case "ticker_has_idea":
      return f ? f.tickers.some((t) => t.ideas.length > 0) : null;
    case "has_ticker":
      return s.tickers.length > 0;
    case "has_image":
      return s.images.length > 0 || s.imageNotes.length > 0;
    case "words_any": {
      const text = `${s.text} ${s.title} ${s.summary}`.toLowerCase();
      return c.words.some((w) => w.trim() && text.includes(w.trim().toLowerCase()));
    }
  }
}

export interface Gate {
  /** все обязательные условия выполнены (или не проверяемы) */
  pass: boolean;
  failedRequired: Condition[];
  preferHit: Condition[];
  preferMiss: Condition[];
}

export function gatePattern(p: Pattern, s: SourceSnapshot): Gate {
  const g: Gate = { pass: true, failedRequired: [], preferHit: [], preferMiss: [] };
  for (const c of p.conditions ?? []) {
    if (c.mode === "off") continue;
    const r = checkCondition(c, s);
    if (r === null) continue;
    if (c.mode === "require" && !r) {
      g.pass = false;
      g.failedRequired.push(c);
    }
    if (c.mode === "prefer") (r ? g.preferHit : g.preferMiss).push(c);
  }
  return g;
}

/** поправка уверенности находки по «желательным» условиям: +5 за каждое (до +10), −5 за каждое невыполненное */
export function adjustConfidence(m: Match, g: Gate): Match {
  if (!g.preferHit.length && !g.preferMiss.length) return m;
  const delta = Math.min(10, 5 * g.preferHit.length) - 5 * g.preferMiss.length;
  const note = [g.preferHit.length ? `код: ${g.preferHit.map(conditionRu).join("; ")}` : "", g.preferMiss.length ? `нет: ${g.preferMiss.map(conditionRu).join("; ")}` : ""].filter(Boolean).join(" · ");
  return { ...m, confidence: Math.max(0, Math.min(100, m.confidence + delta)), differs_ru: g.preferMiss.length ? [m.differs_ru, note].filter(Boolean).join(" · ") : m.differs_ru };
}

/** строки для запроса модели: какие условия поставил управляющий */
export function conditionsForPrompt(p: Pattern): string[] {
  return (p.conditions ?? []).filter((c) => c.mode !== "off").map((c) => `• ${MODE_RU[c.mode].toUpperCase()}: ${conditionRu(c)}`);
}

const share = (xs: boolean[]) => (xs.length ? xs.filter(Boolean).length / xs.length : 0);

/**
 * Предложить условия по примерам и флагам правила. Возвращает только auto-условия (mode prefer).
 *  - автор: флаг author от модели, или упор «Автор» в половине примеров, или одни и те же авторы (3+ сигнала, у главного 2+);
 *  - «наш» тикер: флаг known_ticker или 80%+ сигналов (при 3+) с известным тикером, а у анти — заметно реже;
 *  - скрин: флаг image или 80%+ сигналов (при 3+) со скрином.
 */
export function proposeConditions(p: Pattern, examples: Example[]): Condition[] {
  const pos = examples.filter((e) => e.patternId === p.id && e.direction !== "not_signal");
  const neg = examples.filter((e) => e.patternId === p.id && e.direction === "not_signal");
  if (!pos.length) return [];
  const flags = p.rule?.flags;
  const out: Condition[] = [];

  const authors = new Map<string, { name: string; n: number }>();
  for (const e of pos) {
    const k = authorKey(e.source.author);
    if (k) authors.set(k, { name: e.source.author!, n: (authors.get(k)?.n ?? 0) + 1 });
  }
  const top = [...authors.values()].sort((a, b) => b.n - a.n).slice(0, 3);
  const authorFocus = share(pos.map((e) => !!e.focus?.fields.includes("author"))) >= 0.5;
  // «одни и те же авторы»: 3+ сигнала, у главного автора 2+, и топ-3 автора дают 60%+ сигналов
  const concentrated = pos.length >= 3 && (top[0]?.n ?? 0) >= 2 && top.reduce((a, x) => a + x.n, 0) / pos.length >= 0.6;
  if (authors.size && (flags?.author || authorFocus || concentrated)) out.push({ kind: "author_in", authors: [...authors.values()].map((x) => x.name).slice(0, 12), mode: "prefer", origin: "auto" });

  const withFacts = pos.filter((e) => e.source.facts);
  const knownPos = share(withFacts.map((e) => !!e.source.facts!.known));
  const knownNeg = share(neg.filter((e) => e.source.facts).map((e) => !!e.source.facts!.known));
  if ((flags?.known_ticker && withFacts.some((e) => e.source.facts!.known)) || (withFacts.length >= 3 && knownPos >= 0.8 && (neg.length === 0 || knownNeg <= knownPos - 0.3)))
    out.push({ kind: "ticker_known", mode: "prefer", origin: "auto" });

  const img = share(pos.map((e) => e.source.images.length > 0 || e.source.imageNotes.length > 0));
  if (flags?.image || (pos.length >= 3 && img >= 0.8)) out.push({ kind: "has_image", mode: "prefer", origin: "auto" });
  return out;
}

const same = (a: Condition, b: Condition) => a.kind === b.kind;

/** Слить: твои (manual) остаются как есть; auto заменяются свежими предложениями; auto, которое ты переключил, становится manual. */
export function mergeConditions(current: Condition[] | undefined, proposed: Condition[]): Condition[] {
  const manual = (current ?? []).filter((c) => c.origin === "manual");
  return [...manual, ...proposed.filter((p) => !manual.some((m) => same(m, p)))];
}

/** проверить условие, присланное из интерфейса */
export function cleanCondition(x: any): Condition | null {
  const mode: ConditionMode = ["require", "prefer", "off"].includes(x?.mode) ? x.mode : "prefer";
  const base = { mode, origin: "manual" as const };
  const words = (v: unknown) => (Array.isArray(v) ? v.map((w) => String(w).trim().slice(0, 80)).filter(Boolean).slice(0, 30) : []);
  switch (x?.kind) {
    case "author_in": {
      const authors = words(x.authors);
      return authors.length ? { kind: "author_in", authors, ...base } : null;
    }
    case "author_track":
      return { kind: "author_track", minSignals: Math.max(1, Math.min(20, Math.round(Number(x.minSignals) || 1))), ...base };
    case "words_any": {
      const w = words(x.words);
      return w.length ? { kind: "words_any", words: w, ...base } : null;
    }
    case "ticker_known":
    case "ticker_in_portfolio":
    case "ticker_in_basket":
    case "ticker_has_idea":
    case "has_ticker":
    case "has_image":
      return { kind: x.kind, ...base };
    default:
      return null;
  }
}

/** факты терминала одной-двумя строками — для модели и для интерфейса */
export function factsLines(s: SourceSnapshot): string[] {
  const f = s.facts;
  if (!f) return [];
  const out: string[] = [];
  for (const t of f.tickers) {
    const parts = [t.portfolios.length ? `в портфеле ${t.portfolios.join(", ")}` : "", t.baskets.length ? `в корзине ${t.baskets.map((b) => `«${b}»`).join(", ")}` : "", t.ideas.length ? `есть идея` : ""].filter(Boolean);
    out.push(`$${t.symbol}: ${parts.length ? parts.join("; ") : "не наш (нет в портфелях, корзинах и идеях)"}`);
  }
  if (f.author) out.push(`Автор ${f.author.name}: в учебнике ${f.author.signals} сигн. / ${f.author.anti} анти; находки по нему: ${f.author.right} верно, ${f.author.wrong} мимо`);
  if (f.mentions24h != null) out.push(`Упоминаний тикера в других источниках за 24 ч: ${f.mentions24h}`);
  return out;
}
