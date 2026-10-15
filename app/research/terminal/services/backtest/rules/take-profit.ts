import type { RuleEvaluator } from "../types";
export const takeProfit: RuleEvaluator = ({ history, entryPrice }, params) => Boolean(entryPrice && (history.at(-1)!.close / entryPrice - 1) * 100 >= Number(params.take_profit_pct ?? 25));
