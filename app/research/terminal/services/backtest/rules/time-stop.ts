import type { RuleEvaluator } from "../types";
export const timeStop: RuleEvaluator = ({ barsHeld }, params) => (barsHeld ?? 0) >= Number(params.bars ?? params.days ?? 20);
