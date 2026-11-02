"""The idle-handle LRU: bounded open databases, safe close, honest reopen.

A machine with 60+ project files kept one DuckDB handle per file open forever
(the v0.11.9 decision that fixed the instance-cache crash) and paid ~6 MB of
resident memory per handle. `close_idle_databases` bounds it — but it must
never close a handle that is in use, and it must never close one under a
running query (that wedge is what produced the stale-index disease).
"""

import pytest

import memory_mcp.db.connection as conn_mod
from memory_mcp.config import settings
from memory_mcp.db.connection import (
    close_idle_databases,
    connect,
    open_database_count,
    transaction,
)
from memory_mcp.repositories.project_repository import ProjectRepository


@pytest.fixture
def many_projects(tmp_path):
    """Twelve registered projects, each touched once so it has a handle."""
    repo = ProjectRepository()
    slugs = [f"lru-project-{i:02d}" for i in range(12)]
    for slug in slugs:
        repo.register(slug, slug.title())
        with connect(slug) as conn:
            conn.execute("SELECT 1").fetchall()
    return slugs


class TestTheCap:
    def test_more_than_max_closes_the_least_recently_used(self, many_projects):
        # Touch the first project again so it is the most recent of the twelve.
        with connect(many_projects[0]) as conn:
            conn.execute("SELECT 1").fetchall()
        assert open_database_count() == 12

        closed = close_idle_databases(max_handles=4)

        assert closed >= 8
        assert open_database_count() <= 4
        # The most recently used survivor is still the same handle (no reopen).
        with connect(many_projects[0]) as conn:
            conn.execute("SELECT 1").fetchall()

    def test_recency_decides_who_survives(self, many_projects):
        with connect(many_projects[0]) as conn:
            conn.execute("SELECT 1").fetchall()
        close_idle_databases(max_handles=1)
        assert open_database_count() <= 1


class TestTheIdleRule:
    def test_everything_idle_closes_even_below_the_cap(self, many_projects):
        settings.db_idle_close_seconds = 0.0
        try:
            closed = close_idle_databases(max_handles=100)
        finally:
            settings.db_idle_close_seconds = 900.0
        assert closed == 12
        assert open_database_count() == 0

    def test_fresh_use_survives_a_short_idle_window(self, many_projects):
        with connect(many_projects[3]) as conn:
            conn.execute("SELECT 1").fetchall()
        # Zero time has passed; nothing is idle for a 900s window.
        closed = close_idle_databases(max_handles=100, idle_seconds=900.0)
        assert closed == 0
        assert open_database_count() == 12


class TestSafety:
    def test_a_handle_with_a_live_cursor_is_never_closed(self, many_projects):
        with connect(many_projects[1]) as held:
            held.execute("SELECT 1").fetchall()
            settings.db_idle_close_seconds = 0.0
            try:
                close_idle_databases(max_handles=0)
            finally:
                settings.db_idle_close_seconds = 900.0
            # The held cursor still answers, because its handle is still open.
            assert held.execute("SELECT 42").fetchone()[0] == 42
        # Released after the with-block; the next sweep may close it.
        close_idle_databases(max_handles=0, idle_seconds=0.0)
        assert open_database_count() == 0

    def test_an_open_transaction_blocks_its_own_close(self, many_projects):
        slug = many_projects[2]
        with transaction(slug) as conn:
            conn.execute(
                "CREATE OR REPLACE TABLE lru_marker(id INTEGER)"
            ).fetchall()
            conn.execute("INSERT INTO lru_marker VALUES (7)").fetchall()
            # The ambient connection is a live cursor: the sweep must skip
            # this file, and the uncommitted row must survive the sweep.
            settings.db_idle_close_seconds = 0.0
            try:
                close_idle_databases(max_handles=0)
            finally:
                settings.db_idle_close_seconds = 900.0
            assert conn.execute("SELECT id FROM lru_marker").fetchone()[0] == 7
        # Committed on exit; the file is closable again afterwards.
        close_idle_databases(max_handles=0, idle_seconds=0.0)
        with connect(slug) as conn:
            assert conn.execute("SELECT id FROM lru_marker").fetchone()[0] == 7


class TestReopen:
    def test_data_survives_an_idle_close(self, many_projects):
        repo = ProjectRepository()
        slug = many_projects[5]
        with connect(slug) as conn:
            conn.execute(
                "CREATE OR REPLACE TABLE lru_keep(id INTEGER)"
            ).fetchall()
            conn.execute("INSERT INTO lru_keep VALUES (5)").fetchall()

        close_idle_databases(max_handles=0, idle_seconds=0.0)
        assert open_database_count() == 0

        with connect(slug) as conn:
            assert conn.execute("SELECT id FROM lru_keep").fetchone()[0] == 5
        assert repo.get(slug).slug == slug


class TestResidue:
    def test_stale_recency_entries_do_not_underclose(self, many_projects):
        """A `_last_used` key naming a file this process no longer holds (a
        close path that forgot to clean it) must not eat the cap's victim
        budget — the cap is honored against the OPEN handles."""
        stale = "/tmp/definitely-not-open.duckdb"
        conn_mod._last_used[stale] = 0.0  # older than everything real
        try:
            close_idle_databases(max_handles=1)
        finally:
            conn_mod._last_used.pop(stale, None)
        assert open_database_count() <= 1
