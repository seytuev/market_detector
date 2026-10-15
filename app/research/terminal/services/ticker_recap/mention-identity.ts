/** A source signature is not evidence about the source's own token. */
export function isAssetMention(symbol: string, text: string, handle = "") {
  const ticker = symbol.toUpperCase();
  if (!/^[A-Z0-9]{1,12}$/.test(ticker)) return false;
  const source = handle.replace(/^@/, "").toLowerCase();
  if (ticker !== "AIXBT" && !source.startsWith(ticker.toLowerCase())) return true;
  if (new RegExp(`\\$${ticker}\\b`, "i").test(text)) return true;
  const body = text.replace(new RegExp(`#${ticker}\\b`, "ig"), "").replace(new RegExp(`@${ticker}[\\w]*`, "ig"), "").replace(new RegExp(`\\b${ticker}\\s*:`, "ig"), "");
  return new RegExp(`\\b${ticker}\\b.{0,60}(token|price|virtuals|market cap|токен|цена|капитализац)|(token|price|токен|цена).{0,60}\\b${ticker}\\b`, "i").test(body);
}
