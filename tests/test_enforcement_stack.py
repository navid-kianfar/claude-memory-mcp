"""The lead brief names the repo's own specialists - every turn, in one line.

The user, 2026-09-13: "for a nodejs project a node-agent must be run not a backend
agent which is a general thing." The per-turn line and the session-start intro
are what the lead reads before it picks an agent, so they are what this file
holds to account: for each state of the repo (nothing detected, one stack,
several, a specialist that is not installed), the text says the right name, and
the per-turn line stays ONE line under 400 characters however the roster and the
paths are shaped.

The exact per-turn text was measured and decided (DECISIONS.md section B); the
verbatim assertions below are that decision, not an implementation detail.
"""

from __future__ import annotations

import json
import os

import pytest

from memory_mcp import enforcement
from memory_mcp import setup as setup_mod
from memory_mcp.container import container
from memory_mcp.services import stack_detect as sd


#: The 15 agents installed on the machine the text was measured against.
MEASURED_ROSTER = [
    "app", "backend", "designer", "devops", "docs", "dotnet", "frontend", "go",
    "kotlin", "nodejs", "python", "react", "reviewer", "rust", "test",
]


@pytest.fixture(autouse=True)
def _clean_memo():
    sd.clear_cache()
    yield
    sd.clear_cache()


def _roster(directory, names):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / f"{name}.md").write_text(
            f"---\nname: {name}\ndescription: The {name} agent.\n---\nbody\n"
        )
    return directory


@pytest.fixture
def roster(tmp_path, monkeypatch):
    """Install a synthetic roster; returns a function that (re)writes it."""
    directory = tmp_path / "agents"

    def install(names=MEASURED_ROSTER):
        if directory.exists():
            for path in directory.glob("*.md"):
                path.unlink()
        _roster(directory, names)
        monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", directory)
        return directory

    install()
    return install


