"""The DeepSeek Harness (`dsh`) adapter. dsh is a developer preview.

Verified against deepseek-ai/deepseek-harness @ ca33fd68:

- Home: `$DSH_HOME`, default `~/.dsh`. The home-level user patch layer
  `$DSH_HOME/cordis.patch.yml` is applied over EVERY profile. It is a
  top-level YAML list: `- id: X ...` rows override an existing entry (the
  config is replaced whole), and NEW entries go inside an `- insert: [...]`
  item. An `- id:` naming no existing entry is not an insert.
- MCP: our own `@deepseek-ai/dsh-mcp-client` row. Never the `mcp-manager`
  row: patching it replaces the user's whole `servers` map.
- Hooks: none of dsh's own. The `@deepseek-ai/dsh-hooks-claude-code` bridge
  runs Claude-format `command` hooks from one `configPath`, read once at
  startup. It is NOT bundled and this adapter NEVER installs it: that is a
  package download, which needs the user's explicit OK, and setup also runs
  unattended from the auto-updater. The report carries the exact command. The
  bridge pins its dsh peers to the EXACT runtime version, so the command pins
  both packages to `dsh --version` (an unpinned add resolves `latest`, an
  incompatible 0.0.1 line). Plugins land in `<profile>/node_modules/<pkg>`.
- The `desktop` profile is Electron-owned: the npm CLI refuses plugin
  management for it, and only the Desktop-bundled command
  (`<app>/Contents/Resources/runtime/cli/bin/dsh`) can manage it, with Desktop
  fully quit.
- The hooks row goes in the HOME patch, which every profile reads, and only
  while the bridge is installed in EVERY existing profile. A row naming a
  package a profile lacks prints an import failure with a stack trace at each
  boot of that profile. Profile patches are not used: the Desktop app's config
  editor rewrites its own.
- Bridged events: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse,
  Stop, SubagentStart, SubagentStop. No SessionEnd. `transcript_path` is
  always "", so the attachment inbox has nothing to read. The Claude matcher
  dialect applies: a plain `a|b` list matches tool names EXACTLY.
- Agents: presets are Cordis YAML, off by default, and the `subagent` tool has
  no `subagent_type`. Our team cannot be installed as definitions; the
  composed role briefs are written as plain files the lead hands a child
  (`enforcement` names the path).

pyyaml is a dev-only dependency, so the patch file is edited as TEXT: each row
we own sits inside its own marked block, and only those blocks are ever
rewritten. Everything else in the file - rows, comments, `!!js` tags - stays
byte for byte. A file we cannot read as a block list is a conflict, never a
rewrite: a malformed home patch stops every dsh profile from starting.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from memory_mcp.config import settings
from memory_mcp.constants import HARNESS_DEEPSEEK
from memory_mcp.harnesses.base import ChangeReport, HarnessCapabilities
from memory_mcp.harnesses.claude import (
    AGENTS_DIR,
    HOOK_EVENTS,
    agents_source_dir,
    all_hook_scripts,
    copy_hook_scripts,
    hook_scripts_dir,
    installable_agent_text,
    is_abstract_agent,
    missing_hooks_message,
)

HARNESS_NAME = HARNESS_DEEPSEEK
#: The header every harness's MCP config sends so the daemon can tell which
#: harness a `memory_session_start` came from (server.py reads it).
HARNESS_HEADER = "X-Memory-Harness"

PATCH_FILENAME = "cordis.patch.yml"
MCP_ROW_ID = "memory-mcp"
HOOKS_ROW_ID = "memory-mcp-hooks"
MCP_CLIENT_PACKAGE = "@deepseek-ai/dsh-mcp-client"
HOOKS_PLUGIN_PACKAGE = "@deepseek-ai/dsh-hooks-claude-code"
#: The rows other software owns that we must never touch. Named so a test can
#: hold the adapter to it.
FOREIGN_ROW_IDS = frozenset({"mcp-manager"})

#: dsh's own tool names (tool-fs, subagent). The bridge's Claude dialect
#: matches a literal `a|b` list exactly, so `edit` does not also match
#: `notebook_edit`.
DSH_HOOK_MATCHERS = {
    "require-task.sh": "edit|write|apply_patch",
    "record-dispatch.sh": "subagent|subagent_fork",
    "confirm-dispatch.sh": "subagent|subagent_fork",
}
#: Claude events the bridge does not deliver. Stop carries our session-end
#: script already; there is no SessionEnd in either harness's wiring.
UNSUPPORTED_EVENTS = frozenset({"SessionEnd"})

#: The bridge's peers are pinned to the dsh runtime version; this package is
#: the one it names.
HOOK_PROTOCOL_PACKAGE = "@deepseek-ai/dsh-hook-protocol"
#: What to pin to when `dsh --version` cannot be read.
FALLBACK_DIST_TAG = "next"
DESKTOP_PROFILE = "desktop"
#: The command Desktop ships, relative to the .app (apps/desktop README,
#: "Bundled command runtime"). `/usr/local/bin/dsh` links here when the user
#: registered it from Desktop.
BUNDLED_DSH_SUFFIX = ("Contents", "Resources", "runtime", "cli", "bin", "dsh")
DEFAULT_DESKTOP_APP = Path("/Applications/DeepSeek Harness.app")
VERSION_TIMEOUT_SECONDS = 20
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?$")

_BLOCK_BEGIN = "# >>> memory-mcp:"
_BLOCK_END = "# <<< memory-mcp:"
_OUR_ID_RE = re.compile(
    r"^\s*-?\s*id:\s*['\"]?(" + re.escape(MCP_ROW_ID) + "|"
    + re.escape(HOOKS_ROW_ID) + r")['\"]?\s*(#.*)?$"
)

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def dsh_home() -> Path:
    configured = os.environ.get("DSH_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".dsh"


def patch_path() -> Path:
    return dsh_home() / PATCH_FILENAME


def hooks_config_path() -> Path:
    """The Claude-format hooks.json the bridge reads. Ours, beside our other
    machine-wide state, so the user's own hooks files are never touched."""
    return settings.data_dir / "deepseek-hooks.json"


