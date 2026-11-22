import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Holding } from "../lib/api";
import { money, percent, titleCase, toNumber } from "../lib/format";
import { Badge, Card, Empty, Loading, Table, Td, Th } from "../components/ui";

type GroupMode = "flat" | "security";

export function Holdings() {
  const [search, setSearch] = useState("");
  const [account, setAccount] = useState("");
  const [assetClass, setAssetClass] = useState("");
  const [taxTreatment, setTaxTreatment] = useState("");
  const [includeClosed, setIncludeClosed] = useState(false);
  const [group, setGroup] = useState<GroupMode>("flat");
  const [expanded, setExpanded] = useState<Set<string>>(new Set());

  const { data, isLoading, isError } = useQuery({
    queryKey: ["holdings", includeClosed],
    queryFn: () => api.holdings({ include_closed: String(includeClosed) }),
  });

  const rows = useMemo(() => {
    let out = data ?? [];
    if (search) {
      const needle = search.toLowerCase();
      out = out.filter(
        (h) =>
          (h.symbol ?? "").toLowerCase().includes(needle) ||
          h.security_name.toLowerCase().includes(needle),
      );
    }
    if (account) out = out.filter((h) => h.account_name === account);
    if (assetClass) out = out.filter((h) => h.asset_class === assetClass);
    if (taxTreatment) out = out.filter((h) => h.tax_treatment === taxTreatment);
    return out;
  }, [data, search, account, assetClass, taxTreatment]);

  const grouped = useMemo(() => {
    const map = new Map<string, { label: string; total: number; rows: Holding[] }>();
    for (const row of rows) {
      const key = row.security_id;
      const entry = map.get(key) ?? {
        label: row.symbol ?? row.security_name,
        total: 0,
        rows: [],
      };
      entry.total += toNumber(row.market_value);
      entry.rows.push(row);
      map.set(key, entry);
    }
    return [...map.entries()].sort((a, b) => b[1].total - a[1].total);
  }, [rows]);

  if (isLoading) return <Loading />;
  if (isError) return <Empty title="No portfolio yet" hint="Import a document first." />;

  const options = (key: keyof Holding) =>
    [...new Set((data ?? []).map((h) => String(h[key])))].sort();

  const total = rows.reduce((sum, r) => sum + toNumber(r.market_value), 0);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-lg font-semibold">Holdings</h1>
        <p className="tnum text-sm text-ink-3">
          {rows.length} shown · {money(String(total))}
        </p>
      </div>

      {/* Filters in one row above the table. */}
      <div className="flex flex-wrap gap-2">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search symbol or name"
          aria-label="Search holdings"
          className="min-w-48 flex-1 rounded-lg border border-line bg-surface px-3 py-1.5 text-sm"
        />
        <Select value={account} onChange={setAccount} label="Account" options={options("account_name")} />
        <Select value={assetClass} onChange={setAssetClass} label="Asset class" options={options("asset_class")} />
        <Select value={taxTreatment} onChange={setTaxTreatment} label="Tax" options={options("tax_treatment")} />
        <label className="flex items-center gap-2 rounded-lg border border-line bg-surface px-3 py-1.5 text-sm">
          <input
            type="checkbox"
            checked={includeClosed}
            onChange={(e) => setIncludeClosed(e.target.checked)}
          />
          Closed
        </label>
        <div className="flex rounded-lg border border-line bg-surface p-0.5 text-sm">
          {(["flat", "security"] as GroupMode[]).map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => setGroup(mode)}
              className={`rounded-md px-2.5 py-1 ${group === mode ? "bg-surface-2 font-medium" : "text-ink-2"}`}
            >
              {mode === "flat" ? "All" : "By security"}
            </button>
          ))}
        </div>
      </div>

      <Card
        subtitle={
          group === "security"
            ? "Values are summed across accounts; share counts are not — two accounts can report the same security on different share bases."
            : undefined
        }
      >
        {group === "flat" ? (
          <Table>
            <thead>
              <tr>
                <Th>Symbol</Th>
                <Th>Name</Th>
                <Th>Account</Th>
                <Th align="right">Quantity</Th>
                <Th align="right">Price</Th>
                <Th align="right">Value</Th>
                <Th>Class</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={`${row.account_id}-${row.security_id}-${i}`} className="hover:bg-surface-2">
                  <Td className="font-medium">{row.symbol ?? <span className="text-ink-3">—</span>}</Td>
                  <Td>
                    <span className="block max-w-64 truncate">{row.security_name}</span>
                  </Td>
                  <Td>
                    <span className="block max-w-52 truncate text-ink-2">{row.account_name}</span>
                    {row.account_is_stale && (
                      <Badge tone="warn" title="This account's as-of date is unknown">stale</Badge>
                    )}
                  </Td>
                  <Td align="right" className="tnum">{row.quantity ?? "—"}</Td>
                  <Td align="right" className="tnum">{money(row.price)}</Td>
                  <Td align="right" className="tnum">{money(row.market_value)}</Td>
                  <Td>
                    <span className="text-ink-3">{titleCase(row.asset_class)}</span>
                    {row.is_closed && <Badge> closed</Badge>}
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Security</Th>
                <Th align="right">Accounts</Th>
                <Th align="right">Total value</Th>
                <Th align="right">Share</Th>
              </tr>
            </thead>
            <tbody>
              {grouped.map(([id, entry]) => {
                const open = expanded.has(id);
                return (
                  <>
                    <tr
                      key={id}
                      className="cursor-pointer hover:bg-surface-2"
                      onClick={() =>
                        setExpanded((prev) => {
                          const next = new Set(prev);
                          if (next.has(id)) next.delete(id);
                          else next.add(id);
                          return next;
                        })
                      }
                    >
                      <Td className="font-medium">
                        <span className="mr-1.5 inline-block w-3 text-ink-3">{open ? "▾" : "▸"}</span>
                        {entry.label}
                      </Td>
                      <Td align="right" className="tnum">{entry.rows.length}</Td>
                      <Td align="right" className="tnum">{money(String(entry.total))}</Td>
                      <Td align="right" className="tnum text-ink-3">
                        {total ? percent(String(entry.total / total)) : "—"}
                      </Td>
                    </tr>
                    {open &&
                      entry.rows.map((row, i) => (
                        <tr key={`${id}-${i}`} style={{ background: "var(--surface-2)" }}>
                          <Td>
                            <span className="ml-6 text-ink-2">{row.account_name}</span>
                            {row.account_is_stale && <Badge tone="warn"> stale</Badge>}
                          </Td>
                          <Td align="right" className="tnum text-ink-3">
                            {row.quantity ?? "—"} @ {money(row.price)}
                          </Td>
                          <Td align="right" className="tnum">{money(row.market_value)}</Td>
                          <Td align="right" className="tnum text-ink-3">
                            {titleCase(row.tax_treatment)}
                          </Td>
                        </tr>
                      ))}
                  </>
                );
              })}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}

function Select({
  value,
  onChange,
  label,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  label: string;
  options: string[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      aria-label={label}
      className="rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm"
    >
      <option value="">{label}: all</option>
      {options.map((option) => (
        <option key={option} value={option}>
          {option.length > 34 ? `${option.slice(0, 34)}…` : titleCase(option)}
        </option>
      ))}
    </select>
  );
}
