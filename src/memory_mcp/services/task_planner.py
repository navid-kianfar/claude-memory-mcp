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
"""

from memory_mcp.exceptions import MemoryMCPError
from memory_mcp.models import CreateTaskRequest, TaskSource

# A plan is for a request with several deliverables. One task is not a plan - it
# is memory_task_add - and thirty is not a plan either, it is noise.
MIN_TASKS = 2
MAX_TASKS = 20


class PlanError(MemoryMCPError):
    """The proposed decomposition is not a plan."""


class TaskPlanner:
    def __init__(self, task_service, asoode_bridge=None):
        self._tasks = task_service
        self._bridge = asoode_bridge

    def plan(
        self, project: str, request: str, items: list[dict], *, mirror: bool = True,
    ) -> dict:
        """Create the tasks for one request, in the order given.

        `items` carry title, description, and optionally priority, labels and
        parent_index - an index EARLIER in the same list, so a plan can express
        "this deliverable has these steps" without a second round trip.

        Order is dependency order: `position` follows the list, so the queue can
        be worked top-down. Every task records the verbatim request as its first
        comment.
        """
        text = (request or "").strip()
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

        created, ids = [], []
        for item in items:
            parent_index = item.get("parent_index")
            task = self._tasks.create(CreateTaskRequest(
                project=project,
                title=item["title"].strip(),
                description=item["description"].strip(),
                priority=int(item.get("priority", 0)),
                labels=list(item.get("labels") or []),
                parent_id=ids[parent_index] if parent_index is not None else None,
                source=TaskSource.CLAUDE,
            ))
            ids.append(task.id)
            # The request verbatim, on every task it produced: a title gets edited,
            # a description gets rewritten, and the thing that must not drift is
            # what was actually asked for.
            self._tasks.comment(
                project, task.id, kind="note",
                body=f"Decomposed from this request:\n\n{text}",
            )
            created.append(task)

        result = {
            "project": project,
            "request": text,
            "tasks": [t.model_dump(mode="json") for t in created],
            "count": len(created),
            "mirrored": False,
        }
        # Straight onto the board: a plan the user cannot see outside the session
        # has solved half the problem. Never fatal - the local queue is the record.
        if mirror and self._bridge is not None:
            try:
                if self._bridge.links(project):
                    push = self._bridge.push(project)
                    result["mirrored"] = True
                    result["mirror_counts"] = push["counts"]
            except Exception as e:  # noqa: BLE001
                result["mirror_error"] = f"{type(e).__name__}: {e}"
        return result
