/**
 * Pure helpers for the advisor chat: turning Hermes's stored messages and its
 * streamed events into turns. Kept apart from the components so they can be
 * reasoned about (and tested) without rendering anything.
 */
import type { AdvisorEvent, HermesMessage } from "./api";

export interface ToolUse {
  name: string;
  status: "running" | "done" | "failed";
  preview?: string;
}

export interface Turn {
  role: "user" | "assistant";
  text: string;
  tools: ToolUse[];
  error?: string;
  streaming?: boolean;
}

/** Hermes prefixes MCP tools with their server; the bare name reads better. */
export function bareToolName(name: string): string {
  return name.replace(/^mcp[_-]+portfolio[_-]manager[_-]+/i, "");
}

const TOOL_LABELS: Record<string, string> = {
  web_search: "Searched the web",
  web_extract: "Read a page",
  simulate_trades: "Simulated trades",
  suggest_rebalance: "Drafted a rebalance",
  get_rebalance_gaps: "Measured gaps to target",
  save_recommendation: "Saved to journal",
  save_advisor_context: "Remembered a note",
  skill_view: "Loaded a skill",
};

export function toolLabel(name: string): string {
  const bare = bareToolName(name);
  if (TOOL_LABELS[bare]) return TOOL_LABELS[bare];
  if (bare.startsWith("get_") || bare.startsWith("read_"))
    return `Read ${bare.replace(/^(get|read)_/, "").replaceAll("_", " ")}`;
  return bare.replaceAll("_", " ");
}

function textOf(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content))
    return content
      .map((part) =>
        typeof part === "string"
          ? part
          : part && typeof part === "object" && "text" in part
            ? String((part as { text: unknown }).text ?? "")
            : "",
      )
      .join("");
  return "";
}

/** Hermes's stored messages as turns: consecutive assistant messages merge. */
export function turnsFromHistory(messages: HermesMessage[]): Turn[] {
  const turns: Turn[] = [];
  for (const message of messages) {
    if (message.role === "user") {
      turns.push({ role: "user", text: textOf(message.content), tools: [] });
      continue;
    }
    if (message.role !== "assistant") continue;
    const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
    const tools: ToolUse[] = calls.map((call) => ({
      name: String((call as { function?: { name?: unknown } })?.function?.name ?? "tool"),
      status: "done",
    }));
    const text = textOf(message.content);
    const previous = turns[turns.length - 1];
    if (previous?.role === "assistant") {
      previous.tools.push(...tools);
      if (text) previous.text = previous.text ? `${previous.text}\n\n${text}` : text;
    } else {
      turns.push({ role: "assistant", text, tools });
    }
  }
  return turns;
}

/** Apply one streamed event to the turn being built. Pure, for testing. */
export function applyEvent(turn: Turn, { event, data }: AdvisorEvent): Turn {
  switch (event) {
    case "assistant.delta":
      return { ...turn, text: turn.text + String(data.delta ?? "") };
    case "assistant.completed":
      return { ...turn, text: typeof data.content === "string" && data.content ? data.content : turn.text };
    case "tool.started":
      return {
        ...turn,
        tools: [
          ...turn.tools,
          {
            name: String(data.tool_name ?? "tool"),
            status: "running",
            preview: typeof data.preview === "string" ? data.preview : undefined,
          },
        ],
      };
    case "tool.completed":
    case "tool.failed": {
      const name = String(data.tool_name ?? "");
      const tools = [...turn.tools];
      const index = tools.map((t) => t.name).lastIndexOf(name);
      if (index >= 0)
        tools[index] = { ...tools[index], status: event === "tool.failed" ? "failed" : "done" };
      return { ...turn, tools };
    }
    case "error":
      return { ...turn, error: String(data.message ?? "the advisor stopped with an error") };
    case "approval.request":
      return {
        ...turn,
        error:
          "Hermes asked for approval to run a command. The advisor should not need one; " +
          "check its toolsets (portfolio hermes install, step 4c).",
      };
    case "done":
      return { ...turn, streaming: false };
    default:
      return turn;
  }
}

