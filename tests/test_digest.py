"""Digest tests, written around the failure mode that matters: losing a rule.

Every assertion here is about something that must NOT happen - a clause
disappearing into a merge, an unapproved op being written, a revert that only
mostly restores, a second apply doubling a change.
"""

import pytest

from memory_mcp.container import Container
from memory_mcp.models import MemoryCategory, StoreMemoryRequest
from memory_mcp.services.digest import DigestError
from memory_mcp.utils.diff import clause_coverage, split_clauses


@pytest.fixture
def container():
    return Container()


@pytest.fixture
def project(container, project_slug):
    container.project_service.init_project(project_slug, "Test Project", "A test")
    return project_slug


def _store(container, project, category="decision", title="T", content="C",
           tags=None, priority=0):
    return container.memory_service.store(StoreMemoryRequest(
        project=project, category=MemoryCategory(category), title=title,
        content=content, tags=tags or [], priority=priority,
    ))


# The two rules a merge is supposed to unify: same subject, different words,
# and between them four separate obligations.
RULE_A = (
    "Run the full test suite before every commit. A failing suite blocks the "
    "commit, whatever the reason for the failure."
)
RULE_B = (
    "Always run the tests before you commit. Never commit straight to main - "
    "open a branch."
)


class TestAnalyse:
    def test_clean_corpus_reports_no_findings(self, container, project):
        _store(container, project, title="Ports", content="The API listens on 8080.")
        answer = container.digest_service.analyse(project)
        assert answer["totals"]["findings"] == 0
        assert "no signals" in answer["instructions"]
        assert answer["digest_id"]

    def test_empty_project_is_a_clean_noop(self, container, project):
        answer = container.digest_service.analyse(project)
        assert answer["totals"]["memories"] == 0
        assert answer["corpus"] == []
        assert answer["totals"]["findings"] == 0

    def test_near_duplicate_rules_are_reported(self, container, project):
        _store(container, project, "mandatory_rules", "Test before commit", RULE_A)
        _store(container, project, "mandatory_rules", "Always test first", RULE_B)
        signals = container.digest_service.analyse(project)["signals"]
        found = signals["duplicate"] + signals["overlap"]
        assert found, f"expected an overlap signal, got {signals}"
        assert len(found[0]["memory_ids"]) == 2

    def test_a_rule_filed_as_a_decision_is_reported(self, container, project):
        memory = _store(
            container, project, "decision", "Migrations",
            "Every migration must be reversible. Never edit one that shipped.",
        )
        signals = container.digest_service.analyse(project)["signals"]
        assert [s["memory_id"] for s in signals["misfiled_rule"]] == [memory.id]
        assert signals["misfiled_rule"][0]["markers"]

    def test_a_record_filed_as_a_rule_is_reported(self, container, project):
        memory = _store(
            container, project, "mandatory_rules", "The CI outage",
            "On 2026-09-04 the runner ran out of disk. Root cause was the cache.",
            priority=2,
        )
        signals = container.digest_service.analyse(project)["signals"]
        assert [s["memory_id"] for s in signals["misfiled_memory"]] == [memory.id]

    def test_dead_path_reference_is_reported(self, container, project, tmp_path):
        root = tmp_path / "repo"
        (root / "src").mkdir(parents=True)
        (root / "src" / "live.py").write_text("x = 1\n")
        container.project_repo.update_project_path(project, str(root))

        alive = _store(container, project, "architecture", "Live", "See src/live.py.")
        dead = _store(container, project, "architecture", "Dead", "See src/gone.py.")
        signals = container.digest_service.analyse(project)["signals"]
        reported = {s["memory_id"]: s["missing_paths"] for s in signals["stale_reference"]}
        assert reported.get(dead.id) == ["src/gone.py"]
        assert alive.id not in reported

    def test_analysis_writes_nothing_and_reuses_its_open_digest(self, container, project):
        memory = _store(container, project, "mandatory_rules", "R", RULE_A, priority=2)
        first = container.digest_service.analyse(project)
        second = container.digest_service.analyse(project)
        assert first["digest_id"] == second["digest_id"]
        after = container.memory_repo.get_by_id(project, memory.id)
        assert (after.title, after.content, after.status) == (memory.title, memory.content, "active")


