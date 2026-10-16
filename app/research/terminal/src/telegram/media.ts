import type { MediaReading } from "./analyze";
import type { Story } from "./stories";

/**
 * Картинки из постов и твитов: скрины графиков, дашбордов, твитов, таблицы.
 *
 * 1. Сборщик сохраняет медиа поста к себе (ссылки CDN Telegram живут недолго) → MediaStore.put → свой URL.
 * 2. Для ссылок на X медиа твита берутся из X API v2 (если подключён) или из публичного fxtwitter (без ключа).
 * 3. До 4 картинок уходят в модель вместе с текстом (ChatGPT и Grok оба видят изображения):
 *    модель описывает, что на скрине (график, уровни, цифры, тикеры) → analysis.media[].
 * 4. В интерфейсе: миниатюры в ленте, галерея в разборе, под каждой картинкой — «что на скрине».
 */

export interface TgMedia {
  id: string;
  source: "telegram" | "x";
  kind: "photo" | "video" | "gif";
  /** свой постоянный URL (после MediaStore.put) или прямой URL X */
  url: string;
  /** превью для видео/гиф — оно же уходит в модель */
  thumbUrl?: string;
  width?: number;
  height?: number;
  /** откуда: пост канала или твит по ссылке */
  post: { channel: string; messageId: number };
  tweetUrl?: string;
}

/** Хранилище файлов (S3/R2/локальная папка). */
export interface MediaStore {
  put(bytes: Uint8Array, mime: string, key: string): Promise<string>;
  /** data:URL для отправки в модель, если свой URL не публичный */
  dataUrl(url: string): Promise<string>;
}

/* ---------------- X: медиа твита ---------------- */

export const TWEET_ID = /(?:x|twitter)\.com\/([A-Za-z0-9_]+)\/status\/(\d+)/;

type Raw = { kind: TgMedia["kind"]; url: string; thumbUrl?: string; width?: number; height?: number };

/** Ответ X API v2: GET /2/tweets/:id?expansions=attachments.media_keys&media.fields=type,url,preview_image_url,width,height */
export function parseXApiMedia(j: any): Raw[] {
  return (j?.includes?.media ?? [])
    .map((m: any): Raw | null => {
      if (m.type === "photo" && m.url) return { kind: "photo", url: m.url, width: m.width, height: m.height };
      if ((m.type === "video" || m.type === "animated_gif") && m.preview_image_url)
        return { kind: m.type === "video" ? "video" : "gif", url: m.preview_image_url, thumbUrl: m.preview_image_url, width: m.width, height: m.height };
      return null;
    })
    .filter(Boolean) as Raw[];
}

/** Ответ https://api.fxtwitter.com/<user>/status/<id> (без ключа). */
export function parseFxTweet(j: any): Raw[] {
  const all: any[] = j?.tweet?.media?.all ?? [...(j?.tweet?.media?.photos ?? []), ...(j?.tweet?.media?.videos ?? [])];
  return all
    .map((m: any): Raw | null => {
      if (m.type === "photo" && m.url) return { kind: "photo", url: m.url, width: m.width, height: m.height };
      if ((m.type === "video" || m.type === "gif") && (m.thumbnail_url || m.url))
        return { kind: m.type, url: m.thumbnail_url ?? m.url, thumbUrl: m.thumbnail_url, width: m.width, height: m.height };
      return null;
    })
    .filter(Boolean) as Raw[];
}

/** Медиа твита: сначала X API (если есть токен), иначе fxtwitter. Ошибка → пустой список, разбор не падает. */
export async function fetchTweetMedia(tweetUrl: string, opts: { xBearer?: string; f?: typeof fetch } = {}): Promise<Raw[]> {
  const m = tweetUrl.match(TWEET_ID);
  if (!m) return [];
  const f = opts.f ?? fetch;
  const [, user, id] = m;
  try {
    if (opts.xBearer) {
      const r = await f(`https://api.x.com/2/tweets/${id}?expansions=attachments.media_keys&media.fields=type,url,preview_image_url,width,height`, { headers: { authorization: `Bearer ${opts.xBearer}` } });
      if (r.ok) return parseXApiMedia(await r.json());
    }
    const r = await f(`https://api.fxtwitter.com/${user}/status/${id}`);
    return r.ok ? parseFxTweet(await r.json()) : [];
  } catch {
    return [];
  }
}

/* ---------------- Telegram: если сборщик читает публичную страницу t.me/s/<канал> ---------------- */

/** Картинки одного сообщения из HTML t.me/s: .tgme_widget_message_photo_wrap … background-image:url('…'). */
export function extractTmeMedia(messageHtml: string): Raw[] {
  const out: Raw[] = [];
  const re = /class="[^"]*tgme_widget_message_(photo_wrap|video_thumb)[^"]*"[^>]*style="[^"]*background-image:url\('([^']+)'\)/g;
  for (const m of messageHtml.matchAll(re)) out.push(m[1] === "photo_wrap" ? { kind: "photo", url: m[2] } : { kind: "video", url: m[2], thumbUrl: m[2] });
  return out;
}

/* ---------------- что отправить в модель ---------------- */

/** До max картинок: сначала фото поста, потом из твитов; видео — только превью; без дублей. */
export function pickForVision(media: TgMedia[], max = 4): TgMedia[] {
  const seen = new Set<string>();
  return [...media]
    .sort((a, b) => (a.source === b.source ? 0 : a.source === "telegram" ? -1 : 1) || (a.kind === "photo" ? -1 : 1) - (b.kind === "photo" ? -1 : 1))
    .filter((m) => {
      const u = m.thumbUrl ?? m.url;
      if (seen.has(u)) return false;
      seen.add(u);
      return true;
    })
    .slice(0, max);
}

/** Подписи картинок для текста запроса: модель ссылается на них по номеру [1], [2]… */
export function mediaCaptions(media: TgMedia[]): string {
  return media.map((m, i) => `[${i + 1}] ${m.source === "telegram" ? "картинка из поста" : `картинка из твита ${m.tweetUrl ?? ""}`}${m.kind !== "photo" ? " (кадр видео)" : ""}`).join("\n");
}

/* ---------------- галерея сюжета ---------------- */

export interface GalleryItem extends TgMedia {
  /** что модель увидела на картинке (null — картинка не разбиралась, например 5-я по счёту) */
  reading: MediaReading | null;
  postedAt: number;
}

/** Все картинки сюжета по порядку постов, с описанием от модели. Дубли (один скрин в двух каналах) — один раз. */
export function storyGallery(s: Story): GalleryItem[] {
  const out: GalleryItem[] = [];
  const seen = new Set<string>();
  for (const p of [...s.posts].sort((a, b) => a.post.postedAt - b.post.postedAt)) {
    for (const m of p.media ?? []) {
      const key = m.tweetUrl ? m.tweetUrl + "|" + m.url : m.url;
      if (seen.has(key)) continue;
      seen.add(key);
      const i = (p.mediaIds ?? []).indexOf(m.id);
      const reading = i >= 0 ? p.analysis.media?.find((r) => r.index === i + 1) ?? null : null;
      out.push({ ...m, reading, postedAt: p.post.postedAt });
    }
  }
  return out;
}
