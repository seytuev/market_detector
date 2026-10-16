/**
 * Разбор поста Telegram моделью — по той же схеме, что в Инфополе, плюс фонды/инвесторы и ссылки на X.
 *
 * Маршрутизация модели:
 *  - обычные каналы → ChatGPT (OpenAI), модель LLM_TG_MODEL (по умолчанию gpt-4.1-mini — дёшево и быстро);
 *  - каналы со звёздочкой и любой пост со ссылкой на x.com / twitter.com → Grok (xAI), который читает твит сам.
 *    Для чтения твита переиспользовать клиент Grok X Search, уже сделанный в Инфополе.
 * Модель ничего не считает про цены: цены и изменение после поста считает priceCheck.ts по MarketProvider.
 */

import { pickForVision, mediaCaptions, type TgMedia } from "./media";

export interface TgPost {
  channel: string;
  messageId: number;
  postedAt: number; // ms
  text: string;
  views?: number;
  links: string[];
  hasMedia?: boolean;
}

export interface TgAnalysis {
  title_ru: string; // заголовок сюжета, до 90 символов
  summary_ru: string; // 1–2 строки
  what_happened_ru: string; // «что произошло» простым языком, 2–5 предложений, с переводом
  why_matters_ru: string; // почему это может двинуть цену, 1–3 предложения; "" если не двинет
  check_ru: string[]; // что проверить (до 3 пунктов)
  tickers: { symbol: string; name?: string; role: "главный" | "упомянут"; type?: "crypto" | "equity" }[];
  entities: { name: string; kind: "fund" | "exchange" | "project" | "person" | "company"; action?: string }[]; // Multicoin → fund, «инвестировал в Grass»
  category: "investment" | "listing" | "unlock" | "partnership" | "product" | "onchain" | "macro" | "narrative" | "meme" | "other";
  sentiment: "bullish" | "bearish" | "neutral";
  impact: 1 | 2 | 3 | 4 | 5;
  event: { title: string; date: string | null; date_text?: string } | null;
  x_links: string[];
  unclear: string[];
  confidence: "low" | "medium" | "high";
  /** что на картинках поста/твита — по номеру картинки [1], [2]… (пусто, если картинок нет) */
  media: MediaReading[];
}

export interface MediaReading {
  index: number; // номер картинки, с 1
  kind: "chart" | "tweet_screenshot" | "dashboard" | "table" | "photo" | "other";
  text_ru: string; // что на скрине, 1–2 предложения: актив, таймфрейм, уровни, цифры
  tickers: string[];
}

/** JSON Schema для structured output (OpenAI response_format / xAI). */
export const TG_ANALYSIS_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["title_ru", "summary_ru", "what_happened_ru", "why_matters_ru", "check_ru", "tickers", "entities", "category", "sentiment", "impact", "event", "x_links", "unclear", "confidence", "media"],
  properties: {
    title_ru: { type: "string" },
    summary_ru: { type: "string" },
    what_happened_ru: { type: "string" },
    why_matters_ru: { type: "string" },
    check_ru: { type: "array", items: { type: "string" } },
    tickers: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["symbol", "name", "role", "type"],
        properties: { symbol: { type: "string" }, name: { type: "string" }, role: { type: "string", enum: ["главный", "упомянут"] }, type: { type: "string", enum: ["crypto", "equity"] } },
      },
    },
    entities: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["name", "kind", "action"],
        properties: { name: { type: "string" }, kind: { type: "string", enum: ["fund", "exchange", "project", "person", "company"] }, action: { type: "string" } },
      },
    },
    category: { type: "string", enum: ["investment", "listing", "unlock", "partnership", "product", "onchain", "macro", "narrative", "meme", "other"] },
    sentiment: { type: "string", enum: ["bullish", "bearish", "neutral"] },
    impact: { type: "integer", enum: [1, 2, 3, 4, 5] },
    event: {
      anyOf: [
        { type: "null" },
        { type: "object", additionalProperties: false, required: ["title", "date", "date_text"], properties: { title: { type: "string" }, date: { type: ["string", "null"] }, date_text: { type: "string" } } },
      ],
    },
    x_links: { type: "array", items: { type: "string" } },
    unclear: { type: "array", items: { type: "string" } },
    confidence: { type: "string", enum: ["low", "medium", "high"] },
    media: {
      type: "array",
      items: {
        type: "object",
        additionalProperties: false,
        required: ["index", "kind", "text_ru", "tickers"],
        properties: {
          index: { type: "integer" },
          kind: { type: "string", enum: ["chart", "tweet_screenshot", "dashboard", "table", "photo", "other"] },
          text_ru: { type: "string" },
          tickers: { type: "array", items: { type: "string" } },
        },
      },
    },
  },
} as const;

