export type IndexAsset = { id: string; ticker: string; name: string; provider: string; assetId: string; weight: number; base: number | null; price: number | null; target: number | null; at: number; sector: string };
export type IndexState = { assets: IndexAsset[]; startedAt: number | null; history: { at: number; index: number }[]; archived: { startedAt: number | null; assets: IndexAsset[]; history: { at: number; index: number }[] }[]; refreshedAt: number; warnings: string[] };
export function indexValue(assets: IndexAsset[]) {
  const invested = assets.reduce((s, a) => s + a.weight, 0);
  if (invested > 100.000001 || assets.some(a => !Number.isFinite(a.weight) || a.weight <= 0 || (a.base !== null && (!Number.isFinite(a.base) || a.base <= 0)) || (a.price !== null && (!Number.isFinite(a.price) || a.price <= 0)))) throw new Error("Проверьте веса и цены: сумма долей не должна превышать 100%");
  const cash = Math.max(0, 100 - invested);
  const complete = assets.every(a=>a.base!==null && a.price!==null);
  const value = complete ? cash + assets.reduce((s, a) => s + a.weight * a.price! / a.base!, 0) : null;
  const target = complete && assets.length && assets.every(a => a.target !== null && Number.isFinite(a.target) && a.target > 0) ? cash + assets.reduce((s, a) => s + a.weight * a.target! / a.base!, 0) : null;
  if ((value !== null && (!Number.isFinite(value) || value <= 0)) || (target !== null && !Number.isFinite(target))) throw new Error('Цены выходят за допустимый диапазон расчёта');
  return { invested, cash, value, change: value === null ? null : value - 100, target, targetChange: target === null ? null : target - 100, remainingUpside: target === null || value === null ? null : (target / value - 1) * 100, assets: assets.map(a => ({ ...a, currentWeight: value === null ? null : a.weight * a.price! / a.base! / value * 100, change: a.price === null || a.base === null ? null : (a.price / a.base - 1) * 100, contribution: a.price === null || a.base === null ? null : a.weight * (a.price / a.base - 1) })) };
}
