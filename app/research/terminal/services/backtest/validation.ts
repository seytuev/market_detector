import { z } from "zod";

const rule = z.object({
  template: z.string().min(1),
  params: z.record(z.union([z.number(), z.string(), z.boolean()])),
});

const group = z.object({
  op: z.enum(["AND", "OR"]),
  rules: z.array(rule).min(1),
});

export const strategyConfigSchema = z.object({
  universe: z.object({
    type: z.enum(["portfolio_holdings", "tier_buckets", "manual_list", "top_n_by_mcap"]),
    portfolios: z.array(z.string()).optional(),
    tiers: z.array(z.string()).optional(),
    tickers: z.array(z.string()).optional(),
    n: z.number().int().positive().optional(),
  }),
  entry: group,
  exit: group,
  risk: z.object({
    max_position_pct: z.number().positive().max(100),
    max_positions: z.number().int().positive().max(100),
    stop_loss_pct: z.number().positive().max(100),
    time_stop: z.number().int().positive().max(3650),
    max_drawdown_kill_pct: z.number().positive().max(100),
    cooldown_days: z.number().int().min(0).max(365),
  }),
  costs: z.object({ fee_bps: z.number().min(0).max(1000), slippage_bps: z.number().min(0).max(1000) }),
  params: z.record(z.union([z.number(), z.string(), z.boolean()])).default({}),
});

export const createStrategySchema = z.object({
  name: z.string().trim().min(2).max(100),
  thesis: z.string().trim().max(1200).default(""),
  category: z.enum(["rotation", "narrative", "meme", "event", "mean_reversion", "other"]),
  horizonMin: z.number().int().positive(),
  horizonMax: z.number().int().positive(),
  horizonUnit: z.enum(["hours", "days"]),
  status: z.enum(["draft", "testing", "active", "archived"]).default("draft"),
  portfolios: z.array(z.string()).default([]),
  config: strategyConfigSchema,
}).refine((value) => value.horizonMax >= value.horizonMin, "Максимальный горизонт должен быть больше минимального");

export const backtestRequestSchema = z.object({
  strategyVersionId: z.string().min(1),
  periodFrom: z.number().int().positive(),
  periodTo: z.number().int().positive(),
  initialCapital: z.number().positive(),
  benchmark: z.enum(["BTC", "ETH"]),
  overrides: z.record(z.union([z.number(), z.string(), z.boolean()])).default({}),
}).refine((value) => value.periodTo > value.periodFrom, "Дата окончания должна быть позже даты начала");

export const sweepRequestSchema = backtestRequestSchema.and(z.object({
  axes: z.array(z.object({ path: z.string().min(1), from: z.number(), to: z.number(), step: z.number().positive() })).min(1).max(2),
}));

export const csvBarSchema = z.object({
  ticker: z.string().trim().min(1),
  ts: z.coerce.number().int().positive(),
  open: z.coerce.number().positive(),
  high: z.coerce.number().positive(),
  low: z.coerce.number().positive(),
  close: z.coerce.number().positive(),
  volume: z.coerce.number().min(0).default(0),
});
