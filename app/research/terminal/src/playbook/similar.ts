/**
 * Поиск похожих примеров без внешних сервисов: TF-IDF по словам и парам слов, косинус.
 * Тикеры из сравнения убираются — паттерн про форму идеи («weekend pump»), а не про конкретную монету.
 */
import type { SourceSnapshot } from "./types";
import { snapshotText } from "./snapshot";

const STOP = new Set("the and for that this with from are was were has have but not you your just like will about into out over more than then they them what when which who how its it's our all can get got one also very really tbh imo lol это как что для его она они там тут уже еще ещё при или чем так все всё был была были будет есть нет его её их меня мне вот после перед автор пост посте твит график".split(" "));

export function terms(text: string, drop: Set<string> = new Set()): string[] {
  const w = text
    .toLowerCase()
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/\$[a-z0-9]+/g, " ")
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .split(/\s+/)
    .filter((x) => x.length > 2 && !STOP.has(x) && !drop.has(x) && !/^\d+$/.test(x));
  const out = [...w];
  for (let i = 0; i + 1 < w.length; i++) out.push(`${w[i]}_${w[i + 1]}`);
  return out;
}

export type Vec = Map<string, number>;

export function tfidf(docs: string[][]): { vecs: Vec[]; idf: (t: string) => number } {
  const df = new Map<string, number>();
  for (const d of docs) for (const t of new Set(d)) df.set(t, (df.get(t) ?? 0) + 1);
  const N = docs.length || 1;
  const idf = (t: string) => Math.log(1 + N / (1 + (df.get(t) ?? 0)));
  return { vecs: docs.map((d) => vec(d, idf)), idf };
}

export function vec(d: string[], idf: (t: string) => number): Vec {
  const tf = new Map<string, number>();
  for (const t of d) tf.set(t, (tf.get(t) ?? 0) + 1);
  const v: Vec = new Map();
  let n = 0;
  for (const [t, c] of tf) {
    const x = (1 + Math.log(c)) * idf(t) * (t.includes("_") ? 1.5 : 1);
    v.set(t, x);
    n += x * x;
  }
  n = Math.sqrt(n) || 1;
  for (const [t, x] of v) v.set(t, x / n);
  return v;
}

export function cosine(a: Vec, b: Vec): number {
  let s = 0;
  const [x, y] = a.size < b.size ? [a, b] : [b, a];
  for (const [t, v] of x) s += v * (y.get(t) ?? 0);
  return s;
}

const tickerDrop = (s: SourceSnapshot) => new Set(s.tickers.map((t) => t.toLowerCase()));

/**
 * Ближайшие к посту снимки (примеры), по убыванию сходства.
 * corpus — тексты всех примеров (для IDF), чтобы редкие слова паттерна («weekend», «inscriptions») весили больше.
 */
export function rankBySimilarity<T>(query: SourceSnapshot, items: { item: T; snap: SourceSnapshot }[], extraDocs: string[] = []): { item: T; score: number }[] {
  const docs = items.map((i) => terms(snapshotText(i.snap), tickerDrop(i.snap)));
  const q = terms(snapshotText(query), tickerDrop(query));
  const { vecs, idf } = tfidf([...docs, q, ...extraDocs.map((t) => terms(t))]);
  const qv = vec(q, idf);
  return items.map((i, k) => ({ item: i.item, score: cosine(qv, vecs[k]) })).sort((a, b) => b.score - a.score);
}
