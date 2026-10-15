/**
 * Сюжеты: одна новость в нескольких каналах = одна карточка с несколькими источниками.
 * Пример: «Multicoin инвестирует в Grass» — Shoal Research Hub и easychartgo → один сюжет, 2 канала.
 *
 * Склейка детерминированная, без модели:
 *  - общая ссылка (кроме t.me) → один сюжет;
 *  - или общий главный тикер + похожий заголовок (Jaccard по словам ≥ 0.25) + разница по времени ≤ 48 ч;
 *  - или общий фонд/проект в entities + общий тикер + ≤ 48 ч.
 */
import type { TgAnalysis, TgPost } from "./analyze";
import type { TgMedia } from "./media";

export interface AnalyzedPost {
  id: string; // channel:messageId
  post: TgPost;
  analysis: TgAnalysis;
  model: "grok" | "chatgpt";
  starred: boolean;
  /** сохранённые картинки поста и твитов по его ссылкам */
  media?: TgMedia[];
  /** id картинок в том порядке, в каком они ушли в модель (analysis.media[].index → mediaIds[index-1]) */
  mediaIds?: string[];
  /** текст твита по ссылке (прочитан Grok X Search) — показывается в «Источниках» */
  tweetText?: string;
}

export interface Story {
  id: string;
  posts: AnalyzedPost[];
  firstAt: number;
  lastAt: number;
  title: string;
  mainTickers: string[];
  allTickers: string[];
  entities: string[];
  impact: number;
  sentiment: "bullish" | "bearish" | "neutral" | "mixed";
  starred: boolean;
  channels: string[];
}

const WINDOW = 48 * 3_600_000;
const words = (s: string) => new Set(s.toLowerCase().replace(/[^\p{L}\p{N}\s]/gu, " ").split(/\s+/).filter((w) => w.length > 2));
export function jaccard(a: string, b: string): number {
  const A = words(a), B = words(b);
  if (!A.size || !B.size) return 0;
  let n = 0;
  for (const w of A) if (B.has(w)) n++;
  return n / (A.size + B.size - n);
}
const normLink = (u: string) => u.replace(/[?#].*$/, "").replace(/\/$/, "").toLowerCase();
const extLinks = (p: AnalyzedPost) => new Set(p.post.links.filter((l) => !/t\.me\//.test(l)).map(normLink));
const main = (p: AnalyzedPost) => p.analysis.tickers.filter((t) => t.role === "главный").map((t) => t.symbol);
const ents = (p: AnalyzedPost) => p.analysis.entities.map((e) => e.name.toLowerCase());

export function sameStory(a: AnalyzedPost, b: AnalyzedPost): boolean {
  if (Math.abs(a.post.postedAt - b.post.postedAt) > WINDOW) return false;
  const la = extLinks(a);
  for (const l of extLinks(b)) if (la.has(l)) return true;
  const ta = new Set(a.analysis.tickers.map((t) => t.symbol));
  const shareTicker = b.analysis.tickers.some((t) => ta.has(t.symbol));
  const shareMain = main(b).some((t) => main(a).includes(t));
  if (shareMain && jaccard(a.analysis.title_ru + " " + a.analysis.summary_ru, b.analysis.title_ru + " " + b.analysis.summary_ru) >= 0.25) return true;
  const ea = new Set(ents(a));
  if (shareTicker && ents(b).some((e) => ea.has(e))) return true;
  return false;
}

export function buildStories(posts: AnalyzedPost[]): Story[] {
  const sorted = [...posts].sort((a, b) => a.post.postedAt - b.post.postedAt);
  const groups: AnalyzedPost[][] = [];
  for (const p of sorted) {
    const g = groups.find((gr) => gr.some((q) => sameStory(p, q)));
    if (g) g.push(p);
    else groups.push([p]);
  }
  return groups
    .map((g) => {
      const best = [...g].sort((a, b) => b.analysis.impact - a.analysis.impact || Number(b.starred) - Number(a.starred))[0];
      const sents = new Set(g.map((p) => p.analysis.sentiment));
      const uniq = <T,>(a: T[]) => [...new Set(a)];
      return {
        id: g.map((p) => p.id).sort()[0],
        posts: g,
        firstAt: g[0].post.postedAt,
        lastAt: g[g.length - 1].post.postedAt,
        title: best.analysis.title_ru,
        mainTickers: uniq(g.flatMap(main)),
        allTickers: uniq(g.flatMap((p) => p.analysis.tickers.map((t) => t.symbol))),
        entities: uniq(g.flatMap((p) => p.analysis.entities.map((e) => e.name))),
        impact: Math.max(...g.map((p) => p.analysis.impact)) + (g.length > 1 ? 0.5 : 0),
        sentiment: sents.size === 1 ? [...sents][0] : "mixed",
        starred: g.some((p) => p.starred),
        channels: uniq(g.map((p) => p.post.channel)),
      } as Story;
    })
    .sort((a, b) => b.lastAt - a.lastAt);
}