@pytest.fixture
def repo(tmp_path):
    """A repo root that is not tmp_path itself: tmp_path holds the registry, and
    a repo containing it would invalidate its own cached profile on every write."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def _files(root, files):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def _pkg(**deps):
    return json.dumps({"name": "x", "dependencies": deps})


PYPROJECT = "[project]\nname = 'x'\n"
VITE_REACT = {
    "frontend/package.json": _pkg(react="18", **{"react-dom": "18"}),
    "frontend/vite.config.ts": "export default {}",
}


def _line(repo=None, **kw):
    return enforcement.agent_team_line(cwd=str(repo) if repo else None, **kw)


def _intro(repo=None):
    return enforcement.agent_team_intro(cwd=str(repo) if repo else None)


# ------------------------------------------------------------------ per-turn line


class TestTheMeasuredText:
    """Verbatim, against the roster it was measured with."""

    def test_state_1_nothing_detected(self, roster, repo):
        expected = (
            "[Agent team] You are the technical lead: you plan, brief and integrate; "
            "specialists implement. Available: app, backend, designer, devops, docs, "
            "dotnet, frontend, go, kotlin, nodejs, python, react, reviewer, rust, "
            "test. Dispatch the stack's own expert over generic backend/frontend; "
            "designer before UI; test before a commit. Name the agent type in each "
            "dispatch description."
        )
        assert _line() == expected
        assert _line(repo) == expected  # a repo with no marker at all
        assert len(expected) == 373

    def test_state_2_one_stack(self, roster, repo):
        _files(repo, {"pyproject.toml": PYPROJECT})

        line = _line(repo)

        assert line == (
            "[Agent team] You are the technical lead: you plan, brief and integrate; "
            "specialists implement. This repo: .=python - dispatch those, never "
            "generic backend/frontend. Also: designer (before UI), test (before a "
            "commit), reviewer, devops, docs. Name the agent type in each dispatch "
            "description."
        )
        assert len(line) == 290

    def test_state_3_this_repos_own_shape(self, roster, repo):
        _files(repo, {"pyproject.toml": PYPROJECT, **VITE_REACT})

        line = _line(repo)

        assert "This repo: .=python; frontend/=react - dispatch those" in line
        assert len(line) == 307

    def test_state_3_three_hits_and_overflow(self, roster, repo):
        _files(repo, {
            "pyproject.toml": PYPROJECT,
            "apps/api/package.json": _pkg(**{"@nestjs/core": "10"}),
            "apps/web/package.json": _pkg(react="18"),
            "apps/web/vite.config.ts": "",
            "apps/worker/package.json": _pkg(express="4"),
            "apps/zz/go.mod": "module zz\n",
        })

        line = _line(repo)

        assert (
            "This repo: .=python; apps/api/=nodejs; apps/web/=react "
            "(+2 more, see session intro) - dispatch those"
        ) in line
        assert len(line) == 354

    def test_the_counts_the_brief_quotes_are_what_ships(self, roster, repo):
        """The escalated line, both variants, at the lengths DECISIONS measured."""
        roled = enforcement._escalated_line(enforcement.Escalation(
            role="python", task_id="t", edited=12,
            task_title="Route a task to the work package its path is bound to, "
                       "not the default",
        ))
        unroled = enforcement._escalated_line(enforcement.Escalation(
            role="python", task_id="t", edited=4, task_title="Fix the flusher",
            detected_prefix="src/",
        ))

        assert roled.startswith(
            "[Agent team] STOP: you have edited 12 source files this session on task "
            "'Route a task to the work package its path is bound to, no...' "
            "(role python) without dispatching `python`, who owns that work."
        )
        assert len(roled) == 348
        assert "(role unset; detected python for src/)" in unroled
        assert len(unroled) == 327


class TestTheStackLine:
    def test_a_nextjs_repo_is_nodejs_with_react_on_top(self, roster, repo):
        _files(repo, {"package.json": _pkg(next="14", react="18")})

        assert "This repo: .=nodejs+react - dispatch those" in _line(repo)

    def test_a_specialist_that_is_not_installed_says_so(self, roster, repo):
        roster([n for n in MEASURED_ROSTER if n != "go"])
        _files(repo, {"go.mod": "module x\n"})

        assert "This repo: .=go (not installed; use backend) - dispatch" in _line(repo)

    def test_a_repo_only_the_generic_role_covers_gets_the_roster(self, roster, repo):
        """A Vue app's owner IS frontend - saying "never generic" would be wrong."""
        _files(repo, {"package.json": _pkg(vue="3")})

        line = _line(repo)

        assert "This repo:" not in line
        assert "Available: app, backend" in line

    def test_a_tooling_only_root_is_not_named_beside_real_modules(self, roster, repo):
        _files(repo, {
            "package.json": _pkg(prettier="3"),
            "apps/api/package.json": _pkg(**{"@nestjs/core": "10"}),
        })

        line = _line(repo)

        assert "This repo: apps/api/=nodejs - dispatch" in line
        assert ".=nodejs" not in line

    def test_a_path_longer_than_40_characters_keeps_its_tail(self, roster, repo):
        deep = "services/a-very-long-service-directory-name"
        _files(repo, {f"{deep}/go.mod": "module x\n"})

        line = _line(repo)

        assert f"…/{(deep + '/')[-40:]}=go" in line

    def test_three_long_paths_shrink_to_two(self, roster, repo):
        """Measured: three long-path hits render 405 characters. The builder
        drops to two and folds the rest into the overflow count."""
        _files(repo, {
            "services/identity-gateway/Gateway.csproj": "<Project/>",
            "apps/customer-portal/package.json": _pkg(react="18"),
            "apps/customer-portal/vite.config.ts": "",
            "packages/shared-kernel/package.json": _pkg(express="4"),
            "services/billing-engine/go.mod": "module b\n",
            "services/notification-hub/Cargo.toml": "[package]\n",
            "packages/design-tokens/package.json": _pkg(**{"@nestjs/core": "1"}),
            "apps/admin-console/package.json": _pkg(**{"@nestjs/core": "1"}),
        })

        line = _line(repo)
        shown = line.split("This repo: ", 1)[1].split(" (+", 1)[0]

        assert shown.count("=") == 2, line
        assert "(+5 more, see session intro)" in line
        assert len(line) <= enforcement._LINE_BUDGET

    def test_the_also_list_is_filtered_to_what_is_installed(self, roster, repo):
        roster(["python", "backend", "test"])
        _files(repo, {"pyproject.toml": PYPROJECT})

        line = _line(repo)

        assert "Also: test (before a commit)." in line
        assert "designer" not in line


def _all_states(roster, repo, names):
    """Every reachable state of the per-turn line for one roster."""
    roster(names)
    lines = {"roster": _line()}
    _files(repo, {"pyproject.toml": PYPROJECT, **VITE_REACT})
    sd.clear_cache()
    lines["two hits"] = _line(repo)
    _files(repo, {
        "apps/customer-portal/package.json": _pkg(react="18"),
        "apps/customer-portal/vite.config.ts": "",
        "services/identity-gateway/Gateway.csproj": "<Project/>",
        "packages/shared-kernel/package.json": _pkg(express="4"),
        "services/a-very-long-service-directory-name-indeed/go.mod": "module x\n",
    })
    sd.clear_cache()
    lines["many hits"] = _line(repo)
    lines["escalated"] = enforcement._escalated_line(enforcement.Escalation(
        role="react-native", task_id="t", edited=120, task_title="x" * 120,
        detected_prefix="…/" + "p" * 40,
    ))
    return lines


