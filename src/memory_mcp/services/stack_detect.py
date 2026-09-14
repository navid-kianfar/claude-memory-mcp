"""Resolve a repository to the specialist agent each of its paths needs.

WHY THIS EXISTS. The lead reads its roster from `~/.claude/agents/*.md` in
alphabetical order with no signal about relevance, so for a Node repo the generic
`backend` name wins over `nodejs` - and `backend.md` does not carry the NestJS /
pnpm conventions `nodejs.md` exists to enforce. Stated by the user on 2026-09-13:
"for a nodejs project a node-agent must be run not a backend agent which is a
general thing. same for other frameworks/languages."

PER PATH, NEVER ONE ANSWER PER REPO. A monorepo holds several stacks at once
(`apps/api` NestJS, `apps/web` Vite+React, `mobile` React Native). Every hit
carries the path it covers and the marker files that decided it, so the brief can
say *why* and the escalation can infer a role from a path.

WHAT THIS DELIBERATELY DOES NOT DO, so nobody "improves" it later:

- **No Groovy, TOML or YAML parsing.** Gradle build scripts and
  `libs.versions.toml` are matched as SUBSTRINGS over the first 64 KB. A real
  parser would mean a dependency and a Groovy evaluation on a path that runs on
  every prompt behind `curl --max-time 2`.
- **`package.json` is read as JSON, first 64 KB only**, and only the union of
  `dependencies` / `devDependencies` / `peerDependencies` KEYS is looked at.
  Versions are never compared; a workspace protocol entry is a dependency like
  any other.
- **No network and no subprocess.** In particular no `git` shell-out: the repo
  root is found by walking up for a `.git` entry, exactly as
  `context.detect_project_from_cwd` does.
- **One `os.scandir()` per directory**, `entry.name` only. No `glob()` (it
  re-walks), no `stat()` except the cache-validation ones.

THE BUDGET. `profile_for` runs on every prompt. Warm it is one SQLite read plus a
few dozen `stat()`; cold it is a bounded walk - see MAX_DEPTH / MAX_DIRS /
SCAN_BUDGET_MS, every one of which reports `truncated=True` rather than raising.
Measured cold on 2026-09-13 against the widest repo on this machine: 90
directories in 3.5 ms. The constants carry the full table and why they are set
where they are.

TWO PRODUCT DECISIONS ARE BAKED IN (memory `d8b8baa3`, user, 2026-09-13):

1. A **Next.js** repo resolves to `nodejs` as the one owner, with `react` named
   as the escalation for screen and component work (`StackHit.co_agent`). NOT a
   per-path split: Next puts server components in `app/`, so a path split would
   be wrong about the boundary it drew.
2. **React Native gets its own agent**, `react-native`. An RN repo never resolves
   to `react` and never to the generic `frontend`. The definition file may not be
   installed yet - `agents_for` substitutes the `frontend` fallback when it is
   missing, and the caller renders the note.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, replace
from pathlib import Path

#: Bump to invalidate every cached profile. This is how a new marker rule ships:
#: a cached answer computed by an older ruleset is wrong, not stale.
DETECTOR_VERSION = 1

#: The generic roles a specialist extends. Named here rather than in the caller
#: so "which names are the fallbacks" has one definition.
GENERIC_ROLES = frozenset({"backend", "frontend"})

#: Roles that are not about a stack at all: they apply to every repo, so they are
#: never reordered by detection and never substituted for a specialist.
NON_STACK_ROLES = frozenset({"pm", "designer", "test", "reviewer", "devops", "docs"})

#: Every agent this detector can name, including one that may not be installed
#: yet (`react-native` - a sibling task writes the definition). Used by
#: `order_roster` to tell a stack expert apart from a general role.
STACK_AGENTS = frozenset(
    {
        "python",
        "nodejs",
        "react",
        "react-native",
        "go",
        "rust",
        "dotnet",
        "app",
        "kotlin",
    }
)

# ---------------------------------------------------------------- walk budget
#
# MAX_DEPTH = 2 catches the containers that actually hold a second stack -
# `apps/api`, `packages/ui`, `services/auth`, `androidApp/`, `shared/` - and
# stops before it becomes a tree walk.
#
# MAX_DIRS and SCAN_BUDGET_MS are SAFETY VALVES, not expectations. Measured on
# 2026-09-13 against every repo under ~/Desktop/DEV, one fresh process per repo
# (`dirs` is what this walk actually enters, AFTER SKIP_DIRS and the dot-prefix
# skip - far fewer than the raw directory count):
#
#     repo                                       dirs   cold ms   hits
#     claude-memory-mcp (this repo)                16      0.68      2
#     achasoft/kalagh                              44      2.89     26
#     visitor-analytics/VisitorAnalytics1.0        67      2.39      9
#     achasoft/deepseek-harness                    83      3.46     11
#     smg.core.platform                            90      2.67     25
#
# NOT ONE REAL REPO CAME NEAR EITHER LIMIT. The widest scans 90 directories in
# 3.5 ms; with the tree itself cold (its first touch in a session) the worst
# observed was 22 ms. So MAX_DIRS = 300 leaves 3.3x headroom on count and
# SCAN_BUDGET_MS = 120 leaves ~5x on the worst cold walk - both kept deliberately
# generous rather than tuned down, because the failure mode of a tight budget is
# a TRUNCATED answer (a monorepo module silently losing its specialist) while the
# failure mode of a loose one is latency on a cache miss that happens at most
# once per repo per TTL. 120 ms is 6% of the hook's 2 s timeout.
#
# MAX_DIRS is the limit that binds first and that is deliberate: a hard count is
# deterministic where a clock is not, so the same repo truncates identically on a
# fast machine and a slow one. Neither valve has a real repo to prove it on -
# `TestBudget` monkeypatches them low, which is the only honest way to test them.
MAX_DEPTH = 2
MAX_DIRS = 300
SCAN_BUDGET_MS = 120.0

#: The JVM sub-split has to look DEEPER than MAX_DEPTH - an `AndroidManifest.xml`
#: lives at `src/main/`, a `commonMain/` under `src/`. Those probes are bounded
#: separately, because they are not counted against MAX_DIRS and a large Gradle
#: monorepo would otherwise turn one directory's verdict into a tree walk. The
#: main loop's clock check is per directory, so this cap is what bounds the
#: overshoot a single slow directory can cause.
SUBPROBE_MAX_DIRS = 150

#: Backstop only. Mtime watching (below) is what really keeps a cached profile
#: honest; this catches the case mtimes cannot see, such as a marker file whose
#: mtime was preserved by a checkout.
TTL_SECONDS = 900.0

#: Directory names never entered, at any level. A symlinked directory is skipped
#: outright (see `_scan`) - a symlink loop is the one way a depth-bounded walk
#: still hangs.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        ".tox",
        "__pycache__",
        "dist",
        "build",
        "out",
        "target",
        "bin",
        "obj",
        ".next",
        ".nuxt",
        ".svelte-kit",
        ".output",
        "vendor",
        "Pods",
        ".gradle",
        ".idea",
        ".vscode",
        "coverage",
        "htmlcov",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".terraform",
        ".claude",
        ".claude-memory",
        "site-packages",
        "DerivedData",
    }
)

#: How far up from a cwd to look for a repo root. Matches
#: `context.detect_project_from_cwd`'s own limit.
MAX_ROOT_WALKUP = 10

#: Only the head of a marker file is ever read. A 64 KB `package.json` would be
#: extraordinary; a lockfile that size is routine, which is why no lockfile is
#: parsed.
MARKER_READ_BYTES = 64 * 1024

#: How many paths an `order_roster` annotation names before it says "+N more".
#: Measured against a real pnpm monorepo (postloom) on 2026-09-13: nodejs owned
#: 13 paths there, a 300-character annotation, and the per-turn line it feeds has
#: 19 characters of headroom (survey 3 §0). The full list is always in
#: `profile.hits`, so the cap hides nothing - it just stops the annotation from
#: being the reason a consumer has to truncate.
ANNOTATION_MAX_PATHS = 3

_CACHE_KEY_PREFIX = "stack:profile:"


@dataclass(frozen=True)
class StackHit:
    """One directory's verdict: the stack it is, and who to dispatch for it."""

    #: POSIX, relative to the profile's root; "." for the root itself.
    path: str
    #: The detected stack, finer-grained than the agent: several stacks can map
    #: to one agent (`node-nest` and `node-generic` both to `nodejs`), and the
    #: distinction is what the "why" text is written from.
    stack: str
    #: The agent to dispatch. One of STACK_AGENTS, or a member of GENERIC_ROLES
    #: when no specialist claims the stack.
    agent: str
    #: A second agent to dispatch alongside, for part of the work. Only ever
    #: "react" on a `node-next` hit: nodejs owns a Next repo, react is dispatched
    #: for screens and components inside it.
    co_agent: str | None
    #: The generic role `agent` extends, substituted when `agent` is not
    #: installed.
    fallback: str
    #: The files that decided it, root-relative POSIX, for the "why".
    markers: tuple[str, ...]
    #: "strong" | "weak". A weak marker (a tooling-only `package.json`) loses to
    #: a strong sibling from a lower-precedence ecosystem.
    strength: str
    #: Text naming a framework no installed agent claims, or None.
    warning: str | None


