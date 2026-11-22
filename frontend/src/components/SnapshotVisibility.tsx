import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff } from "lucide-react";
import { api, ApiError, type Snapshot } from "../lib/api";

/**
 * Show or hide one snapshot on the value-over-time chart. Display only:
 * nothing is deleted, and which snapshot is current does not change.
 */
export function SnapshotVisibilityToggle({ snapshot }: { snapshot: Snapshot }) {
  const queryClient = useQueryClient();
  const toggle = useMutation({
    mutationFn: () => api.setSnapshotHidden(snapshot.id, !snapshot.hidden_from_history),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["snapshots"] }),
  });
  const hidden = snapshot.hidden_from_history;
  const Icon = hidden ? EyeOff : Eye;
  // A failed save must not look like a click that did nothing.
  const failure = toggle.isError
    ? toggle.error instanceof ApiError && toggle.error.status === 405
      ? "Not saved: the API is older than this page. Restart it."
      : `Not saved: ${(toggle.error as Error).message}`
    : null;
  return (
    <span className="inline-flex items-center gap-2">
      {failure && (
        <span role="alert" className="text-xs text-bad">
          {failure}
        </span>
      )}
      <button
        type="button"
        onClick={() => toggle.mutate()}
        disabled={toggle.isPending}
        aria-pressed={!hidden}
        title={hidden ? "Hidden from the value-over-time chart. Click to show it." : "Shown on the value-over-time chart. Click to hide it."}
        className={`inline-flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs hover:text-ink disabled:opacity-50 ${
          hidden ? "text-ink-3" : "text-ink-2"
        }`}
      >
        <Icon size={13} aria-hidden />
        {hidden ? "Hidden" : "Shown"}
      </button>
    </span>
  );
}
