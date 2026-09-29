// Display formatting. A null measurement shows as a dash: an absent number
// must never read as zero or as a perfect value.
export const DASH = "—";

export function num(value: number | null | undefined, digits = 0, unit = ""): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return DASH;
  return `${value.toFixed(digits)}${unit ? ` ${unit}` : ""}`;
}

export function shortId(id: string): string {
  return id.slice(0, 8);
}

export function ageSeconds(receivedAt: number, now: number): number {
  return Math.max(0, Math.round((now - receivedAt) / 1000));
}
