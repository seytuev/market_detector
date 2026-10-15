/**
 * Поиск похожих: новый пост → какие активные паттерны он напоминает.
 *
 *  1. Отсев без модели: в посте должен быть тикер или картинка и хоть какой-то текст.
 *  2. Для каждого активного паттерна — 2 ближайших примера и 1 ближайший анти-пример (TF-IDF, similar.ts).
 *     Если паттернов больше 4 — в запрос идут 4 самых похожих.
 *  3. Один запрос дешёвой модели (gpt-4.1-mini / Grok) на пост: правило + примеры + твои общие правила.
 *  4. Проверка ответа кодом: паттерн из списка, тикер из поста, порог уверенности паттерна.
 */
import type { LlmClient, JsonSchemaSpec } from "../telegram/analyze";
import type { Example, Match, Pattern, SourceSnapshot } from "./types";
import { DIRECTION_RU, hashId, TRACK_RU, TRACK_HINT, FOCUS_RU, SOURCE_KINDS, trackOf, focusOf } from "./types";
import { rankBySimilarity } from "./similar";
import { weightedScore, blend, signsForPrompt } from "./weights";
import { factsLines, conditionsForPrompt, gatePattern, adjustConfidence } from "./conditions";

export const MATCH_SCHEMA: JsonSchemaSpec = {
  name: "pattern_match",
  schema: {
    type: "object",
    additionalProperties: false,
    required: ["matches"],
    properties: {
      matches: {
        type: "array",
        items: {
          type: "object",
          additionalProperties: false,
          required: ["pattern_id", "match", "confidence", "symbol", "direction", "why_ru", "differs_ru", "signs_hit"],
          properties: {
            pattern_id: { type: "string" },
            match: { type: "boolean" },
            confidence: { type: "number" },
            symbol: { type: ["string", "null"] },
            direction: { type: "string", enum: ["long", "short", "watch"] },
            why_ru: { type: "string" },
            differs_ru: { type: "string" },
            signs_hit: { type: "array", items: { type: "integer" } },
          },
        },
      },
    },
  },
};

export const MATCH_PROMPT = `Ты помощник управляющего крипто-фонда STV. Он собрал «учебник сигналов» — паттерны идей, которые сам отмечал в постах.
Тебе дают НОВЫЙ пост (оригинал, разбор, описания скринов, картинки) и несколько паттернов: правило, примеры и анти-примеры.
Для КАЖДОГО паттерна реши, относится ли пост к нему:
- у паттерна есть СТЕЗЯ (трейд-сигнал / события / нарратив / внимание / риск) — сравнивай по ней: для «События» важно, есть ли в посте
  предстоящие события того же типа и от того же типа автора; для «Трейд-сигнал» — та же торговая идея;
- если в примерах указан УПОР (автор, формулировка, график, тикер, событие, цифры) — это главный признак, проверяй его первым;
- match = true, только если пост несёт ту же идею, что примеры, по признакам правила; совпадение слов без смысла — не совпадение;
- если пост подходит под анти-признак или похож на анти-пример — match = false;
- confidence 0–100 — насколько уверен (70+ — явно тот же паттерн; 40–70 — похоже частично; меньше 40 — нет);
- symbol — главный тикер поста (только из тикеров поста; null, если тикера нет);
- direction — long / short / watch для этого поста;
- why_ru — одно предложение: какие признаки паттерна есть в посте;
- differs_ru — одно предложение: чем пост отличается от примеров или чего не хватает ("" если ничего);
- signs_hit — номера признаков [0], [1]… из правила, которые реально есть в посте. У признаков есть ВЕС от управляющего:
  «ГЛАВНЫЙ» — без него совпадение слабое; «слабый» — мало значит; «НЕ УЧИТЫВАТЬ» — игнорируй полностью.
- Комментарии управляющего к прошлым находкам (если есть) — его прямые указания, как реагировать: следуй им.
- «Факты терминала» у поста и примеров посчитаны кодом и точны (тикер в портфеле / корзине / идеях, история автора).
  «Условия» паттерна: ОБЯЗАТЕЛЬНЫЕ уже проверены кодом; ЖЕЛАТЕЛЬНЫЕ учитывай как признаки.
Верни ответ по каждому переданному паттерну. Общие правила управляющего учитывай всегда. Это не финансовый совет — ты только сравниваешь пост с его примерами.`;

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

export function passesGate(s: SourceSnapshot): boolean {
  const text = `${s.text} ${s.summary}`.trim();
  return ((s.tickers.length > 0 || s.images.length > 0) && text.length >= 15) || text.length >= 60;
}

/** Источники, которые сканирует паттерн. У паттернов v1 стоял набор из трёх — считаем, что это «все». */
export function sourcesOf(p: Pattern) {
  const v1 = p.sources.length === 3 && ["telegram", "x", "infopole"].every((k) => p.sources.includes(k as any));
  return v1 ? SOURCE_KINDS : p.sources;
}

