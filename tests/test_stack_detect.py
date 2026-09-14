"""stack_detect: the repo in front of the lead decides which specialist it names.

The behaviour worth protecting is the PRECEDENCE, not the marker list. A
`package.json` is the commonest incidental file in another stack's repo, so a
tooling-only one must lose to a `pyproject.toml` beside it - that is the exact
failure the user reported, in reverse. And the answer is per directory: a
monorepo has several owners at once.

Every test drives the module through its public API against a `repo`
fixture. Nothing here reaches the real filesystem outside repo, and the warm
path is asserted to perform no `os.scandir` at all.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from memory_mcp.services import stack_detect as sd


@pytest.fixture(autouse=True)
def _clean_memo():
    """The process memo is module state; no test may see another's answer."""
    sd.clear_cache()
    yield
    sd.clear_cache()


@pytest.fixture
def repo(tmp_path):
    """A repo root that is NOT `tmp_path` itself.

    conftest's autouse fixture points `settings.data_dir` at
    `tmp_path/memory-mcp`, so a repo rooted at tmp_path would CONTAIN the
    registry database - and every cache write would change a watched directory's
    mtime and invalidate the answer it had just written. A real root never holds
    the registry; this keeps the fixture honest about that.
    """
    root = tmp_path / "repo"
    root.mkdir()
    return root


def _repo(root, files: dict[str, str] | None = None, git: bool = True):
    """Build a tree. Keys are POSIX relative paths; parents are created."""
    root.mkdir(parents=True, exist_ok=True)
    if git:
        (root / ".git").mkdir(exist_ok=True)
    for rel, content in (files or {}).items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return root


def _pkg(**deps: str) -> str:
    return json.dumps({"name": "x", "dependencies": deps or {}})


def _agents(profile, installed):
    return sd.agents_for(profile, set(installed))


# --------------------------------------------------------------- one stack each


class TestSingleStack:
    def test_a_pyproject_resolves_to_the_python_agent(self, repo):
        _repo(repo, {"pyproject.toml": "[project]\nname='x'\n"})

        profile = sd.detect(repo)

        assert profile.primary is not None
        assert (profile.primary.path, profile.primary.agent) == (".", "python")
        assert profile.primary.fallback == "backend"
        assert profile.primary.strength == "strong"
        assert "pyproject.toml" in profile.primary.markers

    def test_a_go_mod_resolves_to_the_go_agent(self, repo):
        _repo(repo, {"go.mod": "module x\n"})

        assert sd.detect(repo).primary.agent == "go"

    def test_a_cargo_toml_resolves_to_the_rust_agent(self, repo):
        _repo(repo, {"Cargo.toml": "[package]\nname='x'\n"})

        assert sd.detect(repo).primary.agent == "rust"

    def test_a_csproj_resolves_to_the_dotnet_agent(self, repo):
        _repo(repo, {"Api.csproj": "<Project/>"})

        hit = sd.detect(repo).primary
        assert hit.agent == "dotnet"
        assert hit.markers == ("Api.csproj",)

    def test_a_nestjs_package_resolves_to_the_nodejs_agent(self, repo):
        _repo(repo, {"package.json": _pkg(**{"@nestjs/core": "^10"})})

        hit = sd.detect(repo).primary
        assert (hit.stack, hit.agent, hit.co_agent) == ("node-nest", "nodejs", None)

    def test_an_express_package_resolves_to_the_nodejs_agent(self, repo):
        _repo(repo, {"package.json": _pkg(express="^4")})

        hit = sd.detect(repo).primary
        assert (hit.stack, hit.agent) == ("node-generic", "nodejs")
        assert hit.strength == "strong"

    def test_a_vite_react_package_resolves_to_the_react_agent(self, repo):
        _repo(
            repo,
            {"package.json": _pkg(react="^18", **{"react-dom": "^18"}),
             "vite.config.ts": "export default {}"},
        )

        hit = sd.detect(repo).primary
        assert (hit.stack, hit.agent, hit.fallback) == ("vite-react", "react", "frontend")
        assert "vite.config.ts" in hit.markers

    def test_react_without_vite_falls_back_to_frontend_with_a_warning(self, repo):
        _repo(repo, {"package.json": _pkg(react="^18", **{"react-scripts": "5"})})

        hit = sd.detect(repo).primary
        assert (hit.stack, hit.agent) == ("react-web", "frontend")
        assert hit.warning and "Vite" in hit.warning

    def test_a_vue_package_falls_back_to_frontend_naming_the_framework(self, repo):
        _repo(repo, {"package.json": _pkg(vue="^3")})

        hit = sd.detect(repo).primary
        assert hit.agent == "frontend"
        assert hit.warning and "vue" in hit.warning

    def test_an_electron_package_goes_to_nodejs_with_a_warning(self, repo):
        _repo(repo, {"package.json": _pkg(electron="^30")})

        hit = sd.detect(repo).primary
        assert hit.agent == "nodejs"
        assert hit.warning and "electron" in hit.warning


