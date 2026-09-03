# Asoode — codebase analysis for the Claude bridge

> Captured 2026-09-03 by an Opus analysis agent reading the source directly.
> Repo: `/Users/aslan_nejad/Desktop/DEV/achasoft/asoode` (pnpm + turbo monorepo).
> **Purpose of this file: so we never have to re-read the asoode repo to design the bridge.**
> Line numbers are from that day's working tree — treat as a strong hint, re-verify before editing.

## 0. Shape

`apps/{backend,cms,frontend,mcp,socket,website,worker}` + `packages/shared`.
NestJS + Prisma + PostgreSQL. Frontend is **React 19 + Vite 6 + Tailwind 4 + zustand**
(the root `README.md:63-67` still claims Vue 3 / Vuetify / Pinia — stale, ignore it).

`apps/backend`, `apps/socket`, `apps/worker` ship as **bytecode-compiled single binaries**
(no `node_modules`, no `dist/`) — see `docs/protected-builds.md`. Migrations therefore run
from a separate `kianfar/asoode-migrator` image. `apps/mcp` is the only app shipping plain JS.

---

## 1. `apps/mcp` — the existing MCP server

Self-contained ESM package `@asoode/mcp`. **Zero runtime coupling to `@asoode/shared`** — enums and
endpoint paths are hand-mirrored (`apps/mcp/src/asoode/enums.ts`, `apps/mcp/src/asoode/endpoints.ts`).
Deps: `@modelcontextprotocol/sdk ^1.12.0`, `express`, `zod`, `dotenv`. ~848 LOC.

It is a **thin stateless adapter**: every tool call = exactly one `POST` to the backend REST API.
No DB, no state, no cache.

### 1.1 Registered tools (20)

`registerTools()` at `apps/mcp/src/tools.ts:42`. The 9 read tools always register, then
`if (opts.readOnly) return;` at `tools.ts:206` gates the remaining 11.

**Read (`readOnlyHint: true`)**

| Tool | Input | Backend call | Line |
|---|---|---|---|
| `list_projects` | `{archived=false}` | `POST /projects/list` \| `/projects/archived` | `:44` |
| `get_project` | `{projectId}` | `POST /projects/:id/fetch` | `:63` |
| `get_project_tree` | `{projectId}` | `POST /projects/tree/:id` | `:80` |
| `list_board` | `{workPackageId}` | `POST /work-packages/fetch/:id` | `:97` |
| `get_task` | `{taskId}` | `POST /tasks/:id/detail` | `:115` |
| `my_tasks` | `{}` | `POST /tasks/kartabl` | `:132` |
| `my_calendar` | `{from, to}` (both required) | `POST /tasks/calendar` | `:149` |
| `search` | `{query}` | `POST /search` | `:169` |
| `list_time_entries` | `{from?, to?}` | `POST /times/mine` | `:186` |

**Write**

| Tool | Input | Backend call | Line |
|---|---|---|---|
| `create_work_package` | `{projectId, title, description?, boardTemplate='kanban', subProjectId?}` | `POST /work-packages/create/:projectId` | `:209` |
| `create_list` | `{workPackageId, title, color?}` | `POST /work-packages/:id/lists/create` | `:240` |
| `create_task` | `{listId, title, parentId?}` | `POST /tasks/:listId/create` | `:262` |
| `update_task_state` | `{taskId, state}` | `POST /tasks/:id/change-state` | `:284` |
| `rename_task` | `{taskId, title}` | `POST /tasks/:id/change-title` | `:305` |
| `set_task_dates` | `{taskId, beginAt?, endAt?, dueAt?}` (≥1 enforced client-side `:342`) | `POST /tasks/:id/set-date` | `:326` |
| `assign_task` | `{taskId, recordId, isGroup=false}` | `POST /tasks/:id/member/add` | `:353` |
| `comment_on_task` | `{taskId, message, private?}` | `POST /tasks/:id/comment` | `:375` |
| `log_time` | `{taskId, begin, end?}` | `POST /tasks/:id/spend-time` | `:397` |
| `move_task` | `{taskId, packageId, listId}` | `POST /tasks/:id/move` | `:419` |
| `archive_task` | `{taskId, confirm=false}` (dry-run unless confirm) | `POST /tasks/:id/archive` | `:442` |

**Result format:** one text block of pretty-printed JSON; write tools prefix a one-line summary
(`tools.ts:20-23`). Errors → `isError: true` with an English message (`tools.ts:25-31`).

**No resources, no prompts.** `buildServer()` (`apps/mcp/src/server.ts:18-26`) calls only
`registerTools`. The only non-tool surface is the server `instructions` string (`server.ts:5-7`).

### 1.2 Transports

