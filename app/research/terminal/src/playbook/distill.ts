/**
 * Правило паттерна: сильная модель (Claude Opus / GPT-4.1) читает все примеры паттерна, твои заметки
 * и анти-примеры — и пишет короткое правило, по которому дешёвая модель потом ищет похожие посты.
 * Вызывается редко: при добавлении примера (с задержкой, пачкой) и по кнопке «Обновить правило».
 */
import type { LlmClient, JsonSchemaSpec } from "../telegram/analyze";
import type { Example, Pattern, PatternRule } from "./types";
import { DIRECTION_RU, TRACK_RU, TRACK_HINT, FOCUS_RU, trackOf, focusOf } from "./types";
import { signsForPrompt, carryWeights } from "./weights";
import { factsLines, proposeConditions, mergeConditions } from "./conditions";

export const RULE_SCHEMA: JsonSchemaSpec = {
  name: "pattern_rule",
  schema: {
    type: "object",
    additionalProperties: false,
    required: ["summary_ru", "signs_ru", "anti_signs_ru", "direction", "horizon", "check_ru", "flags"],
    properties: {
      summary_ru: { type: "string" },
      signs_ru: { type: "array", items: { type: "string" } },
      anti_signs_ru: { type: "array", items: { type: "string" } },
      direction: { type: "string", enum: ["long", "short", "watch"] },
      horizon: { type: "string", enum: ["1-3d", "1-2w", "1m+"] },
      check_ru: { type: "array", items: { type: "string" } },
      flags: {
        type: "object",
        additionalProperties: false,
        required: ["author", "known_ticker", "image"],
        properties: { author: { type: "boolean" }, known_ticker: { type: "boolean" }, image: { type: "boolean" } },
      },
    },
  },
};

export const DISTILL_PROMPT = `Ты помогаешь управляющему крипто-фонда STV формализовать его торговое чутьё.
Тебе дают паттерн — тип идеи, который он сам отметил в постах из X, Telegram и своих заметок, — и все примеры этого паттерна:
оригинал поста, разбор ИИ, описания скринов, направление и ЕГО ЗАМЕТКУ (почему это сигнал). Заметки управляющего важнее всего.
Анти-примеры («не сигнал») — похожие посты, которые он отклонил: из них выведи анти-признаки.
У паттерна есть СТЕЗЯ — чему он учит: трейд-сигнал, события в календарь, нарратив, внимание или риск. Пиши правило под стезю
(для «События» — какие посты содержат события, которые надо заносить в календарь; для «Внимание» — что именно должно цеплять глаз).
В примерах может быть УПОР — на что управляющий велел смотреть (автор, формулировка, график, тикер, событие/дата, цифры). Упор — главный признак, ставь его первым.
У текущих признаков есть ВЕСА от управляющего: «ГЛАВНЫЙ» — сохрани формулировку дословно; «НЕ УЧИТЫВАТЬ» — убери или перенеси в анти-признаки.
КОММЕНТАРИИ к находкам («Частично», «Мимо», «Верно» с описанием) — его прямые указания, как правило должно реагировать: учти каждый.
ФАКТЫ ТЕРМИНАЛА у примеров («$NEAR: в корзине «L1»», «Автор …: в учебнике 3 сигн.») посчитаны кодом и точны. Если заметки говорят
«хороший автор», «токен уже у нас в корзине / мы его знаем» — это признаки паттерна: опиши их словами в signs_ru и поставь флаги.

Напиши правило паттерна по-русски, так, чтобы другая модель по нему узнавала НОВЫЕ посты того же типа про ЛЮБОЙ тикер:
- summary_ru — суть паттерна в 1–2 предложениях: что за идея и почему она работает;
- signs_ru — 2–6 признаков, по которым пост относится к паттерну (формулировки, тип автора, что на графике, контекст рынка);
- anti_signs_ru — 0–5 признаков, что пост только похож, но не то (из анти-примеров и заметок);
- direction — long / short / watch (как в большинстве примеров);
- horizon — 1-3d / 1-2w / 1m+;
- check_ru — до 4 пунктов, что проверить перед входом;
- flags — что из этого проверит код по фактам терминала: author = важно, КТО написал (заметки или упор про автора);
  known_ticker = важно, что тикер уже наш (в портфеле / корзине / идеях); image = нужен график или скрин.
Не привязывай правило к конкретным монетам из примеров. Не придумывай того, чего нет в примерах и заметках. Это не финансовый совет, а описание паттерна.`;

const clip = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