SIXTEEN = MEASURED_ROSTER + ["react-native"]
LONG_NAMES = [f"custom-agent-{i:02d}" for i in range(30)]


class TestEveryStateStaysOneLineUnder400:
    @pytest.mark.parametrize("names", [MEASURED_ROSTER, SIXTEEN, LONG_NAMES],
                             ids=["measured-15", "synthetic-16", "30-long-names"])
    def test_every_state(self, roster, repo, names):
        for state, line in _all_states(roster, repo, names).items():
            assert line, state
            assert "\n" not in line, state
            assert len(line) < 400, f"{state}: {len(line)} chars: {line}"
            assert "cheaper path" not in line, state
            assert "worktree-isolated" not in line, state

    def test_the_real_rosters_too(self, monkeypatch, repo, roster):
        """The repo's own definitions (what CI has) and this machine's install."""
        sources = [setup_mod.AGENTS_DIR, enforcement.Path.home() / ".claude" / "agents"]
        checked = 0
        for source in sources:
            if not source.is_dir():
                continue
            monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", source)
            if not enforcement.installed_agents():
                continue
            checked += 1
            _files(repo, {"pyproject.toml": PYPROJECT, **VITE_REACT})
            sd.clear_cache()
            for line in (_line(), _line(repo)):
                assert "\n" not in line
                assert len(line) < 400, f"{source}: {len(line)} chars"
        assert checked, "the repo's agent definitions must be readable"

    def test_a_120_character_title_is_capped(self, roster):
        line = enforcement._escalated_line(enforcement.Escalation(
            role="python", task_id="t", edited=3, task_title="T" * 120,
        ))
        assert f"'{'T' * 57}...'" in line
        assert len(line) < 400

    def test_a_title_with_newlines_stays_one_line(self, roster):
        line = enforcement._escalated_line(enforcement.Escalation(
            role="python", task_id="t", edited=3, task_title="Fix\nthe\r\n flusher",
        ))
        assert "\n" not in line and "\r" not in line
        assert "'Fix the flusher'" in line

    def test_no_agents_means_nothing_in_any_state(self, tmp_path, monkeypatch, repo):
        monkeypatch.setattr(enforcement, "AGENT_TEAM_DIR", tmp_path / "none")
        _files(repo, {"pyproject.toml": PYPROJECT})

        assert _line(repo, slug="x", session_id="s") == ""
        assert _intro(repo) == ""


# ------------------------------------------------------------------------- intro


