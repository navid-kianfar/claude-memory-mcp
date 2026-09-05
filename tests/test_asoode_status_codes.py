"""asoode's OperationResultStatus, matched by number.

The message table was off by one for a day: a Duplicate rendered as "access
denied", and every call site that tolerated a repeat by matching the string
failed instead. These pin the enum (packages/shared/src/enums/core.enum.ts) and
the two tolerant paths.
"""

import pytest

from memory_mcp.asoode_client import (
    STATUS_DUPLICATE, STATUS_NOT_FOUND, AsoodeClient, AsoodeError, _STATUS_MESSAGE,
)


def test_the_status_table_matches_the_enum():
    assert _STATUS_MESSAGE[STATUS_NOT_FOUND] == "not found"
    assert _STATUS_MESSAGE[STATUS_DUPLICATE] == "already exists"
    assert STATUS_NOT_FOUND == 3 and STATUS_DUPLICATE == 4
    assert _STATUS_MESSAGE[1] == "pending"


def _client_raising(status):
    client = AsoodeClient("https://api.example", "tok")
    client._post = lambda path, body=None: (_ for _ in ()).throw(
        AsoodeError(f"asoode {path}: {_STATUS_MESSAGE[status]}", status=status)
    )
    return client


def test_a_duplicate_member_is_the_state_we_wanted():
    assert _client_raising(STATUS_DUPLICATE).add_task_member("t1", "u1") is None


def test_a_rejected_member_add_still_raises():
    with pytest.raises(AsoodeError):
        _client_raising(5).add_task_member("t1", "u1")


def test_by_external_ref_answers_none_on_not_found():
    assert _client_raising(STATUS_NOT_FOUND).find_task_by_external_ref("wp", "x") is None


def test_by_external_ref_raises_on_anything_else():
    with pytest.raises(AsoodeError):
        _client_raising(STATUS_DUPLICATE).find_task_by_external_ref("wp", "x")
