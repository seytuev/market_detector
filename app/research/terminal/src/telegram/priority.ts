/**
 * Что в ленте важно прямо сейчас — считается кодом, прозрачно, с причинами.
 * Каждая причина — чип на карточке сюжета («фонд», «2 канала», «+30% с поста», «в портфеле»…).
 * Уровни: ≥ 60 — «важно» (попадает в блок «Важное сейчас» и в уведомления), 35–59 — «интересно», ниже — обычный.
 */
import type { Story } from "./stories";
import type { PriceReaction } from "./priceCheck";

export interface PriorityContext {
  /** тикеры в портфелях и корзинах */
  portfolioTickers: Set<string>;
  /** открытые инвестиционные идеи по тикеру */
  ideaByTicker: Map<string, { id: string; title: string }>;
  now?: number;
}

export type ReasonKey = "fund" | "multi" | "rally" | "dump" | "portfolio" | "idea" | "starred" | "event" | "impact" | "fresh";

export interface Priority {
  score: number; // 0–100
  level: "important" | "interesting" | "normal";
  reasons: { key: ReasonKey; label: string; weight: number }[];
}

export function storyPriority(s: Story, prices: Record<string, PriceReaction>, ctx: PriorityContext): Priority {
  const r: Priority["reasons"] = [];
  const add = (key: ReasonKey, label: string, weight: number) => r.push({ key, label, weight });
  const funds = [...new Set(s.posts.flatMap((p) => p.analysis.entities.filter((e) => e.kind === "fund").map((e) => e.name)))];
  if (funds.length) add("fund", `фонд: ${funds[0]}${funds.length > 1 ? ` +${funds.length - 1}` : ""}`, 25);
  if (s.channels.length > 1) add("multi", `${s.channels.length} канала`, Math.min(30, 15 * (s.channels.length - 1)));
  const moves = s.mainTickers.map((t) => [t, prices[t]] as const).filter(([, p]) => p && p.sincePostPct != null);
  const rally = moves.find(([, p]) => p!.verdict === "rally");
  const dump = moves.find(([, p]) => p!.verdict === "dump");
  if (rally) add("rally", `${rally[0]} ${fmt(rally[1]!.sincePostPct!)} с поста`, 20);
  else if (dump) add("dump", `${dump[0]} ${fmt(dump[1]!.sincePostPct!)} с поста`, 20);
  const inPf = s.allTickers.filter((t) => ctx.portfolioTickers.has(t));
  if (inPf.length) add("portfolio", `в портфеле: ${inPf.join(", ")}`, 20);
  const inIdea = s.allTickers.filter((t) => ctx.ideaByTicker.has(t));
  if (inIdea.length) add("idea", `есть идея: ${inIdea.join(", ")}`, 10);
  if (s.starred) add("starred", "★ канал", 10);
  if (s.posts.some((p) => p.analysis.event?.date)) add("event", "есть дата события", 5);
  add("impact", `impact ${Math.floor(s.impact)}`, Math.round(Math.min(5, s.impact) * 4));
  const age = (ctx.now ?? Date.now()) - s.lastAt;
  if (age < 3 * 3_600_000) add("fresh", "свежее", 5);
  const score = Math.min(100, r.reduce((a, x) => a + x.weight, 0));
  return { score, level: score >= 60 ? "important" : score >= 35 ? "interesting" : "normal", reasons: r.filter((x) => x.key !== "impact").sort((a, b) => b.weight - a.weight) };
}

const fmt = (v: number) => (v > 0 ? "+" : "") + v.toFixed(0) + "%";
