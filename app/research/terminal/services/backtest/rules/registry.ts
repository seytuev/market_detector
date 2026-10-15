import type { RuleContext, RuleEvaluator, RuleGroup } from "../types";
import { breakout } from "./breakout";
import { dipBuy } from "./dip-buy";
import { eventWindow } from "./event-window";
import { narrativeMomentum } from "./narrative-momentum";
import { relativeStrength } from "./relative-strength";
import { rsFade } from "./rs-fade";
import { stopLoss } from "./stop-loss";
import { takeProfit } from "./take-profit";
import { timeStop } from "./time-stop";
import { trailing } from "./trailing";

export const ruleEvaluators: Record<string, RuleEvaluator> = { relative_strength: relativeStrength, breakout, narrative_momentum: narrativeMomentum, event_window: eventWindow, dip_buy: dipBuy, take_profit: takeProfit, stop_loss: stopLoss, time_stop: timeStop, trailing, rs_fade: rsFade };

export const ruleTemplateSeed = [
  ["relative_strength", "entry", "Relative strength", "Актив опережает benchmark", { threshold_pct: { type: "percent", min: 0, max: 200 }, lookback_days: { type: "int", min: 2, max: 365 } }],
  ["breakout", "entry", "Breakout", "Закрытие выше максимума", { lookback_days: { type: "int", min: 2, max: 365 } }],
  ["narrative_momentum", "entry", "Narrative momentum", "Рост упоминаний из recap", { threshold_pct: { type: "percent", min: 0, max: 1000 } }],
  ["event_window", "entry", "Event window", "Вход перед событием", { days_before: { type: "int", min: 0, max: 90 }, event_types: { type: "enum" } }],
  ["dip_buy", "entry", "Dip buy", "Откат от максимума выше SMA", { drop_pct: { type: "percent", min: 1, max: 90 }, sma_period: { type: "int", min: 2, max: 365 } }],
  ["take_profit", "exit", "Take profit", "Фиксация прибыли", { take_profit_pct: { type: "percent", min: 0.1, max: 1000 } }],
  ["stop_loss", "exit", "Stop loss", "Ограничение убытка", { stop_loss_pct: { type: "percent", min: 0.1, max: 100 } }],
  ["time_stop", "exit", "Time stop", "Выход по времени", { bars: { type: "int", min: 1, max: 1000 } }],
  ["trailing", "exit", "Trailing", "Откат от пика позиции", { trail_pct: { type: "percent", min: 0.1, max: 100 } }],
  ["rs_fade", "exit", "RS fade", "Утрата относительной силы", { days: { type: "int", min: 1, max: 90 } }],
] as const;

export function evaluateGroup(group: RuleGroup, context: RuleContext) {
  if (!group.rules.length) return false;
  const results = group.rules.map((rule) => ruleEvaluators[rule.template]?.(context, rule.params) ?? false);
  return group.op === "AND" ? results.every(Boolean) : results.some(Boolean);
}