export function distillMessage(p: Pattern, examples: Example[], notes: string, outcomes: Map<string, string> = new Map(), comments: string[] = []): string {
  const pos = examples.filter((e) => e.direction !== "not_signal").slice(-25);
  const neg = examples.filter((e) => e.direction === "not_signal").slice(-10);
  const one = (e: Example, i: number) =>
    [
      `#${i + 1} · ${DIRECTION_RU[e.direction]} · ${e.source.author ?? "автор неизвестен"} · ${new Date(e.source.at).toISOString().slice(0, 10)}${e.source.symbol ? " · $" + e.source.symbol : ""}`,
      e.note ? `Заметка управляющего: ${clip(e.note, 600)}` : "",
      focusOf(e) ? `Упор: ${focusOf(e)!.fields.map((f) => FOCUS_RU[f]).join(", ")}${focusOf(e)!.note ? ` — ${clip(focusOf(e)!.note, 400)}` : ""}` : "",
      `Источник: ${e.source.kind}${e.source.author ? ` · автор ${e.source.author}` : ""}`,
      e.source.text ? `Оригинал: ${clip(e.source.text, 700)}` : "",
      e.source.summary ? `Разбор: ${clip(e.source.summary, 600)}` : "",
      e.source.imageNotes.length ? `Скрины: ${clip(e.source.imageNotes.join(" "), 400)}` : "",
      factsLines(e.source).length ? `Факты терминала: ${factsLines(e.source).join(" · ")}` : "",
      outcomes.get(e.id) ? `Что было с ценой: ${outcomes.get(e.id)}` : "",
    ].filter(Boolean).join("\n");
  return [
    `Паттерн: «${p.name}»`,
    `Стезя: ${TRACK_RU[trackOf(p)]} — ${TRACK_HINT[trackOf(p)]}`,
    notes.trim() ? `Общие правила управляющего:\n${clip(notes, 2000)}` : "",
    p.rule ? `Текущее правило (уточни, а не переписывай с нуля):\n${JSON.stringify({ ...p.rule, signs_ru: undefined })}\nПризнаки с весами:\n${signsForPrompt(p).join("\n")}` : "",
    comments.length ? `КОММЕНТАРИИ УПРАВЛЯЮЩЕГО К НАХОДКАМ (${comments.length}):\n${comments.map((c) => "• " + clip(c, 300)).join("\n")}` : "",
    `ПРИМЕРЫ (${pos.length}):\n${pos.map(one).join("\n\n")}`,
    neg.length ? `АНТИ-ПРИМЕРЫ (${neg.length}):\n${neg.map(one).join("\n\n")}` : "",
  ].filter(Boolean).join("\n\n");
}

const str = (x: unknown, n: number) => (typeof x === "string" ? x.trim().slice(0, n) : "");
const list = (x: unknown, max: number, n = 240) => (Array.isArray(x) ? x.map((s) => str(s, n)).filter(Boolean).slice(0, max) : []);

export function validateRule(x: any, examples: Example[]): { ok: true; value: PatternRule } | { ok: false; error: string } {
  if (!x || typeof x !== "object") return { ok: false, error: "не объект" };
  const summary_ru = str(x.summary_ru, 600);
  const signs_ru = list(x.signs_ru, 6);
  if (!summary_ru) return { ok: false, error: "пустая суть" };
  if (signs_ru.length < 2) return { ok: false, error: "меньше двух признаков" };
  if (!["1-3d", "1-2w", "1m+"].includes(x.horizon)) return { ok: false, error: "горизонт" };
  // направление — как у большинства примеров (модель может ошибиться, примеры — нет)
  const dirs = examples.filter((e) => e.direction !== "not_signal").map((e) => e.direction as PatternRule["direction"]);
  const major = (["long", "short", "watch"] as const).map((d) => [d, dirs.filter((x) => x === d).length] as const).sort((a, b) => b[1] - a[1])[0];
  const direction: PatternRule["direction"] = major && major[1] > dirs.length / 2 ? major[0] : ["long", "short", "watch"].includes(x.direction) ? x.direction : "watch";
  const fl = x.flags && typeof x.flags === "object" ? { author: x.flags.author === true, known_ticker: x.flags.known_ticker === true, image: x.flags.image === true } : undefined;
  return { ok: true, value: { summary_ru, signs_ru, anti_signs_ru: list(x.anti_signs_ru, 5), direction, horizon: x.horizon, check_ru: list(x.check_ru, 4), ...(fl ? { flags: fl } : {}) } };
}

export async function distillRule(p: Pattern, examples: Example[], notes: string, client: LlmClient, outcomes?: Map<string, string>, comments: string[] = []): Promise<PatternRule> {
  if (!examples.some((e) => e.direction !== "not_signal")) throw new Error("в паттерне нет ни одного примера-сигнала");
  const user = distillMessage(p, examples, notes, outcomes, comments);
  let last = "";
  for (let i = 0; i < 2; i++) {
    const v = validateRule(await client.analyze(DISTILL_PROMPT, i ? `${user}\n\nПрошлый ответ не прошёл проверку: ${last}. Исправь.` : user, [], RULE_SCHEMA), examples);
    if (v.ok) return v.value;
    last = v.error;
  }
  throw new Error(`правило не прошло проверку: ${last}`);
}

/**
 * Записать новое правило в паттерн. Если правило поправлено руками — не перезаписываем, кладём в «предложено».
 * Паттерн с правилом и хотя бы одним примером-сигналом сам переходит из learning в active (паузу не трогаем).
 * v2: раньше нужно было 2 примера — на практике паттерн «висел» в «Копит примеры». Шум режет порог уверенности.
 */
export function applyRule(p: Pattern, rule: PatternRule, examples: Example[], now: number): Pattern {
  const positives = examples.filter((e) => e.direction !== "not_signal").length;
  const status = p.status === "learning" && positives >= 1 ? "active" : p.status;
  if (p.ruleEdited && p.rule) return { ...p, proposedRule: rule, ruleAt: now, status, updatedAt: now };
  // v4: условия кода пересчитываются по свежему правилу (флагам) и примерам; твои ручные — не трогаются
  const next = { ...p, rule, weights: carryWeights(p.weights, rule.signs_ru), proposedRule: null, ruleAt: now, version: p.version + 1, status, updatedAt: now };
  return { ...next, conditions: mergeConditions(p.conditions, proposeConditions(next, examples)) };
}
