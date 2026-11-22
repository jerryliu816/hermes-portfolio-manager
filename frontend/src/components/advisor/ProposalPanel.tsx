/**
 * The advisor's current proposal, as the engine computes it.
 *
 * Nothing here comes from the chat text. The server re-runs the rebalance
 * engine on the last trades the advisor simulated, against the current
 * snapshot and policy, and this panel only formats what comes back. Every
 * figure is shown with what qualifies it: stale data, missing cost basis,
 * unconfirmed tax treatment, and any trade the engine refused.
 */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  advisorApi,
  type Gaps,
  type SimulatedTrade,
  type Simulation,
  type TaxEstimate,
  type WireBreakdown,
} from "../../lib/api";
import { money, percent, shortDate, signedMoney, titleCase, toNumber } from "../../lib/format";
import { Badge, Caveats, Loading } from "../ui";

const TAX_LABEL: Record<TaxEstimate["status"], string> = {
  estimated: "Estimated",
  basis_missing: "No cost basis",
  no_realization_event: "Sheltered",
  unknown: "Treatment unconfirmed",
};

export function TaxCell({ tax }: { tax: TaxEstimate | null }) {
  if (!tax) return <span className="text-ink-3">—</span>;
  if (tax.status === "estimated") {
    const partial = tax.basis_coverage !== null && toNumber(tax.basis_coverage) < 1;
    return (
      <span title={tax.reason ?? undefined}>
        <span className="tnum">{signedMoney(tax.estimated_gain)}</span>
        <span className="block text-[11px] text-ink-3">
          gain{partial ? `, basis for ${percent(tax.basis_coverage, 0)}` : ""}
        </span>
      </span>
    );
  }
  return (
    <Badge tone={tax.status === "no_realization_event" ? "neutral" : "warn"} title={tax.reason ?? undefined}>
      {TAX_LABEL[tax.status]}
    </Badge>
  );
}

