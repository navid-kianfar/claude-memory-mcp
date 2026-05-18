import { useState } from "react";
import { FolderOpen, Loader2 } from "lucide-react";
import { cn } from "../../lib/utils";
import { api } from "../../lib/api";
import { Input } from "./Input";
import { Button } from "./Button";
import { Tooltip } from "./Tooltip";

export interface FolderInputProps {
  value: string;
  onChange: (value: string) => void;
  id?: string;
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  /** optional prompt passed to the native folder picker dialog */
  pickPrompt?: string;
  /** forwarded to the underlying input */
  onKeyDown?: React.KeyboardEventHandler<HTMLInputElement>;
}

/**
 * Controlled absolute-folder-path input with a native folder-picker button.
 * The text field stays fully editable — users can type a path or pick one.
 */
export function FolderInput({
  value,
  onChange,
  id,
  placeholder = "/absolute/path/to/folder",
  disabled,
  className,
  pickPrompt,
  onKeyDown,
}: FolderInputProps) {
  const [picking, setPicking] = useState(false);
  const [unavailable, setUnavailable] = useState(false);

  async function handlePick() {
    if (picking || disabled) return;
    setPicking(true);
    try {
      const res = await api.pickFolder(pickPrompt);
      if (res.status === "ok" && res.path) {
        setUnavailable(false);
        onChange(res.path);
      } else if (res.status === "unavailable") {
        setUnavailable(true);
      }
      // "cancelled" → do nothing
    } catch {
      // network/other errors: stay silent, the path can still be typed
      setUnavailable(true);
    } finally {
      setPicking(false);
    }
  }

  const button = (
    <Button
      type="button"
      variant="outline"
      size="icon"
      disabled={disabled || picking || unavailable}
      onClick={() => void handlePick()}
      aria-label="Browse for folder"
      title={unavailable ? "Folder picker unavailable — type a path" : "Browse"}
    >
      {picking ? (
        <Loader2 className="animate-spin" />
      ) : (
        <FolderOpen />
      )}
    </Button>
  );

  return (
    <div className={cn("flex items-center gap-2", className)}>
      <Input
        id={id}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        className="flex-1 font-mono"
      />
      {unavailable ? (
        <Tooltip content="Folder picker unavailable on this host — type a path">
          {button}
        </Tooltip>
      ) : (
        button
      )}
    </div>
  );
}
