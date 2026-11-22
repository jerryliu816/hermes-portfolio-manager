/**
 * A small markdown renderer for the advisor's replies.
 *
 * Deliberately limited, and deliberately built from React elements rather than
 * an HTML string: the advisor's text carries content from web pages and
 * documents, so nothing it says may become markup. Covers what advice needs:
 * headings, paragraphs, lists, tables (trade lists), code, bold, italics, and
 * links -- http(s) only, opened in a new tab without a referrer.
 */
import type { ReactNode } from "react";

type Block =
  | { kind: "heading"; level: number; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; ordered: boolean; items: string[] }
  | { kind: "table"; header: string[]; rows: string[][] }
  | { kind: "code"; text: string }
  | { kind: "quote"; text: string }
  | { kind: "rule" };

const splitRow = (line: string) =>
  line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());

const isTableDivider = (line: string) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);

function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    if (line.trim().startsWith("```")) {
      const body: string[] = [];
      i += 1;
      while (i < lines.length && !lines[i].trim().startsWith("```")) body.push(lines[i++]);
      i += 1;
      blocks.push({ kind: "code", text: body.join("\n") });
      continue;
    }
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    if (heading) {
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2] });
      i += 1;
      continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      blocks.push({ kind: "rule" });
      i += 1;
      continue;
    }
    if (line.includes("|") && i + 1 < lines.length && isTableDivider(lines[i + 1])) {
      const header = splitRow(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(splitRow(lines[i++]));
      blocks.push({ kind: "table", header, rows });
      continue;
    }
    const listMatch = /^\s*([-*+]|\d+[.)])\s+/.exec(line);
    if (listMatch) {
      const ordered = /\d/.test(listMatch[1]);
      const items: string[] = [];
      while (i < lines.length) {
        const item = /^\s*([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i]);
        if (item) items.push(item[2]);
        else if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length)
          items[items.length - 1] += ` ${lines[i].trim()}`;
        else break;
        i += 1;
      }
      blocks.push({ kind: "list", ordered, items });
      continue;
    }
    if (line.startsWith(">")) {
      const body: string[] = [];
      while (i < lines.length && lines[i].startsWith(">")) body.push(lines[i++].replace(/^>\s?/, ""));
      blocks.push({ kind: "quote", text: body.join(" ") });
      continue;
    }
    const para: string[] = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(#{1,4})\s/.test(lines[i]) &&
      !lines[i].trim().startsWith("```") &&
      !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]) &&
      !(lines[i].includes("|") && i + 1 < lines.length && isTableDivider(lines[i + 1]))
    )
      para.push(lines[i++].trim());
    blocks.push({ kind: "paragraph", text: para.join(" ") });
  }
  return blocks;
}

const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*)|(\*[^*\s][^*]*\*)|(\[[^\]]+\]\([^)\s]+\))|(https?:\/\/[^\s)<>]+)/g;

