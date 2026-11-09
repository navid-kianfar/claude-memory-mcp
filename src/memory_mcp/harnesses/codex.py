"""The Codex adapter.

Codex (OpenAI's CLI, verified live on codex-cli 0.166.0) reads
`~/.codex/config.toml` and supports streamable-HTTP MCP servers natively:

    [mcp_servers.memory]
    url = "http://127.0.0.1:8765/mcp"

There is no stdlib TOML writer, and the user's config may carry comments and
formatting we must not churn - so the merge is TEXTUAL: our `[mcp_servers.memory]`
section is appended or replaced as a block, everything else is left
byte-identical, and the result is validated with `tomllib` afterwards. A file
we cannot parse at all is skipped, never edited blind.

The lifecycle surface is minimal: Codex's `notify` key names a program it
invokes on turn events, with the JSON payload appended as the last argument.
The wrapper installed here runs the SAME session-end scripts Claude runs
(export + update apply) on each notify. Codex has one `notify` slot, so a
user's existing notify is CHAINED - kept in our command and run by the
wrapper with the same payload - never overwritten. There is no
per-turn prompt hook and no tool gate, and Codex has no subagent system - all
three are declared, not faked.

The one instruction-file touch is a machine-level wiring pointer in
`~/.codex/AGENTS.md` (Codex reads it globally): a folder with `.agent-memory/`
is a memory project, call `memory_session_start` first. That is harness
wiring - the same class of text the MCP server instructions carry - and
deliberately NOT project knowledge; project rules travel through the memory
tools, never through instruction files.
"""

import json
import re
import subprocess
import tomllib
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.harnesses.base import (
    ChangeReport,
    HarnessCapabilities,
    HarnessNotSupported,
)

NOTIFY_WRAPPER_NAME = "codex-notify.sh"
#: Second element of our `notify` command. Marks the entry as ours, and in the
#: wrapper separates it from a chained user command that follows.
NOTIFY_MARKER = "memory-mcp"

#: The pointer this adapter owns in ~/.codex/AGENTS.md, marker-delimited so a
#: re-run updates it in place and never appends a second copy.
AGENTS_POINTER_START = "<!-- memory-mcp:begin -->"
AGENTS_POINTER_END = "<!-- memory-mcp:end -->"
AGENTS_POINTER = (
    AGENTS_POINTER_START + "\n"
    "A project folder containing `.agent-memory/` is a memory-mcp project: call\n"
    "`memory_session_start(project=<slug>, cwd=<folder>)` at the start of every\n"
    "session (the tool list comes from the `memory` MCP server), and\n"
    "`memory_session_end(session_id, summary, project=<slug>)` at the end -\n"
    "`project` is required there. The rules it returns are binding. Store\n"
    "decisions with `memory_store`.\n"
    + AGENTS_POINTER_END
)


def codex_dir() -> Path:
    return Path.home() / ".codex"


def codex_config_path() -> Path:
    return codex_dir() / "config.toml"


def codex_agents_path() -> Path:
    return codex_dir() / "AGENTS.md"


