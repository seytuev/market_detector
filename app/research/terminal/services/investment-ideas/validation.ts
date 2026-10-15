import { z } from "zod";

export const createIdeaSchema = z.object({
  ticker: z.string().trim().regex(/^[A-Za-z0-9]{1,16}$/).transform((value) => value.toUpperCase()),
  title: z.string().trim().min(3).max(180),
  thesis: z.string().trim().max(2000).default(""),
  body: z.string().max(12000).default(""),
  catalysts: z.array(z.string().trim().min(1).max(300)).max(20).default([]),
  risks: z.array(z.string().trim().min(1).max(300)).max(20).default([]),
  invalidation: z.string().trim().max(1000).default(""),
  sourceUrl: z.string().url().or(z.literal("")).default(""),
  marketSource: z.enum(["coinmarketcap", "coingecko", "dexscreener"]),
  marketSourceId: z.string().trim().max(500).default(""),
  assetRef: z.string().trim().max(600).default(""),
  assetName: z.string().trim().max(180).default(""),
  assetLogo: z.string().trim().max(1000).default(""),
  assetChain: z.string().trim().max(240).default(""),
  assetContract: z.string().trim().max(500).default(""),
  signalPrice: z.number().positive().optional(),
  signalAt: z.number().int().positive().optional(),
  narrativeBasketId: z.string().trim().max(100).optional(),
  trackingStatus: z.enum(["active_watch", "watch", "note"]).default("watch"),
});

export const updateIdeaSchema = createIdeaSchema.partial().extend({
  id: z.string().uuid(),
  status: z.enum(["active", "cancel_pending", "cancelled", "completed", "archived"]).optional(),
  trackingStatus: z.enum(["active_watch", "watch", "note"]).optional(),
  pinned: z.boolean().optional(),
});

export const ideaSourceSchema = z.object({
  id: z.string().uuid().optional(),
  type: z.enum(["x_project", "x_extra", "unlocks", "publication", "nansen", "website", "market"]),
  url: z.string().url().or(z.literal("")).default(""),
  handle: z.string().trim().max(80).default(""),
  meta: z.record(z.string(), z.unknown()).default({}),
});

export const ideaCancelRuleSchema = z.object({
  id: z.string().uuid().optional(),
  type: z.enum(["price_below", "price_above", "time_limit", "volume_dry", "mcap_below", "event_missed", "custom_note"]),
  severity: z.enum(["cancel", "warn"]).default("warn"),
  config: z.record(z.string(), z.unknown()).default({}),
  completed: z.boolean().default(false),
});