@dataclass(frozen=True)
class StackProfile:
    """Every verdict for one repo root, plus what the walk cost."""

    root: str
    #: Root hit first, then by path: shortest first, then alphabetical.
    hits: tuple[StackHit, ...]
    #: The root hit; else the single strong hit; else None when several compete.
    primary: StackHit | None
    detected_at: float
    scan_ms: float
    dirs_scanned: int
    #: A limit was hit. The hits found are still valid - there may be more.
    truncated: bool
    from_cache: bool


# --------------------------------------------------------------- marker table
#
# Two axes per ecosystem: an ORDINAL (which ecosystem wins inside one directory)
# and a STRENGTH. Node is deliberately last: a `package.json` is the commonest
# incidental file in another stack's repo, where a `go.mod` or `pyproject.toml`
# almost never is. Inside a directory the comparison is STRENGTH FIRST, THEN THE
# ORDINAL - so `requirements.txt` (weak python) loses to a NestJS `package.json`
# (strong node), while `pyproject.toml` (strong python) beats any `package.json`
# beside it. Across directories there is no contest: each keeps its own hit.

_ORDINAL = {"dotnet": 1, "go": 2, "rust": 3, "jvm": 4, "python": 5, "node": 6}

_STRONG_NAMES: dict[str, str] = {
    "go.mod": "go",
    "Cargo.toml": "rust",
    "settings.gradle.kts": "jvm",
    "settings.gradle": "jvm",
    "build.gradle.kts": "jvm",
    "build.gradle": "jvm",
    "pom.xml": "jvm",
    "pyproject.toml": "python",
    "uv.lock": "python",
    "Pipfile": "python",
    "setup.py": "python",
    "setup.cfg": "python",
    "package.json": "node",  # strength decided by its contents - see _node_hit
}

