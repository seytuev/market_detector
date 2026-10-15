import type { RuleEvaluator } from "../types";
export const stopLoss: RuleEvaluator = ({ history, entryPrice }, params) => Boolean(entryPrice && (history.at(-1)!.close / entryPrice - 1) * 100 <= -Math.abs(Number(params.stop_loss_pct ?? 10)));
