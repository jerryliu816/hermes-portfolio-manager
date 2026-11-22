import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Plus, Upload, X } from "lucide-react";
import { api, ApiError } from "../lib/api";
import { money, shortDate } from "../lib/format";
import { Badge, Card, Caveats, Loading, Table, Td, Th } from "../components/ui";
import { SnapshotVisibilityToggle } from "../components/SnapshotVisibility";
import { SnapshotDeleteButton } from "../components/SnapshotDelete";

interface ImportResult {
  outcome: string;
  filename: string;
  sha256_prefix: string;
  message: string;
  snapshot_id: string | null;
  reconciliation: {
    summary: Record<string, number>;
    failures: string[];
    cash_residuals: Record<string, string>;
  } | null;
  validation: Record<string, unknown> | null;
  joined: boolean;
  unmatched_accounts: string[];
}

interface ImportBatch {
  results: ImportResult[];
  order: string[];
  reordered: boolean;
  as_of_date: string | null;
}

export function Imports() {
  const queryClient = useQueryClient();
  const inputRef = useRef<HTMLInputElement>(null);
  const [chosen, setChosen] = useState<File[]>([]);
  const [batch, setBatch] = useState<ImportBatch | null>(null);
  const snapshots = useQuery({ queryKey: ["snapshots"], queryFn: api.snapshots, retry: false });

  // Empty means a new snapshot dated from the document; otherwise the as-of
  // date of a live snapshot to join.
  const [joinDate, setJoinDate] = useState("");
  const live = (snapshots.data ?? []).filter((s) => !s.superseded_by);

  const upload = useMutation({
    mutationFn: ({ files, force }: { files: File[]; force: boolean }) =>
      api.uploadDocuments(files, {
        asOf: joinDate || undefined,
        forceAsOf: force,
      }) as Promise<ImportBatch>,
    onSuccess: (data) => {
      setBatch(data);
      setChosen([]);
      setJoinDate("");
      void queryClient.invalidateQueries();
    },
  });

  // A document dated far from the snapshot it is joining is refused unless
  // confirmed: a month-old statement would reconcile perfectly against itself.
  const refusal =
    upload.error instanceof ApiError &&
    upload.error.status === 409 &&
    (upload.error.detail as { kind?: string })?.kind === "date_override_refused"
      ? (upload.error.detail as { stated: string; requested: string; tolerance_days: number })
      : null;

  const addFiles = (list: FileList | null) => {
    // Copy now: a FileList is live, and clearing the input to allow re-picking
    // the same file empties it before the state update below reads it.
    const files = Array.from(list ?? []);
    if (files.length === 0) return;
    upload.reset();
    setChosen((current) => {
      const names = new Set(current.map((f) => f.name));
      return [...current, ...files.filter((f) => !names.has(f.name))];
    });
  };

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-semibold">Imports</h1>

      <Card>
        <p className="max-w-3xl text-sm text-ink-2">
          Upload a brokerage export — a CSV or a PDF. The original file is kept, so a reading can
          always be re-derived later. Re-uploading an identical file does nothing: deduplication
          is by content, not filename.
        </p>
        <p className="mt-2 max-w-3xl text-sm text-ink-2">
          Have two exports of the same portfolio from a day or two apart? Add both and import them
          together. They become one snapshot: the file covering the most accounts goes first, and
          the other joins it with its fresher values for the accounts it covers. Imported one at a
          time, the second would open a snapshot of its own showing only part of the portfolio.
        </p>

        <input
          ref={inputRef}
          type="file"
          multiple
          accept=".csv,.pdf,.txt"
          className="hidden"
          onChange={(e) => {
            addFiles(e.target.files);
            e.target.value = "";
          }}
        />

        {chosen.length > 0 && (
          <ul className="mt-4 max-w-xl divide-y divide-line rounded-lg border border-line text-sm">
            {chosen.map((file) => (
              <li key={file.name} className="flex items-center justify-between gap-3 px-3 py-2">
                <span className="flex min-w-0 items-center gap-2">
                  <FileText size={14} className="shrink-0 text-ink-3" aria-hidden />
                  <span className="truncate">{file.name}</span>
                </span>
                <button
                  type="button"
                  onClick={() => {
                    upload.reset();
                    setChosen((current) => current.filter((f) => f !== file));
                  }}
                  disabled={upload.isPending}
                  aria-label={`Remove ${file.name}`}
                  className="rounded p-1 text-ink-3 hover:text-ink disabled:opacity-50"
                >
                  <X size={14} />
                </button>
              </li>
            ))}
          </ul>
        )}

        {chosen.length > 0 && live.length > 0 && (
          <label className="mt-4 block max-w-xl text-xs font-medium text-ink-2">
            File into
            <select
              value={joinDate}
              onChange={(e) => {
                setJoinDate(e.target.value);
                upload.reset();
              }}
              disabled={upload.isPending}
              className="mt-1 w-full rounded-lg border border-line bg-surface px-2.5 py-1.5 text-sm"
            >
              <option value="">A new snapshot, dated from the document</option>
              {live.map((s) => (
                <option key={s.id} value={s.as_of_date}>
                  Join the {shortDate(s.as_of_date)} snapshot · {s.account_count} account
                  {s.account_count === 1 ? "" : "s"}
                </option>
              ))}
            </select>
            <span className="mt-1 block font-normal text-ink-3">
              {joinDate
                ? "The file supplies the accounts it wins (a direct source beats an aggregation feed). " +
                  "It cannot add accounts the snapshot does not already have; any it reports are listed, not dropped silently."
                : "Choose a snapshot to add a fresher statement to it — say, a brokerage's own statement into a snapshot that only had its accounts second-hand."}
            </span>
          </label>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            disabled={upload.isPending}
            className={
              chosen.length > 0
                ? "inline-flex items-center gap-2 rounded-lg border border-line px-3 py-1.5 text-sm text-ink-2 hover:text-ink disabled:opacity-50"
                : "inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            }
            style={chosen.length > 0 ? undefined : { background: "var(--series-1)" }}
          >
            {chosen.length > 0 ? <Plus size={15} /> : <Upload size={15} />}
            {chosen.length > 0 ? "Add another file" : "Choose files"}
          </button>
          {chosen.length > 0 && (
            <button
              type="button"
              onClick={() => upload.mutate({ files: chosen, force: false })}
              disabled={upload.isPending}
              className="inline-flex items-center gap-2 rounded-lg px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
              style={{ background: "var(--series-1)" }}
            >
              <Upload size={15} />
              {upload.isPending
                ? "Extracting…"
                : joinDate
                  ? `Add ${chosen.length === 1 ? "it" : `${chosen.length} files`} to ${shortDate(joinDate)}`
                  : chosen.length === 1
                    ? "Import 1 file"
                    : `Import ${chosen.length} files as one portfolio`}
            </button>
          )}
          {upload.isPending && (
            <span className="text-sm text-ink-3">
              reading the documents, validating holdings, reconciling accounts…
            </span>
          )}
        </div>

        {refusal ? (
          <div className="mt-4 max-w-xl">
            <Caveats
              tone="warn"
              title="Not imported: the dates are far apart"
              items={[
                `The document says ${shortDate(refusal.stated)}, but you chose the ${shortDate(refusal.requested)} snapshot — ` +
                  `more than ${refusal.tolerance_days} days apart. Joining would present the older numbers as of the later date.`,
              ]}
            />
            <button
              type="button"
              onClick={() => upload.mutate({ files: chosen, force: true })}
              disabled={upload.isPending}
              className="mt-2 rounded-lg border border-line px-3 py-1.5 text-sm text-ink-2 hover:text-ink disabled:opacity-50"
            >
              They describe the same portfolio — add it anyway
            </button>
          </div>
        ) : upload.isError && (
          <div className="mt-4">
            <Caveats
              tone="bad"
              title="Nothing was imported"
              items={[String((upload.error as Error).message)]}
            />
          </div>
        )}
      </Card>

      {batch && (
        <>
          {batch.results.length > 1 && (
            <p className="text-sm text-ink-3">
              {batch.reordered
                ? `Filed widest first, so every account survives the join: ${batch.order.join(" then ")}.`
                : `Filed in the order given: ${batch.order.join(" then ")}.`}
              {batch.as_of_date && ` One snapshot, as of ${shortDate(batch.as_of_date)}.`}
            </p>
          )}
          {batch.results.map((result) => (
            <ImportResultCard key={result.filename} result={result} several={batch.results.length > 1} />
          ))}
        </>
      )}

      <Card title="Snapshots">
        {snapshots.isLoading ? (
          <Loading />
        ) : (snapshots.data ?? []).length === 0 ? (
          <p className="text-sm text-ink-3">Nothing imported yet.</p>
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Date</Th>
                <Th>Status</Th>
                <Th align="right">Value</Th>
                <Th align="right">Accounts</Th>
                <Th align="right">Holdings</Th>
                <Th>Notes</Th>
                <Th>On chart</Th>
                <Th>
                  <span className="sr-only">Delete</span>
                </Th>
              </tr>
            </thead>
            <tbody>
              {(snapshots.data ?? []).map((snapshot) => (
                <tr key={snapshot.id}>
                  <Td>{shortDate(snapshot.as_of_date)}</Td>
                  <Td>
                    <span className="text-ink-2">{snapshot.status.replace(/_/g, " ")}</span>
                    {snapshot.completeness !== "complete" && (
                      <Badge tone="bad" title="An account in the source was not extracted">
                        {" "}{snapshot.completeness}
                      </Badge>
                    )}
                    {snapshot.superseded_by && <Badge> superseded</Badge>}
                  </Td>
                  <Td align="right" className="tnum">{money(snapshot.total_value)}</Td>
                  <Td align="right" className="tnum">{snapshot.account_count}</Td>
                  <Td align="right" className="tnum">{snapshot.position_count}</Td>
                  <Td className="text-xs text-ink-3">
                    {snapshot.skipped_accounts.length > 0 &&
                      `${snapshot.skipped_accounts.length} skipped`}
                    {snapshot.unmatched_enrichment.length > 0 &&
                      ` · ${snapshot.unmatched_enrichment.length} unmatched`}
                  </Td>
                  <Td>
                    {/* A superseded snapshot is never plotted, so there is nothing to toggle. */}
                    {snapshot.superseded_by ? (
                      <span className="text-xs text-ink-3">—</span>
                    ) : (
                      <SnapshotVisibilityToggle snapshot={snapshot} />
                    )}
                  </Td>
                  <Td>
                    <SnapshotDeleteButton snapshot={snapshot} />
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  );
}

function ImportResultCard({ result, several }: { result: ImportResult; several: boolean }) {
  const failures = result.reconciliation?.failures ?? [];
  const skipped =
    (result.validation?.skipped_accounts as { display_name: string; reason: string }[]) ?? [];
  return (
    <Card
      title={several ? result.filename : "Result"}
      subtitle={`${result.outcome}${result.joined ? " · joined the snapshot" : ""} · sha256 ${result.sha256_prefix}`}
    >
      <p className="text-sm text-ink-2">{result.message}</p>

      {result.reconciliation && (
        <p className="mt-2 text-sm text-ink-3 tnum">
          {result.reconciliation.summary.passed} checks passed,{" "}
          {result.reconciliation.summary.failed} failed,{" "}
          {result.reconciliation.summary.skipped} skipped
          <span className="ml-1 text-ink-3">
            (skipped means a field the document does not report, not an error)
          </span>
        </p>
      )}

      {result.unmatched_accounts.length > 0 && (
        <div className="mt-4">
          <Caveats
            tone="bad"
            title={`${result.unmatched_accounts.length} account(s) not added`}
            items={result.unmatched_accounts.map(
              (name) => `${name} — this document reports it, but the snapshot it joined does not cover it`,
            )}
          />
        </div>
      )}

      {skipped.length > 0 && (
        <div className="mt-4">
          <h3 className="mb-1.5 text-xs font-semibold uppercase tracking-wide text-ink-3">
            Not imported
          </h3>
          <ul className="space-y-1 text-sm text-ink-2">
            {skipped.map((entry) => (
              <li key={entry.display_name}>
                {entry.display_name}{" "}
                <span className="text-ink-3">— {entry.reason.replace(/_/g, " ")}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {failures.length > 0 && (
        <div className="mt-4">
          <Caveats
            tone="bad"
            title={`${failures.length} arithmetic check(s) failed`}
            items={failures.slice(0, 8)}
          />
          <p className="mt-2 text-xs text-ink-3">
            These are the document's own numbers against what they add up to. The import was kept
            and marked for review rather than discarded.
          </p>
        </div>
      )}
    </Card>
  );
}