/**
 * Паттерны, по которым пост вообще стоит проверять: активные, с правилом, сканируют этот источник,
 * и обязательные условия кода выполнены (v4). only — ограничить списком (прогон по истории одного паттерна).
 */
export function eligiblePatterns(s: SourceSnapshot, patterns: Pattern[], only?: string[]): Pattern[] {
  return patterns.filter((p) => p.status === "active" && p.rule && sourcesOf(p).includes(s.kind) && (!only || only.includes(p.id)) && gatePattern(p, s).pass);
}

export interface Candidate {
  pattern: Pattern;
  positives: Example[];
  negatives: Example[];
  score: number;
  /** последние комментарии к находкам этого паттерна («Частично: …», «Мимо: …») */
  comments?: string[];
}

/** Активные паттерны с правилом, которые сканируют этот источник; к каждому — ближайшие примеры. Не больше maxPatterns. */
export function candidatesFor(s: SourceSnapshot, patterns: Pattern[], examples: Example[], maxPatterns = 4): Candidate[] {
  const active = eligiblePatterns(s, patterns);
  const own = (e: Example) => e.source.kind === s.kind && e.source.ref === s.ref;
  const out = active.map((p) => {
    const ex = examples.filter((e) => e.patternId === p.id && !own(e));
    const pos = rankBySimilarity(s, ex.filter((e) => e.direction !== "not_signal").map((e) => ({ item: e, snap: e.source })), [p.rule!.summary_ru, ...p.rule!.signs_ru]);
    const neg = rankBySimilarity(s, ex.filter((e) => e.direction === "not_signal").map((e) => ({ item: e, snap: e.source })));
    return { pattern: p, positives: pos.slice(0, 2).map((x) => x.item), negatives: neg.slice(0, 1).map((x) => x.item), score: pos[0]?.score ?? 0 };
  });
  return out.sort((a, b) => b.score - a.score).slice(0, maxPatterns);
}

function exampleBlock(e: Example, label: string): string {
  return [
    `${label} · ${DIRECTION_RU[e.direction]}${e.source.symbol ? " · $" + e.source.symbol : ""} · ${e.source.author ?? ""}`,
    e.note ? `  заметка управляющего: ${clip(e.note, 300)}` : "",
    focusOf(e) ? `  упор: ${focusOf(e)!.fields.map((f) => FOCUS_RU[f]).join(", ")}${focusOf(e)!.note ? ` — ${clip(focusOf(e)!.note, 200)}` : ""}` : "",
    e.source.text ? `  оригинал: ${clip(e.source.text, 400)}` : "",
    e.source.summary ? `  разбор: ${clip(e.source.summary, 300)}` : "",
    e.source.imageNotes.length ? `  скрины: ${clip(e.source.imageNotes.join(" "), 200)}` : "",
    factsLines(e.source).length ? `  факты терминала: ${factsLines(e.source).join(" · ")}` : "",
  ].filter(Boolean).join("\n");
}

export function matchMessage(s: SourceSnapshot, cands: Candidate[], notes: string): string {
  const pats = cands.map((c) => {
    const r = c.pattern.rule!;
    return [
      `=== ПАТТЕРН id=${c.pattern.id} «${c.pattern.name}» · стезя ${TRACK_RU[trackOf(c.pattern)]} (${TRACK_HINT[trackOf(c.pattern)]}) · ${DIRECTION_RU[r.direction]} · горизонт ${r.horizon}`,
      `Суть: ${r.summary_ru}`,
      `Признаки:\n${signsForPrompt(c.pattern).join("\n")}`,
      r.anti_signs_ru.length ? `Анти-признаки: ${r.anti_signs_ru.map((x) => "• " + x).join(" ")}` : "",
      conditionsForPrompt(c.pattern).length ? `Условия управляющего:\n${conditionsForPrompt(c.pattern).join("\n")}` : "",
      ...c.positives.map((e, i) => exampleBlock(e, `Пример ${i + 1}`)),
      ...c.negatives.map((e) => exampleBlock(e, "Анти-пример")),
      c.comments?.length ? `Комментарии управляющего к прошлым находкам:\n${c.comments.map((x) => "• " + x).join("\n")}` : "",
    ].filter(Boolean).join("\n");
  });
  return [
    notes.trim() ? `Общие правила управляющего:\n${clip(notes, 1500)}` : "",
    pats.join("\n\n"),
    `=== НОВЫЙ ПОСТ · ${s.kind} · ${s.author ?? ""} · ${new Date(s.at).toISOString().slice(0, 16).replace("T", " ")} UTC`,
    `Тикеры поста: ${s.tickers.length ? s.tickers.map((t) => "$" + t).join(", ") : "нет"}`,
    s.title ? `Заголовок разбора: ${s.title}` : "",
    s.text ? `Оригинал: ${clip(s.text, 1500)}` : "",
    s.summary ? `Разбор: ${clip(s.summary, 800)}` : "",
    s.imageNotes.length ? `Скрины: ${clip(s.imageNotes.join(" "), 500)}` : "",
    factsLines(s).length ? `Факты терминала (посчитано кодом):\n${factsLines(s).join("\n")}` : "",
    s.images.length ? `Картинки поста приложены (${Math.min(s.images.length, 2)}).` : "",
  ].filter(Boolean).join("\n\n");
}

