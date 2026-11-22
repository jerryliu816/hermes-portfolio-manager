import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Account } from "../lib/api";
import { money, percent, shortDate, titleCase } from "../lib/format";
import { Badge, Card, Caveats, Empty, Loading, Table, Td, Th } from "../components/ui";

const ACCOUNT_TYPES = [
  "brokerage", "joint", "traditional_ira", "rollover_ira", "roth_ira",
  "529", "hsa", "corporate", "equity_plan", "trust", "unknown",
];
const TAX_TREATMENTS = ["taxable", "tax_deferred", "tax_free", "education", "unknown"];

export function Accounts() {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const [editing, setEditing] = useState<Account | null>(null);

  if (accounts.isLoading) return <Loading />;
  if (accounts.isError) return <Empty title="No portfolio yet" hint="Import a document first." />;

  const rows = accounts.data ?? [];
  const unclassified = rows.filter((a) => a.needs_classification);
  const total = rows.reduce((sum, a) => sum + Number(a.total_value), 0);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Accounts</h1>

      {unclassified.length > 0 && (
        <Caveats
          title="Account setup"
          items={[
            `${unclassified.length} account${unclassified.length === 1 ? " has" : "s have"} no confirmed tax treatment, so ${
              unclassified.length === 1 ? "it is" : "they are"
            } excluded from tax analysis. Confirm ${unclassified.length === 1 ? "it" : "them"} below — a guess would quietly distort every tax figure.`,
          ]}
        />
      )}

      <Card>
        <Table>
          <thead>
            <tr>
              <Th>Account</Th>
              <Th>Type</Th>
              <Th>Tax treatment</Th>
              <Th align="right">Value</Th>
              <Th>Source</Th>
              <Th>As of</Th>
              <Th />
            </tr>
          </thead>
          <tbody>
            {rows.map((account) => (
              <tr key={account.id} className="hover:bg-surface-2">
                <Td>
                  <div className="max-w-72 truncate font-medium">{account.display_name}</div>
                  <div className="text-xs text-ink-3">
                    {account.institution === "unknown"
                      ? (account.supplied_by ?? "unknown institution")
                      : account.institution}{" "}
                    · {account.account_key_masked}
                    {account.is_aggregated && (
                      <Badge title="Reported second-hand by an aggregation feed"> aggregated</Badge>
                    )}
                  </div>
                </Td>
                <Td className="text-ink-2">{titleCase(account.account_type)}</Td>
                <Td>
                  {account.needs_classification ? (
                    <Badge tone="warn" title="Excluded from tax analysis until confirmed">
                      needs confirming
                    </Badge>
                  ) : (
                    <span className="text-ink-2">{titleCase(account.tax_treatment)}</span>
                  )}
                </Td>
                <Td align="right" className="tnum">
                  <div>{money(account.total_value)}</div>
                  {Number(account.cash_balance) > 0 && (
                    <div className="text-xs text-ink-3">
                      incl. {money(account.cash_balance)} cash
                    </div>
                  )}
                  <div className="text-xs text-ink-3">
                    {total ? percent(String(Number(account.total_value) / total)) : ""}
                  </div>
                </Td>
                <Td className="text-ink-2">{account.supplied_by ?? "—"}</Td>
                <Td>
                  {account.is_stale ? (
                    <Badge tone="warn" title={account.staleness_reason ?? undefined}>
                      unknown
                    </Badge>
                  ) : (
                    <span className="text-ink-3">
                      {account.as_of_date ? shortDate(account.as_of_date) : "—"}
                    </span>
                  )}
                </Td>
                <Td align="right">
                  <button
                    type="button"
                    onClick={() => setEditing(account)}
                    className="rounded-md border border-line px-2 py-1 text-xs text-ink-2 hover:text-ink"
                  >
                    Classify
                  </button>
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </Card>

      {editing && <ClassifyDialog account={editing} onClose={() => setEditing(null)} />}
    </div>
  );
}

function ClassifyDialog({ account, onClose }: { account: Account; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [accountType, setAccountType] = useState(account.account_type);
  const [taxTreatment, setTaxTreatment] = useState(account.tax_treatment);
  const [reason, setReason] = useState("");

  const save = useMutation({
    mutationFn: () =>
      api.classifyAccount(account.id, {
        account_type: accountType,
        tax_treatment: taxTreatment,
        reason: reason || undefined,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries();
      onClose();
    },
  });

  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`Classify ${account.display_name}`}
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-xl border border-line bg-surface p-5"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-sm font-semibold">{account.display_name}</h2>
        <p className="mt-1 text-xs text-ink-3">
          {account.institution === "unknown"
            ? (account.supplied_by ?? "unknown institution")
            : account.institution}{" "}
          · {account.account_key_masked}
        </p>
        <p className="mt-3 text-xs text-ink-2">
          Tax treatment drives every placement figure, so an unconfirmed account is left out of
          them rather than assumed. Your choice is recorded and a later import will not overwrite it.
        </p>

        <label className="mt-4 block text-xs font-medium text-ink-2">
          Account type
          <select
            value={accountType}
            onChange={(e) => setAccountType(e.target.value)}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm"
          >
            {ACCOUNT_TYPES.map((t) => (
              <option key={t} value={t}>{titleCase(t)}</option>
            ))}
          </select>
        </label>

        <label className="mt-3 block text-xs font-medium text-ink-2">
          Tax treatment
          <select
            value={taxTreatment}
            onChange={(e) => setTaxTreatment(e.target.value)}
            className="mt-1 w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm"
          >
            {TAX_TREATMENTS.map((t) => (
              <option key={t} value={t}>{titleCase(t)}</option>
            ))}
          </select>
        </label>

        <label className="mt-3 block text-xs font-medium text-ink-2">
          Note (optional)
          <input
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            placeholder="Why — recorded in the audit log"
            className="mt-1 w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm"
          />
        </label>

        {save.isError && (
          <p className="mt-3 text-xs text-bad">{(save.error as Error).message}</p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rounded-lg px-3 py-1.5 text-sm text-ink-2">
            Cancel
          </button>
          <button
            type="button"
            onClick={() => save.mutate()}
            disabled={save.isPending}
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            style={{ background: "var(--series-1)" }}
          >
            {save.isPending ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}