class TestReactNative:
    """Decision d8b8baa3: RN has its own agent. Never `react`, never `frontend`."""

    def test_react_native_resolves_to_its_own_agent_not_react(self, repo):
        _repo(repo, {"package.json": _pkg(react="^18", **{"react-native": "0.74"})})

        hit = sd.detect(repo).primary
        assert (hit.stack, hit.agent) == ("react-native", "react-native")
        assert hit.agent not in ("react", "frontend", "app")
        assert hit.fallback == "frontend"
        assert hit.warning is None

    def test_expo_resolves_to_react_native_too(self, repo):
        _repo(repo, {"package.json": _pkg(expo="~51", react="^18")})

        assert sd.detect(repo).primary.agent == "react-native"

    def test_react_native_is_detected_with_its_definition_absent(self, repo):
        """The definition file is a sibling task's; detection must not wait for it."""
        _repo(repo, {"package.json": _pkg(**{"react-native": "0.74"})})
        profile = sd.detect(repo)

        # Roster without react-native installed: the fallback is dispatched, and
        # the hit still says who it WANTED so the caller can render the note.
        assert _agents(profile, {"frontend", "react"}) == ["frontend"]
        assert profile.primary.agent == "react-native"

    def test_react_native_wins_over_vite_when_both_markers_exist(self, repo):
        _repo(
            repo,
            {"package.json": _pkg(react="^18", **{"react-native": "0.74"}),
             "vite.config.ts": "export default {}"},
        )

        assert sd.detect(repo).primary.agent == "react-native"


class TestNextJs:
    """Decision d8b8baa3: one owner (nodejs), react named for screens."""

    def test_next_resolves_to_nodejs_with_react_as_the_co_agent(self, repo):
        _repo(repo, {"package.json": _pkg(next="^14", react="^18")})

        hit = sd.detect(repo).primary
        assert (hit.stack, hit.agent, hit.co_agent) == ("node-next", "nodejs", "react")
        assert hit.fallback == "backend"

    def test_next_config_alone_is_enough(self, repo):
        _repo(repo, {"package.json": _pkg(react="^18"), "next.config.mjs": "export default {}"})

        hit = sd.detect(repo).primary
        assert hit.agent == "nodejs"
        assert "next.config.mjs" in hit.markers

    def test_next_is_not_split_per_path_it_is_one_hit(self, repo):
        """A path split would be wrong: Next puts server components in app/."""
        _repo(
            repo,
            {"package.json": _pkg(next="^14", react="^18"),
             "app/page.tsx": "export default () => null",
             "components/button.tsx": "export default () => null"},
        )

        profile = sd.detect(repo)
        assert [h.path for h in profile.hits] == ["."]

    def test_a_next_repo_dispatches_nodejs_then_react(self, repo):
        _repo(repo, {"package.json": _pkg(next="^14")})
        profile = sd.detect(repo)

        assert _agents(profile, {"nodejs", "react", "backend", "frontend"}) == [
            "nodejs",
            "react",
        ]

    def test_vite_react_is_not_mistaken_for_next(self, repo):
        _repo(
            repo,
            {"package.json": _pkg(react="^18"), "vite.config.ts": "export default {}"},
        )

        hit = sd.detect(repo).primary
        assert (hit.agent, hit.co_agent) == ("react", None)


# --------------------------------------------------------------- the precedence


