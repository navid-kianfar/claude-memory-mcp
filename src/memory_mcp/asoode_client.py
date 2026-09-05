"""REST client for asoode: the outbound half of the bridge.

Every asoode route is a POST with a JSON body - no query strings anywhere - and
every response is an `OperationResult` envelope, `{status, data, message?}`, where
status 2 is success and anything else is a failure the HTTP status code will not
tell you about (a 201 can still carry status 7, Validation). `_post` collapses
both failure modes into one `AsoodeError`, so callers never inspect an envelope.

Auth is the machine-wide PAT from `memory_mcp.asoode` - `Authorization: Bearer
asoode_pat_…`. `from_settings()` is the only constructor most callers want; it
resolves the endpoints and the token together and fails loudly if no PAT is set.

Idempotency is the reason this can be re-run safely: work packages, lists and
tasks all take a caller-supplied `externalRef`, unique per parent, and repeating a
create with the same value returns the existing row instead of a duplicate. The
bridge passes the local UUID, so "push everything again" is a no-op rather than a
second copy of the board.
"""

from typing import Any

import httpx

from memory_mcp.providers.base import (
    ProviderAuthError, ProviderError, TransientProviderError,
)

from memory_mcp.asoode import get_endpoints, get_pat

# asoode's BoardTemplate enum (app.enum.ts:80).
BOARD_KANBAN = 5

# asoode's WorkPackageTaskState (app.enum.ts:283) keyed by the local TaskState
# value - the two vocabularies are the same list, which is why this is a plain
# lookup and not a translation with a fallback.
STATE_TO_ORDINAL = {
    "todo": 1,
    "in_progress": 2,
    "done": 3,
    "paused": 4,
    "blocked": 5,
    "cancelled": 6,
    "duplicate": 7,
    "incomplete": 8,
    "blocker": 9,
}
ORDINAL_TO_STATE = {v: k for k, v in STATE_TO_ORDINAL.items()}

# Local priority (0 = normal .. 3 = highest) onto asoode's five-step
# WorkPackageTaskObjectiveValue (app.enum.ts:301, 1 = BarelyValuable .. 5 =
# ExtremelyValuable). 0 maps to asoode's own default (schema.prisma: @default(1))
# so an untouched local task and an untouched remote one agree.
PRIORITY_TO_OBJECTIVE = {0: 1, 1: 2, 2: 4, 3: 5}
OBJECTIVE_TO_PRIORITY = {1: 0, 2: 1, 3: 1, 4: 2, 5: 3}

# OperationResultStatus (packages/shared/src/enums/core.enum.ts) - 2 is Success;
# the rest are why not. Verified against the enum on 2026-09-05: an earlier copy
# of this table was off by one for 1, 3 and 4, so a Duplicate read as "access
# denied" and every tolerant call site that matched on the string failed.
STATUS_PENDING = 1
STATUS_SUCCESS = 2
STATUS_NOT_FOUND = 3
STATUS_DUPLICATE = 4
_STATUS_MESSAGE = {
    STATUS_PENDING: "pending",
    STATUS_NOT_FOUND: "not found",
    STATUS_DUPLICATE: "already exists",
    5: "rejected",
    6: "unauthorized",
    7: "validation failed",
    8: "failed",
    9: "captcha required",
    10: "over capacity",
    11: "expired",
}


