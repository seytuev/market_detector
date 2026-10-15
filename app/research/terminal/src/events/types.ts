/**
 * Эвенты (Catalyst Calendar) — общие типы.
 *
 * Поток данных по всему терминалу:
 *   источники (API, X-аккаунты, Telegram Feed, Инфополе, ручной ввод)
 *     → EventCandidate (кандидат: что, когда, тикеры, откуда)
 *     → merge.ts: склейка с уже известными событиями (одно событие — много источников и контекста)
 *     → CalEvent в базе → calendarView.ts (страница «Эвенты») и toDashboard.ts (командный центр)
 *
 * Время храним в UTC (startsAt, мс), показываем в часовом поясе пользователя.
 */

export type EventGroup = "crypto" | "unlock" | "macro" | "stocks" | "conference" | "derivatives";

export type EventKind =
  | "unlock" // разлок токенов
  | "listing" // листинг / делистинг
  | "launch" // запуск продукта, мейннет, TGE, airdrop
  | "upgrade" // хардфорк, апгрейд сети
  | "governance" // голосование
  | "tokenomics" // сжигание, байбэк, смена эмиссии
  | "partnership"
  | "conference" // конференции, саммиты, AMA, хакатоны
  | "sale" // токенсейл, публичный раунд
  | "macro" // FOMC, CPI, GDP, NFP, решения ЦБ
  | "earnings" // отчёты компаний
  | "expiry" // экспирации опционов/фьючерсов
  | "deadline" // дедлайны (выплаты Mt.Gox, окончание подписки и т.п.)
  | "other";

export const KIND_GROUP: Record<EventKind, EventGroup> = {
  unlock: "unlock", listing: "crypto", launch: "crypto", upgrade: "crypto", governance: "crypto", tokenomics: "crypto", partnership: "crypto",
  conference: "conference", sale: "crypto", macro: "macro", earnings: "stocks", expiry: "derivatives", deadline: "crypto", other: "crypto",
};

export type SourceKind = "tokenomist" | "coinmarketcal" | "dropstab" | "fomc" | "fred" | "finnhub" | "deribit" | "x" | "telegram" | "infopole" | "manual";

/** Структурные источники — их даты считаем подтверждёнными. */
export const STRUCTURED: SourceKind[] = ["tokenomist", "coinmarketcal", "dropstab", "fomc", "fred", "finnhub", "deribit", "manual"];

export type Precision = "exact" | "day" | "week" | "month" | "quarter" | "unknown";

export interface EventSource {
  kind: SourceKind;
  /** id записи у источника (tweet id, story id, token id + дата …) */
  ref: string;
  url?: string | null;
  author?: string | null;
  /** когда источник это опубликовал */
  at: number;
  /** что именно сказал источник (одна-две строки) */
  text?: string | null;
  /** дата, которую назвал этот источник (для поиска расхождений) */
  date?: string | null;
}

export interface ContextEntry {
  at: number;
  source: SourceKind;
  author?: string | null;
  text: string;
  url?: string | null;
}

/** Кандидат в событие — выход любого источника до склейки. */
export interface EventCandidate {
  title: string;
  kind: EventKind;
  /** YYYY-MM-DD в UTC; null — дата не известна */
  date: string | null;
  /** HH:MM в UTC; null — время не известно */
  time: string | null;
  precision: Precision;
  /** для конференций и многодневных событий */
  endDate?: string | null;
  tickers: string[];
  projects: string[];
  /** 1–3, если источник знает (иначе посчитает importance.ts) */
  importance?: 1 | 2 | 3;
  valueUsd?: number | null;
  /** % от рыночной капитализации (для разлоков) */
  pctOfMcap?: number | null;
  allocation?: string | null;
  /** страна для макро (флаг): US, EU, RU, CN, JP … */
  country?: string | null;
  /** лого/иконка, если источник дал (CoinGecko/CMC image url) */
  icon?: string | null;
  summary?: string | null;
  confidence: "low" | "medium" | "high";
  source: EventSource;
}

export type EventStatus = "verified" | "review" | "undated" | "cancelled" | "hidden";

export interface CalEvent {
  id: string;
  title: string;
  kind: EventKind;
  group: EventGroup;
  date: string | null;
  time: string | null;
  /** мс UTC; для событий без времени — 00:00 UTC даты */
  startsAt: number | null;
  endDate: string | null;
  precision: Precision;
  tickers: string[];
  projects: string[];
  importance: 1 | 2 | 3;
  status: EventStatus;
  valueUsd: number | null;
  pctOfMcap: number | null;
  allocation: string | null;
  country: string | null;
  icon: string | null;
  /** короткое описание (из источника или от модели) */
  summary: string | null;
  /** «что это значит и за чем следить» — пишет модель, когда добавляется источник */
  context: string | null;
  contextModel: string | null;
  /** по скольким источникам написан context (если источников стало больше — переписать) */
  contextSources: number;
  sources: EventSource[];
  /** лента контекста: каждый источник, что он сказал */
  timeline: ContextEntry[];
  /** источники называют разные даты */
  conflicts: { date: string; source: SourceKind; author?: string | null }[];
  /** закреплено в полосе отсчёта сверху */
  pinned: boolean;
  /** пользователь подтвердил / поправил */
  userConfirmed: boolean;
  createdAt: number;
  updatedAt: number;
}

/** Хранилище событий (таблица cal_events + cal_event_sources). */
export interface EventStore {
  all(range?: { from: string; to: string }): Promise<CalEvent[]>;
  upsert(e: CalEvent): Promise<void>;
  /** уже обработанные ref источников — чтобы не разбирать пост дважды */
  seen(kind: SourceKind, ref: string): Promise<boolean>;
  markSeen(kind: SourceKind, ref: string): Promise<void>;
}