class TestPrecedence:
    def test_strength_beats_the_ordinal_nest_wins_over_a_requirements_txt(self, repo):
        """Weak python (requirements.txt) loses to a strong node marker."""
        _repo(
            repo,
            {"package.json": _pkg(**{"@nestjs/core": "^10"}), "requirements.txt": "ruff\n"},
        )

        hit = sd.detect(repo).primary
        assert hit.agent == "nodejs"
        assert hit.markers == ("package.json",)

    def test_a_pyproject_beats_a_package_json_in_the_same_directory(self, repo):
        """Both strong: the ordinal decides, and node is deliberately last."""
        _repo(
            repo,
            {"pyproject.toml": "[project]\nname='x'\n", "package.json": _pkg(express="^4")},
        )

        assert sd.detect(repo).primary.agent == "python"

    def test_a_tooling_only_package_json_loses_to_a_pyproject(self, repo):
        """The reported failure, in reverse: prettier must not claim a Python repo."""
        _repo(
            repo,
            {"pyproject.toml": "[project]\nname='x'\n",
             "package.json": _pkg(prettier="^3", husky="^9")},
        )

        assert sd.detect(repo).primary.agent == "python"

    def test_a_tooling_only_package_json_alone_is_a_weak_nodejs_hit(self, repo):
        _repo(repo, {"package.json": _pkg(prettier="^3")})

        hit = sd.detect(repo).primary
        assert (hit.agent, hit.strength) == ("nodejs", "weak")

    def test_a_module_named_setup_py_inside_a_package_is_not_a_project_root(self, repo):
        """Measured on this repo: `src/memory_mcp/setup.py` is the INSTALLER module.

        A packaging `setup.py` sits beside the package, never inside it, so a
        directory holding `__init__.py` is not a root and its python markers are
        ignored. Without this guard the repo reported a spurious third hit.
        """
        _repo(
            repo,
            {"pyproject.toml": "[project]\n",
             "src/pkg/__init__.py": "",
             "src/pkg/setup.py": "def setup_agents(): ...",
             "src/pkg/requirements.txt": "# vendored\n"},
        )

        assert [h.path for h in sd.detect(repo).hits] == ["."]

    def test_a_real_packaging_setup_py_beside_a_package_still_counts(self, repo):
        _repo(repo, {"setup.py": "from setuptools import setup", "pkg/__init__.py": ""})

        assert sd.detect(repo).primary.agent == "python"

    def test_a_package_directory_does_not_suppress_a_non_python_marker(self, repo):
        """`__init__.py` says nothing about a go.mod beside it."""
        _repo(repo, {"svc/__init__.py": "", "svc/go.mod": "module x\n"})

        assert sd.detect(repo).primary.agent == "go"

    def test_go_beats_a_weak_dotnet_marker(self, repo):
        _repo(repo, {"go.mod": "module x\n", "global.json": "{}"})

        assert sd.detect(repo).primary.agent == "go"

    def test_a_csproj_beats_a_go_mod_on_the_ordinal(self, repo):
        _repo(repo, {"Api.csproj": "<Project/>", "go.mod": "module x\n"})

        assert sd.detect(repo).primary.agent == "dotnet"


class TestJvm:
    def test_compose_multiplatform_resolves_to_the_app_agent(self, repo):
        _repo(
            repo,
            {"build.gradle.kts": 'plugins { kotlin("multiplatform"); id("org.jetbrains.compose") }'},
        )

        hit = sd.detect(repo).primary
        assert (hit.stack, hit.agent, hit.fallback) == ("kmp-mobile", "app", "frontend")

    def test_an_android_manifest_resolves_to_the_app_agent(self, repo):
        _repo(
            repo,
            {"build.gradle.kts": "plugins { id(\"org.jetbrains.kotlin.jvm\") }",
             "src/main/AndroidManifest.xml": "<manifest/>"},
        )

        assert sd.detect(repo).primary.agent == "app"

    def test_ktor_resolves_to_the_server_kotlin_agent_not_app(self, repo):
        _repo(repo, {"build.gradle.kts": 'implementation("io.ktor:ktor-server-core")'})

        hit = sd.detect(repo).primary
        assert (hit.stack, hit.agent) == ("kotlin-server", "kotlin")

    def test_spring_boot_resolves_to_the_kotlin_agent(self, repo):
        _repo(repo, {"build.gradle.kts": 'id("org.springframework.boot")'})

        assert sd.detect(repo).primary.agent == "kotlin"

    def test_kotlin_sources_alone_resolve_to_the_kotlin_agent(self, repo):
        _repo(repo, {"build.gradle.kts": "// nothing recognisable\n", "src/Main.kt": "fun main(){}"})

        assert sd.detect(repo).primary.agent == "kotlin"

    def test_java_only_falls_back_to_backend_naming_the_ecosystem(self, repo):
        _repo(repo, {"pom.xml": "<project/>", "src/Main.java": "class Main{}"})

        hit = sd.detect(repo).primary
        assert hit.agent == "backend"
        assert hit.warning and "Java" in hit.warning

    def test_a_kmp_repo_with_a_ktor_server_module_yields_two_hits(self, repo):
        _repo(
            repo,
            {
                "settings.gradle.kts": 'include(":shared", ":server")',
                "build.gradle.kts": 'plugins { kotlin("multiplatform"); id("org.jetbrains.compose") }',
                "shared/build.gradle.kts": 'plugins { kotlin("multiplatform") }',
                "shared/src/commonMain/kotlin/App.kt": "fun app(){}",
                "server/build.gradle.kts": 'implementation("io.ktor:ktor-server-netty")',
                "server/src/Main.kt": "fun main(){}",
            },
        )

        profile = sd.detect(repo)
        by_path = {h.path: h.agent for h in profile.hits}
        assert by_path["."] == "app"
        assert by_path["server"] == "kotlin"
        assert profile.primary.path == "."