/** Trades grouped by account, since money never moves between accounts. */
export function TradesTable({ trades }: { trades: SimulatedTrade[] }) {
  const groups = new Map<string, SimulatedTrade[]>();
  for (const t of trades) groups.set(t.account_id, [...(groups.get(t.account_id) ?? []), t]);
  return (
    <div className="space-y-3">
      {[...groups.entries()].map(([accountId, rows]) => (
        <div key={accountId}>
          <h4 className="mb-1 truncate text-xs font-medium text-ink-2" title={rows[0].account ?? accountId}>
            {rows[0].account ?? "Unknown account"}
          </h4>
          <table className="w-full text-sm">
            <tbody>
              {rows.map((t) => {
                const capped = t.applied_amount !== t.requested_amount;
                return (
                  <tr key={t.index} className="border-t border-line align-top">
                    <td className="py-1.5 pr-2">
                      <span className={`text-xs font-semibold uppercase ${t.action === "sell" ? "text-ink-2" : "text-ink"}`}>
                        {t.action}
                      </span>
                    </td>
                    <td className="min-w-0 py-1.5 pr-2">
                      <span className="block truncate font-medium">{t.security}</span>
                      <span className="block text-[11px] text-ink-3">
                        {t.asset_class ? titleCase(t.asset_class) : "Unclassified"}
                      </span>
                    </td>
                    <td className="tnum py-1.5 pr-2 text-right">
                      {money(t.applied_amount)}
                      {capped && (
                        <span className="block text-[11px] text-bad">asked {money(t.requested_amount)}</span>
                      )}
                    </td>
                    <td className="py-1.5 text-right text-xs">{t.action === "sell" ? <TaxCell tax={t.tax} /> : null}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}

interface AllocationRow {
  key: string;
  label: string;
  before: string | null;
  after: string | null;
  target: string | null;
}

function allocationRows(before: WireBreakdown, after: WireBreakdown, gaps: Gaps): AllocationRow[] {
  const rows = new Map<string, AllocationRow>();
  const row = (key: string, label?: string) => {
    const found = rows.get(key) ?? { key, label: label ?? titleCase(key), before: null, after: null, target: null };
    rows.set(key, found);
    return found;
  };
  for (const s of before.slices) row(s.key, s.label).before = s.fraction;
  for (const s of after.slices) row(s.key, s.label).after = s.fraction;
  for (const g of gaps.gaps) if (!g.untargeted) row(g.asset_class).target = g.target;
  return [...rows.values()];
}

/**
 * Before, after and target per asset class. Before is recessive, after carries
 * the first series colour, and the target is a tick; each row states all three
 * numbers, so identity never rests on colour.
 */
export function AllocationCompare({ before, after, gaps }: { before: WireBreakdown; after: WireBreakdown; gaps: Gaps }) {
  const rows = allocationRows(before, after, gaps);
  const max = Math.max(0.05, ...rows.flatMap((r) => [r.before, r.after, r.target].map((v) => toNumber(v))));
  const width = (v: string | null) => `${(toNumber(v) / max) * 100}%`;
  return (
    <div>
      <div className="mb-2 flex flex-wrap gap-3 text-[11px] text-ink-3" aria-hidden>
        <span className="inline-flex items-center gap-1">
          <span className="h-1.5 w-3 rounded-sm" style={{ background: "var(--text-muted)" }} /> Now
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-1.5 w-3 rounded-sm" style={{ background: "var(--series-1)" }} /> After
        </span>
        <span className="inline-flex items-center gap-1">
          <span className="h-2.5 w-0.5" style={{ background: "var(--text-primary)" }} /> Target
        </span>
      </div>
      <ul className="space-y-2.5">
        {rows.map((r) => (
          <li
            key={r.key}
            title={`${r.label}: now ${percent(r.before)}, after ${percent(r.after)}, target ${r.target ? percent(r.target) : "none"}`}
          >
            <div className="mb-1 flex items-baseline justify-between gap-2 text-xs">
              <span className="truncate text-ink-2">{r.label}</span>
              <span className="tnum shrink-0 text-ink-3">
                {percent(r.before)} → <span className="font-medium text-ink">{percent(r.after)}</span>
                {r.target ? ` · target ${percent(r.target)}` : " · no target"}
              </span>
            </div>
            <div className="relative space-y-0.5">
              <div className="h-1.5 rounded-r-sm" style={{ width: width(r.before), background: "var(--text-muted)" }} />
              <div className="h-1.5 rounded-r-sm" style={{ width: width(r.after), background: "var(--series-1)" }} />
              {r.target && (
                <div
                  className="absolute -top-0.5 h-[calc(100%+4px)] w-0.5"
                  style={{ left: width(r.target), background: "var(--text-primary)" }}
                />
              )}
            </div>
          </li>
        ))}
      </ul>
      <p className="mt-2 text-[11px] text-ink-3">
        Shares of invested, classified value. Uninvested cash and unclassified holdings are left out.
      </p>
    </div>
  );
}

export function SimulationCaveats({ simulation }: { simulation: Simulation }) {
  return (
    <div className="space-y-2">
      {simulation.violations.length > 0 && (
        <Caveats
          tone="bad"
          title={simulation.is_feasible ? "Policy problems" : "Not feasible as written"}
          items={simulation.violations.map((v) => v.detail)}
        />
      )}
      {simulation.caveats.length > 0 && <Caveats items={simulation.caveats} />}
      {simulation.basis_notes.length > 0 && <Caveats tone="info" items={simulation.basis_notes} />}
    </div>
  );
}

/** Where sale proceeds end up: as uninvested cash in the same account. */
export function CashChanges({ cash }: { cash: Simulation["cash"] }) {
  const moved = cash.filter((c) => toNumber(c.net_change) !== 0);
  if (!moved.length) return <p className="text-xs text-ink-3">No account’s cash changes; every sale is reinvested.</p>;
  return (
    <ul className="space-y-1 text-xs">
      {moved.map((c) => (
        <li key={c.account_id} className="flex items-baseline justify-between gap-3">
          <span className="truncate text-ink-2" title={c.account ?? undefined}>
            {c.account ?? "Unknown account"}
          </span>
          <span className={`tnum shrink-0 ${toNumber(c.ending_cash) < 0 ? "text-bad" : "text-ink"}`}>
            {signedMoney(c.net_change)} → {money(c.ending_cash)}
          </span>
        </li>
      ))}
    </ul>
  );
}

function TaxSummary({ tax }: { tax: Simulation["tax"] }) {
  const hasTaxable = toNumber(tax.taxable_sale_value) > 0;
  const partial = hasTaxable && tax.taxable_sale_value_with_basis !== tax.taxable_sale_value;
  return (
    <dl className="grid grid-cols-2 gap-x-3 gap-y-1 text-xs">
      <dt className="text-ink-3">Taxable sales</dt>
      <dd className="tnum text-right">{money(tax.taxable_sale_value)}</dd>
      {hasTaxable && (
        <>
          <dt className="text-ink-3">Estimated gain</dt>
          <dd className="tnum text-right">
            {tax.estimated_gain === null ? "unknown" : signedMoney(tax.estimated_gain)}
            {partial && (
              <span className="block text-[11px] text-warn">on {money(tax.taxable_sale_value_with_basis)} with basis only</span>
            )}
          </dd>
        </>
      )}
      <dt className="text-ink-3">Sheltered sales</dt>
      <dd className="tnum text-right">{money(tax.sheltered_sale_value)}</dd>
      {toNumber(tax.unknown_treatment_sale_value) > 0 && (
        <>
          <dt className="text-warn">Treatment unconfirmed</dt>
          <dd className="tnum text-right text-warn">{money(tax.unknown_treatment_sale_value)}</dd>
        </>
      )}
      {tax.caveat && <dd className="col-span-2 text-[11px] text-ink-3">{tax.caveat}</dd>}
    </dl>
  );
}

function SaveToJournal({ sessionId }: { sessionId: string }) {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [rationale, setRationale] = useState("");
  const [savedId, setSavedId] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => advisorApi.saveProposal(sessionId, { title, rationale }),
    onSuccess: (entry) => {
      setSavedId(entry.id);
      void queryClient.invalidateQueries({ queryKey: ["recommendations"] });
      void queryClient.invalidateQueries({ queryKey: ["journal-stats"] });
    },
  });
  if (savedId)
    return (
      <p className="text-xs text-ink-2">
        Saved to the <Link to="/journal" className="underline underline-offset-2">journal</Link>, with the figures above.
      </p>
    );
  return (
    <form
      className="space-y-2"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <input
        required
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Title, e.g. Trim US equity in the IRA"
        aria-label="Recommendation title"
        className="w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm"
      />
      <textarea
        required
        rows={2}
        value={rationale}
        onChange={(e) => setRationale(e.target.value)}
        placeholder="Why, in your words or the advisor's"
        aria-label="Rationale"
        className="w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm"
      />
      {save.isError && <Caveats tone="bad" items={[(save.error as Error).message]} />}
      <button
        type="submit"
        disabled={save.isPending}
        className="rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-surface-2 disabled:opacity-50"
      >
        {save.isPending ? "Saving…" : "Save to journal"}
      </button>
      <p className="text-[11px] text-ink-3">The server simulates the trades again and stores its own figures.</p>
    </form>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="space-y-2 border-t border-line pt-3">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-ink-3">{title}</h3>
      {children}
    </section>
  );
}

export function ProposalPanel({ sessionId }: { sessionId: string }) {
  const proposal = useQuery({
    queryKey: ["advisor-proposal", sessionId],
    queryFn: () => advisorApi.proposal(sessionId),
  });
  const data = proposal.data;
  const sim = data?.simulation ?? null;

  return (
    <aside className="space-y-3 rounded-xl border border-line bg-surface p-4" aria-label="Current proposal">
      <div>
        <h2 className="text-sm font-semibold">Current proposal</h2>
        <p className="text-xs text-ink-3">
          {sim
            ? `Recomputed by the engine against the ${shortDate(sim.as_of_date)} snapshot and ${
                sim.targets === "proposed" ? "proposed targets" : `policy v${sim.policy_version}`
              }.`
            : "The latest trades the advisor simulated, recomputed here by the engine."}
        </p>
      </div>
      {proposal.isLoading && <Loading />}
      {proposal.isError && <Caveats tone="bad" items={[(proposal.error as Error).message]} />}
      {data && !data.has_proposal && (
        <p className="text-sm text-ink-3">
          No trades yet. When the advisor simulates a set of trades, they appear here with the engine’s figures.
        </p>
      )}
      {data?.error && (
        <Caveats tone="bad" title="Could not recompute the last proposal" items={[data.error]} />
      )}
      {sim && (
        <>
          <div className="flex flex-wrap gap-1.5">
            <Badge tone={sim.is_feasible ? "good" : "bad"}>{sim.is_feasible ? "Feasible" : "Not feasible"}</Badge>
            {toNumber(sim.stale_fraction) > 0 && (
              <Badge tone="warn" title="Share of value in accounts whose as-of date is unknown or old">
                {percent(sim.stale_fraction, 0)} stale
              </Badge>
            )}
          </div>
          <Section title="Trades">
            <TradesTable trades={sim.trades} />
          </Section>
          <Section title="Allocation">
            <AllocationCompare before={sim.before.by_asset_class} after={sim.after.by_asset_class} gaps={sim.after.gaps} />
          </Section>
          <Section title="Uninvested cash">
            <CashChanges cash={sim.cash} />
          </Section>
          <Section title="Tax">
            <TaxSummary tax={sim.tax} />
          </Section>
          <SimulationCaveats simulation={sim} />
          <Section title="Keep it">
            <SaveToJournal key={JSON.stringify(data?.trades)} sessionId={sessionId} />
          </Section>
        </>
      )}
    </aside>
  );
}
