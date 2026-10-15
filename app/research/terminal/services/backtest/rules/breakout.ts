import type { RuleEvaluator } from "../types";
export const breakout: RuleEvaluator = ({ history }, params) => {
  const lookback = Number(params.lookback_days ?? 20);
  if (history.length <= lookback) return false;
  const previousHigh = Math.max(...history.slice(-lookback - 1, -1).map((bar) => bar.high));
  return history.at(-1)!.close > previousHigh;
};
