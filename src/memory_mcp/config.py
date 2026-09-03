"""Configuration via environment variables and pydantic-settings."""

from pathlib import Path

from pydantic_settings import BaseSettings


# Available embedding model presets
EMBEDDING_MODELS = {
    "english": {
        "name": "all-MiniLM-L6-v2",
        "dim": 384,
        "languages": ["English"],
        "size_mb": 80,
        "ram_mb": 90,
        "params": "22M",
        "speed": "Very fast (~14k sentences/sec)",
        "description": "Lightweight English-only model. Best for English-only projects.",
    },
    "multilingual": {
        "name": "paraphrase-multilingual-MiniLM-L12-v2",
        "dim": 384,
        "languages": [
            "ar", "bg", "ca", "cs", "da", "de", "el", "en", "es", "et",
            "fa", "fi", "fr", "gl", "gu", "he", "hi", "hr", "hu", "hy",
            "id", "it", "ja", "ka", "ko", "ku", "lt", "lv", "mk", "mn",
            "mr", "ms", "my", "nb", "nl", "pl", "pt", "ro", "ru", "sk",
            "sl", "sq", "sr", "sv", "th", "tr", "uk", "ur", "vi",
        ],
        "size_mb": 470,
        "ram_mb": 500,
        "params": "118M",
        "speed": "Fast (~5k sentences/sec)",
        "description": "50+ languages including Turkish, Japanese, Korean, Arabic. Same 384 dimensions.",
    },
}


class Settings(BaseSettings):
    model_config = {"env_prefix": "MEMORY_MCP_"}

    # Default data home. Distinct from the legacy "~/.memory-mcp" so this
    # rebuilt server never shares a directory with an older install.
    data_dir: Path = Path.home() / ".claude-memory-mcp"
    embedding_model: str = "all-MiniLM-L6-v2"
    embedding_dim: int = 384
    max_connections: int = 5
    rules_cache_ttl: int = 60
    search_oversample: int = 3
    relevance_weights: tuple[float, float, float] = (0.7, 0.15, 0.15)

    # HTTP daemon (shared server for Claude clients + the management UI).
    # Note: TCP ports must be 0-65535, so 98765 is not usable; 8765 is the
    # in-range stand-in. Override with MEMORY_MCP_DAEMON_PORT if needed.
    daemon_host: str = "127.0.0.1"
    daemon_port: int = 8765
    daemon_hostname: str = "claude-memory-mcp"

    # Deployment mode. "local" (default) is the original single-user, no-auth
    # behavior - every new auth/identity/approval path is a no-op. "server"
    # opts in to multi-user token auth, per-request isolation, and the rule
    # approval workflow. Set with MEMORY_MCP_MODE=server on a shared install.
    mode: str = "local"

    # Mark the UI session cookie Secure (HTTPS-only). Correct for a server behind
    # TLS (the expected deployment); set MEMORY_MCP_COOKIE_SECURE=false only for a
    # plain-HTTP server install where the browser talks to the daemon over http.
    cookie_secure: bool = True

    # asoode endpoints. Empty means "use the hosted default" (constants.py);
    # set these only for an on-premise asoode, e.g.
    # MEMORY_MCP_ASOODE_API_URL=https://api.asoode.internal. An env value wins
    # over one stored from the UI, so a site can bake its URLs into the daemon's
    # launchd environment and have that be authoritative.
    # Mirror task mutations to asoode automatically. Off in tests, which must
    # never reach the network - a suite that talks to a live server is slow,
    # flaky, and one fixture typo away from writing to a real board.
    asoode_auto_mirror: bool = True
    asoode_app_url: str = ""
    asoode_api_url: str = ""
    asoode_socket_url: str = ""

    # Explicit path to the built frontend (frontend/dist). Leave empty to use
    # the repo-relative location; set MEMORY_MCP_UI_DIR for non-editable
    # installs (e.g. Homebrew) where the package is not next to the repo.
    ui_dir: str = ""

    @property
    def server_mode(self) -> bool:
        """True when running as a shared multi-user server (auth + governance).

        Read from the module-level `settings` singleton, which is evaluated
        from the environment at import time, so this is safe to consult while
        wiring `mcp.auth` during import.
        """
        return self.mode.strip().lower() == "server"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def registry_path(self) -> Path:
        """SQLite database for the project list and local app settings."""
        return self.data_dir / "registry.db"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def model_preset(self) -> str:
        """Return preset key for current model."""
        for key, info in EMBEDDING_MODELS.items():
            if info["name"] == self.embedding_model:
                return key
        return "custom"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()
