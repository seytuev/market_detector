/**
 * «Учебник сигналов» — как научить ИИ находить такие же идеи, как те, что ты отметил сам.
 *
 * Это не дообучение весов модели. Это обучение на примерах (in-context learning + поиск похожих):
 *  1. В карточке Инфополя / Telegram Feed ставишь галочку «Обучить на этом»: выбираешь паттерн
 *     («Weekend pump», «Второй шанс после ралли»…), направление и пишешь заметку — почему это сигнал.
 *     Пост, разбор ИИ, описания скринов, цена в момент сохранения и твоя заметка ложатся в учебник.
 *  2. По примерам паттерна сильная модель пишет «правило паттерна»: суть, признаки, анти-признаки,
 *     направление, горизонт, что проверить. Правило видно и правится руками.
 *  3. Новые посты из X / Telegram Feed / Инфополя проверяются против активных паттернов: дешёвая модель
 *     получает правило, 2 ближайших примера, 1 анти-пример и твои общие правила — и решает, похоже ли.
 *  4. Найденное — только предложение («Похоже на „Weekend pump“ · 82%»). Кнопки «Верно» / «Мимо»
 *     добавляют пост в учебник примером или анти-примером — так учебник уточняется со временем.
 *  5. Цена проверяет и примеры, и находки (1/3/7 дней) — видно, какой паттерн реально работает.
 *
 * Ничего не делается само: ни идей, ни сделок. Идея создаётся только кнопкой в источнике.
 *
 * v2:
 *  - СТЕЗИ (track): паттерн учит не только «лонг/шорт». Трейд-сигнал, События → календарь, Нарратив, Внимание, Риск.
 *    От стези зависит, что предлагает находка: направление, «добавить события из поста», пометку нарратива и т.д.
 *  - УПОР (focus): в каждом примере ты отмечаешь, на что смотреть — автор, формулировка, график, тикер, дата/событие,
 *    цифры или всё вместе — и пишешь своими словами. Модель держит упор в приоритете и при правилах, и при поиске.
 *  - Все источники информационного потока: Инфополе, Reflexion Radar, Telegram Feed, X Feed, Article Feed, Video Feed, Tickers.
 *    Карточку можно перетащить на «Обучение сигналам» в сайдбаре или нажать «★ В учебник».
 */

export type Direction = "long" | "short" | "watch" | "not_signal";
export type SourceKind = "infopole" | "telegram" | "x" | "radar" | "article" | "video" | "ticker";
export const SOURCE_KINDS: SourceKind[] = ["infopole", "radar", "telegram", "x", "article", "video", "ticker"];

/** Стезя: чему учится паттерн и что предлагает находка. */
export type Track = "trade" | "events" | "narrative" | "attention" | "risk";
export const TRACKS: Track[] = ["trade", "events", "narrative", "attention", "risk"];

/** На что упор в примере. */
export type FocusField = "author" | "wording" | "chart" | "ticker" | "event" | "numbers" | "whole";
export const FOCUS_FIELDS: FocusField[] = ["author", "wording", "chart", "ticker", "event", "numbers", "whole"];
export interface Focus {
  fields: FocusField[];
  /** своими словами: «посты @TheDeFinvestor со списком на неделю → всё в эвенты» */
  note: string;
}
export type Horizon = "1-3d" | "1-2w" | "1m+";

/** Снимок источника — всё, что ИИ видел о посте. Одинаковый для Инфополя, Telegram и X. */
export interface SourceSnapshot {
  kind: SourceKind;
  /** id карточки Инфополя / channel:messageId / id твита */
  ref: string;
  /** внешняя ссылка (x.com, t.me) */
  url: string | null;
  /** внутренняя ссылка в терминале (карточка Инфополя, сюжет Telegram) — там же создаётся идея */
  href: string | null;
  author: string | null;
  at: number;
  /** оригинал поста / твита */
  text: string;
  /** заголовок разбора ИИ */
  title: string;
  /** разбор ИИ коротко (что произошло, почему важно) */
  summary: string;
  tickers: string[];
  /** главный тикер (по разбору) */
  symbol: string | null;
  /** что на скринах (из разбора) */
  imageNotes: string[];
  /** картинки (URL или data:URL) — для сравнения графиков */
  images: string[];
  /**
   * v4: факты терминала на момент поста — считает КОД, не модель (src/learn/facts.ts):
   * тикер в портфеле / корзине / идеях, история автора в учебнике. Сохраняется вместе с примером,
   * поэтому заметка «токен уже в корзине» и «хороший автор» превращаются в проверяемые признаки.
   */
  facts?: SnapshotFacts;
}

