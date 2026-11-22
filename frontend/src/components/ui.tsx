import type { ReactNode } from "react";
import { AlertTriangle, ArrowDownRight, ArrowUpRight, Info, Minus } from "lucide-react";
import { signedMoney } from "../lib/format";

export function Card({
  title,
  subtitle,
  children,
  className = "",
  actions,
}: {
  title?: string;
  subtitle?: ReactNode;
  children: ReactNode;
  className?: string;
  actions?: ReactNode;
}) {
  return (
    <section
      className={`rounded-xl border border-line bg-surface p-5 ${className}`}
      aria-label={title}
    >
      {(title || subtitle || actions) && (
        <header className="mb-4 flex items-start justify-between gap-4">
          <div>
            {title && <h2 className="text-sm font-semibold text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-ink-3">{subtitle}</p>}
          </div>
          {actions}
        </header>
      )}
      {children}
    </section>
  );
}

export function StatTile({
  label,
  value,
  hint,
  tone = "neutral",
}: {
  label: string;
  value: string;
  hint?: ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad";
}) {
  const toneClass = {
    neutral: "text-ink",
    good: "text-good",
    warn: "text-warn",
    bad: "text-bad",
  }[tone];
  return (
    <div className="rounded-xl border border-line bg-surface p-5">
      <div className="text-xs font-medium uppercase tracking-wide text-ink-3">{label}</div>
      <div className={`tnum mt-1.5 text-2xl font-semibold ${toneClass}`}>{value}</div>
      {hint && <div className="mt-1 text-xs text-ink-3">{hint}</div>}
    </div>
  );
}

/**
 * Gains and losses carry an icon and an explicit sign, never colour alone.
 * Roughly one in twelve men cannot reliably separate the red from the green.
 */
export function Delta({ value, kind = "money" }: { value: string | null; kind?: "money" | "pp" }) {
  if (value === null || value === undefined) return <span className="text-ink-3">—</span>;
  const n = Number(value);
  const Icon = n > 0 ? ArrowUpRight : n < 0 ? ArrowDownRight : Minus;
  const tone = n > 0 ? "text-good" : n < 0 ? "text-bad" : "text-ink-3";
  const text =
    kind === "money"
      ? signedMoney(value)
      : `${n >= 0 ? "+" : "−"}${(Math.abs(n) * 100).toFixed(1)} pp`;
  return (
    <span className={`tnum inline-flex items-center gap-1 ${tone}`}>
      <Icon size={14} aria-hidden />
      {text}
    </span>
  );
}

/**
 * Data-quality caveats, shown beside the figures they qualify rather than
 * tucked away. On this data they are not footnotes: a portfolio can be 61%
 * stale and still render a confident-looking total.
 */
export function Caveats({
  items,
  tone = "warn",
  title,
}: {
  items: string[];
  tone?: "warn" | "info" | "bad";
  title?: string;
}) {
  if (!items.length) return null;
  const style = {
    warn: { bg: "var(--status-warning-bg)", fg: "var(--status-warning)", Icon: AlertTriangle },
    bad: { bg: "var(--status-critical-bg)", fg: "var(--status-critical)", Icon: AlertTriangle },
    info: { bg: "var(--surface-2)", fg: "var(--text-secondary)", Icon: Info },
  }[tone];
  return (
    <div
      className="rounded-lg px-4 py-3 text-sm"
      style={{ background: style.bg, color: style.fg }}
      role="note"
    >
      {title && <div className="mb-1 font-semibold">{title}</div>}
      <ul className="space-y-1">
        {items.map((item) => (
          <li key={item} className="flex gap-2">
            <style.Icon size={15} className="mt-0.5 shrink-0" aria-hidden />
            <span>{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function Badge({
  children,
  tone = "neutral",
  title,
}: {
  children: ReactNode;
  tone?: "neutral" | "good" | "warn" | "bad";
  title?: string;
}) {
  const styles = {
    neutral: "border-line text-ink-3",
    good: "text-good",
    warn: "text-warn",
    bad: "text-bad",
  }[tone];
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium ${styles}`}
      style={
        tone === "neutral"
          ? undefined
          : { borderColor: "currentColor", background: `var(--status-${tone === "good" ? "good" : tone === "warn" ? "warning" : "critical"}-bg)` }
      }
    >
      {children}
    </span>
  );
}

export function Empty({ title, hint }: { title: string; hint?: ReactNode }) {
  return (
    <div className="rounded-xl border border-dashed border-line bg-surface p-10 text-center">
      <p className="text-sm font-medium text-ink">{title}</p>
      {hint && <p className="mx-auto mt-2 max-w-md text-sm text-ink-3">{hint}</p>}
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="p-8 text-center text-sm text-ink-3" role="status">
      {label}…
    </div>
  );
}

export function Table({ children }: { children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">{children}</table>
    </div>
  );
}

export function Th({
  children = null,
  align = "left",
}: {
  children?: ReactNode;
  align?: "left" | "right";
}) {
  return (
    <th
      scope="col"
      className={`sticky top-0 z-10 whitespace-nowrap border-b border-line bg-surface px-3 py-2 text-xs font-semibold uppercase tracking-wide text-ink-3 text-${align}`}
    >
      {children}
    </th>
  );
}

export function Td({
  children,
  align = "left",
  className = "",
}: {
  children: ReactNode;
  align?: "left" | "right";
  className?: string;
}) {
  return (
    <td className={`border-b border-line px-3 py-2 text-${align} ${className}`}>{children}</td>
  );
}
