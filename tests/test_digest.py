"""Digest tests, written around the failure mode that matters: losing a rule.

Every assertion here is about something that must NOT happen - a clause
disappearing into a merge, an unapproved op being written, a revert that only
mostly restores, a second apply doubling a change.
"""

import pytest

from memory_mcp.container import Container
from memory_mcp.models import MemoryCategory, StoreMemoryRequest
from memory_mcp.services.digest import DigestError
from memory_mcp.utils.diff import clause_coverage, coverage_problems, split_clauses


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


# Rule-losing edits a verification pass found passing as `clean` on the first
# implementation, when the guard was recall-only. Each one keeps most of the
# sentence and changes the part that IS the rule, which is why a threshold on
# word recall can never see them: the number, the modal, the quantifier is one
# token however long the clause is.
SLO = (
    "Every response from the search endpoint must come back in under 200ms at "
    "the 99th percentile, measured at the load balancer rather than in the "
    "application."
)
REVIEW = "Every schema migration must be reviewed by a second engineer before it runs."
NEVER_PROD = "Never run a migration directly against production."
EXEMPT = "The billing service is exempt from the shared rate limiter."
WINDOW = "Deploy only from main, within the window that closes at 16:00."
INCIDENT = "Page the on-call lead within 24 hours of any data-loss incident."

LOSSES = [
    ("a threshold is rewritten", [SLO], SLO.replace("200ms", "500ms")),
    ("a threshold is deleted", [SLO],
     "Every response from the search endpoint must come back quickly, measured "
     "at the load balancer rather than in the application."),
    ("a percentile is rewritten", [SLO], SLO.replace("99th", "50th")),
    ("must becomes should", [REVIEW], REVIEW.replace("must", "should")),
    ("an obligation nobody wrote is added", [REVIEW],
     REVIEW + " A hotfix migration may be applied straight to production "
     "without a pull request when the on-call lead agrees."),
    ("a prohibition becomes a preference", [NEVER_PROD],
     "Avoid running a migration directly against production where possible."),
    ("a prohibition is inverted", [NEVER_PROD],
     "Always run a migration directly against production."),
    ("an exemption becomes an obligation", [EXEMPT],
     EXEMPT.replace("exempt from", "subject to")),
    ("a universal becomes a single case",
     ["Every service must export a health endpoint."],
     "The search service must export a health endpoint."),
    ("a deadline is softened", [INCIDENT],
     "Page the on-call lead promptly after any data-loss incident."),
    ("an exclusivity is dropped", [WINDOW],
     "Deploy from main, within the window that closes at 16:00."),
    ("an exception is appended", [NEVER_PROD],
     "Never run a migration directly against production, unless the on-call "
     "lead approves."),
    ("a whole clause is dropped",
     [NEVER_PROD + " Migrations run through the pipeline."],
     "Migrations run through the pipeline."),
]

# Rewrites that lose nothing. These matter as much as the losses: a guard that
# fires on honest work is a guard the agent learns to wave through, and then the
# real losses go with it.
FAITHFUL = [
    ("reordered", [REVIEW],
     "A second engineer must review every schema migration before it runs."),
    ("merged", [NEVER_PROD, "Migrations run through the deploy pipeline."],
     "Never run a migration directly against production; migrations run through "
     "the deploy pipeline."),
    ("retitled and reordered", [SLO, "The load balancer is the measurement point."],
     "Search latency\nEvery response from the search endpoint must come back in "
     "under 200ms at the 99th percentile. The load balancer is the measurement "
     "point, rather than the application."),
    ("compressed, always carried by every",
     ["Always run the tests before you commit.",
      "Run the full test suite before every commit."],
     "Committing\nRun the full test suite before every commit."),
]


class TestTheGuardCatchesRealLosses:
    @pytest.mark.parametrize("name,sources,replacement", LOSSES,
                             ids=[c[0] for c in LOSSES])
    def test_a_losing_rewrite_is_never_clean(self, name, sources, replacement):
        report = clause_coverage(sources, replacement)
        assert not report["clean"], f"{name} passed as clean"
        assert coverage_problems(report), f"{name} produced no problem to show"

    @pytest.mark.parametrize("name,sources,replacement", FAITHFUL,
                             ids=[c[0] for c in FAITHFUL])
    def test_a_faithful_rewrite_stays_clean(self, name, sources, replacement):
        report = clause_coverage(sources, replacement)
        assert report["clean"], f"{name} was wrongly flagged: {coverage_problems(report)}"

    def test_a_rewritten_threshold_names_the_number_in_the_report(self):
        report = clause_coverage([SLO], SLO.replace("200ms", "500ms"))
        assert "200ms" in report["altered"][0]["missing"]

    def test_approve_all_refuses_a_rewrite_that_softens_a_threshold(
        self, container, project
    ):
        rule = _store(container, project, "mandatory_rules", "Search latency", SLO,
                      priority=2)
        answer = container.digest_service.propose(project, [{
            "op": "rewrite", "memory_ids": [rule.id],
            "reason": "tighten the wording",
            "title": "Search latency", "content": SLO.replace("200ms", "500ms"),
        }])
        assert answer["blocked"] == [answer["operations"][0]["op_id"]]
        result = container.digest_service.apply(
            project, answer["digest_id"], approve_all=True,
        )
        assert result["applied"] == 0
        assert container.memory_repo.get_by_id(project, rule.id).content == SLO


