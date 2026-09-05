"""Markdown in the store, HTML on the board - and back again.

WHY THIS EXISTS: asoode renders a task description and a comment with
`dangerouslySetInnerHTML`, fed by a TipTap editor
(apps/frontend/src/components/task/TaskModalMain.tsx:559 and :938). Its storage
format is therefore HTML. Everything this project has mirrored so far went up as
plain text, so a markdown bullet list showed on the card as literal "- "
characters.

The local store keeps MARKDOWN: it is what an agent writes without being asked,
it stays readable in a DuckDB column, and it is the one format a second provider
could also be given. HTML is asoode's vocabulary, so the translation lives at the
provider boundary and nowhere else.

THE SUBSET, and why it is this and not more. TipTap runs StarterKit v2 with
headings limited to levels 1-3 (TaskEditor.tsx:152), so the nodes that survive a
human opening the card are exactly:

    p  h1 h2 h3  ul ol li  blockquote  pre>code  hr  br
    strong  em  s  code

Anything outside that list is DROPPED by TipTap the moment someone edits the
description - the text survives, the markup does not. So:

- LINKS stay literal. `[text](url)` is emitted as the characters `[text](url)`
  rather than an `<a>`, because StarterKit has no Link extension: an anchor
  renders fine until a human touches the card, and then the URL is gone for
  good. Literal syntax is ugly for one line and lossless forever.
- TABLES degrade to a code block. Same reason, worse failure: a `<table>` would
  vanish wholesale. A monospace block keeps the columns aligned and readable,
  and the round-trip is stable after the first pass.

ESCAPING IS A SECURITY REQUIREMENT, not tidiness. asoode's backend does not
sanitize - there is no sanitize-html or DOMPurify anywhere in apps/backend - and
its frontend injects the string straight into the DOM. Every piece of text that
passes through `markdown_to_html` is escaped BEFORE any tag is added to it.

PLAIN TEXT MUST SURVIVE UNTOUCHED. The store is already full of plain
descriptions written before any of this existed. A description with no markdown
in it becomes one `<p>`, and comes back from `html_to_markdown` byte-identical.
That is the property the inbound path relies on: it can call `html_to_markdown`
on anything asoode hands it without first deciding whether it is HTML.

NO NEW DEPENDENCY. A general markdown library emits the whole of CommonMark -
tables, links, images, raw HTML - which is precisely the set TipTap would throw
away. Hand-rolling the subset keeps the two halves in agreement.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

__all__ = ["markdown_to_html", "html_to_markdown", "looks_like_html"]

# TipTap here is configured with `heading: { levels: [1, 2, 3] }`, so a deeper
# heading is clamped rather than emitted as an h4 the editor cannot represent.
_MAX_HEADING = 3

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_HR_RE = re.compile(r"^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*(\S*)\s*$")
_BULLET_RE = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d+)[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
# A markdown table is a header row of pipes followed by a |---|---| divider.
_TABLE_DIVIDER_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")

# Inline markers, longest first: ** must be tried before *, ~~ before anything
# that could eat a single ~. `code` is pulled out before any of them, so a
# backtick span is never reinterpreted.
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
_INLINE_PATTERNS = (
    (re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*", re.DOTALL), "strong"),
    (re.compile(r"__(?=\S)(.+?)(?<=\S)__", re.DOTALL), "strong"),
    (re.compile(r"~~(?=\S)(.+?)(?<=\S)~~", re.DOTALL), "s"),
    (re.compile(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])", re.DOTALL), "em"),
    (re.compile(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])", re.DOTALL), "em"),
)


# ---------------------------------------------------------------- markdown -> html


def markdown_to_html(text: str | None, *, allow_headings: bool = True) -> str:
    """Markdown to the HTML subset TipTap can hold.

    `allow_headings=False` for COMMENTS: asoode's comment editor is the same
    component in `compact` mode, which turns the heading node off
    (TaskEditor.tsx:152), so an h1 in a comment is markup the editor cannot
    round-trip. A heading in that mode becomes a bold paragraph, which is what
    it was trying to say anyway.

    Returns "" for empty input - not "<p></p>", which asoode would show as a
    description that exists and is blank.

    A body that is ALREADY HTML is normalised rather than escaped. "Support md
    or basic html" was the requirement, and a description pasted from a rich
    editor would otherwise arrive on the board as literal `<ul>` characters.
    Round-tripping it through markdown is also what strips the tags TipTap
    cannot hold, so a pasted `<table>` or `<script>` comes out as the subset or
    as text - never as itself.
    """
    if not text or not text.strip():
        return ""
    if _is_html_document(text):
        text = html_to_markdown(text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "".join(_blocks_to_html(lines, allow_headings=allow_headings))


def _is_html_document(text: str) -> bool:
    """Whether the WHOLE string is HTML, not markdown that mentions a tag.

    Deliberately stricter than `looks_like_html`: it must OPEN with a block
    tag, the way every TipTap document does. A markdown description containing
    `<p>` inside backticks starts with prose, so it stays markdown and its
    example survives.
    """
    return text.lstrip().startswith("<") and looks_like_html(text)


def _blocks_to_html(lines: list[str], *, allow_headings: bool) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        if not line.strip():
            i += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            marker, language = fence.group(1)[0], fence.group(2)
            body, i = _take_fenced(lines, i + 1, marker)
            out.append(_code_block(body, language))
            continue

        if _HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            content = _inline(heading.group(2).strip())
            if allow_headings:
                level = min(len(heading.group(1)), _MAX_HEADING)
                out.append(f"<h{level}>{content}</h{level}>")
            else:
                out.append(f"<p><strong>{content}</strong></p>")
            i += 1
            continue

        if _QUOTE_RE.match(line):
            quoted, i = _take_quote(lines, i)
            inner = _blocks_to_html(quoted, allow_headings=allow_headings)
            out.append("<blockquote>" + "".join(inner) + "</blockquote>")
            continue

        if _is_table_start(lines, i):
            table, i = _take_table(lines, i)
            out.append(_code_block("\n".join(table), ""))
            continue

        if _BULLET_RE.match(line) or _ORDERED_RE.match(line):
            items, i = _take_list(lines, i)
            out.append(_list_to_html(items, 0, allow_headings=allow_headings))
            continue

        paragraph, i = _take_paragraph(lines, i)
        # A single newline inside a paragraph is a hard break, not a new block.
        # TipTap's hardBreak node is exactly this, and losing it would join two
        # deliberately separate lines into one run-on sentence.
        out.append("<p>" + "<br>".join(_inline(x) for x in paragraph) + "</p>")
    return out


def _take_fenced(lines: list[str], start: int, marker: str) -> tuple[str, int]:
    body: list[str] = []
    i = start
    while i < len(lines):
        closing = _FENCE_RE.match(lines[i])
        if closing and closing.group(1)[0] == marker:
            return "\n".join(body), i + 1
        body.append(lines[i])
        i += 1
    # An unterminated fence runs to the end of the text rather than failing:
    # a half-written description is still a description.
    return "\n".join(body), i


def _take_quote(lines: list[str], start: int) -> tuple[list[str], int]:
    quoted: list[str] = []
    i = start
    while i < len(lines):
        match = _QUOTE_RE.match(lines[i])
        if match is None:
            break
        quoted.append(match.group(1))
        i += 1
    return quoted, i


def _is_table_start(lines: list[str], i: int) -> bool:
    return (
        "|" in lines[i]
        and i + 1 < len(lines)
        and "|" in lines[i + 1]
        and bool(_TABLE_DIVIDER_RE.match(lines[i + 1]))
    )


def _take_table(lines: list[str], start: int) -> tuple[list[str], int]:
    table: list[str] = []
    i = start
    while i < len(lines) and lines[i].strip() and "|" in lines[i]:
        table.append(lines[i].rstrip())
        i += 1
    return table, i


def _take_paragraph(lines: list[str], start: int) -> tuple[list[str], int]:
    body: list[str] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            break
        # Any block opener ends the paragraph, so "text\n- item" is a paragraph
        # followed by a list rather than one paragraph containing a dash.
        if (
            _HEADING_RE.match(line)
            or _HR_RE.match(line)
            or _FENCE_RE.match(line)
            or _QUOTE_RE.match(line)
            or _BULLET_RE.match(line)
            or _ORDERED_RE.match(line)
        ):
            break
        body.append(line.strip())
        i += 1
    return body, i


def _code_block(body: str, language: str) -> str:
    attr = f' class="language-{_escape_attr(language)}"' if language else ""
    return f"<pre><code{attr}>{_escape(body)}</code></pre>"


# ---- lists -------------------------------------------------------------------


def _take_list(lines: list[str], start: int) -> tuple[list[tuple[int, bool, str]], int]:
    """Collect one list block as (indent, ordered, text) items.

    A blank line inside a list keeps the list going when the next line is
    another item - that is how a "loose" list is written, and splitting it into
    two lists would restart the numbering.
    """
    items: list[tuple[int, bool, str]] = []
    i = start
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            following = i + 1
            while following < len(lines) and not lines[following].strip():
                following += 1
            if following < len(lines) and (
                _BULLET_RE.match(lines[following]) or _ORDERED_RE.match(lines[following])
            ):
                i = following
                continue
            break
        bullet = _BULLET_RE.match(line)
        ordered = _ORDERED_RE.match(line)
        if bullet:
            items.append((len(bullet.group(1)), False, bullet.group(3).strip()))
        elif ordered:
            items.append((len(ordered.group(1)), True, ordered.group(3).strip()))
        elif items and line.startswith((" ", "\t")):
            # A continuation line belongs to the item above it.
            indent, is_ordered, text = items[-1]
            items[-1] = (indent, is_ordered, f"{text} {line.strip()}")
        else:
            break
        i += 1
    return items, i


def _list_to_html(
    items: list[tuple[int, bool, str]], index: int, *, allow_headings: bool
) -> str:
    """Render items from `index` at their own indent, recursing into deeper ones."""
    html, _ = _list_level(items, index, items[index][0], allow_headings=allow_headings)
    return html


def _list_level(
    items: list[tuple[int, bool, str]], index: int, indent: int, *, allow_headings: bool
) -> tuple[str, int]:
    tag = "ol" if items[index][1] else "ul"
    parts = [f"<{tag}>"]
    i = index
    while i < len(items):
        item_indent, _, text = items[i]
        if item_indent < indent:
            break
        if item_indent > indent:
            # A nested list is a child of the item it sits under - TipTap
            # requires the ul/ol INSIDE the preceding li, not beside it.
            nested, i = _list_level(items, i, item_indent, allow_headings=allow_headings)
            parts[-1] = parts[-1][: -len("</li>")] + nested + "</li>"
            continue
        parts.append(f"<li>{_inline(text)}</li>")
        i += 1
    parts.append(f"</{tag}>")
    return "".join(parts), i


# ---- inline ------------------------------------------------------------------


def _inline(text: str) -> str:
    """Escape, then apply the inline marks - never the other way round.

    Code spans come out first so that `**not bold**` inside backticks stays
    literal, which is the whole point of writing it in backticks.
    """
    parts: list[str] = []
    position = 0
    for match in _CODE_SPAN_RE.finditer(text):
        parts.append(_marks(_escape(text[position : match.start()])))
        parts.append(f"<code>{_escape(match.group(2))}</code>")
        position = match.end()
    parts.append(_marks(_escape(text[position:])))
    return "".join(parts)


def _marks(escaped: str) -> str:
    for pattern, tag in _INLINE_PATTERNS:
        escaped = pattern.sub(lambda m, t=tag: f"<{t}>{m.group(1)}</{t}>", escaped)
    return escaped


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(text: str) -> str:
    return _escape(text).replace('"', "&quot;")


# ---------------------------------------------------------------- html -> markdown


def looks_like_html(text: str | None) -> bool:
    """Whether a stored string came from a rich-text editor.

    Used by the inbound path to keep provenance honest in logs. Conversion does
    not depend on it: `html_to_markdown` leaves tagless text alone, so it is
    always safe to call.
    """
    if not text:
        return False
    return bool(re.search(r"<(p|div|br|ul|ol|li|h[1-6]|pre|code|blockquote|strong|em|b|i|s)\b[^>]*>", text, re.I))


class _MarkdownWriter(HTMLParser):
    """HTML back to markdown, for a description a human wrote on the board.

    Deliberately forgiving: an unknown tag contributes its text and nothing
    else. asoode's HTML comes from TipTap and is well formed, but a description
    pasted in from elsewhere must degrade to readable text rather than raise.
    """

    _BLOCK = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "ul", "ol", "li", "hr", "div"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # (kind, text) - "item" blocks are list items, and consecutive ones are
        # joined by a single newline so a list comes back as a list rather than
        # a run of one-line paragraphs.
        self.blocks: list[tuple[str, str]] = []
        self._buffer: list[str] = []
        self._list_stack: list[dict] = []
        self._quote_depth = 0
        self._in_pre = False
        self._pre: list[str] = []
        self._heading: int | None = None

    # -- helpers

    def _pending(self) -> bool:
        return bool("".join(self._buffer).strip())

    def _flush(self, prefix: str = "", kind: str = "block") -> None:
        text = "".join(self._buffer)
        self._buffer.clear()
        if not text.strip():
            return
        text = re.sub(r"[ \t]+", " ", text).strip()
        if self._quote_depth:
            prefix = "> " * self._quote_depth + prefix
        self.blocks.append((kind, prefix + text))

    def _flush_item(self) -> None:
        """Emit the buffered text as a list item, numbering it as we go.

        The emptiness check comes FIRST: `_list_prefix` advances the ordered
        counter, so calling it for a `</li>` whose text was already flushed (a
        nested list does exactly that) would skip a number.
        """
        if not self._pending():
            return
        self._flush(self._list_prefix(), kind="item")

    def _list_prefix(self) -> str:
        level = self._list_stack[-1]
        indent = "  " * (len(self._list_stack) - 1)
        if level["ordered"]:
            level["index"] += 1
            return f"{indent}{level['index']}. "
        return f"{indent}- "

    # -- parser callbacks

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001
        tag = tag.lower()
        if tag == "br":
            self._buffer.append("\n")
            return
        if tag == "hr":
            self._flush()
            self.blocks.append(("block", "---"))
            return
        if tag == "pre":
            self._flush()
            self._in_pre = True
            self._pre = []
            return
        if tag == "code" and not self._in_pre:
            self._buffer.append("`")
            return
        if tag in ("strong", "b"):
            self._buffer.append("**")
            return
        if tag in ("em", "i"):
            self._buffer.append("*")
            return
        if tag in ("s", "del", "strike"):
            self._buffer.append("~~")
            return
        if tag in ("ul", "ol"):
            # A nested list arrives while its parent item's text is still in the
            # buffer. Flushing it as an ITEM keeps the parent's bullet; flushing
            # it plainly is what used to turn "- a" into a bare paragraph.
            if self._list_stack:
                self._flush_item()
            else:
                self._flush()
            self._list_stack.append({"ordered": tag == "ol", "index": 0})
            return
        if tag == "li":
            self._flush()
            return
        if tag == "blockquote":
            self._flush()
            self._quote_depth += 1
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self._heading = int(tag[1])
            return
        if tag in self._BLOCK:
            self._flush()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "pre":
            body = "".join(self._pre).strip("\n")
            self.blocks.append(("block", f"```\n{body}\n```"))
            self._in_pre = False
            self._pre = []
            return
        if tag == "code" and not self._in_pre:
            self._buffer.append("`")
            return
        if tag in ("strong", "b"):
            self._buffer.append("**")
            return
        if tag in ("em", "i"):
            self._buffer.append("*")
            return
        if tag in ("s", "del", "strike"):
            self._buffer.append("~~")
            return
        if tag == "li":
            if self._list_stack:
                self._flush_item()
            else:
                self._flush("- ", kind="item")
            return
        if tag in ("ul", "ol"):
            self._flush()
            if self._list_stack:
                self._list_stack.pop()
            return
        if tag == "blockquote":
            self._flush()
            self._quote_depth = max(0, self._quote_depth - 1)
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = self._heading or 1
            self._heading = None
            self._flush("#" * level + " ")
            return
        if tag in self._BLOCK:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._in_pre:
            self._pre.append(data)
        else:
            self._buffer.append(data)

    def result(self) -> str:
        self._flush()
        blocks = [(kind, text) for kind, text in self.blocks if text.strip()]
        out = ""
        for index, (kind, text) in enumerate(blocks):
            if index:
                # Two list items in a row are one list; anything else is a new
                # block and gets a blank line.
                out += "\n" if kind == "item" and blocks[index - 1][0] == "item" else "\n\n"
            out += text
        return out


def html_to_markdown(html: str | None) -> str:
    """HTML from the board back to the markdown the store keeps.

    Text with no tags in it comes back UNCHANGED, entities and all - the store
    is full of plain descriptions and the inbound path must not rewrite one just
    because it passed through here.
    """
    if not html or not html.strip():
        return ""
    if not looks_like_html(html):
        return html
    writer = _MarkdownWriter()
    writer.feed(html)
    writer.close()
    text = writer.result()
    # TipTap emits an empty paragraph for a blank line; collapse the runs of
    # blank lines that produces.
    return re.sub(r"\n{3,}", "\n\n", text).strip()
