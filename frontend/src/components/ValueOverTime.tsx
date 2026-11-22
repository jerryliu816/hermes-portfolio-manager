import { useEffect, useState, type ReactNode } from "react";
import { Maximize2, X } from "lucide-react";
import type { Snapshot } from "../lib/api";
import { money, shortDate } from "../lib/format";
import { ValueHistory } from "./charts/Charts";
import { Badge, Card } from "./ui";
import { SnapshotVisibilityToggle } from "./SnapshotVisibility";
import { SnapshotDeleteButton } from "./SnapshotDelete";

const RANGES = [
  { key: "1m", label: "1M", title: "Last month" },
  { key: "3m", label: "3M", title: "Last 3 months" },
  { key: "6m", label: "6M", title: "Last 6 months" },
  { key: "ytd", label: "YTD", title: "Since January 1" },
  { key: "1y", label: "1Y", title: "Last year" },
  { key: "all", label: "All", title: "Every snapshot" },
] as const;
type Range = (typeof RANGES)[number]["key"];

const STORAGE_KEY = "value-over-time-range";

/** The first day inside the range, as YYYY-MM-DD, or null for no limit. */
function rangeStart(range: Range, today = new Date()): string | null {
  const d = new Date(today.getFullYear(), today.getMonth(), today.getDate());
  switch (range) {
    case "1m":
      d.setMonth(d.getMonth() - 1);
      break;
    case "3m":
      d.setMonth(d.getMonth() - 3);
      break;
    case "6m":
      d.setMonth(d.getMonth() - 6);
      break;
    case "ytd":
      d.setMonth(0, 1);
      break;
    case "1y":
      d.setFullYear(d.getFullYear() - 1);
      break;
    case "all":
      return null;
  }
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// A per-browser convenience only: storage can be blocked, so it never matters.
function loadRange(): Range {
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (RANGES.some((r) => r.key === saved)) return saved as Range;
  } catch {
    /* fall through */
  }
  return "all";
}

function saveRange(range: Range) {
  try {
    localStorage.setItem(STORAGE_KEY, range);
  } catch {
    /* not remembered, which is fine */
  }
}

function RangeButtons({ range, onChange }: { range: Range; onChange: (r: Range) => void }) {
  return (
    <div role="group" aria-label="Date range" className="inline-flex rounded-lg border border-line p-0.5">
      {RANGES.map((r) => (
        <button
          key={r.key}
          type="button"
          title={r.title}
          aria-pressed={range === r.key}
          onClick={() => onChange(r.key)}
          className={`rounded-md px-2 py-0.5 text-xs ${
            range === r.key ? "bg-surface-2 font-medium text-ink" : "text-ink-3 hover:text-ink"
          }`}
        >
          {r.label}
        </button>
      ))}
    </div>
  );
}

/**
 * Value over time, with a date range, a point list for hiding or deleting
 * snapshots, and an expanded view.
 *
 * Snapshots are sparse, so the subtitle always says how many fall in the
 * range and which dates they span: a line through two points is not a trend
 * at the resolution the axis implies.
 */
