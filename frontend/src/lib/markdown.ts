/**
 * A markdown parser that produces a tree, never a string of HTML.
 *
 * Task descriptions and comments are authored in markdown and stored as
 * markdown; the asoode mirror converts them to HTML on the way out. Locally we
 * render them ourselves, and the rule that shapes this whole module is:
 *
 *   THE PARSER MUST NEVER BUILD HTML.
 *
 * It emits a typed tree, and `<Markdown>` turns that tree into React elements.
 * Text ends up in React *children*, which React escapes — so a description
 * containing `<img onerror=...>` is text, not an element, and no amount of
 * hostile input can become markup. Building an HTML string and handing it to
 * `dangerouslySetInnerHTML` would need a sanitizer to be safe, and a sanitizer
 * is exactly the dependency (and the class of bug) this avoids. There is no
 * markdown library in this app and none is added here.
 *
 * The supported subset is *exactly* what the Python converter emits, because a
 * feature only one half understands is a format the two halves disagree about:
 *
 *   headings (# ## ###), bullet and ordered lists with one level of nesting,
 *   blockquotes, fenced code blocks, inline code, **bold**, *italic*,
 *   ~~strike~~, --- rules, paragraphs, hard line breaks.
 *
 * Everything else stays literal text — deliberately including tables, which the
 * converter does not emit: half-rendering a table is worse than showing the
 * pipes. `_underscores_` are literal too, so `parent_id` and
 * `markdown_to_html` read as themselves.
 */

export type MdInline =
  | { kind: "text"; value: string }
  | { kind: "code"; value: string }
  | { kind: "strong"; children: MdInline[] }
  | { kind: "em"; children: MdInline[] }
  | { kind: "strike"; children: MdInline[] }
  | { kind: "break" };

export interface MdList {
  ordered: boolean;
  /** First number of an ordered list, so `3.` starts at three. */
  start: number;
  items: MdListItem[];
}

export interface MdListItem {
  spans: MdInline[];
  /** The nested list under this item. The converter emits one level; deeper
   *  indentation parses the same way rather than being dropped. */
  nested: MdList | null;
}

export type MdBlock =
  | { kind: "heading"; level: 1 | 2 | 3; spans: MdInline[] }
  | { kind: "paragraph"; spans: MdInline[] }
  | { kind: "list"; list: MdList }
  | { kind: "quote"; blocks: MdBlock[] }
  | { kind: "codeblock"; language: string | null; value: string }
  | { kind: "rule" };