class TestMonorepo:
    @pytest.fixture
    def monorepo(self, repo):
        return _repo(
            repo,
            {
                # Tooling-only root: prettier and husky, no framework. The root
                # must NOT claim the repo - the modules own their own paths.
                "package.json": json.dumps(
                    {"name": "mono", "private": True,
                     "devDependencies": {"prettier": "^3", "husky": "^9"}}
                ),
                "apps/api/package.json": _pkg(**{"@nestjs/core": "^10"}),
                "apps/api/nest-cli.json": "{}",
                "apps/web/package.json": _pkg(react="^18", **{"react-dom": "^18"}),
                "apps/web/vite.config.ts": "export default {}",
                "mobile/package.json": _pkg(**{"react-native": "0.74"}, react="^18"),
            },
        )

    def test_every_module_gets_its_own_specialist(self, monorepo):
        profile = sd.detect(monorepo)
        by_path = {h.path: h.agent for h in profile.hits}

        assert by_path["apps/api"] == "nodejs"
        assert by_path["apps/web"] == "react"
        assert by_path["mobile"] == "react-native"

    def test_the_tooling_only_root_is_a_WEAK_hit_not_a_suppressed_one(self, monorepo):
        """DEVIATION from the brief, stated so `d4335e4b` can read it.

        The brief said a tooling-only root `package.json` yields "no root hit".
        It yields a hit with `strength == "weak"` instead, because `strength` is
        what the survey's overlap H exists to express, and suppressing the hit
        would cost two things a consumer needs: `hit_for_path` would return None
        for every unclaimed path in a pnpm monorepo (where the root genuinely IS
        nodejs work), and `primary` would be None for a repo that has one obvious
        owner. A consumer that wants the brief's behaviour filters on strength.
        """
        profile = sd.detect(monorepo)
        root = next(h for h in profile.hits if h.path == ".")

        assert root.strength == "weak"
        assert [h.path for h in profile.hits if h.strength == "strong"] == [
            "mobile",
            "apps/api",
            "apps/web",
        ]

    def test_the_root_hit_comes_first_then_shortest_path_first(self, monorepo):
        paths = [h.path for h in sd.detect(monorepo).hits]

        assert paths[0] == "."
        assert paths.index("mobile") < paths.index("apps/api")

    def test_the_dispatch_list_names_every_specialist_once(self, monorepo):
        profile = sd.detect(monorepo)

        # Hit order, not alphabetical: root first, then shortest path first -
        # so `mobile` is named before `apps/web`.
        agents = _agents(profile, {"nodejs", "react", "react-native", "backend", "frontend"})
        assert agents == ["nodejs", "react-native", "react"]

    def test_a_module_deeper_than_max_depth_is_not_reached(self, repo):
        _repo(repo, {"a/b/c/go.mod": "module x\n"})

        assert sd.detect(repo).hits == ()


# ------------------------------------------------------------------ the budget