def role_briefs_dir() -> Path:
    """Where the composed role briefs go. dsh cannot load them as agents; the
    lead points a child at `<this>/<role>.md` (see enforcement)."""
    return settings.data_dir / "agents"


def _default_runner(argv: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv), capture_output=True, text=True, timeout=VERSION_TIMEOUT_SECONDS,
    )


def profiles_dir() -> Path:
    return dsh_home() / "profiles"


def existing_profiles() -> tuple[str, ...]:
    """Initialized profiles. `.<name>.staging-*` dirs are dsh's own scratch."""
    root = profiles_dir()
    if not root.is_dir():
        return ()
    return tuple(sorted(
        p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
    ))


def bridge_installed(profile: str) -> bool:
    """The bridge is importable in `profile`: pnpm (hoisted linker) put it in
    the profile's own node_modules."""
    package_json = (
        profiles_dir() / profile / "node_modules" / HOOKS_PLUGIN_PACKAGE / "package.json"
    )
    return package_json.is_file()


def is_bundled_dsh(path: str | None) -> bool:
    if not path:
        return False
    return Path(path).resolve().parts[-len(BUNDLED_DSH_SUFFIX):] == BUNDLED_DSH_SUFFIX


# ---------- the patch file, as text ----------


class PatchConflict(ValueError):
    """The patch file has a shape this adapter will not edit."""


def _yaml_string(value: str) -> str:
    """A double-quoted YAML scalar. JSON string syntax is valid YAML, and it
    escapes everything a URL, header or path could carry."""
    return json.dumps(value)


def _render_block(name: str, row_id: str, package: str, config: dict) -> list[str]:
    lines = [
        f"{_BLOCK_BEGIN}{name} (written by memory-mcp setup; edits inside are replaced)",
        "- insert:",
        f"    - id: {row_id}",
        f"      name: {_yaml_string(package)}",
        "      config:",
    ]
    for key, value in config.items():
        if isinstance(value, dict):
            lines.append(f"        {key}:")
            lines.extend(
                f"          {sub}: {_yaml_string(str(v))}" for sub, v in value.items()
            )
            continue
        lines.append(f"        {key}: {_yaml_string(str(value))}")
    lines.append(f"{_BLOCK_END}{name}")
    return lines


