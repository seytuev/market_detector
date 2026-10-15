/**
 * Чтение публичных Telegram-каналов без ключей — через веб-превью https://t.me/s/<канал>.
 *
 * Работает для любого публичного канала с включённым веб-просмотром (так у всех каналов Telegram Feed).
 * Закрытые каналы и каналы без веб-превью читает MTProto-читатель (mtproto.ts, нужен вход в свой аккаунт).
 *
 * Страница отдаёт ~20 последних постов; более старые — по ?before=<id>. Читаем постранично назад,
 * пока не дойдём до последнего сохранённого id (курсор) или до лимита страниц.
 */
import type { TgPost } from "../telegram/analyze";

export interface RawPhoto {
  kind: "photo" | "video";
  /** прямой URL картинки (для видео — превью). CDN Telegram живёт недолго: сохранить через MediaStore сразу. */
  url: string;
}

export interface FetchedPost {
  post: TgPost;
  photos: RawPhoto[];
}

export interface ChannelReader {
  name: "web" | "mtproto";
  /**
   * Новые посты канала после sinceId (не включая), по возрастанию id.
   * sinceId = null — первый запуск: последние `backfill` постов.
   */
  fetchSince(handle: string, sinceId: number | null, o: { backfill: number; maxPosts: number }): Promise<FetchedPost[]>;
}

export class ChannelUnavailableError extends Error {
  constructor(public handle: string, why: string) {
    super(`@${handle}: ${why}`);
  }
}

/* ---------------- разбор HTML ---------------- */

const ENT: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", nbsp: " " };

export function decodeEntities(s: string): string {
  return s.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (m, e: string) => {
    if (e[0] === "#") {
      const code = e[1] === "x" || e[1] === "X" ? parseInt(e.slice(2), 16) : parseInt(e.slice(1), 10);
      return Number.isFinite(code) ? String.fromCodePoint(code) : m;
    }
    return ENT[e.toLowerCase()] ?? m;
  });
}

/** HTML текста поста → обычный текст: <br> → перевод строки, эмодзи-картинки → их alt, теги убираются. */
export function htmlToText(html: string): string {
  return decodeEntities(
    html
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<i class="emoji"[^>]*>([\s\S]*?)<\/i>/gi, "$1")
      .replace(/<img[^>]*alt="([^"]*)"[^>]*>/gi, "$1")
      .replace(/<[^>]+>/g, ""),
  )
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

/** «12.3K» → 12300, «1.1M» → 1100000 */
export function parseViews(s: string | undefined): number | undefined {
  if (!s) return undefined;
  const m = s.trim().match(/^([\d.,]+)\s*([KM]?)$/i);
  if (!m) return undefined;
  const n = parseFloat(m[1].replace(",", "."));
  return Math.round(n * (m[2].toUpperCase() === "K" ? 1e3 : m[2].toUpperCase() === "M" ? 1e6 : 1));
}

const bgUrl = (style: string) => style.match(/background-image:\s*url\(['"]?([^'")]+)['"]?\)/)?.[1] ?? null;