_WEAK_NAMES: dict[str, str] = {
    "global.json": "dotnet",
    "Directory.Build.props": "dotnet",
    "poetry.lock": "python",
    "environment.yml": "python",
}

#: A `*.csproj` / `*.sln` / `*.fsproj` is a strong dotnet marker. Checked as a
#: name suffix inside the one scandir, never with glob().
_DOTNET_SUFFIXES = (".sln", ".csproj", ".fsproj")

# Node sub-split signals. Order matters: the first match wins, and the order is
# the product decision - Nest and Next are nodejs's own territory per
# `nodejs.md`, and react-native outranks react because an RN repo has `react` in
# it too.
_NEST_DEPS = ("@nestjs/core", "@nestjs/common")
_NEXT_DEPS = ("next",)
_RN_DEPS = ("react-native", "expo")
_REACT_DEPS = ("react", "react-dom")
_OTHER_UI_DEPS = ("@angular/core", "vue", "svelte", "@sveltejs/kit", "astro", "nuxt")
_NODE_SERVER_DEPS = (
    "express",
    "fastify",
    "koa",
    "@hapi/hapi",
    "bullmq",
    "socket.io",
    "ws",
    "apollo-server",
    "@apollo/server",
    "typeorm",
    "prisma",
    "drizzle-orm",
)

_JVM_MOBILE_SIGNALS = (
    "com.android.application",
    "com.android.library",
    "org.jetbrains.compose",
    "compose-multiplatform",
    'kotlin("multiplatform")',
    "kotlin-multiplatform",
)
_JVM_SERVER_SIGNALS = (
    "io.ktor",
    "ktor-server",
    "org.springframework.boot",
    "spring-boot",
)
_JVM_BUILD_FILES = (
    "build.gradle.kts",
    "build.gradle",
    "settings.gradle.kts",
    "settings.gradle",
)


