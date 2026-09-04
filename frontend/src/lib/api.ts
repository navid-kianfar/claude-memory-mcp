import type {
  AsoodeStatus,
  BoardRef,
  ProjectLink,
  ApplyTemplateResult,
  AuthSession,
  BulkAddRuleInput,
  BulkAddRuleResult,
  CreateUserInput,
  CreateUserResult,
  Health,
  ImportResult,
  ImportRulesResult,
  LinkFolderResult,
  LoadFromFolderResult,
  LoginInput,
  Memory,
  MemoryInput,
  MemoryListResponse,
  MemoryStatus,
  MemoryUpdate,
  Meta,
  PendingAdaptationsResponse,
  PendingRulesResponse,
  Project,
  ProjectDetail,
  ProjectInput,
  ProjectUpdate,
  ProvenanceEntry,
  RotateTokenResult,
  RulesResponse,
  Session,
  Task,
  TaskActivityEntry,
  TaskDetail,
  TaskInput,
  TaskListResponse,
  TaskUpdate,
  Template,
  TemplateInput,
  TemplateItem,
  TemplateItemInput,
  TemplateItemUpdate,
  TemplateUpdate,
  UpdateStatus,
  User,
  UsersResponse,
} from "../types";

class ApiError extends Error {
  type: string;
  status: number;
  constructor(message: string, type: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.type = type;
    this.status = status;
  }
}

// Called on an unexpected 401 (server-mode session expired). AuthGate registers
// this to bounce back to the login screen. Never fires in local mode (no 401s).
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    // CSRF guard for cookie-authenticated writes (server mode). Harmless for
    // GETs and for local mode, so it's sent on every request.
    "X-Requested-With": "memory-mcp",
    ...(options.headers as Record<string, string>),
  };
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  let res: Response;
  try {
    res = await fetch(path, { credentials: "same-origin", ...options, headers });
  } catch (err) {
    throw new ApiError(
      err instanceof Error ? err.message : "Network request failed",
      "network_error",
      0
    );
  }

  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }

  if (!res.ok) {
    // Session expired mid-app: signal AuthGate to return to login. Auth
    // endpoints handle their own 401s (bad login), so exclude them.
    if (res.status === 401 && !path.startsWith("/api/auth/") && onUnauthorized) {
      onUnauthorized();
    }
    const body = (data ?? {}) as { error?: string; type?: string };
    throw new ApiError(
      body.error || `Request failed (${res.status})`,
      body.type || "http_error",
      res.status
    );
  }

  return data as T;
}