/** v4: что терминал знает о посте (точные данные, не мнение модели). */
export interface SnapshotFacts {
  tickers: { symbol: string; portfolios: string[]; baskets: string[]; ideas: string[] }[];
  /** хоть один тикер поста уже «наш»: в портфеле, корзине или идеях */
  known: boolean;
  /** автор: нормализованный ключ и его история в учебнике */
  author: { key: string; name: string; signals: number; anti: number; right: number; wrong: number } | null;
  /** сколько других источников упоминали главный тикер за 24 ч до поста (если считается) */
  mentions24h?: number | null;
  at: number;
}

/**
 * v4: условия паттерна, которые проверяет КОД (точно и бесплатно), до и после модели.
 *  require — без условия пост даже не отправляется модели по этому паттерну;
 *  prefer  — желательно: есть → +5 к уверенности, нет → −5;
 *  off     — выключено.
 * origin: auto — предложено по примерам и заметкам (обновляется само), manual — поставлено тобой (само не меняется).
 */
export type ConditionMode = "require" | "prefer" | "off";
export type Condition = (
  | { kind: "author_in"; authors: string[] }
  | { kind: "author_track"; minSignals: number }
  | { kind: "ticker_known" }
  | { kind: "ticker_in_portfolio" }
  | { kind: "ticker_in_basket" }
  | { kind: "ticker_has_idea" }
  | { kind: "has_ticker" }
  | { kind: "has_image" }
  | { kind: "words_any"; words: string[] }
) & { mode: ConditionMode; origin: "auto" | "manual" };

export interface PatternRule {
  summary_ru: string;
  signs_ru: string[];
  anti_signs_ru: string[];
  direction: Exclude<Direction, "not_signal">;
  horizon: Horizon;
  check_ru: string[];
  /** v4: что модель вычитала из заметок — важен ли автор, «наш» ли тикер, нужен ли скрин (→ условия кода) */
  flags?: { author: boolean; known_ticker: boolean; image: boolean };
}

export interface Pattern {
  id: string;
  name: string;
  /** стезя; у паттернов v1 нет поля — считать "trade" (trackOf) */
  track?: Track;
  /** веса признаков правила по тексту признака (weights.ts); нет — все 1 */
  weights?: Record<string, number>;
  /** learning — копит примеры (сканер не ищет); active — ищет похожие; paused — выключен */
  status: "learning" | "active" | "paused";
  rule: PatternRule | null;
  /** правило поправлено руками — ИИ больше не перезаписывает его, а предлагает обновление */
  ruleEdited: boolean;
  proposedRule: PatternRule | null;
  /** когда правило последний раз писалось моделью (примеры новее → правило пора обновить) */
  ruleAt: number | null;
  /** версия правила: растёт при каждом изменении (находки помнят, по какой версии найдены) */
  version: number;
  /** порог уверенности для показа находки, 0–100 */
  minConfidence: number;
  sources: SourceKind[];
  /** v4: условия, которые проверяет код (conditions.ts) */
  conditions?: Condition[];
  /** v4: последняя проверка на своих примерах (eval.ts) */
  lastEval?: PatternEval | null;
  createdAt: number;
  updatedAt: number;
}

/** v4: «научился ли» — прогон правила по твоим же размеченным примерам, каждый раз без самого примера. */
export interface PatternEval {
  at: number;
  version: number;
  /** сигналы, которые правило узнало / пропустило; анти, которые правило ошибочно приняло / верно отсекло */
  tp: number;
  fn: number;
  fp: number;
  tn: number;
  precision: number | null;
  recall: number | null;
  misses: { exampleId: string; title: string; expected: "signal" | "anti"; got: "match" | "no_match"; why: string }[];
}

export interface Example {
  id: string;
  patternId: string;
  /** not_signal — анти-пример: «похоже, но не то» */
  direction: Direction;
  note: string;
  /** на что упор (v2); у примеров v1 нет — считать null (focusOf) */
  focus?: Focus | null;
  source: SourceSnapshot;
  /** цена главного тикера в момент поста (если известна) и в момент сохранения */
  priceAtPost: number | null;
  /** откуда пример: галочка в карточке или кнопка «Верно/Мимо» на находке */
  origin: "manual" | "match_right" | "match_wrong";
  createdAt: number;
  createdBy: string;
  /** последняя правка в журнале (v3): правило переписывается, если правки новее правила */
  updatedAt?: number;
}

