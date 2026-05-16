import { useEffect } from "react";

/** Registers a global hotkey. `combo` example: "mod+k" (mod = cmd/ctrl). */
export function useHotkey(combo: string, handler: (e: KeyboardEvent) => void) {
  useEffect(() => {
    const parts = combo.toLowerCase().split("+");
    const key = parts[parts.length - 1];
    const needMod = parts.includes("mod");
    const needShift = parts.includes("shift");

    const onKey = (e: KeyboardEvent) => {
      const modActive = e.metaKey || e.ctrlKey;
      if (needMod && !modActive) return;
      if (!needMod && modActive) return;
      if (needShift && !e.shiftKey) return;
      if (e.key.toLowerCase() !== key) return;
      handler(e);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [combo, handler]);
}
