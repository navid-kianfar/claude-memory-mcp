import type {
  ApplyTemplateResult,
  Health,
  ImportResult,
  ImportRulesResult,
  LinkFolderResult,
  LoadFromFolderResult,
  Memory,
  MemoryInput,
  MemoryListResponse,
  MemoryStatus,
  MemoryUpdate,
  Meta,
  Project,
  ProjectDetail,
  ProjectInput,
  ProvenanceEntry,
  RulesResponse,
  Session,
  Template,
  TemplateInput,
  TemplateItem,
  TemplateItemInput,
  TemplateItemUpdate,
  TemplateUpdate,
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

async function request<T>(
  path: string,
  options: RequestInit = {}
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
    ...(options.headers as Record<string, string>),
  };
  if (options.body && !headers["Content-Type"]) {
    headers["Content-Type"] = "application/json";
  }

  let res: Response;
  try {
    res = await fetch(path, { ...options, headers });
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
    input: { source_project: string; memory_ids: string[] }
  ): Promise<ImportRulesResult> {
    return request<ImportRulesResult>(
      `/api/projects/${encodeURIComponent(slug)}/import-rules`,
      {
        method: "POST",
        body: JSON.stringify(input),
      }
    );
  },
};

export { ApiError };
