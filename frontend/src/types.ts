export type Category =
  | "decision"
  | "session"
  | "sprint"
  | "project_plan"
  | "architecture"
  | "devops"
  | "mandatory_rules"
  | "forbidden_rules"
  | "developer_docs"
  | "feedback"
  | "reference";

export interface Meta {
  version: string;
  categories: string[];
  rule_categories: string[];
  active_project: string | null;
  model: string;
}

export interface Health {
  status: string;
  version: string;
}

export interface Project {
  slug: string;
  display_name: string;
  description: string;
  created_at: string;
  last_accessed: string;
  db_path: string;
  project_path: string | null;
  memory_count: number;
}

export interface Memory {
  id: string;
  category: string;
  title: string;
  content: string;
  summary: string;
  tags: string[];
  metadata: Record<string, unknown> | null;
  status: string;
  priority: number;
  source: string | null;
  related_ids: string[];
  entities: string[];
  access_count: number;
  expires_at: string | null;
  created_at: string;
  updated_at: string;
  _similarity?: number;
  _relevance?: number;
}

export interface ProvenanceEntry {
  id: string;
  memory_id: string;
  operation: string;
  details: string;
  created_at: string;
}

export interface Session {
  id: string;
  started_at: string;
  ended_at: string | null;
  summary: string;
  memories_created: number;
  memories_accessed: number;
}

export type MemoryStatus = "active" | "archived" | "all";

export interface MemoryListResponse {
  mode: "list" | "search";
  memories: Memory[];
  total: number;
  limit?: number;
  offset?: number;
}

export interface ProjectDetail {
  project: Project;
  counts: Record<string, number>;
}

export interface RulesResponse {
  mandatory_rules: Memory[];
  forbidden_rules: Memory[];
  total: number;
}

export interface ImportResult {
  status: string;
  source: string;
  imported: number;
  memories: number;
  stub?: string;
}

export interface MemoryInput {
  category: string;
  title: string;
  content: string;
  tags?: string[];
  priority?: number;
  metadata?: Record<string, unknown>;
}

export interface MemoryUpdate {
  title?: string;
  content?: string;
  tags?: string[];
  status?: string;
  priority?: number;
  metadata?: Record<string, unknown>;
}

export interface ProjectInput {
  slug: string;
  display_name: string;
  description?: string;
  project_path?: string;
}

export interface LinkFolderResult {
  status: string;
  project: Project;
}

export interface TemplateItem {
  id: number;
  template_id: number;
  category: string;
  title: string;
  content: string;
  priority: number;
}

export interface Template {
  id: number;
  name: string;
  description: string | null;
  created_at: string;
  items: TemplateItem[];
}

export interface TemplateInput {
  name: string;
  description?: string;
}

export interface TemplateUpdate {
  name?: string;
  description?: string;
}

export interface TemplateItemInput {
  category: string;
  title: string;
  content: string;
  priority?: number;
}

export interface TemplateItemUpdate {
  category?: string;
  title?: string;
  content?: string;
  priority?: number;
}

export interface ApplyTemplateResult {
  status: string;
  template: Template;
  applied: number;
  memories: number;
}

export interface ImportRulesResult {
  status: string;
  imported: number;
  skipped: number;
  memories: number;
}

export type LoadFromFolderSource =
  | "existing_memory_db"
  | "claude_md"
  | "new_empty";

export interface LoadFromFolderResult {
  status: string;
  project: {
    slug: string;
    display_name: string;
    db_path: string;
    project_path: string;
  };
  folder: string;
  claude_md_imported: number;
  source: LoadFromFolderSource;
  active: boolean;
}
