"""Dependency injection container.

Wires together repositories and services in a single place so the
server layer can pull the composed graph without knowing construction details.
"""

import atexit
import threading
import time

from memory_mcp.repositories import (
    MemoryRepository, ProjectRepository, SessionRepository, ProvenanceRepository,
    AttachmentRepository, OutboxRepository, TaskRepository, TemplateRepository,
)
from memory_mcp.services import (
    MemoryService, SearchService, RulesService, RulesCache,
    SessionService, ProjectService, PortableService,
    ExportImportService, ModelService, UpdateService, ClaudeMdService,
    TaskService, TemplateService, SyncService, TaskBridge, TaskPlanner,
)


#: How often the daemon looks for outbox rows nobody nudged. Cheap: one depth
#: query per linked project, no network unless something is pending.
OUTBOX_SWEEP_SECONDS = 60.0
#: How long a short-lived process waits for its own mirrors before exiting.
MIRROR_EXIT_GRACE_SECONDS = 30.0


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
        self.attachment_repo = AttachmentRepository()

        # Caches
        self.rules_cache = RulesCache()
        self._flush_lock = threading.Lock()
        self._flushing: dict[str, bool] = {}
        # Projects nudged while their flush was already running. The running
        # flush goes round once more for them instead of the nudge being lost.
        self._dirty: set[str] = set()
        self._flush_threads: dict[str, threading.Thread] = {}
        self._sweeper: threading.Thread | None = None
        self._sweeper_stop = threading.Event()

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
            link_resolver=lambda project, target: self.task_bridge.resolve_link(
                project, target
            ),
            outbox_repo=self.outbox_repo,
            mirror=self._mirror_soon,
            attachment_repo=self.attachment_repo,
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
        self.task_bridge = TaskBridge(
            self.project_service, self.task_service, outbox_repo=self.outbox_repo,
            attachment_repo=self.attachment_repo,
        )
        self.task_planner = TaskPlanner(self.task_service, self.task_bridge)
        # Constructed after the bridge: a bound project's session brief tells the
        # agent to work the board, so the session service needs it.
        self.session_service = SessionService(
            self.session_repo, self.memory_repo, self.project_repo,
            self.rules_service, self.task_service, self.task_bridge,
        )


    @property
    def asoode_bridge(self) -> TaskBridge:
        """Kept as an alias. The bridge is provider-agnostic and is now
        `task_bridge`; this name is what existing callers and tests say."""
        return self.task_bridge

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
                # A flush is in flight. It may already have read its batch, so
                # the row this nudge is for would sit until the next mutation -
                # done() queues two rows back to back and hit exactly that.
                # Mark the project and let the running flush go round again.
                self._dirty.add(project)
                return
            self._flushing[project] = True

        def _run():
            rerun = False
            try:
                while True:
                    try:
                        self.task_bridge.flush(project)
                    except Exception:  # noqa: BLE001 - a mirror can never break a local write
                        pass
                    with self._flush_lock:
                        if project in self._dirty:
                            self._dirty.discard(project)
                            continue
                    break
                # Then pull anything added on the board - ONCE per drain, not per
                # pass: it is a full board read. Only NEW remote tasks - see
                # TaskBridge.reconcile - so this can never overwrite local work,
                # which is what makes it safe to run unattended.
                try:
                    self.task_bridge.reconcile(project)
                except Exception:  # noqa: BLE001
                    pass
            finally:
                with self._flush_lock:
                    self._flushing.pop(project, None)
                    self._flush_threads.pop(project, None)
                    # A nudge that landed during the reconcile.
                    rerun = project in self._dirty
                    self._dirty.discard(project)
            if rerun:
                self._mirror_soon(project)

        thread = threading.Thread(target=_run, name=f"asoode-flush-{project}", daemon=True)
        try:
            with self._flush_lock:
                self._flush_threads[project] = thread
            thread.start()
        except Exception:  # noqa: BLE001 - a spawn that fails must not jam the project
            with self._flush_lock:
                self._flushing.pop(project, None)
                self._flush_threads.pop(project, None)

    # ---------- draining what a nudge could not ----------
    #
    # A nudge only fires from a mutation in THIS process. Rows left behind by an
    # outage, a crash mid-flush, or a write from a process that exited before
    # its flush thread finished would otherwise wait for the next unrelated
    # mutation of that project - possibly forever.

    def sweep_outboxes(self) -> list[str]:
        """Nudge every linked project that has something pending. Returns them."""
        from memory_mcp.db.registry import linked_slugs

        nudged = []
        try:
            slugs = linked_slugs()
        except Exception:  # noqa: BLE001 - no registry, nothing to sweep
            return nudged
        for slug in slugs:
            try:
                if self.outbox_repo.depth(slug) > 0:
                    self._mirror_soon(slug)
                    nudged.append(slug)
            except Exception:  # noqa: BLE001 - one bad project must not stop the sweep
                continue
        return nudged

    def start_outbox_sweeper(self, interval: float = OUTBOX_SWEEP_SECONDS) -> None:
        """Sweep once now and then every `interval` seconds, until stopped."""
        if self._sweeper is not None and self._sweeper.is_alive():
            return
        self._sweeper_stop.clear()

        def _loop():
            while not self._sweeper_stop.is_set():
                try:
                    self.sweep_outboxes()
                except Exception:  # noqa: BLE001
                    pass
                self._sweeper_stop.wait(interval)

        self._sweeper = threading.Thread(target=_loop, name="outbox-sweeper", daemon=True)
        self._sweeper.start()

    def stop_outbox_sweeper(self) -> None:
        self._sweeper_stop.set()

    def wait_for_mirrors(self, timeout: float = MIRROR_EXIT_GRACE_SECONDS) -> None:
        """Give in-flight flush threads a chance to finish. For a short-lived
        process - the CLI, stdio mode - whose daemon threads would otherwise be
        killed mid-request when the interpreter exits."""
        deadline = time.monotonic() + timeout
        while True:
            with self._flush_lock:
                alive = [t for t in self._flush_threads.values() if t.is_alive()]
            if not alive:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            alive[0].join(timeout=remaining)


# Module-level singleton
container = Container()

# A process that exits with a flush still in flight loses that mirror - the
# thread is a daemon thread and dies with the interpreter. Bounded, so a hung
# network cannot hold the exit hostage.
atexit.register(container.wait_for_mirrors)
