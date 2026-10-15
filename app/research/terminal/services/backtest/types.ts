export type Bar = { ticker: string; ts: number; open: number; high: number; low: number; close: number; volume: number };
export type RuleSpec = { template: string; params: Record<string, number | string | boolean> };
export type RuleGroup = { op: "AND" | "OR"; rules: RuleSpec[] };
export type RuleContext = { history: Bar[]; benchmarkHistory: Bar[]; entryPrice?: number; peakPrice?: number; barsHeld?: number; mentionDeltaPct?: number; daysToEvent?: number; eventType?: string };
export type RuleEvaluator = (context: RuleContext, params: Record<string, number | string | boolean>) => boolean;
export type StrategyConfig = {
  universe: { type: "portfolio_holdings" | "tier_buckets" | "manual_list" | "top_n_by_mcap"; portfolios?: string[]; tiers?: string[]; tickers?: string[]; n?: number };
  entry: RuleGroup;
  exit: RuleGroup;
  risk: { max_position_pct: number; max_positions: number; stop_loss_pct: number; time_stop: number; max_drawdown_kill_pct: number; cooldown_days: number };
  costs: { fee_bps: number; slippage_bps: number };
  params: Record<string, number | string | boolean>;
};
export type Trade = { ticker: string; entryTs: number; entryPx: number; exitTs: number; exitPx: number; pnlPct: number; reasonEntry: string; reasonExit: string; holdDays: number };
export type CurvePoint = { ts: number; value: number };
export type BacktestResult = { metrics: Record<string, number>; equityCurve: CurvePoint[]; drawdownCurve: CurvePoint[]; benchmarkCurve: CurvePoint[]; monthlyReturns: { month: string; returnPct: number }[]; trades: Trade[]; log: { tickers: number; bars: number; trades: number; durationMs: number } };