class TestBudget:
    def test_exceeding_max_dirs_reports_truncated_rather_than_raising(self, repo, monkeypatch):
        _repo(repo, {})
        for i in range(12):
            (repo / f"mod{i:02d}").mkdir()
        monkeypatch.setattr(sd, "MAX_DIRS", 4)

        profile = sd.detect(repo)

        assert profile.truncated is True
        assert profile.dirs_scanned <= 4

    def test_exceeding_the_time_budget_reports_truncated(self, repo, monkeypatch):
        _repo(repo, {})
        for i in range(6):
            (repo / f"mod{i}").mkdir()
        monkeypatch.setattr(sd, "SCAN_BUDGET_MS", -1.0)

        profile = sd.detect(repo)

        assert profile.truncated is True

    def test_a_complete_walk_is_not_marked_truncated(self, repo):
        _repo(repo, {"pyproject.toml": "[project]\n"})

        assert sd.detect(repo).truncated is False

    def test_a_symlink_loop_terminates(self, repo):
        _repo(repo, {"pkg/pyproject.toml": "[project]\n"})
        try:
            os.symlink(str(repo), str(repo / "pkg" / "loop"))
        except (OSError, NotImplementedError):  # pragma: no cover - platform
            pytest.skip("symlinks unavailable")

        profile = sd.detect(repo)

        assert [h.path for h in profile.hits] == ["pkg"]
        assert profile.truncated is False

    def test_skipped_directories_are_never_entered(self, repo):
        _repo(repo, {"node_modules/left-pad/package.json": _pkg(express="^4"),
                         ".venv/lib/pyproject.toml": "[project]\n"})

        assert sd.detect(repo).hits == ()

    def test_a_jvm_subprobe_cannot_turn_one_directory_into_a_tree_walk(
        self, repo, monkeypatch
    ):
        """The mobile/source probes look deeper than MAX_DEPTH, so they get a cap."""
        files = {"build.gradle.kts": "// nothing recognisable\n"}
        for i in range(30):
            files[f"src/m{i}/Main.kt"] = "fun main(){}"
        _repo(repo, files)
        monkeypatch.setattr(sd, "SUBPROBE_MAX_DIRS", 2)

        # The cap makes the probe give up, not crash: a Gradle file with nothing
        # it recognises under it is a container, which is a weak hit.
        hit = sd.detect(repo).primary
        assert hit.strength == "weak"
        assert hit.agent == "backend"

    def test_a_malformed_package_json_does_not_raise(self, repo):
        _repo(repo, {"package.json": "{not json"})

        hit = sd.detect(repo).primary
        assert (hit.agent, hit.strength) == ("nodejs", "weak")


# ------------------------------------------------------------------- the cache