class CodexHarness:
    name = "codex"

    def detect(self) -> bool:
        if codex_config_path().exists() or codex_dir().is_dir():
            return True
        try:
            result = subprocess.run(
                ["codex", "--version"], capture_output=True, text=True, timeout=10,
            )
            return result.returncode == 0
        except Exception:  # noqa: BLE001 - no codex on PATH is the normal case
            return False

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            hooks=True, agents=False, per_turn_line=False, edit_gate=False,
        )

    # ---------- MCP ----------

    def configure_mcp(
        self, server_url: str, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        report = ChangeReport(self.name)
        path = codex_config_path()

        if remote_url:
            url = f"{remote_url.rstrip('/')}/mcp"
            entry: dict = {"url": url}
            if token:
                entry["http_headers"] = {"Authorization": f"Bearer {token}"}
        else:
            url = server_url
            entry = {"url": url}
        section = _render_mcp_section("memory", entry)

        text = ""
        if path.exists():
            text = path.read_text()
            try:
                tomllib.loads(text)
            except Exception as e:  # noqa: BLE001
                report.warn(f"{path} does not parse as TOML ({e}) - skipping.")
                return report
            span = _section_span(text, "mcp_servers.memory")
            if span is not None:
                old = text[span[0]:span[1]]
                old_url = None
                match = re.search(r'url\s*=\s*"([^"]+)"', old)
                if match:
                    old_url = match.group(1)
                if old_url not in (None, url):
                    report.conflict(
                        f"mcp_servers.memory pointed at {old_url!r}; "
                        f"repointed to {url!r}"
                    )
                text = text[:span[0]] + section + text[span[1]:]
            else:
                text = text.rstrip("\n") + "\n\n" + section if text.strip() else section
        else:
            text = section

        # Validate what we are about to leave behind: a config Codex cannot
        # parse is worse than one we did not touch.
        try:
            parsed = tomllib.loads(text)
            assert parsed["mcp_servers"]["memory"]["url"] == url
        except Exception as e:  # noqa: BLE001
            report.warn(f"post-write validation failed ({e}) - config not written.")
            return report

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        report.ok(f"MCP server 'memory' -> {url}")
        return report

    # ---------- lifecycle ----------

    def install_hooks(
        self, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        report = ChangeReport(self.name)
        if remote_url:
            raise HarnessNotSupported(
                "Codex client mode (remote server) is not wired yet; "
                "local daemon only"
            )

        wrapper_dest = settings.data_dir / "hooks" / NOTIFY_WRAPPER_NAME
        wrapper_src = Path(__file__).parent / "codex" / NOTIFY_WRAPPER_NAME
        if not wrapper_src.is_file():
            report.warn(f"notify wrapper missing at {wrapper_src} - skipping.")
            return report
        wrapper_dest.parent.mkdir(parents=True, exist_ok=True)
        wrapper_dest.write_text(wrapper_src.read_text())
        wrapper_dest.chmod(0o755)

        path = codex_config_path()
        text = path.read_text() if path.exists() else ""
        try:
            parsed = tomllib.loads(text)
        except Exception as e:  # noqa: BLE001
            report.warn(f"{path} does not parse as TOML ({e}) - skipping notify.")
            return report
        chained = _chained_notify(parsed.get("notify"), wrapper_dest)
        if chained is None:
            report.conflict(
                f"{path}: `notify` is not a list of strings "
                f"({parsed.get('notify')!r}); left as is, memory-mcp notify not wired"
            )
            return report
        if chained:
            report.ok(
                f"existing notify {chained!r} kept: the memory-mcp wrapper runs "
                "it first, with the same payload"
            )
        command = [str(wrapper_dest), NOTIFY_MARKER, *chained]
        line = "notify = [" + ", ".join(_toml_value(arg) for arg in command) + "]"
        span = _section_span(text, "top:notify")
        if span is not None:
            text = text[:span[0]] + line + "\n" + text[span[1]:]
        else:
            # A top-level TOML key is only top-level BEFORE the first table
            # header - appended at the end it would nest inside whatever table
            # is last, and Codex would never see it. Insert ahead of the
            # first `[` line (after any leading comments).
            first_table = re.search(r"^\[", text, re.MULTILINE)
            if first_table:
                text = (
                    text[:first_table.start()] + line + "\n\n" + text[first_table.start():]
                )
            else:
                text = (
                    text.rstrip("\n") + "\n\n" + line + "\n"
                    if text.strip() else line + "\n"
                )
        try:
            tomllib.loads(text)
        except Exception as e:  # noqa: BLE001
            report.warn(f"post-write validation failed ({e}) - notify not written.")
            return report
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        report.ok(
            f"notify wired to {wrapper_dest} (turn end -> export + update apply)"
        )

        pointer = _upsert_agents_pointer(codex_agents_path())
        if pointer == "updated":
            report.ok(f"Wiring pointer updated in {codex_agents_path()}")
        elif pointer == "created":
            report.ok(f"Wiring pointer written to {codex_agents_path()}")
        return report

    # ---------- agents ----------

    def install_agents(self) -> ChangeReport:
        raise HarnessNotSupported(
            "Codex has no subagent system to install a team into; briefs ride "
            "the MCP instructions and memory_session_start"
        )


def _chained_notify(existing: object, wrapper: Path) -> list[str] | None:
    """The user's own notify command to keep running behind ours.

    Codex has ONE `notify` slot, so wiring ours by overwriting it silently
    switched off whatever the user had there (a desktop notifier, a logger).
    Chaining keeps it: our command becomes `[wrapper, marker, *theirs]`, and the
    wrapper runs `theirs` with the payload appended exactly as Codex would have.
    Re-running setup recognises our own entry by the marker and keeps the chain.

    [] when there is nothing to chain; None when the value is not a list of
    strings, which is not a command we can run and is left for the user.
    """
    if existing is None:
        return []
    if not isinstance(existing, list) or not all(isinstance(a, str) for a in existing):
        return None
    is_ours = (
        len(existing) >= 2
        and Path(existing[0]).name == wrapper.name
        and existing[1] == NOTIFY_MARKER
    )
    return list(existing[2:]) if is_ours else list(existing)


def _render_mcp_section(name: str, entry: dict) -> str:
    """The TOML text of one `[mcp_servers.<name>]` table, 2-space friendly."""
    lines = [f"[mcp_servers.{name}]"]
    for key, value in entry.items():
        if isinstance(value, dict):
            lines.append(f"[mcp_servers.{name}.{key}]")
            for sub_key, sub_value in value.items():
                lines.append(f'{sub_key} = {_toml_value(sub_value)}')
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    return "\n".join(lines) + "\n"


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _section_span(text: str, locator: str) -> tuple[int, int] | None:
    """Byte span of one TOML section, or a top-level key when locator starts
    with 'top:'.

    `[mcp_servers.memory]` extends over its own sub-tables
    (`[mcp_servers.memory.http_headers]`, ...) until the next UNRELATED section
    header or EOF: the sub-tables are part of the entry we render, so a
    replacement that stopped at the first of them left a stale Bearer header
    behind (remote -> local) or duplicated the table (token rotation), which
    the post-write validation then refused. The span includes the trailing
    blank line separation, so a replacement slots in cleanly.

    A top-level key's span covers its whole value, including a multi-line
    array, and the line ending.
    """
    if locator.startswith("top:"):
        return _top_level_key_span(text, locator[4:])
    match = re.search(rf"^\[{re.escape(locator)}\]\s*$", text, re.MULTILINE)
    if not match:
        return None
    own_subtable = re.compile(rf"\[\[?{re.escape(locator)}\.")
    end = match.end()
    while True:
        next_section = re.search(r"^\[", text[end:], re.MULTILINE)
        if not next_section:
            return match.start(), len(text)
        header_at = end + next_section.start()
        if not own_subtable.match(text, header_at):
            return match.start(), header_at
        end = header_at + 1


def _top_level_key_span(text: str, key: str) -> tuple[int, int] | None:
    # Only the pre-table region can hold a top-level key: after the first
    # `[header]`, the same line belongs to that table instead.
    first_table = re.search(r"^\[", text, re.MULTILINE)
    region = text[: first_table.start()] if first_table else text
    match = re.search(rf"^{re.escape(key)}\s*=\s*", region, re.MULTILINE)
    if not match:
        return None
    end = _value_end(text, match.end())
    while end < len(text) and text[end] != "\n":
        end += 1
    return match.start(), end + 1 if end < len(text) else end


def _value_end(text: str, start: int) -> int:
    """Index just past a TOML value starting at `start`, arrays spanning lines.

    Only arrays need the scan - a scalar ends on its own line, which the caller
    finishes. Quoted strings are skipped so a `]` inside one does not close the
    array; a `#` outside a string runs to the end of its line.
    """
    if start >= len(text) or text[start] != "[":
        return start
    depth = 0
    position = start
    while position < len(text):
        char = text[position]
        if char in "\"'":
            position = _string_end(text, position)
            continue
        if char == "#":
            newline = text.find("\n", position)
            position = len(text) if newline < 0 else newline
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    return position


def _string_end(text: str, start: int) -> int:
    """Index just past the single-line TOML string opening at `start`."""
    quote = text[start]
    position = start + 1
    while position < len(text) and text[position] != quote:
        # Basic strings escape with a backslash; literal ('...') strings do not.
        position += 2 if quote == '"' and text[position] == "\\" else 1
    return position + 1


def _upsert_agents_pointer(path: Path) -> str | None:
    """Idempotent marker-delimited pointer in the global AGENTS.md.

    The rest of the file (the user's own instructions) is never touched; an
    existing pointer is replaced in place, an absent one appended.
    """
    text = path.read_text() if path.exists() else ""
    pattern = re.compile(
        re.escape(AGENTS_POINTER_START) + r".*?" + re.escape(AGENTS_POINTER_END),
        re.DOTALL,
    )
    if pattern.search(text):
        if AGENTS_POINTER in text:
            return None  # already current
        new = pattern.sub(AGENTS_POINTER.replace("\\", "\\\\"), text)
        path.write_text(new)
        return "updated"
    new = (text.rstrip("\n") + "\n\n" if text.strip() else "") + AGENTS_POINTER + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)
    return "created"
