"""Memory digest - review, unify and prune a project's memory, safely.

A corpus that has been written to for months drifts: two rules say the same
thing in different words and both are in force, a rule names a file that no
longer exists, a standing instruction sits in a `decision` memory where nothing
enforces it, a note nobody has read in a year costs tokens in every session.

The digest is the pass that fixes that, in three stages, and the stages exist
because of who is good at what:

  analyse   the store computes what it can measure exactly - embedding
            distance, dead paths, TTLs, ages - and hands it over as evidence
            (`digest_signals`), with a brief (`digest_brief`) telling the agent
            how to reason about it.
  propose   the agent submits operations. The store validates them, checks
            every rewrite and merge CLAUSE BY CLAUSE against its sources, and
            returns a diff. Nothing is written.
  apply     only the ops the user approved, one by one, each with the
            overwritten row saved first so `revert` is exact.

Two invariants hold the whole thing up:

1. **A digest never hard-deletes.** Its strongest operation is `archive`: the
   row stays, its provenance stays, and revert brings it back. A rule dropped by
   mistake is recoverable, always.
2. **No clause disappears silently.** "Unify these and write them better" is
   where a business rule really gets lost - not by deletion but by a tightened
   paragraph that quietly stops saying one of the things it used to. Clause
   coverage catches that and names the dropped text verbatim.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from memory_mcp.db.connection import transaction
from memory_mcp.embeddings import embed_text
from memory_mcp.exceptions import MemoryNotFoundError
from memory_mcp.models import (
    Digest, DigestDecision, DigestOp, DigestOperation, DigestState, Memory,
    MemoryCategory, RULE_CATEGORIES, StoreMemoryRequest, TEXT_OPERATIONS,
)
from memory_mcp.services.digest_brief import UNMATCHED_WARNING, digest_brief
from memory_mcp.services.digest_signals import OVERLAP_DISTANCE, collect_signals
from memory_mcp.utils.diff import (
    clause_coverage, coverage_problems, field_diff, fingerprint,
)
from memory_mcp.utils.extraction import (
    estimate_tokens, extract_entities, generate_summary,
)
from memory_mcp.utils.text import prepare_embedding_text

#: Fields a before-image restores. Everything else about a memory is derived
#: from these (summary, entities, embedding) or is history that must not be
#: rewound (access_count, created_at, provenance).
RESTORABLE_FIELDS = (
    "category", "title", "content", "tags", "priority", "status", "metadata",
    "related_ids",
)

#: Where a split's new memories are remembered, so a revert can find them.
CREATED_KEY = "__created__"

_ONE_SOURCE = {
    DigestOperation.REWRITE, DigestOperation.SPLIT, DigestOperation.RECATEGORIZE,
    DigestOperation.RETAG, DigestOperation.REPRIORITIZE, DigestOperation.ARCHIVE,
    DigestOperation.KEEP,
}


class DigestError(ValueError):
    """A proposal or a decision the store refuses. Carries a plain reason."""


class DigestService:
    """Analyse, propose, apply and revert a review of one project's memory."""

    def __init__(
        self, memory_repo, digest_repo, provenance_repo, project_repo,
        rules_service, memory_service,
    ):
        self._memories = memory_repo
        self._digests = digest_repo
        self._provenance = provenance_repo
        self._projects = project_repo
        self._rules = rules_service
        self._memory_service = memory_service

    # ---------- Stage 1: analyse ----------

    def analyse(self, project: str, categories: list[str] | None = None) -> dict:
        """Read the corpus, compute the signals, open a digest. Writes no memory."""
        memories = self._memories.corpus(project, categories)
        pairs = self._memories.similar_pairs(project, OVERLAP_DISTANCE)
        signals = collect_signals(memories, pairs, self._project_root(project))

        rules = [m for m in memories if m.category in RULE_CATEGORIES]
        totals = {
            "memories": len(memories),
            "rules": len(rules),
            "corpus_tokens": sum(self._tokens(m) for m in memories),
            "rule_tokens": sum(self._tokens(m) for m in rules),
            "findings": sum(len(v) for v in signals.values()),
            "by_category": self._by_category(memories),
        }
        analysis = {"totals": totals, "signals": signals}

        digest = self._open_digest(project, analysis)
        waiting = self._digests.list(project, limit=5, state=DigestState.PROPOSED.value)

        answer = {
            "digest_id": digest.id,
            "project": project,
            "instructions": digest_brief(
                project, totals["memories"], totals["rules"],
                totals["findings"], totals["corpus_tokens"],
            ),
            "totals": totals,
            "signals": signals,
            "corpus": [self._corpus_row(m) for m in memories],
        }
        if waiting:
            answer["awaiting_decision"] = [
                {"digest_id": d.id, "ops": d.op_count,
                 "proposed_at": d.proposed_at.isoformat() if d.proposed_at else None}
                for d in waiting
            ]
            answer["warning"] = (
                "A previous proposal is still waiting for the user's decision. "
                "Resolve it (memory_digest_apply or memory_digest_reject) before "
                "proposing another, or the two proposals will overlap."
            )
        return answer

    def _open_digest(self, project: str, analysis: dict) -> Digest:
        """The project's one open digest, refreshed - or a new one.

        Re-running the analysis is a normal thing to do (the corpus changed, the
        agent wants a second look) and must not litter the store with abandoned
        digests. An open digest has no ops and no decisions, so overwriting its
        analysis loses nothing.
        """
        existing = self._digests.list(project, limit=1, state=DigestState.OPEN.value)
        if existing:
            self._digests.set_analysis(project, existing[0].id, analysis)
            refreshed = self._digests.get(project, existing[0].id)
            if refreshed is not None:
                return refreshed
        return self._digests.create(project, analysis)

    def _project_root(self, project: str) -> Path | None:
        info = self._projects.get(project)
        if info is None or not info.project_path:
            return None
        root = Path(info.project_path)
        return root if root.is_dir() else None

    @staticmethod
    def _tokens(memory: Memory) -> int:
        return estimate_tokens(f"{memory.title}\n{memory.content}")

    @staticmethod
    def _by_category(memories: list[Memory]) -> dict:
        counts: dict[str, int] = {}
        for memory in memories:
            counts[memory.category.value] = counts.get(memory.category.value, 0) + 1
        return counts

    def _corpus_row(self, memory: Memory) -> dict:
        """A memory as the reviewing agent needs to see it: whole, plus its cost.

        Content is NOT truncated. A digest that proposes rewriting a rule from a
        summary of it is exactly how a clause gets dropped.
        """
        return {
            "memory_id": memory.id,
            "category": memory.category.value,
            "title": memory.title,
            "content": memory.content,
            "tags": memory.tags,
            "priority": memory.priority,
            "source": memory.source,
            "access_count": memory.access_count,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
            "updated_at": memory.updated_at.isoformat() if memory.updated_at else None,
            "tokens": self._tokens(memory),
        }

    # ---------- Stage 2: propose ----------

    def propose(
        self, project: str, operations: list[dict], digest_id: str | None = None,
        notes: str | None = None,
    ) -> dict:
        """Validate and store a proposal, and return the diff for the user.

        Writes nothing to any memory. A re-propose replaces the previous op set
        wholesale, so what the user reads is always the complete proposal.
        """
        if not operations:
            raise DigestError("a proposal needs at least one operation")

        digest = self._proposable_digest(project, digest_id)
        corpus = {m.id: m for m in self._memories.corpus(project)}

        prepared: list[dict] = []
        for index, raw in enumerate(operations):
            prepared.append(self._validate_op(raw, index, corpus))

        stored = self._digests.replace_ops(project, digest.id, prepared)
        self._digests.set_state(
            project, digest.id, DigestState.PROPOSED.value, stamp="proposed_at"
        )
        if notes:
            self._digests.set_notes(project, digest.id, notes)

        unmatched = [op for op in stored if coverage_problems(op.payload.get("coverage"))]
        answer = {
            "digest_id": digest.id,
            "project": project,
            "state": DigestState.PROPOSED.value,
            "operations": [self._op_view(op, corpus) for op in stored],
            "summary": self._proposal_summary(stored, corpus),
            "next_step": (
                "Show these diffs to the user, with the reason for each. Then call "
                "memory_digest_apply(digest_id, approve=[op ids the user approved]). "
                "memory_digest_reject(digest_id) drops the whole proposal."
            ),
        }
        if unmatched:
            answer["blocked"] = [op.id for op in unmatched]
            answer["warning"] = UNMATCHED_WARNING.format(count=len(unmatched))
        return answer

    def _proposable_digest(self, project: str, digest_id: str | None) -> Digest:
        if digest_id:
            digest = self._digests.get(project, digest_id)
            if digest is None:
                raise MemoryNotFoundError(f"Digest not found: {digest_id}")
            if digest.state in (DigestState.APPLIED.value, DigestState.REVERTED.value):
                raise DigestError(
                    f"digest {digest_id} is already {digest.state}; run memory_digest "
                    "again to start a new review"
                )
            return digest
        existing = self._digests.list(project, limit=1, state=DigestState.OPEN.value)
        if existing:
            return existing[0]
        return self._digests.create(project, None)

    # ---------- Validation ----------

    def _validate_op(self, raw: dict, index: int, corpus: dict[str, Memory]) -> dict:
        """Turn one submitted operation into a stored op, or refuse it.

        Every refusal names the operation's position and what is wrong, because
        the caller is an agent that has to fix it without seeing this code.
        """
        where = f"operation {index + 1}"
        kind = self._op_kind(raw.get("op"), where)
        reason = (raw.get("reason") or "").strip()
        if not reason:
            raise DigestError(
                f"{where} ({kind.value}) has no reason. Every operation needs one - "
                "it is what the user reads when deciding whether to approve it."
            )

        memory_ids = [str(i) for i in (raw.get("memory_ids") or []) if str(i).strip()]
        if not memory_ids:
            raise DigestError(f"{where} ({kind.value}) names no memory_ids")
        missing = [i for i in memory_ids if i not in corpus]
        if missing:
            raise DigestError(
                f"{where} ({kind.value}) names memories that are not active in this "
                f"project: {missing}. Re-run memory_digest - the corpus has changed."
            )
        if kind in _ONE_SOURCE and len(memory_ids) != 1:
            raise DigestError(
                f"{where} ({kind.value}) takes exactly one memory_id, got {len(memory_ids)}"
            )
        if kind is DigestOperation.MERGE and len(memory_ids) < 2:
            raise DigestError(f"{where} (merge) needs at least two memory_ids")

        payload: dict = {"reason": reason}
        if raw.get("note"):
            payload["note"] = str(raw["note"])
        target_id = raw.get("target_id")

        handler = getattr(self, f"_prepare_{kind.value}")
        target_id = handler(raw, payload, memory_ids, target_id, corpus, where)

        sources = [corpus[i] for i in memory_ids]
        payload["sources"] = {
            m.id: {"title": m.title, "fingerprint": fingerprint(f"{m.title} {m.content}")}
            for m in sources
        }
        if kind in TEXT_OPERATIONS:
            payload["coverage"] = clause_coverage(
                [m.content for m in sources], self._replacement_text(kind, payload),
            )
        return {
            "id": str(uuid.uuid4()), "op": kind.value, "memory_ids": memory_ids,
            "target_id": target_id, "payload": payload,
        }

    @staticmethod
    def _op_kind(value, where: str) -> DigestOperation:
        try:
            return DigestOperation((value or "").strip().lower())
        except ValueError:
            allowed = ", ".join(o.value for o in DigestOperation)
            raise DigestError(
                f"{where}: unknown op {value!r}. Allowed: {allowed}. There is no "
                "delete - a digest archives, so nothing it touches is unrecoverable."
            ) from None

    @staticmethod
    def _replacement_text(kind: DigestOperation, payload: dict) -> str:
        """The text a coverage check measures the sources against.

        Deliberately asymmetric with what it is measured FROM. The source side is
        content only, because a title is a label and every merge renames - taking
        titles as clauses made "Always test first" a dropped clause of every
        honest merge. The replacement side includes the new title, because a
        statement the agent moved up into the title has not been lost.
        """
        if kind is DigestOperation.SPLIT:
            return "\n".join(
                f"{part.get('title', '')}\n{part.get('content', '')}"
                for part in payload.get("parts", [])
            )
        return f"{payload.get('title', '')}\n{payload.get('content', '')}"

    # Each _prepare_* fills `payload` and returns the op's target_id.

    def _prepare_keep(self, raw, payload, memory_ids, target_id, corpus, where):
        return memory_ids[0]

    def _prepare_rewrite(self, raw, payload, memory_ids, target_id, corpus, where):
        current = corpus[memory_ids[0]]
        payload["title"] = (raw.get("title") or current.title).strip()
        payload["content"] = (raw.get("content") or current.content).strip()
        if not payload["content"]:
            raise DigestError(f"{where} (rewrite) has empty content")
        if (payload["title"], payload["content"]) == (current.title, current.content):
            raise DigestError(
                f"{where} (rewrite) changes nothing. Use `keep` to record that you "
                "looked at this one and left it alone."
            )
        self._optional_fields(raw, payload, where)
        return memory_ids[0]

    def _prepare_merge(self, raw, payload, memory_ids, target_id, corpus, where):
        survivor = target_id or memory_ids[0]
        if survivor not in memory_ids:
            raise DigestError(
                f"{where} (merge): target_id {survivor!r} must be one of the merged "
                "memory_ids. The survivor keeps its id, so its history and anything "
                "linking to it survive the merge."
            )
        title = (raw.get("title") or "").strip()
        content = (raw.get("content") or "").strip()
        if not title or not content:
            raise DigestError(
                f"{where} (merge) needs the unified title and content - the merged "
                "text is the whole point of the operation"
            )
        payload["title"], payload["content"] = title, content
        # Tags default to the union of the sources': a merge must not quietly
        # drop the tag that made one of them findable.
        if raw.get("tags") is None:
            merged_tags: list[str] = []
            for mid in memory_ids:
                for tag in corpus[mid].tags:
                    if tag not in merged_tags:
                        merged_tags.append(tag)
            payload["tags"] = merged_tags
        self._optional_fields(raw, payload, where)
        # Same for priority: the strictest of the sources wins unless told
        # otherwise, so merging a priority-3 rule into a priority-0 note cannot
        # demote it.
        if payload.get("priority") is None:
            payload["priority"] = max(corpus[mid].priority for mid in memory_ids)
        payload["archived"] = [mid for mid in memory_ids if mid != survivor]
        return survivor

    def _prepare_split(self, raw, payload, memory_ids, target_id, corpus, where):
        parts = raw.get("parts") or []
        if len(parts) < 2:
            raise DigestError(f"{where} (split) needs at least two `parts`")
        cleaned = []
        for position, part in enumerate(parts):
            title = (part.get("title") or "").strip()
            content = (part.get("content") or "").strip()
            if not title or not content:
                raise DigestError(
                    f"{where} (split): part {position + 1} needs a title and content"
                )
            entry = {"title": title, "content": content}
            if part.get("category"):
                entry["category"] = self._category(part["category"], where).value
            if part.get("tags") is not None:
                entry["tags"] = [str(t) for t in part["tags"]]
            if part.get("priority") is not None:
                entry["priority"] = self._priority(part["priority"], where)
            cleaned.append(entry)
        payload["parts"] = cleaned
        return memory_ids[0]

    def _prepare_recategorize(self, raw, payload, memory_ids, target_id, corpus, where):
        current = corpus[memory_ids[0]]
        category = self._category(raw.get("category"), where)
        if category == current.category:
            raise DigestError(
                f"{where} (recategorize) already has category {category.value}"
            )
        payload["category"] = category.value
        payload["from_category"] = current.category.value
        # A rule is enforced by priority as well as by category - see
        # MemoryService.store, which floors every rule at 2. A promotion that
        # kept priority 0 would land in the rule block ranked below notes.
        if category in RULE_CATEGORIES:
            payload["priority"] = max(
                self._priority(raw.get("priority"), where) or 0, current.priority, 2
            )
        elif raw.get("priority") is not None:
            payload["priority"] = self._priority(raw["priority"], where)
        # A recategorize may carry a rewrite: promoting a decision to a rule
        # usually means rephrasing it as an instruction.
        if raw.get("title"):
            payload["title"] = str(raw["title"]).strip()
        if raw.get("content"):
            payload["content"] = str(raw["content"]).strip()
        if payload.get("content"):
            payload["coverage"] = clause_coverage(
                [current.content],
                f"{payload.get('title', current.title)}\n{payload['content']}",
            )
        return memory_ids[0]

    def _prepare_retag(self, raw, payload, memory_ids, target_id, corpus, where):
        if raw.get("tags") is None:
            raise DigestError(f"{where} (retag) needs `tags`")
        payload["tags"] = [str(t) for t in raw["tags"]]
        return memory_ids[0]

    def _prepare_reprioritize(self, raw, payload, memory_ids, target_id, corpus, where):
        priority = self._priority(raw.get("priority"), where)
        if priority is None:
            raise DigestError(f"{where} (reprioritize) needs `priority` 0-3")
        current = corpus[memory_ids[0]]
        if current.category in RULE_CATEGORIES and priority < 2:
            raise DigestError(
                f"{where} (reprioritize): a rule cannot go below priority 2 - it "
                "would sit under ordinary notes in the rule block. Recategorize it "
                "out of the rules instead, if that is what you mean."
            )
        payload["priority"] = priority
        return memory_ids[0]

    def _prepare_archive(self, raw, payload, memory_ids, target_id, corpus, where):
        current = corpus[memory_ids[0]]
        if current.category in RULE_CATEGORIES and len(payload["reason"]) < 25:
            raise DigestError(
                f"{where} (archive) is dropping a RULE and its reason is one line. "
                "Say what in the system is gone and how you checked - the user is "
                "being asked to stop enforcing something they once required."
            )
        payload["archived_title"] = current.title
        return memory_ids[0]

    def _optional_fields(self, raw: dict, payload: dict, where: str) -> None:
        if raw.get("tags") is not None:
            payload["tags"] = [str(t) for t in raw["tags"]]
        if raw.get("priority") is not None:
            payload["priority"] = self._priority(raw["priority"], where)
        if raw.get("category"):
            payload["category"] = self._category(raw["category"], where).value

    @staticmethod
    def _category(value, where: str) -> MemoryCategory:
        try:
            return MemoryCategory((value or "").strip().lower())
        except ValueError:
            allowed = ", ".join(c.value for c in MemoryCategory)
            raise DigestError(f"{where}: unknown category {value!r}. Allowed: {allowed}") from None

    @staticmethod
    def _priority(value, where: str) -> int | None:
        if value is None:
            return None
        try:
            priority = int(value)
        except (TypeError, ValueError):
            raise DigestError(f"{where}: priority must be an integer 0-3") from None
        if not 0 <= priority <= 3:
            raise DigestError(f"{where}: priority must be 0-3, got {priority}")
        return priority

    # ---------- Views ----------

    def _op_view(self, op: DigestOp, corpus: dict[str, Memory]) -> dict:
        """One operation as the user should see it: what it does, and the diff."""
        view = {
            "op_id": op.id,
            "op": op.op,
            "reason": op.payload.get("reason"),
            "memory_ids": op.memory_ids,
            "target_id": op.target_id,
            "decision": op.decision,
            "titles": [corpus[i].title for i in op.memory_ids if i in corpus],
        }
        coverage = op.payload.get("coverage")
        if coverage:
            view["coverage"] = {
                "clean": coverage.get("clean"),
                "ratio": coverage.get("ratio"),
                "unmatched": coverage.get("unmatched", []),
                "polarity_changed": coverage.get("polarity_changed", []),
            }
            problems = coverage_problems(coverage)
            if problems:
                view["blocked"] = (
                    "; ".join(problems)
                    + ". Fold the clauses back in and re-propose, or quote them to "
                    "the user and let them decide."
                )
        view["changes"] = self._changes(op, corpus)
        return view

    def _changes(self, op: DigestOp, corpus: dict[str, Memory]) -> list[dict]:
        kind = DigestOperation(op.op)
        payload = op.payload
        changes: list[dict] = []

        if kind is DigestOperation.KEEP:
            return [{"memory_id": op.memory_ids[0], "action": "unchanged"}]

        if kind is DigestOperation.SPLIT:
            original = corpus.get(op.memory_ids[0])
            parts = payload.get("parts", [])
            changes.append({
                "memory_id": op.memory_ids[0],
                "action": "rewritten as part 1 of the split",
                "fields": field_diff(
                    self._before_view(original), parts[0], ("title", "content", "category"),
                ) if original else [],
            })
            for part in parts[1:]:
                changes.append({
                    "memory_id": None, "action": "created",
                    "new": {k: part.get(k) for k in ("title", "content", "category", "tags", "priority")},
                })
            return changes

        if kind is DigestOperation.MERGE:
            survivor = corpus.get(op.target_id)
            changes.append({
                "memory_id": op.target_id,
                "action": "kept as the unified memory",
                "fields": field_diff(
                    self._before_view(survivor), payload,
                    ("title", "content", "category", "tags", "priority"),
                ) if survivor else [],
            })
            for mid in payload.get("archived", []):
                source = corpus.get(mid)
                changes.append({
                    "memory_id": mid, "action": "archived, folded into the unified memory",
                    "title": source.title if source else None,
                    "content": source.content if source else None,
                })
            return changes

        current = corpus.get(op.memory_ids[0])
        if kind is DigestOperation.ARCHIVE:
            return [{
                "memory_id": op.memory_ids[0], "action": "archived",
                "title": current.title if current else payload.get("archived_title"),
                "content": current.content if current else None,
                "note": "recoverable with memory_digest_revert",
            }]
        return [{
            "memory_id": op.memory_ids[0], "action": kind.value,
            "fields": field_diff(
                self._before_view(current), payload,
                ("title", "content", "category", "tags", "priority"),
            ) if current else [],
        }]

    @staticmethod
    def _before_view(memory: Memory | None) -> dict:
        if memory is None:
            return {}
        return {
            "title": memory.title, "content": memory.content,
            "category": memory.category.value, "tags": memory.tags,
            "priority": memory.priority, "status": memory.status,
        }

    @staticmethod
    def _proposal_summary(ops: list[DigestOp], corpus: dict[str, Memory]) -> dict:
        by_op: dict[str, int] = {}
        archived = 0
        for op in ops:
            by_op[op.op] = by_op.get(op.op, 0) + 1
            if op.op == DigestOperation.ARCHIVE.value:
                archived += 1
            archived += len(op.payload.get("archived", []))
        return {
            "operations": len(ops), "by_op": by_op,
            "memories_archived": archived,
            "unmatched_clauses": sum(
                len(op.payload.get("coverage", {}).get("unmatched", [])) for op in ops
            ),
            "polarity_changes": sum(
                len(op.payload.get("coverage", {}).get("polarity_changed", []))
                for op in ops
            ),
        }

    # ---------- Stage 3: apply ----------

    def apply(
        self, project: str, digest_id: str, approve: list[str] | None = None,
        reject: list[str] | None = None, approve_all: bool = False,
    ) -> dict:
        """Apply the approved operations, and only those.

        Runs as one transaction: a digest that failed halfway would leave the
        corpus in a state nobody proposed and nobody approved.
        """
        digest = self._digests.get(project, digest_id)
        if digest is None:
            raise MemoryNotFoundError(f"Digest not found: {digest_id}")
        if digest.state == DigestState.REVERTED.value:
            raise DigestError(
                f"digest {digest_id} was reverted; run memory_digest again rather "
                "than re-applying a proposal the user undid"
            )
        if digest.state == DigestState.REJECTED.value:
            raise DigestError(f"digest {digest_id} was rejected")

        approve = [str(i) for i in (approve or [])]
        reject = [str(i) for i in (reject or [])]
        by_id = {op.id: op for op in digest.ops}
        unknown = [i for i in approve + reject if i not in by_id]
        if unknown:
            raise DigestError(f"these op ids are not in digest {digest_id}: {unknown}")

        approved_ids, blocked = self._decide(project, digest, approve, reject, approve_all)
        if not approved_ids:
            return {
                "digest_id": digest_id, "project": project, "applied": 0,
                "state": digest.state, "blocked": blocked,
                "message": (
                    "Nothing was approved, so nothing was written. Pass "
                    "approve=[op ids] with the operations the user agreed to."
                ),
            }

        results: list[dict] = []
        touched_rules = False
        with transaction(project):
            for op in digest.ops:
                if op.id not in approved_ids:
                    continue
                if op.applied_at is not None:
                    results.append({"op_id": op.id, "op": op.op, "status": "already applied"})
                    continue
                outcome = self._apply_op(project, digest_id, op)
                results.append(outcome)
                touched_rules = touched_rules or outcome.get("touched_rules", False)

        if touched_rules:
            self._rules.invalidate(project)

        refreshed = self._digests.get(project, digest_id)
        pending = [op.id for op in refreshed.ops if op.decision == DigestDecision.PENDING.value]
        if not pending:
            self._digests.set_state(
                project, digest_id, DigestState.APPLIED.value, stamp="applied_at"
            )
        return {
            "digest_id": digest_id,
            "project": project,
            "applied": len([r for r in results if r.get("status") == "ok"]),
            "results": results,
            "state": DigestState.APPLIED.value if not pending else DigestState.PROPOSED.value,
            "still_pending": pending,
            "blocked": blocked,
            "undo": (
                f"memory_digest_revert('{digest_id}') restores every memory this "
                "digest touched, exactly as it was."
            ),
        }

    def _decide(
        self, project: str, digest: Digest, approve: list[str], reject: list[str],
        approve_all: bool,
    ) -> tuple[set[str], list[dict]]:
        """Record the verdicts and return (ids to apply, ids refused with why).

        `approve_all` is deliberately not a blanket yes: an op that drops a
        clause still needs its own id in `approve`. The user cannot have agreed
        to lose a clause they were never shown.
        """
        blocked: list[dict] = []
        approved: set[str] = set(approve)
        if approve_all:
            for op in digest.ops:
                if op.id in reject or op.id in approved:
                    continue
                problems = coverage_problems(op.payload.get("coverage"))
                if problems:
                    coverage = op.payload["coverage"]
                    blocked.append({
                        "op_id": op.id, "op": op.op,
                        "reason": (
                            "; ".join(problems)
                            + " - so approve_all does not cover it. Approve it by id "
                            "only if the user was shown what it loses and accepted it."
                        ),
                        "unmatched": coverage.get("unmatched", []),
                        "polarity_changed": coverage.get("polarity_changed", []),
                    })
                    continue
                approved.add(op.id)

        keeps = {
            op.id for op in digest.ops
            if op.op == DigestOperation.KEEP.value and op.id in approved
        }
        if approved:
            self._digests.decide(project, sorted(approved), DigestDecision.APPROVED.value)
        if reject:
            self._digests.decide(project, reject, DigestDecision.REJECTED.value)
        # A `keep` is approved and decided, but there is nothing to write.
        return approved - keeps, blocked

    def _apply_op(self, project: str, digest_id: str, op: DigestOp) -> dict:
        """Write one operation, after saving what it overwrites."""
        try:
            before = self._capture_before(project, op)
        except MemoryNotFoundError as exc:
            self._digests.mark_error(project, op.id, str(exc))
            return {"op_id": op.id, "op": op.op, "status": "skipped", "error": str(exc)}

        drift = self._drift(op, before)
        if drift:
            self._digests.mark_error(project, op.id, drift)
            return {"op_id": op.id, "op": op.op, "status": "skipped", "error": drift}

        self._digests.record_before(project, op.id, before)
        handler = getattr(self, f"_do_{op.op}")
        touched_rules = handler(project, digest_id, op, before)
        self._digests.mark_applied(project, op.id, op.target_id)
        return {
            "op_id": op.id, "op": op.op, "status": "ok",
            "memory_ids": op.memory_ids, "target_id": op.target_id,
            "touched_rules": touched_rules,
        }

    def _capture_before(self, project: str, op: DigestOp) -> dict:
        before: dict = {}
        for memory_id in op.memory_ids:
            memory = self._memories.get_by_id(project, memory_id)
            if memory is None:
                raise MemoryNotFoundError(
                    f"memory {memory_id} is gone; this operation was skipped"
                )
            before[memory_id] = {
                "category": memory.category.value, "title": memory.title,
                "content": memory.content, "tags": list(memory.tags),
                "priority": memory.priority, "status": memory.status,
                "metadata": memory.metadata, "related_ids": list(memory.related_ids),
            }
        return before

    @staticmethod
    def _drift(op: DigestOp, before: dict) -> str | None:
        """Whether a source changed between the proposal and the approval.

        The user approved a diff against particular text. If that text has since
        been edited, applying the proposal would overwrite an edit nobody
        reviewed - so the op is skipped and the digest re-run instead.
        """
        proposed = op.payload.get("sources") or {}
        for memory_id, snapshot in proposed.items():
            current = before.get(memory_id)
            if current is None:
                continue
            now = fingerprint(f"{current['title']} {current['content']}")
            if snapshot.get("fingerprint") and snapshot["fingerprint"] != now:
                return (
                    f"memory {memory_id} changed after this was proposed, so applying "
                    "it would overwrite an edit the user never reviewed. Run "
                    "memory_digest again."
                )
        return None

    # ---------- The writes ----------

    def _do_keep(self, project: str, digest_id: str, op: DigestOp, before: dict) -> bool:
        self._provenance.record(
            project, op.memory_ids[0], "digest_keep",
            {"digest_id": digest_id, "reason": op.payload.get("reason")},
        )
        return False

    def _do_rewrite(self, project: str, digest_id: str, op: DigestOp, before: dict) -> bool:
        memory_id = op.memory_ids[0]
        fields = self._text_fields(op.payload)
        self._optional_write_fields(op.payload, fields)
        return self._write(project, digest_id, memory_id, fields, op, "digest_rewrite")

    def _do_merge(self, project: str, digest_id: str, op: DigestOp, before: dict) -> bool:
        survivor = op.target_id
        payload = op.payload
        fields = self._text_fields(payload)
        self._optional_write_fields(payload, fields)

        # Everything the sources pointed at, minus the rows this merge archives:
        # a related_id pointing at an archived memory is a dead link.
        archived = set(payload.get("archived", []))
        related: list[str] = []
        for memory_id in op.memory_ids:
            for link in before[memory_id]["related_ids"]:
                if link not in related and link not in archived and link != survivor:
                    related.append(link)
        fields["related_ids"] = related

        metadata = dict(before[survivor]["metadata"] or {})
        metadata["merged_from"] = [
            {"memory_id": mid, "title": before[mid]["title"]} for mid in archived
        ]
        metadata["merged_by_digest"] = digest_id
        fields["metadata"] = json.dumps(metadata)

        touched = self._write(project, digest_id, survivor, fields, op, "digest_merge")

        for memory_id in archived:
            source_metadata = dict(before[memory_id]["metadata"] or {})
            source_metadata["superseded_by"] = survivor
            source_metadata["superseded_by_digest"] = digest_id
            touched = self._write(
                project, digest_id, memory_id,
                {"status": "archived", "metadata": json.dumps(source_metadata)},
                op, "digest_merge_source",
            ) or touched
        return touched

    def _do_split(self, project: str, digest_id: str, op: DigestOp, before: dict) -> bool:
        original_id = op.memory_ids[0]
        parts = op.payload.get("parts", [])
        first, rest = parts[0], parts[1:]

        fields = self._text_fields(first)
        self._optional_write_fields(first, fields)
        metadata = dict(before[original_id]["metadata"] or {})
        metadata["split_by_digest"] = digest_id
        fields["metadata"] = json.dumps(metadata)
        touched = self._write(project, digest_id, original_id, fields, op, "digest_split")

        created: list[str] = []
        for part in rest:
            category = MemoryCategory(part.get("category") or before[original_id]["category"])
            memory = self._memory_service.store(
                StoreMemoryRequest(
                    project=project, category=category,
                    title=part["title"], content=part["content"],
                    tags=part.get("tags") or list(before[original_id]["tags"]),
                    priority=part.get("priority", before[original_id]["priority"]),
                    source="digest",
                    metadata={"split_from": original_id, "split_by_digest": digest_id},
                )
            )
            created.append(memory.id)
            touched = touched or category in RULE_CATEGORIES

        if created:
            # Recorded inside the before-image so revert can find rows that did
            # not exist when it was captured.
            enriched = dict(before)
            enriched[CREATED_KEY] = created
            self._digests.record_before(project, op.id, enriched)
        return touched

    def _do_recategorize(self, project: str, digest_id: str, op: DigestOp, before: dict) -> bool:
        memory_id = op.memory_ids[0]
        payload = op.payload
        fields: dict = {"category": payload["category"]}
        if payload.get("content"):
            fields.update(self._text_fields(payload, fallback=before[memory_id]))
        elif payload.get("title"):
            fields["title"] = payload["title"]
        if payload.get("priority") is not None:
            fields["priority"] = payload["priority"]
        touched = self._write(project, digest_id, memory_id, fields, op, "digest_recategorize")
        # Either side of the move changes the rule block.
        return touched or before[memory_id]["category"] in {c.value for c in RULE_CATEGORIES}

    def _do_retag(self, project: str, digest_id: str, op: DigestOp, before: dict) -> bool:
        return self._write(
            project, digest_id, op.memory_ids[0], {"tags": op.payload["tags"]},
            op, "digest_retag",
        )

    def _do_reprioritize(self, project: str, digest_id: str, op: DigestOp, before: dict) -> bool:
        return self._write(
            project, digest_id, op.memory_ids[0], {"priority": op.payload["priority"]},
            op, "digest_reprioritize",
        )

    def _do_archive(self, project: str, digest_id: str, op: DigestOp, before: dict) -> bool:
        memory_id = op.memory_ids[0]
        metadata = dict(before[memory_id]["metadata"] or {})
        metadata["archived_by_digest"] = digest_id
        metadata["archive_reason"] = op.payload.get("reason")
        return self._write(
            project, digest_id, memory_id,
            {"status": "archived", "metadata": json.dumps(metadata)},
            op, "digest_archive",
        )

    @staticmethod
    def _text_fields(payload: dict, fallback: dict | None = None) -> dict:
        """Title/content plus everything derived from them.

        A memory whose text changes without its embedding changing is worse than
        one that was never rewritten: search would keep matching it on the old
        wording. Summary and entities go the same way.
        """
        title = payload.get("title") or (fallback or {}).get("title") or ""
        content = payload.get("content") or (fallback or {}).get("content") or ""
        return {
            "title": title, "content": content,
            "summary": generate_summary(title, content),
            "entities": extract_entities(f"{title} {content}"),
            "embedding": embed_text(prepare_embedding_text(title, content)),
        }

    def _optional_write_fields(self, payload: dict, fields: dict) -> None:
        if payload.get("tags") is not None:
            fields["tags"] = payload["tags"]
        if payload.get("priority") is not None:
            fields["priority"] = payload["priority"]
        if payload.get("category"):
            fields["category"] = payload["category"]

    def _write(
        self, project: str, digest_id: str, memory_id: str, fields: dict,
        op: DigestOp, operation: str,
    ) -> bool:
        """Apply field updates and record provenance. Returns True for a rule write.

        Goes to the repository rather than MemoryService.update because a digest
        changes things that API does not expose - `category` above all, which is
        the whole of the promote/demote operation.
        """
        before_category = None
        existing = self._memories.get_by_id(project, memory_id)
        if existing is not None:
            before_category = existing.category
        updated = self._memories.update(project, memory_id, fields)
        self._provenance.record(
            project, memory_id, operation,
            {
                "digest_id": digest_id, "op_id": op.id,
                "reason": op.payload.get("reason"),
                "changed_fields": sorted(fields.keys()),
            },
        )
        return (
            before_category in RULE_CATEGORIES or updated.category in RULE_CATEGORIES
        )

    # ---------- Undo ----------

    def revert(self, project: str, digest_id: str) -> dict:
        """Put every memory this digest touched back exactly as it was."""
        digest = self._digests.get(project, digest_id)
        if digest is None:
            raise MemoryNotFoundError(f"Digest not found: {digest_id}")
        applied = [op for op in digest.ops if op.applied_at is not None]
        if not applied:
            raise DigestError(f"digest {digest_id} has nothing applied to revert")

        restored: list[str] = []
        archived_created: list[str] = []
        touched_rules = False
        with transaction(project):
            for op in reversed(applied):
                before = op.before or {}
                for memory_id, snapshot in before.items():
                    if memory_id == CREATED_KEY:
                        continue
                    fields = {k: snapshot[k] for k in RESTORABLE_FIELDS if k in snapshot}
                    if isinstance(fields.get("metadata"), (dict, list)):
                        fields["metadata"] = json.dumps(fields["metadata"])
                    elif fields.get("metadata") is None:
                        fields["metadata"] = None
                    fields.update(self._text_fields(snapshot))
                    touched_rules = self._write(
                        project, digest_id, memory_id, fields, op, "digest_revert",
                    ) or touched_rules
                    restored.append(memory_id)
                for created_id in before.get(CREATED_KEY, []):
                    # Archived rather than deleted: a digest never destroys a
                    # row, not even one it created itself.
                    self._memories.soft_delete(project, created_id)
                    self._provenance.record(
                        project, created_id, "digest_revert_archive",
                        {"digest_id": digest_id, "op_id": op.id},
                    )
                    archived_created.append(created_id)

        if touched_rules:
            self._rules.invalidate(project)
        self._digests.set_state(
            project, digest_id, DigestState.REVERTED.value, stamp="reverted_at"
        )
        return {
            "digest_id": digest_id, "project": project, "status": "reverted",
            "memories_restored": restored,
            "memories_archived": archived_created,
            "note": (
                "Every field is back to what it was before the digest. Memories the "
                "digest created were archived, not deleted."
            ),
        }

    def reject(self, project: str, digest_id: str, reason: str | None = None) -> dict:
        """Drop a proposal without applying any of it."""
        digest = self._digests.get(project, digest_id)
        if digest is None:
            raise MemoryNotFoundError(f"Digest not found: {digest_id}")
        if any(op.applied_at is not None for op in digest.ops):
            raise DigestError(
                f"digest {digest_id} has applied operations; use memory_digest_revert"
            )
        self._digests.decide(
            project, [op.id for op in digest.ops], DigestDecision.REJECTED.value
        )
        self._digests.set_state(project, digest_id, DigestState.REJECTED.value)
        if reason:
            self._digests.set_notes(project, digest_id, reason)
        return {
            "digest_id": digest_id, "project": project, "status": "rejected",
            "operations_dropped": len(digest.ops),
        }

    # ---------- Reading ----------

    def list(self, project: str, limit: int = 20) -> dict:
        digests = self._digests.list(project, limit=limit)
        return {
            "project": project,
            "total": len(digests),
            "awaiting_decision": [
                d.id for d in digests if d.state == DigestState.PROPOSED.value
            ],
            "digests": [
                {
                    "digest_id": d.id, "state": d.state, "operations": d.op_count,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                    "applied_at": d.applied_at.isoformat() if d.applied_at else None,
                    "reverted_at": d.reverted_at.isoformat() if d.reverted_at else None,
                    "findings": (d.analysis or {}).get("totals", {}).get("findings"),
                    "notes": d.notes,
                }
                for d in digests
            ],
        }

    def get(self, project: str, digest_id: str) -> dict:
        digest = self._digests.get(project, digest_id)
        if digest is None:
            raise MemoryNotFoundError(f"Digest not found: {digest_id}")
        corpus = {m.id: m for m in self._memories.corpus(project)}
        return {
            "digest_id": digest.id, "project": project, "state": digest.state,
            "created_at": digest.created_at.isoformat() if digest.created_at else None,
            "notes": digest.notes,
            "totals": (digest.analysis or {}).get("totals"),
            "operations": [self._op_view(op, corpus) for op in digest.ops],
        }