/** Разбор страницы t.me/s/<канал>. Возвращает посты страницы по возрастанию id. */
export function parseTmePage(html: string, handle: string): FetchedPost[] {
  const out: FetchedPost[] = [];
  const chunks = html.split(/(?=<div class="tgme_widget_message_wrap)/);
  for (const c of chunks) {
    const dp = c.match(/data-post="([^"/]+)\/(\d+)"/);
    if (!dp) continue;
    if (/tgme_widget_message_service/.test(c)) continue; // «канал закрепил сообщение» и т.п.
    const messageId = Number(dp[2]);
    const dt = c.match(/<time[^>]*datetime="([^"]+)"/)?.[1];
    const postedAt = dt ? Date.parse(dt) : NaN;
    if (!Number.isFinite(postedAt)) continue;
    // текст поста — блок js-message_text, но не цитата ответа (js-message_reply_text)
    const tm = c.match(/<div class="tgme_widget_message_text[^"]*js-message_text[^"]*"[^>]*>([\s\S]*?)<\/div>/);
    const textHtml = tm?.[1] ?? "";
    const text = htmlToText(textHtml);
    const links = new Set<string>();
    for (const m of textHtml.matchAll(/<a[^>]*href="([^"]+)"/g)) {
      const u = decodeEntities(m[1]);
      if (/^https?:\/\//.test(u) && !/^https?:\/\/t\.me\/s?\/?[^/]+\/?$/.test(u)) links.add(u);
    }
    const lp = c.match(/<a class="tgme_widget_message_link_preview"[^>]*href="([^"]+)"/)?.[1];
    if (lp) links.add(decodeEntities(lp));
    const photos: RawPhoto[] = [];
    for (const m of c.matchAll(/class="tgme_widget_message_photo_wrap[^"]*"[^>]*style="([^"]+)"/g)) {
      const u = bgUrl(decodeEntities(m[1]));
      if (u) photos.push({ kind: "photo", url: u });
    }
    for (const m of c.matchAll(/class="tgme_widget_message_video_thumb"[^>]*style="([^"]+)"/g)) {
      const u = bgUrl(decodeEntities(m[1]));
      if (u) photos.push({ kind: "video", url: u });
    }
    const views = parseViews(c.match(/<span class="tgme_widget_message_views">([^<]+)<\/span>/)?.[1]);
    if (!text && !photos.length && !links.size) continue;
    out.push({ post: { channel: handle, messageId, postedAt, text, views, links: [...links], hasMedia: photos.length > 0 }, photos });
  }
  return out.sort((a, b) => a.post.messageId - b.post.messageId);
}

/* ---------------- чтение канала ---------------- */

export interface WebReaderOptions {
  fetch?: typeof fetch;
  /** максимум страниц назад за один проход (по ~20 постов) */
  maxPages?: number;
  /** пауза между страницами, мс — не долбить t.me */
  pageDelayMs?: number;
  sleep?: (ms: number) => Promise<void>;
}

export function webReader(o: WebReaderOptions = {}): ChannelReader {
  const f = o.fetch ?? fetch;
  const maxPages = o.maxPages ?? 5;
  const sleep = o.sleep ?? ((ms: number) => new Promise((r) => setTimeout(r, ms)));
  const get = async (handle: string, before?: number) => {
    const url = `https://t.me/s/${encodeURIComponent(handle)}${before ? `?before=${before}` : ""}`;
    const res = await f(url, { headers: { "user-agent": "Mozilla/5.0 (STV terminal reader)", "accept-language": "en" } });
    if (!res.ok) throw new Error(`t.me HTTP ${res.status} для @${handle}`);
    const html = await res.text();
    if (!/tgme_channel_history|tgme_widget_message_wrap/.test(html)) throw new ChannelUnavailableError(handle, "у канала нет веб-превью (или он закрытый) — нужен MTProto-читатель");
    return parseTmePage(html, handle);
  };
  return {
    name: "web",
    async fetchSince(handle, sinceId, { backfill, maxPosts }) {
      const all = new Map<number, FetchedPost>();
      let page = await get(handle);
      for (let i = 0; ; i++) {
        page.forEach((p) => all.set(p.post.messageId, p));
        const minId = page.length ? page[0].post.messageId : null;
        const enough = sinceId == null ? all.size >= backfill : minId == null || minId <= sinceId + 1;
        if (enough || !minId || i + 1 >= maxPages || all.size >= maxPosts) break;
        await sleep(o.pageDelayMs ?? 1200);
        page = await get(handle, minId);
        if (!page.length) break;
      }
      let list = [...all.values()].sort((a, b) => a.post.messageId - b.post.messageId);
      list = sinceId == null ? list.slice(-backfill) : list.filter((p) => p.post.messageId > sinceId);
      return list.slice(-maxPosts);
    },
  };
}
