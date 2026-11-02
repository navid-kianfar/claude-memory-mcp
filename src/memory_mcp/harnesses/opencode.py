"""The OpenCode adapter.

OpenCode carries the MCP surface and a plugin lifecycle, and nothing else:

- MCP: the global `~/.config/opencode/opencode.json(c)` under a top-level
  `mcp` key - `{"memory": {"type": "remote", "url": ..., "enabled": true}}`.
  The file is JSONC (the user's copy on a real machine carries comments), so
  the common case - adding a key that is not there yet - is a TEXTUAL
  insertion that preserves the file byte for byte around it; only a change to
  an existing `mcp` block falls back to parse-and-rewrite, with a warning
  that comments were lost. Verified real: this machine's file is JSONC.
- Lifecycle: a small plugin in `~/.config/opencode/plugins/` (global plugins
  load automatically) that runs the SAME hook scripts Claude runs -
  session-start.sh on `session.created`, session-end.sh on `session.idle`.
  There is no UserPromptSubmit equivalent, so no per-turn rules line; there
  is no PreToolUse equivalent whose verdict can refuse, so no edit gate.
- Agents: OpenCode's agent format differs (mode/model/temperature/tools) and
  was not verified against a live load, so agents report NotSupported rather
  than shipping an unverified shape - the briefs ride the MCP instructions
  and memory_session_start, which is the designed degradation.
"""

import json
import shutil
from pathlib import Path

from memory_mcp.harnesses.base import (
    ChangeReport,
    HarnessCapabilities,
    HarnessNotSupported,
)

PLUGIN_SOURCE = Path(__file__).parent / "opencode-plugin" / "memory-mcp.js"


def opencode_config_dir() -> Path:
    return Path.home() / ".config" / "opencode"


def opencode_config_path() -> Path:
    return opencode_config_dir() / "opencode.jsonc"


def opencode_plugins_dir() -> Path:
    return opencode_config_dir() / "plugins"


def _strip_jsonc(text: str) -> str:
    """Blank out comments only, preserving every other byte's position.

    Enough of a scanner to find a top-level key and nothing more. String
    contents are KEPT verbatim - keys are strings, and the span finder needs
    them - which is safe because a `//` inside a string is then never mistaken
    for a comment. Comments become spaces, so offsets in the stripped text
    index into the original.
    """
    out = []
    i, n = 0, len(text)
    in_string = False
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            out.append(" " * (end - i))
            i = end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _top_level_key_span(stripped: str, key: str) -> tuple[int, int] | None:
    """(value_start, value_end) of a TOP-LEVEL key's value in stripped text.

    Depth-tracks braces from the first `{`; a key at depth 1 is top-level.
    Returns None for a nested or absent key.
    """
    target = f'"{key}"'
    depth = 0
    i, n = 0, len(stripped)
    while i < n:
        ch = stripped[i]
        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            i += 1
            continue
        if depth == 1 and stripped.startswith(target, i):
            j = stripped.index(":", i + len(target)) + 1
            while j < n and stripped[j] in " \t\r\n":
                j += 1
            # Value span: to the matching brace (objects) or the comma/close.
            if j < n and stripped[j] == "{":
                k, d = j, 0
                while k < n:
                    if stripped[k] == "{":
                        d += 1
                    elif stripped[k] == "}":
                        d -= 1
                        if d == 0:
                            return j, k + 1
                    k += 1
            return None
        i += 1
    return None


class OpenCodeHarness:
    name = "opencode"

    def detect(self) -> bool:
        return (
            opencode_config_dir().is_dir()
            or (Path.home() / ".opencode" / "bin" / "opencode").exists()
        )

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            hooks=True, agents=False, per_turn_line=False, edit_gate=False,
        )

    # ---------- MCP ----------

    def configure_mcp(
        self, server_url: str, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        report = ChangeReport(self.name)
        path = opencode_config_path()
        if not path.exists():
            report.warn(f"{path} not found - skipping MCP config.")
            return report
        text = path.read_text()
        stripped = _strip_jsonc(text)
        # Every branch below edits the file; none may run against something
        # that is not JSONC at all. A scanner-verified-but-unparseable file is
        # the user's broken state to fix, not ours to "repair".
        try:
            json.loads(stripped)
        except Exception:  # noqa: BLE001
            report.warn(f"{path} does not parse as JSONC - skipping MCP config.")
            return report

        if remote_url:
            url = f"{remote_url.rstrip('/')}/mcp"
            entry: dict = {"type": "remote", "url": url, "enabled": True}
            if token:
                entry["headers"] = {"Authorization": f"Bearer {token}"}
        else:
            url = server_url
            entry = {"type": "remote", "url": url, "enabled": True}

        span = _top_level_key_span(stripped, "mcp")
        if span is None:
            # The common case: no mcp key yet. Insert it textually right after
            # the opening brace so every comment in the file survives. The
            # block is indented to sit at the top level of a 2-space file.
            block = json.dumps({"memory": entry}, indent=2).replace("\n", "\n  ")
            insertion = f'\n  "mcp": {block},'
            open_brace = stripped.find("{")
            new_text = text[: open_brace + 1] + insertion + text[open_brace + 1 :]
            path.write_text(new_text)
            report.ok(f"MCP server 'memory' -> {url} (comments preserved)")
            return report

        # An mcp block exists: parse (comments stripped) and merge, rewriting
        # the file only when something would actually change.
        try:
            config = json.loads(_strip_jsonc(text))
        except Exception:  # noqa: BLE001
            report.warn(f"{path} does not parse as JSONC - skipping MCP config.")
            return report
        mcp = config.get("mcp") or {}
        previous = mcp.get("memory")
        if isinstance(previous, dict) and previous.get("url") not in (None, url):
            report.conflict(
                f"mcp.memory pointed at {previous.get('url')!r}; "
                f"repointed to {url!r}"
            )
        if mcp.get("memory") == entry:
            report.ok(f"MCP server 'memory' already -> {url}")
            return report
        mcp["memory"] = entry
        config["mcp"] = mcp
        path.write_text(json.dumps(config, indent=2))
        report.ok(f"MCP server 'memory' -> {url}")
        report.warn(
            "the existing mcp block required a rewrite, so JSONC comments in "
            f"{path} were not preserved"
        )
        return report

    # ---------- lifecycle ----------

    def install_hooks(
        self, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        report = ChangeReport(self.name)
        if remote_url:
            # The plugin runs the local scripts, which take MEMORY_MCP_URL via
            # env; a client-mode OpenCode install needs the wrapper inline.
            raise HarnessNotSupported(
                "OpenCode client mode (remote server) is not wired yet; "
                "local daemon only"
            )

        if not PLUGIN_SOURCE.is_file():
            report.warn(f"plugin source missing at {PLUGIN_SOURCE} - skipping.")
            return report

        dest = opencode_plugins_dir()
        dest.mkdir(parents=True, exist_ok=True)
        target = dest / "memory-mcp.js"
        shutil.copy2(PLUGIN_SOURCE, target)
        report.ok(
            f"Lifecycle plugin installed to {target} "
            "(session.created -> import+claim, session.idle -> export+update)"
        )
        report.warn(
            "no per-turn rules line and no edit gate exist in OpenCode; "
            "rules ride the MCP instructions and memory_session_start"
        )
        return report

    # ---------- agents ----------

    def install_agents(self) -> ChangeReport:
        raise HarnessNotSupported(
            "OpenCode's agent format (mode/model/temperature/tools) is not "
            "verified against a live load; briefs ride the MCP instructions "
            "and memory_session_start instead"
        )
