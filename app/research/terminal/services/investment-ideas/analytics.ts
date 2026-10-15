export type IdeaSnapshot = {
  price: number;
  marketCap: number | null;
  volume24h: number | null;
  capturedAt: number;
};

export type CancelRuleType =
  | "price_below"
  | "price_above"
  | "time_limit"
  | "volume_dry"
  | "mcap_below"
  | "event_missed"
  | "custom_note";

export type CancelRule = {
  id: string;
  type: CancelRuleType;
  severity: "cancel" | "warn";
  config: Record<string, unknown>;
  completed?: boolean;
};

export type IdeaUnlock = {
  date: string;
  supplyPct: number | null;
  amountUsd: number | null;
  recipient: "team" | "investors" | "ecosystem" | "community";
};

const finite = (value: unknown) => {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
};

export function deriveIdeaMetrics(snapshots: IdeaSnapshot[], signalPrice: number, now = Date.now(), signalAt = now) {
  const ordered = [...snapshots].sort((a, b) => a.capturedAt - b.capturedAt);
  const current = ordered.at(-1) ?? null;
  const high = ordered.length ? Math.max(signalPrice, ...ordered.map((row) => row.price)) : null;
  const low = ordered.length ? Math.min(signalPrice, ...ordered.map((row) => row.price)) : null;
  const volumeHistory = ordered.slice(-8, -1).map((row) => row.volume24h).filter((value): value is number => value != null && value >= 0);
  const volumeAvg7d = volumeHistory.length ? volumeHistory.reduce((sum, value) => sum + value, 0) / volumeHistory.length : null;
  const volumeRatio = current?.volume24h != null && volumeAvg7d && volumeAvg7d > 0 ? current.volume24h / volumeAvg7d : null;
  const sevenDaysAgo = ordered.filter((row) => row.capturedAt <= now - 7 * 86400000).at(-1) ?? null;
  return {
    current,
    high,
    low,
    resultPct: current ? (current.price / signalPrice - 1) * 100 : null,
    highPct: high == null ? null : (high / signalPrice - 1) * 100,
    lowPct: low == null ? null : (low / signalPrice - 1) * 100,
    daysSinceSignal: Math.max(0, Math.floor((now - signalAt) / 86400000)),
    volumeAvg7d,
    volumeRatio,
    trend7dPct: current && sevenDaysAgo ? (current.price / sevenDaysAgo.price - 1) * 100 : null,
    mcapChangePct7d: current?.marketCap && sevenDaysAgo?.marketCap ? (current.marketCap / sevenDaysAgo.marketCap - 1) * 100 : null,
  };
}

export function evaluateCancelRule(rule: CancelRule, input: {
  signalPrice: number;
  signalAt: number;
  snapshots: IdeaSnapshot[];
  now?: number;
}) {
  const now = input.now ?? Date.now();
  const metrics = deriveIdeaMetrics(input.snapshots, input.signalPrice, now, input.signalAt);
  const current = metrics.current;
  const value = finite(rule.config.value);
  let triggered = false;
  if (rule.type === "price_below" && current && value != null) {
    const threshold = rule.config.mode === "pct" ? input.signalPrice * (1 - value / 100) : value;
    triggered = current.price < threshold;
  } else if (rule.type === "price_above" && current && value != null) {
    const threshold = rule.config.mode === "pct" ? input.signalPrice * (1 + value / 100) : value;
    triggered = current.price > threshold;
  } else if (rule.type === "time_limit" && value != null) {
    triggered = now - input.signalAt >= value * 86400000;
  } else if (rule.type === "volume_dry" && current?.volume24h != null && metrics.volumeAvg7d != null && value != null) {
    triggered = current.volume24h < metrics.volumeAvg7d * (value / 100);
  } else if (rule.type === "mcap_below" && current?.marketCap != null && value != null) {
    triggered = current.marketCap < value;
  } else if (rule.type === "event_missed" || rule.type === "custom_note") {
    triggered = Boolean(rule.completed);
  }
  return { triggered, metrics };
}

const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));

export function calculateIdeaScore(input: {
  signalPrice: number;
  signalAt: number;
  snapshots: IdeaSnapshot[];
  rules?: CancelRule[];
  sentimentDelta?: number | null;
  catalystsAhead?: boolean;
  unlocks?: IdeaUnlock[];
  now?: number;
}) {
  const now = input.now ?? Date.now();
  const metrics = deriveIdeaMetrics(input.snapshots, input.signalPrice, now, input.signalAt);
  const reasons: string[] = [];
  let score = 50;
  if (metrics.resultPct != null) {
    score += clamp(metrics.resultPct, -25, 25);
    reasons.push(`цена ${metrics.resultPct >= 0 ? "+" : ""}${metrics.resultPct.toFixed(1)}%`);
  }
  if (metrics.trend7dPct != null) score += clamp(metrics.trend7dPct, -15, 15);
  if (metrics.volumeRatio != null) {
    if (metrics.volumeRatio >= 1.5) score += 10;
    else if (metrics.volumeRatio <= 0.5) score -= 10;
    reasons.push(`объём ×${metrics.volumeRatio.toFixed(1)}`);
  }
  if (input.sentimentDelta != null) {
    score += clamp(input.sentimentDelta, -15, 15);
    if (Math.abs(input.sentimentDelta) >= 3) reasons.push(input.sentimentDelta > 0 ? "нарратив ↑" : "нарратив ↓");
  }
  for (const rule of input.rules ?? []) {
    if (evaluateCancelRule(rule, { signalPrice: input.signalPrice, signalAt: input.signalAt, snapshots: input.snapshots, now }).triggered) {
      score -= rule.severity === "cancel" ? 30 : 10;
      reasons.push(rule.severity === "cancel" ? "условие отмены" : "предупреждение");
    }
  }
  if (input.catalystsAhead) { score += 5; reasons.push("катализатор ≤14 дн"); }
  const upcomingUnlock = (input.unlocks ?? []).map((unlock) => ({ unlock, days: Math.ceil((Date.parse(unlock.date) - now) / 86400000) })).filter(({ days }) => days >= 0 && days <= 14).sort((a, b) => a.days - b.days)[0];
  if (upcomingUnlock?.unlock.supplyPct != null && upcomingUnlock.unlock.supplyPct >= 3) {
    score -= 10;
    reasons.push(`анлок ${upcomingUnlock.unlock.supplyPct}% через ${upcomingUnlock.days} дн`);
  }
  return { score: Math.round(clamp(score, 0, 100)), reasons: [...new Set(reasons)].slice(0, 4), metrics };
}
