import type {
  Health,
  ImportResult,
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
};

export { ApiError };