- **stdio** — `src/stdio.ts`. Requires `ASOODE_PAT` or exits 1 (`:10-13`). Entry `node dist/stdio.js`.
- **Streamable HTTP** — `src/http.ts`. Express + `StreamableHTTPServerTransport`, session map keyed by
  `mcp-session-id` (`:16`). `POST /mcp` (`:27`), `GET`/`DELETE /mcp` share `sessionRequest` (`:79-90`),
  `GET /health` → `{ok, sessions}` (`:23`). A new session must be an `initialize` request (`:33`)
  **and** carry `Authorization: Bearer …` or HTTP 401 (`:42-48`). Token bound per-session (`:65-71`).
  Listens on `cfg.httpPort`. No legacy SSE transport.

### 1.3 Config (`src/config.ts:15-25`)

| Var | Default |
|---|---|
| `MCP_TRANSPORT` | `stdio` (`'http'` string-compare, anything else → stdio) |
| `ASOODE_API_URL` | `http://localhost:3000` (trailing slashes stripped) |
| `ASOODE_PAT` | — (stdio only) |
| `MCP_HTTP_PORT` | `8030` |
| `MCP_READ_ONLY` | `false` (`=== 'true'`) |
| `MCP_REQUEST_TIMEOUT_MS` | `20000` |
| `LOG_LEVEL` | `info` |

Logging is **stderr-only** (`logger.ts:11`) — correct for stdio.
Dockerfile: node:22-alpine, `MCP_TRANSPORT=http`, `EXPOSE 8030`, `CMD ["node","dist/http.js"]`,
healthcheck on `/health`, plus a build-time gate that fails if a native addon enters the prod closure.

### 1.4 Auth

**PAT forwarded verbatim.** The MCP never mints, stores, or refreshes anything.
`createRestClient(baseUrl, token, timeoutMs)` sets `Authorization: Bearer ${token}` on every request
(`src/rest.ts:36,44-47`). stdio takes it from `ASOODE_PAT`; http takes it per-session from the
`initialize` request.

**Error mapping (`rest.ts:54-101`):** HTTP 401 → `unauthorized`, 403 → `forbidden` (real codes because
the guard runs before the response interceptor); everything else is an `OperationResult` envelope whose
`status !== 2` maps to prose via `STATUS_MESSAGE` (`rest.ts:20-29`) with field errors appended.
`AbortError` → timeout message.

### 1.5 Documented-vs-actual discrepancies (traps)

- `README.md:78-79` claims a `['read']`-scoped token makes write tools "return a permission error".
  **False** — see §5.3. A read-only PAT can create, move and archive tasks.
