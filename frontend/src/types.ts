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

export type Role = "admin" | "member";

export interface User {
  id: string;
  username: string;
  display_name: string | null;
  role: Role;
  active: boolean;
  created_at: string;
  last_login: string | null;
}

export interface Meta {
  version: string;
  categories: string[];
  rule_categories: string[];
  active_project: string | null;
  model: string;
  // Present since server mode. Older/local backends omit these; the UI treats a
  // missing mode as "local" so nothing governance-related renders.
  mode?: "local" | "server";
  current_user?: { id: string; username: string; role: Role } | null;
  role?: Role | null;
}

export interface AuthSession {
  status: string;
  user: { id: string; username: string; role: Role } | null;
}

export interface LoginInput {
  username: string;
  token: string;
}

export interface CreateUserInput {
  username: string;
  role: Role;
  display_name?: string;
}

export interface UsersResponse {
  users: User[];
}

export interface CreateUserResult {
  status: string;
  user: User;
  token: string;
}

export interface RotateTokenResult {
  status: string;
  user: User;
  token: string;
}

export interface PendingRuleEntry {
  project: { slug: string; display_name: string };
  rule: Memory;
}

export interface PendingRulesResponse {
  pending: PendingRuleEntry[];
  total: number;
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
  // Rule approval lifecycle (server mode). Optional so local-mode data - which
  // never sets these - is unaffected. approval_status defaults to "approved".
  created_by?: string | null;
  approval_status?: "approved" | "proposed" | "revoked";
  approved_by?: string | null;
  approved_at?: string | null;
  // Imported from another project and not yet rewritten for this one. Pending
  // memories are stored but inert: no rule block, no search, no git snapshot.
  pending?: boolean;
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

export interface ProjectUpdate {
  display_name?: string;
  description?: string;
}

export interface BulkAddRuleInput {
  rule_type: "mandatory" | "forbidden";
  title: string;
  content: string;
  priority?: number;
  projects?: string[];
}

export interface BulkAddRuleResult {
  status: string;
  added: number;
  total: number;
  results: Array<{
    slug: string;
    status: string;
    error?: string;
  }>;
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
  pending?: boolean;
  memories: number;
}

/** The original a pending memory was copied from, kept for the adapting agent. */
export interface ImportOrigin {
  project: string;
  memory_id: string;
  title: string;
  content: string;
}

export interface PendingAdaptationsResponse {
  pending: Memory[];
  total: number;
  instructions: string | null;
}

/** asoode's task-state vocabulary, verbatim, so the bridge maps losslessly. */
export type TaskState =
  | "todo"
  | "in_progress"
  | "done"
  | "paused"
  | "blocked"
  | "cancelled"
  | "duplicate"
  | "incomplete"
  | "blocker";

export type TaskCommentKind = "note" | "rule" | "decision" | "reminder";

/** A queued requirement. Stored in its own tables, never in the git snapshot. */
export interface Task {
  id: string;
  title: string;
  description: string | null;
  state: TaskState;
  priority: number;
  assignee: string | null;
  labels: string[];
  due_at: string | null;
  begin_at: string | null;
  end_at: string | null;
  estimated_minutes: number | null;
  parent_id: string | null;
  position: number;
  source: string;
  triage: boolean;
  /** Which session is holding this task; null means free. */
  claimed_by: string | null;
  claimed_at: string | null;
  lease_expires_at: string | null;
  created_at: string | null;
  updated_at: string | null;
  done_at: string | null;
  archived_at: string | null;
}

export interface TaskComment {
  id: string;
  task_id: string;
  body: string;
  kind: string;
  author: string | null;
  created_at: string | null;
}

export interface TaskTimeEntry {
  id: string;
  task_id: string;
  begin_at: string;
  end_at: string | null;
  manual: boolean;
}

export interface TaskDetail {
  task: Task;
  comments: TaskComment[];
  time_entries: TaskTimeEntry[];
  subtasks: Task[];
  minutes_spent: number;
  running: boolean;
}

/** What a list row shows beyond the task's own columns. */
export interface TaskRowMeta {
  comments: number;
  subtasks_total: number;
  subtasks_done: number;
  minutes_spent: number;
  running: boolean;
}

export interface TaskActivityEntry {
  id: number;
  memory_id: string;
  operation: string;
  details: Record<string, unknown> | null;
  actor: string | null;
  created_at: string | null;
}

export interface TaskListResponse {
  tasks: Task[];
  total: number;
  /** How many of the matching tasks are still waiting. */
  open: number;
  /** Ids of tasks with a running clock - not derivable from `state`. */
  running: string[];
  /** Per-row counts, keyed by task id. */
  meta: Record<string, TaskRowMeta>;
}

export interface TaskInput {
  title: string;
  description?: string | null;
  priority?: number;
  labels?: string[];
  assignee?: string | null;
  due_at?: string | null;
  estimated_minutes?: number | null;
  parent_id?: string | null;
  source?: string;
}

export interface TaskUpdate {
  title?: string;
  description?: string | null;
  state?: TaskState;
  priority?: number;
  assignee?: string | null;
  labels?: string[];
  due_at?: string | null;
  begin_at?: string | null;
  end_at?: string | null;
  estimated_minutes?: number | null;
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
