import type { RuleEvaluator } from "../types";
export const rsFade: RuleEvaluator = ({ history, benchmarkHistory }, params) => {
  const days = Number(params.days ?? 3);
  if (history.length <= days || benchmarkHistory.length <= days) return false;
  for (let offset = 0; offset < days; offset += 1) {
    const asset = history.at(-1 - offset)!.close / history.at(-2 - offset)!.close - 1;
    const benchmark = benchmarkHistory.at(-1 - offset)!.close / benchmarkHistory.at(-2 - offset)!.close - 1;
    if (asset >= benchmark) return false;
  }
  return true;
};
