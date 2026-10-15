/**
 * Шум: посты, которые не должны попадать ни в «Важное сейчас», ни в дайджест, ни в верх ленты.
 * По умолчанию скрыты; в ленте есть переключатель «Показать шум (N)».
 *
 * Считает код, а не модель — правила прозрачные:
 *  1. пустой пост: после удаления тикеров, ссылок и эмодзи остаётся < 15 символов, нет картинок и нет твита;
 *  2. нет ни тикеров, ни фондов/бирж/проектов, а impact ≤ 2 («Фильм „Хоуп“ — хит 2026»);
 *  3. impact 1 и категория meme/other;
 *  4. модель сама пометила «пост без торговой идеи» / «пустой пост» в заголовке.
 * Сюжет — шум, только если шум все его посты.
 */
import type { AnalyzedPost, Story } from "./stories";

export interface NoiseVerdict {
  noise: boolean;
  reason?: string;
}

const EMPTY_TITLE = /(пуст(ой|ая)\s+пост|без\s+текста|без\s+торговой\s+идеи|не\s+про\s+рынок)/i;

export function meaningfulText(text: string): string {
  return text
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/\b(?:x|twitter|t)\.(?:com|me)\/\S+/g, " ")
    .replace(/\$[A-Za-z0-9]{1,15}/g, " ")
    .replace(/[\p{Extended_Pictographic}\p{S}\p{P}]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}

export function postNoise(p: AnalyzedPost & { tweetText?: string }): NoiseVerdict {
  const a = p.analysis;
  const hasMedia = (p.media?.length ?? 0) > 0;
  const hasTweet = !!p.tweetText || a.x_links.length > 0;
  if (meaningfulText(p.post.text).length < 15 && !hasMedia && !hasTweet) return { noise: true, reason: "пустой пост" };
  if (EMPTY_TITLE.test(a.title_ru)) return { noise: true, reason: "без торговой мысли" };
  const facts = a.entities.some((e) => e.kind === "fund" || e.kind === "exchange" || e.kind === "project");
  if (!a.tickers.length && !facts && a.impact <= 2) return { noise: true, reason: "нет тикеров и фактов" };
  if (a.impact === 1 && (a.category === "meme" || a.category === "other")) return { noise: true, reason: "не про рынок" };
  return { noise: false };
}

export function storyNoise(s: Story): NoiseVerdict {
  const v = s.posts.map(postNoise);
  return v.every((x) => x.noise) ? { noise: true, reason: v[0].reason } : { noise: false };
}