class TestContradictions:
    """The case embeddings are blind to: two rules 0.05 apart that say the
    opposite thing. Measured, not assumed - see digest_signals."""

    def test_a_rule_and_its_negation_is_a_contradiction_not_a_duplicate(
        self, container, project
    ):
        _store(container, project, "mandatory_rules", "Deploys",
               "Always deploy on Friday afternoon.", priority=2)
        _store(container, project, "mandatory_rules", "Deploys",
               "Never deploy on Friday afternoon.", priority=2)
        signals = container.digest_service.analyse(project)["signals"]
        assert len(signals["contradiction"]) == 1
        assert signals["duplicate"] == []
        assert "ASK THE USER" in signals["contradiction"][0]["why"]

    def test_an_extra_prohibition_is_not_a_contradiction(self, container, project):
        _store(container, project, "mandatory_rules", "Test before commit", RULE_A,
               priority=2)
        _store(container, project, "mandatory_rules", "Always test first", RULE_B,
               priority=2)
        signals = container.digest_service.analyse(project)["signals"]
        assert signals["contradiction"] == []
        assert signals["overlap"] or signals["duplicate"]

    def test_a_merge_that_inverts_a_rule_is_blocked(self, container, project):
        a = _store(container, project, "mandatory_rules", "Deploys",
                   "Never deploy on Friday afternoon.", priority=2)
        b = _store(container, project, "mandatory_rules", "Fridays",
                   "Friday deploys need a second reviewer.", priority=2)
        answer = container.digest_service.propose(project, [{
            "op": "merge", "memory_ids": [a.id, b.id], "target_id": a.id,
            "reason": "one rule about Friday deploys",
            "title": "Friday deploys",
            "content": (
                "Always deploy on Friday afternoon. Friday deploys need a second "
                "reviewer."
            ),
        }])
        op = answer["operations"][0]
        assert op["coverage"]["polarity_changed"]
        assert answer["blocked"] == [op["op_id"]]
        result = container.digest_service.apply(
            project, answer["digest_id"], approve_all=True,
        )
        assert result["applied"] == 0
        assert "inverted" in result["blocked"][0]["reason"]
        assert container.memory_repo.get_by_id(project, a.id).content == (
            "Never deploy on Friday afternoon."
        )


class TestClauseCoverage:
    def test_a_merge_that_keeps_every_clause_is_clean(self):
        unified = (
            "Run the full test suite before every commit. A failing suite blocks "
            "the commit, whatever the reason. Never commit straight to main - open "
            "a branch."
        )
        assert clause_coverage([RULE_A, RULE_B], unified)["clean"]

    def test_a_merge_that_drops_a_clause_names_it(self):
        lossy = "Run the tests before every commit."
        report = clause_coverage([RULE_A, RULE_B], lossy)
        assert not report["clean"]
        assert any("main" in clause for clause in report["unmatched"])

    def test_bullets_are_separate_clauses(self):
        assert len(split_clauses("- One thing.\n- Another thing.")) == 2


class TestPropose:
    def _two_rules(self, container, project):
        a = _store(container, project, "mandatory_rules", "Test before commit", RULE_A,
                   tags=["ci"], priority=2)
        b = _store(container, project, "mandatory_rules", "Always test first", RULE_B,
                   tags=["git"], priority=3)
        return a, b

    def test_a_lossy_merge_is_flagged_with_the_dropped_text(self, container, project):
        a, b = self._two_rules(container, project)
        answer = container.digest_service.propose(project, [{
            "op": "merge", "memory_ids": [a.id, b.id], "target_id": a.id,
            "reason": "the two say the same thing",
            "title": "Testing before a commit",
            "content": "Run the tests before every commit.",
        }])
        op = answer["operations"][0]
        assert not op["coverage"]["clean"]
        assert any("main" in clause for clause in op["coverage"]["unmatched"])
        assert answer["blocked"] == [op["op_id"]]

    def test_a_faithful_merge_is_clean_and_unions_tags_and_priority(self, container, project):
        a, b = self._two_rules(container, project)
        answer = container.digest_service.propose(project, [{
            "op": "merge", "memory_ids": [a.id, b.id], "target_id": a.id,
            "reason": "both are the same rule about committing",
            "title": "Committing",
            "content": (
                "Run the full test suite before every commit. A failing suite "
                "blocks the commit, whatever the reason for the failure. Never "
                "commit straight to main - open a branch."
            ),
        }])
        op = answer["operations"][0]
        assert op["coverage"]["clean"]
        assert "blocked" not in answer
        stored = container.digest_repo.get(project, answer["digest_id"]).ops[0]
        assert set(stored.payload["tags"]) == {"ci", "git"}
        assert stored.payload["priority"] == 3

    def test_an_operation_without_a_reason_is_refused(self, container, project):
        memory = _store(container, project)
        with pytest.raises(DigestError, match="no reason"):
            container.digest_service.propose(project, [
                {"op": "archive", "memory_ids": [memory.id]}
            ])

    def test_there_is_no_delete_operation(self, container, project):
        memory = _store(container, project)
        with pytest.raises(DigestError, match="no delete"):
            container.digest_service.propose(project, [
                {"op": "delete", "memory_ids": [memory.id], "reason": "gone"}
            ])

    def test_archiving_a_rule_needs_a_real_reason(self, container, project):
        rule = _store(container, project, "mandatory_rules", "R", RULE_A, priority=2)
        with pytest.raises(DigestError, match="dropping a RULE"):
            container.digest_service.propose(project, [
                {"op": "archive", "memory_ids": [rule.id], "reason": "old"}
            ])

    def test_a_merge_survivor_must_be_one_of_the_sources(self, container, project):
        a, b = self._two_rules(container, project)
        with pytest.raises(DigestError, match="must be one of"):
            container.digest_service.propose(project, [{
                "op": "merge", "memory_ids": [a.id, b.id], "target_id": "nope",
                "reason": "unify", "title": "T", "content": "C",
            }])

    def test_a_rewrite_that_changes_nothing_is_refused(self, container, project):
        memory = _store(container, project, title="T", content="C")
        with pytest.raises(DigestError, match="changes nothing"):
            container.digest_service.propose(project, [{
                "op": "rewrite", "memory_ids": [memory.id], "reason": "tidy",
                "title": "T", "content": "C",
            }])

    def test_a_rule_cannot_be_demoted_below_priority_two(self, container, project):
        rule = _store(container, project, "mandatory_rules", "R", RULE_A, priority=2)
        with pytest.raises(DigestError, match="below priority 2"):
            container.digest_service.propose(project, [{
                "op": "reprioritize", "memory_ids": [rule.id], "priority": 0,
                "reason": "less important now",
            }])


