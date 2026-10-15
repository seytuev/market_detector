/**
 * Как из упоминания тикера (твит, заметка, ручной ввод) получить ПРАВИЛЬНЫЙ актив.
 *
 * Ошибки, которые это исправляет:
 *  - «$HOOD / Robinhood» из твита про акции превращался в DEX-мемкоин ROBINHOOD по $0.0029;
 *  - у всех идей стоял источник DEX, даже у NEAR/XMR/ENA, у которых есть листинг на CMC/CoinGecko;
 *  - цена сигнала расходилась с рынком в разы, и никто этого не замечал.
 *
 * Правила:
 *  1. Акция (assetClass = "equity" из разбора или известный тикер акции) → только Finnhub. В DEX не ищем.
 *  2. Крипта → сначала листинговые источники (CMC / CoinGecko / Binance). DEX — только если листинга нет.
 *  3. DEX-токен никогда не выбирается автоматически: пользователь подтверждает (сеть, адрес, ликвидность, капа).
 *     Исключение — в тексте был адрес контракта: тогда берём именно его.
 *  4. Несколько разных токенов с одним символом → needs_choice, ничего не подставляем.
 *  5. Цена сигнала сверяется с рынком: расхождение > 50% → флаг mismatch, идея не сохраняется без подтверждения.
 */
import type { MarketData } from "./marketData";
import type { AssetRefs, Quote, SearchResult } from "./types";

export interface Mention {
  symbol: string; // "ZEC", "$HOOD", "ROBINHOOD"
  name?: string; // «Robinhood», «Monero» — из разбора
  assetClass?: "crypto" | "equity" | "unknown";
  contract?: { chain: string; address: string };
  /** цена, которую назвал автор / зафиксировали как сигнал */
  signalPrice?: number;
}

export type Resolution =
  | { status: "resolved"; refs: AssetRefs; via: string; candidates: SearchResult[]; warnings: string[] }
  | { status: "needs_choice"; reason: string; candidates: SearchResult[]; warnings: string[] }
  | { status: "not_found"; reason: string; candidates: []; warnings: string[] };

/** Тикеры, которые в крипто-контексте почти всегда означают акцию. Дополняется из разбора (assetClass). */
export const KNOWN_EQUITIES = new Set(["HOOD", "NVDA", "COIN", "MSTR", "CRCL", "TSLA", "AAPL", "AMZN", "META", "GOOGL", "MSFT", "PLTR", "GME", "AMC", "SPY", "QQQ", "BMNR", "SBET", "GLXY", "CRWV"]);
/** Названия компаний → тикер акции */
const EQUITY_NAMES: Record<string, string> = { robinhood: "HOOD", nvidia: "NVDA", coinbase: "COIN", microstrategy: "MSTR", strategy: "MSTR", circle: "CRCL", tesla: "TSLA" };

const clean = (s: string) => s.trim().replace(/^\$/, "").toUpperCase();
const listed = (r: SearchResult) => r.provider === "cmc" || r.provider === "coingecko";

export function classify(m: Mention): "crypto" | "equity" {
  if (m.assetClass === "equity" || m.assetClass === "crypto") return m.assetClass;
  const sym = clean(m.symbol);
  if (KNOWN_EQUITIES.has(sym)) return "equity";
  const byName = EQUITY_NAMES[(m.name ?? sym).toLowerCase()];
  return byName ? "equity" : "crypto";
}

export async function resolveMention(md: MarketData, m: Mention): Promise<Resolution> {
  const warnings: string[] = [];
  const sym = clean(m.symbol);

  // 0. Адрес контракта — берём ровно его
  if (m.contract) return { status: "resolved", refs: { symbol: sym, dex: m.contract, assetClass: "crypto" }, via: "contract", candidates: [], warnings };

  // 1. Акции
  if (classify(m) === "equity") {
    const ticker = EQUITY_NAMES[(m.name ?? "").toLowerCase()] ?? EQUITY_NAMES[sym.toLowerCase()] ?? sym;
    const groups = await md.search(ticker);
    const eq = groups.flatMap((g) => g.items).filter((x) => x.provider === "finnhub" && x.symbol === ticker);
    if (eq.length === 1) return { status: "resolved", refs: { ...eq[0].refs, symbol: ticker }, via: "finnhub", candidates: eq, warnings };
    return { status: "not_found", reason: `акция ${ticker}: нет котировки (проверьте FINNHUB_API_KEY)`, candidates: [], warnings };
  }

  // 2. Крипта
  const g = (await md.search(sym)).find((x) => x.symbol === sym);
  if (!g) return { status: "not_found", reason: `${sym}: не найден ни у одного провайдера`, candidates: [], warnings };
  const lst = g.items.filter(listed);
  const listedIds = new Set(lst.map((x) => x.refs.cmcId ?? x.refs.coingeckoId));
  const listedNames = new Set(lst.map((x) => x.name.toLowerCase().replace(/[^a-z0-9]/g, "")));

  if (lst.length && listedNames.size === 1) {
    // один листинговый токен (CMC и CoinGecko могут оба его вернуть) — склеиваем их refs
    const refs: AssetRefs = { symbol: sym, assetClass: "crypto" };
    for (const x of lst) Object.assign(refs, x.refs, { symbol: sym, assetClass: "crypto" });
    if (g.items.some((x) => x.provider === "dexscreener")) warnings.push("есть DEX-токены с тем же символом — используется листинговый");
    return { status: "resolved", refs, via: lst[0].provider, candidates: g.items, warnings };
  }
  if (lst.length > 1 && listedIds.size > 1) return { status: "needs_choice", reason: `${sym}: несколько листинговых токенов с этим символом`, candidates: g.items, warnings };

  // только DEX — выбирает пользователь
  return { status: "needs_choice", reason: `${sym}: только DEX-токены, подтвердите сеть и адрес`, candidates: g.items, warnings };
}

/** Сверка цены сигнала с рынком. */
export function priceSanity(signalPrice: number | undefined, q: Quote | null): { ok: boolean; diffPct: number | null; message?: string } {
  if (!q || !signalPrice) return { ok: true, diffPct: null };
  const diff = (signalPrice / q.price - 1) * 100;
  if (Math.abs(diff) > 50)
    return { ok: false, diffPct: diff, message: `цена сигнала $${signalPrice} отличается от рынка $${q.price} на ${diff.toFixed(0)}% — вероятно, выбран не тот токен` };
  return { ok: true, diffPct: diff };
}
