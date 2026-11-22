import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileUp, GitBranch, Link2, Loader2, Plus, Send, Square, Wrench } from "lucide-react";
import { advisorApi, ApiError, type AdvisorSession } from "../../lib/api";
import {
  applyEvent,
  bareToolName,
  toolLabel,
  turnsFromHistory,
  type ToolUse,
  type Turn,
} from "../../lib/advisorChat";
import { shortDate } from "../../lib/format";
import { Markdown } from "../Markdown";
import { Caveats, Empty, Loading } from "../ui";
import { ProposalPanel } from "./ProposalPanel";

function ToolChips({ tools }: { tools: ToolUse[] }) {
  if (!tools.length) return null;
  return (
    <div className="mb-2 flex flex-wrap gap-1.5">
      {tools.map((tool, i) => (
        <span
          key={`${tool.name}-${i}`}
          title={tool.preview ?? bareToolName(tool.name)}
          className={`inline-flex items-center gap-1 rounded-md border border-line px-1.5 py-0.5 text-[11px] ${
            tool.status === "failed" ? "text-bad" : "text-ink-3"
          }`}
        >
          {tool.status === "running" ? (
            <Loader2 size={11} className="animate-spin" aria-hidden />
          ) : (
            <Wrench size={11} aria-hidden />
          )}
          {toolLabel(tool.name)}
          {tool.status === "failed" && " (failed)"}
        </span>
      ))}
    </div>
  );
}

function TurnView({ turn }: { turn: Turn }) {
  if (turn.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] whitespace-pre-wrap rounded-xl bg-surface-2 px-3.5 py-2.5 text-sm text-ink">
          {turn.text}
        </div>
      </div>
    );
  }
  return (
    <div className="max-w-full">
      <ToolChips tools={turn.tools} />
      {turn.text ? (
        <Markdown text={turn.text} />
      ) : turn.streaming ? (
        <p className="text-sm text-ink-3">Working…</p>
      ) : null}
      {turn.error && (
        <div className="mt-2">
          <Caveats items={[turn.error]} tone="bad" />
        </div>
      )}
    </div>
  );
}

function AttachUrl({ onAttached }: { onAttached: (line: string) => void }) {
  const [open, setOpen] = useState(false);
  const [url, setUrl] = useState("");
  const [note, setNote] = useState("");
  const add = useMutation({
    mutationFn: () => advisorApi.addUrl({ url, note }),
    onSuccess: (item) => {
      onAttached(`I've added a page to my context: ${item.source_url}${note ? ` (${note})` : ""}`);
      setUrl("");
      setNote("");
      setOpen(false);
    },
  });
  if (!open)
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-ink-2 hover:text-ink"
      >
        <Link2 size={14} aria-hidden /> Attach URL
      </button>
    );
  return (
    <form
      className="flex w-full flex-wrap items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        add.mutate();
      }}
    >
      <input
        autoFocus
        type="url"
        required
        placeholder="https://…"
        value={url}
        onChange={(e) => setUrl(e.target.value)}
        className="min-w-0 flex-1 rounded-lg border border-line bg-surface px-2 py-1 text-xs"
        aria-label="URL to add to context"
      />
      <input
        placeholder="Why it matters (optional)"
        value={note}
        onChange={(e) => setNote(e.target.value)}
        className="min-w-0 flex-1 rounded-lg border border-line bg-surface px-2 py-1 text-xs"
        aria-label="Note about the URL"
      />
      <button type="submit" className="rounded-lg border border-line px-2 py-1 text-xs" disabled={add.isPending}>
        Add
      </button>
      <button type="button" className="px-1 text-xs text-ink-3" onClick={() => setOpen(false)}>
        Cancel
      </button>
      {add.isError && <span className="w-full text-xs text-bad">{(add.error as Error).message}</span>}
    </form>
  );
}

function AttachDocument({ onAttached }: { onAttached: (line: string) => void }) {
  const input = useRef<HTMLInputElement>(null);
  const add = useMutation({
    mutationFn: (file: File) => advisorApi.addDocument(file),
    onSuccess: (item) =>
      onAttached(
        item.extraction === "no_text"
          ? `I've uploaded "${item.title}", but it has no readable text.`
          : `I've uploaded "${item.title}" to my context; please read it.`,
      ),
  });
  return (
    <>
      <input
        ref={input}
        type="file"
        accept=".pdf,.txt,.md,application/pdf,text/plain,text/markdown"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) add.mutate(file);
          e.target.value = "";
        }}
      />
      <button
        type="button"
        onClick={() => input.current?.click()}
        disabled={add.isPending}
        className="inline-flex items-center gap-1 rounded-lg px-2 py-1 text-xs text-ink-2 hover:text-ink"
      >
        <FileUp size={14} aria-hidden /> {add.isPending ? "Uploading…" : "Attach PDF"}
      </button>
      {add.isError && <span className="text-xs text-bad">{(add.error as Error).message}</span>}
    </>
  );
}

