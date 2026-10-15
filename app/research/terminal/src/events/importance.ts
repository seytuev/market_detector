/**
 * Важность события 1–3 — считает код (модель может только поднять через кандидата):
 *  3 — FOMC, CPI, NFP; разлок ≥ 2% капитализации или ≥ $50M; квартальная экспирация; главные конференции;
 *      любое событие по тикеру из моих портфелей/идей с датой в пределах 3 дней;
 *  2 — разлок ≥ 0,5% или ≥ $10M; GDP/PCE; листинг на топ-бирже; апгрейд/мейннет; месячная экспирация; мой тикер;
 *      событие, о котором написали 3+ разных источника;
 *  1 — остальное.
 */
import type { CalEvent } from "./types";

const TOP_CONF = /token2049|korea blockchain week|\bkbw\b|consensus|devcon|ethcc|bitcoin conference|breakpoint|permissionless|ethdenver/i;
const TOP_EX = /binance|coinbase|upbit|bithumb|okx|bybit|robinhood/i;

export function scoreImportance(e: CalEvent, mine: Set<string>, now = Date.now()): 1 | 2 | 3 {
  let s: 1 | 2 | 3 = 1;
  const up = (v: 1 | 2 | 3) => (s = Math.max(s, v) as 1 | 2 | 3);
  const t = `${e.title} ${e.projects.join(" ")}`;
  if (e.kind === "macro") up(/fomc|cpi|non.?farm|nfp|payrolls|ставк|инфляц/i.test(t) ? 3 : 2);
  if (e.kind === "unlock") {
    const pct = e.pctOfMcap ?? 0, v = e.valueUsd ?? 0;
    if (pct >= 2 || v >= 50e6) up(3);
    else if (pct >= 0.5 || v >= 10e6) up(2);
  }
  if (e.kind === "expiry") up(/квартал/i.test(t) ? 3 : /месяч/i.test(t) ? 2 : 1);
  if (e.kind === "conference" && TOP_CONF.test(t)) up(3);
  if (e.kind === "listing" && TOP_EX.test(t)) up(2);
  if (e.kind === "upgrade" || e.kind === "launch") up(2);
  const authors = new Set(e.sources.map((x) => `${x.kind}:${x.author ?? x.ref}`));
  if (authors.size >= 3) up(2);
  if (e.tickers.some((x) => mine.has(x))) {
    up(2);
    if (e.startsAt != null && e.startsAt - now < 3 * 86_400_000 && e.startsAt > now - 86_400_000) up(3);
  }
  return s;
}
