import type { RuleEvaluator } from "../types";
export const eventWindow: RuleEvaluator = ({ daysToEvent, eventType }, params) => {
  const accepted = String(params.event_types ?? "listing,unlock,governance").split(",").map((value) => value.trim());
  return daysToEvent != null && daysToEvent >= 0 && daysToEvent <= Number(params.days_before ?? 5) && Boolean(eventType && accepted.includes(eventType));
};
