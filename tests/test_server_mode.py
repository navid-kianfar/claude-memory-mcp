"""Server-mode governance behavior: approval gating, org-wide rules, per-scope
mix, approve/revoke, and per-user isolation. Local mode is exercised by the rest
of the suite; these tests flip settings.mode to 'server'."""

import uuid

import pytest

from memory_mcp.config import settings
from memory_mcp.container import container
from memory_mcp.context import RequestUser, reset_request_user, set_request_user
from memory_mcp.db import registry as R
from memory_mcp.exceptions import ForbiddenError
from memory_mcp.models import GLOBAL_PROJECT_SLUG, StoreMemoryRequest


@pytest.fixture
def server_mode():
    original = settings.mode
    settings.mode = "server"
    container.rules_service._cache.clear()
    yield
    settings.mode = original
    container.rules_service._cache.clear()


def _as(user: RequestUser):
    return set_request_user(user)


ADMIN = RequestUser(id="admin-1", username="admin", role="admin")
BOB = RequestUser(id="bob-1", username="bob", role="member")


def _add_rule(project, title, user=BOB):
    tok = _as(user)
    try:
        return container.memory_service.store(
            StoreMemoryRequest(
                project=project, category="mandatory_rules", title=title, content=title
            )
        )
    finally:
        reset_request_user(tok)


def test_own_project_rule_auto_approved(server_mode):
    tok = _as(BOB)
    try:
        container.project_service.init_project("bobproj", "Bob")  # Bob becomes owner
    finally:
        reset_request_user(tok)
    m = _add_rule("bobproj", "own", BOB)
    assert m.approval_status == "approved"
    assert m.created_by == BOB.id


def test_foreign_project_rule_is_proposed_and_not_enforced(server_mode):
    tok = _as(ADMIN)
    try:
        container.project_service.init_project("shared", "Shared")  # admin owns it
    finally:
        reset_request_user(tok)
    m = _add_rule("shared", "foreign", BOB)  # Bob is not owner/admin
    assert m.approval_status == "proposed"
    # Proposed rule must NOT be enforced.
    container.rules_service._cache.clear()
    rules = container.rules_service.get_rules("shared")
    assert "foreign" not in [r.title for r in rules.mandatory_rules]


def test_approve_enforces_and_revoke_unenforces(server_mode):
    tok = _as(ADMIN)
    try:
        container.project_service.init_project("shared2", "Shared2")
    finally:
        reset_request_user(tok)
    m = _add_rule("shared2", "needs-approval", BOB)
    assert m.approval_status == "proposed"

    tok = _as(ADMIN)
    try:
        container.memory_service.approve_rule("shared2", m.id)
    finally:
        reset_request_user(tok)
    container.rules_service._cache.clear()
    assert "needs-approval" in [
        r.title for r in container.rules_service.get_rules("shared2").mandatory_rules
    ]

    tok = _as(ADMIN)
    try:
        container.memory_service.revoke_rule("shared2", m.id)
    finally:
        reset_request_user(tok)
    container.rules_service._cache.clear()
    assert "needs-approval" not in [
        r.title for r in container.rules_service.get_rules("shared2").mandatory_rules
    ]


def test_member_cannot_approve(server_mode):
    tok = _as(ADMIN)
    try:
        container.project_service.init_project("shared3", "Shared3")
    finally:
        reset_request_user(tok)
    m = _add_rule("shared3", "r", BOB)
    tok = _as(BOB)
    try:
        with pytest.raises(ForbiddenError):
            container.memory_service.approve_rule("shared3", m.id)
    finally:
        reset_request_user(tok)


def test_org_wide_rules_inject_into_every_project_when_approved(server_mode):
    tok = _as(ADMIN)
    try:
        container.project_service.init_project("p1", "P1")
        # Admin creates an org rule -> auto-approved (admin authority).
        org = container.memory_service.store(
            StoreMemoryRequest(
                project=GLOBAL_PROJECT_SLUG, category="mandatory_rules",
                title="org-rule", content="x",
            )
        )
    finally:
        reset_request_user(tok)
    assert org.approval_status == "approved"
    container.rules_service._cache.clear()
    titles = [r.title for r in container.rules_service.get_rules("p1").mandatory_rules]
    assert "org-rule" in titles  # merged into the project's block


def test_per_user_active_project_isolation(server_mode):
    from memory_mcp.context import get_active_project, set_active_project

    tok = _as(ADMIN)
    try:
        set_active_project("admin-proj")
        assert get_active_project() == "admin-proj"
    finally:
        reset_request_user(tok)

    tok = _as(BOB)
    try:
        set_active_project("bob-proj")
        assert get_active_project() == "bob-proj"  # not clobbered by admin
    finally:
        reset_request_user(tok)

    # Admin still sees their own.
    tok = _as(ADMIN)
    try:
        assert get_active_project() == "admin-proj"
    finally:
        reset_request_user(tok)


def test_token_auth_round_trip(server_mode):
    admin, token = R.create_user("alice-" + uuid.uuid4().hex[:6], role="admin")
    assert R.authenticate_token(token)["id"] == admin["id"]
    assert R.authenticate_token("bogus") is None
