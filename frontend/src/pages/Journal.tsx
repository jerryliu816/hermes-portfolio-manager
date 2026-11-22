import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, type GapsReport, type Recommendation, type Simulation } from "../lib/api";
import { money, percent, shortDate, signedMoney, titleCase } from "../lib/format";
import { Badge, Card, Caveats, Empty, Loading, StatTile } from "../components/ui";
import { AllocationCompare, SimulationCaveats, TradesTable } from "../components/advisor/ProposalPanel";

const TONE = {
  pending: "warn",
  accepted: "good",
  rejected: "bad",
  deferred: "neutral",
} as const;

export function Journal() {
  const recommendations = useQuery({ queryKey: ["recommendations"], queryFn: api.recommendations });
  const stats = useQuery({ queryKey: ["journal-stats"], queryFn: api.journalStats });
  const [filter, setFilter] = useState<string>("");

  if (recommendations.isLoading) return <Loading />;

  const rows = (recommendations.data ?? []).filter((r) => !filter || r.status === filter);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Recommendation journal</h1>
      <p className="max-w-3xl text-sm text-ink-3">
        What was suggested, and what you decided. Nothing here has been acted on — a
        recommendation is a note for you to review. Deferring keeps it open; rejecting closes it.
      </p>

      {stats.data && (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          <StatTile label="Open" value={String(stats.data.pending + stats.data.deferred)} hint="pending or deferred" />
          <StatTile label="Accepted" value={String(stats.data.accepted)} tone="good" />
          <StatTile label="Rejected" value={String(stats.data.rejected)} />
          <StatTile
            label="Acceptance rate"
            value={stats.data.acceptance_rate ? percent(stats.data.acceptance_rate, 0) : "—"}
            hint={stats.data.decided ? `of ${stats.data.decided} decided` : "nothing decided yet"}
          />
        </div>
      )}

      <div className="flex flex-wrap gap-1">
        {["", "pending", "deferred", "accepted", "rejected"].map((value) => (
          <button
            key={value || "all"}
            type="button"
            onClick={() => setFilter(value)}
            className={`rounded-lg px-2.5 py-1 text-sm ${
              filter === value ? "bg-surface-2 font-medium" : "text-ink-2"
            }`}
          >
            {value === "" ? "All" : value}
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <Empty
          title="Nothing recorded yet"
          hint="Recommendations appear here when an analyst records one, or when you write down your own reasoning."
        />
      ) : (
        <div className="space-y-3">
          {rows.map((r) => (
            <Entry key={r.id} recommendation={r} />
          ))}
        </div>
      )}
    </div>
  );
}

function Entry({ recommendation }: { recommendation: Recommendation }) {
  const queryClient = useQueryClient();
  const [note, setNote] = useState("");
  const [open, setOpen] = useState(false);

  const decide = useMutation({
    mutationFn: (decision: string) => api.decide(recommendation.id, decision, note || undefined),
    onSuccess: () => {
      void queryClient.invalidateQueries();
      setNote("");
      setOpen(false);
    },
  });

  return (
    <Card>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Badge tone={TONE[recommendation.status]}>{recommendation.status}</Badge>
            <h2 className="font-medium">{recommendation.title}</h2>
          </div>
          <p className="mt-1.5 text-sm text-ink-2">{recommendation.rationale}</p>
          <p className="mt-1 text-xs text-ink-3">
            from {recommendation.source} on {shortDate(recommendation.created_at)}
            {recommendation.advisor_session_id && (
              <>
                {" · "}
                <Link
                  to={`/advisor?session=${recommendation.advisor_session_id}`}
                  className="underline underline-offset-2 hover:text-ink"
                >
                  the conversation
                </Link>
              </>
            )}
          </p>
        </div>
        {recommendation.is_open && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="rounded-lg border border-line px-2.5 py-1 text-xs text-ink-2 hover:text-ink"
          >
            Decide
          </button>
        )}
      </div>

      <ProposalDetails recommendation={recommendation} />

      {recommendation.decisions.length > 0 && (
        <ol className="mt-3 space-y-1 border-t border-line pt-3 text-xs text-ink-3">
          {recommendation.decisions.map((d) => (
            <li key={d.id}>
              {shortDate(d.decided_at)} — <span className="font-medium">{d.decision}</span>
              {d.note && ` · ${d.note}`}
            </li>
          ))}
        </ol>
      )}

      {open && (
        <div className="mt-3 border-t border-line pt-3">
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Why — future-you will want the reasoning"
            className="w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm"
            aria-label="Decision note"
          />
          <div className="mt-2 flex gap-2">
            {(["accepted", "deferred", "rejected"] as const).map((decision) => (
              <button
                key={decision}
                type="button"
                onClick={() => decide.mutate(decision)}
                disabled={decide.isPending}
                className="rounded-lg border border-line px-3 py-1.5 text-sm capitalize text-ink-2 hover:text-ink disabled:opacity-50"
              >
                {decision}
              </button>
            ))}
          </div>
        </div>
      )}
    </Card>
  );
}

