"""Dependency injection container.

Wires together repositories and services in a single place so the
server layer can pull the composed graph without knowing construction details.
"""

import threading

from memory_mcp.repositories import (
    MemoryRepository, ProjectRepository, SessionRepository, ProvenanceRepository,
    OutboxRepository, TaskRepository, TemplateRepository,
)
from memory_mcp.services import (
    MemoryService, SearchService, RulesService, RulesCache,
    SessionService, ProjectService, PortableService,
    ExportImportService, ModelService, UpdateService, ClaudeMdService,
    TaskService, TemplateService, SyncService, AsoodeBridge, TaskPlanner,
)


class Container:
    """Holds the instantiated dependency graph."""

    def __init__(self):
        # Repositories (stateless)
        self.memory_repo = MemoryRepository()
        self.project_repo = ProjectRepository()
        self.session_repo = SessionRepository()
        self.provenance_repo = ProvenanceRepository()
        self.template_repo = TemplateRepository()
        self.task_repo = TaskRepository()
        self.outbox_repo = OutboxRepository()

        # Caches
        self.rules_cache = RulesCache()
        self._flush_lock = threading.Lock()
        self._flushing: dict[str, bool] = {}

        # Services
        self.rules_service = RulesService(self.memory_repo, self.rules_cache)
        self.memory_service = MemoryService(
            self.memory_repo, self.provenance_repo,
            self.project_repo, self.rules_service,
        )
        self.search_service = SearchService(self.memory_repo)
        self.task_service = TaskService(
            self.task_repo, self.provenance_repo, self.project_repo,
            self.session_repo,
            # Late-bound: the bridge is constructed below and needs task_service,
            # so the resolver is a lambda rather than the object itself.
            link_resolver=lambda project, target: self.asoode_bridge.resolve_link(
                project, target
            ),
            outbox_repo=self.outbox_repo,
            mirror=self._mirror_soon,
        )
        self.project_service = ProjectService(self.project_repo)
        self.portable_service = PortableService(self.project_repo)
        self.export_import_service = ExportImportService(
            self.memory_repo, self.provenance_repo,
        )
        self.model_service = ModelService(self.memory_repo)
        self.update_service = UpdateService()
        self.claude_md_service = ClaudeMdService(self.memory_service)
        self.template_service = TemplateService(self.template_repo, self.memory_service)
        self.sync_service = SyncService(self.memory_repo, self.project_repo)
        # The asoode client is built lazily inside the bridge, so a machine
        # with no PAT stored still constructs the container fine.
        self.asoode_bridge = AsoodeBridge(
            self.project_service, self.task_service, outbox_repo=self.outbox_repo,
        )
        self.task_planner = TaskPlanner(self.task_service, self.asoode_bridge)
        # Constructed after the bridge: a bound project's session brief tells the
        # agent to work the board, so the session service needs it.
        self.session_service = SessionService(
            self.session_repo, self.memory_repo, self.project_repo,
            self.rules_service, self.task_service, self.asoode_bridge,
        )


    def _mirror_soon(self, project: str) -> None:
        """Drain the outbox off the caller's thread.

        A task mutation must return at local-write speed: mirroring is one or
        more HTTPS round trips, and making an edit wait on them would make the
        remote's availability a property of the local store. One flusher per
        project at a time - a second mutation while a flush is running does not
        start a second flush, it is picked up by the one already going or by the
        next one.
        """
        from memory_mcp.config import settings

        if not settings.asoode_auto_mirror:
            return
        with self._flush_lock:
            if self._flushing.get(project):
                return
            self._flushing[project] = True

        def _run():
            try:
                self.asoode_bridge.flush(project)
            except Exception:  # noqa: BLE001 - a mirror can never break a local write
                pass
            finally:
                with self._flush_lock:
                    self._flushing.pop(project, None)

        threading.Thread(target=_run, name=f"asoode-flush-{project}", daemon=True).start()


# Module-level singleton
container = Container()