export const SYSTEM_PROMPT = `Ты аналитик крипто-терминала STV. Разбираешь пост из Telegram-канала и объясняешь по-русски, что произошло.
Правила:
- Переводи с корейского/английского. Пиши просто, без воды.
- Тикеры: только те, что явно есть в посте ($GRASS) или однозначно следуют из названия проекта (Grass → GRASS). Не выдумывай.
- Акции (HOOD, NVDA, COIN, CRCL) помечай type="equity".
- Фонды и инвесторы (Multicoin, a16z, Binance Labs…) — в entities с kind="fund" и action (например «инвестировал в Grass»).
- why_matters_ru: почему это может повлиять на цену. Если не влияет — пустая строка.
- check_ru: что стоит проверить трейдеру (токеномика, анлоки, объём, реакция цены).
- Если пост — только ссылка на твит, опирайся на содержимое твита (если оно передано).
- Всё непонятное — в unclear. Ничего не додумывай. Цифры цены не придумывай.
- Картинки: если приложены — опиши каждую в media (index = номер [N]): что это (график, скрин твита, дашборд, таблица), какой актив и таймфрейм, какие уровни/цифры видны. Цифры со скрина переписывай точно, как видно; нечитаемое — в unclear. Тикеры со скрина добавляй и в tickers. Мем-картинки без информации — kind="photo", коротко.`;

export function buildUserMessage(p: TgPost, channelTitle: string, extra?: { tweetText?: string; linkedPageText?: string; mediaCaptions?: string }): string {
  return [
    `Канал: ${channelTitle} (@${p.channel})`,
    `Время: ${new Date(p.postedAt).toISOString()}`,
    `Текст поста:\n${p.text.slice(0, 6000)}`,
    p.links.length ? `Ссылки: ${p.links.join(" ")}` : "",
    extra?.tweetText ? `Содержимое твита по ссылке:\n${extra.tweetText.slice(0, 4000)}` : "",
    extra?.linkedPageText ? `Содержимое страницы по ссылке:\n${extra.linkedPageText.slice(0, 4000)}` : "",
    extra?.mediaCaptions ? `Приложены картинки:\n${extra.mediaCaptions}` : "",
  ]
    .filter(Boolean)
    .join("\n\n");
}

export const X_LINK = /https?:\/\/(?:www\.)?(?:x|twitter)\.com\/[A-Za-z0-9_]+\/status\/\d+/g;
export const extractXLinks = (p: TgPost) => [...new Set([...p.links, p.text].join(" ").match(X_LINK) ?? [])];

/** Кто разбирает пост. */
export function pickModel(p: TgPost, starred: boolean): "grok" | "chatgpt" {
  return starred || extractXLinks(p).length > 0 ? "grok" : "chatgpt";
}

/** Жёсткая проверка ответа модели (на случай, если провайдер не поддерживает strict-схему). */
export function validateAnalysis(x: any): { ok: true; value: TgAnalysis } | { ok: false; error: string } {
  const req = TG_ANALYSIS_SCHEMA.required.filter((k) => k !== "media");
  for (const k of req) if (!(k in (x ?? {}))) return { ok: false, error: `нет поля ${k}` };
  if (typeof x.title_ru !== "string" || !x.title_ru.trim()) return { ok: false, error: "пустой title_ru" };
  if (!Array.isArray(x.tickers)) return { ok: false, error: "tickers не массив" };
  if (![1, 2, 3, 4, 5].includes(x.impact)) return { ok: false, error: "impact вне 1–5" };
  if (!Array.isArray(x.media)) x.media = [];
  x.tickers = x.tickers
    .map((t: any) => ({ ...t, symbol: String(t.symbol ?? "").replace(/^\$/, "").toUpperCase().trim() }))
    .filter((t: any) => /^[A-Z0-9]{2,12}$/.test(t.symbol));
  return { ok: true, value: x as TgAnalysis };
}

