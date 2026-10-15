import type { RuleEvaluator } from "../types";
export const trailing: RuleEvaluator = ({ history, peakPrice }, params) => Boolean(peakPrice && (history.at(-1)!.close / peakPrice - 1) * 100 <= -Math.abs(Number(params.trail_pct ?? 12)));
