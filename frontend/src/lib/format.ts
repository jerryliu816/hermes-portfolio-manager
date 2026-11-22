/**
 * Formatting money that arrives as a string.
 *
 * The API sends money as a string precisely so it never passes through a
 * JavaScript number, where `0.1 + 0.2 !== 0.3`. So these helpers format for
 * display and nothing more -- there is no arithmetic here, and there must not
 * be. The server has already done the sums exactly.
 *
 * `Number(...)` appears only where a value is about to become a pixel width or
 * a chart coordinate, which is a rendering concern, not a monetary one.
 */

export function money(value: string | null | undefined, options?: { compact?: boolean }): string {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  if (options?.compact && Math.abs(n) >= 1_000_000) {
    return `$${(n / 1_000_000).toFixed(2)}M`;
  }
  if (options?.compact && Math.abs(n) >= 1_000) {
    return `$${Math.round(n / 1_000)}k`;
  }
  return n.toLocaleString(undefined, {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  });
}

export function signedMoney(value: string | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n >= 0 ? "+" : "−"}${money(Math.abs(n).toString())}`;
}

export function percent(value: string | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${(n * 100).toFixed(digits)}%`;
}

export function signedPercent(value: string | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  const n = Number(value);
  if (!Number.isFinite(n)) return "—";
  return `${n >= 0 ? "+" : "−"}${(Math.abs(n) * 100).toFixed(digits)} pp`;
}

/** For chart geometry only. Never for money the user will read. */
export function toNumber(value: string | null | undefined): number {
  const n = Number(value ?? 0);
  return Number.isFinite(n) ? n : 0;
}

export function titleCase(value: string): string {
  return value
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

/**
 * Format a date the API sent.
 *
 * `new Date("2026-08-26")` is parsed as UTC midnight, so anywhere west of
 * Greenwich it renders as the 25th -- a portfolio silently dated a day early.
 * A date-only string has no timezone and must not acquire one, so the parts
 * are read directly and a local date is constructed from them. Timestamps
 * (which do carry a zone) still go through the normal parser.
 */
export function shortDate(value: string): string {
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  const date = dateOnly
    ? new Date(Number(dateOnly[1]), Number(dateOnly[2]) - 1, Number(dateOnly[3]))
    : new Date(value);
  return date.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}