function qs(params: Record<string, string | number | undefined | null>) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export const api = {
  isApiError(err: unknown): err is ApiError {
    return err instanceof ApiError;
  },

  getMeta(): Promise<Meta> {
    return request<Meta>("/api/meta");
  },

  getHealth(): Promise<Health> {
    return request<Health>("/api/health");
  },

  listProjects(): Promise<{ projects: Project[] }> {
    return request<{ projects: Project[] }>("/api/projects");
  },

  createProject(
    input: ProjectInput
  ): Promise<{ status: string; project: Project }> {
    return request("/api/projects", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  updateProject(
    slug: string,
    input: ProjectUpdate
  ): Promise<{ status: string; project: Project }> {
    return request(`/api/projects/${encodeURIComponent(slug)}`, {
      method: "PUT",
      body: JSON.stringify(input),
    });
  },

  loadProjectFromFolder(path: string): Promise<LoadFromFolderResult> {
    return request<LoadFromFolderResult>("/api/projects/load-from-folder", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
  },

  pickFolder(
    prompt?: string
  ): Promise<{ status: "ok" | "cancelled" | "unavailable"; path?: string }> {
    return request("/api/pick-folder", {
      method: "POST",
      body: JSON.stringify(prompt ? { prompt } : {}),
    });
  },

  linkFolder(slug: string, path: string): Promise<LinkFolderResult> {
    return request<LinkFolderResult>(
      `/api/projects/${encodeURIComponent(slug)}/link-folder`,
      {
        method: "POST",
        body: JSON.stringify({ path }),
      }
    );
  },

  getProject(slug: string): Promise<ProjectDetail> {
    return request<ProjectDetail>(
      `/api/projects/${encodeURIComponent(slug)}`
    );
  },

  setActive(slug: string): Promise<{ status: string; active_project: string }> {
    return request("/api/active", {
      method: "POST",
      body: JSON.stringify({ slug }),
    });
  },

  listMemories(
    slug: string,
    opts: {
      q?: string;
      category?: string;
      status?: MemoryStatus;
      limit?: number;
      offset?: number;
    } = {}
  ): Promise<MemoryListResponse> {
    return request<MemoryListResponse>(
      `/api/projects/${encodeURIComponent(slug)}/memories${qs({
        q: opts.q,
        category: opts.category,
        status: opts.status,
        limit: opts.limit,
        offset: opts.offset,
      })}`
    );
  },

  createMemory(
    slug: string,
    input: MemoryInput
  ): Promise<{ status: string; memory: Memory }> {
    return request(`/api/projects/${encodeURIComponent(slug)}/memories`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  updateMemory(
    slug: string,
    id: string,
    input: MemoryUpdate
  ): Promise<{ status: string; memory: Memory }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/memories/${encodeURIComponent(
        id
      )}`,
      {
        method: "PUT",
        body: JSON.stringify(input),
      }
    );
  },

  deleteMemory(
    slug: string,
    id: string,
    opts: { hard?: boolean; reason?: string } = {}
  ): Promise<{ status: string; action: string; memory_id: string }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/memories/${encodeURIComponent(
        id
      )}${qs({ hard: opts.hard ? "true" : undefined, reason: opts.reason })}`,
      { method: "DELETE" }
    );
  },

  getProvenance(
    slug: string,
    id: string
  ): Promise<{ memory_id: string; provenance: ProvenanceEntry[] }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/memories/${encodeURIComponent(
        id
      )}/provenance`
    );
  },

  getRules(slug: string): Promise<RulesResponse> {
    return request<RulesResponse>(
      `/api/projects/${encodeURIComponent(slug)}/rules`
    );
  },

  getSessions(slug: string): Promise<{ sessions: Session[] }> {
    return request<{ sessions: Session[] }>(
      `/api/projects/${encodeURIComponent(slug)}/sessions`
    );
  },

  importClaudeMd(
    slug: string,
    input: { path: string; stub_rewrite?: boolean }
  ): Promise<ImportResult> {
    return request<ImportResult>(
      `/api/projects/${encodeURIComponent(slug)}/import-claude-md`,
      {
        method: "POST",
        body: JSON.stringify(input),
      }
    );
  },

  listTemplates(): Promise<{ templates: Template[] }> {
    return request<{ templates: Template[] }>("/api/templates");
  },

  getTemplate(id: number): Promise<{ template: Template }> {
    return request<{ template: Template }>(`/api/templates/${id}`);
  },

  createTemplate(
    input: TemplateInput
  ): Promise<{ status: string; template: Template }> {
    return request("/api/templates", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  updateTemplate(
    id: number,
    input: TemplateUpdate
  ): Promise<{ status: string; template: Template }> {
    return request(`/api/templates/${id}`, {
      method: "PUT",
      body: JSON.stringify(input),
    });
  },

  deleteTemplate(id: number): Promise<{ status: string; deleted: unknown }> {
    return request(`/api/templates/${id}`, { method: "DELETE" });
  },

  createTemplateItem(
    templateId: number,
    input: TemplateItemInput
  ): Promise<{ status: string; item: TemplateItem }> {
    return request(`/api/templates/${templateId}/items`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  updateTemplateItem(
    templateId: number,
    itemId: number,
    input: TemplateItemUpdate
  ): Promise<{ status: string; item: TemplateItem }> {
    return request(`/api/templates/${templateId}/items/${itemId}`, {
      method: "PUT",
      body: JSON.stringify(input),
    });
  },

  deleteTemplateItem(
    templateId: number,
    itemId: number
  ): Promise<{ status: string; deleted_item: unknown }> {
    return request(`/api/templates/${templateId}/items/${itemId}`, {
      method: "DELETE",
    });
  },

  applyTemplate(
    slug: string,
    input: { template_id: number; item_ids?: number[] }
  ): Promise<ApplyTemplateResult> {
    return request<ApplyTemplateResult>(
      `/api/projects/${encodeURIComponent(slug)}/apply-template`,
      {
        method: "POST",
        body: JSON.stringify(input),
      }
    );
  },

  importRules(
    slug: string,
    input: { source_project: string; memory_ids: string[]; pending?: boolean }
  ): Promise<ImportRulesResult> {
    return request<ImportRulesResult>(
      `/api/projects/${encodeURIComponent(slug)}/import-rules`,
      {
        method: "POST",
        body: JSON.stringify(input),
      }
    );
  },

  bulkAddRule(input: BulkAddRuleInput): Promise<BulkAddRuleResult> {
    return request<BulkAddRuleResult>("/api/rules/bulk", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  // ---------- auth (server mode) ----------

  login(input: LoginInput): Promise<AuthSession> {
    return request<AuthSession>("/api/auth/login", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  logout(): Promise<{ status: string }> {
    return request<{ status: string }>("/api/auth/logout", { method: "POST" });
  },

  whoami(): Promise<{ mode: string; user: AuthSession["user"] }> {
    return request("/api/auth/whoami");
  },

  // ---------- users (admin) ----------

  listUsers(): Promise<UsersResponse> {
    return request<UsersResponse>("/api/users");
  },

  createUser(input: CreateUserInput): Promise<CreateUserResult> {
    return request<CreateUserResult>("/api/users", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  deactivateUser(id: string): Promise<{ status: string; user: User }> {
    return request(`/api/users/${encodeURIComponent(id)}/deactivate`, {
      method: "POST",
    });
  },

  rotateUserToken(id: string): Promise<RotateTokenResult> {
    return request<RotateTokenResult>(
      `/api/users/${encodeURIComponent(id)}/rotate-token`,
      { method: "POST" }
    );
  },

  // ---------- tasks ----------

  listTasks(
    slug: string,
    opts: {
      includeDone?: boolean;
      includeArchived?: boolean;
      parentId?: string;
      state?: string;
      source?: string;
    } = {}
  ): Promise<TaskListResponse> {
    const q = new URLSearchParams();
    if (opts.includeDone) q.set("include_done", "true");
    if (opts.includeArchived) q.set("include_archived", "true");
    if (opts.parentId) q.set("parent_id", opts.parentId);
    if (opts.state) q.set("state", opts.state);
    if (opts.source) q.set("source", opts.source);
    q.set("limit", "500");
    const qs = q.toString();
    return request<TaskListResponse>(
      `/api/projects/${encodeURIComponent(slug)}/tasks${qs ? `?${qs}` : ""}`
    );
  },

  createTask(slug: string, input: TaskInput): Promise<{ status: string; task: Task }> {
    return request(`/api/projects/${encodeURIComponent(slug)}/tasks`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  getTask(slug: string, tid: string): Promise<TaskDetail> {
    return request<TaskDetail>(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}`
    );
  },

  updateTask(
    slug: string,
    tid: string,
    input: TaskUpdate
  ): Promise<{ status: string; task: Task; changed: string[] }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}`,
      { method: "PUT", body: JSON.stringify(input) }
    );
  },

  commentTask(
    slug: string,
    tid: string,
    input: { body: string; kind?: string; author?: string }
  ): Promise<{ status: string }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}/comments`,
      { method: "POST", body: JSON.stringify(input) }
    );
  },

  startTask(slug: string, tid: string): Promise<TaskDetail> {
    return request<TaskDetail>(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}/start`,
      { method: "POST" }
    );
  },

  stopTask(slug: string, tid: string): Promise<TaskDetail> {
    return request<TaskDetail>(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}/stop`,
      { method: "POST" }
    );
  },

  doneTask(slug: string, tid: string, note?: string): Promise<TaskDetail> {
    return request<TaskDetail>(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}/done`,
      { method: "POST", body: JSON.stringify({ note: note ?? null }) }
    );
  },

  convertTaskToTop(slug: string, tid: string): Promise<{ status: string; task: Task }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}/convert`,
      { method: "POST" }
    );
  },

  deleteTask(slug: string, tid: string): Promise<{ status: string; deleted: string }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}`,
      { method: "DELETE" }
    );
  },

  /** Archive many at once - the Clear button. Ids, not a filter, so it clears
   *  exactly what the user was shown. Archive is reversible; this never deletes. */
  archiveTasks(
    slug: string,
    ids: string[]
  ): Promise<{ status: string; archived: number; failed: Record<string, string> }> {
    return request(`/api/projects/${encodeURIComponent(slug)}/tasks/archive`, {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
  },

  reorderTasks(slug: string, ids: string[]): Promise<{ status: string; reordered: number }> {
    return request(`/api/projects/${encodeURIComponent(slug)}/tasks/reorder`, {
      method: "POST",
      body: JSON.stringify({ ids }),
    });
  },

  taskActivity(slug: string, tid: string): Promise<{ activity: TaskActivityEntry[] }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}/activity`
    );
  },

  releaseTask(slug: string, tid: string): Promise<{ status: string; task: Task }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}/release`,
      { method: "POST" }
    );
  },

  archiveTask(slug: string, tid: string): Promise<{ status: string; task: Task }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/tasks/${encodeURIComponent(tid)}/archive`,
      { method: "POST" }
    );
  },

  // ---------- rule governance (admin) ----------

  listPendingAdaptations(slug: string): Promise<PendingAdaptationsResponse> {
    return request<PendingAdaptationsResponse>(
      `/api/projects/${encodeURIComponent(slug)}/pending`
    );
  },

  adaptPending(
    slug: string,
    mid: string,
    input: { title: string; content: string; priority?: number }
  ): Promise<{ status: string; memory: Memory }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/pending/${encodeURIComponent(mid)}/adapt`,
      { method: "POST", body: JSON.stringify(input) }
    );
  },

  discardPending(
    slug: string,
    mid: string,
    reason?: string
  ): Promise<{ status: string; discarded: string; title: string }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/pending/${encodeURIComponent(mid)}`,
      { method: "DELETE", body: JSON.stringify({ reason: reason ?? null }) }
    );
  },

  listPendingRules(): Promise<PendingRulesResponse> {
    return request<PendingRulesResponse>("/api/rules/pending");
  },

  approveRule(slug: string, rid: string): Promise<{ status: string; rule: Memory }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/rules/${encodeURIComponent(rid)}/approve`,
      { method: "POST" }
    );
  },

  revokeRule(slug: string, rid: string): Promise<{ status: string; rule: Memory }> {
    return request(
      `/api/projects/${encodeURIComponent(slug)}/rules/${encodeURIComponent(rid)}/revoke`,
      { method: "POST" }
    );
  },

  // ---------- org-wide rules (admin) ----------

  listOrgRules(): Promise<RulesResponse> {
    return request<RulesResponse>("/api/org/rules");
  },

  createOrgRule(
    input: BulkAddRuleInput
  ): Promise<{ status: string; rule: Memory }> {
    return request("/api/org/rules", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },

  updateOrgRule(
    rid: string,
    input: MemoryUpdate
  ): Promise<{ status: string; rule: Memory }> {
    return request(`/api/org/rules/${encodeURIComponent(rid)}`, {
      method: "PUT",
      body: JSON.stringify(input),
    });
  },

  deleteOrgRule(rid: string): Promise<{ status: string }> {
    return request(`/api/org/rules/${encodeURIComponent(rid)}`, {
      method: "DELETE",
    });
  },

  // ---------- asoode integration ----------

  getAsoodeStatus(): Promise<AsoodeStatus> {
    return request("/api/asoode");
  },

  setAsoodeUrls(input: {
    api_url?: string;
    app_url?: string;
    socket_url?: string;
    reset?: boolean;
  }): Promise<AsoodeStatus> {
    return request("/api/asoode", { method: "PUT", body: JSON.stringify(input) });
  },

  setAsoodePat(token: string): Promise<AsoodeStatus> {
    return request("/api/asoode/pat", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
  },

  clearAsoodePat(): Promise<AsoodeStatus> {
    return request("/api/asoode/pat", { method: "DELETE" });
  },

  getProjectLinks(slug: string): Promise<{ slug: string; links: ProjectLink[] }> {
    return request(`/api/projects/${encodeURIComponent(slug)}/asoode/links`);
  },

  listBoards(): Promise<{ boards: BoardRef[] }> {
    return request("/api/asoode/boards");
  },

  attachBoard(
    slug: string,
    input: { work_package_id?: string; external_ref?: string; label?: string; is_default?: boolean; backfill?: boolean }
  ): Promise<Record<string, unknown>> {
    return request(`/api/projects/${encodeURIComponent(slug)}/asoode/link`, {
      method: "POST",
      body: JSON.stringify({ ...input, attach: true }),
    });
  },

  pushProject(slug: string): Promise<Record<string, unknown>> {
    return request(`/api/projects/${encodeURIComponent(slug)}/asoode/push`, {
      method: "POST",
      body: JSON.stringify({}),
    });
  },

  // ---------- self-update ----------
  // Machine-wide, like the asoode credential: one daemon, one installation.

  getUpdateStatus(): Promise<UpdateStatus> {
    return request("/api/update");
  },

  /**
   * Record approval. Does NOT install - the Stop hook applies it at the end of
   * the turn. Refused with an error when the cached poll says there is nothing
   * to apply, which is how a stale banner tells on itself.
   */
  approveUpdate(): Promise<{ status: string; approved: boolean; note?: string }> {
    return request("/api/update/approve", { method: "POST" });
  },

  cancelUpdateApproval(): Promise<{ status: string; approved: boolean }> {
    return request("/api/update/approve", { method: "DELETE" });
  },
};

export { ApiError };
