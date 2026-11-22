/**
 * Charts.
 *
 * Palette, marks and interaction follow the project's visualization method:
 * categorical hues are assigned in fixed order and never cycled, marks are thin
 * with a 2px surface gap between fills, every multi-series chart carries a
 * legend AND direct labels so identity is never colour alone, and a table view
 * exists behind each one.
 *
 * The six-slot categorical palette was validated with the method's checker in
 * both modes. Light mode returns a contrast warning on three slots, which
 * obliges visible labels -- so the donut and the bars are directly labelled
 * rather than relying on the legend.
 */
import {
  Bar,
  BarChart,
  Cell,
  Line,
  LineChart,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ReactNode } from "react";
import { SERIES } from "../../lib/api";
import { money, percent, shortDate, toNumber } from "../../lib/format";

const AXIS = "var(--text-muted)";
const SURFACE = "var(--surface-1)";

export interface Datum {
  key: string;
  label: string;
  value: string;
  fraction?: string;
}

function TooltipBox({ children }: { children: ReactNode }) {
  return (
    <div
      className="rounded-lg border px-3 py-2 text-xs shadow-lg"
      style={{ background: "var(--surface-1)", borderColor: "var(--border)", color: "var(--text-primary)" }}
    >
      {children}
    </div>
  );
}

