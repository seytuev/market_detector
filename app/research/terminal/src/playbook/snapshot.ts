/**
 * Снимки источников: пост Telegram (уже разобранный), карточка Инфополя (разбор Grok), твит из X.
 * Разбор ИИ переиспользуется — второй раз модель не вызывается.
 */
import type { SourceSnapshot } from "./types";
import type { AnalyzedPost } from "../telegram/stories";

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
const cleanTicker = (s: string) => s.replace(/^\$/, "").toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, 12);
const uniq = (a: string[]) => [...new Set(a.filter(Boolean))];

export function snapshotFromTelegram(p: AnalyzedPost, channelTitle?: string): SourceSnapshot {
  const a = p.analysis;
  const main = a.tickers.find((t) => t.role === "главный") ?? a.tickers[0];
  const media = p.media ?? [];
  const byId = new Map(media.map((m) => [m.id, m]));
  const images = (p.mediaIds ?? []).map((id) => byId.get(id)).filter(Boolean).map((m) => m!.thumbUrl ?? m!.url);
  return {
    kind: "telegram",
    ref: p.id,
    url: `https://t.me/${p.post.channel}/${p.post.messageId}`,
    href: `/telegram?post=${encodeURIComponent(p.id)}`,
    author: channelTitle ?? p.post.channel,
    at: p.post.postedAt,
    text: clip([p.post.text, p.tweetText ? `Твит: ${p.tweetText}` : ""].filter(Boolean).join("\n\n"), 4000),
    title: a.title_ru,
    summary: clip([a.what_happened_ru, a.why_matters_ru].filter(Boolean).join(" "), 1500),
    tickers: uniq(a.tickers.map((t) => cleanTicker(t.symbol))),
    symbol: main ? cleanTicker(main.symbol) : null,
    imageNotes: (a.media ?? []).map((m) => `[${m.index}] ${m.text_ru}`),
    images: images.slice(0, 4),
  };
}

/** Карточка Инфополя. Поля — как в разборе Grok на странице (заголовок, разбор, оригинал, тикеры с ролью). */
export interface InfopoleCardLike {
  id: string;
  url?: string | null;
  author?: string | null;
  at: number;
  originalText?: string | null;
  images?: string[];
  analysis: {
    title_ru: string;
    /** «Разбор Grok» целиком */
    text_ru?: string | null;
    summary_ru?: string | null;
    thesis_ru?: string | null;
    tickers: { symbol: string; role?: string }[];
    media?: { text_ru: string }[];
  };
}

export function snapshotFromInfopole(c: InfopoleCardLike): SourceSnapshot {
  const a = c.analysis;
  const main = a.tickers.find((t) => (t.role ?? "").startsWith("главн")) ?? a.tickers[0];
  return {
    kind: "infopole",
    ref: c.id,
    url: c.url ?? null,
    href: `/inbox?card=${encodeURIComponent(c.id)}`,
    author: c.author ?? null,
    at: c.at,
    text: clip(c.originalText ?? "", 4000),
    title: a.title_ru,
    summary: clip(a.text_ru ?? a.summary_ru ?? a.thesis_ru ?? "", 1500),
    tickers: uniq(a.tickers.map((t) => cleanTicker(t.symbol))),
    symbol: main ? cleanTicker(main.symbol) : null,
    imageNotes: (a.media ?? []).map((m, i) => `[${i + 1}] ${m.text_ru}`),
    images: (c.images ?? []).slice(0, 4),
  };
}

/** Твит (формат как XPost в src/events/sources/x.ts и в X Feed). */
export interface XPostLike {
  id: string;
  author: string;
  text: string;
  at: number;
  url: string;
  images: string[];
}

/** Твит из X Feed. Разбора нет — тикеры берём из $CASHTAG, остальное модель поиска увидит в тексте и картинках. */
export function snapshotFromX(p: XPostLike): SourceSnapshot {
  const tickers = uniq((p.text.match(/\$[A-Za-z][A-Za-z0-9]{1,11}\b/g) ?? []).map(cleanTicker));
  return {
    kind: "x",
    ref: p.id,
    url: p.url,
    href: null,
    author: p.author,
    at: p.at,
    text: clip(p.text, 4000),
    title: clip(p.text.split("\n")[0], 90),
    summary: "",
    tickers,
    symbol: tickers[0] ?? null,
    imageNotes: [],
    images: p.images.slice(0, 4),
  };
}

/** Текст снимка для сравнения и для запроса модели. */
export function snapshotText(s: SourceSnapshot): string {
  return [s.title, s.text, s.summary, s.imageNotes.join(" "), s.tickers.map((t) => "$" + t).join(" ")].filter(Boolean).join("\n");
}

/**
 * Любая карточка информационного потока, у которой нет своего разбора в этом модуле:
 * Reflexion Radar, Article Feed, Video Feed, Tickers (и всё, что появится потом). Заполни из своей таблицы.
 */
export interface GenericCardLike {
  kind: "radar" | "article" | "video" | "ticker" | "x" | "infopole" | "telegram";
  id: string;
  url?: string | null;
  href?: string | null;
  author?: string | null;
  at: number;
  title: string;
  /** оригинал / транскрипт / текст статьи (обрежется до 4000 знаков) */
  text?: string | null;
  /** разбор ИИ, если есть */
  summary?: string | null;
  tickers?: string[];
  images?: string[];
  imageNotes?: string[];
}

export function snapshotFromCard(c: GenericCardLike): SourceSnapshot {
  const tickers = uniq([...(c.tickers ?? []), ...((c.text ?? "").match(/\$[A-Za-z][A-Za-z0-9]{1,11}\b/g) ?? [])].map(cleanTicker));
  return {
    kind: c.kind,
    ref: c.id,
    url: c.url ?? null,
    href: c.href ?? null,
    author: c.author ?? null,
    at: c.at,
    text: clip(c.text ?? "", 4000),
    title: clip(c.title, 140),
    summary: clip(c.summary ?? "", 1500),
    tickers,
    symbol: tickers[0] ?? null,
    imageNotes: c.imageNotes ?? [],
    images: (c.images ?? []).slice(0, 4),
  };
}