function Thread({
  session,
  available,
  onFork,
}: {
  session: AdvisorSession;
  available: boolean;
  onFork: (session: AdvisorSession) => void;
}) {
  const queryClient = useQueryClient();
  const history = useQuery({
    queryKey: ["advisor-messages", session.id],
    queryFn: () => advisorApi.messages(session.id),
    enabled: available,
    retry: false,
  });
  // Turns sent in this visit, layered over the stored history until it refetches.
  const [live, setLive] = useState<{ sessionId: string; turns: Turn[] }>({ sessionId: session.id, turns: [] });
  const liveTurns = live.sessionId === session.id ? live.turns : [];
  const stored = useMemo(() => turnsFromHistory(history.data ?? []), [history.data]);
  const turns = liveTurns.length ? [...stored, ...liveTurns] : stored;

  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const bottom = useRef<HTMLDivElement>(null);

  const lastText = liveTurns[liveTurns.length - 1]?.text;
  useEffect(() => {
    bottom.current?.scrollIntoView({ block: "end" });
  }, [turns.length, lastText]);

  const fork = useMutation({
    mutationFn: () => advisorApi.forkSession(session.id),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["advisor-sessions"] });
      onFork(created);
    },
  });

  const send = async () => {
    const message = draft.trim();
    if (!message || sending) return;
    setDraft("");
    setSending(true);
    const controller = new AbortController();
    abort.current = controller;
    const update = (fn: (turn: Turn) => Turn) =>
      setLive((prev) => {
        const turns = [...prev.turns];
        turns[turns.length - 1] = fn(turns[turns.length - 1]);
        return { ...prev, turns };
      });
    setLive((prev) => ({
      sessionId: session.id,
      turns: [
        ...(prev.sessionId === session.id ? prev.turns : []),
        { role: "user", text: message, tools: [] },
        { role: "assistant", text: "", tools: [], streaming: true },
      ],
    }));
    try {
      await advisorApi.stream(session.id, message, (event) => update((t) => applyEvent(t, event)), controller.signal);
    } catch (error) {
      const stopped = error instanceof DOMException && error.name === "AbortError";
      update((t) => ({
        ...t,
        error: stopped ? "Stopped." : error instanceof ApiError ? error.message : String(error),
      }));
    } finally {
      update((t) => ({ ...t, streaming: false }));
      setSending(false);
      abort.current = null;
      void queryClient.invalidateQueries({ queryKey: ["advisor-sessions"] });
      void queryClient.invalidateQueries({ queryKey: ["advisor-proposal", session.id] });
    }
  };

  return (
    <section className="flex min-h-[32rem] flex-col rounded-xl border border-line bg-surface" aria-label="Conversation">
      <header className="flex items-center justify-between gap-2 border-b border-line px-4 py-2.5">
        <div className="min-w-0">
          <h2 className="truncate text-sm font-semibold">{session.title}</h2>
          <p className="text-xs text-ink-3">Started {shortDate(session.created_at.slice(0, 10))}</p>
        </div>
        <button
          type="button"
          onClick={() => fork.mutate()}
          disabled={!available || fork.isPending || sending}
          title="Branch this conversation to try a different direction"
          className="inline-flex items-center gap-1 rounded-lg border border-line px-2 py-1 text-xs text-ink-2 hover:text-ink disabled:opacity-50"
        >
          <GitBranch size={13} aria-hidden /> Branch
        </button>
      </header>

      <div className="flex-1 space-y-5 overflow-y-auto px-4 py-4" aria-live="polite">
        {history.isLoading && <Loading label="Loading the conversation" />}
        {history.isError && (
          <Caveats items={[(history.error as Error).message]} tone="bad" title="Could not load this conversation" />
        )}
        {!available && !history.data && (
          <p className="text-sm text-ink-3">
            This conversation is held by Hermes, so it cannot be shown while the advisor is unreachable.
          </p>
        )}
        {available && !history.isLoading && !turns.length && (
          <div className="space-y-2 text-sm text-ink-3">
            <p>Ask for advice, for example:</p>
            <ul className="list-disc space-y-1 pl-5">
              <li>“Given my philosophy and today’s news, how should I rebalance?”</li>
              <li>“I think rates fall next year. What would you change?”</li>
              <li>“Get me back to target without realising gains in taxable accounts.”</li>
            </ul>
          </div>
        )}
        {turns.map((turn, i) => (
          <TurnView key={i} turn={turn} />
        ))}
        <div ref={bottom} />
      </div>

      <footer className="border-t border-line p-3">
        <div className="mb-2 flex flex-wrap items-center gap-1">
          <AttachUrl onAttached={(line) => setDraft((d) => (d ? `${d}\n${line}` : line))} />
          <AttachDocument onAttached={(line) => setDraft((d) => (d ? `${d}\n${line}` : line))} />
        </div>
        <form
          className="flex items-end gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void send();
              }
            }}
            rows={2}
            placeholder={available ? "Message the advisor" : "The advisor is not reachable"}
            disabled={!available}
            aria-label="Message the advisor"
            className="min-h-[2.75rem] flex-1 resize-y rounded-lg border border-line bg-surface px-3 py-2 text-sm"
          />
          {sending ? (
            <button
              type="button"
              onClick={() => abort.current?.abort()}
              className="inline-flex items-center gap-1 rounded-lg border border-line px-3 py-2 text-sm"
            >
              <Square size={14} aria-hidden /> Stop
            </button>
          ) : (
            <button
              type="submit"
              disabled={!available || !draft.trim()}
              className="inline-flex items-center gap-1 rounded-lg bg-ink px-3 py-2 text-sm font-medium text-surface disabled:opacity-40"
            >
              <Send size={14} aria-hidden /> Send
            </button>
          )}
        </form>
        <p className="mt-2 text-[11px] text-ink-3">
          Advice only. Nothing here places a trade; you decide what to do.
        </p>
      </footer>
    </section>
  );
}