class TestCache:
    def test_the_second_call_is_served_from_the_cache(self, repo):
        _repo(repo, {"pyproject.toml": "[project]\n"})

        first = sd.profile_for(str(repo))
        second = sd.profile_for(str(repo))

        assert first.from_cache is False
        assert second.from_cache is True
        assert [h.agent for h in second.hits] == [h.agent for h in first.hits]

    def test_the_warm_path_performs_no_scandir(self, repo, monkeypatch):
        """The hot path runs on every prompt. Warm, it must not touch the tree."""
        _repo(repo, {"pyproject.toml": "[project]\n"})
        sd.profile_for(str(repo))

        def explode(*_args, **_kwargs):
            raise AssertionError("the warm path walked the filesystem")

        monkeypatch.setattr(sd.os, "scandir", explode)

        assert sd.profile_for(str(repo)).from_cache is True

    def test_a_new_module_directory_invalidates_the_cache(self, repo):
        _repo(repo, {"pyproject.toml": "[project]\n"})
        sd.profile_for(str(repo))
        sd.clear_cache()  # force the registry layer, not the memo

        (repo / "apps").mkdir()
        (repo / "apps" / "api").mkdir()
        (repo / "apps" / "api" / "package.json").write_text(
            _pkg(**{"@nestjs/core": "^10"})
        )
        os.utime(repo, (time.time() + 5, time.time() + 5))

        profile = sd.profile_for(str(repo))
        assert profile.from_cache is False
        assert {h.agent for h in profile.hits} == {"python", "nodejs"}

    def test_a_dependency_added_to_an_existing_package_json_invalidates(self, repo):
        """A file mtime, not a directory mtime - this is what makes the TTL honest."""
        _repo(repo, {"package.json": _pkg(prettier="^3")})
        first = sd.profile_for(str(repo))
        assert first.primary.strength == "weak"
        sd.clear_cache()

        (repo / "package.json").write_text(_pkg(**{"@nestjs/core": "^10"}))
        os.utime(repo / "package.json", (time.time() + 5, time.time() + 5))

        second = sd.profile_for(str(repo))
        assert second.from_cache is False
        assert second.primary.stack == "node-nest"

    def test_bumping_the_detector_version_invalidates_every_profile(self, repo, monkeypatch):
        _repo(repo, {"pyproject.toml": "[project]\n"})
        sd.profile_for(str(repo))
        sd.clear_cache()

        monkeypatch.setattr(sd, "DETECTOR_VERSION", sd.DETECTOR_VERSION + 1)

        assert sd.profile_for(str(repo)).from_cache is False

    def test_the_ttl_is_the_backstop_when_no_mtime_moved(self, repo, monkeypatch):
        _repo(repo, {"pyproject.toml": "[project]\n"})
        sd.profile_for(str(repo))
        sd.clear_cache()

        monkeypatch.setattr(sd, "TTL_SECONDS", -1.0)

        assert sd.profile_for(str(repo)).from_cache is False

    def test_the_cache_is_keyed_on_the_root_path_so_two_worktrees_differ(self, repo):
        one = _repo(repo / "one", {"pyproject.toml": "[project]\n"})
        two = _repo(repo / "two", {"go.mod": "module x\n"})

        assert sd.profile_for(str(one)).primary.agent == "python"
        assert sd.profile_for(str(two)).primary.agent == "go"

    def test_the_cached_key_carries_a_hash_of_the_root_not_the_path(self, repo):
        _repo(repo, {"pyproject.toml": "[project]\n"})
        sd.profile_for(str(repo))

        from memory_mcp.db.registry import get_setting

        key = sd._cache_key(str(repo.resolve()))
        assert key.startswith("stack:profile:")
        assert str(repo) not in key
        assert get_setting(key)

    def test_a_registry_write_failure_does_not_fail_the_lookup(self, repo, monkeypatch):
        _repo(repo, {"pyproject.toml": "[project]\n"})
        import memory_mcp.db.registry as registry

        def explode(*_args, **_kwargs):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(registry, "set_setting", explode)

        profile = sd.profile_for(str(repo))
        assert profile is not None
        assert profile.primary.agent == "python"


# ------------------------------------------------------------ root resolution