export function ValueOverTimeCard({ snapshots }: { snapshots: Snapshot[] }) {
  const [range, setRangeState] = useState<Range>(loadRange);
  const [managing, setManaging] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const setRange = (r: Range) => {
    setRangeState(r);
    saveRange(r);
  };

  // Superseded snapshots are never plotted; hidden ones are the user's choice,
  // and the subtitle says how many so the line never looks more complete than it is.
  const live = snapshots.filter((snap) => !snap.superseded_by);
  const hiddenCount = live.filter((snap) => snap.hidden_from_history).length;
  const plotted = live
    .filter((snap) => !snap.hidden_from_history)
    .slice()
    .reverse()
    .map((snap) => ({ date: snap.as_of_date, value: snap.total_value, accounts: snap.account_count }));
  const start = rangeStart(range);
  const history = start ? plotted.filter((p) => p.date >= start) : plotted;
  const outsideRange = plotted.length - history.length;

  const span =
    history.length > 1
      ? `${history.length} snapshots, ${shortDate(history[0].date)} – ${shortDate(history[history.length - 1].date)}`
      : null;
  const subtitle = (
    <>
      {span ??
        (outsideRange > 0
          ? `${history.length === 0 ? "no" : "only one"} snapshot in this range`
          : hiddenCount > 0
            ? "too few points shown for a trend"
            : "import a second month to see a trend")}
      {hiddenCount > 0 && <span className="text-warn"> · {hiddenCount} hidden</span>}
    </>
  );

  const chart = (height: number | string) =>
    history.length > 1 ? (
      <ValueHistory data={history} height={height} />
    ) : (
      <p className="py-12 text-center text-sm text-ink-3">
        {outsideRange > 0
          ? `${history.length === 0 ? "No snapshots fall" : "Only one snapshot falls"} in this range. Choose a longer one to see a line.`
          : hiddenCount > 0
            ? "Fewer than two snapshots are shown, so there is no line to draw."
            : "Only one snapshot so far."}{" "}
        A change between snapshots is a change in value, not a return — it mixes market movement
        with contributions and withdrawals.
      </p>
    );

  return (
    <>
      <Card
        title="Value over time"
        subtitle={subtitle}
        actions={
          <div className="flex items-center gap-2">
            {(live.length > 1 || managing) && (
              <button
                type="button"
                onClick={() => setManaging((v) => !v)}
                aria-expanded={managing}
                className="whitespace-nowrap rounded-lg border border-line px-2.5 py-1 text-xs text-ink-2 hover:text-ink"
              >
                {managing ? "Done" : "Choose points"}
              </button>
            )}
            <button
              type="button"
              onClick={() => setExpanded(true)}
              aria-label="Show a larger chart"
              title="Show a larger chart"
              className="rounded-lg border border-line p-1.5 text-ink-2 hover:text-ink"
            >
              <Maximize2 size={13} aria-hidden />
            </button>
          </div>
        }
      >
        <div className="mb-3">
          <RangeButtons range={range} onChange={setRange} />
        </div>
        {chart(200)}
        {managing && (
          <div className="mt-4 border-t border-line pt-3">
            <p className="mb-2 text-xs text-ink-3">
              Hiding a snapshot only takes it off this chart; the rest of the dashboard is
              unchanged. Deleting it removes it everywhere, after showing you what that involves.
            </p>
            <ul className="space-y-1.5 text-sm">
              {live.map((snap) => (
                <li key={snap.id} className="flex items-center justify-between gap-3">
                  <span className={snap.hidden_from_history ? "text-ink-3" : undefined}>
                    {shortDate(snap.as_of_date)}
                    <span className="ml-2 whitespace-nowrap text-xs text-ink-3">
                      {snap.account_count} account{snap.account_count === 1 ? "" : "s"}
                    </span>
                    {snap.completeness !== "complete" && (
                      <Badge tone="bad" title="An account in the source was not extracted">
                        {" "}
                        {snap.completeness}
                      </Badge>
                    )}
                  </span>
                  <span className="flex items-center gap-3">
                    <span className="tnum text-ink-2">{money(snap.total_value)}</span>
                    <SnapshotVisibilityToggle snapshot={snap} />
                    <SnapshotDeleteButton snapshot={snap} />
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
      </Card>

      {expanded && (
        <ExpandedChart
          subtitle={subtitle}
          range={range}
          onRange={setRange}
          onClose={() => setExpanded(false)}
        >
          {chart("min(65vh, 640px)")}
        </ExpandedChart>
      )}
    </>
  );
}

function ExpandedChart({
  subtitle,
  range,
  onRange,
  onClose,
  children,
}: {
  subtitle: ReactNode;
  range: Range;
  onRange: (r: Range) => void;
  onClose: () => void;
  children: ReactNode;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label="Value over time, enlarged"
      onClick={onClose}
    >
      <div
        className="w-full max-w-6xl rounded-xl border border-line bg-surface p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="mb-3 flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold text-ink">Value over time</h2>
            <p className="mt-0.5 text-xs text-ink-3">{subtitle}</p>
          </div>
          <div className="flex items-center gap-2">
            <RangeButtons range={range} onChange={onRange} />
            <button
              type="button"
              onClick={onClose}
              aria-label="Close the larger chart"
              className="rounded-lg border border-line p-1.5 text-ink-2 hover:text-ink"
            >
              <X size={14} aria-hidden />
            </button>
          </div>
        </header>
        {children}
      </div>
    </div>
  );
}
