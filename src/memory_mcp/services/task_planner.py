"""Turn one multi-part request into an ordered set of tasks.

The problem this solves: a request like "add the endpoint, wire the UI, and write
the docs" lives only in the transcript. If the session ends after the endpoint,
the other two are gone - the queue holds only what someone thought to write down.
Decomposing up front makes the queue, not the conversation, the record of what
was asked.

WHERE THE JUDGEMENT LIVES: in the model, not here. Deciding whether a request has
two separable deliverables or is one job described in two clauses is a reading
comprehension problem, and a hook - a shell script running before the model sees
anything - cannot do it. So this module is the recording half, and the boundary
is stated in SERVER_INSTRUCTIONS for the deciding half.

That boundary is enforced structurally where it can be: a plan of one task is
rejected, because a single deliverable is `memory_task_add`, and a plan is capped
so a runaway decomposition cannot bury a board. The verbatim request is stored on
every task it produced, so the original wording survives even if a title is later
edited into something narrower.

A PLAN IS SAFE TO RETRY. On 2026-09-04 a plan did all its work, its response was
lost to a daemon restart, the caller sent it again, and a second identical set
of seven tasks and seven board cards was created. So every plan records an exact
fingerprint of itself, and a plan whose fingerprint matches a live one from the
last PLAN_RETRY_WINDOW_SECONDS returns that set instead of creating another.
"""

import hashlib
import json
import threading
from collections.abc import Sequence

from memory_mcp.db.connection import transaction
from memory_mcp.exceptions import MemoryMCPError
from memory_mcp.utils.decomposition import decomposition_hint
from memory_mcp.models import CreateTaskRequest, Task, TaskSource

# A plan is for a request with several deliverables. One task is not a plan - it
# is memory_task_add - and thirty is not a plan either, it is noise.
MIN_TASKS = 2
MAX_TASKS = 20

# How long an identical plan counts as a retry of the first. Long enough to
# cover a daemon restart and a caller that reconnects before re-sending; short
# enough that the same request asked again another day is planned again.
PLAN_RETRY_WINDOW_SECONDS = 30 * 60

DEDUPLICATED_NOTE = (
    "This exact plan - the same request and the same titles in the same order - "
    "was already created in this project within the last "
    f"{PLAN_RETRY_WINDOW_SECONDS // 60} minutes, so its tasks are returned as they "
    "are now and NOTHING new was created, locally or on the board. Anything this "
    "call carried that differs (a description, a priority, labels) was not "
    "applied; use memory_task_update for that."
)


class PlanError(MemoryMCPError):
    """The proposed decomposition is not a plan."""