/* ---------------- клиенты моделей ---------------- */

export interface LlmClient {
  /** images — URL (публичный или data:URL) картинок в порядке [1], [2]… */
  analyze(system: string, user: string, images?: string[], schema?: JsonSchemaSpec): Promise<unknown>;
}

/** Схема ответа: по умолчанию — разбор поста (TG_ANALYSIS_SCHEMA); торговый вывод передаёт свою. */
export interface JsonSchemaSpec {
  name: string;
  schema: Record<string, unknown>;
}
const DEFAULT_SCHEMA: JsonSchemaSpec = { name: "tg_analysis", schema: TG_ANALYSIS_SCHEMA as unknown as Record<string, unknown> };

/** Сообщение пользователя: текст + картинки (формат OpenAI; xAI принимает тот же). */
export function userContent(user: string, images: string[] = [], detail: "low" | "high" | "auto" = "auto") {
  if (!images.length) return user;
  return [{ type: "text", text: user }, ...images.map((url) => ({ type: "image_url", image_url: { url, detail } }))];
}

/** OpenAI Chat Completions со structured output. */
export function openAiClient(apiKey: string, model = "gpt-4.1-mini", f: typeof fetch = fetch, imageDetail: "low" | "high" | "auto" = "auto"): LlmClient {
  return {
    async analyze(system, user, images, schema = DEFAULT_SCHEMA) {
      const res = await f("https://api.openai.com/v1/chat/completions", {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${apiKey}` },
        body: JSON.stringify({
          model,
          temperature: 0.2,
          messages: [
            { role: "system", content: system },
            { role: "user", content: userContent(user, images, imageDetail) },
          ],
          response_format: { type: "json_schema", json_schema: { name: schema.name, strict: true, schema: schema.schema } },
        }),
      });
      if (!res.ok) throw new Error(`OpenAI HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
      const j: any = await res.json();
      return JSON.parse(j.choices?.[0]?.message?.content ?? "{}");
    },
  };
}

/**
 * xAI (Grok) — API совместим с OpenAI Chat Completions (https://api.x.ai/v1).
 * Чтение твитов: подставить в tweetText результат существующей функции Grok X Search из Инфополя
 * (или включить её инструменты поиска в этом же вызове — как сделано там).
 */
export function xAiClient(apiKey: string, model = "grok-4", f: typeof fetch = fetch, imageDetail: "low" | "high" | "auto" = "auto"): LlmClient {
  return {
    async analyze(system, user, images, schema = DEFAULT_SCHEMA) {
      const res = await f("https://api.x.ai/v1/chat/completions", {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${apiKey}` },
        body: JSON.stringify({
          model,
          temperature: 0.2,
          messages: [
            { role: "system", content: system },
            { role: "user", content: userContent(user, images, imageDetail) },
          ],
          response_format: { type: "json_schema", json_schema: { name: schema.name, strict: true, schema: schema.schema } },
        }),
      });
      if (!res.ok) throw new Error(`xAI HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
      const j: any = await res.json();
      return JSON.parse(j.choices?.[0]?.message?.content ?? "{}");
    },
  };
}

/**
 * Anthropic (Claude Opus и др.) — Messages API. Структурированный ответ — через принудительный вызов инструмента:
 * модель обязана вернуть input по нашей JSON-схеме. Картинки: data:URL → base64-блок, https → url-блок.
 * Ключ ANTHROPIC_API_KEY — только в серверном .env. Модель — LLM_DEEP_MODEL (точное имя — из консоли Anthropic).
 */
export function anthropicClient(apiKey: string, model: string, f: typeof fetch = fetch, maxTokens = 2000): LlmClient {
  return {
    async analyze(system, user, images = [], schema = DEFAULT_SCHEMA) {
      const imgs = images.map((u) => {
        const m = u.match(/^data:(image\/[a-z+]+);base64,(.*)$/i);
        return m ? { type: "image", source: { type: "base64", media_type: m[1], data: m[2] } } : { type: "image", source: { type: "url", url: u } };
      });
      const res = await f("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: { "content-type": "application/json", "x-api-key": apiKey, "anthropic-version": "2023-06-01" },
        body: JSON.stringify({
          model,
          max_tokens: maxTokens,
          temperature: 0.2,
          system,
          messages: [{ role: "user", content: [...imgs, { type: "text", text: user }] }],
          tools: [{ name: schema.name, description: "Верни ответ строго по схеме", input_schema: schema.schema }],
          tool_choice: { type: "tool", name: schema.name },
        }),
      });
      if (!res.ok) throw new Error(`Anthropic HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
      const j: any = await res.json();
      const block = (j.content ?? []).find((b: any) => b.type === "tool_use");
      if (!block) throw new Error("Anthropic: нет ответа по схеме");
      return block.input;
    },
  };
}

/**
 * Сильная модель для шага 2 из .env:
 *   LLM_DEEP_PROVIDER=anthropic | openai,  LLM_DEEP_MODEL=<имя модели>
 * Нет ключа — шаг 2 выключен (вывод не пишется, лента работает как раньше).
 */
export function deepClientFromEnv(env: Record<string, string | undefined>, f: typeof fetch = fetch): { client: LlmClient; model: string } | null {
  const provider = env.LLM_DEEP_PROVIDER ?? (env.ANTHROPIC_API_KEY ? "anthropic" : "openai");
  const model = env.LLM_DEEP_MODEL;
  if (!model) return null;
  if (provider === "anthropic" && env.ANTHROPIC_API_KEY) return { client: anthropicClient(env.ANTHROPIC_API_KEY, model, f), model };
  if (provider === "openai" && env.OPENAI_API_KEY) return { client: openAiClient(env.OPENAI_API_KEY, model, f, "high"), model };
  return null;
}

export interface AnalyzeDeps {
  chatgpt: LlmClient;
  grok?: LlmClient; // если нет ключа xAI — всё идёт в ChatGPT
  /** существующий Grok X Search из Инфополя: ссылка на твит → текст твита и треда */
  readTweet?: (url: string) => Promise<string | null>;
  /** картинки поста и твитов (media.ts): уже сохранённые; уходят в модель через pickForVision */
  media?: TgMedia[];
  /** свой URL → то, что можно отдать модели (публичный URL или data:URL из MediaStore) */
  imageUrl?: (m: TgMedia) => Promise<string>;
}

/** Полный разбор одного поста: выбор модели → чтение твитов → вызов → проверка → один повтор при ошибке. */
export async function analyzePost(p: TgPost, ch: { title: string; starred: boolean }, d: AnalyzeDeps): Promise<{ model: "grok" | "chatgpt"; analysis: TgAnalysis; mediaIds: string[] }> {
  const want = pickModel(p, ch.starred);
  const model = want === "grok" && d.grok ? "grok" : "chatgpt";
  let tweetText: string | undefined;
  const xl = extractXLinks(p);
  if (xl.length && d.readTweet) tweetText = (await Promise.all(xl.slice(0, 2).map((u) => d.readTweet!(u).catch(() => null)))).filter(Boolean).join("\n---\n") || undefined;
  const client = model === "grok" ? d.grok! : d.chatgpt;
  const picked = pickForVision(d.media ?? []);
  const urls = await Promise.all(picked.map((m) => (d.imageUrl ? d.imageUrl(m) : Promise.resolve(m.thumbUrl ?? m.url)).catch(() => null)));
  const vis = picked.filter((_, i) => urls[i]);
  const images = urls.filter((u): u is string => !!u);
  const user = buildUserMessage(p, ch.title, { tweetText, mediaCaptions: images.length ? mediaCaptions(vis) : undefined });
  let last = "";
  for (let attempt = 0; attempt < 2; attempt++) {
    const raw = await client.analyze(SYSTEM_PROMPT + (last ? `\nПредыдущий ответ отклонён: ${last}. Верни строго JSON по схеме.` : ""), user, images);
    const v = validateAnalysis(raw);
    if (v.ok) return { model, mediaIds: vis.map((m) => m.id), analysis: { ...v.value, x_links: [...new Set([...(v.value.x_links ?? []), ...xl])] } };
    last = v.error;
  }
  throw new Error(`разбор не прошёл проверку: ${last}`);
}
