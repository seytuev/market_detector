import type { RuleEvaluator } from "../types";
export const narrativeMomentum: RuleEvaluator = ({ mentionDeltaPct }, params) => mentionDeltaPct != null && mentionDeltaPct > Number(params.threshold_pct ?? 50);
