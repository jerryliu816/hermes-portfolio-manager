import { useSearchParams } from "react-router-dom";
import { AdvisorChat } from "../components/advisor/Chat";
import { ContextPanel } from "../components/advisor/ContextPanel";

const TABS = [
  { id: "chat", label: "Chat" },
  { id: "context", label: "Philosophy & context" },
] as const;

export function Advisor() {
  const [params, setParams] = useSearchParams();
  const tab = params.get("tab") === "context" ? "context" : "chat";
  const session = params.get("session");

  const update = (changes: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [key, value] of Object.entries(changes)) {
      if (value === null) next.delete(key);
      else next.set(key, value);
    }
    setParams(next, { replace: true });
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold">Advisor</h1>
          <p className="max-w-3xl text-sm text-ink-3">
            Proposes what to sell and buy, from your portfolio, your philosophy, your views and
            current events. Every figure comes from this app's own calculations, not the model.
            It cannot trade, and nothing it proposes is acted on unless you do it.
          </p>
        </div>
        <div className="flex gap-1 rounded-lg border border-line p-0.5" role="tablist" aria-label="Advisor">
          {TABS.map((t) => (
            <button
              key={t.id}
              type="button"
              role="tab"
              aria-selected={tab === t.id}
              onClick={() => update({ tab: t.id === "chat" ? null : t.id })}
              className={`rounded-md px-3 py-1.5 text-sm ${tab === t.id ? "bg-surface-2 font-medium text-ink" : "text-ink-2 hover:text-ink"}`}
            >
              {t.label}
            </button>
          ))}
        </div>
      </div>
      {tab === "chat" ? (
        <AdvisorChat selectedId={session} onSelect={(id) => update({ session: id })} />
      ) : (
        <ContextPanel />
      )}
    </div>
  );
}
