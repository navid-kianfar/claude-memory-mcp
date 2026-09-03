"""Dependency injection container.

Wires together repositories and services in a single place so the
server layer can pull the composed graph without knowing construction details.
"""

from memory_mcp.repositories import (
    MemoryRepository, ProjectRepository, SessionRepository, ProvenanceRepository,
    TaskRepository, TemplateRepository,
)
from memory_mcp.services import (
    MemoryService, SearchService, RulesService, RulesCache,
    SessionService, ProjectService, PortableService,
    ExportImportService, ModelService, UpdateService, ClaudeMdService,
    TaskService, TemplateService, SyncService, AsoodeBridge,
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

        # Caches
        self.rules_cache = RulesCache()

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
        self.asoode_bridge = AsoodeBridge(self.project_service, self.task_service)
        # Constructed after the bridge: a bound project's session brief tells the
        # agent to work the board, so the session service needs it.
        self.session_service = SessionService(
            self.session_repo, self.memory_repo, self.project_repo,
            self.rules_service, self.task_service, self.asoode_bridge,
        )


# Module-level singleton
container = Container()
