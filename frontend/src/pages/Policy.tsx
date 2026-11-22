import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import { money, percent, titleCase } from "../lib/format";
import { DriftBars } from "../components/charts/Charts";
import { Badge, Card, Caveats, Loading, Table, Td, Th } from "../components/ui";

export function PolicyPage() {
  const queryClient = useQueryClient();
  const policy = useQuery({ queryKey: ["policy"], queryFn: api.policy, retry: false });
  const assessment = useQuery({
    queryKey: ["assessment"],
    queryFn: api.assessment,
    retry: false,
  });

  // The draft is only ever an *override* of what the server holds, so it is
  // null until the user edits. Deriving the textarea's value during render
  // avoids seeding state from a query in an effect, which would cascade an
  // extra render every time the policy refetches.
  const [override, setOverride] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const draft = override ?? policy.data?.raw_yaml ?? "";

  const save = useMutation({
    mutationFn: (yaml: string) => api.savePolicy(yaml),
    onSuccess: () => {
      void queryClient.invalidateQueries();
      setOverride(null);
      setEditing(false);
    },
  });

  const startFromTemplate = async () => {
    setOverride(await api.starterPolicy());
    setEditing(true);
  };

  const missing = policy.isError && (policy.error as ApiError)?.isMissing;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-lg font-semibold">Investment policy</h1>
        {policy.data && (
          <p className="text-sm text-ink-3">
            {policy.data.name} · version {policy.data.version}
          </p>
        )}
      </div>

      {missing && (
        <Card>
          <p className="text-sm text-ink-2">
            No policy yet. A policy is your own statement of intent — targets, drift bands and
            constraints. Nothing here recommends targets for you; the template is a starting
            point that states its own assumptions.
          </p>
          <button
            type="button"
            onClick={startFromTemplate}
            className="mt-4 rounded-lg px-3 py-1.5 text-sm font-medium text-white"
            style={{ background: "var(--series-1)" }}
          >
            Start from a template
          </button>
        </Card>
      )}

      {assessment.data && (
        <>
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={assessment.data.is_compliant ? "good" : "warn"}>
              {assessment.data.is_compliant ? "in policy" : "out of policy"}
            </Badge>
            {assessment.data.is_provisional && (
              <Badge tone="warn" title="The underlying data is too uncertain to act on without checking">
                provisional
              </Badge>
            )}
            <span className="text-sm text-ink-3">
              measured against {money(assessment.data.classified_value)} of classified value
            </span>
          </div>

          <Caveats items={assessment.data.data_caveats} title="Why these figures may be unreliable" />
          <Caveats items={assessment.data.policy_notes} tone="info" title="Gaps in the policy itself" />

          <div className="grid gap-5 lg:grid-cols-2">
            <Card title="Drift from target" subtitle="percentage points from the policy target">
              <DriftBars
                data={assessment.data.drift.map((d) => ({
                  label: d.label,
                  drift: d.drift,
                  band: d.band,
                  inBand: d.is_in_band,
                }))}
              />
            </Card>

            <Card
              title="Targets"
              subtitle="rebalance figures are arithmetic only — they ignore tax, trading cost, and whether the money should move"
            >
              <Table>
                <thead>
                  <tr>
                    <Th>Asset class</Th>
                    <Th align="right">Target</Th>
                    <Th align="right">Actual</Th>
                    <Th align="right">To rebalance</Th>
                  </tr>
                </thead>
                <tbody>
                  {assessment.data.drift.map((d) => (
                    <tr key={d.asset_class}>
                      <Td>
                        <span className="inline-flex items-center gap-2">
                          {d.label}
                          {!d.is_in_band && <Badge tone="warn">{d.direction}</Badge>}
                        </span>
                      </Td>
                      <Td align="right" className="tnum text-ink-3">{percent(d.target)}</Td>
                      <Td align="right" className="tnum">{percent(d.actual)}</Td>
                      <Td align="right" className="tnum">
                        {Number(d.rebalance_value) >= 0 ? "sell " : "buy "}
                        {money(String(Math.abs(Number(d.rebalance_value))), { compact: true })}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Card>
          </div>

          {assessment.data.constraints.length > 0 && (
            <Card title="Constraints">
              <ul className="space-y-2 text-sm">
                {assessment.data.constraints.map((c) => (
                  <li key={c.id} className="flex items-start gap-2">
                    <Badge tone={c.satisfied ? "good" : "bad"}>{c.satisfied ? "ok" : "breach"}</Badge>
                    <span className="text-ink-2">{c.detail}</span>
                  </li>
                ))}
              </ul>
            </Card>
          )}

          {assessment.data.placement.length > 0 && (
            <Card
              title="Tax placement"
              subtitle="where each preferred asset class actually sits"
            >
              <ul className="space-y-1.5 text-sm">
                {assessment.data.placement.map((p) => (
                  <li key={p.asset_class} className="flex items-center justify-between gap-3">
                    <span>
                      {titleCase(p.asset_class)}{" "}
                      <span className="text-ink-3">prefers {titleCase(p.preferred)}</span>
                    </span>
                    <span className="tnum">
                      {percent(p.fraction_in_preferred)} in place
                      {Number(p.value_elsewhere) > 0 && (
                        <span className="ml-2 text-ink-3">
                          {money(p.value_elsewhere)} elsewhere
                        </span>
                      )}
                    </span>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}

      <Card
        title="Policy document"
        subtitle="edited as YAML, so your comments and reasoning are kept verbatim"
        actions={
          <button
            type="button"
            onClick={() => setEditing((v) => !v)}
            className="rounded-lg border border-line px-2.5 py-1 text-xs text-ink-2 hover:text-ink"
          >
            {editing ? "Cancel" : "Edit"}
          </button>
        }
      >
        {editing ? (
          <>
            <textarea
              value={draft}
              onChange={(e) => setOverride(e.target.value)}
              spellCheck={false}
              rows={22}
              className="w-full rounded-lg border border-line bg-surface-2 p-3 font-mono text-xs"
              aria-label="Policy YAML"
            />
            {save.isError && (
              <p className="mt-2 text-xs text-bad">{(save.error as Error).message}</p>
            )}
            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                onClick={() => draft && save.mutate(draft)}
                disabled={save.isPending}
                className="rounded-lg px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
                style={{ background: "var(--series-1)" }}
              >
                {save.isPending ? "Saving…" : "Save as new version"}
              </button>
              <span className="text-xs text-ink-3">
                Saving never overwrites — the previous version is retired, not replaced.
              </span>
            </div>
          </>
        ) : policy.data?.raw_yaml ? (
          <pre className="overflow-x-auto rounded-lg bg-surface-2 p-3 font-mono text-xs">
            {policy.data.raw_yaml}
          </pre>
        ) : (
          !missing && <Loading />
        )}
      </Card>
    </div>
  );
}