/**
 * v3: «Журнал сигналов» — сигнал переслан, но ещё не размечен (быстрая пересылка без паттерна).
 * В обучение не идёт, пока ты не поставишь разметку «Сигнал» / «Анти» — тогда становится примером (Example).
 */
export interface InboxItem {
  id: string;
  source: SourceSnapshot;
  /** паттерн можно выбрать заранее, а разметить позже */
  patternId: string | null;
  note: string;
  focus: Focus | null;
  priceAtPost: number | null;
  createdAt: number;
  createdBy: string;
  updatedAt: number;
}

export interface Match {
  id: string;
  patternId: string;
  patternVersion: number;
  source: SourceSnapshot;
  symbol: string | null;
  direction: Exclude<Direction, "not_signal">;
  confidence: number;
  why_ru: string;
  differs_ru: string;
  /** ближайшие примеры, на которые опиралась модель */
  exampleIds: string[];
  priceAtMatch: number | null;
  /** partial — «Частично»: близко, но не то; пример не создаётся, комментарий идёт в правило */
  status: "new" | "right" | "partial" | "wrong" | "dismissed";
  /** номера признаков правила, которые модель нашла в посте */
  signsHit?: number[];
  /** оценка модели до смешивания с весами; confidence = смешанная */
  modelConfidence?: number;
  /** уверенность по весам признаков, 0–100 */
  weightedScore?: number | null;
  /** твоё описание к находке: почему верно / что не так */
  comment?: string;
  createdAt: number;
  feedbackAt: number | null;
}

export interface PlaybookStore {
  patterns(): Promise<Pattern[]>;
  savePattern(p: Pattern): Promise<void>;
  examples(patternId?: string): Promise<Example[]>;
  saveExample(e: Example): Promise<void>;
  deleteExample(id: string): Promise<void>;
  matches(q?: { patternId?: string; status?: Match["status"]; sourceRef?: string }): Promise<Match[]>;
  saveMatch(m: Match): Promise<void>;
  /** общие правила пользователя («Мой подход») — идут в каждый запрос поиска */
  notes(): Promise<string>;
  saveNotes(t: string): Promise<void>;
  /** уже проверенные посты: kind:ref:версия набора паттернов */
  seen(key: string): Promise<boolean>;
  markSeen(key: string): Promise<void>;
  /** v3: неразмеченные пересланные сигналы (таблица pb_inbox) */
  inbox(): Promise<InboxItem[]>;
  saveInbox(i: InboxItem): Promise<void>;
  deleteInbox(id: string): Promise<void>;
}

export const DIRECTION_RU: Record<Direction, string> = { long: "Лонг", short: "Шорт", watch: "Наблюдать", not_signal: "Не сигнал" };
export const HORIZON_RU: Record<Horizon, string> = { "1-3d": "1–3 дня", "1-2w": "1–2 недели", "1m+": "месяц+" };
export const SOURCE_RU: Record<SourceKind, string> = { infopole: "Инфополе", telegram: "Telegram Feed", x: "X Feed", radar: "Reflexion Radar", article: "Article Feed", video: "Video Feed", ticker: "Tickers" };
export const TRACK_RU: Record<Track, string> = { trade: "Трейд-сигнал", events: "События → календарь", narrative: "Нарратив", attention: "Внимание", risk: "Риск" };
/** что делает находка этой стези (подсказка в интерфейсе) */
export const TRACK_HINT: Record<Track, string> = {
  trade: "ищет похожие идеи и предлагает направление: лонг / шорт / наблюдать",
  events: "ищет посты с датами и предлагает добавить все события из поста в календарь и Daily Recap",
  narrative: "ищет посты того же нарратива и помечает их — видно, как нарратив набирает обороты",
  attention: "ищет похожие посты и поднимает их наверх — «на это обратить внимание»",
  risk: "ищет похожие тревожные сигналы (разлоки, взломы, продажи фондов) и предупреждает",
};
export const FOCUS_RU: Record<FocusField, string> = { author: "Автор", wording: "Формулировка", chart: "График / скрин", ticker: "Тикер", event: "Событие / дата", numbers: "Цифры", whole: "Всё вместе" };

export const trackOf = (p: Pick<Pattern, "track">): Track => (p.track && TRACKS.includes(p.track) ? p.track : "trade");
export const focusOf = (e: Pick<Example, "focus">): Focus | null => (e.focus && Array.isArray(e.focus.fields) ? e.focus : null);

export const hashId = (s: string) => {
  let h = 2166136261;
  for (const c of s) h = Math.imul(h ^ c.charCodeAt(0), 16777619) >>> 0;
  return h.toString(36);
};