class TestMalformedOperations:
    """The `operations` argument is free-form dicts, so it has no pydantic schema
    to lean on the way memory_store does. Every shape has to be refused here."""

    def test_a_bare_string_of_tags_is_refused(self, container, project):
        memory = _store(container, project, tags=["build"])
        with pytest.raises(DigestError, match="tags is a string"):
            container.digest_service.propose(project, [{
                "op": "retag", "memory_ids": [memory.id], "tags": "legacy",
                "reason": "one tag",
            }])
        assert container.memory_repo.get_by_id(project, memory.id).tags == ["build"]

    def test_a_bare_string_of_memory_ids_is_refused(self, container, project):
        memory = _store(container, project)
        with pytest.raises(DigestError, match="memory_ids is a string"):
            container.digest_service.propose(project, [{
                "op": "keep", "memory_ids": memory.id, "reason": "fine",
            }])

    def test_an_operation_that_is_not_an_object_is_refused(self, container, project):
        _store(container, project)
        with pytest.raises(DigestError, match="not an object"):
            container.digest_service.propose(project, ["archive everything"])

    def test_split_parts_must_be_objects(self, container, project):
        memory = _store(container, project, content="One thing. Another thing.")
        with pytest.raises(DigestError, match="not an object"):
            container.digest_service.propose(project, [{
                "op": "split", "memory_ids": [memory.id], "reason": "two rules",
                "parts": ["One thing.", "Another thing."],
            }])

    def test_the_tool_layer_turns_a_refusal_into_an_error_dict(self, container, project):
        """The MCP layer must answer, not raise - an agent cannot act on a stack
        trace."""
        from memory_mcp import server

        answer = server.memory_digest_propose(
            operations=[{"op": "nonsense", "memory_ids": ["x"], "reason": "y"}],
            project=project,
        )
        assert "error" in answer and "no delete" in answer["error"]


class TestKeepOnlyDigest:
    def test_a_digest_of_nothing_but_keeps_closes(self, container, project):
        """It used to stay `proposed` forever, so every later analysis warned
        about a proposal the user had already dealt with."""
        first = _store(container, project, "architecture", "Ports", "Listens on 8080.")
        second = _store(container, project, "reference", "Board", "The old board.")
        answer = container.digest_service.propose(project, [
            {"op": "keep", "memory_ids": [first.id], "reason": "still accurate"},
            {"op": "keep", "memory_ids": [second.id], "reason": "checked, still used"},
        ])
        result = container.digest_service.apply(
            project, answer["digest_id"], approve_all=True,
        )
        assert result["state"] == "applied"
        assert result["still_pending"] == []
        assert container.digest_service.list(project)["awaiting_decision"] == []

    def test_a_keep_is_recorded_in_provenance(self, container, project):
        """So the next digest can see this signal was investigated and rejected,
        rather than missed."""
        memory = _store(container, project, "architecture", "Ports", "Listens on 8080.")
        answer = container.digest_service.propose(project, [
            {"op": "keep", "memory_ids": [memory.id], "reason": "still accurate"},
        ])
        container.digest_service.apply(project, answer["digest_id"], approve_all=True)
        operations = [
            entry.operation
            for entry in container.provenance_repo.for_memory(project, memory.id)
        ]
        assert "digest_keep" in operations
        # And nothing about the memory changed.
        after = container.memory_repo.get_by_id(project, memory.id)
        assert (after.title, after.content, after.status) == (
            "Ports", "Listens on 8080.", "active",
        )


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


class TestOverlapThreshold:
    def test_a_pair_just_past_the_old_threshold_is_reported(self, container, project):
        """A verification pass measured two genuinely overlapping deploy-window
        rules at 0.4014 against a 0.40 cut and got no signal at all. The distance
        is passed in directly here: the point under test is the threshold, and
        pinning it to particular sentences would make the test hostage to the
        embedding model."""
        from memory_mcp.services.digest_signals import collect_signals

        left = _store(container, project, "mandatory_rules", "Deploy window",
                      "Production deploys stop at 16:00.", priority=2)
        right = _store(container, project, "mandatory_rules", "Late deploys",
                       "Shipping after 16:00 needs the on-call lead's agreement.",
                       priority=2)
        signals = collect_signals([left, right], [(left.id, right.id, 0.4014)])
        assert signals["overlap"], "0.4014 must still be close enough to report"
        assert signals["overlap"][0]["distance"] == 0.4014

    def test_an_unrelated_pair_is_not_reported(self, container, project):
        from memory_mcp.services.digest_signals import collect_signals

        left = _store(container, project, "architecture", "Ports", "Listens on 8080.")
        right = _store(container, project, "reference", "Board", "The old dashboard.")
        signals = collect_signals([left, right], [(left.id, right.id, 0.66)])
        assert not any(signals.values())


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