const cleanT = (x: unknown) => (typeof x === "string" ? x.replace(/^\$/, "").toUpperCase().replace(/[^A-Z0-9]/g, "") : "");

export function validateMatches(x: any, s: SourceSnapshot, cands: Candidate[], now: number, price: number | null = null): Match[] {
  if (!x || !Array.isArray(x.matches)) return [];
  const byId = new Map(cands.map((c) => [c.pattern.id, c]));
  const out: Match[] = [];
  for (const m of x.matches) {
    const c = byId.get(String(m?.pattern_id ?? ""));
    if (!c || m.match !== true || out.some((o) => o.patternId === c.pattern.id)) continue;
    const modelConfidence = Math.max(0, Math.min(100, Math.round(Number(m.confidence) || 0)));
    const nSigns = c.pattern.rule?.signs_ru.length ?? 0;
    const signsHit = Array.isArray(m.signs_hit) ? [...new Set((m.signs_hit as unknown[]).map(Number).filter((i) => Number.isInteger(i) && i >= 0 && i < nSigns))] : undefined;
    const ws = weightedScore(c.pattern, signsHit);
    const confidence = blend(modelConfidence, ws);
    if (confidence < c.pattern.minConfidence) continue;
    const sym = cleanT(m.symbol);
    const symbol = sym && s.tickers.includes(sym) ? sym : s.symbol;
    const direction = trackOf(c.pattern) !== "trade" ? "watch" : ["long", "short", "watch"].includes(m.direction) ? m.direction : c.pattern.rule!.direction;
    const why_ru = typeof m.why_ru === "string" ? m.why_ru.trim().slice(0, 400) : "";
    if (!why_ru) continue;
    out.push({
      id: `m_${hashId(`${c.pattern.id}|${s.kind}|${s.ref}`)}`,
      patternId: c.pattern.id,
      patternVersion: c.pattern.version,
      source: s,
      symbol,
      direction,
      confidence,
      modelConfidence,
      weightedScore: ws,
      signsHit,
      why_ru,
      differs_ru: typeof m.differs_ru === "string" ? m.differs_ru.trim().slice(0, 400) : "",
      exampleIds: [...c.positives, ...c.negatives].map((e) => e.id),
      priceAtMatch: price,
      status: "new",
      createdAt: now,
      feedbackAt: null,
    });
  }
  return out.sort((a, b) => b.confidence - a.confidence);
}

export interface MatchDeps {
  client: LlmClient;
  now?: () => number;
  price?: (symbol: string) => Promise<number | null>;
  maxPatterns?: number;
}

export async function matchPost(
  s: SourceSnapshot,
  patterns: Pattern[],
  examples: Example[],
  notes: string,
  d: MatchDeps & { comments?: Map<string, string[]>; only?: string[]; select?: (s: SourceSnapshot, eligible: Pattern[], examples: Example[], max: number) => Promise<Candidate[]> },
): Promise<Match[]> {
  if (!passesGate(s)) return [];
  const eligible = eligiblePatterns(s, patterns, d.only);
  if (!eligible.length) return [];
  // v4: отбор кандидатов может делать смысловой поиск (src/learn/embed.ts); без него — TF-IDF как раньше
  const picked = d.select ? await d.select(s, eligible, examples, d.maxPatterns ?? 4) : candidatesFor(s, eligible, examples, d.maxPatterns ?? 4);
  const cands = picked.map((c) => ({ ...c, comments: d.comments?.get(c.pattern.id) }));
  if (!cands.length) return [];
  const raw = await d.client.analyze(MATCH_PROMPT, matchMessage(s, cands, notes), s.images.slice(0, 2), MATCH_SCHEMA);
  const now = (d.now ?? Date.now)();
  // v4: «желательные» условия кода поправляют уверенность; порог паттерна проверяется ещё раз
  const byId = new Map(cands.map((c) => [c.pattern.id, c.pattern]));
  const first = validateMatches(raw, s, cands, now)
    .map((m) => adjustConfidence(m, gatePattern(byId.get(m.patternId)!, s)))
    .filter((m) => m.confidence >= byId.get(m.patternId)!.minConfidence);
  if (!first.length || !d.price) return first;
  const px = new Map<string, number | null>();
  for (const m of first) if (m.symbol && !px.has(m.symbol)) px.set(m.symbol, await d.price(m.symbol).catch(() => null));
  return first.map((m) => ({ ...m, priceAtMatch: m.symbol ? px.get(m.symbol) ?? null : null }));
}