const FENCE = /^ {0,3}(`{3,})\s*([^`]*)$/;
const HEADING = /^ {0,3}(#{1,3})\s+(.*)$/;
/** `---`, `***`, `___`, and their spaced forms. Checked before list items. */
const RULE = /^ {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$/;
const QUOTE = /^ {0,3}>[ \t]?(.*)$/;
/**
 * A markdown table: a row of pipes followed by a |---|---| divider. Tables are
 * NOT in the subset TipTap can hold, so they are shown as a code block —
 * aligned and readable — exactly as the Python converter sends them to the
 * board. Rendering half a table locally and a code block remotely would make
 * the two halves disagree about the same description.
 */
const TABLE_DIVIDER = /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/;

const BULLET = /^([ \t]*)([-*+])[ \t]+(.*)$/;
const ORDERED = /^([ \t]*)(\d{1,9})[.)][ \t]+(.*)$/;

const ESCAPABLE = "\\`*_~#>-+.!|[](){}";

/** How far a line is indented, a tab counting as four columns. */
function indentOf(line: string): number {
  let width = 0;
  for (const ch of line) {
    if (ch === " ") width += 1;
    else if (ch === "\t") width += 4;
    else break;
  }
  return width;
}

function isListLine(line: string): boolean {
  return !RULE.test(line) && (BULLET.test(line) || ORDERED.test(line));
}

/** True for a line that would end a paragraph by starting something else. */
function startsBlock(line: string): boolean {
  if (!line.trim()) return true;
  return (
    FENCE.test(line) ||
    RULE.test(line) ||
    HEADING.test(line) ||
    QUOTE.test(line) ||
    isListLine(line)
  );
}

export function parseMarkdown(source: string | null | undefined): MdBlock[] {
  if (!source || !source.trim()) return [];
  return parseBlocks(source.replace(/\r\n?/g, "\n").split("\n"), 0);
}

function parseBlocks(lines: string[], depth: number): MdBlock[] {
  const blocks: MdBlock[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      i += 1;
      continue;
    }

    const fence = line.match(FENCE);
    if (fence) {
      const marker = fence[1];
      const language = fence[2].trim() || null;
      const body: string[] = [];
      i += 1;
      // An unterminated fence runs to the end of the text rather than throwing
      // the rest of the description away.
      while (i < lines.length && !new RegExp(`^ {0,3}${marker}\`*[ \t]*$`).test(lines[i])) {
        body.push(lines[i]);
        i += 1;
      }
      if (i < lines.length) i += 1;
      blocks.push({ kind: "codeblock", language, value: body.join("\n") });
      continue;
    }

    if (RULE.test(line)) {
      blocks.push({ kind: "rule" });
      i += 1;
      continue;
    }

    const heading = line.match(HEADING);
    if (heading) {
      const level = heading[1].length as 1 | 2 | 3;
      const text = heading[2].replace(/[ \t]+#+[ \t]*$/, "").trim();
      blocks.push({ kind: "heading", level, spans: parseInline(text) });
      i += 1;
      continue;
    }

    if (QUOTE.test(line)) {
      const inner: string[] = [];
      while (i < lines.length && QUOTE.test(lines[i])) {
        inner.push((lines[i].match(QUOTE) as RegExpMatchArray)[1]);
        i += 1;
      }
      // A quote holds blocks (a list inside a quote is common), but nesting
      // stops at two: deeper than that, the content is shown as paragraphs.
      blocks.push({
        kind: "quote",
        blocks: depth < 2 ? parseBlocks(inner, depth + 1) : [
          { kind: "paragraph", spans: parseInline(inner.join("\n").trim()) },
        ],
      });
      continue;
    }

    if (
      line.includes("|") &&
      i + 1 < lines.length &&
      lines[i + 1].includes("|") &&
      TABLE_DIVIDER.test(lines[i + 1])
    ) {
      const rows: string[] = [];
      while (i < lines.length && lines[i].trim() && lines[i].includes("|")) {
        rows.push(lines[i].trimEnd());
        i += 1;
      }
      blocks.push({ kind: "codeblock", language: null, value: rows.join("\n") });
      continue;
    }

    if (isListLine(line)) {
      const [list, next] = parseList(lines, i);
      blocks.push({ kind: "list", list });
      i = next;
      continue;
    }

    const paragraph: string[] = [];
    while (i < lines.length && !startsBlock(lines[i])) {
      // A trailing double space is markdown's hard break; every newline inside
      // a paragraph is one here anyway, matching what the plain-text dialog
      // showed before and keeping non-subset lines (a table) on their own line.
      paragraph.push(lines[i].replace(/[ \t]+$/, ""));
      i += 1;
    }
    blocks.push({ kind: "paragraph", spans: parseInline(paragraph.join("\n")) });
  }

  return blocks;
}

/** Collect one list, returning it and the index of the first line after it. */
function parseList(lines: string[], start: number): [MdList, number] {
  const first = lines[start].match(BULLET) ?? (lines[start].match(ORDERED) as RegExpMatchArray);
  const baseIndent = indentOf(lines[start]);
  const orderedFirst = ORDERED.test(lines[start]);
  const list: MdList = {
    ordered: orderedFirst,
    start: orderedFirst ? Number(first[2]) : 1,
    items: [],
  };

  // Text lines are collected per item and parsed at the end, so a wrapped item
  // keeps its line breaks.
  let itemLines: string[] = [];
  let nestedLines: string[] = [];
  let inNested = false;

  const closeItem = () => {
    if (itemLines.length === 0 && nestedLines.length === 0) return;
    const item: MdListItem = { spans: parseInline(itemLines.join("\n")), nested: null };
    if (nestedLines.length) {
      const [nested] = parseList(nestedLines, 0);
      item.nested = nested;
    }
    list.items.push(item);
    itemLines = [];
    nestedLines = [];
    inNested = false;
  };

  let i = start;
  while (i < lines.length) {
    const line = lines[i];

    if (!line.trim()) {
      // A blank line ends the list unless the next non-blank line continues it.
      let look = i + 1;
      while (look < lines.length && !lines[look].trim()) look += 1;
      const continues =
        look < lines.length &&
        (isListLine(lines[look]) || indentOf(lines[look]) > baseIndent + 1);
      if (!continues) break;
      i = look;
      continue;
    }

    if (isListLine(line)) {
      const indent = indentOf(line);
      const match = (line.match(BULLET) ?? line.match(ORDERED)) as RegExpMatchArray;
      const content = match[3];
      if (indent > baseIndent + 1) {
        // A nested item: hand the whole line to the recursive call so the child
        // list keeps its own marker type and numbering.
        inNested = true;
        nestedLines.push(line);
      } else {
        closeItem();
        itemLines.push(content);
      }
      i += 1;
      continue;
    }

    // A heading, fence, rule or quote ends the list even without a blank line
    // between them — it is a new block, not a wrapped item.
    if (indentOf(line) <= baseIndent + 1 && startsBlock(line)) break;

    const indent = indentOf(line);
    if (indent > baseIndent + 1 || list.items.length || itemLines.length) {
      // A continuation line belongs to whichever item is open.
      if (inNested) nestedLines.push(line);
      else itemLines.push(line.trim());
      i += 1;
      continue;
    }

    break;
  }

  closeItem();
  return [list, i];
}

/* ── Inline ──────────────────────────────────────────────────────────── */

function runLength(src: string, at: number, ch: string): number {
  let n = 0;
  while (at + n < src.length && src[at + n] === ch) n += 1;
  return n;
}

/**
 * Find the closing delimiter for an emphasis run, honouring the two rules that
 * stop arithmetic and stray asterisks from turning into italics: the content
 * may not start or end with a space, and a `*` that is really part of a `**`
 * pair is skipped.
 */
function findCloser(src: string, from: number, delim: string): number {
  let at = from;
  while (at < src.length) {
    const next = src.indexOf(delim, at);
    if (next === -1) return -1;
    if (delim === "*" && src[next + 1] === "*") {
      at = next + 2;
      continue;
    }
    if (next === from || /\s/.test(src[next - 1])) {
      at = next + delim.length;
      continue;
    }
    return next;
  }
  return -1;
}

export function parseInline(source: string): MdInline[] {
  const spans: MdInline[] = [];
  let text = "";
  const flush = () => {
    if (text) {
      spans.push({ kind: "text", value: text });
      text = "";
    }
  };

  let i = 0;
  while (i < source.length) {
    const ch = source[i];

    if (ch === "\\" && i + 1 < source.length && ESCAPABLE.includes(source[i + 1])) {
      text += source[i + 1];
      i += 2;
      continue;
    }

    if (ch === "\n") {
      flush();
      spans.push({ kind: "break" });
      i += 1;
      continue;
    }

    if (ch === "`") {
      const run = runLength(source, i, "`");
      const close = source.indexOf("`".repeat(run), i + run);
      if (close !== -1) {
        let value = source.slice(i + run, close);
        if (value.length > 2 && value.startsWith(" ") && value.endsWith(" ")) {
          value = value.slice(1, -1);
        }
        flush();
        spans.push({ kind: "code", value });
        i = close + run;
        continue;
      }
      text += ch;
      i += 1;
      continue;
    }

    const delim =
      source.startsWith("**", i)
        ? "**"
        : source.startsWith("~~", i)
          ? "~~"
          : ch === "*"
            ? "*"
            : null;

    if (delim) {
      const from = i + delim.length;
      const opensWell = from < source.length && !/\s/.test(source[from]);
      const close = opensWell ? findCloser(source, from, delim) : -1;
      if (close !== -1) {
        const children = parseInline(source.slice(from, close));
        flush();
        spans.push(
          delim === "**"
            ? { kind: "strong", children }
            : delim === "~~"
              ? { kind: "strike", children }
              : { kind: "em", children }
        );
        i = close + delim.length;
        continue;
      }
      // No partner: the delimiter is just text, e.g. `2 * 3` or a lone `**`.
      text += delim;
      i += delim.length;
      continue;
    }

    text += ch;
    i += 1;
  }

  flush();
  return spans;
}

/* ── One-line preview ────────────────────────────────────────────────── */

/**
 * Markdown with its syntax stripped, flattened to a single line.
 *
 * List and board rows want a preview, not a document: a card that renders a
 * heading and a bullet list stops being a card. This strips rather than parses
 * because it runs once per visible row on every render.
 */
export function markdownToPlainText(
  source: string | null | undefined,
  limit = 160
): string {
  if (!source) return "";
  const out: string[] = [];
  let fenced = false;

  for (const raw of source.replace(/\r\n?/g, "\n").split("\n")) {
    const line = raw.trim();
    if (/^`{3,}/.test(line)) {
      fenced = !fenced;
      continue;
    }
    if (fenced) {
      out.push(line);
      continue;
    }
    if (!line) continue;
    if (RULE.test(line)) continue;
    const stripped = line
      .replace(/^#{1,3}[ \t]+/, "")
      .replace(/^>[ \t]?/, "")
      .replace(/^([-*+]|\d{1,9}[.)])[ \t]+/, "");
    out.push(stripInline(stripped));
  }

  const flat = out.join(" ").replace(/\s+/g, " ").trim();
  if (limit > 0 && flat.length > limit) return `${flat.slice(0, limit - 1).trimEnd()}…`;
  return flat;
}

function stripInline(value: string): string {
  return value
    .replace(/`{1,3}([^`]*)`{1,3}/g, "$1")
    .replace(/\*\*([^*]+)\*\*/g, "$1")
    .replace(/~~([^~]+)~~/g, "$1")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1$2")
    .replace(/\\([\\`*_~#>+.!|[\]{}()-])/g, "$1");
}
