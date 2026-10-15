/** Время: перевод «дата + время + пояс источника» в UTC и обратно, пятницы для экспираций. */

const TZ_ALIAS: Record<string, string> = {
  UTC: "UTC", GMT: "UTC", Z: "UTC",
  KST: "Asia/Seoul", JST: "Asia/Tokyo", SGT: "Asia/Singapore", HKT: "Asia/Hong_Kong", CST_CN: "Asia/Shanghai",
  ET: "America/New_York", EST: "America/New_York", EDT: "America/New_York",
  PT: "America/Los_Angeles", PST: "America/Los_Angeles", PDT: "America/Los_Angeles",
  CET: "Europe/Berlin", CEST: "Europe/Berlin", MSK: "Europe/Moscow", BST: "Europe/London",
};

export function zoneOf(tz: string | null | undefined, fallback = "UTC"): string {
  if (!tz) return fallback;
  const t = tz.trim();
  if (TZ_ALIAS[t.toUpperCase()]) return TZ_ALIAS[t.toUpperCase()];
  try {
    new Intl.DateTimeFormat("en-US", { timeZone: t });
    return t;
  } catch {
    return fallback;
  }
}

/** смещение пояса (мин) в момент utcMs */
function offsetMin(utcMs: number, zone: string): number {
  const p = new Intl.DateTimeFormat("en-US", { timeZone: zone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).formatToParts(utcMs);
  const g = (k: string) => Number(p.find((x) => x.type === k)!.value);
  return (Date.UTC(g("year"), g("month") - 1, g("day"), g("hour") % 24, g("minute"), g("second")) - utcMs) / 60000;
}

/** «2026-10-07» + «09:30» в поясе KST → мс UTC. Без времени — 00:00 по поясу. */
export function toUtcMs(date: string, time: string | null, tz: string | null | undefined): number {
  const zone = zoneOf(tz);
  const [h, m] = (time ?? "00:00").split(":").map(Number);
  const guess = Date.parse(`${date}T${String(h).padStart(2, "0")}:${String(m || 0).padStart(2, "0")}:00Z`);
  let t = guess - offsetMin(guess, zone) * 60000;
  t = guess - offsetMin(t, zone) * 60000; // второй проход — на случай перехода на летнее время
  return t;
}

export const utcDate = (ms: number) => new Date(ms).toISOString().slice(0, 10);
export const utcTime = (ms: number) => new Date(ms).toISOString().slice(11, 16);

/** дата в поясе пользователя */
export function localDate(ms: number, tz: string): string {
  return new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit" }).format(ms);
}
export function localTime(ms: number, tz: string): string {
  return new Intl.DateTimeFormat("en-GB", { timeZone: tz, hour: "2-digit", minute: "2-digit", hourCycle: "h23" }).format(ms);
}

export function addDays(date: string, n: number): string {
  return utcDate(Date.parse(date + "T00:00:00Z") + n * 86_400_000);
}
export function diffDays(a: string, b: string): number {
  return Math.round((Date.parse(b + "T00:00:00Z") - Date.parse(a + "T00:00:00Z")) / 86_400_000);
}

/** все пятницы между from и to (включительно), YYYY-MM-DD */
export function fridays(from: string, to: string): string[] {
  const out: string[] = [];
  let d = Date.parse(from + "T00:00:00Z");
  const end = Date.parse(to + "T00:00:00Z");
  while (new Date(d).getUTCDay() !== 5) d += 86_400_000;
  for (; d <= end; d += 7 * 86_400_000) out.push(utcDate(d));
  return out;
}
export function lastFriday(year: number, month1: number): string {
  let d = Date.UTC(year, month1, 0); // последний день месяца
  while (new Date(d).getUTCDay() !== 5) d -= 86_400_000;
  return utcDate(d);
}