def plan_fingerprint(project: str, request: str, titles: Sequence[str]) -> str:
    """The exact identity of a plan, for retry detection.

    The project, the verbatim request and the titles in order - each already
    trimmed at its ends, nothing else normalised. Exact, not fuzzy: a plan whose
    request or any title differs is a different plan and is never merged.
    Descriptions and the other fields are left out on purpose: a caller
    re-issuing a lost call may reword a description, and that is still the call
    it already made.
    """
    ordered_titles = list(titles)
    identity = {"project": project, "request": request, "titles": ordered_titles}
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True)
    encoded = canonical.encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TaskPlanner:
    def __init__(self, task_service, task_bridge=None):
        self._tasks = task_service
        self._bridge = task_bridge
        # One lock per project around "find an earlier identical plan, else
        # create": a retry racing the original must not have both miss.
        self._plan_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _plan_lock(self, project: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._plan_locks.get(project)
            if lock is None:
                lock = threading.Lock()
                self._plan_locks[project] = lock
            return lock

    def plan(
        self, project: str, request: str, items: list[dict], *, mirror: bool = True,
        path: str | None = None,
    ) -> dict:
        """Create the tasks for one request, in the order given.

        `items` carry title, description, and optionally priority, labels, role
        (which agent the task is for), parent_index - an index EARLIER in the
        same list, so a plan can express "this deliverable has these steps"
        without a second round trip - and `path` / `target`, which route the
        task to the board that owns that subtree (see TaskBridge.routing_for).
        The plan-level `path` is the default for an item that names neither;
        an item's own `target` is never overridden by it.

        Forwarding these is the fix for the reported bug: the planner used to
        drop `target`, so every planned task landed on the default board.
        Each created task in the answer carries its `routing` when there was
        one.

        Order is dependency order: `position` follows the list, so the queue can
        be worked top-down. Every task records the verbatim request as its first
        comment.

        Idempotent on (project, request, ordered titles) for
        PLAN_RETRY_WINDOW_SECONDS: a repeat returns the earlier plan's tasks as
        they are now, with `deduplicated: true` and a `note`, and creates
        nothing. Those entries carry no `routing` - the decision was reported
        by the call that made it. An earlier plan with a deleted or archived
        task does not count, and a new set is created.
        """
        text = (request or "").strip()
        self._validate(text, items)
        titles = tuple(item["title"].strip() for item in items)
        fingerprint = plan_fingerprint(project, text, titles)

        with self._plan_lock(project):
            result = self._plan_once(project, text, items, path, fingerprint)
        if mirror:
            self._mirror_to_board(project, result)
        return result

    @staticmethod
    def _validate(text: str, items: Sequence[dict]) -> None:
        if not text:
            raise PlanError("request must not be empty - it is what the tasks trace back to")
        if len(items) < MIN_TASKS:
            raise PlanError(
                f"a plan needs at least {MIN_TASKS} tasks; for a single deliverable "
                "use memory_task_add instead"
            )
        if len(items) > MAX_TASKS:
            raise PlanError(
                f"{len(items)} tasks is over the {MAX_TASKS} cap - decompose by "
                "deliverable, not by step. Steps belong under a parent task."
            )

        for index, item in enumerate(items):
            if not (item.get("title") or "").strip():
                raise PlanError(f"task {index + 1} has no title")
            if not (item.get("description") or "").strip():
                raise PlanError(
                    f"task {index + 1} ({item['title']!r}) has no description. A task "
                    "must carry enough detail to be implemented without this "
                    "conversation - that is the entire point of writing it down."
                )
            parent = item.get("parent_index")
            if parent is not None and not (0 <= parent < index):
                raise PlanError(
                    f"task {index + 1} has parent_index {parent}: it must point at a "
                    "task EARLIER in the list, so a plan cannot contain a cycle"
                )
            if parent is not None and items[parent].get("parent_index") is not None:
                # One level only - the same rule TaskService.create enforces, but
                # checked HERE so the whole plan is rejected before a single task
                # is created and the message can name the item by its index.
                raise PlanError(
                    f"task {index + 1} ({item['title']!r}) hangs off task "
                    f"{parent + 1} ({items[parent]['title']!r}), which is itself a "
                    "sub-task. Sub-tasks cannot have sub-tasks: point it at task "
                    f"{items[parent]['parent_index'] + 1} instead, or make it a "
                    "task of its own."
                )

    def _plan_once(
        self, project: str, text: str, items: Sequence[dict], path: str | None,
        fingerprint: str,
    ) -> dict:
        """The earlier identical plan when there is one, else a new one."""
        earlier = self._tasks.recorded_plan(project, fingerprint, PLAN_RETRY_WINDOW_SECONDS)
        if earlier is not None:
            entries = [task.model_dump(mode="json") for task in earlier]
            result = self._answer(project, text, earlier, entries)
            result["deduplicated"] = True
            result["note"] = DEDUPLICATED_NOTE
            return result

        created, routings = self._create(project, text, items, path, fingerprint)
        entries = []
        for task, routing in zip(created, routings):
            entry = task.model_dump(mode="json")
            if routing is not None:
                entry["routing"] = routing
            entries.append(entry)
        result = self._answer(project, text, created, entries)
        result["deduplicated"] = False
        return result

    def _create(
        self, project: str, text: str, items: Sequence[dict], path: str | None,
        fingerprint: str,
    ) -> tuple[tuple[Task, ...], tuple[dict | None, ...]]:
        created, ids, routings = [], [], []
        index = -1
        try:
            # ONE transaction for the whole plan. A half-applied plan is worse
            # than no plan at all: the queue reads as a considered decomposition
            # when it is really the first fragment of one, nothing records that
            # the rest was lost, and re-running creates a SECOND parent rather
            # than resuming. Every repository call below keeps its own
            # `with connect(project)` and joins this transaction through it, so
            # a failure on task 7 of 9 takes tasks 1-6 down with it.
            with transaction(project):
                for index, item in enumerate(items):
                    parent_index = item.get("parent_index")
                    target = (item.get("target") or "").strip() or None
                    item_path = (item.get("path") or "").strip() or None
                    create_request = CreateTaskRequest(
                        project=project,
                        title=item["title"].strip(),
                        description=item["description"].strip(),
                        priority=int(item.get("priority", 0)),
                        labels=list(item.get("labels") or []),
                        parent_id=ids[parent_index] if parent_index is not None else None,
                        source=TaskSource.CLAUDE,
                        role=(item.get("role") or "").strip() or None,
                        target=target,
                        # The plan's path only fills in for an item that names
                        # no board at all - a target is a stronger statement.
                        path=item_path or (None if target else path),
                    )
                    task, routing = self._tasks.create_routed(create_request)
                    ids.append(task.id)
                    routings.append(routing)
                    # The request verbatim, on every task it produced: a title gets
                    # edited, a description gets rewritten, and the thing that must
                    # not drift is what was actually asked for.
                    self._tasks.comment(
                        project, task.id, kind="note",
                        body=f"Decomposed from this request:\n\n{text}",
                    )
                    created.append(task)
                # In the same transaction as the tasks: a record of a plan that
                # rolled back would answer a retry with tasks that do not exist.
                self._tasks.record_plan(project, fingerprint, ids)
        except Exception as e:  # noqa: BLE001
            # Say what happened to the plan, not just what threw. "which tasks
            # got created" now has an answer, and the answer is none.
            where = (
                f"task {index + 1} of {len(items)} "
                f"({(items[index].get('title') or '').strip()!r})"
                if 0 <= index < len(items)
                else "opening the transaction"
            )
            raise PlanError(
                f"the plan was ROLLED BACK and no tasks were created - failed on "
                f"{where}: {type(e).__name__}: {e}"
            ) from e
        return tuple(created), tuple(routings)

    @staticmethod
    def _answer(
        project: str, text: str, tasks: Sequence[Task], entries: list[dict],
    ) -> dict:
        result = {
            "project": project,
            "request": text,
            "tasks": entries,
            "count": len(tasks),
            "mirrored": False,
        }
        # A plan is where decomposition is already on the caller's mind, so a
        # top-level item that still reads like several deliverables is worth
        # saying out loud - once, next to the task it is about.
        hints = [
            {"task_id": task.id, "title": task.title, "hint": hint}
            for task in tasks
            if task.parent_id is None
            and (hint := decomposition_hint(task.description))
        ]
        if hints:
            result["hints"] = hints
        return result

    def _mirror_to_board(self, project: str, result: dict) -> None:
        """Straight onto the board: a plan the user cannot see outside the
        session has solved half the problem. Never fatal - the local queue is
        the record.

        Through the OUTBOX, not `push`: every create already queued its own row,
        so draining the queue sends exactly these tasks with every field they
        carry. `push` re-POSTed the whole project - one call per task,
        twenty-five for a seven-task plan - and skipped their fields.

        A deduplicated plan drains too. It creates nothing - the first call's
        rows are already queued or sent, and a create carries the task's
        externalRef - but if the restart that lost the response also cut the
        first flush short, this finishes it rather than leaving it to the next
        nudge.
        """
        if self._bridge is None:
            return
        try:
            if not self._bridge.links(project):
                return
            flushed = self._bridge.flush(project)
        except Exception as e:  # noqa: BLE001
            result["mirror_error"] = f"{type(e).__name__}: {e}"
            return
        result["mirrored"] = True
        result["mirror_counts"] = {
            "flushed": flushed.get("flushed", 0),
            "failed": flushed.get("failed", 0),
            "remaining": flushed.get("remaining", 0),
        }
