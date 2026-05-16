import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { CornerDownLeft, Search } from "lucide-react";
import { cn } from "../../lib/utils";
import {
  filterCommands,
  type Command,
  type CommandGroup,
} from "../../lib/commands";

export interface CommandPaletteProps {
  open: boolean;
  onClose: () => void;
  commands: Command[];
}

const GROUP_ORDER: CommandGroup[] = [
  "Projects",
  "Create",
  "Navigate",
  "Filters",
  "Actions",
];

export function CommandPalette({
  open,
  onClose,
  commands,
}: CommandPaletteProps) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const filtered = useMemo(
    () => filterCommands(commands, query),
    [commands, query]
  );

  const grouped = useMemo(() => {
    const map = new Map<CommandGroup, Command[]>();
    for (const cmd of filtered) {
      const arr = map.get(cmd.group) ?? [];
      arr.push(cmd);
      map.set(cmd.group, arr);
    }
    const ordered: { group: CommandGroup; items: Command[] }[] = [];
    for (const g of GROUP_ORDER) {
      const items = map.get(g);
      if (items && items.length) ordered.push({ group: g, items });
    }
    return ordered;
  }, [filtered]);

  // flatten for keyboard navigation
  const flat = useMemo(
    () => grouped.flatMap((g) => g.items),
    [grouped]
  );

  useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      const t = window.setTimeout(() => inputRef.current?.focus(), 20);
      return () => window.clearTimeout(t);
    }
  }, [open]);

  useEffect(() => {
    setActive(0);
  }, [query]);

  useEffect(() => {
    if (!open) return;
    const node = listRef.current?.querySelector<HTMLElement>(
      `[data-index="${active}"]`
    );
    node?.scrollIntoView({ block: "nearest" });
  }, [active, open]);

  if (!open) return null;

  function runAt(index: number) {
    const cmd = flat[index];
    if (cmd) {
      onClose();
      cmd.run();
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActive((a) => Math.min(a + 1, flat.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => Math.max(a - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      runAt(active);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  }

  return createPortal(
    <div className="fixed inset-0 z-[90] flex items-start justify-center p-4 pt-[12vh]">
      <div
        className="fixed inset-0 bg-black/60 backdrop-blur-sm animate-fade-in"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        className="relative z-10 w-full max-w-xl overflow-hidden rounded-xl border border-border bg-popover text-popover-foreground shadow-2xl animate-scale-in"
      >
        <div className="flex items-center gap-2 border-b border-border px-3.5">
          <Search className="size-4 shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="Type a command or search…"
            className="h-12 flex-1 bg-transparent text-sm outline-none placeholder:text-muted-foreground"
          />
          <kbd className="rounded border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground">
            ESC
          </kbd>
        </div>
        <div
          ref={listRef}
          className="max-h-[55vh] overflow-y-auto p-2 scrollbar-thin"
        >
          {flat.length === 0 && (
            <div className="px-3 py-8 text-center text-sm text-muted-foreground">
              No commands match “{query}”.
            </div>
          )}
          {grouped.map(({ group, items }) => (
            <div key={group} className="mb-1">
              <div className="px-2 py-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                {group}
              </div>
              {items.map((cmd) => {
                const index = flat.indexOf(cmd);
                const isActive = index === active;
                return (
                  <button
                    key={cmd.id}
                    data-index={index}
                    onMouseMove={() => setActive(index)}
                    onClick={() => runAt(index)}
                    className={cn(
                      "flex w-full items-center justify-between gap-3 rounded-md px-2 py-2 text-left text-sm transition-colors",
                      isActive
                        ? "bg-accent text-accent-foreground"
                        : "text-foreground"
                    )}
                  >
                    <span className="truncate">{cmd.label}</span>
                    <span className="flex items-center gap-2">
                      {cmd.hint && (
                        <span className="truncate text-xs text-muted-foreground">
                          {cmd.hint}
                        </span>
                      )}
                      {isActive && (
                        <CornerDownLeft className="size-3.5 text-muted-foreground" />
                      )}
                    </span>
                  </button>
                );
              })}
            </div>
          ))}
        </div>
      </div>
    </div>,
    document.body
  );
}
