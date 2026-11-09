"""Test fixtures for memory-mcp tests."""

import os
import shutil
import tempfile

import pytest

# The product keeps fastembed's model cache under the data dir, and every test
# gets a fresh temp data dir - which would re-download the model (~90 MB) for
# each one. Pin the suite to one shared cache instead: fastembed's own default
# location, so a machine that already has the model downloads nothing.
os.environ.setdefault(
    "FASTEMBED_CACHE_PATH", os.path.join(tempfile.gettempdir(), "fastembed_cache")
)

from memory_mcp.config import settings
import memory_mcp.db.connection as conn_mod


@pytest.fixture(autouse=True)
def temp_data_dir(tmp_path):
    """Use a temporary directory for all tests. Reset all process-level caches."""
    original = settings.data_dir
    original_mirror = settings.asoode_auto_mirror
    # No test may reach asoode. A test that wants the flusher drives
    # TaskBridge.flush directly, against a fake client.
    settings.asoode_auto_mirror = False
    settings.data_dir = tmp_path / "memory-mcp"
    settings.ensure_dirs()

    # Reset initialization flags so each test gets a fresh schema
    conn_mod._initialized_dbs.clear()
    conn_mod.invalidate_path_cache()

    yield tmp_path / "memory-mcp"

    conn_mod._initialized_dbs.clear()
    conn_mod.invalidate_path_cache()
    settings.data_dir = original
    settings.asoode_auto_mirror = original_mirror

    # Delete the DuckDB files this test made. pytest keeps the last few runs of
    # tmp_path, and a DuckDB file has a ~5 MB floor even with one row - a suite
    # that creates a project per test otherwise leaves gigabytes behind.
    shutil.rmtree(tmp_path / "memory-mcp", ignore_errors=True)


@pytest.fixture
def project_slug():
    return "test-project"
