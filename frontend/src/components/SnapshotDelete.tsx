import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Trash2 } from "lucide-react";
import { api, ApiError, type Snapshot } from "../lib/api";
import { money, shortDate } from "../lib/format";
import { Caveats, Loading } from "./ui";

/** Delete one snapshot, after showing exactly what that will do. */
export function SnapshotDeleteButton({ snapshot }: { snapshot: Snapshot }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        aria-label={`Delete the ${shortDate(snapshot.as_of_date)} snapshot`}
        title="Delete this snapshot"
        className="inline-flex items-center rounded-md border border-line p-1.5 text-ink-3 hover:text-bad"
      >
        <Trash2 size={13} aria-hidden />
      </button>
      {open && <DeleteDialog snapshot={snapshot} onClose={() => setOpen(false)} />}
    </>
  );
}

function DeleteDialog({ snapshot, onClose }: { snapshot: Snapshot; onClose: () => void }) {
  const queryClient = useQueryClient();
  const plan = useQuery({
    queryKey: ["snapshot-removal", snapshot.id],
    queryFn: () => api.snapshotRemoval(snapshot.id),
    retry: false,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteSnapshot(snapshot.id, plan.data?.needs_force ?? false),
    onSuccess: () => {
      // Every figure on every page may have rested on this snapshot.
      void queryClient.invalidateQueries();
      onClose();
    },
  });

  const p = plan.data;
  const date = shortDate(snapshot.as_of_date);
  const failure = remove.isError
    ? remove.error instanceof ApiError && remove.error.status === 405
      ? "Not deleted: the API is older than this page. Restart it."
      : `Not deleted: ${(remove.error as Error).message}`
    : plan.isError
      ? `Could not work out what deleting would do: ${(plan.error as Error).message}`
      : null;

  return (
    <div
      className="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      aria-label={`Delete the ${date} snapshot`}
      onClick={onClose}
    >
      <div
        className="w-full max-w-md rounded-xl border border-line bg-surface p-5 text-left"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-sm font-semibold">Delete the {date} snapshot?</h2>
        <p className="mt-1 text-xs text-ink-3 tnum">
          {money(snapshot.total_value)} · {snapshot.account_count} account
          {snapshot.account_count === 1 ? "" : "s"}
        </p>

        {plan.isLoading && <Loading label="Working out what this would remove" />}

        {p && (
          <div className="mt-3 space-y-3 text-sm text-ink-2">
            <p>
              This permanently deletes the snapshot and its {p.account_count} account and{" "}
              {p.position_count} holding readings. It cannot be undone. Your accounts, securities
              and the original files on disk are kept.
            </p>
            {p.documents.length > 0 && (
              <div>
                <p className="text-xs text-ink-3">Can be imported again afterwards:</p>
                <ul className="mt-1 list-inside list-disc text-sm">
                  {p.documents.map((name) => (
                    <li key={name}>{name}</li>
                  ))}
                </ul>
              </div>
            )}
            {p.shared_documents.length > 0 && (
              <p className="text-xs text-ink-3">
                Kept because another snapshot uses it: {p.shared_documents.join(", ")}
              </p>
            )}
            {p.objections.length > 0 && (
              <Caveats tone="warn" title="Deleting it also affects" items={p.objections} />
            )}
          </div>
        )}

        {failure && (
          <p role="alert" className="mt-3 text-xs text-bad">
            {failure}
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <button type="button" onClick={onClose} className="rounded-lg px-3 py-1.5 text-sm text-ink-2">
            Cancel
          </button>
          <button
            type="button"
            onClick={() => remove.mutate()}
            disabled={!p || remove.isPending}
            className="rounded-lg px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
            style={{ background: "var(--status-critical)" }}
          >
            {remove.isPending ? "Deleting…" : p?.needs_force ? "Delete anyway" : "Delete permanently"}
          </button>
        </div>
      </div>
    </div>
  );
}