function safeHref(href: string): string | null {
  try {
    const url = new URL(href);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

function link(href: string, label: ReactNode, key: number): ReactNode {
  const safe = safeHref(href);
  if (!safe) return <span key={key}>{label}</span>;
  return (
    <a
      key={key}
      href={safe}
      target="_blank"
      rel="noopener noreferrer nofollow"
      className="underline decoration-line underline-offset-2 hover:text-ink"
    >
      {label}
    </a>
  );
}

/**
 * Models sometimes write symbols as LaTeX (`$\rightarrow$`, `\approx`). This
 * page does not render math, so known symbol commands become their Unicode
 * character. Anything unrecognised is left exactly as written: a lone `$` is
 * money here, so a `$...$` span is only rewritten when every command in it is
 * one we know.
 */
const LATEX_SYMBOLS: Record<string, string> = {
  rightarrow: "→", to: "→", leftarrow: "←", gets: "←", leftrightarrow: "↔",
  Rightarrow: "⇒", implies: "⇒", Leftarrow: "⇐", Leftrightarrow: "⇔", iff: "⇔",
  uparrow: "↑", downarrow: "↓", approx: "≈", sim: "~", neq: "≠", ne: "≠",
  leq: "≤", le: "≤", geq: "≥", ge: "≥", times: "×", div: "÷", pm: "±", mp: "∓",
  cdot: "·", ldots: "…", dots: "…", cdots: "⋯", infty: "∞", Delta: "Δ", delta: "δ",
  sigma: "σ", beta: "β", alpha: "α", mu: "μ", percent: "%", "%": "%", "$": "$",
};
const LATEX_COMMAND = /\\([a-zA-Z]+|[%$])/g;
const LATEX_MATH_SPAN = /\$\s*((?:\\(?:[a-zA-Z]+|[%$])\s*)+)\$/g;
const LATEX_BARE = /\\([a-zA-Z]+|[%$])(?![a-zA-Z])/g;

function replaceLatexSymbols(text: string): string {
  if (!text.includes("\\")) return text;
  const withSpans = text.replace(LATEX_MATH_SPAN, (span, body: string) => {
    const commands = [...body.matchAll(LATEX_COMMAND)].map((m) => m[1]);
    return commands.every((c) => c in LATEX_SYMBOLS) ? commands.map((c) => LATEX_SYMBOLS[c]).join("") : span;
  });
  return withSpans.replace(LATEX_BARE, (cmd, name: string) => LATEX_SYMBOLS[name] ?? cmd);
}

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let key = 0;
  for (const match of text.matchAll(INLINE)) {
    const index = match.index ?? 0;
    if (index > last) out.push(replaceLatexSymbols(text.slice(last, index)));
    const token = match[0];
    if (match[1]) {
      out.push(
        <code key={key++} className="rounded bg-surface-2 px-1 py-0.5 text-[0.85em]">
          {token.slice(1, -1)}
        </code>,
      );
    } else if (match[2]) {
      out.push(<strong key={key++}>{inline(token.slice(2, -2))}</strong>);
    } else if (match[3]) {
      out.push(<em key={key++}>{inline(token.slice(1, -1))}</em>);
    } else if (match[4]) {
      const parts = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(token);
      out.push(parts ? link(parts[2], parts[1], key++) : token);
    } else if (match[5]) {
      out.push(link(token, token, key++));
    }
    last = index + token.length;
  }
  if (last < text.length) out.push(replaceLatexSymbols(text.slice(last)));
  return out;
}

const NUMERIC = /^[−+-]?[$€£]?[\d,.]+%?$|^[−+-]?\d[\d,.]*\s?(pp|bp)?$/;

export function Markdown({ text }: { text: string }) {
  return (
    <div className="space-y-3 text-sm leading-relaxed text-ink">
      {parseBlocks(text).map((block, i) => {
        switch (block.kind) {
          case "heading": {
            const size = block.level <= 2 ? "text-base" : "text-sm";
            return (
              <p key={i} className={`${size} font-semibold`} role="heading" aria-level={block.level + 1}>
                {inline(block.text)}
              </p>
            );
          }
          case "paragraph":
            return <p key={i}>{inline(block.text)}</p>;
          case "list": {
            const items = block.items.map((item, j) => <li key={j}>{inline(item)}</li>);
            return block.ordered ? (
              <ol key={i} className="list-decimal space-y-1 pl-5">
                {items}
              </ol>
            ) : (
              <ul key={i} className="list-disc space-y-1 pl-5">
                {items}
              </ul>
            );
          }
          case "table":
            return (
              <div key={i} className="overflow-x-auto rounded-lg border border-line">
                <table className="w-full text-xs">
                  <thead className="bg-surface-2">
                    <tr>
                      {block.header.map((cell, j) => (
                        <th key={j} scope="col" className="px-2.5 py-1.5 text-left font-semibold text-ink-2">
                          {inline(cell)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, r) => (
                      <tr key={r} className="border-t border-line">
                        {row.map((cell, c) => (
                          <td
                            key={c}
                            className={`px-2.5 py-1.5 align-top ${NUMERIC.test(cell) ? "tnum text-right" : ""}`}
                          >
                            {inline(cell)}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          case "code":
            return (
              <pre key={i} className="overflow-x-auto rounded-lg bg-surface-2 p-3 text-xs">
                <code>{block.text}</code>
              </pre>
            );
          case "quote":
            return (
              <blockquote key={i} className="border-l-2 border-line pl-3 text-ink-2">
                {inline(block.text)}
              </blockquote>
            );
          case "rule":
            return <hr key={i} className="border-line" />;
        }
      })}
    </div>
  );
}