export function AdvisorChat({
  selectedId,
  onSelect,
}: {
  selectedId: string | null;
  onSelect: (id: string) => void;
}) {
  const queryClient = useQueryClient();
  const status = useQuery({ queryKey: ["advisor-status"], queryFn: advisorApi.status, staleTime: 10_000 });
  const sessions = useQuery({ queryKey: ["advisor-sessions"], queryFn: advisorApi.sessions });
  const available = status.data?.reachable ?? false;

  const create = useMutation({
    mutationFn: () => advisorApi.createSession(),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["advisor-sessions"] });
      onSelect(created.id);
    },
  });

  const selected = sessions.data?.find((s) => s.id === selectedId) ?? sessions.data?.[0] ?? null;

  return (
    <div className="space-y-4">
      {status.data && !status.data.reachable && (
        <Caveats
          tone={status.data.enabled ? "bad" : "info"}
          title={status.data.enabled ? "The advisor is not reachable" : "The advisor is not set up"}
          items={[
            status.data.detail ?? "Hermes did not answer.",
            "Run `portfolio hermes install` for the setup steps. Everything else in this app works without it.",
          ]}
        />
      )}
      {create.isError && <Caveats tone="bad" items={[(create.error as Error).message]} />}

      <div className="grid grid-cols-[minmax(0,1fr)] items-start gap-4 lg:grid-cols-[15rem_minmax(0,1fr)] 2xl:grid-cols-[15rem_minmax(0,1fr)_24rem]">
        <aside className="space-y-2" aria-label="Conversations">
          <button
            type="button"
            onClick={() => create.mutate()}
            disabled={!available || create.isPending}
            className="inline-flex w-full items-center justify-center gap-1 rounded-lg border border-line bg-surface px-3 py-2 text-sm font-medium hover:bg-surface-2 disabled:opacity-50"
          >
            <Plus size={14} aria-hidden /> New conversation
          </button>
          {sessions.isLoading && <Loading />}
          <ul className="space-y-1">
            {(sessions.data ?? []).map((s) => (
              <li key={s.id}>
                <button
                  type="button"
                  onClick={() => onSelect(s.id)}
                  aria-current={selected?.id === s.id ? "true" : undefined}
                  className={`w-full rounded-lg px-3 py-2 text-left text-sm ${
                    selected?.id === s.id ? "bg-surface-2 font-medium text-ink" : "text-ink-2 hover:bg-surface-2"
                  }`}
                >
                  <span className="block truncate">{s.title}</span>
                  <span className="block text-[11px] text-ink-3">
                    {shortDate(s.updated_at.slice(0, 10))}
                    {s.forked_from_id ? " · branch" : ""}
                    {s.has_proposal ? " · proposal" : ""}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </aside>

        {selected ? (
          <>
            <Thread key={selected.id} session={selected} available={available} onFork={(s) => onSelect(s.id)} />
            <div className="lg:col-start-2 2xl:col-start-auto">
              <ProposalPanel key={`proposal-${selected.id}`} sessionId={selected.id} />
            </div>
          </>
        ) : (
          !sessions.isLoading && (
            <Empty
              title="No conversations yet"
              hint="Start one to ask for advice. The advisor reads your portfolio, your philosophy and the context you give it, and can research current events."
            />
          )
        )}
      </div>
    </div>
  );
}