class AsoodeError(ProviderError):
    """An asoode call failed: unreachable, non-2xx, or a non-success envelope.

    Subclasses ProviderError so the flusher's retry logic treats every platform's
    failures alike, while the many existing `except AsoodeError` sites keep
    working unchanged. `status` carries the envelope's OperationResultStatus
    when there was one, so a caller that tolerates Duplicate or NotFound checks
    the NUMBER, never the message text.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class AsoodeAuthError(AsoodeError, ProviderAuthError):
    """The PAT is missing, revoked, expired, or not accepted."""


class AsoodeTransientError(AsoodeError, TransientProviderError):
    """asoode could not be reached, or answered 5xx. Retry later; do not count it."""


class AsoodeClient:
    def __init__(self, base_url: str, token: str, timeout: float = 20.0):
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    @property
    def base_url(self) -> str:
        return self._base

    @classmethod
    def from_settings(cls, timeout: float = 20.0) -> "AsoodeClient":
        """Build a client from the machine-wide endpoint + PAT configuration."""
        endpoints = get_endpoints()
        token = get_pat(endpoints.api_url)
        if not token:
            raise AsoodeAuthError(
                f"No asoode PAT stored for {endpoints.api_url}. Set it once with "
                "`memory-mcp asoode set-pat` - it then covers every project."
            )
        return cls(endpoints.api_url, token, timeout)

    # ---------- transport ----------

    def _post(self, path: str, body: dict | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            with httpx.Client(timeout=self._timeout) as client:
                resp = client.post(self._base + path, json=body or {}, headers=headers)
        except httpx.HTTPError as e:
            raise AsoodeTransientError(f"asoode unreachable ({self._base}): {e}") from e

        if resp.status_code in (401, 403):
            raise AsoodeAuthError(
                f"asoode rejected the PAT ({resp.status_code}) on {path}. "
                "It may be revoked or expired - reissue it in Profile → Access "
                "Tokens and store it with `memory-mcp asoode set-pat`."
            )
        if resp.status_code >= 500:
            raise AsoodeTransientError(
                f"asoode {resp.status_code} on {path}: {resp.text[:300]}"
            )
        if resp.status_code >= 400:
            raise AsoodeError(f"asoode {resp.status_code} on {path}: {resp.text[:300]}")

        try:
            payload = resp.json()
        except ValueError as e:
            raise AsoodeError(f"asoode returned non-JSON on {path}") from e

        # The envelope. A 2xx with status != 2 is still a failure.
        if isinstance(payload, dict) and "status" in payload:
            status = payload.get("status")
            if status != STATUS_SUCCESS:
                reason = _STATUS_MESSAGE.get(status, f"status {status}")
                detail = payload.get("message") or payload.get("errors") or ""
                raise AsoodeError(
                    f"asoode {path}: {reason}{f' - {detail}' if detail else ''}",
                    status=status if isinstance(status, int) else None,
                )
            return payload.get("data")
        return payload

    # ---------- projects ----------

    def list_projects(self) -> list[dict]:
        return self._post("/projects/list") or []

    def find_project_by_title(self, title: str) -> dict | None:
        """Projects have no externalRef, so identity is the title.

        Only used to make `bootstrap` re-runnable; the work package below is
        keyed properly and is what the bridge actually stores.
        """
        wanted = title.strip().lower()
        for project in self.list_projects():
            if (project.get("title") or "").strip().lower() == wanted:
                return project
        return None

    def create_project(
        self, title: str, description: str = "", *, complex_: bool = False,
        board_template: int = BOARD_KANBAN,
    ) -> dict:
        return self._post("/projects/create", {
            "title": title,
            "description": description,
            "complex": complex_,
            "boardTemplate": board_template,
        })

    def fetch_project(self, project_id: str) -> dict:
        return self._post(f"/projects/{project_id}/fetch")

    # ---------- work packages ----------

    def create_work_package(
        self, project_id: str, title: str, *, description: str = "",
        external_ref: str | None = None, board_template: int = BOARD_KANBAN,
    ) -> dict:
        """Create a board. Returns the full board, lists included.

        With `external_ref` this is idempotent: the same key returns the board
        that already exists rather than adding a second one.
        """
        body: dict = {
            "title": title,
            "description": description,
            "boardTemplate": board_template,
        }
        if external_ref:
            body["externalRef"] = external_ref
        return self._post(f"/work-packages/create/{project_id}", body)

    def find_work_package(self, external_ref: str) -> dict | None:
        """Locate an existing board by its externalRef, across every project.

        The ref is unique per project, not globally, so a duplicate ref in two
        projects would be ambiguous - the first match wins and the caller can
        pass a work-package id instead when that matters. Reads only: attaching
        to a board must never create one.
        """
        wanted = (external_ref or "").strip()
        if not wanted:
            return None
        for project in self.list_projects():
            for board in project.get("workPackages") or []:
                if (board.get("externalRef") or "") == wanted:
                    return {**board, "projectId": board.get("projectId") or project["id"]}
        return None

    def list_work_packages(self, project_id: str | None = None) -> list[dict]:
        """Every board the token can see, optionally within one project."""
        boards = []
        for project in self.list_projects():
            if project_id and project["id"] != project_id:
                continue
            for board in project.get("workPackages") or []:
                boards.append({
                    "id": board.get("id"),
                    "title": board.get("title"),
                    "external_ref": board.get("externalRef"),
                    "project_id": project["id"],
                    "project_title": project.get("title"),
                })
        return boards

    def fetch_work_package(self, package_id: str) -> dict:
        return self._post(f"/work-packages/fetch/{package_id}")

    # ---------- tasks ----------

    def create_task(
        self, list_id: str, title: str, *, description: str = "",
        external_ref: str | None = None, parent_id: str | None = None,
        assign_self: bool = True, assignees: list[str] | None = None,
    ) -> dict:
        """Create a task. Returns the full task, `id` included.

        `assign_self` defaults to True on purpose: `my_tasks`/kartabl filters on
        TaskMember, so a task created with no assignee is invisible in the
        creator's own list - the single most surprising behaviour of this API.
        """
        body: dict = {"title": title, "listId": list_id}
        if description:
            body["description"] = description
        if external_ref:
            body["externalRef"] = external_ref
        if parent_id:
            body["parentId"] = parent_id
        if assignees:
            body["assignees"] = assignees
        elif assign_self:
            body["assignSelf"] = True
        return self._post(f"/tasks/{list_id}/create", body)

    def reposition(self, task_id: str, list_id: str, order: int = 0) -> Any:
        """Move a task into a board column.

        Needed alongside change_state because asoode keeps `state` and `listId`
        independent: setting state alone leaves a Done card sitting in the To Do
        column, which is what a human actually looks at.
        """
        return self._post(
            f"/tasks/{task_id}/reposition", {"listId": list_id, "order": order}
        )

    def comment(self, task_id: str, message: str, private: bool = False) -> Any:
        return self._post(
            f"/tasks/{task_id}/comment", {"message": message, "private": private}
        )

    def change_state(self, task_id: str, state: str | int) -> Any:
        ordinal = state if isinstance(state, int) else STATE_TO_ORDINAL.get(state)
        if not ordinal:
            raise AsoodeError(f"unknown task state: {state!r}")
        return self._post(f"/tasks/{task_id}/change-state", {"state": ordinal})

    def change_priority(self, task_id: str, objective_value: int) -> Any:
        return self._post(
            f"/tasks/{task_id}/change-priority", {"objectiveValue": objective_value}
        )

    def change_description(self, task_id: str, description: str) -> Any:
        return self._post(
            f"/tasks/{task_id}/change-description", {"description": description}
        )

    def change_title(self, task_id: str, title: str) -> Any:
        """POST /tasks/:id/change-title {title} (tasks.controller.ts:101)."""
        return self._post(f"/tasks/{task_id}/change-title", {"title": title})

    def set_estimate(self, task_id: str, minutes: int) -> Any:
        """POST /tasks/:id/estimated {estimatedTime} - MINUTES, which is what
        the board's formatMinutes() renders (WpBoardCard.tsx:298)."""
        return self._post(f"/tasks/{task_id}/estimated", {"estimatedTime": int(minutes)})

    def task_detail(self, task_id: str) -> dict:
        """POST /tasks/:id/detail - the full view model: members, labels,
        attachments, timeSpents, subTasks, externalRef."""
        return self._post(f"/tasks/{task_id}/detail") or {}

    def find_task_by_external_ref(self, package_id: str, external_ref: str) -> dict | None:
        """POST /tasks/by-external-ref {packageId, externalRef}, or None."""
        try:
            return self._post(
                "/tasks/by-external-ref",
                {"packageId": package_id, "externalRef": external_ref},
            ) or None
        except AsoodeError as e:
            if e.status == STATUS_NOT_FOUND:
                return None
            raise

    def convert_to_task(self, task_id: str) -> Any:
        """POST /tasks/:id/convert-to-task - a sub-task becomes top-level."""
        return self._post(f"/tasks/{task_id}/convert-to-task")

    def add_task_member(self, task_id: str, record_id: str, is_group: bool = False) -> Any:
        """POST /tasks/:id/member/add {recordId, isGroup}. A repeat is
        reported as `already exists` by the service, which callers treat as done."""
        try:
            return self._post(
                f"/tasks/{task_id}/member/add",
                {"recordId": record_id, "isGroup": bool(is_group)},
            )
        except AsoodeError as e:
            # TaskMember is unique per (task, recordId) and the service answers
            # Duplicate - the state we wanted. The PAT owner is a member of every
            # card the flusher creates (assignSelf), so this is the common case.
            if e.status == STATUS_DUPLICATE:
                return None
            raise

    def remove_task_member(self, task_id: str, record_id: str) -> Any:
        """POST /tasks/:taskId/member/:id/remove - `:id` is the user/group
        recordId, not the TaskMember row (tasks.service.ts, BUG-MEMBER-01)."""
        return self._post(f"/tasks/{task_id}/member/{record_id}/remove")

    def remove_attachment(self, attachment_id: str) -> Any:
        """POST /tasks/attachment/:id/remove."""
        return self._post(f"/tasks/attachment/{attachment_id}/remove")

    def attach(self, task_id: str, filename: str, content: bytes,
               content_type: str | None = None) -> Any:
        """POST /tasks/:taskId/attach - multipart, and the field is `files`.

        Not `file`: the controller uses FileInterceptor('files')
        (tasks.controller.ts:281), and the wrong field name is silently accepted
        as an empty upload.
        """
        headers = {"Authorization": f"Bearer {self._token}", "Accept": "application/json"}
        files = {"files": (filename, content, content_type or "application/octet-stream")}
        try:
            with httpx.Client(timeout=max(self._timeout, 60.0)) as client:
                resp = client.post(
                    f"{self._base}/tasks/{task_id}/attach", files=files, headers=headers,
                )
        except httpx.HTTPError as e:
            raise AsoodeTransientError(f"asoode unreachable ({self._base}): {e}") from e
        if resp.status_code in (401, 403):
            raise AsoodeAuthError(f"asoode rejected the PAT ({resp.status_code}) on attach")
        if resp.status_code >= 500:
            raise AsoodeTransientError(f"asoode {resp.status_code} on attach: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise AsoodeError(f"asoode {resp.status_code} on attach: {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def archive_task(self, task_id: str, archived: bool = True) -> Any:
        """Take a task off the board, or restore it.

        An empty body toggles; sending {archived} sets it absolutely, which is
        what a mirror needs - a retried flush must land on the same state rather
        than flipping it back.
        """
        return self._post(f"/tasks/{task_id}/archive", {"archived": bool(archived)})

    def archive_list_tasks(self, list_id: str) -> Any:
        """Archive EVERY unarchived task in one list, in a single call.

        POST /work-packages/lists/:id/archive-tasks. Server-side it is
        updateMany({listId, archivedAt: null} -> archivedAt = now), so it is a
        soft delete and reversible per task.

        Use it to clear a finished column rather than looping archive_task over
        forty cards. NOT to be confused with lists/:id/clear-tasks, which is the
        destructive sibling.

        It archives what is IN the list regardless of state, so only point it at
        a column whose contents you have actually looked at.
        """
        return self._post(f"/work-packages/lists/{list_id}/archive-tasks", {})

    def spend_time(self, task_id: str, begin: str, end: str | None = None) -> Any:
        """Log a stretch of work. SpendTimeDto is {begin, end?} (task.dto.ts:116)."""
        body: dict = {"begin": begin}
        if end:
            body["end"] = end
        return self._post(f"/tasks/{task_id}/spend-time", body)

    def set_dates(self, task_id: str, **dates) -> Any:
        """`beginAt` / `endAt` / `dueAt`, ISO strings. Omitted fields are untouched."""
        body = {k: v for k, v in dates.items() if v is not None}
        return self._post(f"/tasks/{task_id}/set-date", body)

    def create_label(self, package_id: str, title: str,
                     color: str = "#6366f1") -> Any:
        """POST /work-packages/labels/:packageId/create -> the new label."""
        return self._post(
            f"/work-packages/labels/{package_id}/create",
            {"title": title, "color": color, "darkColor": False},
        )

    def add_task_label(self, task_id: str, label_id: str) -> Any:
        return self._post(f"/tasks/{task_id}/label/add/{label_id}")

    def remove_task_label(self, task_id: str, label_id: str) -> Any:
        return self._post(f"/tasks/{task_id}/label/{label_id}/remove")

    def task_changes(self, since: str, cursor: str | None = None,
                     take: int | None = None, package_id: str | None = None) -> Any:
        """POST /tasks/changes - what changed since an instant, across EVERY
        work package this token can reach.

        Not to be confused with `kartabl`, which filters on TaskMember and so
        only ever returns tasks ASSIGNED to this user. A catch-up must see a task
        somebody else created and left unassigned, so kartabl cannot serve it.

        Returns {changes, nextCursor, syncedAt}. `nextCursor` absent means the
        end - an empty `changes` does NOT, so page on the cursor. `syncedAt` is
        the watermark to use as the next `since` once pages are exhausted.

        Omitting package_id is the point: one call covers all boards.
        """
        body: dict = {"since": since}
        if cursor:
            body["cursor"] = cursor
        if take:
            body["take"] = take
        if package_id:
            body["packageId"] = package_id
        return self._post("/tasks/changes", body)

    def kartabl(self, **filters) -> Any:
        """Tasks assigned to this token's user.

        `updatedSince` makes it a change feed, which is what the inbound
        reconcile poll uses; `states`, `take` and `skip` narrow it further.
        """
        body = {k: v for k, v in filters.items() if v is not None}
        return self._post("/tasks/kartabl", body)

    def socket_ticket(self) -> dict:
        """Exchange the PAT for a short-lived socket ticket.

        The realtime gateway has no database by design and verifies signed JWTs
        only, so a raw PAT is REJECTED there - it authenticates the REST API and
        nothing else (main.gateway.ts:55-90). Returns {token, userId, expiresAt};
        the ticket expires, so it is fetched fresh on every connect rather than
        cached.
        """
        return self._post("/account/socket-token") or {}

    def whoami(self) -> dict | None:
        """Best-effort identity check, used by `asoode check` to prove the PAT works."""
        try:
            return self._post("/account/profile")
        except AsoodeError:
            return None