class TestApply:
    def _proposal(self, container, project):
        keep = _store(container, project, "architecture", "Ports", "The API listens on 8080.")
        doomed = _store(container, project, "reference", "Old dashboard",
                        "The Grafana board at metrics.internal was retired.")
        promote = _store(container, project, "decision", "Migrations",
                         "Every migration must be reversible.")
        answer = container.digest_service.propose(project, [
            {"op": "archive", "memory_ids": [doomed.id],
             "reason": "the dashboard it points at no longer exists anywhere"},
            {"op": "recategorize", "memory_ids": [promote.id],
             "category": "mandatory_rules",
             "reason": "it is a standing instruction, so it should be enforced"},
            {"op": "keep", "memory_ids": [keep.id], "reason": "still accurate"},
        ])
        return answer, keep, doomed, promote

    def test_only_approved_operations_are_written(self, container, project):
        answer, _keep, doomed, promote = self._proposal(container, project)
        archive_op = next(o for o in answer["operations"] if o["op"] == "archive")
        result = container.digest_service.apply(
            project, answer["digest_id"], approve=[archive_op["op_id"]],
        )
        assert result["applied"] == 1
        assert container.memory_repo.get_by_id(project, doomed.id).status == "archived"
        # The recategorize was never approved, so it never happened.
        assert container.memory_repo.get_by_id(project, promote.id).category == (
            MemoryCategory.DECISION
        )
        assert result["state"] == "proposed"

    def test_nothing_approved_writes_nothing(self, container, project):
        answer, _keep, doomed, _promote = self._proposal(container, project)
        result = container.digest_service.apply(project, answer["digest_id"])
        assert result["applied"] == 0
        assert container.memory_repo.get_by_id(project, doomed.id).status == "active"

    def test_promotion_puts_the_memory_in_the_rule_block(self, container, project):
        answer, _keep, _doomed, promote = self._proposal(container, project)
        before = container.rules_service.get_rules(project)
        assert not any(r.id == promote.id for r in before.mandatory_rules)

        op = next(o for o in answer["operations"] if o["op"] == "recategorize")
        container.digest_service.apply(project, answer["digest_id"], approve=[op["op_id"]])

        updated = container.memory_repo.get_by_id(project, promote.id)
        assert updated.category == MemoryCategory.MANDATORY_RULES
        assert updated.priority >= 2
        after = container.rules_service.get_rules(project)
        assert any(r.id == promote.id for r in after.mandatory_rules)

    def test_approve_all_refuses_an_operation_that_drops_a_clause(self, container, project):
        a = _store(container, project, "mandatory_rules", "A", RULE_A, priority=2)
        b = _store(container, project, "mandatory_rules", "B", RULE_B, priority=2)
        answer = container.digest_service.propose(project, [{
            "op": "merge", "memory_ids": [a.id, b.id], "target_id": a.id,
            "reason": "unify the two commit rules",
            "title": "Committing", "content": "Run the tests before every commit.",
        }])
        result = container.digest_service.apply(
            project, answer["digest_id"], approve_all=True,
        )
        assert result["applied"] == 0
        assert result["blocked"] and "clause" in result["blocked"][0]["reason"]
        # Both rules are untouched and still in force.
        assert container.memory_repo.get_by_id(project, b.id).status == "active"
        assert container.memory_repo.get_by_id(project, a.id).content == RULE_A

    def test_an_applied_operation_is_not_applied_twice(self, container, project):
        answer, _keep, doomed, _promote = self._proposal(container, project)
        op = next(o for o in answer["operations"] if o["op"] == "archive")
        container.digest_service.apply(project, answer["digest_id"], approve=[op["op_id"]])
        again = container.digest_service.apply(
            project, answer["digest_id"], approve=[op["op_id"]],
        )
        assert again["results"][0]["status"] == "already applied"
        assert again["applied"] == 0

    def test_an_edit_after_the_proposal_blocks_the_apply(self, container, project):
        from memory_mcp.models import UpdateMemoryRequest

        memory = _store(container, project, "architecture", "Ports", "Listens on 8080.")
        answer = container.digest_service.propose(project, [{
            "op": "rewrite", "memory_ids": [memory.id], "reason": "clearer",
            "title": "Ports", "content": "The API listens on port 8080.",
        }])
        container.memory_service.update(UpdateMemoryRequest(
            project=project, memory_id=memory.id, content="Listens on 9090 now.",
        ))
        op = answer["operations"][0]
        result = container.digest_service.apply(
            project, answer["digest_id"], approve=[op["op_id"]],
        )
        assert result["results"][0]["status"] == "skipped"
        assert "changed after this was proposed" in result["results"][0]["error"]
        assert container.memory_repo.get_by_id(project, memory.id).content == (
            "Listens on 9090 now."
        )

    def test_a_merge_archives_its_sources_and_records_where_they_went(self, container, project):
        a = _store(container, project, "mandatory_rules", "A", RULE_A, priority=2)
        b = _store(container, project, "mandatory_rules", "B", RULE_B, priority=3)
        unified = (
            "Run the full test suite before every commit. A failing suite blocks "
            "the commit, whatever the reason for the failure. Never commit "
            "straight to main - open a branch."
        )
        answer = container.digest_service.propose(project, [{
            "op": "merge", "memory_ids": [a.id, b.id], "target_id": a.id,
            "reason": "one rule about committing, said once",
            "title": "Committing", "content": unified,
        }])
        result = container.digest_service.apply(
            project, answer["digest_id"], approve_all=True,
        )
        assert result["applied"] == 1

        survivor = container.memory_repo.get_by_id(project, a.id)
        folded = container.memory_repo.get_by_id(project, b.id)
        assert survivor.content == unified and survivor.status == "active"
        assert survivor.priority == 3
        assert folded.status == "archived"
        assert folded.metadata["superseded_by"] == a.id
        assert survivor.metadata["merged_from"][0]["memory_id"] == b.id
        rules = container.rules_service.get_rules(project)
        assert [r.id for r in rules.mandatory_rules] == [a.id]

    def test_a_split_keeps_the_original_and_creates_the_rest(self, container, project):
        both = _store(
            container, project, "mandatory_rules", "Commits",
            "Run the full suite before a commit. Never commit straight to main.",
            priority=2,
        )
        answer = container.digest_service.propose(project, [{
            "op": "split", "memory_ids": [both.id],
            "reason": "two separate rules in one memory",
            "parts": [
                {"title": "Test before commit",
                 "content": "Run the full suite before a commit."},
                {"title": "No direct commits to main",
                 "content": "Never commit straight to main."},
            ],
        }])
        assert answer["operations"][0]["coverage"]["clean"]
        container.digest_service.apply(project, answer["digest_id"], approve_all=True)

        first = container.memory_repo.get_by_id(project, both.id)
        assert first.content == "Run the full suite before a commit."
        rules = container.rules_service.get_rules(project)
        assert len(rules.mandatory_rules) == 2