class TestTheIntro:
    def test_this_repos_stack_block(self, roster, repo):
        _files(repo, {"pyproject.toml": PYPROJECT, **VITE_REACT})

        text = _intro(repo)

        assert "THIS REPO'S STACK, detected from its own markers:" in text
        rows = [r for r in text.splitlines() if r.startswith("  - `")]
        assert rows[0].startswith("  - `.`") and rows[0].endswith("-> dispatch `python`")
        assert "(pyproject.toml)" in rows[0]
        assert rows[1].startswith("  - `frontend/`")
        assert rows[1].endswith("-> dispatch `react`")
        assert "(package.json, vite.config.ts)" in rows[1]
        assert "`kotlin` is SERVER Kotlin; `app` is Android+iOS" in text
        assert "A Next.js repo is `nodejs` first" in text

    def test_the_roster_is_ordered_and_annotated(self, roster, repo):
        _files(repo, {"pyproject.toml": PYPROJECT, **VITE_REACT})

        text = _intro(repo)
        roster_rows = text.split("Available specialists:\n", 1)[1].split("\n\n", 1)[0]
        names = [row[4:].split(":", 1)[0] for row in roster_rows.splitlines()]

        assert names == [
            "python", "react", "designer", "test", "reviewer", "devops", "docs",
            "backend", "frontend", "app, dotnet, go, kotlin, nodejs, rust",
        ]
        rows = dict(zip(names, roster_rows.splitlines()))
        assert rows["python"].endswith("The python agent. [THIS REPO - `.`]")
        assert rows["react"].endswith("[THIS REPO - `frontend/`]")
        assert "[generic fallback" in rows["backend"]
        assert "[generic fallback" in rows["frontend"]
        assert rows["designer"].endswith("The designer agent.")
        assert rows["app, dotnet, go, kotlin, nodejs, rust"].endswith(
            "other stacks' experts, not this repo's"
        )

    def test_the_division_of_work_is_the_contract(self, roster, repo):
        _files(repo, {"pyproject.toml": PYPROJECT})

        text = _intro(repo)

        assert "HOW THE WORK IS DIVIDED - this is the contract, not advice:" in text
        assert "NEVER pass `isolation`" in text
        assert "Implementation is DISPATCHED." in text
        for gone in ("worktree-isolated", "cheaper path", "cheapest way",
                     "HOW TO USE THEM", "asoode"):
            assert gone not in text, gone

    def test_nothing_detected(self, roster, repo):
        text = _intro(repo)

        assert (
            f"THIS REPO'S STACK could not be detected: nothing at depth 2 under "
            f"{repo.resolve()} matched a known marker"
        ) in text
        assert "Read the manifest yourself, or ask" in text
        roster_rows = text.split("Available specialists:\n", 1)[1].split("\n\n", 1)[0]
        assert [r[4:].split(":", 1)[0] for r in roster_rows.splitlines()] == MEASURED_ROSTER
        assert "[generic fallback" not in text
        assert "NEVER pass `isolation`" in text

    def test_no_cwd_is_nothing_detected_too(self, roster):
        assert "could not be detected: nothing at depth 2 under this directory" in _intro()

    def test_a_specialist_that_is_not_installed(self, roster, repo):
        roster([n for n in MEASURED_ROSTER if n != "go"])
        _files(repo, {"go.mod": "module x\n"})

        text = _intro(repo)

        assert (
            "-> go is NOT installed on this machine; use `backend` and tell the user "
            "`go` is missing."
        ) in text
        assert "[THIS REPO - `.`, where no installed specialist applies]" in text

    def test_a_warning_hit_carries_its_warning(self, roster, repo):
        _files(repo, {"web/package.json": _pkg(vue="3")})

        text = _intro(repo)

        row = next(r for r in text.splitlines() if r.startswith("  - `web/`"))
        assert "-> dispatch `frontend` - vue detected; no installed specialist" in row

    def test_a_nextjs_repo(self, roster, repo):
        _files(repo, {"package.json": _pkg(next="14", react="18")})

        text = _intro(repo)

        assert "-> dispatch `nodejs`, with `react` on top for screens and components" in text
        assert "  - nodejs: The nodejs agent. [THIS REPO - `.`]" in text
        assert "  - react: The react agent. [THIS REPO - `.`]" in text

    def test_a_detector_that_explodes_costs_the_stack_block_not_the_session(
        self, roster, repo, monkeypatch,
    ):
        _files(repo, {"pyproject.toml": PYPROJECT})

        def boom(*_a, **_k):
            raise RuntimeError("disk gone")

        monkeypatch.setattr(sd, "profile_for", boom)

        assert "could not be detected" in _intro(repo)
        assert "Available: app, backend" in _line(repo, slug="x", session_id="s")


# ------------------------------------------------------------ the hot path budget


class TestNoNetworkOnTheHotPath:
    def test_the_rules_block_with_a_session_touches_no_network_and_walks_nothing_warm(
        self, roster, repo, monkeypatch,
    ):
        """The per-turn hook runs behind a 2 s timeout on every prompt. Warm, it
        reads the cached profile and the ledgers - it never walks the repo."""
        slug = "hot-path"
        container.project_service.init_project(slug, "Hot", project_path=str(repo))
        _files(repo, {"pyproject.toml": PYPROJECT, **VITE_REACT})
        enforcement.rules_text_for_project(slug, cwd=str(repo), session_id="s1")

        def forbidden(*_a, **_k):
            raise AssertionError("the hook path must not touch the network")

        monkeypatch.setattr("httpx.Client.request", forbidden)
        monkeypatch.setattr("httpx.Client.post", forbidden)
        scanned: list[str] = []
        real_scandir = os.scandir

        def recording_scandir(path=".", *args, **kwargs):
            scanned.append(os.fspath(path))
            return real_scandir(path, *args, **kwargs)

        monkeypatch.setattr(os, "scandir", recording_scandir)

        block = enforcement.rules_text_for_project(slug, cwd=str(repo), session_id="s1")

        assert "This repo: .=python; frontend/=react" in block
        root = str(repo.resolve())
        walked = [p for p in scanned if os.path.realpath(p).startswith(root)]
        assert walked == [], f"the warm path walked the repo: {walked}"