/**
 * The engine's figures as they were when the recommendation was saved. They
 * are not recomputed: the journal records what was proposed, against the
 * snapshot it was proposed on.
 */
function ProposalDetails({ recommendation }: { recommendation: Recommendation }) {
  const [open, setOpen] = useState(false);
  const { kind, simulation } = recommendation;
  if (kind === "note" || !simulation) return null;
  const asOf = `as computed on the ${shortDate(simulation.as_of_date)} snapshot`;

  if (kind === "trades") {
    const sim = simulation as Simulation;
    return (
      <div className="mt-3 space-y-3 border-t border-line pt-3">
        <div className="flex flex-wrap items-center gap-2 text-xs text-ink-3">
          <Badge tone={sim.is_feasible ? "good" : "bad"}>{sim.is_feasible ? "Feasible" : "Not feasible"}</Badge>
          <span>
            {sim.trades.length} trade{sim.trades.length === 1 ? "" : "s"}, taxable sales{" "}
            {money(sim.tax.taxable_sale_value)}
            {sim.tax.estimated_gain !== null && `, estimated gain ${signedMoney(sim.tax.estimated_gain)}`}, {asOf}
          </span>
          <button type="button" className="underline underline-offset-2 hover:text-ink" onClick={() => setOpen(!open)}>
            {open ? "Hide details" : "Show trades"}
          </button>
        </div>
        {open && (
          <div className="grid grid-cols-[minmax(0,1fr)] gap-4 md:grid-cols-2">
            <TradesTable trades={sim.trades} />
            <div className="space-y-3">
              <AllocationCompare before={sim.before.by_asset_class} after={sim.after.by_asset_class} gaps={sim.after.gaps} />
              <SimulationCaveats simulation={sim} />
            </div>
          </div>
        )}
      </div>
    );
  }

  const report = simulation as GapsReport;
  return (
    <div className="mt-3 space-y-2 border-t border-line pt-3">
      <p className="text-xs text-ink-3">
        Proposed targets, {asOf}. Applying them means editing your{" "}
        <Link to="/policy" className="underline underline-offset-2 hover:text-ink">policy</Link>.
      </p>
      <table className="w-full max-w-lg text-sm">
        <thead>
          <tr className="text-left text-xs text-ink-3">
            <th className="py-1 font-medium">Asset class</th>
            <th className="py-1 text-right font-medium">Now</th>
            <th className="py-1 text-right font-medium">Proposed</th>
            <th className="py-1 text-right font-medium" title="Dollars over (+) or under (−) the proposed target">Over / under</th>
          </tr>
        </thead>
        <tbody>
          {report.gaps.map((g) => (
            <tr key={g.asset_class} className="border-t border-line">
              <td className="py-1">{titleCase(g.asset_class)}</td>
              <td className="tnum py-1 text-right">{percent(g.actual)}</td>
              <td className="tnum py-1 text-right">{g.untargeted ? "none" : percent(g.target)}</td>
              <td className="tnum py-1 text-right">{signedMoney(g.gap)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {report.caveats.length > 0 && <Caveats items={report.caveats} />}
    </div>
  );
}