- `tools.ts:84` says `get_project_tree` returns "all boards and tasks at once". **False** —
  `/projects/tree/:id` returns `TreeViewModel`, a map of aggregate counters keyed by sub-project
  (`packages/shared/src/models/projects/project.model.ts:74-88`; impl `projects.service.ts:1215-1300`).
  **No task objects, no list IDs.** The `create_task` hint at `tools.ts:266` ("get the listId from
  list_board or get_project_tree") is wrong for the second option. List IDs come from `get_project`
  (`projects.service.ts:205-214`) or `list_board` (`work-packages.service.ts:488-519`).
- `tools.ts:136` says `my_tasks` returns tasks "assigned to **or created by**" the user. **False** —
  `kartabl` filters on `members: { some: { recordId: userId } }` only (`tasks.service.ts:2250-2253`).
  A task you create is invisible there unless you also assign yourself.

---

## 2. Data model

PostgreSQL via Prisma. `apps/backend/prisma/schema.prisma` (899 lines, 5 migrations).

**Every primary key is `String @id @default(uuid()) @db.Uuid`** — v4 UUID, server-generated.
No int IDs, no nanoid, no slug, **no client-supplied ID on any create path**.

### 2.1 Entities

| Concept | Model | Line | Notes |
|---|---|---|---|
| Organization | `Group` | `:165` | self-hierarchy (`parentId`/`rootId`), `type` GroupType 1-12, quotas |
| Org membership | `GroupMember` | `:216` | `@@unique([userId, groupId])` |
| **Project** | `Project` | `:245` | `userId` owner, `groupId?`, `title`, `description`, `complex`, `archivedAt` |
| Project membership | `ProjectMember` | `:276` | `recordId` (user **or** group), `access`, `isGroup`, `@@unique([recordId, projectId])` |
| Sub-project | `SubProject` | `:292` | self-nesting, `level`, `order` |
| **"Sprint"** | `ProjectSeason` | `:309` | `projectId`, `title`, `description` — **that is all**. No dates, no state |
| Objective | `WorkPackageObjective` | `:457` | MustHave/ShouldHave/NiceToHave |
| **WorkPackage (board)** | `WorkPackage` | `:324` | `projectId`, `subProjectId?`, `order`, `color`, `boardTemplate`, ~13 `permission*` ints, ~14 `allow*` bools, `beginAt/endAt/actualBeginAt/actualEndAt` |
| WP membership | `WorkPackageMember` | `:393` | `@@unique([recordId, packageId])` |
| **Status / Column** | `WorkPackageList` | `:424` | `packageId`, `title`, `order`, `color`, `darkColor` |
| **Label** | `WorkPackageLabel` | `:409` | **per-board**, not global |
| **Task** | `WorkPackageTask` | `:506` | see below |
| Assignment | `TaskMember` | `:561` | `@@unique([taskId, recordId])`, `isGroup` |
| Task↔label | `TaskLabel` | `:577` | `@@unique([taskId, labelId])` |
| Comment | `TaskComment` | `:616` | `replyId?`, `private`, `message` |
| Attachment | `TaskAttachment` | `:592` | `type` Link=1/Upload=2, `isCover` |
| Time | `TaskTimeSpent` | `:650` | `begin`, `end?`, `manual` |
| Vote | `TaskVote` | `:632` | `@@unique([taskId, userId])` |
| Custom field | `CustomField`/`CustomFieldValue` | `:473`/`:490` | `@@unique([fieldId, taskId])` |
| Blocker | `TaskBlocker` | `:871` | `blockedId`/`blockerId` |
| Relation | `TaskRelation` | `:886` | type 1=RelatesTo, 2=DuplicateOf, 3=ChildOf |
| **Audit log** | `ActivityLog` | `:815` | `userId, type, description, entityId?, entityType?, taskId?, createdAt` |
| **User** | `User` | `:10` | `email`/`username` unique, `working*Id` current-focus fields |
| **Machine credential** | `PersonalAccessToken` | `:112` | `tokenHash` sha256 unique, `tokenPrefix`, `last4`, `scopes String[]`, `expiresAt?`, `lastUsedAt?`, `revokedAt?` |

### 2.2 `WorkPackageTask` (`schema.prisma:506-559`)

```
id uuid PK | userId uuid (creator) | packageId uuid | projectId uuid | subProjectId uuid?
seasonId uuid? | listId uuid (FK→WorkPackageList) | parentId uuid? (self, sub-tasks)
title String | description String = "" | order Int = 0 | state Int = 1
geoLocation String? | dueAt/beginAt/endAt DateTime? | beginReminder/endReminder Int = 1
beginRemindedAt/endRemindedAt DateTime? | archivedAt DateTime? | doneAt DateTime? | doneUserId uuid?
coverUrl String? | coverId uuid? | estimatedTime Float = 0 | watching Bool | restricted Bool
votePaused/votePrivate Bool | voteNecessity Int = 1 | objectiveValue Int = 1
createdAt DateTime @default(now()) | updatedAt DateTime @updatedAt
```

**Denormalized parents:** `packageId`/`projectId`/`subProjectId` are copies kept in sync by the service
layer on **create** (`tasks.service.ts:420-431`) and **move** (`:825-833`) only.

### 2.3 Hierarchy

```
Group (org, self-nesting)
 └─ Project
     ├─ SubProject (self-nesting)
     ├─ ProjectSeason        ← "sprint": title/description only
     └─ WorkPackage (board)  ← subProjectId?
         ├─ WorkPackageLabel   (per-board)
         ├─ CustomField
         └─ WorkPackageList (column = status)
             └─ WorkPackageTask ← parentId? (sub-tasks)
                 └─ TaskMember / TaskLabel / TaskComment / TaskAttachment
                    / TaskTimeSpent / TaskVote / CustomFieldValue
```

### 2.4 Minimum to CREATE a task

`CreateTaskDto = { title: string; listId: string; parentId?: string }`
(`packages/shared/src/dto/task.dto.ts:1-5`); route takes `listId` from the path
(`tasks.controller.ts:37-44`).

So: **`POST /tasks/:listId/create` with `{"title": "…"}`.** Everything else is derived server-side
(`tasks.service.ts:420-431`): `userId` from the token, `packageId`/`projectId`/`subProjectId` from the
list's work package, `order` = max+1, `state` = 1 (ToDo), `description` = `""`.

**No global ValidationPipe** — `main.ts` never calls `useGlobalPipes` and DTOs are bare TS `interface`s
(erased at runtime). An empty/missing `title` reaches Prisma unchecked. The MCP's zod schemas are
currently the **only** input validation on the AI path.

### 2.5 Enums (`packages/shared/src/enums/app.enum.ts`)

- `WorkPackageTaskState` (`:273`): **ToDo 1, InProgress 2, Done 3, Paused 4, Blocked 5, Cancelled 6,
  Duplicate 7, Incomplete 8, Blocker 9**
- `AccessType` (`:26`): Owner 1, Admin 2, HiddenEditor 3, Editor 4, Visitor 5 —
  **lower number = more access**; checks are `access <= minAccess` (`tasks.service.ts:108`)
- `BoardTemplate` (`:80`): Blank 1, WeekDay 2, TeamMembers 3, Departments 4, Kanban 5
- `ActivityType` (`:112`): ~90 values; task events in the 800 range
- `PAT_SCOPES` (`:245`): `['read','write','account']`
- `OperationResultStatus` (`core.enum.ts:1-13`): Pending 1, **Success 2**, NotFound 3, Duplicate 4,
  Rejected 5, UnAuthorized 6, Validation 7, Failed 8, Captcha 9, OverCapacity 10, Expire 11

---

## 3. Backend HTTP API

`apps/backend/src/main.ts`: no global prefix, **CORS `origin: true, credentials: true`** (`:11`),
Swagger at `GET /docs` (`:21`), port `PORT || 3000`.

**Every route is `POST`.** Only exceptions: `GET /health` (`misc.controller.ts:11`) and the Google
OAuth `GET` routes. All parameters are path params or JSON body — **no query strings anywhere**
(`@Query(` appears only in `oauth.controller.ts`).

**Every route is guarded by default** — `AuthGuard` registered as `APP_GUARD` (`app.module.ts:90`);
opt out with `@Public()`.

**Every response is wrapped** by `ResponseInterceptor` (`app.module.ts:91`) into
`OperationResult<T> = {data, status, errors, exception}`. Critically,
`ResponseInterceptor.catchError` (`common/interceptors/response.interceptor.ts:24-40`)
**swallows HttpExceptions and returns HTTP 200** with `status: 6/3/8`. Only guard-thrown 401/403
produce real HTTP error codes. **`status === 2` is the only truth for success.**

### 3.1 Task routes (`apps/backend/src/modules/tasks/tasks.controller.ts`)

| Method+Path | Line | Body | Service | Access |
|---|---|---|---|---|
| `POST /tasks/:listId/create` | `:37` | `{title, listId, parentId?}` | `:398` | Editor |
| `POST /tasks/:id/detail` | `:46` | — | `:453` | Visitor |
| `POST /tasks/:id/convert-to-task` | `:51` | — | `:519` | Editor |
| `POST /tasks/:id/change-title` | `:56` | `{title}` | `:587` | Editor |
| `POST /tasks/:id/change-priority` | `:65` | `{objectiveValue}` | `:620` | Editor |
| `POST /tasks/:id/change-description` | `:74` | `{description}` | `:653` | Editor |
| `POST /tasks/:id/change-state` | `:83` | `{state}` | `:686` | Editor |
| `POST /tasks/:id/reposition` | `:92` | `{listId, order}` | `:748` | Editor |
| `POST /tasks/:id/move` | `:101` | `{packageId, listId}` | `:833` | Editor |
| `POST /tasks/:id/set-date` | `:110` | `{beginAt?, endAt?, dueAt?, beginReminder?, endReminder?}` | `:900` | Editor |
| `POST /tasks/:id/location` | `:119` | `{geoLocation}` | `:946` | Editor |
| `POST /tasks/:id/member/add` | `:130` | `{recordId, isGroup}` | `:981` | Editor |
| `POST /tasks/:taskId/member/:id/remove` | `:139` | — | `:1044` | Editor |
| `POST /tasks/:taskId/label/add/:labelId` | `:150` | — | `:1091` | Editor |
| `POST /tasks/:taskId/label/:labelId/remove` | `:159` | — | `:1152` | Editor |
| `POST /tasks/:id/comment` | `:230` | `{message, private?}` | `:1529` | Editor |
| `POST /tasks/:id/vote` | `:239` | `{vote}` | `:1595` | |
| `POST /tasks/:id/watch` | `:248` | — | `:1666` | |
| `POST /tasks/:id/estimated` | `:255` | `{estimatedTime}` | `:1704` | |
| `POST /tasks/:id/spend-time` | `:264` | `{begin, end?}` | `:1737` | |
| `POST /tasks/:id/toggle-timer` | `:273` | — | `:1907` | |
| `POST /tasks/time/:entryId/edit` \| `/delete` | `:278,287` | `{begin, end?}` | `:1798,1861` | |
| `POST /tasks/:taskId/custom-field/:fieldId/value` | `:297` | `{value}` | `:2060` | |
| `POST /tasks/:id/logs` | `:309` | — | `:2106` | Visitor |
| `POST /tasks/:id/archive` | `:314` | — | `:2150` | **Admin** |
| `POST /tasks/calendar` | `:321` | `{from, to}` | `:2199` | |
| `POST /tasks/kartabl` | `:329` | — (no body) | `:2248` | |

Attachments: `/tasks/attachment/:id/{rename,remove,cover}` (`:170,179,184`),
`/tasks/:id/bulk-attach` (`:193`), `/tasks/:taskId/attach` (`:218`).

### 3.2 The two "assigned to me" endpoints

**`POST /tasks/kartabl`** (`tasks.service.ts:2248-2276`):

```
where:   { archivedAt: null, members: { some: { recordId: userId } } }
orderBy: [{ state: 'asc' }, { updatedAt: 'desc' }]
```

Returns `{ tasks: WorkPackageTaskViewModel[] }`. **No `take`, no `skip`, no state filter, no date
filter, no request body at all.** Every non-archived task ever assigned to you, including all Done
ones, each with nested members/labels/attachments/votes/subTasks.

**`POST /tasks/calendar`** (`:2199-2246`): same member filter plus a date-range `OR` over
`dueAt`/`beginAt`/`endAt` (`:2211-2222`). Filters on **task dates, not `updatedAt`** — useless as a
change feed.

### 3.3 Pagination / filtering — global finding

- `@Query(` used nowhere outside OAuth.
- Prisma `take:`/`skip:` appears only as **hardcoded caps**: `misc.service.ts:271` (100), `:358` (50),
  `search.service.ts` (`SEARCH_LIMIT` ×6), `workflows.service.ts:333` (50), `messenger.service.ts`.
- **`grep -rn "updatedAt: {" apps/backend/src/modules/` returns zero hits.**
  There is no `updated_since` / delta filter on any endpoint in the codebase.

### 3.4 Project / work-package routes

`projects.controller.ts`: `list` `:18`, `archived` `:23`, `:id/fetch` `:28`, `create` `:33`,
`:id/edit` `:38`, `:id/sub/create` `:59`, `:id/season/create` `:93`, `season/:id/edit` `:102`,
`:id/add-access` `:118`, `objectives/:id` `:157`, `tree/:id` `:167`, `road-map/:id` `:172`,
`progress/:id` `:177`.

`work-packages.controller.ts`: `create/:projectId` `:19`, `fetch/:id` `:28`, `fetch/:id/archived` `:33`,
`:id/edit` `:38`, `:id/archive` `:52`, `:id/lists/create` `:68`, `lists/:id/rename` `:77`,
`lists/:id/reposition` `:95`, `labels/:id/create` `:130`, `labels/:id/rename` `:139`,
`:id/custom-fields/create` `:155`, `:id/add-access` `:230`, `:id/setting` `:269`,
`:id/permissions` `:326`.

Return-shape gotchas:
- `work-packages.service.ts:389-469` `create()` ends `return this.fetch(userId, wp.id)` —
  **returns the full board including auto-created lists**, so `create_work_package` does hand back list IDs.
- `work-packages.service.ts:881-930` `createList()` ends `return OperationResult.Success(true)` —
  **returns `true`, not the list.** `create_list` cannot give the model the new `listId`;
  a board re-fetch is mandatory.
- `tasks.service.ts:398-451` `create()` returns the full `WorkPackageTaskViewModel` including `id`. Good.

Canonical client-side route map: `packages/shared/src/constants/api-endpoints.ts:1-178` (`API` object).
The MCP's `EP` is a hand-copied subset of it.

---

## 4. Realtime / eventing

### 4.1 Pipeline

```
Service → DomainEventService.emit()                common/services/domain-event.service.ts:18
        → EventEmitter2 'domain.event'             (in-process, async)
        → DomainEventListener.handleDomainEvent    common/listeners/domain-event.listener.ts:118
            ├─ QueuePublisher.emitSocket → RabbitMQ "{prefix}-asoode-socket"   :122
            ├─ QueuePublisher.emitPush   → RabbitMQ "{prefix}-asoode-push"     :137
            ├─ prisma.activityLog.create (direct write, not queued)            :206
            └─ NotificationMailerService → "…-asoode-email"                     :144
apps/socket  MessageHandlerService consumes socket+push   app/services/message-handler.service.ts:22-32
        → socket.emit('push-notification', model) per connected client          :117-120
apps/worker  consumes "…-asoode-email" and "…-asoode-sms" only                  app.module.ts:23
```

Queue naming: `` `${prefix}-asoode-${suffix}`.replace(/^-+|-+$/g,'') `` (`queue-publisher.service.ts:23-28`,
mirrored in worker and socket). Prefix from `QUEUE_PREFIX` (dev env sets `dev`).
Four durable queues: `socket`, `push`, `email`, `sms`. Worker dead-letters to `{queue}.dlq`.

**Queue = RabbitMQ (amqplib). No Bull, no Redis.**

### 4.2 Task events and payloads

`DomainEvent` = `{type, actorId, entityId, entityType, recipientUserIds, data, push?, log?}`
(`packages/shared/src/models/core/domain-event.ts:3-28`).

The browser receives `{type, data, push:{title,description,avatar,url}}` on event name
**`'push-notification'`** (`message-handler.service.ts:103-120`). **One event name for everything**;
`type` (ActivityType) is the discriminator.

| Action | ActivityType | `data` | Emit site |
|---|---|---|---|
| Task created | `WorkPackageTaskAdd` | full `WorkPackageTaskViewModel` + `packageId` | `tasks.service.ts:436-448` |
| State changed | `WorkPackageTaskEdit`, or `…Done` when state=3 | `{id, state, doneAt, doneUserId, packageId, listId}` | `:718-743` |
| Member assigned | `WorkPackageTaskMemberAdd` | `{taskId, recordId, isGroup, packageId, listId, member}` | `:1020-1039` |
| Moved | `WorkPackageTaskMove` | `{id, packageId, listId, oldPackageId, oldListId}` | `:851-863` |
| Repositioned | `WorkPackageTaskReposition` | `{id, listId, order, packageId, oldListId, siblings[]}` | `:797-804` |
| Archived/restored | `WorkPackageTaskArchive`/`…Restore` | `{id, taskId, archived, archivedAt, packageId, listId}` | `:2173-2192` |
| Commented | `WorkPackageTaskComment` | comment VM | `~:1550` |
| Viewed | `WorkPackageTaskView` | `{}`, `recipientUserIds: []` | `:506-513` |

**Recipient targeting is broken.** `getPackageMemberUserIds()` (`tasks.service.ts:178-184`) queries
**only `WorkPackageMember` rows with `isGroup:false`**. It excludes the work-package owner, the project
owner, and all project-level members. A user with access purely via `ProjectMember` (which
`verifyTaskAccess` accepts, `:114-117`) **never receives the socket event**. An empty `users` array
causes the socket app to drop the message outright (`message-handler.service.ts:98-101`).

### 4.3 WebSocket gateway — no authentication

`apps/socket/src/app/gateways/main.gateway.ts:15` `@WebSocketGateway()`, no namespace/path config;
port `PORT || 8020`, host `0.0.0.0`; CORS `origin: true, credentials: true`.

```ts
handleConnection(client: Socket): void {
  const userId = client.handshake.query['userId'] as string | undefined;   // main.gateway.ts:32
  if (!userId) return;
  this.notificationService.onConnect(userId, client.id);
}
```

**Identity is an unverified query parameter.** No JWT, no PAT, no signature. Anyone who knows a user's
UUID can `io('http://host:8020', {query:{userId}})` and receive that user's entire realtime feed. The
socket app has no `PrismaService`, no auth guard, no `JWT_SECRET` dependency.
Convenient for a local bridge; a real security hole that must be closed.

Other gateway messages: `focus:set` / `focus:clear` (`:44,54`) — suppress push for a user already
looking at the context. Client precedent: `apps/frontend/src/services/socket.service.ts:14-17`.

### 4.4 Outbound webhooks — none

No webhook registration table, endpoint, or config. The only outbound HTTP is the workflow `webhook`
node (`workflows.service.ts:844-864`): `fetch(url, {method: method||'POST', body: JSON.stringify(context)})`.

**Workflows only run manually** — `POST /workflows/:id/execute` (`workflows.controller.ts:44`).
`Workflow.trigger` is a free-text string defaulting to `'manual'` (`schema.prisma:842`) that
**nothing ever dispatches on**: `@OnEvent` appears exactly once in the whole backend
(`domain-event.listener.ts:118`) and does not touch workflows. No cron for workflows either — the only
`@Cron`s are `task-reminder.service.ts:41` (per-minute reminder emails) and `digest.service.ts:24`
(daily 7am digest).

Workflow node types that could act as bridge actions **if triggering existed**: `create_task` (`:555`),
`assign_member` (`:495`), `change_state` (`:427`), `move_task` (`:528`), `add_comment` (`:473`),
`webhook` (`:844`).

### 4.5 "Changes since" / audit log

- **`ActivityLog` exists** (`schema.prisma:815-830`), written on every domain event
  (`domain-event.listener.ts:206-215`).
- **Exposed by exactly one endpoint:** `POST /tasks/:id/logs` (`tasks.controller.ts:309`) →
  `where: {taskId}`, no date filter, no limit. **Per-task only.** No global or per-user activity feed.
- Nearest polling surrogate: `POST /reports/recent-activities` (`misc.controller.ts:33`) →
  `misc.service.ts:305-360`: the **50 most recently `updatedAt` tasks** across the user's projects.
  No parameters, no cursor, hardcoded `take: 50`. A burst > 50 loses data.

---

## 5. Auth for a machine client

### 5.1 Two credential types

`AuthGuard.canActivate` (`common/guards/auth.guard.ts:29-54`) requires `Authorization: Bearer …`
then branches on the prefix `asoode_pat_` (`:17,46`):

- **PAT path** (`:73-100`): sha256 the whole token → `personalAccessToken.findUnique({where:{tokenHash}})`,
  reject if missing / `revokedAt` set / `expiresAt` past (`:80-85`); sets `request.userId` and
  `request.tokenScopes`; throttled `lastUsedAt` bump every 60s (`:19,92-99`).
- **JWT path** (`:57-70`): `jwt.verify(token, jwt.secret)`, payload `{userId, username}`;
  sets `request.tokenScopes = undefined` = "full browser session". Lifetime `JWT_EXPIRES_IN || '30d'`.

`@CurrentUser()` reads `request.userId` (`common/decorators/current-user.decorator.ts:3-8`).

### 5.2 PAT issuance — **PATs already exist**

Endpoints, all tagged `@Scopes('account')` (`account.controller.ts:208-224`):
`POST /account/tokens` (create), `POST /account/tokens/list` (masked; `tokenHash` never selected),
`POST /account/tokens/:id/revoke`.

`CreateTokenDto = {name: string; expiresInDays?: number|null; scopes?: string[]}`
(`packages/shared/src/dto/auth.dto.ts:55-61`).

Creation (`account.service.ts:1332-1373`):
- secret = `asoode_pat_` + `randomBytes(32).toString('base64url')`; stored as sha256 hex; `last4` kept for masking
- **plaintext returned exactly once**, at creation (`:1372`)
- max 20 active tokens/user (`PAT_MAX_PER_USER`)
- unknown scopes silently dropped
- **always given an expiry**: default 90 days, clamped to max 365
  (`PAT_DEFAULT_EXPIRY_DAYS`/`PAT_MAX_EXPIRY_DAYS`, `configuration.ts:20-27`).
  `expiresAt: null` (never expires) is **unreachable through the API**.

UI exists: `apps/frontend/src/components/profile/AccessTokensPanel.tsx`, surfaced in `ProfilePage.tsx`.

### 5.3 Scope enforcement — **not implemented**

`enforceScopes` (`auth.guard.ts:107-122`) returns early when the handler has no `@Scopes(...)` metadata (`:112`).
`grep -rn "@Scopes" apps/backend/src` = **three call sites, all in `account.controller.ts`, all `'account'`**.

Consequences:
- `read` and `write` scopes are **declared but enforced nowhere**. A PAT with `scopes:['read']` can call
  `POST /tasks/:listId/create`, `/change-state`, `/archive` — every mutating endpoint in the product.
- The only real protection a scoped PAT gives is that a non-`account` PAT cannot mint or revoke tokens.
- `apps/mcp/README.md:76-80` documents scope semantics that do not exist.

### 5.4 Bottom line for a laptop bridge

One long-lived PAT (`Authorization: Bearer asoode_pat_…`), max 365-day life, obtained by the human from
Profile → Access Tokens. **No OAuth client-credentials flow, no device flow, no refresh, no per-agent
scoping that actually restricts anything.** The PAT is a full-power user credential.

---

## 6. Gaps blocking the bridge

### 6.1 Claude → asoode (mirror created tasks)

1. **No `external_ref`/idempotency key on `WorkPackageTask`.** Verified: `grep -rn "externalId|external_ref|externalRef|idempot|clientRef"` across backend/mcp/shared returns only unrelated comments and a `sourceId` local in `work-packages.service.ts:783`. **Every re-sync creates duplicates.** No unique constraint can prevent it — `WorkPackageTask` has no `@@unique` at all.
2. **No dedupe read path.** `POST /search` returns `SearchTaskViewModel` (`packages/shared/src/models/search/search.model.ts:23-36`) which **has no `listId` and no `updatedAt`** — unusable for reconciliation.
3. **`create_task` cannot set a description.** `CreateTaskDto` has no `description`. `POST /tasks/:id/change-description` exists but **has no MCP tool**. A Claude-authored task arrives as a bare title.
4. **`create_task` assigns nobody.** `tasks.service.ts:420-431` writes no `TaskMember`. Since `kartabl` filters on `TaskMember`, a task Claude creates **is invisible in `my_tasks`** until a separate `assign_task` call.
5. **No MCP tool for labels.** Endpoints exist, neither exposed. Can't tag `claude-generated`.
6. **No MCP tool for** `change-description`, `change-priority`, `reposition`, `estimated`, `watch`, `vote`, `custom-field/value`, `attach`, `logs`, `convert-to-task`, `member/remove`, `label/remove`, `toggle-timer`, `time/:entryId/edit|delete`.
7. **`create_list` returns `true`, not the list** — extra `list_board` round trip needed.
8. **No bulk/batch create.** One HTTP call per task.
9. **No `seasonId` write path.** `ProjectSeason` can be created/edited but **no endpoint assigns a task to one**. Sprint attribution impossible via API.

### 6.2 asoode → Claude (assigned tasks flow back)

10. **No `updated_since` filter anywhere.** Delta sync impossible server-side; the bridge must full-scan and diff locally.
11. **No pagination on `kartabl`.** No `take`/`skip`/cursor, no request body. Payload grows without bound.
12. **No state filter on `kartabl`.** Done/Cancelled come back forever; filter client-side on `state`.
13. **Group-assigned tasks are invisible.** `kartabl` matches `recordId: userId` only; nothing expands groups on read even though `TaskMember.isGroup` exists and `assign_task` can set it.
14. **No global activity-log endpoint.** Only per-task, unfiltered, unpaginated.
15. **No webhook registration.** Push-to-Claude is impossible today.
16. **Workflow triggers are dead code.** Building on "workflow fires webhook on task assign" requires implementing the trigger dispatcher first.
17. **Realtime is available but unauthenticated and mis-targeted** (§4.2, §4.3).

### 6.3 Duplicate / corruption risks

- **`create_task` is fully non-idempotent.** No unique constraint; a retry after the 20 s timeout creates a second task, and the MCP surfaces the timeout with **no way to know whether the write landed**.
- **`archive_task` is a TOGGLE, not an archive.** `tasks.service.ts:2159-2162`: `const newArchivedAt = isArchived ? null : new Date()`. Calling it twice **un-archives** and emits `WorkPackageTaskRestore`. The MCP's `confirm` flag does not protect against this.
- **`assign_task` is safe:** `TaskMember` has `@@unique([taskId, recordId])` and the service catches the violation → `Duplicate` (status 4), not a 500. Same for `TaskLabel`.
- **`create_work_package` / `create_list` are non-idempotent.** No unique on `(projectId,title)` / `(packageId,title)`. Re-running bootstrap makes duplicate boards/columns.
- **`reposition` corrupts denormalized parents.** It updates `listId` **without** updating `packageId` (`tasks.service.ts:782-785`). **A bridge must always use `move`, never `reposition`, for cross-board changes.**
- **No optimistic concurrency.** No `version`/`etag`/`If-Match`. Two-way sync is last-write-wins and will clobber concurrent human edits.
- **Errors are HTTP 200.** A bridge that retries on non-2xx will never retry; one that treats 200 as success will silently drop failures.

---

## 7. Deployment

Both local (`docker-compose.yml` at repo root) and images on Docker Hub (`kianfar/asoode-*`).
GitLab CI exists but its deploy stage is **entirely commented out** (`.gitlab-ci.yml:408-453`).

### Docker ports

| Service | Host port |
|---|---|
| postgres 16 | 5432 |
| rabbitmq 3-mgmt | 5672 / 15672 |
| minio | 9000 / 9001 |
| **backend** | **3000** |
| **socket** | **8020** |
| worker | none (no HTTP) |
| **frontend** (nginx SPA) | **80** → `http://localhost` |
| website | 8080 |
| **mcp** | **8030** (`MCP_TRANSPORT: http`, `ASOODE_API_URL: http://backend:3000`) |

### Dev ports (`pnpm dev`, no Docker)

| App | URL |
|---|---|
| **frontend** | **`http://localhost:5174`** (`apps/frontend/vite.config.ts:81`) |
| backend | `http://localhost:3000` |
| socket | `http://localhost:8020` |
| website | `http://localhost:4000` |
| cms (`acha-cms`, Next.js, separate product) | `http://localhost:3600` |
| mcp http | `http://localhost:8030/mcp` |

### Frontend → backend wiring (matters for the Browser pane)

- **Dev:** Vite proxies `/api` → `:3000` (stripping the prefix) and `/socket.io` → `:8020` with
  `ws: true` (`vite.config.ts:82-93`). The SPA is a **single origin, `http://localhost:5174`** — ideal
  for an embedded pane.
- **Prod:** nginx serves the SPA; `docker-entrypoint.sh:15-21` writes `/runtime-config.json` from
  `API_URL`/`SOCKET_URL`/`VAPID_PUBLIC_KEY`, fetched at boot
  (`apps/frontend/src/services/runtime-config.service.ts:16-26`). Compose defaults
  `API_URL=http://localhost:3000`, `SOCKET_URL=http://localhost:8020`. Default `apiUrl` when the file
  is absent is `/api`.
- Backend CORS reflects any origin, so a bridge or pane on any localhost port can call it directly.
- **Swagger UI at `GET /docs`** with bearer auth configured — a live, accurate route reference.
- Deep link format in every push payload: `/work-package/{packageId}` (e.g. `tasks.service.ts:446`).
  **There is no per-task URL in event payloads** — task deep links must be constructed by the bridge.

### Notes

- No `docker-compose` service or launch config for a local MCP **stdio** process — that is configured
  client-side (`apps/mcp/README.md:152-165`).
- As of this analysis the memory-mcp repo contained **zero references to asoode** — no bridge code
  existed on either side.
