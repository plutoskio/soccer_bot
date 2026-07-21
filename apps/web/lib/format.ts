const formatter = (options: Intl.DateTimeFormatOptions) =>
  new Intl.DateTimeFormat("en-GB", { timeZone: "Europe/Luxembourg", ...options });

export function formatDay(value: string) {
  return formatter({ weekday: "long", day: "numeric", month: "long" }).format(new Date(value));
}

export function formatKickoffTime(value: string) {
  return formatter({ hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

export function formatKickoffLong(value: string) {
  return formatter({
    weekday: "long",
    day: "numeric",
    month: "long",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(new Date(value));
}

export function formatMatchDate(value: string) {
  return formatter({ day: "numeric", month: "short" }).format(new Date(value));
}

export function formatTimestamp(value: string) {
  return formatter({
    day: "2-digit",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  }).format(new Date(value));
}

export function formatAsOf(value: string) {
  return formatter({ day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }).format(
    new Date(value),
  );
}

export function formatPercent(value: number | null) {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

export function formatInteger(value: number) {
  return new Intl.NumberFormat("en-GB").format(value);
}

export function formatRate(value: number | null) {
  return value === null ? "—" : value.toFixed(2);
}

export function humanize(value: string) {
  return value.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}