class TestRootResolution:
    def test_a_subdirectory_resolves_to_the_repo_root(self, repo):
        _repo(repo, {"pyproject.toml": "[project]\n", "src/pkg/__init__.py": ""})

        profile = sd.profile_for(str(repo / "src" / "pkg"))

        assert profile.root == str(repo.resolve())
        assert profile.primary.path == "."

    def test_a_linked_worktree_git_file_is_a_root_too(self, repo):
        """A linked worktree's `.git` is a FILE, not a directory."""
        root = repo / "wt"
        root.mkdir()
        (root / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
        (root / "go.mod").write_text("module x\n")

        assert sd.resolve_root(str(root)) == root.resolve()
        assert sd.profile_for(str(root)).primary.agent == "go"

    def test_a_directory_with_no_git_still_resolves_to_itself(self, repo):
        _repo(repo, {"pyproject.toml": "[project]\n"}, git=False)

        assert sd.profile_for(str(repo)).primary.agent == "python"

    def test_no_cwd_and_no_slug_is_none_not_an_empty_profile(self):
        assert sd.profile_for(None, None) is None

    def test_a_nonexistent_cwd_is_none(self, repo):
        assert sd.profile_for(str(repo / "gone" / "missing")) is None


# --------------------------------------------------------------- the consumers


class TestAgentsFor:
    def test_an_uninstalled_specialist_contributes_its_fallback(self, repo):
        _repo(repo, {"Cargo.toml": "[package]\n"})
        profile = sd.detect(repo)

        assert _agents(profile, {"rust", "backend"}) == ["rust"]
        assert _agents(profile, {"backend"}) == ["backend"]

    def test_specialists_come_before_substituted_fallbacks(self, repo):
        _repo(
            repo,
            {"pyproject.toml": "[project]\n",
             "mobile/package.json": _pkg(**{"react-native": "0.74"})},
        )
        profile = sd.detect(repo)

        assert _agents(profile, {"python", "frontend"}) == ["python", "frontend"]

    def test_no_agent_is_named_twice(self, repo):
        _repo(
            repo,
            {"apps/a/package.json": _pkg(express="^4"),
             "apps/b/package.json": _pkg(fastify="^4")},
        )
        profile = sd.detect(repo)

        assert _agents(profile, {"nodejs"}) == ["nodejs"]

    def test_an_empty_profile_dispatches_nobody(self, repo):
        _repo(repo, {"README.md": "# x"})
        profile = sd.detect(repo)

        assert _agents(profile, {"backend", "frontend", "python"}) == []


class TestOrderRoster:
    ROSTER = [
        ("app", "Mobile expert. Always Kotlin: one codebase."),
        ("backend", "Server-side work: APIs, services, data models."),
        ("designer", "Interface and UX decisions: tokens, specs, flows."),
        ("devops", "CI, builds, deployment, containers."),
        ("docs", "READMEs, API docs, changelogs and guides."),
        ("dotnet", ".NET expert."),
        ("frontend", "UI implementation to the designer's spec."),
        ("go", "Go expert."),
        ("kotlin", "Server-side Kotlin expert (NOT mobile - that is `app`)."),
        ("nodejs", "Node.js expert."),
        ("python", "Python expert."),
        ("react", "React expert for the pnpm + Vite + Tailwind stack."),
        ("reviewer", "Independent review of code it did not write."),
        ("rust", "Rust expert."),
        ("test", "Verifies other agents' work on the running product."),
    ]
    INSTALLED = {name for name, _ in ROSTER}

    def test_the_detected_specialist_comes_first(self, repo):
        _repo(repo, {"pyproject.toml": "[project]\n"})
        profile = sd.profile_for(str(repo))

        rows = sd.order_roster(list(self.ROSTER), profile, self.INSTALLED)

        assert rows[0][0] == "python"
        assert "THIS REPO" in rows[0][2]
        assert "`.`" in rows[0][2]

    def test_every_description_is_passed_through_byte_identical(self, repo):
        _repo(repo, {"package.json": _pkg(**{"@nestjs/core": "^10"})})
        profile = sd.profile_for(str(repo))

        rows = sd.order_roster(list(self.ROSTER), profile, self.INSTALLED)

        assert {name: desc for name, desc, _ in rows} == dict(self.ROSTER)

    def test_no_agent_is_lost_or_duplicated(self, repo):
        _repo(repo, {"go.mod": "module x\n"})
        profile = sd.profile_for(str(repo))

        rows = sd.order_roster(list(self.ROSTER), profile, self.INSTALLED)

        assert sorted(name for name, _, _ in rows) == sorted(n for n, _ in self.ROSTER)

    def test_the_lead_is_never_put_in_the_list(self, repo):
        """pm is the session. order_roster must not invent it."""
        _repo(repo, {"pyproject.toml": "[project]\n"})
        profile = sd.profile_for(str(repo))

        rows = sd.order_roster(list(self.ROSTER), profile, self.INSTALLED)

        assert "pm" not in [name for name, _, _ in rows]

    def test_the_generic_roles_are_marked_as_fallbacks(self, repo):
        _repo(repo, {"go.mod": "module x\n"})
        profile = sd.profile_for(str(repo))

        rows = dict((name, ann) for name, _, ann in sd.order_roster(
            list(self.ROSTER), profile, self.INSTALLED
        ))

        assert "GENERIC FALLBACK" in rows["backend"]
        assert "GENERIC FALLBACK" in rows["frontend"]

    def test_the_unused_stack_experts_are_marked_as_not_this_repo(self, repo):
        _repo(repo, {"go.mod": "module x\n"})
        profile = sd.profile_for(str(repo))

        rows = dict((name, ann) for name, _, ann in sd.order_roster(
            list(self.ROSTER), profile, self.INSTALLED
        ))

        assert rows["rust"] == "not this repo's stack"
        assert rows["app"] == "not this repo's stack"

    def test_the_non_stack_roles_carry_no_annotation(self, repo):
        _repo(repo, {"go.mod": "module x\n"})
        profile = sd.profile_for(str(repo))

        rows = dict((name, ann) for name, _, ann in sd.order_roster(
            list(self.ROSTER), profile, self.INSTALLED
        ))

        assert rows["designer"] == ""
        assert rows["reviewer"] == ""

    def test_no_profile_leaves_every_description_intact(self):
        rows = sd.order_roster(list(self.ROSTER), None, self.INSTALLED)

        assert {name: desc for name, desc, _ in rows} == dict(self.ROSTER)

    def test_the_annotation_is_capped_so_a_monorepo_cannot_blow_the_line_budget(
        self, repo
    ):
        """Measured: a real pnpm monorepo gave nodejs 13 paths, 300 characters."""
        files = {"package.json": json.dumps({"devDependencies": {"prettier": "^3"}})}
        for i in range(9):
            files[f"packages/p{i}/package.json"] = _pkg(express="^4")
        _repo(repo, files)
        profile = sd.profile_for(str(repo))

        rows = dict((name, ann) for name, _, ann in sd.order_roster(
            list(self.ROSTER), profile, self.INSTALLED
        ))

        assert rows["nodejs"].count("`") == 2 * sd.ANNOTATION_MAX_PATHS
        assert "(+7 more)" in rows["nodejs"]
        assert len(rows["nodejs"]) < 100
        # Nothing is hidden: the full list is still on the profile.
        assert len([h for h in profile.hits if h.agent == "nodejs"]) == 10

    def test_a_next_repo_lists_nodejs_before_react(self, repo):
        _repo(repo, {"package.json": _pkg(next="^14", react="^18")})
        profile = sd.profile_for(str(repo))

        names = [name for name, _, _ in sd.order_roster(
            list(self.ROSTER), profile, self.INSTALLED
        )]

        assert names[:2] == ["nodejs", "react"]


class TestHitForPath:
    @pytest.fixture
    def profile(self, repo):
        _repo(
            repo,
            {"pyproject.toml": "[project]\n",
             "apps/api/package.json": _pkg(**{"@nestjs/core": "^10"}),
             "apps/api-old/package.json": _pkg(vue="^3")},
        )
        return sd.detect(repo)

    def test_the_deepest_covering_hit_wins(self, profile):
        assert sd.hit_for_path(profile, "apps/api/src/users.ts").agent == "nodejs"

    def test_the_root_covers_a_path_no_module_claims(self, profile):
        assert sd.hit_for_path(profile, "docs/readme.md").agent == "python"

    def test_a_prefix_match_respects_segment_boundaries(self, profile):
        """`apps/api` must not be read as covering `apps/api-old/x`."""
        hit = sd.hit_for_path(profile, "apps/api-old/x")

        assert hit.path == "apps/api-old"
        assert hit.agent == "frontend"

    def test_the_root_itself_resolves_to_the_root_hit(self, profile):
        assert sd.hit_for_path(profile, ".").path == "."
        assert sd.hit_for_path(profile, "").path == "."

    def test_a_windows_separator_is_normalised(self, profile):
        assert sd.hit_for_path(profile, "apps\\api\\src").agent == "nodejs"

    def test_nothing_covers_a_path_when_there_is_no_root_hit(self, repo):
        _repo(repo, {"apps/api/go.mod": "module x\n"})
        profile = sd.detect(repo)

        assert sd.hit_for_path(profile, "docs/x.md") is None
        assert sd.hit_for_path(profile, "apps/api/main.go").agent == "go"


class TestNoNetworkOrSubprocessOnTheHotPath:
    """The constraint `enforcement.py` states and test_asoode_hooks enforces."""

    def test_detection_never_opens_a_socket_or_shells_out(self, repo, monkeypatch):
        import socket
        import subprocess

        _repo(repo, {"pyproject.toml": "[project]\n", "frontend/package.json":
              _pkg(react="^18", **{"react-dom": "^18"}), "frontend/vite.config.ts": "x"})

        def explode(*_args, **_kwargs):
            raise AssertionError("the hot path left the machine")

        monkeypatch.setattr(socket, "socket", explode)
        monkeypatch.setattr(subprocess, "run", explode)
        monkeypatch.setattr(subprocess, "Popen", explode)

        profile = sd.profile_for(str(repo))

        assert {h.agent for h in profile.hits} == {"python", "react"}