def _managed_spans(lines: Sequence[str]) -> dict[str, tuple[int, int]]:
    """{block name: (begin index, end index)} for every well-formed block."""
    spans: dict[str, tuple[int, int]] = {}
    open_name: str | None = None
    open_at = -1
    for index, line in enumerate(lines):
        if line.startswith(_BLOCK_BEGIN):
            if open_name is not None:
                raise PatchConflict("a memory-mcp block opens inside another one")
            open_name = line[len(_BLOCK_BEGIN):].split(" ", 1)[0]
            open_at = index
            continue
        if not line.startswith(_BLOCK_END):
            continue
        name = line[len(_BLOCK_END):].strip()
        if open_name != name:
            raise PatchConflict(f"a memory-mcp block end ({name!r}) has no matching begin")
        if name in spans:
            raise PatchConflict(f"the memory-mcp block {name!r} appears twice")
        spans[name] = (open_at, index)
        open_name = None
    if open_name is not None:
        raise PatchConflict(f"the memory-mcp block {open_name!r} is never closed")
    return spans


def _significant(lines: Sequence[str]) -> list[str]:
    return [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def upsert_block(text: str, name: str, block: Sequence[str]) -> str:
    """`text` with the managed block `name` set to `block`.

    The block keeps its position on a re-run; a new one goes at the end. A file
    that is only dsh's template (`[]` under comments) has the `[]` replaced -
    an empty flow list followed by a block item is not YAML. Raises
    PatchConflict for anything else that is not a top-level block list, and
    for a hand-written row using one of our ids.
    """
    lines = text.splitlines()
    spans = _managed_spans(lines)
    position: int | None = None
    if name in spans:
        begin, end = spans[name]
        position = begin
        lines = lines[:begin] + lines[end + 1:]
        spans = _managed_spans(lines)

    inside = {i for b, e in spans.values() for i in range(b, e + 1)}
    outside = [ln for i, ln in enumerate(lines) if i not in inside]
    for line in outside:
        if _OUR_ID_RE.match(line):
            raise PatchConflict(
                f"a row with a memory-mcp id is written by hand ({line.strip()!r}); "
                "remove it or move it inside the marked block"
            )

    significant = _significant(outside)
    if significant == ["[]"]:
        template_at = next(i for i, ln in enumerate(lines) if ln.strip() == "[]")
        lines = lines[:template_at] + lines[template_at + 1:]
        if position is not None and position > template_at:
            position -= 1
    elif any(not ln.startswith("-") for ln in significant if not ln[0].isspace()):
        raise PatchConflict("the top level is not a YAML block list")
    elif significant and significant[0][0].isspace():
        raise PatchConflict("the file starts indented, not with a list item")

    if position is None:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("")
        position = len(lines)
    new_lines = lines[:position] + list(block) + lines[position:]
    return "\n".join(new_lines) + "\n"


def remove_block(text: str, name: str) -> str:
    """`text` without the managed block `name`; unchanged when it is absent.

    A file left with no entries gets dsh's own `[]` back, so it stays a list.
    """
    lines = text.splitlines()
    spans = _managed_spans(lines)
    if name not in spans:
        return text
    begin, end = spans[name]
    lines = lines[:begin] + lines[end + 1:]
    while begin < len(lines) and begin > 0 and not lines[begin].strip() and not lines[begin - 1].strip():
        del lines[begin]
    while lines and not lines[-1].strip():
        lines.pop()
    if not _significant(lines):
        lines.append("[]")
    return "\n".join(lines) + "\n"


# ---------- the adapter ----------


class DeepSeekHarness:
    name = HARNESS_NAME

    def __init__(
        self,
        runner: Runner | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        # Injectable so tests never start a real `dsh`. The only process this
        # adapter ever runs is `dsh --version`; it never installs anything.
        self._run = runner or _default_runner
        self._which = which or shutil.which

    def detect(self) -> bool:
        return self._which("dsh") is not None or dsh_home().is_dir()

    def capabilities(self) -> HarnessCapabilities:
        # agents=False: dsh has no file-based agent definitions (presets are
        # Cordis YAML, off by default, and `subagent` takes no type), so the
        # role brief rides in the dispatch prompt instead.
        return HarnessCapabilities(
            hooks=True, agents=False, per_turn_line=True, edit_gate=True,
            # The bridge runs require-task.sh on edit|write|apply_patch; the
            # nudge is its Claude-format additionalContext.
            markdown_nudge=True,
        )

    # ---------- MCP ----------

    def configure_mcp(
        self, server_url: str, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        report = ChangeReport(self.name)
        url = f"{remote_url.rstrip('/')}/mcp" if remote_url else server_url
        headers = {HARNESS_HEADER: HARNESS_NAME}
        if remote_url and token:
            headers["Authorization"] = f"Bearer {token}"
        block = _render_block("mcp", MCP_ROW_ID, MCP_CLIENT_PACKAGE, {
            "serverName": "memory",
            "transport": "streamable-http",
            "url": url,
            "headers": headers,
        })
        if self._write_block(report, "mcp", block, secret=bool(remote_url and token)):
            report.ok(f"MCP server 'memory' -> {url} (row {MCP_ROW_ID} in {patch_path()})")
        return report

    # ---------- hooks ----------

    def install_hooks(
        self, remote_url: str | None = None, token: str | None = None
    ) -> ChangeReport:
        """Write our hooks.json, and mount the bridge row only where the bridge
        is installed. Installs nothing: the report says what to run."""
        report = ChangeReport(self.name)
        hooks_dest = settings.data_dir / "hooks"
        path = hooks_config_path()
        source = hook_scripts_dir()
        if source is None:
            report.conflict(missing_hooks_message(self.name, path))
            return report
        present = set(copy_hook_scripts(source, hooks_dest))
        missing = [name for name in all_hook_scripts() if name not in present]
        if missing:
            report.warn(
                f"hook script(s) not found in {source}: {', '.join(missing)} - "
                "those events are not wired"
            )
        config = self._hooks_config(hooks_dest, present, remote_url, token)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2) + "\n")
        if remote_url and token:
            os.chmod(path, 0o600)
        report.ok(f"Hooks config {path}; events wired: {', '.join(config['hooks'])}")

        profiles = existing_profiles()
        missing = tuple(p for p in profiles if not bridge_installed(p))
        if not profiles or missing:
            if self._remove_block(report, "hooks"):
                report.ok(f"Hooks bridge row {HOOKS_ROW_ID} left out of {patch_path()} "
                          "until the bridge is installed in every profile")
            self._report_bridge_commands(report, missing or ("web",))
            return report

        block = _render_block("hooks", HOOKS_ROW_ID, HOOKS_PLUGIN_PACKAGE, {
            "configPath": str(path),
        })
        if not self._write_block(report, "hooks", block, secret=False):
            return report
        report.ok(f"Hooks bridge mounted for profile(s): {', '.join(profiles)}")
        report.warn(
            "dsh has no SessionEnd and sends an empty transcript_path: session "
            "close rides Stop, and the attachment inbox does not work here"
        )
        return report

    def _hooks_config(
        self, hooks_dest: Path, present: set[str],
        remote_url: str | None, token: str | None,
    ) -> dict:
        parts = ["env", f"MEMORY_MCP_HARNESS={HARNESS_NAME}"]
        if remote_url:
            parts.append(f"MEMORY_MCP_URL={shlex.quote(remote_url.rstrip('/'))}")
            if token:
                parts.append(f"MEMORY_MCP_TOKEN={shlex.quote(token)}")
        env_prefix = " ".join(parts) + " "

        events: dict[str, list[dict]] = {}
        for event, script_names in HOOK_EVENTS.items():
            if event in UNSUPPORTED_EVENTS:
                continue
            groups = []
            for script in script_names:
                if script not in present:
                    continue
                dst = hooks_dest / script
                group: dict = {"hooks": [{
                    "type": "command",
                    "command": f"{env_prefix}{shlex.quote(str(dst))}",
                }]}
                matcher = DSH_HOOK_MATCHERS.get(script)
                if matcher:
                    group["matcher"] = matcher
                groups.append(group)
            if groups:
                events[event] = groups
        return {"hooks": events}

    # ---------- the bridge: commands for the user, never run here ----------

    def _report_bridge_commands(
        self, report: ChangeReport, profiles: Sequence[str],
    ) -> None:
        """One warning per profile with the exact command to install the
        bridge. Reading `dsh --version` is local; the add itself is a download
        and is left to the user."""
        for profile in profiles:
            binary = self._binary_for(profile)
            if binary is None:
                report.warn(
                    f"Hooks need the bridge in the Electron-owned {profile!r} profile, "
                    "which only DeepSeek Harness Desktop's bundled command can manage "
                    "(Manage dsh Command... in Desktop registers it). Launch Desktop "
                    "once, quit it fully, then run: dsh plugin --profile desktop add "
                    f"{HOOKS_PLUGIN_PACKAGE}@<dsh --version> "
                    f"{HOOK_PROTOCOL_PACKAGE}@<dsh --version>, reopen Desktop and "
                    "rerun memory-mcp setup"
                )
                continue
            version = self._dsh_version(binary)
            pin = version or FALLBACK_DIST_TAG
            argv = (
                binary, "plugin", "--profile", profile, "add",
                f"{HOOKS_PLUGIN_PACKAGE}@{pin}", f"{HOOK_PROTOCOL_PACKAGE}@{pin}",
            )
            command = " ".join(shlex.quote(a) for a in argv)
            unpinned = "" if version else (
                f" (`{binary} --version` was unreadable, so this pins the "
                f"`{FALLBACK_DIST_TAG}` tag; match it to your dsh version if that fails)"
            )
            desktop = (
                " Quit DeepSeek Harness Desktop first and reopen it after."
                if profile == DESKTOP_PROFILE else ""
            )
            report.warn(
                f"Hooks are off for dsh profile {profile!r} until its bridge is "
                f"installed (a package download - run it yourself):{desktop} "
                f"{command}{unpinned} - then rerun memory-mcp setup"
            )

    def _binary_for(self, profile: str) -> str | None:
        """The `dsh` that may manage `profile`, as the user would type it.
        Only Desktop's bundled command manages `desktop` (None when it cannot
        be found); it also manages ordinary profiles, as npm dsh does."""
        on_path = self._which("dsh")
        if profile != DESKTOP_PROFILE:
            return "dsh"
        if is_bundled_dsh(on_path):
            return "dsh"
        bundled = DEFAULT_DESKTOP_APP.joinpath(*BUNDLED_DSH_SUFFIX)
        return str(bundled) if bundled.exists() else None

    def _dsh_version(self, binary: str) -> str | None:
        resolved = self._which(binary) if binary == "dsh" else binary
        if resolved is None:
            return None
        try:
            result = self._run((resolved, "--version"))
        except (OSError, subprocess.SubprocessError):
            return None
        version = (result.stdout or "").strip().splitlines()
        if result.returncode != 0 or not version:
            return None
        candidate = version[-1].strip()
        return candidate if _VERSION_RE.match(candidate) else None

    # ---------- agents ----------

    def install_agents(self) -> ChangeReport:
        """Write the composed role briefs as plain files - NOT agent
        definitions. dsh cannot load them; the lead tells a child to read one."""
        report = ChangeReport(self.name)
        source = agents_source_dir()
        if source is None:
            report.warn(f"No agents/ directory at {AGENTS_DIR} - nothing to write.")
            return report
        by_stem = {
            p.stem: p for p in source.glob("*.md") if p.name.lower() != "readme.md"
        }
        sources = sorted(p for p in by_stem.values() if not is_abstract_agent(p))
        dest = role_briefs_dir()
        dest.mkdir(parents=True, exist_ok=True)
        for src in sources:
            (dest / src.name).write_text(installable_agent_text(src, by_stem))
        report.ok(f"{len(sources)} role brief(s) written to {dest}")
        report.warn(
            "dsh has no file-based agent definitions (presets are Cordis YAML, "
            "off by default; `subagent` takes no type): the lead puts the role "
            "brief in the dispatch prompt and names the child after the role"
        )
        return report

    # ---------- shared ----------

    def _write_block(
        self, report: ChangeReport, name: str, block: Sequence[str], *, secret: bool,
    ) -> bool:
        return self._edit_patch(
            report, name, lambda text: upsert_block(text, name, block), secret=secret,
        )

    def _remove_block(self, report: ChangeReport, name: str) -> bool:
        """True when the block was there and is now gone."""
        path = patch_path()
        if not path.exists():
            return False
        before = path.read_text()
        if not self._edit_patch(report, name, lambda text: remove_block(text, name), secret=False):
            return False
        return path.read_text() != before

    def _edit_patch(
        self, report: ChangeReport, name: str, edit: Callable[[str], str], *, secret: bool,
    ) -> bool:
        path = patch_path()
        try:
            current = path.read_text() if path.exists() else ""
        except (OSError, UnicodeDecodeError) as error:
            report.conflict(f"{path} could not be read ({error}); nothing written")
            return False
        try:
            updated = edit(current)
        except PatchConflict as error:
            report.conflict(f"{path}: {error}; row {name!r} not changed - edit it by hand")
            return False
        if updated == current:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated)
        if secret:
            os.chmod(path, 0o600)
        return True