class TestRevert:
    def test_revert_restores_every_field_exactly(self, container, project):
        a = _store(container, project, "mandatory_rules", "A", RULE_A,
                   tags=["ci"], priority=2)
        b = _store(container, project, "mandatory_rules", "B", RULE_B,
                   tags=["git"], priority=3)
        before = {
            m.id: (m.category, m.title, m.content, tuple(m.tags), m.priority, m.status)
            for m in (a, b)
        }
        unified = (
            "Run the full test suite before every commit. A failing suite blocks "
            "the commit, whatever the reason for the failure. Never commit "
            "straight to main - open a branch."
        )
        answer = container.digest_service.propose(project, [{
            "op": "merge", "memory_ids": [a.id, b.id], "target_id": a.id,
            "reason": "unify", "title": "Committing", "content": unified,
        }])
        container.digest_service.apply(project, answer["digest_id"], approve_all=True)
        container.digest_service.revert(project, answer["digest_id"])

        for memory_id, expected in before.items():
            now = container.memory_repo.get_by_id(project, memory_id)
            assert (
                now.category, now.title, now.content, tuple(now.tags),
                now.priority, now.status,
            ) == expected
        rules = container.rules_service.get_rules(project)
        assert len(rules.mandatory_rules) == 2

    def test_revert_archives_what_a_split_created(self, container, project):
        both = _store(
            container, project, "mandatory_rules", "Commits",
            "Run the full suite before a commit. Never commit straight to main.",
            priority=2,
        )
        answer = container.digest_service.propose(project, [{
            "op": "split", "memory_ids": [both.id], "reason": "two rules in one",
            "parts": [
                {"title": "Test first", "content": "Run the full suite before a commit."},
                {"title": "No direct main", "content": "Never commit straight to main."},
            ],
        }])
        container.digest_service.apply(project, answer["digest_id"], approve_all=True)
        created = [
            m.id for m in container.memory_repo.corpus(project) if m.id != both.id
        ]
        assert created

        result = container.digest_service.revert(project, answer["digest_id"])
        assert set(result["memories_archived"]) == set(created)
        restored = container.memory_repo.get_by_id(project, both.id)
        assert restored.content == (
            "Run the full suite before a commit. Never commit straight to main."
        )
        for memory_id in created:
            # Archived, never destroyed.
            assert container.memory_repo.get_by_id(project, memory_id).status == "archived"

    def test_revert_needs_something_applied(self, container, project):
        memory = _store(container, project)
        answer = container.digest_service.propose(project, [
            {"op": "keep", "memory_ids": [memory.id], "reason": "fine as is"}
        ])
        with pytest.raises(DigestError, match="nothing applied"):
            container.digest_service.revert(project, answer["digest_id"])

    def test_a_reverted_digest_cannot_be_applied_again(self, container, project):
        memory = _store(container, project, "architecture", "Ports", "Listens on 8080.")
        answer = container.digest_service.propose(project, [{
            "op": "rewrite", "memory_ids": [memory.id], "reason": "clearer",
            "title": "Ports", "content": "The API listens on port 8080.",
        }])
        op = answer["operations"][0]
        container.digest_service.apply(project, answer["digest_id"], approve=[op["op_id"]])
        container.digest_service.revert(project, answer["digest_id"])
        with pytest.raises(DigestError, match="was reverted"):
            container.digest_service.apply(
                project, answer["digest_id"], approve=[op["op_id"]],
            )