def _read_head(path: Path) -> str:
    """The first MARKER_READ_BYTES of a text file, or "" if unreadable."""
    try:
        with path.open("rb") as handle:
            return handle.read(MARKER_READ_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""


def _package_deps(path: Path) -> set[str]:
    """Union of dependencies/devDependencies/peerDependencies KEYS.

    Malformed JSON yields an empty set, which makes the `package.json` a weak
    marker - the honest answer, since nothing about the project was learned.
    """
    try:
        data = json.loads(_read_head(path))
    except (ValueError, TypeError):
        return set()
    if not isinstance(data, dict):
        return set()
    deps: set[str] = set()
    for field in ("dependencies", "devDependencies", "peerDependencies"):
        block = data.get(field)
        if isinstance(block, dict):
            deps.update(str(k) for k in block)
    return deps


def _has_config(names: set[str], stem: str) -> str | None:
    """`<stem>.{js,cjs,mjs,ts,mts}` in this directory, or None."""
    for ext in (".js", ".mjs", ".cjs", ".ts", ".mts"):
        candidate = f"{stem}{ext}"
        if candidate in names:
            return candidate
    return None


def _node_hit(names: set[str], directory: Path) -> tuple:
    """The node sub-split: (stack, agent, co_agent, fallback, markers, strength, warning).

    `markers` are bare file names here; `_hit_for_dir` prefixes the directory.
    """
    deps = _package_deps(directory / "package.json")
    markers = ["package.json"]

    nest_cli = "nest-cli.json" in names
    if nest_cli or any(d in deps for d in _NEST_DEPS):
        if nest_cli:
            markers.append("nest-cli.json")
        return ("node-nest", "nodejs", None, "backend", tuple(markers), "strong", None)

    next_config = _has_config(names, "next.config")
    if next_config or any(d in deps for d in _NEXT_DEPS):
        if next_config:
            markers.append(next_config)
        # Decision d8b8baa3: one owner (nodejs), react named as the escalation
        # for screens. Next puts server components in app/, so a path split
        # would be wrong about the boundary it drew.
        return ("node-next", "nodejs", "react", "backend", tuple(markers), "strong", None)

    # react-native before react: an RN package.json lists `react` too.
    if any(d in deps for d in _RN_DEPS):
        # No warning: `react-native` is a real agent now (decision d8b8baa3).
        # Whether its definition is installed on THIS machine is `agents_for`'s
        # business, and the caller renders the note.
        return (
            "react-native",
            "react-native",
            None,
            "frontend",
            tuple(markers),
            "strong",
            None,
        )

    vite_config = _has_config(names, "vite.config")
    if vite_config and any(d in deps for d in _REACT_DEPS):
        markers.append(vite_config)
        return ("vite-react", "react", None, "frontend", tuple(markers), "strong", None)

    for dep in _OTHER_UI_DEPS:
        if dep in deps:
            return (
                "node-" + dep.lstrip("@").split("/")[0],
                "frontend",
                None,
                "frontend",
                tuple(markers),
                "strong",
                f"{dep} detected; no installed specialist covers it",
            )

    if any(d in deps for d in _REACT_DEPS):
        # react.md claims the Vite + pnpm + Tailwind + shadcn stack specifically,
        # not "React in general". Do not stretch it past its own sentence.
        return (
            "react-web",
            "frontend",
            None,
            "frontend",
            tuple(markers),
            "strong",
            "react without Vite detected; the react agent covers the Vite stack only",
        )

    if "electron" in deps:
        return (
            "electron",
            "nodejs",
            None,
            "backend",
            tuple(markers),
            "strong",
            "electron detected; no installed specialist covers it",
        )

    if any(d in deps for d in _NODE_SERVER_DEPS):
        return (
            "node-generic",
            "nodejs",
            None,
            "backend",
            tuple(markers),
            "strong",
            None,
        )

    # Overlap H: a tooling-only package.json (prettier + husky in a Python or Go
    # repo) must not beat a strong sibling marker. Weak is how that is expressed.
    return ("node-generic", "nodejs", None, "backend", tuple(markers), "weak", None)


def _jvm_hit(names: set[str], directory: Path) -> tuple:
    """The JVM sub-split: mobile artefacts decide `app`, not the Kotlin language."""
    markers: list[str] = []
    text_parts: list[str] = []
    for build_file in _JVM_BUILD_FILES:
        if build_file in names:
            markers.append(build_file)
            text_parts.append(_read_head(directory / build_file))
    if "pom.xml" in names:
        markers.append("pom.xml")
        text_parts.append(_read_head(directory / "pom.xml"))
    catalog = directory / "gradle" / "libs.versions.toml"
    if catalog.is_file():
        markers.append("gradle/libs.versions.toml")
        text_parts.append(_read_head(catalog))
    text = "\n".join(text_parts)

    mobile = any(signal in text for signal in _JVM_MOBILE_SIGNALS)
    if not mobile:
        mobile = _has_dir(directory, "iosApp") or _has_mobile_artefact(directory)
    server = any(signal in text for signal in _JVM_SERVER_SIGNALS)
    if not server and 'kotlin("jvm")' in text and "application" in text:
        server = True

    if mobile:
        return ("kmp-mobile", "app", None, "frontend", tuple(markers), "strong", None)
    if server:
        return (
            "kotlin-server",
            "kotlin",
            None,
            "backend",
            tuple(markers),
            "strong",
            None,
        )
    kinds = _source_kinds(directory)
    if "kt" in kinds:
        return (
            "kotlin-server",
            "kotlin",
            None,
            "backend",
            tuple(markers),
            "strong",
            None,
        )
    if "java" in kinds:
        return (
            "jvm-java",
            "backend",
            None,
            "backend",
            tuple(markers),
            "strong",
            "Java/JVM detected; no installed specialist covers it",
        )
    # A Gradle file with no sources under it is a container (the `settings.gradle
    # .kts` of a monorepo). Weak, so a real module's hit is what the lead sees.
    return ("jvm-java", "backend", None, "backend", tuple(markers), "weak", None)


def _has_dir(directory: Path, name: str) -> bool:
    try:
        return (directory / name).is_dir()
    except OSError:
        return False


def _has_mobile_artefact(directory: Path) -> bool:
    """An `AndroidManifest.xml` or a `commonMain/` within 3 levels of `directory`.

    Bounded by hand rather than by os.walk: the budget is the whole point.
    """
    frontier = [(directory, 0)]
    seen = 0
    while frontier and seen < SUBPROBE_MAX_DIRS:
        current, depth = frontier.pop()
        if depth > 3:
            continue
        seen += 1
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.name == "AndroidManifest.xml":
                return True
            if entry.name in SKIP_DIRS:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name == "commonMain":
                        return True
                    frontier.append((Path(entry.path), depth + 1))
            except OSError:
                continue
    return False


def _source_kinds(directory: Path, max_depth: int = 3) -> set[str]:
    """Which of {"kt", "java"} appear under `directory` within `max_depth`."""
    kinds: set[str] = set()
    frontier = [(directory, 0)]
    seen = 0
    while frontier and seen < SUBPROBE_MAX_DIRS and kinds != {"kt", "java"}:
        current, depth = frontier.pop()
        if depth > max_depth:
            continue
        seen += 1
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.name in SKIP_DIRS:
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                frontier.append((Path(entry.path), depth + 1))
            elif entry.name.endswith(".kt"):
                kinds.add("kt")
            elif entry.name.endswith(".java"):
                kinds.add("java")
    return kinds


def _hit_for_dir(root: Path, directory: Path, names: set[str]) -> StackHit | None:
    """The single winning verdict for one directory, or None if it has no marker.

    A directory holding `__init__.py` is INSIDE a Python package, not a project
    root, so its python markers are ignored. Measured on this repo:
    `src/memory_mcp/setup.py` is a MODULE of the installer, not a packaging
    script, and without this guard it produced a spurious third hit at
    `src/memory_mcp`. A real packaging `setup.py` sits beside the package, never
    within it. The guard is python-only - `__init__.py` says nothing about a
    `go.mod` or a `package.json`.
    """
    in_package = "__init__.py" in names
    candidates: list[tuple[int, int, tuple]] = []

    def add(ecosystem: str, payload: tuple) -> None:
        strength = payload[5]
        # strength first (strong = 0), then the ecosystem ordinal
        candidates.append((0 if strength == "strong" else 1, _ORDINAL[ecosystem], payload))

    dotnet_markers = tuple(
        sorted(n for n in names if n.endswith(_DOTNET_SUFFIXES))
    )
    if dotnet_markers:
        add("dotnet", ("dotnet", "dotnet", None, "backend", dotnet_markers, "strong", None))

    # Each ecosystem's sub-split runs AT MOST ONCE. A Gradle root routinely
    # carries `settings.gradle.kts` and `build.gradle.kts` together, and
    # `_jvm_hit` already aggregates every build file it finds - calling it per
    # matching name would repeat its SUBPROBE_MAX_DIRS walk up to five times for
    # one directory's verdict. Insertion order of _STRONG_NAMES decides which
    # marker name gets reported, so the most informative one is listed first
    # (`pyproject.toml` before `setup.py`).
    decided: set[str] = set()
    for name, ecosystem in _STRONG_NAMES.items():
        if name not in names or ecosystem in decided:
            continue
        if ecosystem == "python" and in_package:
            continue
        decided.add(ecosystem)
        if ecosystem == "node":
            add("node", _node_hit(names, directory))
        elif ecosystem == "jvm":
            add("jvm", _jvm_hit(names, directory))
        elif ecosystem == "python":
            add(
                "python",
                ("python", "python", None, "backend", (name,), "strong", None),
            )
        elif ecosystem == "go":
            add("go", ("go", "go", None, "backend", ("go.mod",), "strong", None))
        elif ecosystem == "rust":
            add("rust", ("rust", "rust", None, "backend", ("Cargo.toml",), "strong", None))

    for name, ecosystem in _WEAK_NAMES.items():
        if name in names and not (ecosystem == "python" and in_package):
            add(
                ecosystem,
                (ecosystem, ecosystem, None, "backend", (name,), "weak", None),
            )
    if not in_package:
        for name in sorted(names):
            if name.startswith("requirements") and name.endswith(".txt"):
                add("python", ("python", "python", None, "backend", (name,), "weak", None))
                break

    if not candidates:
        return None

    candidates.sort(key=lambda c: (c[0], c[1]))
    stack, agent, co_agent, fallback, markers, strength, warning = candidates[0][2]

    rel = _rel(root, directory)
    prefix = "" if rel == "." else f"{rel}/"
    return StackHit(
        path=rel,
        stack=stack,
        agent=agent,
        co_agent=co_agent,
        fallback=fallback,
        markers=tuple(f"{prefix}{m}" for m in markers),
        strength=strength,
        warning=warning,
    )


def _rel(root: Path, directory: Path) -> str:
    try:
        rel = directory.relative_to(root).as_posix()
    except ValueError:
        return "."
    return rel or "."


def _scan(root: Path) -> tuple[list[StackHit], int, bool, list[Path]]:
    """The bounded walk. Returns (hits, dirs_scanned, truncated, watched_dirs).

    Breadth-first so that a truncated walk has still looked at everything
    shallow - a depth-1 module matters more than a depth-2 one.
    """
    started = time.monotonic()
    hits: list[StackHit] = []
    watched: list[Path] = [root]
    frontier: list[tuple[Path, int]] = [(root, 0)]
    scanned = 0
    truncated = False

    while frontier:
        if scanned >= MAX_DIRS:
            truncated = True
            break
        if (time.monotonic() - started) * 1000.0 > SCAN_BUDGET_MS:
            truncated = True
            break
        directory, depth = frontier.pop(0)
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        scanned += 1
        names = {entry.name for entry in entries}
        hit = _hit_for_dir(root, directory, names)
        if hit is not None:
            hits.append(hit)
            if directory != root:
                watched.append(directory)
        if depth >= MAX_DEPTH:
            continue
        for entry in entries:
            if entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            try:
                # follow_symlinks=False: a symlink loop is the one way a
                # depth-bounded walk still hangs.
                if entry.is_dir(follow_symlinks=False):
                    child = Path(entry.path)
                    frontier.append((child, depth + 1))
                    if depth == 0:
                        watched.append(child)
            except OSError:
                continue

    hits.sort(key=lambda h: (h.path != ".", h.path.count("/"), h.path))
    return hits, scanned, truncated, watched


def _pick_primary(hits: tuple[StackHit, ...]) -> StackHit | None:
    for hit in hits:
        if hit.path == ".":
            return hit
    strong = [h for h in hits if h.strength == "strong"]
    if len(strong) == 1:
        return strong[0]
    return None


def detect(root: str | Path) -> StackProfile:
    """Walk `root` and resolve every directory it reaches. Never cached."""
    root_path = Path(root).resolve()
    started = time.monotonic()
    hits, scanned, truncated, _watched = _scan(root_path)
    hit_tuple = tuple(hits)
    return StackProfile(
        root=str(root_path),
        hits=hit_tuple,
        primary=_pick_primary(hit_tuple),
        detected_at=time.time(),
        scan_ms=(time.monotonic() - started) * 1000.0,
        dirs_scanned=scanned,
        truncated=truncated,
        from_cache=False,
    )


# ----------------------------------------------------------------- root resolution


def resolve_root(cwd: str | None, slug: str | None = None) -> Path | None:
    """The repo root for a cwd: a `.git` entry walking up, else the bound path.

    A linked worktree's `.git` is a FILE, not a directory, so the test is for an
    entry of either kind. No `git` shell-out: a subprocess is banned on this path.
    """
    if cwd:
        try:
            current = Path(cwd).resolve()
        except OSError:
            current = None
        if current is not None:
            for _ in range(MAX_ROOT_WALKUP):
                try:
                    if (current / ".git").exists():
                        return current
                except OSError:
                    break
                if current.parent == current:
                    break
                current = current.parent
    if slug:
        try:
            from memory_mcp.container import container

            project = container.project_repo.get(slug)
        except Exception:  # noqa: BLE001
            project = None
        if project is not None and project.project_path:
            candidate = Path(project.project_path)
            if candidate.is_dir():
                return candidate.resolve()
    if cwd:
        candidate = Path(cwd)
        if candidate.is_dir():
            return candidate.resolve()
    return None


# ------------------------------------------------------------------------ cache
#
# L1 is a process memo (the daemon is long-lived, so every turn after the first
# is a dict lookup). L2 is the SQLite registry, the precedent `update_poller`
# set: "Cheap: a single SQLite read. The hook path calls this on every prompt."
#
# Keyed on the resolved root's ABSOLUTE PATH, never the slug: a slug can be
# rebound to a moved folder, a monorepo subdirectory must share the root's
# answer, and two worktrees of one repo are different roots with different
# content.

_MEMO: dict[str, tuple[StackProfile, dict[str, int], dict[str, int]]] = {}


def _cache_key(root: str) -> str:
    return f"{_CACHE_KEY_PREFIX}{hashlib.sha1(root.encode()).hexdigest()}"


def _mtime_ns(path: Path | str) -> int | None:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


def _watch_maps(
    root: Path, hits: tuple[StackHit, ...], watched_dirs: list[Path]
) -> tuple[dict[str, int], dict[str, int]]:
    """What must be re-stat()ed to trust a cached answer.

    Directories: adding `apps/web/package.json` changes `apps/web`'s mtime;
    adding the whole `apps/web/` changes `apps`'. Files: adding a dependency to
    an EXISTING `package.json` changes the file, not its directory - which is
    what makes a 900 s TTL honest.
    """
    dirs: dict[str, int] = {}
    for directory in watched_dirs:
        mtime = _mtime_ns(directory)
        if mtime is not None:
            dirs[str(directory)] = mtime
    files: dict[str, int] = {}
    for hit in hits:
        for marker in hit.markers:
            path = root / marker
            mtime = _mtime_ns(path)
            if mtime is not None:
                files[str(path)] = mtime
    return dirs, files


def _watches_hold(dirs: dict[str, int], files: dict[str, int]) -> bool:
    for path, mtime in dirs.items():
        if _mtime_ns(path) != mtime:
            return False
    for path, mtime in files.items():
        if _mtime_ns(path) != mtime:
            return False
    return True


def _hit_to_dict(hit: StackHit) -> dict:
    return {
        "path": hit.path,
        "stack": hit.stack,
        "agent": hit.agent,
        "co_agent": hit.co_agent,
        "fallback": hit.fallback,
        "markers": list(hit.markers),
        "strength": hit.strength,
        "warning": hit.warning,
    }


def _hit_from_dict(data: dict) -> StackHit:
    return StackHit(
        path=str(data["path"]),
        stack=str(data["stack"]),
        agent=str(data["agent"]),
        co_agent=data.get("co_agent"),
        fallback=str(data.get("fallback") or "backend"),
        markers=tuple(data.get("markers") or ()),
        strength=str(data.get("strength") or "strong"),
        warning=data.get("warning"),
    )


def _read_cache(root: Path) -> StackProfile | None:
    """A cached profile for `root`, revalidated, or None.

    Invalidation order, cheapest decisive check first: DETECTOR_VERSION, then
    directory mtimes, then marker-file mtimes, then the TTL backstop.
    """
    key = _cache_key(str(root))
    memo = _MEMO.get(key)
    if memo is not None:
        profile, dirs, files = memo
        if (
            time.time() - profile.detected_at <= TTL_SECONDS
            and _watches_hold(dirs, files)
        ):
            # The memo holds the object the walk produced, whose `from_cache` is
            # False. Returning it unchanged would tell the caller it had just
            # walked the tree.
            return replace(profile, from_cache=True)
        _MEMO.pop(key, None)

    try:
        from memory_mcp.db.registry import get_setting

        raw = get_setting(key)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("version") != DETECTOR_VERSION:
        return None
    dirs = {str(k): int(v) for k, v in (data.get("watch_dirs") or {}).items()}
    files = {str(k): int(v) for k, v in (data.get("watch_files") or {}).items()}
    if not _watches_hold(dirs, files):
        return None
    detected_at = float(data.get("detected_at") or 0.0)
    if time.time() - detected_at > TTL_SECONDS:
        return None
    try:
        hits = tuple(_hit_from_dict(h) for h in data.get("hits") or ())
    except (KeyError, TypeError, ValueError):
        return None
    profile = StackProfile(
        root=str(data.get("root") or root),
        hits=hits,
        primary=_pick_primary(hits),
        detected_at=detected_at,
        scan_ms=float(data.get("scan_ms") or 0.0),
        dirs_scanned=int(data.get("dirs_scanned") or 0),
        truncated=bool(data.get("truncated")),
        from_cache=True,
    )
    _MEMO[key] = (profile, dirs, files)
    return profile


def _write_cache(
    root: Path, profile: StackProfile, dirs: dict[str, int], files: dict[str, int]
) -> None:
    """Best effort. The hook must never fail because the registry was locked."""
    key = _cache_key(str(root))
    _MEMO[key] = (profile, dirs, files)
    payload = {
        "version": DETECTOR_VERSION,
        "root": profile.root,
        "hits": [_hit_to_dict(h) for h in profile.hits],
        "detected_at": profile.detected_at,
        "scan_ms": profile.scan_ms,
        "dirs_scanned": profile.dirs_scanned,
        "truncated": profile.truncated,
        "watch_dirs": dirs,
        "watch_files": files,
    }
    try:
        from memory_mcp.db.registry import set_setting

        set_setting(key, json.dumps(payload))
    except Exception:  # noqa: BLE001
        pass


def clear_cache() -> None:
    """Drop the process memo. The registry layer revalidates itself."""
    _MEMO.clear()


def profile_for(cwd: str | None, slug: str | None = None) -> StackProfile | None:
    """The cached profile for whatever repo `cwd` (or `slug`) is in.

    Returns None - not an empty profile - when no root can be resolved, so every
    caller has one obvious "say nothing" branch.
    """
    root = resolve_root(cwd, slug)
    if root is None:
        return None
    cached = _read_cache(root)
    if cached is not None:
        return cached

    started = time.monotonic()
    hits, scanned, truncated, watched = _scan(root)
    hit_tuple = tuple(hits)
    profile = StackProfile(
        root=str(root),
        hits=hit_tuple,
        primary=_pick_primary(hit_tuple),
        detected_at=time.time(),
        scan_ms=(time.monotonic() - started) * 1000.0,
        dirs_scanned=scanned,
        truncated=truncated,
        from_cache=False,
    )
    dirs, files = _watch_maps(root, hit_tuple, watched)
    _write_cache(root, profile, dirs, files)
    return profile


# ------------------------------------------------------------------- consumers


def agents_for(profile: StackProfile, installed: set[str]) -> list[str]:
    """Dispatch order: specialists first, de-duplicated, fallbacks substituted.

    A hit whose agent is not installed contributes its `fallback` instead - the
    CALLER renders the "not installed" note, from the hit, because only the
    caller knows how much room it has for prose.
    """
    ordered: list[str] = []
    generic: list[str] = []

    def add(name: str, bucket: list[str]) -> None:
        if name not in ordered and name not in generic:
            bucket.append(name)

    for hit in profile.hits:
        if hit.agent in installed:
            add(hit.agent, ordered)
        else:
            add(hit.fallback, generic)
        if hit.co_agent:
            if hit.co_agent in installed:
                add(hit.co_agent, ordered)
            else:
                add(hit.fallback, generic)
    return ordered + generic


def order_roster(
    agents: list[tuple[str, str]],
    profile: StackProfile | None,
    installed: set[str],
) -> list[tuple[str, str, str]]:
    """(name, description, annotation), re-ordered by what this repo actually is.

    Detected specialists first with their paths named, then the non-stack roles,
    then the generic fallbacks, then the stack experts this repo has no use for.
    Descriptions are passed through byte-identical: the annotation is a separate
    string so the caller decides whether to render it at all.
    """
    paths: dict[str, list[str]] = {}
    if profile is not None:
        for hit in profile.hits:
            if hit.agent in installed:
                paths.setdefault(hit.agent, []).append(hit.path)
            else:
                paths.setdefault(hit.fallback, []).append(hit.path)
            if hit.co_agent and hit.co_agent in installed:
                paths.setdefault(hit.co_agent, []).append(hit.path)

    def where(hit_paths: list[str]) -> str:
        shown = ", ".join(f"`{p}`" for p in hit_paths[:ANNOTATION_MAX_PATHS])
        extra = len(hit_paths) - ANNOTATION_MAX_PATHS
        return f"{shown} (+{extra} more)" if extra > 0 else shown

    detected: list[tuple[str, str, str]] = []
    non_stack: list[tuple[str, str, str]] = []
    fallbacks: list[tuple[str, str, str]] = []
    unused: list[tuple[str, str, str]] = []

    for name, description in agents:
        if name in NON_STACK_ROLES:
            non_stack.append((name, description, ""))
            continue
        hit_paths = paths.get(name)
        if hit_paths and name not in GENERIC_ROLES:
            detected.append((name, description, f"THIS REPO - under {where(hit_paths)}"))
        elif name in GENERIC_ROLES:
            if hit_paths:
                detected.append(
                    (
                        name,
                        description,
                        f"THIS REPO - under {where(hit_paths)}, no specialist installed",
                    )
                )
            else:
                fallbacks.append(
                    (name, description, "GENERIC FALLBACK - only where no specialist matches")
                )
        else:
            unused.append((name, description, "not this repo's stack"))

    dispatch = agents_for(profile, installed) if profile is not None else []
    order = {name: i for i, name in enumerate(dispatch)}
    detected.sort(key=lambda row: order.get(row[0], len(order)))
    return detected + non_stack + fallbacks + unused


def hit_for_path(profile: StackProfile, rel_path: str) -> StackHit | None:
    """The longest path-prefix hit covering `rel_path`, SEGMENT-WISE.

    Segment-wise matters: `apps/api` must not be read as covering
    `apps/api-old/x`. A string `startswith` would get that wrong.
    """
    target = (rel_path or ".").replace("\\", "/").strip("/")
    if target in ("", "."):
        target_parts: tuple[str, ...] = ()
    else:
        target_parts = tuple(p for p in target.split("/") if p not in ("", "."))

    best: StackHit | None = None
    best_depth = -1
    for hit in profile.hits:
        if hit.path == ".":
            parts: tuple[str, ...] = ()
        else:
            parts = tuple(p for p in hit.path.split("/") if p)
        if parts == target_parts[: len(parts)]:
            if len(parts) > best_depth:
                best = hit
                best_depth = len(parts)
    return best
