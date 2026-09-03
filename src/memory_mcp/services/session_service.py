"""Session service - start/end sessions, load context, handle orphans."""

import uuid
from datetime import datetime, timedelta, timezone

from memory_mcp.models import SessionContext
from memory_mcp.services.adaptation import adaptation_brief
from memory_mcp.repositories import (
    MemoryRepository, ProjectRepository, SessionRepository,
)
from memory_mcp.services.rules_service import RulesService
from memory_mcp.services.task_brief import (
    bound_queue_brief, task_brief, unreachable_brief,
)
from memory_mcp.services.task_service import TaskService

AUTO_CLOSE_SUMMARY = "[Auto-closed: session was not properly ended (context overflow or crash)]"

# How far back "recent decisions" reaches at session start.
RECENT_DECISION_WINDOW = timedelta(days=7)


class SessionService:
    """Session lifecycle with orphaned-session auto-close and context loading."""

    def __init__(
        self,
        session_repo: SessionRepository,
        memory_repo: MemoryRepository,
        project_repo: ProjectRepository,
        rules_service: RulesService,
        task_service: TaskService,
        asoode_bridge=None,
    ):
        self._session_repo = session_repo
        self._memory_repo = memory_repo
        self._project_repo = project_repo
        self._rules_service = rules_service
        self._task_service = task_service
        # Optional: absent in tests and on installs that never link a board, in
        # which case a session behaves exactly as it always has.
        self._asoode_bridge = asoode_bridge

    def start(self, project: str) -> SessionContext:
        session_id = str(uuid.uuid4())

        # Auto-close orphans
        orphans = self._session_repo.orphaned(project)
        for orphan_id in orphans:
            self._session_repo.end(project, orphan_id, AUTO_CLOSE_SUMMARY, 0, 0)

        self._session_repo.insert(project, session_id)
        self._project_repo.touch(project)

        rules = self._rules_service.get_rules(project)

        # Find the last non-auto-closed summary
        last = self._session_repo.last_with_summary(project)
        last_summary = last.summary if last else None
        if last_summary == AUTO_CLOSE_SUMMARY:
            real_last = self._session_repo.last_with_summary(
                project, exclude_summary=AUTO_CLOSE_SUMMARY
            )
            last_summary = real_last.summary if real_last else None

        # Imports that have not been rewritten for this project yet. They are
        # deliberately absent from `rules` above - the session runs without them
        # until an agent adapts each one.
        pending = self._memory_repo.pending_memories(project)

        # No caps here, for the same reason rules have none: session context must
        # load COMPLETELY. A top-N sample silently drops sprint goals and
        # decisions, and nothing downstream can tell that anything is missing -
        # the session just proceeds without them. "Recent" is bounded by the
        # window below, not by an arbitrary count.
        active_sprint = self._memory_repo.get_active_by_category(
            project, "sprint", limit=None,
        )
        recent_decisions = self._memory_repo.get_recent_by_category(
            project, "decision", datetime.now(timezone.utc) - RECENT_DECISION_WINDOW,
            limit=None,
        )

        # Requirements the user parked without interrupting a session. They are
        # surfaced, never started: the brief below is what keeps a queued task
        # from being read as an instruction.
        queued_tasks = self._task_service.queued(project)
        asoode, instructions = self._task_context(project, queued_tasks)

        return SessionContext(
            session_id=session_id,
            project=project,
            mandatory_rules=rules.mandatory_rules,
            forbidden_rules=rules.forbidden_rules,
            last_session_summary=last_summary,
            active_sprint=active_sprint,
            recent_decisions=recent_decisions,
            orphaned_sessions_closed=len(orphans),
            pending_adaptations=pending,
            pending_instructions=adaptation_brief(project, pending),
            queued_tasks=queued_tasks,
            task_instructions=instructions,
            asoode=asoode,
        )

    def _task_context(self, project: str, queued: list) -> tuple[dict | None, str | None]:
        """Pick the brief the queue gets, and why.

        Unbound project -> the capture brief: surface the list, start nothing.
        Bound to an asoode board -> that board is the work queue, so the brief
        says to work it. The binding IS the opt-in; nothing needs configuring per
        project, which is the whole point.

        A bound-but-unreachable board still gets a brief telling the session to
        work the local list, because the local list is the same queue mirrored.
        Never raises: no integration failure may stop a session from starting.
        """
        if self._asoode_bridge is None:
            return None, task_brief(project, queued)
        try:
            status = self._asoode_bridge.queue_status(project)
        except Exception:  # noqa: BLE001 - defensive; queue_status already swallows
            return None, task_brief(project, queued)
        if status is None:
            return None, task_brief(project, queued)
        if not status["reachable"]:
            return status, unreachable_brief(project, status["error"] or "unreachable")
        return status, bound_queue_brief(
            project, queued, status["board_url"], status["remote_only"],
        )

    def end(
        self,
        project: str,
        session_id: str,
        summary: str,
        memories_created: int = 0,
        memories_accessed: int = 0,
    ) -> dict:
        self._session_repo.end(project, session_id, summary, memories_created, memories_accessed)
        # Hand back whatever this session was holding, so its tasks are
        # available immediately instead of waiting out the claim lease.
        released = self._task_service.release_session(project, session_id)
        return {
            "status": "ok",
            "session_id": session_id,
            "summary": summary,
            "tasks_released": released,
        }
