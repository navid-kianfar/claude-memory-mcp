import { forwardRef } from "react";
import { FileUp, Plus, RefreshCw, Search, X } from "lucide-react";
import type { Memory, MemoryStatus } from "../types";
import { titleCase } from "../lib/utils";
import { Input } from "./ui/Input";
import { Button } from "./ui/Button";
import { Select } from "./ui/Select";
import { MemoryCard } from "./MemoryCard";

export interface MemoriesTabProps {
  projectSlug: string;
  memories: Memory[];
  total: number;
  mode: "list" | "search";
  loading: boolean;
  error: string | null;
  categories: string[];
  searchInput: string;
  categoryFilter: string;
  statusFilter: MemoryStatus;
  onSearchInputChange: (value: string) => void;
  onSearchSubmit: () => void;
  onClearSearch: () => void;
  onCategoryChange: (value: string) => void;
  onStatusChange: (value: MemoryStatus) => void;
  onNewMemory: () => void;
  onImport: () => void;
  onRefresh: () => void;
  onEdit: (memory: Memory) => void;
  onDelete: (memory: Memory) => void;
}

const STATUS_OPTIONS = [
  { value: "active", label: "Active" },
  { value: "archived", label: "Archived" },
  { value: "all", label: "All statuses" },
];

export const MemoriesTab = forwardRef<HTMLInputElement, MemoriesTabProps>(
  (props, searchRef) => {
    const {
      projectSlug,
      memories,
      total,
      mode,
      loading,
      error,
      categories,
      searchInput,
      categoryFilter,
      statusFilter,
      onSearchInputChange,
      onSearchSubmit,
      onClearSearch,
      onCategoryChange,
      onStatusChange,
      onNewMemory,
      onImport,
      onRefresh,
      onEdit,
      onDelete,
    } = props;

    const categoryOptions = [
      { value: "all", label: "All categories" },
      ...categories.map((c) => ({ value: c, label: titleCase(c) })),
    ];

    return (
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative min-w-[220px] flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              ref={searchRef}
              value={searchInput}
              onChange={(e) => onSearchInputChange(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") onSearchSubmit();
              }}
              placeholder="Semantic search… (press Enter)"
              className="pl-8 pr-8"
            />
            {searchInput && (
              <button
                onClick={onClearSearch}
                aria-label="Clear search"
                className="absolute right-2 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
              >
                <X className="size-4" />
              </button>
            )}
          </div>
          <Select
            options={categoryOptions}
            value={categoryFilter}
            onValueChange={onCategoryChange}
            className="w-44"
          />
          <Select
            options={STATUS_OPTIONS}
            value={statusFilter}
            onValueChange={(v) => onStatusChange(v as MemoryStatus)}
            className="w-40"
          />
          <Button variant="outline" size="icon" onClick={onRefresh}>
            <RefreshCw />
          </Button>
          <Button variant="outline" onClick={onImport}>
            <FileUp />
            Import CLAUDE.md
          </Button>
          <Button onClick={onNewMemory}>
            <Plus />
            New Memory
          </Button>
        </div>

        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            {mode === "search" ? "Search results" : "All memories"} ·{" "}
            {total} item{total === 1 ? "" : "s"}
          </span>
        </div>

        {error && (
          <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        )}

        {loading && (
          <div className="space-y-2">
            {[0, 1, 2].map((i) => (
              <div
                key={i}
                className="h-24 animate-pulse rounded-lg bg-muted"
              />
            ))}
          </div>
        )}

        {!loading && !error && memories.length === 0 && (
          <div className="rounded-lg border border-dashed border-border py-16 text-center">
            <p className="text-sm text-muted-foreground">
              {mode === "search"
                ? "No memories match your search."
                : "No memories yet. Create one to get started."}
            </p>
          </div>
        )}

        {!loading && memories.length > 0 && (
          <div className="space-y-2">
            {memories.map((m) => (
              <MemoryCard
                key={m.id}
                memory={m}
                projectSlug={projectSlug}
                onEdit={onEdit}
                onDelete={onDelete}
              />
            ))}
          </div>
        )}
      </div>
    );
  }
);
MemoriesTab.displayName = "MemoriesTab";