/** Allocation. A donut rather than a pie: the hole holds the total. */
export function AllocationDonut({
  data,
  total,
  totalLabel = "Total",
}: {
  data: Datum[];
  total: string;
  /** What the hole's figure covers. Allocation excludes cash, so saying
   *  "Total" beside a larger headline number invites the reader to think one
   *  of the two is wrong. */
  totalLabel?: string;
}) {
  const rows = data.map((d) => ({ ...d, n: toNumber(d.value) }));
  return (
    <div className="grid gap-4 sm:grid-cols-[220px_1fr] sm:items-center">
      <div className="relative h-[220px]">
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie
              data={rows}
              dataKey="n"
              nameKey="label"
              innerRadius={62}
              outerRadius={96}
              paddingAngle={2}
              stroke={SURFACE}
              strokeWidth={2}
              isAnimationActive={false}
            >
              {rows.map((row, i) => (
                <Cell key={row.key} fill={SERIES[i % SERIES.length]} />
              ))}
            </Pie>
            <Tooltip
              content={({ payload }) =>
                payload?.[0] ? (
                  <TooltipBox>
                    <div className="font-medium">{payload[0].payload.label}</div>
                    <div className="tnum">{money(payload[0].payload.value)}</div>
                    <div className="tnum text-ink-3">{percent(payload[0].payload.fraction)}</div>
                  </TooltipBox>
                ) : null
              }
            />
          </PieChart>
        </ResponsiveContainer>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <div className="text-[11px] uppercase tracking-wide text-ink-3">{totalLabel}</div>
          <div className="tnum text-lg font-semibold">{money(total, { compact: true })}</div>
        </div>
      </div>
      {/* Direct labels beside the mark: identity is never colour alone, and the
          light-mode contrast warning obliges visible labels. */}
      <ul className="space-y-1.5">
        {rows.map((row, i) => (
          <li key={row.key} className="flex items-center gap-2 text-sm">
            <span
              className="size-2.5 shrink-0 rounded-sm"
              style={{ background: SERIES[i % SERIES.length] }}
              aria-hidden
            />
            <span className="flex-1 truncate text-ink-2">{row.label}</span>
            <span className="tnum text-ink-3">{percent(row.fraction)}</span>
            <span className="tnum w-24 text-right">{money(row.value, { compact: true })}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

/** Ranked magnitudes. Horizontal bars: the labels are words, not dates. */
export function RankedBars({
  data,
  colorIndex = 0,
  valueFormat = (v: string) => money(v, { compact: true }),
}: {
  data: Datum[];
  colorIndex?: number;
  valueFormat?: (value: string) => string;
}) {
  const rows = data.map((d) => ({ ...d, n: toNumber(d.value) }));
  const max = Math.max(...rows.map((r) => r.n), 1);
  return (
    <ul className="space-y-2">
      {rows.map((row) => (
        <li key={row.key} className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3">
          <div className="min-w-0">
            <div className="mb-1 flex items-baseline justify-between gap-2">
              <span className="truncate text-sm text-ink-2">{row.label}</span>
              <span className="tnum shrink-0 text-xs text-ink-3">
                {row.fraction ? percent(row.fraction) : ""}
              </span>
            </div>
            <div className="h-2 rounded-full" style={{ background: "var(--surface-2)" }}>
              <div
                className="h-2 rounded-full"
                style={{
                  width: `${Math.max((row.n / max) * 100, 1.5)}%`,
                  background: SERIES[colorIndex % SERIES.length],
                }}
              />
            </div>
          </div>
          <span className="tnum w-28 text-right text-sm">{valueFormat(row.value)}</span>
        </li>
      ))}
    </ul>
  );
}

/** Tax placement. One bar per treatment, in both dollars and share. */
export function CategoryBars({ data }: { data: Datum[] }) {
  const rows = data.map((d, i) => ({
    ...d,
    n: toNumber(d.value),
    fill: SERIES[i % SERIES.length],
  }));
  return (
    <div className="h-[200px]">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={rows} margin={{ top: 4, right: 8, left: 8, bottom: 4 }}>
          <XAxis
            dataKey="label"
            tick={{ fill: AXIS, fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: "var(--border)" }}
            interval={0}
          />
          <YAxis
            tick={{ fill: AXIS, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={54}
            tickFormatter={(v: number) => money(String(v), { compact: true })}
          />
          <Tooltip
            cursor={{ fill: "var(--surface-2)" }}
            content={({ payload }) =>
              payload?.[0] ? (
                <TooltipBox>
                  <div className="font-medium">{payload[0].payload.label}</div>
                  <div className="tnum">{money(payload[0].payload.value)}</div>
                  <div className="tnum text-ink-3">{percent(payload[0].payload.fraction)}</div>
                </TooltipBox>
              ) : null
            }
          />
          <Bar dataKey="n" radius={[4, 4, 0, 0]} isAnimationActive={false}>
            {rows.map((row) => (
              <Cell key={row.key} fill={row.fill} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

/** A YYYY-MM-DD date as local midnight, the same reading `shortDate` uses. */
function dayTimestamp(isoDate: string): number {
  const [y, m, d] = isoDate.split("-").map(Number);
  return new Date(y, m - 1, d).getTime();
}

/**
 * Value over time. A single series, so no legend: the title names it.
 *
 * The x axis is time, not a category: two snapshots two days apart sit close
 * together and two months apart sit far apart, so the slope between points
 * reflects how fast the value actually moved.
 */
export function ValueHistory({
  data,
  height = 200,
}: {
  /** `date` is an ISO YYYY-MM-DD as-of date. */
  data: { date: string; value: string; accounts?: number }[];
  /** Pixels, or any CSS length such as "60vh" for the expanded view. */
  height?: number | string;
}) {
  const rows = data.map((d) => ({ ...d, t: dayTimestamp(d.date), n: toNumber(d.value) }));
  const labels = new Map(rows.map((r) => [r.t, shortDate(r.date)]));
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={rows} margin={{ top: 8, right: 12, left: 8, bottom: 4 }}>
          <XAxis
            dataKey="t"
            type="number"
            scale="time"
            domain={["dataMin", "dataMax"]}
            // A tick at each snapshot, so every label is a real reading; ones
            // that would collide are dropped, never squeezed.
            ticks={rows.map((r) => r.t)}
            // When labels collide, keep the first and last so the range shows.
            interval="preserveStartEnd"
            tickFormatter={(t: number) => labels.get(t) ?? ""}
            padding={{ left: 24, right: 24 }}
            tick={{ fill: AXIS, fontSize: 11 }}
            tickLine={false}
            axisLine={{ stroke: "var(--border)" }}
          />
          <YAxis
            tick={{ fill: AXIS, fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={54}
            domain={["auto", "auto"]}
            tickFormatter={(v: number) => money(String(v), { compact: true })}
          />
          <Tooltip
            cursor={{ stroke: "var(--border)" }}
            content={({ payload }) =>
              payload?.[0] ? (
                <TooltipBox>
                  <div className="font-medium">{shortDate(payload[0].payload.date)}</div>
                  <div className="tnum">{money(payload[0].payload.value)}</div>
                  {payload[0].payload.accounts !== undefined && (
                    <div className="text-ink-3">
                      {payload[0].payload.accounts} account
                      {payload[0].payload.accounts === 1 ? "" : "s"}
                    </div>
                  )}
                </TooltipBox>
              ) : null
            }
          />
          <Line
            type="monotone"
            dataKey="n"
            stroke={SERIES[0]}
            strokeWidth={2}
            dot={{ r: 4, strokeWidth: 2, stroke: SURFACE, fill: SERIES[0] }}
            activeDot={{ r: 6 }}
            isAnimationActive={false}
          />
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}

/** Drift against policy. A diverging bar around a zero line. */
export function DriftBars({
  data,
}: {
  data: { label: string; drift: string; band: string; inBand: boolean }[];
}) {
  const max = Math.max(...data.map((d) => Math.abs(toNumber(d.drift))), 0.05);
  return (
    <ul className="space-y-3">
      {data.map((row) => {
        const drift = toNumber(row.drift);
        const width = (Math.abs(drift) / max) * 50;
        return (
          <li key={row.label}>
            <div className="mb-1 flex items-baseline justify-between text-sm">
              <span className="text-ink-2">{row.label}</span>
              <span className="tnum text-ink-3">
                {drift >= 0 ? "+" : "−"}
                {(Math.abs(drift) * 100).toFixed(1)} pp
                {!row.inBand && <span className="ml-1.5 text-warn">out of band</span>}
              </span>
            </div>
            <div className="relative h-2.5" style={{ background: "var(--surface-2)" }}>
              <div className="absolute inset-y-0 left-1/2 w-px" style={{ background: "var(--border)" }} />
              <div
                className="absolute inset-y-0 rounded-sm"
                style={{
                  width: `${Math.max(width, 0.8)}%`,
                  left: drift >= 0 ? "50%" : `${50 - width}%`,
                  // A diverging encoding: warm for over, cool for under, with
                  // the in-band case deliberately recessive.
                  background: row.inBand
                    ? "var(--text-muted)"
                    : drift > 0
                      ? "var(--series-2)"
                      : "var(--series-1)",
                }}
              />
            </div>
          </li>
        );
      })}
    </ul>
  );
}
