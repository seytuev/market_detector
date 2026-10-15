/**
 * Веса признаков — как учебник подстраивается под твои реакции на находки.
 *
 * Модель на каждой находке отмечает, какие признаки правила есть в посте (signs_hit). Дальше всё считает код:
 *  - «Верно»     → признаки этой находки весят больше (+0.2);
 *  - «Частично»  → чуть больше (+0.05), а комментарий идёт в правило: что было не так;
 *  - «Мимо»      → признаки, которые сработали зря, весят меньше (−0.25);
 *  - ↑ / ↓ на признаке — твоё прямое слово: +0.5 / −0.5. Вес 0 — «не учитывать».
 * Вес от 0 до 3, по умолчанию 1. Хранится по тексту признака, поэтому переживает переписывание правила.
 *
 * Уверенность находки = 60% оценка модели + 40% «по весам» (доля веса сработавших признаков).
 * Порог паттерна сравнивается с этой смешанной уверенностью.
 * По оценённым находкам считается рекомендуемый порог.
 */
import type { Match, Pattern } from "./types";

export const W_DEFAULT = 1;
export const W_MIN = 0;
export const W_MAX = 3;
const STEP = { right: 0.2, partial: 0.05, wrong: -0.25, up: 0.5, down: -0.5 } as const;

export const signKey = (s: string) => s.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim().slice(0, 120);
const clamp = (x: number) => Math.round(Math.max(W_MIN, Math.min(W_MAX, x)) * 100) / 100;

export function signWeight(p: Pick<Pattern, "weights">, sign: string): number {
  const w = p.weights?.[signKey(sign)];
  return typeof w === "number" ? w : W_DEFAULT;
}

/** Уверенность «по весам»: сколько веса набрали признаки, найденные в посте, от веса всех признаков. null — модель не отметила признаки. */
export function weightedScore(p: Pick<Pattern, "weights" | "rule">, hits: number[] | undefined): number | null {
  const signs = p.rule?.signs_ru ?? [];
  if (!signs.length || !hits) return null;
  const total = signs.reduce((a, s) => a + signWeight(p, s), 0);
  if (!total) return null;
  const got = [...new Set(hits)].filter((i) => i >= 0 && i < signs.length).reduce((a, i) => a + signWeight(p, signs[i]), 0);
  return Math.round((100 * got) / total);
}

export const blend = (model: number, weighted: number | null) => (weighted == null ? model : Math.round(0.6 * model + 0.4 * weighted));

/** Новые веса после реакции на находку. votes — ↑/↓ по номерам признаков правила. */
export function applyFeedback(p: Pattern, m: Pick<Match, "signsHit">, verdict: "right" | "partial" | "wrong" | "dismiss", votes: { index: number; vote: "up" | "down" }[] = []): Record<string, number> {
  const signs = p.rule?.signs_ru ?? [];
  const w = { ...(p.weights ?? {}) };
  const bump = (i: number, d: number) => {
    if (i < 0 || i >= signs.length) return;
    const k = signKey(signs[i]);
    w[k] = clamp((typeof w[k] === "number" ? w[k] : W_DEFAULT) + d);
  };
  if (verdict !== "dismiss") for (const i of new Set(m.signsHit ?? [])) bump(i, STEP[verdict]);
  for (const v of votes) bump(v.index, STEP[v.vote]);
  return w;
}

/** Перенести веса на новое правило: тот же текст признака — тот же вес; остальные — по умолчанию. */
export function carryWeights(old: Record<string, number> | undefined, signs: string[]): Record<string, number> {
  const out: Record<string, number> = {};
  for (const s of signs) {
    const k = signKey(s);
    if (old && typeof old[k] === "number") out[k] = old[k];
  }
  return out;
}

/** Строки признаков для запроса модели: вес и пометка. */
export function signsForPrompt(p: Pick<Pattern, "weights" | "rule">): string[] {
  return (p.rule?.signs_ru ?? []).map((s, i) => {
    const w = signWeight(p, s);
    const tag = w === 0 ? " — НЕ УЧИТЫВАТЬ (управляющий отключил)" : w >= 1.5 ? " — ГЛАВНЫЙ признак" : w <= 0.6 ? " — слабый признак" : "";
    return `[${i}] ${s} (вес ${w.toFixed(1)}${tag})`;
  });
}

/**
 * Рекомендуемый порог: самый низкий порог (с шагом 5), при котором среди оценённых находок выше него «Верно» не меньше 70%.
 * Нужно хотя бы 5 оценок, иначе null.
 */
export function suggestThreshold(matches: Match[]): { value: number; precision: number; rated: number } | null {
  const rated = matches.filter((m) => m.status === "right" || m.status === "wrong" || m.status === "partial");
  if (rated.length < 5) return null;
  for (let t = 30; t <= 95; t += 5) {
    const above = rated.filter((m) => m.confidence >= t);
    if (above.length < 3) break;
    const prec = above.filter((m) => m.status === "right").length / above.length;
    if (prec >= 0.7) return { value: t, precision: Math.round(prec * 100) / 100, rated: rated.length };
  }
  return null;
}
