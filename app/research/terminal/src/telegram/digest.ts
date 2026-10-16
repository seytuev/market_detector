/**
 * Дайджест Telegram за период (по умолчанию 24 ч) — через ChatGPT.
 * Вход — уже разобранные сюжеты с ценами (цифры из priceCheck), модель только пишет текст.
 * Запуск: по расписанию (08:00 и 20:00 по времени пользователя) + кнопка «Собрать дайджест».
 */
import type { Story } from "./stories";
import type { PriceReaction } from "./priceCheck";

export interface DigestInput {
  from: number;
  to: number;
  stories: (Story & { prices?: Record<string, PriceReaction> })[];
}

export interface Digest {
  headline_ru: string;
  bullets: { text_ru: string; story_id: string; tickers: string[] }[]; // 3–6 главных сюжетов
  funds_ru: string[]; // кто из фондов что сделал
  tickers_heat: { symbol: string; mentions: number; channels: number; since_post_pct: number | null }[];
  watch_ru: string[]; // за чем следить
}

export const DIGEST_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["headline_ru", "bullets", "funds_ru", "watch_ru"],
  properties: {
    headline_ru: { type: "string" },
    bullets: {
      type: "array",
      items: { type: "object", additionalProperties: false, required: ["text_ru", "story_id", "tickers"], properties: { text_ru: { type: "string" }, story_id: { type: "string" }, tickers: { type: "array", items: { type: "string" } } } },
    },
    funds_ru: { type: "array", items: { type: "string" } },
    watch_ru: { type: "array", items: { type: "string" } },
  },
} as const;

/** Компактный вход для модели: без полного текста постов, только разбор и цифры. */
export function digestPayload(d: DigestInput) {
  return d.stories
    .filter((s) => s.lastAt >= d.from && s.lastAt <= d.to)
    .sort((a, b) => b.impact - a.impact)
    .slice(0, 40)
    .map((s) => ({
      story_id: s.id,
      title: s.title,
      summary: s.posts[0].analysis.summary_ru,
      why: s.posts[0].analysis.why_matters_ru,
      tickers: s.mainTickers,
      funds: s.posts.flatMap((p) => p.analysis.entities.filter((e) => e.kind === "fund").map((e) => `${e.name}: ${e.action ?? ""}`)),
      channels: s.channels.length,
      starred: s.starred,
      impact: s.impact,
      price: Object.fromEntries(Object.entries(s.prices ?? {}).map(([k, v]) => [k, { since_post_pct: v.sincePostPct == null ? null : +v.sincePostPct.toFixed(1), verdict: v.verdict }])),
    }));
}

/** Горячие тикеры считаются кодом, не моделью. */
export function tickersHeat(d: DigestInput): Digest["tickers_heat"] {
  const m = new Map<string, { mentions: number; channels: Set<string>; since: number | null }>();
  for (const s of d.stories) {
    if (s.lastAt < d.from || s.lastAt > d.to) continue;
    for (const p of s.posts)
      for (const t of p.analysis.tickers) {
        const e = m.get(t.symbol) ?? { mentions: 0, channels: new Set<string>(), since: null };
        e.mentions++;
        e.channels.add(p.post.channel);
        e.since = s.prices?.[t.symbol]?.sincePostPct ?? e.since;
        m.set(t.symbol, e);
      }
  }
  return [...m.entries()]
    .map(([symbol, e]) => ({ symbol, mentions: e.mentions, channels: e.channels.size, since_post_pct: e.since }))
    .sort((a, b) => b.channels - a.channels || b.mentions - a.mentions)
    .slice(0, 12);
}

export const DIGEST_PROMPT = `Составь дайджест Telegram-каналов для крипто-трейдера по-русски.
headline_ru — одно предложение: главное за период. bullets — 3–6 самых важных сюжетов, каждый 1–2 предложения, со ссылкой на story_id.
funds_ru — что сделали фонды (если есть). watch_ru — 2–4 пункта, за чем следить.
Используй только данные из входа, цифры бери оттуда же, ничего не добавляй.`;
