import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { advisorApi, ApiError, type AdvisorContextItem } from "../../lib/api";
import { shortDate } from "../../lib/format";
import { Markdown } from "../Markdown";
import { Badge, Card, Caveats, Loading } from "../ui";

const KIND_LABEL = { view: "View", preference: "Preference", url: "Page", document: "Document" } as const;

const inputClass = "w-full rounded-lg border border-line bg-surface px-3 py-2 text-sm";
const buttonClass =
  "shrink-0 whitespace-nowrap rounded-lg border border-line px-3 py-1.5 text-sm hover:bg-surface-2 disabled:opacity-50";

function PhilosophyCard() {
  const queryClient = useQueryClient();
  const philosophy = useQuery({ queryKey: ["philosophy"], queryFn: advisorApi.philosophy, retry: false });
  const [draft, setDraft] = useState<string | null>(null);
  const missing = philosophy.isError && (philosophy.error as ApiError).isMissing;

  const save = useMutation({
    mutationFn: (text: string) => advisorApi.savePhilosophy(text),
    onSuccess: () => {
      setDraft(null);
      void queryClient.invalidateQueries({ queryKey: ["philosophy"] });
    },
  });

  const editing = draft !== null;
  return (
    <Card
      title="Investment philosophy"
      subtitle={
        philosophy.data
          ? `Version ${philosophy.data.version}, saved ${shortDate(philosophy.data.created_at.slice(0, 10))}. The advisor reads it before every proposal.`
          : "In your own words. The advisor reads it before every proposal and never infers it from your holdings."
      }
      actions={
        !editing && (
          <button
            type="button"
            className={buttonClass}
            onClick={async () =>
              setDraft(philosophy.data?.body_markdown ?? (await advisorApi.starterPhilosophy()))
            }
          >
            {philosophy.data ? "Edit" : "Write one"}
          </button>
        )
      }
    >
      {philosophy.isLoading && <Loading />}
      {editing ? (
        <div className="space-y-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={16}
            className={`${inputClass} font-mono text-xs`}
            aria-label="Investment philosophy"
          />
          {save.isError && <Caveats tone="bad" items={[(save.error as Error).message]} />}
          <div className="flex gap-2">
            <button
              type="button"
              className={buttonClass}
              disabled={save.isPending}
              onClick={() => save.mutate(draft)}
            >
              Save as a new version
            </button>
            <button type="button" className="px-2 text-sm text-ink-3" onClick={() => setDraft(null)}>
              Cancel
            </button>
          </div>
        </div>
      ) : philosophy.data ? (
        <div className="max-h-96 overflow-y-auto">
          <Markdown text={philosophy.data.body_markdown} />
        </div>
      ) : (
        missing && (
          <p className="text-sm text-ink-2">
            Nothing written yet. Without it the advisor will ask about your goals, horizon and risk
            tolerance before proposing anything.
          </p>
        )
      )}
    </Card>
  );
}

function ContextRow({ item }: { item: AdvisorContextItem }) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const refresh = () => void queryClient.invalidateQueries({ queryKey: ["advisor-context"] });
  const update = useMutation({
    mutationFn: (body: Parameters<typeof advisorApi.updateContext>[1]) =>
      advisorApi.updateContext(item.id, body),
    onSuccess: refresh,
  });
  const remove = useMutation({ mutationFn: () => advisorApi.deleteContext(item.id), onSuccess: refresh });

  const long = item.body.length > 280;
  return (
    <li className="space-y-2 py-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-sm font-medium text-ink">{item.title}</span>
        <Badge>{KIND_LABEL[item.kind]}</Badge>
        {item.is_expired && (
          <Badge tone="warn" title="Past its expiry date; the advisor will ask whether it still holds">
            Expired {shortDate(item.expires_on!)}
          </Badge>
        )}
        {!item.is_expired && item.expires_on && <Badge>Until {shortDate(item.expires_on)}</Badge>}
        {!item.confirmed && (
          <Badge tone="warn" title="Recorded by the advisor from a chat. Confirm it, edit it or delete it.">
            From the advisor, unconfirmed
          </Badge>
        )}
        {item.extraction === "no_text" && (
          <Badge tone="bad" title="This PDF has no text layer; the advisor cannot read it">
            No readable text
          </Badge>
        )}
        {item.extraction === "not_captured" && <Badge title="The advisor reads this page live">Read live</Badge>}
      </div>
      {item.source_url && (
        <a
          href={item.source_url}
          target="_blank"
          rel="noopener noreferrer nofollow"
          className="block truncate text-xs text-ink-3 underline underline-offset-2"
        >
          {item.source_url}
        </a>
      )}
      {item.body && (
        <p className={`whitespace-pre-wrap text-sm text-ink-2 ${expanded ? "" : "line-clamp-3"}`}>{item.body}</p>
      )}
      <div className="flex flex-wrap gap-3 text-xs">
        {long && (
          <button type="button" className="text-ink-3 hover:text-ink" onClick={() => setExpanded(!expanded)}>
            {expanded ? "Show less" : "Show all"}
          </button>
        )}
        {!item.confirmed && (
          <button type="button" className="text-ink-2 hover:text-ink" onClick={() => update.mutate({ confirmed: true })}>
            Confirm
          </button>
        )}
        {item.is_expired && (
          <button
            type="button"
            className="text-ink-2 hover:text-ink"
            onClick={() => update.mutate({ clear_expiry: true })}
          >
            Still holds
          </button>
        )}
        <button
          type="button"
          className="text-ink-3 hover:text-ink"
          title="Stop the advisor reading it, without deleting it"
          onClick={() => update.mutate({ is_active: false })}
        >
          Archive
        </button>
        {confirmDelete ? (
          <span className="inline-flex gap-2">
            <button type="button" className="text-bad" onClick={() => remove.mutate()}>
              Delete permanently
            </button>
            <button type="button" className="text-ink-3" onClick={() => setConfirmDelete(false)}>
              Keep
            </button>
          </span>
        ) : (
          <button type="button" className="text-ink-3 hover:text-bad" onClick={() => setConfirmDelete(true)}>
            Delete
          </button>
        )}
      </div>
      {(update.isError || remove.isError) && (
        <p className="text-xs text-bad">{((update.error ?? remove.error) as Error).message}</p>
      )}
    </li>
  );
}

