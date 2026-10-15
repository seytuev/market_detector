import type { RuleEvaluator } from "../types";
export const dipBuy: RuleEvaluator = ({ history }, params) => {
  const peakLookback = Number(params.peak_lookback_days ?? 20);
  const smaPeriod = Number(params.sma_period ?? 50);
  if (history.length < Math.max(peakLookback, smaPeriod)) return false;
  const close = history.at(-1)!.close;
  const peak = Math.max(...history.slice(-peakLookback).map((bar) => bar.high));
  const sma = history.slice(-smaPeriod).reduce((sum, bar) => sum + bar.close, 0) / smaPeriod;
  return (close / peak - 1) * 100 <= -Math.abs(Number(params.drop_pct ?? 15)) && close > sma;
};
