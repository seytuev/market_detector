import type { RuleEvaluator } from "../types";
export const relativeStrength: RuleEvaluator = ({ history, benchmarkHistory }, params) => {
  const lookback = Number(params.lookback_days ?? 10);
  if (history.length <= lookback || benchmarkHistory.length <= lookback) return false;
  const asset = history.at(-1)!.close / history.at(-1 - lookback)!.close - 1;
  const benchmark = benchmarkHistory.at(-1)!.close / benchmarkHistory.at(-1 - lookback)!.close - 1;
  return (asset - benchmark) * 100 > Number(params.threshold_pct ?? 15);
};