type AddKind = "view" | "preference" | "url" | "document";

function AddContext() {
  const queryClient = useQueryClient();
  const [kind, setKind] = useState<AddKind>("view");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [url, setUrl] = useState("");
  const [expires, setExpires] = useState("");
  const file = useRef<HTMLInputElement>(null);

  const add = useMutation({
    mutationFn: async () => {
      const expires_on = expires || null;
      if (kind === "view" || kind === "preference")
        return advisorApi.addNote({ kind, title, body, expires_on });
      if (kind === "url") return advisorApi.addUrl({ url, title: title || undefined, note: body, expires_on });
      const chosen = file.current?.files?.[0];
      if (!chosen) throw new Error("Choose a PDF, .txt or .md file");
      return advisorApi.addDocument(chosen, { title: title || undefined, note: body, expires_on: expires || undefined });
    },
    onSuccess: () => {
      setTitle("");
      setBody("");
      setUrl("");
      setExpires("");
      if (file.current) file.current.value = "";
      void queryClient.invalidateQueries({ queryKey: ["advisor-context"] });
    },
  });

  const placeholders: Record<AddKind, string> = {
    view: "e.g. I expect rates to fall over the next year, so longer-duration bonds look better than cash.",
    preference: "e.g. No individual bank stocks. Keep at least a year of spending in cash.",
    url: "Why it matters (optional)",
    document: "Why it matters (optional)",
  };

  return (
    <form
      className="space-y-3"
      onSubmit={(e) => {
        e.preventDefault();
        add.mutate();
      }}
    >
      <div className="flex flex-wrap gap-1" role="radiogroup" aria-label="What to add">
        {(["view", "preference", "url", "document"] as const).map((k) => (
          <button
            key={k}
            type="button"
            role="radio"
            aria-checked={kind === k}
            onClick={() => setKind(k)}
            className={`rounded-lg px-3 py-1.5 text-sm ${kind === k ? "bg-surface-2 font-medium" : "text-ink-2"}`}
          >
            {KIND_LABEL[k]}
          </button>
        ))}
      </div>
      {kind === "url" && (
        <input type="url" required placeholder="https://…" value={url} onChange={(e) => setUrl(e.target.value)} className={inputClass} aria-label="URL" />
      )}
      {kind === "document" && (
        <input ref={file} type="file" accept=".pdf,.txt,.md" className="block text-sm" aria-label="Document" />
      )}
      <input
        required={kind === "view" || kind === "preference"}
        placeholder={kind === "url" || kind === "document" ? "Title (optional)" : "Short title"}
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        className={inputClass}
        aria-label="Title"
      />
      <textarea
        required={kind === "view" || kind === "preference"}
        rows={3}
        placeholder={placeholders[kind]}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        className={inputClass}
        aria-label="Text"
      />
      <label className="flex flex-wrap items-center gap-2 text-sm text-ink-2">
        Stops being true on
        <input type="date" value={expires} onChange={(e) => setExpires(e.target.value)} className="rounded-lg border border-line bg-surface px-2 py-1 text-sm" />
        <span className="text-xs text-ink-3">optional; the advisor will ask about it after this date</span>
      </label>
      {add.isError && <Caveats tone="bad" items={[(add.error as Error).message]} />}
      <button type="submit" className={buttonClass} disabled={add.isPending}>
        {add.isPending ? "Adding…" : "Add"}
      </button>
    </form>
  );
}

export function ContextPanel() {
  const context = useQuery({ queryKey: ["advisor-context"], queryFn: () => advisorApi.context() });
  const current = context.data?.current ?? [];
  const expired = context.data?.expired ?? [];
  return (
    <div className="grid grid-cols-[minmax(0,1fr)] items-start gap-4 lg:grid-cols-2">
      <PhilosophyCard />
      <div className="space-y-4">
        <Card
          title="What the advisor should know"
          subtitle="Your views, preferences, pages and documents. This is your input, not portfolio data, and the advisor treats it as evidence to weigh, never as instructions."
        >
          {context.isLoading && <Loading />}
          {expired.length > 0 && (
            <>
              <h3 className="text-xs font-semibold uppercase tracking-wide text-warn">Expired, to reconsider</h3>
              <ul className="divide-y divide-line">
                {expired.map((item) => (
                  <ContextRow key={item.id} item={item} />
                ))}
              </ul>
            </>
          )}
          {current.length > 0 ? (
            <ul className="divide-y divide-line">
              {current.map((item) => (
                <ContextRow key={item.id} item={item} />
              ))}
            </ul>
          ) : (
            !context.isLoading &&
            !expired.length && <p className="text-sm text-ink-3">Nothing yet. Add a view, a preference, a page or a document below.</p>
          )}
        </Card>
        <Card title="Add">
          <AddContext />
        </Card>
      </div>
    </div>
  );
}
