import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { money, percent, shortDate } from "../lib/format";
import { AllocationDonut, CategoryBars, RankedBars } from "../components/charts/Charts";
import { Badge, Card, Caveats, Empty, Loading, StatTile } from "../components/ui";
import { ValueOverTimeCard } from "../components/ValueOverTime";

export function Dashboard() {
  const summary = useQuery({ queryKey: ["summary"], queryFn: api.summary });
  const assets = useQuery({ queryKey: ["alloc-asset"], queryFn: api.assetAllocation });
  const tax = useQuery({ queryKey: ["alloc-tax"], queryFn: api.taxAllocation });
  const cash = useQuery({ queryKey: ["cash"], queryFn: api.cash });
  const concentration = useQuery({ queryKey: ["concentration"], queryFn: api.concentration });
  const gains = useQuery({ queryKey: ["gains"], queryFn: api.gains });
  const snapshots = useQuery({ queryKey: ["snapshots"], queryFn: api.snapshots });

  if (summary.isLoading) return <Loading />;
  if (summary.isError) {
    return (
      <Empty
        title="No portfolio yet"
        hint={
          <>
            Import a brokerage export to get started. Nothing here is guessed — every figure
            comes from a document you supply. <Link className="underline" to="/imports">Import a file</Link>.
          </>
        }
      />
    );
  }

  const s = summary.data!;
  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <h1 className="text-lg font-semibold">Portfolio</h1>
          <p className="text-sm text-ink-3">
            as of {shortDate(s.as_of_date)}
            {s.completeness !== "complete" && (
              <Badge tone="bad" title="An account in the source document was not extracted">
                {" "}
                {s.completeness}
              </Badge>
            )}
          </p>
        </div>
        {!s.quality.is_fully_trusted && (
          <Badge tone="warn" title="Some figures rest on stale or incomplete data">
            data quality caveats apply
          </Badge>
        )}
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <StatTile
          label="Total value"
          value={money(s.total_value)}
          hint={`${s.account_count} accounts · ${s.position_count} holdings`}
        />
        <StatTile
          label="Cash & equivalents"
          value={cash.data ? money(cash.data.total_value) : money(s.cash_value)}
          hint={
            cash.data
              ? `${money(cash.data.taxable_value, { compact: true })} of it taxable · ${percent(cash.data.fraction_of_portfolio)} of the portfolio`
              : "settlement balances reported outside holdings"
          }
        />
        <StatTile
          label="Unrealized gain"
          value={gains.data ? money(gains.data.unrealized_gain) : "—"}
          tone={gains.data && Number(gains.data.unrealized_gain) >= 0 ? "good" : "bad"}
          hint={gains.data?.coverage.caveat ?? "full coverage"}
        />
        <StatTile
          label="Stale value"
          value={percent(s.quality.stale_fraction)}
          tone={Number(s.quality.stale_fraction) > 0.1 ? "warn" : "neutral"}
          hint={
            s.quality.stale_accounts.length
              ? `${s.quality.stale_accounts.length} account(s) with an unknown as-of date`
              : "every account has a known date"
          }
        />
      </div>

      {/* Caveats sit beside the numbers they qualify, not in a footnote. */}
      <Caveats items={[...s.quality.caveats, ...s.warnings]} title="Worth knowing about these figures" />

      <div className="grid gap-5 lg:grid-cols-2">
        <Card
          title="Asset allocation"
          subtitle={
            assets.data && Number(assets.data.excluded_value) > 0
              ? assets.data.excluded_label
              : "share of holdings value"
          }
        >
          {assets.data ? (
            <AllocationDonut
              data={assets.data.slices}
              total={assets.data.total}
              totalLabel="Holdings"
            />
          ) : (
            <Loading />
          )}
        </Card>

        <Card
          title="Tax placement"
          subtitle={
            tax.data && Number(tax.data.excluded_value) > 0
              ? `${money(tax.data.excluded_value)} excluded — ${tax.data.excluded_label}`
              : "where the money sits for tax purposes"
          }
        >
          {tax.data ? <CategoryBars data={tax.data.slices} /> : <Loading />}
        </Card>

        <Card
          title="Cash & equivalents"
          subtitle={
            cash.data
              ? `${money(cash.data.settlement_value)} settlement · ${money(cash.data.holdings_value)} money market and bills`
              : "settlement balances plus money-market holdings"
          }
        >
          {cash.data ? (
            cash.data.buckets.length === 0 ? (
              <p className="py-12 text-center text-sm text-ink-3">
                No cash or cash-equivalent holdings in this snapshot.
              </p>
            ) : (
              <div className="space-y-3">
                <table className="w-full text-sm">
                  <thead className="text-xs uppercase tracking-wide text-ink-3">
                    <tr>
                      <th className="pb-2 text-left font-medium">Tax treatment</th>
                      <th className="pb-2 text-right font-medium">Settlement</th>
                      <th className="pb-2 text-right font-medium">Money market</th>
                      <th className="pb-2 text-right font-medium">Total</th>
                    </tr>
                  </thead>
                  <tbody>
                    {cash.data.buckets.map((b) => (
                      <tr key={b.key} className="border-t border-line">
                        <td className="py-2">
                          <span
                            className="font-medium"
                            title={
                              b.key === "unknown"
                                ? "These accounts have no confirmed tax treatment, so this cash belongs to neither side"
                                : b.treatments.join(", ")
                            }
                          >
                            {b.label}
                          </span>
                          <div className="text-xs text-ink-3">
                            {b.account_count} account{b.account_count === 1 ? "" : "s"} ·{" "}
                            {b.position_count} holding{b.position_count === 1 ? "" : "s"}
                          </div>
                        </td>
                        <td className="tnum py-2 text-right">
                          {Number(b.settlement_value) === 0 ? (
                            <span className="text-ink-3">—</span>
                          ) : (
                            money(b.settlement_value)
                          )}
                        </td>
                        <td className="tnum py-2 text-right">
                          {Number(b.holdings_value) === 0 ? (
                            <span className="text-ink-3">—</span>
                          ) : (
                            money(b.holdings_value)
                          )}
                        </td>
                        <td className="tnum py-2 text-right font-medium">
                          {money(b.total_value)}{" "}
                          <span className="font-normal text-ink-3">{percent(b.fraction)}</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {/* Tax-deferred is sheltered now and taxed on withdrawal. Saying
                    "non-taxable" would claim more than the data supports. */}
                <p className="text-xs text-ink-3">
                  Tax-sheltered covers tax-deferred, tax-free and education accounts. Yield in the
                  taxable bucket is ordinary income each year; elsewhere it is deferred or exempt.
                </p>
              </div>
            )
          ) : (
            <Loading />
          )}
        </Card>

        <Card
          title="Largest holdings"
          subtitle={
            concentration.data
              ? `top 5 are ${percent(concentration.data.top_5_fraction)} of the portfolio`
              : undefined
          }
        >
          {concentration.data ? (
            <RankedBars
              data={concentration.data.largest.slice(0, 8).map((e) => ({
                key: e.security_id,
                label: e.symbol ?? e.name,
                value: e.total_value,
                fraction: e.fraction_of_portfolio,
              }))}
            />
          ) : (
            <Loading />
          )}
        </Card>

        <ValueOverTimeCard snapshots={snapshots.data ?? []} />
      </div>

      {concentration.data && concentration.data.over_threshold.length > 0 && (
        <Card
          title="Concentrated positions"
          subtitle={`at or above ${percent(concentration.data.threshold, 0)} of the portfolio`}
        >
          <ul className="space-y-1.5 text-sm">
            {concentration.data.over_threshold.map((e) => (
              <li key={e.security_id} className="flex items-center justify-between gap-3">
                <span>
                  <span className="font-medium">{e.symbol ?? e.name}</span>
                  <span className="ml-2 text-ink-3">
                    in {e.account_count} account{e.account_count === 1 ? "" : "s"}
                  </span>
                  {e.has_divergent_prices && (
                    <Badge tone="warn" title="Accounts disagree on this instrument's price, usually because one source is stale">
                      {" "}divergent prices
                    </Badge>
                  )}
                </span>
                <span className="tnum">
                  {money(e.total_value)}{" "}
                  <span className="text-ink-3">{percent(e.fraction_of_portfolio)}</span>
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}
    </div>
  );
}
