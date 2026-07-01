"""Rules service - cached retrieval of mandatory/forbidden rules."""

import threading
import time

from memory_mcp.config import settings
from memory_mcp.models import GLOBAL_PROJECT_SLUG, RulesResponse
from memory_mcp.repositories import MemoryRepository


class RulesCache:
    """Thread-safe in-memory cache for rules per project."""

    def __init__(self, ttl: int | None = None):
        self._cache: dict[str, tuple[float, RulesResponse]] = {}
        self._lock = threading.Lock()
        self._ttl = ttl if ttl is not None else settings.rules_cache_ttl

    def get(self, project: str) -> RulesResponse | None:
        now = time.time()
        with self._lock:
            entry = self._cache.get(project)
            if not entry:
                return None
            ts, value = entry
            if now - ts > self._ttl:
                return None
            return value

    def set(self, project: str, value: RulesResponse) -> None:
        with self._lock:
            self._cache[project] = (time.time(), value)

    def invalidate(self, project: str) -> None:
        with self._lock:
            self._cache.pop(project, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


class RulesService:
    """Retrieves rules with caching. Never uses vector search - rules must be complete."""

    def __init__(self, memory_repo: MemoryRepository, cache: RulesCache):
        self._repo = memory_repo
        self._cache = cache

    def get_rules(self, project: str) -> RulesResponse:
        cached = self._cache.get(project)
        if cached is not None:
            return cached

        # Server mode enforces the approval gate; local mode returns every active
        # rule (the flag is False), so behavior is unchanged.
        enforce = settings.server_mode
        mandatory, forbidden = self._repo.get_rules(project, enforce_approval=enforce)

        # In server mode, every project also inherits the org-wide approved rules
        # (from the reserved __global__ project), injected ahead of its own so
        # they sort first in the rendered block.
        if enforce and project != GLOBAL_PROJECT_SLUG:
            gm, gf = self._global_rules()
            mandatory = gm + mandatory
            forbidden = gf + forbidden

        response = RulesResponse(
            mandatory_rules=mandatory,
            forbidden_rules=forbidden,
            total=len(mandatory) + len(forbidden),
        )
        self._cache.set(project, response)
        return response

    def _global_rules(self) -> tuple[list, list]:
        """Approved org-wide rules, or ([], []) if the global project is empty
        or unavailable. Never fails a project's rule load."""
        try:
            return self._repo.get_rules(GLOBAL_PROJECT_SLUG, enforce_approval=True)
        except Exception:  # noqa: BLE001
            return [], []

    def pending_rules(self, project: str) -> list:
        """Active rules awaiting approval, for the admin moderation queue."""
        return self._repo.rules_by_approval(project, "proposed")

    def invalidate(self, project: str) -> None:
        self._cache.invalidate(project)
        # A change to the org-wide set affects every project's merged block, so
        # blow away the whole cache when the global project is touched.
        if project == GLOBAL_PROJECT_SLUG:
            self._cache.clear()