class TestRejectAndList:
    def test_reject_drops_the_proposal_without_writing(self, container, project):
        memory = _store(container, project, "reference", "Old", "Retired thing.")
        answer = container.digest_service.propose(project, [
            {"op": "archive", "memory_ids": [memory.id], "reason": "retired long ago"}
        ])
        result = container.digest_service.reject(project, answer["digest_id"], "not now")
        assert result["status"] == "rejected"
        assert container.memory_repo.get_by_id(project, memory.id).status == "active"
        with pytest.raises(DigestError, match="was rejected"):
            container.digest_service.apply(
                project, answer["digest_id"], approve_all=True,
            )

    def test_list_surfaces_a_proposal_awaiting_a_decision(self, container, project):
        memory = _store(container, project, "reference", "Old", "Retired thing.")
        answer = container.digest_service.propose(project, [
            {"op": "archive", "memory_ids": [memory.id], "reason": "retired long ago"}
        ])
        listing = container.digest_service.list(project)
        assert listing["awaiting_decision"] == [answer["digest_id"]]
        assert listing["digests"][0]["operations"] == 1

    def test_analyse_warns_about_a_waiting_proposal(self, container, project):
        memory = _store(container, project, "reference", "Old", "Retired thing.")
        container.digest_service.propose(project, [
            {"op": "archive", "memory_ids": [memory.id], "reason": "retired long ago"}
        ])
        answer = container.digest_service.analyse(project)
        assert answer["awaiting_decision"]
        assert "waiting for the user's decision" in answer["warning"]
